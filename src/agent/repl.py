"""REPL interaction layer for LabPilot LangGraph Agent.

Graph 内部负责所有输出（thinking、tool_result、text）。
REPL 只负责调用 graph 并管理对话历史。
"""

import json
import time
from typing import Any, Dict, Union

from langgraph.graph import END

from src.agent.config import WORKDIR, TRANSCRIPT_DIR, TOKEN_THRESHOLD, MODEL_ID, NOTIFICATION_HUB_ENABLED
from src.agent.llm import summarize_text
from src.agent.session_manager import message_to_dict
from src.agent.tools import TOOLS, SKILLS
from src.agent.graph_thinking import build_graph
from src.agent.ask_user import build_turn_row, find_pending_ask

from src.agent.memory import memory_system


def append_user_turn(history: list[dict], query: str) -> dict:
    """把一行用户输入追加进历史，返回追加的那一行。

    有待答提问时，这一行输入是对上一问的回答 —— 必须作为 tool_result 回填，
    模型才能从 tool_use 的断点继续；否则就是普通用户消息。
    """
    row = build_turn_row(find_pending_ask(history), query)
    history.append(row)
    return row


def can_set_idle(history: list[dict]) -> bool:
    """现在是否可以把通知队列置为 idle。

    有待答提问时不能置 idle —— 通知会触发 agent 跑一轮，那段通知文本会被
    当成「用户对上一问的回答」吞掉。
    """
    return find_pending_ask(history) is None


def should_compact(history: list[dict], turn_row: dict, threshold: int | None = None) -> bool:
    """这一轮要不要触发自动压缩。

    回答轮不压缩：auto_compact 会把整个 history 换成一行摘要，提问的
    tool_use 和刚记下的 tool_result 双双消失 —— 模型这一轮根本看不到
    用户的回答。
    """
    if isinstance(turn_row, dict) and turn_row.get("role") == "tool":
        return False
    limit = TOKEN_THRESHOLD if threshold is None else threshold
    return estimate_tokens(history) > limit

# ==================== Loop Detection ====================

LOOP_WARNING_PROMPT = """
[Warning] Repetitive tool usage detected (3+ consecutive identical calls).
Before continuing, carefully review:
1. Is the previous tool call actually necessary?
2. Is there a bug in the parameters being passed?
3. Should you report the current status to the user instead?

Do NOT repeat the same tool call again without addressing these questions.
"""

LOOP_BLOCK_PROMPT = """
[Blocked] Repetitive tool usage detected (6+ consecutive identical calls).
The agent is stuck in a loop and cannot continue.
Please report this situation to the user and ask for clarification.
"""


def detect_tool_loop(history: list) -> tuple:
    """Detect tool loops from history.

    Detects 6+ consecutive identical tool calls with the same parameters.
    Returns (warning, block):
    - warning: warning message if 3+ consecutive identical calls detected, else ""
    - block: block message if 6+ consecutive identical calls detected, else ""
    """
    # Collect tool calls with their full parameters
    tool_calls_with_params = []
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        # OpenAI mode: tool_calls is a top-level field on the assistant dict.
        for tc in msg.get("tool_calls") or []:
            args = tc.get("args", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = args
            tool_calls_with_params.append((tc.get("name", ""), args))
        # Anthropic mode: tool_use blocks live inside content.
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls_with_params.append((
                        block.get("name", ""),
                        block.get("input", {}),
                    ))

    if len(tool_calls_with_params) < 3:
        return ("", "")

    # Check for 6+ consecutive identical tool calls (block)
    if len(tool_calls_with_params) >= 6:
        last_6 = tool_calls_with_params[-6:]
        if all(t == last_6[0] for t in last_6):
            tool_name = last_6[0][0]
            return ("", LOOP_BLOCK_PROMPT.replace("6+ consecutive identical calls", f"6+ identical calls of {tool_name}"))

    # Check for 3+ consecutive identical tool calls (warning)
    last_3 = tool_calls_with_params[-3:]
    if all(t == last_3[0] for t in last_3):
        tool_name = last_3[0][0]
        return (LOOP_WARNING_PROMPT.replace("3+ consecutive identical calls", f"3+ identical calls of {tool_name}"), "")

    return ("", "")


# ==================== Context Compression ====================

def estimate_tokens(messages: list) -> int:
    """Rough token estimation for a message list."""
    # Convert all messages to dicts before serializing
    dicts = [message_to_dict(m) for m in messages]
    return len(json.dumps(dicts, default=str)) // 4


def auto_compact(messages: list) -> list:
    """Save full history to file, replace with LLM summary."""
    # Convert all messages to dicts first
    dicts = [message_to_dict(m) for m in messages]

    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in dicts:
            f.write(json.dumps(msg, default=str) + "\n")

    conv_text = json.dumps(dicts, default=str)[-80000:]
    summary = summarize_text(f"Summarize for continuity:\n{conv_text}", max_tokens=2000)

    # [新增]将真正的摘要持久化到 ChromaDB
    memory_system.save_summary(summary, filepath=str(path))
    # =====================================
    # origin=system —— 这行是压缩摘要，不是用户说的话。
    return [{"role": "user", "origin": "system",
             "content": f"[Compressed. Transcript: {path}]\n{summary}"}]


# ==================== REPL Main ====================

def main() -> None:
    """Run the REPL interaction loop."""
    # Start NotificationHub on port 8000
    if NOTIFICATION_HUB_ENABLED:
        from src.agent.websocket_server import (
            start_notification_hub_thread,
            get_notification_queue,
            NOTIFICATION_HUB_PORT,
        )
        start_notification_hub_thread(NOTIFICATION_HUB_PORT)
        notification_queue = get_notification_queue()

    graph = build_graph()

    print(f"LabPilot LangGraph Agent (model: {MODEL_ID})")
    print(f"Workspace: {WORKDIR}")
    print("Commands: /compact (compact), /history (show), /tools, q (quit)")
    print()

    # 对话历史：完整累积 messages
    history: list[dict] = []

    def run_agent_query(query: str) -> None:
        """Run a single agent query."""
        nonlocal history

        # === Core Memory 注入（缓存失效策略）===
        from src.agent.memory.core import CoreMemoryManager

        # 延迟初始化 CoreMemoryManager（在函数属性上缓存）
        if not hasattr(run_agent_query, '_core_memory_manager'):
            run_agent_query._core_memory_manager = CoreMemoryManager()
            run_agent_query._last_memory_str = ""

        core_memory_manager = run_agent_query._core_memory_manager

        # 编译当前 memory
        new_memory_str = core_memory_manager.compile()

        # 缓存失效检测：只有变化时才更新
        if new_memory_str != run_agent_query._last_memory_str:
            run_agent_query._last_memory_str = new_memory_str

        # 保存 compiled memory 供后续使用
        run_agent_query._compiled_memory = new_memory_str
        # === Core Memory 注入结束 ===

        # 添加用户消息到历史（有待答提问时，这一行就是对上一问的回答）
        turn_row = append_user_turn(history, query)

        # 检测工具循环
        loop_warning, loop_block = detect_tool_loop(history)

        # 如果 6+ 循环，直接阻断
        if loop_block:
            print(f"\033[31m{loop_block}\033[0m")
            history = [{"role": "user", "origin": "system",
                        "content": "Please respond with a brief status summary."}]
            return

        # 如果 3+ 循环，注入警告
        if loop_warning:
            history.append({"role": "user", "origin": "system", "content": loop_warning})

        # 自动压缩检查（回答轮不压缩 —— 会把刚记下的提问与回答一起抹掉）
        if should_compact(history, turn_row):
            print("[auto-compact triggered]", flush=True)
            history = auto_compact(history)

        # 转换为 LangChain messages
        from src.agent.prompt_context import build_prompt_messages
        from src.agent.tools import SKILLS, TOOLS

        # 注入 Core Memory 作为 system message（只有变化时才注入）
        compiled_memory = getattr(run_agent_query, '_compiled_memory', '')

        # 统一拼装：系统提示词（动态工具索引）+ Core Memory + 历史/当前轮，
        # 每条都带来源标签。REPL 不做检索注入（设计上不新增噪音源）。
        # history 里已经含当前这轮（append_user_turn 在前面执行过）。
        langchain_messages = build_prompt_messages(
            history=list(history),
            core_memory=compiled_memory,
            retrieved=None,
            tools=TOOLS,
            skills=SKILLS.descriptions(),
        )

        # PROMPT_DEBUG=1 —— 把这一轮完整输入打到后端终端
        from src.agent.prompt_debug import maybe_dump
        maybe_dump(langchain_messages, {
            "session_id": None,
            "query": query,
            "tool_count": len(TOOLS),
            "history_rows": len(history),
        })

        # 调用 graph
        try:
            result = graph.invoke(
                {"messages": langchain_messages},
                config={"recursion_limit": 100},
            )

            # 从 result 中提取完整的 messages 列表
            result_messages = result.get("messages", [])

            # 累积到 history（确保所有消息都是可序列化的 dict）
            # 偏移按「本次实际送进 graph 的消息条数」切：result_messages 前面
            # 还带着 system message，用 len(history) 会多切掉同样数量的历史消息。
            for msg in result_messages[len(langchain_messages):]:
                history.append(message_to_dict(msg))

        except Exception as e:
            print(f"Error: {e}")

        print()

    # 注册 WebSocket 回调
    if NOTIFICATION_HUB_ENABLED:
        notification_queue.set_trigger_callback(run_agent_query)
        notification_queue.set_idle(True)

    while True:
        # 有待答提问时保持 idle=False —— 通知会被排队而不是立刻触发 agent，
        # 否则通知文本会被当成「用户对上一问的回答」吞掉。
        if NOTIFICATION_HUB_ENABLED and can_set_idle(history):
            notification_queue.set_idle(True)

        try:
            query = input("\033[36mLabPilot >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if NOTIFICATION_HUB_ENABLED:
            notification_queue.set_idle(False)

        if query.strip().lower() in ("q", "exit", ""):
            break

        if query.strip() == "/help":
            print("Available tools:")
            for t in TOOLS:
                first_line = t.description.split("\n")[0]
                print(f"  - {t.name}: {first_line}")
            print()
            continue

        if query.strip() == "/tools":
            print("Available tools:")
            for t in TOOLS:
                print(f"  - {t.name}")
            print()
            continue

        if query.strip() == "/history":
            print(f"[history: {len(history)} messages]")
            for i, m in enumerate(history):
                role = m.get("role", "?")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = str(content)[:100]
                elif isinstance(content, str):
                    content = content[:80]
                print(f"  [{i}] {role}: {content}...")
            print()
            continue

        if query.strip() == "/compact":
            if history:
                history = auto_compact(history)
                print(f"[compressed to {len(history)} messages]")
            else:
                print("No history to compact")
            continue

        run_agent_query(query)


if __name__ == "__main__":
    main()