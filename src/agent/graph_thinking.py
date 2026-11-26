"""LangGraph Agent with thinking block support.

Graph structure:
  generate ──(有 tool_use)──► execute_tools ──► generate
           │
           └──(无 tool_use)──► END

All output (thinking, tool_result, text) happens inside the graph.

Pending / drain 设计 (fix/pending-session-id):
  - state.session_id = None → REPL 模式, _save_to_pending 早返回,不写 pending
  - state.session_id = uuid → WebSocket 模式,pending 全程带 session_id
  - Trigger A (本文件 generate 内): count_by_session(current) >= PENDING_THRESHOLD
    → 后台异步 process_pending_self(current_session_id)
  - Trigger B (websocket_server.py session_query 入口): 异步 process_pending_others(current)
"""

import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Callable, Literal, Optional, Sequence

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, StateGraph, add_messages

from src.agent import tools as tools_mod
from src.agent.ask_user import detect_ask, format_ask_cli, sanitize_ask_message
from src.agent.authz import Verdict, authorize, protect
from src.agent.authz import ledger as authz_ledger
from src.agent.authz import pending as authz_pending
from src.agent.llm import llm
from src.agent.memory import PendingCache, process_pending_self
from src.agent.memory.constants import PENDING_THRESHOLD
from src.agent.prompt_context import strip_labels
from src.agent.tools import TOOLS, is_failure_result


def _ensure_unique_tool_use_ids(messages: list) -> None:
    """保证整个 messages 列表里所有 tool_use id 跨 message 唯一.

    langchain_anthropic 的去重是 per-message (chat_models.py:774-797):
    同一个 AIMessage 的 content 和 tool_calls 内部去重, 但 **不会跨 message 去重**.
    如果两条不同的 AIMessage 共享同一 tool_use id, 序列化给上游 Anthropic
    API 时会收到 400 `duplicate tool_call id`.

    触发场景 (按概率排序):
    1. 模型罕见地给两个不同 tool_use 分配了同一 id
    2. OpenAI/Anthropic 模式混用时, 一边返回的 `call_xxx` id 和另一边返回的
       `toolu_xxx` 都被 normalize 后巧合冲突
    3. session reload 路径 (message_to_dict 未去重) 把同 id 写入多块

    关键不变量: langchain_anthropic 在最终输出里, 每个 AIMessage 最多贡献
    *一个* tool_use 块 per id (content 命中 tool_calls 时替换, 不重复).
    所以跨 message 去重的判据 = "本 message 的 (content tool_use ids ∪ tool_calls
    ids) 与之前已加入 seen 的 ids 是否有交集".

    策略: 先 in-place 修本 message 的 content (per-msg 内去重, 重复的改名);
    再修本 message 的 tool_calls (不去重, 允许 content+tool_calls 共存 ——
    langchain 会内部消解); 最后把本 message 实际产出的 id 集合 (content 的
    最终 id ∪ tool_calls 的最终 id) 加入 seen, 供后续 message 比对.

    rename 时同步更新紧随其后的 ToolMessage.tool_call_id 以及 Anthropic 风格
    content 里的 tool_result.tool_use_id, 保持 tool_use/tool_result 配对.
    """
    seen: set = set()
    pending_renames: dict = {}  # old_id -> new_id, 用完即清
    for msg in messages:
        # 同步 ToolMessage
        if isinstance(msg, ToolMessage):
            if msg.tool_call_id in pending_renames:
                msg.tool_call_id = pending_renames[msg.tool_call_id]
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        if block.get("tool_use_id") in pending_renames:
                            block["tool_use_id"] = pending_renames[block["tool_use_id"]]
            continue

        if not isinstance(msg, AIMessage):
            continue

        # 1) 处理本 message 的 content (per-msg 内去重, 跨 msg 靠 seen)
        own_ids: set = set()  # 本 message 实际产出的 id
        if isinstance(msg.content, list):
            new_content = []
            for block in msg.content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    new_content.append(block)
                    continue
                old_id = block.get("id")
                if old_id is None or old_id in seen or old_id in own_ids:
                    base = old_id or "tool_use"
                    n = 1
                    new_id = f"{base}_dup{n}"
                    while new_id in seen or new_id in own_ids:
                        n += 1
                        new_id = f"{base}_dup{n}"
                    block["id"] = new_id
                    if old_id is not None:
                        pending_renames[old_id] = new_id
                own_ids.add(block["id"])
                new_content.append(block)
            msg.content = new_content

        # 2) 处理本 message 的 tool_calls (不去重 — 允许 content+tool_calls 同 id,
        #    langchain 内部会消解; 只在跨 message 冲突时改名)
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if not isinstance(tc, dict):
                    continue
                old_id = tc.get("id")
                if old_id is None or old_id in seen:
                    base = old_id or "tool_use"
                    n = 1
                    new_id = f"{base}_dup{n}"
                    while new_id in seen or new_id in own_ids:
                        n += 1
                        new_id = f"{base}_dup{n}"
                    tc["id"] = new_id
                    if old_id is not None:
                        pending_renames[old_id] = new_id
                own_ids.add(tc["id"])

        # 3) 把本 message 实际产出的 id 加入 seen, 供后续 message 校验
        seen.update(own_ids)


# ==================== Event Emitter for SSE Streaming ====================

# [Fix #concurrent-emitter] 用 ContextVar 替代模块级全局变量
# asyncio 每个 Task 有独立 context,并发 SSE 请求不会互相覆盖 emitter
_event_emitter: ContextVar[Optional[Callable[[dict], None]]] = ContextVar(
    "_event_emitter", default=None
)


def set_event_emitter(emitter: Optional[Callable[[dict], None]]):
    """Set the per-task event emitter for SSE streaming.

    When set in an async task, print_* functions in that task will call
    emitter(event_dict). Other concurrent tasks have independent emitters.
    When None (default), print_* functions only print to stdout.
    """
    _event_emitter.set(emitter)


# ==================== State ====================

@dataclass
class InterleavedState:
    """State for the thinking-aware graph."""
    messages: Annotated[Sequence[AnyMessage], add_messages] = field(default_factory=list)
    pending_tool_calls: list = field(default_factory=list)  # 临时存储待执行的工具调用
    step_count: int = 0
    session_id: Optional[str] = None  # None = REPL (不写 pending);WebSocket 注入 uuid
    pending_ask: Optional[dict] = None  # 本轮发出的提问；非 None 表示在等用户回答
    pending_auth: Optional[dict] = None  # 本轮挂起的鉴权批次；非 None 表示在等用户裁决


# ==================== Content Block Parsing ====================

def parse_content_blocks(content: any, additional_kwargs: dict | None = None) -> tuple:
    """Parse AIMessage.content into thinking, tool_use, and text blocks.

    Optional ``additional_kwargs`` is consulted for a ``"thinking"`` key — the
    OpenAI-mode wrapper parks reasoning there (instead of polluting
    ``content`` with langchain_anthropic-style blocks, which the OpenAI
    upstream rejects). Anthropic mode leaves ``additional_kwargs`` empty and
    the thinking blocks are read from ``content`` directly.
    """
    thinking_blocks = []
    tool_use_blocks = []
    text_blocks = []

    # OpenAI mode: reasoning was injected into additional_kwargs.thinking by
    # ChatOpenAIWithThinking._inject_thinking. Surface it as a thinking block
    # so the SSE stream and persistence layer see a uniform shape.
    if additional_kwargs:
        t = additional_kwargs.get("thinking")
        if t:
            thinking_blocks.append(t)

    if content is None:
        return thinking_blocks, tool_use_blocks, text_blocks

    if isinstance(content, str):
        if content:
            text_blocks.append(content)
        return thinking_blocks, tool_use_blocks, text_blocks

    if not isinstance(content, list):
        text_blocks.append(str(content))
        return thinking_blocks, tool_use_blocks, text_blocks

    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type == "thinking":
                thinking_blocks.append(block.get("thinking", ""))
            elif block_type == "tool_use":
                tool_use_blocks.append(block)
            elif block_type == "text":
                # `text` may be a string (langchain_anthropic convention) or
                # a list of strings (some OpenAI-mode captures). Flatten both.
                txt = block.get("text", "")
                if isinstance(txt, list):
                    text_blocks.extend(t for t in txt if isinstance(t, str))
                elif txt:
                    text_blocks.append(str(txt))
            else:
                text_blocks.append(str(block))
        elif hasattr(block, "type"):
            block_type = block.type
            if block_type == "thinking":
                thinking_blocks.append(getattr(block, "thinking", ""))
            elif block_type == "tool_use":
                tool_use_blocks.append(block.model_dump() if hasattr(block, "model_dump") else {})
            elif block_type == "text":
                # Same as the dict branch: handle text as string or list[str].
                txt = getattr(block, "text", "")
                if isinstance(txt, list):
                    text_blocks.extend(t for t in txt if isinstance(t, str))
                elif txt:
                    text_blocks.append(str(txt))
            else:
                text_blocks.append(str(block))
        else:
            text_blocks.append(str(block))

    return thinking_blocks, tool_use_blocks, text_blocks


# ==================== Output Helpers ====================

def print_thinking(thinking: str, step: int) -> None:
    """Print thinking block in gray color."""
    if not thinking.strip():
        return
    # [Fix #concurrent-emitter] 读当前 task 的 emitter(每个 asyncio Task 独立 context)
    emitter = _event_emitter.get()
    if emitter:
        emitter({"type": "thinking", "content": thinking, "step": step})
    # Fallback to stdout
    print(f"\033[90m[Thinking]\033[0m", flush=True)
    for line in thinking.split("\n"):
        print(f"  {line}", flush=True)
    print(flush=True)


def print_tool_call(tool_name: str, tool_input: dict, step: int) -> None:
    """Print tool call header with input details."""
    emitter = _event_emitter.get()
    if emitter:
        emitter({
            "type": "tool_call",
            "name": tool_name,
            "input": tool_input,
            "step": step,
        })
    print(f"\n\033[33m[Step {step}] Calling tool: {tool_name}\033[0m", flush=True)
    print(f"  Input: {tool_input}", flush=True)


def print_tool_result(result: str, step: int) -> None:
    """Print tool result in green."""
    emitter = _event_emitter.get()
    if emitter:
        emitter({"type": "tool_result", "result": result, "step": step})
    display = result[:500] + "..." if len(result) > 500 else result
    print(f"\033[32m[Step {step}] Result\033[0m: {display}", flush=True)


def print_final_text(text_blocks: list[str], step: int) -> None:
    """Print final response in green."""
    if not text_blocks:
        return
    emitter = _event_emitter.get()
    if emitter:
        emitter({"type": "text", "content": text_blocks, "step": step})
    print(f"\n\033[92m[Response]\033[0m", flush=True)
    for text in text_blocks:
        print(text, flush=True)
    print(flush=True)


def print_ask(ask: dict, step: int) -> None:
    """Emit the ask event and print the CLI version.

    提问之后本轮即结束 —— 前端据此弹出选项卡片，等待用户回答。
    """
    emitter = _event_emitter.get()
    if emitter:
        emitter({
            "type": "ask_user",
            "question": ask.get("question", ""),
            "options": ask.get("options", []),
            "tool_call_id": ask.get("tool_call_id", ""),
            "step": step,
        })
    print(f"\n\033[93m{format_ask_cli(ask)}\033[0m", flush=True)


# ==================== Helper Functions ====================

_pending_cache: Optional[PendingCache] = None


def _get_pending_cache() -> PendingCache:
    """Lazy init of pending cache"""
    global _pending_cache
    if _pending_cache is None:
        _pending_cache = PendingCache()
    return _pending_cache


def _save_to_pending(messages: list, session_id: Optional[str]) -> None:
    """Save messages to pending cache (session_id is REQUIRED for WebSocket).

    REPL 模式: session_id=None,本函数早返回,不写 pending。
    WebSocket 模式: session_id=uuid,正常写入 pending_cache.db。
    """
    # REPL 旁路: 无 session_id 不写 pending
    if session_id is None:
        return

    # 转换为dict格式以便JSON序列化
    # 注意：state.messages 里已经带了提示词标签（<UserMessage> 等），那些标签是
    # 给主模型看的结构边界，不能流进 pending cache —— memory sub-agent 读到带标签
    # 的文本，会把标签一起当成记忆内容写进长期记忆。
    dict_messages = []
    for msg in messages:
        if hasattr(msg, "content"):
            dict_messages.append({
                "role": getattr(msg, "role", "unknown"),
                "content": strip_labels(getattr(msg, "content", ""))
            })
        elif isinstance(msg, dict):
            row = dict(msg)
            if isinstance(row.get("content"), str):
                row["content"] = strip_labels(row["content"])
            dict_messages.append(row)
    if dict_messages:
        cache = _get_pending_cache()
        cache.save_pending(dict_messages, session_id=session_id)


# ==================== Graph Nodes ====================

def _collect_used_tool_call_ids(messages: list) -> set:
    """历史里已经用过的 tool_call id。"""
    used: set = set()
    for msg in messages or []:
        if isinstance(msg, ToolMessage):
            if msg.tool_call_id:
                used.add(msg.tool_call_id)
            continue
        if not isinstance(msg, AIMessage):
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("id"):
                        used.add(block["id"])
        for tc in getattr(msg, "tool_calls", None) or []:
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tid:
                used.add(tid)
    return used


def _write_back_tool_call_ids(response, tool_use_blocks: list[dict]) -> None:
    """把改过的 id 写回响应本身（content 块与 tool_calls 属性）。

    content 与 tool_calls 是同一批调用的两种表示，两边都要改 —— 只改一边，
    落库时又会按「另一边」的 id 合成出重复块。
    """
    content = getattr(response, "content", None)
    if isinstance(content, list):
        idx = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if idx < len(tool_use_blocks):
                    block["id"] = tool_use_blocks[idx]["id"]
                idx += 1
    tool_calls = getattr(response, "tool_calls", None)
    if isinstance(tool_calls, list):
        for i, tc in enumerate(tool_calls):
            if isinstance(tc, dict) and i < len(tool_use_blocks):
                tc["id"] = tool_use_blocks[i]["id"]


def _ensure_unique_response_tool_call_ids(response, tool_use_blocks: list[dict], messages: list) -> list[dict]:
    """响应里的 tool_use id 不能和历史里用过的重复。

    有些 OpenAI 兼容上游每轮都从 ``call_0`` 开始编号。撞上旧 id 时，
    用户的回答会配到旧的提问上 —— 当前这个提问既不会弹出，回答也送不到
    模型那里，提问静默消失。
    """
    used = _collect_used_tool_call_ids(messages)
    changed = False
    for block in tool_use_blocks:
        tid = block.get("id")
        if tid and tid not in used:
            used.add(tid)
            continue
        block["id"] = f"call_{uuid.uuid4().hex[:12]}"
        used.add(block["id"])
        changed = True
    if changed:
        _write_back_tool_call_ids(response, tool_use_blocks)
    return tool_use_blocks


def generate(state: InterleavedState) -> dict:
    """Call the LLM and parse content blocks into the state.

    Uses llm.bind_tools(TOOLS) for proper tool format handling.
    Prints thinking blocks immediately upon receipt.
    Persists messages to pending cache (WebSocket only).
    Triggers Trigger A when same-session pending count exceeds threshold.
    """
    step = state.step_count + 1
    session_id = getattr(state, "session_id", None)

    # 每次保存消息到pending cache（跨会话持久化）
    # 注意：state.messages 是累积列表，每个 step 都在增长
    # 为避免重复保存，只取该 step 新增的最后一条消息
    # REPL (session_id=None) 时 _save_to_pending 早返回,不写 pending
    if state.messages:
        try:
            last_msg = state.messages[-1]
            _save_to_pending([last_msg], session_id=session_id)
        except Exception as e:
            print(f"\033[94m[Memory Agent]\033[0m Failed to save pending: {e}")

    # Bind tools to the LLM - LangChain handles format conversion
    llm_with_tools = llm.bind_tools(TOOLS)

    # Defensive: ensure all tool_use ids in conversation are unique.
    # langchain_anthropic dedupes per-message but NOT cross-message, so a
    # shared id across two AIMessages would trigger upstream 400
    # `duplicate tool_call id`. In-place mutation of state.messages list.
    _ensure_unique_tool_use_ids(list(state.messages))

    # Invoke the LLM with tools bound
    # LangChain will automatically:
    # 1. Convert messages to API format
    # 2. Convert tools to provider format (Anthropic tool_use or OpenAI function)
    # 3. Parse response and extract tool calls
    response = llm_with_tools.invoke(state.messages)

    # OpenAI mode: tool_calls is an attribute on AIMessage, not a content block.
    # Anthropic mode: tool_use blocks live inside response.content (handled by parse_content_blocks).
    openai_tool_calls = getattr(response, "tool_calls", None) or []
    response_kwargs = getattr(response, "additional_kwargs", None) or {}
    if openai_tool_calls:
        # Authoritative source: response.tool_calls (OpenAI mode contract).
        tool_use_blocks = []
        for tc in openai_tool_calls:
            tc_id = tc.get("id") if isinstance(tc, dict) else tc.id
            tc_name = tc.get("name") if isinstance(tc, dict) else tc.name
            tc_args = tc.get("args") if isinstance(tc, dict) else tc.args
            if isinstance(tc_args, str):
                import json as _json
                try:
                    tc_args = _json.loads(tc_args)
                except Exception:
                    tc_args = {}
            tool_use_blocks.append({"id": tc_id, "name": tc_name, "input": tc_args or {}})
        # OpenAI mode: response.content is a plain string (post-fix), with
        # reasoning parked in additional_kwargs.thinking. Anthropic mode
        # (langchain_anthropic) returns content as a list of blocks. parse_content_blocks
        # handles both shapes.
        thinking_blocks, _content_tool_use_ignored, text_blocks = parse_content_blocks(
            response.content, response_kwargs
        )
    else:
        # Parse content blocks (Anthropic mode primary path)
        thinking_blocks, tool_use_blocks, text_blocks = parse_content_blocks(
            response.content, response_kwargs
        )

    # Print thinking immediately
    for thinking in thinking_blocks:
        print_thinking(thinking, step)

    # 响应里的 tool_use id 不能撞上历史里用过的 id —— 撞上了用户的回答就
    # 配不到当前这个提问上。
    if tool_use_blocks:
        tool_use_blocks = _ensure_unique_response_tool_call_ids(
            response, tool_use_blocks, list(state.messages)
        )

    # 提问即终止本轮：模型调用 ask_user 后，agent loop 必须停下等用户回答，
    # 不能继续执行其它工具。响应里的兄弟 tool_use 一并丢弃 —— 它们没有对应的
    # tool_result，留在历史里会让下一轮上游 API 报 400。
    ask = detect_ask(tool_use_blocks)
    if ask:
        response = sanitize_ask_message(response, ask)
        print_final_text(text_blocks, step)
        print_ask(ask, step)
        return {
            "messages": [response],
            "pending_tool_calls": [],
            "step_count": step,
            "pending_ask": ask,
        }

    # 如果没有工具调用，打印最终文本
    if not tool_use_blocks:
        print_final_text(text_blocks, step)

    # Trigger A: 同 session 阈值检查 (仅 WebSocket, REPL session_id=None 跳过)
    # 阈值触发时,后台异步 drain 本 session 的 pending (single-flight 锁保证并发安全)
    # 注意: 这里用的是 singleton _pending_cache,绝对不能 close (否则后续 _save_to_pending 全失败)
    # drain 线程内部 process_pending_self 自己 new PendingCache() 用完 close,与 singleton 解耦
    if session_id is not None and not tool_use_blocks:
        try:
            cache = _get_pending_cache()
            count = cache.count_by_session(session_id)
            if count >= PENDING_THRESHOLD:
                print(f"\033[94m[Memory Agent]\033[0m Trigger A: pending={count} >= {PENDING_THRESHOLD}, spawn drain for session={session_id[:8]}")
                thread = threading.Thread(
                    target=process_pending_self,
                    args=(session_id,),
                    daemon=True,
                )
                thread.start()
        except Exception as e:
            print(f"\033[94m[Memory Agent]\033[0m Trigger A check failed: {e}")

    return {
        "messages": [response],  # LangChain AIMessage already properly formatted
        "pending_tool_calls": tool_use_blocks,
        "step_count": step,
    }


def _snapshot_protected() -> Path | None:
    """给 bash 执行拍一张受保护区的快照。拍不了就返回 None（不挡工具执行）。"""
    try:
        return protect.snapshot(tools_mod.WORKDIR)
    except OSError:
        return None


def _apply_protection(snapshot_dir, result_str: str) -> str:
    """比对快照与现状，把越界的改动还原掉，并把这次调用判为失败。

    还原报告拼在原始输出前面 —— 主 agent 要先看到「保护区被动过」，
    再看到命令本身说了什么。
    """
    try:
        report = protect.diff_and_restore(snapshot_dir, tools_mod.WORKDIR)
    except OSError:
        return result_str
    if not report["restored"]:
        return result_str
    return (
        "Error: 隔离兜底触发：命令改动了受保护区域，已还原。\n"
        f"{protect.format_report(report)}\n\n"
        f"命令原始输出：\n{result_str}"
    )


def run_single_tool(tool_name: str, tool_input: dict) -> str:
    """执行一个工具，返回结果字符串。找不到工具 → "Error: Unknown tool 'x'"。

    工具执行只有这一处实现 —— 图里的 `execute_tools` 和提权恢复都用它，
    两边跑出来的结果必须一模一样。
    """
    tool_map = {}
    for t in TOOLS:
        if hasattr(t, "name"):
            tool_map[t.name] = t
        elif hasattr(t, "__name__"):
            tool_map[t.__name__] = t

    tool = tool_map.get(tool_name)
    if tool is None:
        result = f"Error: Unknown tool '{tool_name}'"
        result_str = str(result)
    else:
        # bash 前后拍快照 —— 隔离兜底（spec §5.6）。
        # background=True 的 bash 不走这条路（Popen 立刻返回，还原跑完后台
        # 进程还没动文件），那一类由 L2 判官强制挡。
        snapshot_dir = None
        if tool_name == "bash" and not (tool_input or {}).get("background"):
            snapshot_dir = _snapshot_protected()
        try:
            try:
                result = tool.invoke(tool_input or {})
            except Exception as e:
                result = f"Error: {e}"
            result_str = str(result)
            if snapshot_dir is not None:
                result_str = _apply_protection(snapshot_dir, result_str)
        finally:
            # 快照比对完就没用了 —— 不删的话每次 bash 都在 tmp/_protect/ 里
            # 留一份受保护区的副本，跑几十次就是几百 MB。
            if snapshot_dir is not None:
                protect.cleanup(snapshot_dir)

    # 轻量触发：当工具返回失败时，提示模型记录
    if is_failure_result(result_str):
        result_str += "\n\n[System note] It seems there has been an error. Please immediately use the memory tool to record this mistake. Before responding to the user, you need to review this error and ensure it has been recorded."
    return result_str


def execute_tools(state: InterleavedState) -> dict:
    """Execute pending tool calls and return results."""
    tool_messages = []

    for tool_use in state.pending_tool_calls:
        if isinstance(tool_use, dict):
            tool_name = tool_use.get("name", "")
            tool_id = tool_use.get("id", "")
            # Streaming LLM may provide input in partial_json instead of input
            raw_input = tool_use.get("input", {})
            if not raw_input and tool_use.get("partial_json"):
                import json
                try:
                    raw_input = json.loads(tool_use["partial_json"])
                except Exception:
                    raw_input = {}
        elif hasattr(tool_use, "partial_json"):
            tool_name = getattr(tool_use, "name", "?")
            tool_id = getattr(tool_use, "id", "")
            import json
            try:
                raw_input = json.loads(tool_use.partial_json)
            except Exception:
                raw_input = {}
        else:
            tool_name = getattr(tool_use, "name", "?")
            tool_id = getattr(tool_use, "id", "")
            raw_input = getattr(tool_use, "input", {})

        print_tool_call(tool_name, raw_input, state.step_count)

        result_str = run_single_tool(tool_name, raw_input)

        print_tool_result(result_str, state.step_count)

        tool_msg = ToolMessage(
            content=result_str,
            name=tool_name,
            tool_call_id=tool_id,
        )
        tool_messages.append(tool_msg)

    return {
        "messages": tool_messages,
        "pending_tool_calls": [],  # 清空，已执行完毕
    }


def _route(state: InterleavedState) -> Literal["execute_tools", "done"]:
    """Route based on pending tool calls."""
    if state.pending_tool_calls:
        return "execute_tools"
    else:
        return "done"


# ==================== Authorization Node ====================

def _tool_call_fields(tool_use) -> tuple[str, str, dict]:
    """从一条 tool_use 里取 (tool_call_id, tool_name, tool_input)。"""
    if isinstance(tool_use, dict):
        tool_id = tool_use.get("id") or ""
        tool_name = tool_use.get("name") or ""
        raw_input = tool_use.get("input") or {}
        if not raw_input and tool_use.get("partial_json"):
            import json
            try:
                raw_input = json.loads(tool_use["partial_json"])
            except Exception:
                raw_input = {}
    else:
        tool_id = getattr(tool_use, "id", "") or ""
        tool_name = getattr(tool_use, "name", "") or ""
        raw_input = getattr(tool_use, "input", {}) or {}
    if not isinstance(raw_input, dict):
        raw_input = {}
    return tool_id, tool_name, raw_input


def build_auth_request_event(v: Verdict, tool_call_id: str, batch_id: str) -> dict:
    """构造 SSE 的 auth_request 事件。"""
    return {
        "type": "auth_request",
        "tool_call_id": tool_call_id,
        "question": v.question or v.reason,
        "reason": v.reason,
        "pattern": v.pattern,
        "batch_id": batch_id,
        "step": 0,
    }


def _prompt_cli_decision(v: Verdict) -> str:
    """REPL 里问用户一个操作要不要执行。返回 allow_once / allow_always / deny。

    非法输入按「拒绝」处理 —— 宁可少做一次，不可误做一次。
    """
    question = v.question or v.reason or "是否执行这个操作？"
    lines = [
        f"\n\033[93m[待批准] {question}\033[0m",
        f"  理由：{v.reason}",
        "  1. 允许一次",
        "  2. 允许并记住",
        "  3. 拒绝",
        "  → 输入 1/2/3：",
    ]
    print("\n".join(lines), flush=True)
    try:
        raw = input().strip()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    if raw == "1":
        return "allow_once"
    if raw == "2":
        return "allow_always"
    return "deny"


def authorize_node(state: InterleavedState) -> dict:
    """裁决整批 pending_tool_calls。

    批次原子性：只要有一个调用 escalate，**整批挂起**，本轮不执行任何工具
    —— 一个 tool_use 缺了配对的 tool_result，上游 API 会直接报 400。
    """
    calls = list(state.pending_tool_calls or [])
    if not calls:
        return {"pending_tool_calls": [], "pending_auth": None}

    session_id = getattr(state, "session_id", None)
    verdicts: list[dict] = []
    for call in calls:
        tool_id, tool_name, tool_input = _tool_call_fields(call)
        v = authorize(tool_name, tool_input, session_id=session_id)
        verdicts.append(
            {
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "verdict": v,
            }
        )

    escalations = [x for x in verdicts if x["verdict"].decision == "escalate"]

    # ---- REPL：没有 session_id → 直接在终端里问，批内当场解决 ----
    if escalations and session_id is None:
        for item in escalations:
            choice = _prompt_cli_decision(item["verdict"])
            if choice == "deny":
                item["verdict"] = Verdict(
                    "deny", item["verdict"].layer, "用户拒绝了这个操作"
                )
                continue
            if choice == "allow_always":
                pattern = item["verdict"].pattern
                if pattern:
                    authz_ledger.record_grant(pattern)
            item["verdict"] = Verdict(
                "allow", item["verdict"].layer, item["verdict"].reason
            )
        escalations = []

    # ---- 有 escalate → 整批挂起 ----
    if escalations:
        batch_id = authz_pending.save_batch(
            session_id,
            [
                {
                    "tool_call_id": x["tool_call_id"],
                    "tool_name": x["tool_name"],
                    "tool_input": x["tool_input"],
                    "verdict": x["verdict"].decision,
                    "reason": x["verdict"].reason,
                    "pattern": x["verdict"].pattern,
                }
                for x in verdicts
            ],
            [
                {
                    "tool_call_id": x["tool_call_id"],
                    "tool_name": x["tool_name"],
                    "tool_input": x["tool_input"],
                    "question": x["verdict"].question or x["verdict"].reason,
                    "reason": x["verdict"].reason,
                    "pattern": x["verdict"].pattern,
                }
                for x in escalations
            ],
        )
        emitter = _event_emitter.get()
        for item in escalations:
            event = build_auth_request_event(
                item["verdict"], item["tool_call_id"], batch_id
            )
            if emitter:
                emitter(event)
            print(
                f"\n\033[93m[待批准] {event['question']}\033[0m",
                flush=True,
            )
        return {
            "messages": [],
            "pending_tool_calls": [],
            "pending_auth": {
                "batch_id": batch_id,
                "session_id": session_id,
                "pending": [
                    {
                        "tool_call_id": x["tool_call_id"],
                        "tool_name": x["tool_name"],
                        "question": x["verdict"].question or x["verdict"].reason,
                        "reason": x["verdict"].reason,
                        "pattern": x["verdict"].pattern,
                    }
                    for x in escalations
                ],
            },
        }

    # ---- 没有 escalate → allow 的留下执行，deny 的当场产拒绝说明 ----
    messages: list = []
    allowed: list = []
    for item, call in zip(verdicts, calls):
        v = item["verdict"]
        if v.decision == "deny":
            messages.append(
                ToolMessage(
                    content=f"授权拒绝：{v.reason}",
                    name=item["tool_name"],
                    tool_call_id=item["tool_call_id"],
                )
            )
        else:
            allowed.append(call)

    return {
        "messages": messages,
        "pending_tool_calls": allowed,
        "pending_auth": None,
    }


def _route_after_authorize(state: InterleavedState) -> Literal["execute_tools", "generate", "done"]:
    """authorize 之后的路由。

    - 有挂起的鉴权批次 → 本轮结束（等用户裁决）。
    - 还有放行的调用 → 去执行。
    - 全部被拒 → 回 generate，让模型看到拒绝说明并给用户一个交代。
    """
    if getattr(state, "pending_auth", None):
        return "done"
    if state.pending_tool_calls:
        return "execute_tools"
    return "generate"


# ==================== Graph Factory ====================

def build_graph() -> StateGraph:
    """Build and return the thinking-aware LangGraph.

    Graph structure:
      generate ──(pending_tool_calls?)──► authorize ──► execute_tools ──► generate
                ──(else)──► END                    └──(escalate)──► END

    `authorize` 必须在 `execute_tools` 之前 —— 工具执行之前先过闸门。
    """
    builder = StateGraph(InterleavedState, input=InterleavedState, output=InterleavedState)

    # Add nodes
    builder.add_node("generate", generate)
    builder.add_node("authorize", authorize_node)
    builder.add_node("execute_tools", execute_tools)

    # Edges
    builder.add_conditional_edges("generate", _route, {
        "execute_tools": "authorize",
        "done": END,
    })
    builder.add_conditional_edges("authorize", _route_after_authorize, {
        "execute_tools": "execute_tools",
        "generate": "generate",
        "done": END,
    })
    builder.add_edge("execute_tools", "generate")

    # Set entry point
    builder.set_entry_point("generate")

    return builder.compile()


# Module-level graph instance
graph = build_graph()
