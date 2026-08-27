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
from dataclasses import dataclass, field
from typing import Annotated, Literal, Optional, Sequence, Callable

from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END, add_messages

from src.agent.llm import llm
from src.agent.tools import TOOLS, is_failure_result
from src.agent.memory import process_pending_self, PendingCache
from src.agent.memory.constants import PENDING_THRESHOLD


# ==================== Event Emitter for SSE Streaming ====================

_event_emitter: Optional[Callable[[dict], None]] = None
_emitter_lock = threading.RLock()


def set_event_emitter(emitter: Optional[Callable[[dict], None]]):
    """Set the global event emitter for SSE streaming.

    When set, print_* functions will also call emitter(event_dict).
    When None (default), print_* functions only print to stdout.
    """
    global _event_emitter
    with _emitter_lock:
        _event_emitter = emitter


# ==================== State ====================

@dataclass
class InterleavedState:
    """State for the thinking-aware graph."""
    messages: Annotated[Sequence[AnyMessage], add_messages] = field(default_factory=list)
    pending_tool_calls: list = field(default_factory=list)  # 临时存储待执行的工具调用
    step_count: int = 0
    session_id: Optional[str] = None  # None = REPL (不写 pending);WebSocket 注入 uuid


# ==================== Content Block Parsing ====================

def parse_content_blocks(content: any) -> tuple:
    """Parse AIMessage.content into thinking, tool_use, and text blocks."""
    thinking_blocks = []
    tool_use_blocks = []
    text_blocks = []

    if content is None:
        return thinking_blocks, tool_use_blocks, text_blocks

    if isinstance(content, str):
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
                text_blocks.append(block.get("text", ""))
            else:
                text_blocks.append(str(block))
        elif hasattr(block, "type"):
            block_type = block.type
            if block_type == "thinking":
                thinking_blocks.append(getattr(block, "thinking", ""))
            elif block_type == "tool_use":
                tool_use_blocks.append(block.model_dump() if hasattr(block, "model_dump") else {})
            elif block_type == "text":
                text_blocks.append(getattr(block, "text", ""))
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
    # Emit via callback if registered
    with _emitter_lock:
        if _event_emitter:
            _event_emitter({"type": "thinking", "content": thinking, "step": step})
    # Fallback to stdout
    print(f"\033[90m[Thinking]\033[0m", flush=True)
    for line in thinking.split("\n"):
        print(f"  {line}", flush=True)
    print(flush=True)


def print_tool_call(tool_name: str, tool_input: dict, step: int) -> None:
    """Print tool call header with input details."""
    with _emitter_lock:
        if _event_emitter:
            _event_emitter({
                "type": "tool_call",
                "name": tool_name,
                "input": tool_input,
                "step": step,
            })
    print(f"\n\033[33m[Step {step}] Calling tool: {tool_name}\033[0m", flush=True)
    print(f"  Input: {tool_input}", flush=True)


def print_tool_result(result: str, step: int) -> None:
    """Print tool result in green."""
    with _emitter_lock:
        if _event_emitter:
            _event_emitter({"type": "tool_result", "result": result, "step": step})
    display = result[:500] + "..." if len(result) > 500 else result
    print(f"\033[32m[Step {step}] Result\033[0m: {display}", flush=True)


def print_final_text(text_blocks: list[str], step: int) -> None:
    """Print final response in green."""
    if not text_blocks:
        return
    with _emitter_lock:
        if _event_emitter:
            _event_emitter({"type": "text", "content": text_blocks, "step": step})
    print(f"\n\033[92m[Response]\033[0m", flush=True)
    for text in text_blocks:
        print(text, flush=True)
    print(flush=True)


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
    dict_messages = []
    for msg in messages:
        if hasattr(msg, "content"):
            dict_messages.append({
                "role": getattr(msg, "role", "unknown"),
                "content": getattr(msg, "content", "")
            })
        elif isinstance(msg, dict):
            dict_messages.append(msg)
    if dict_messages:
        cache = _get_pending_cache()
        cache.save_pending(dict_messages, session_id=session_id)


# ==================== Graph Nodes ====================

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

    # Invoke the LLM with tools bound
    # LangChain will automatically:
    # 1. Convert messages to API format
    # 2. Convert tools to Anthropic format
    # 3. Parse response and extract tool_use blocks
    response = llm_with_tools.invoke(state.messages)

    # response is an AIMessage with content containing blocks
    content = response.content

    # Parse content blocks
    thinking_blocks, tool_use_blocks, text_blocks = parse_content_blocks(content)

    # Print thinking immediately
    for thinking in thinking_blocks:
        print_thinking(thinking, step)

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


def execute_tools(state: InterleavedState) -> dict:
    """Execute pending tool calls and return results."""
    tool_messages = []

    # Build a name->tool map
    tool_map = {}
    for t in TOOLS:
        if hasattr(t, "name"):
            tool_map[t.name] = t
        elif hasattr(t, "__name__"):
            tool_map[t.__name__] = t

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

        tool = tool_map.get(tool_name)
        if tool is None:
            result = f"Error: Unknown tool '{tool_name}'"
            print(f"  Error: {result}", flush=True)
        else:
            try:
                result = tool.invoke(raw_input)
            except Exception as e:
                result = f"Error: {e}"

        # 轻量触发：当工具返回失败时，提示模型记录
        result_str = str(result)
        if is_failure_result(result_str):
            result_str += "\n\n[System note] It seems there has been an error. Please immediately use the memory tool to record this mistake. Before responding to the user, you need to review this error and ensure it has been recorded."

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


# ==================== Graph Factory ====================

def build_graph() -> StateGraph:
    """Build and return the thinking-aware LangGraph.

    Graph structure:
      generate ──(pending_tool_calls?)──► execute_tools ──► generate
                ──(else)──► END
    """
    builder = StateGraph(InterleavedState, input=InterleavedState, output=InterleavedState)

    # Add nodes
    builder.add_node("generate", generate)
    builder.add_node("execute_tools", execute_tools)

    # Edges
    builder.add_conditional_edges("generate", _route, {
        "execute_tools": "execute_tools",
        "done": END,
    })
    builder.add_edge("execute_tools", "generate")

    # Set entry point
    builder.set_entry_point("generate")

    return builder.compile()


# Module-level graph instance
graph = build_graph()
