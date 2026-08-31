"""REPL interaction layer for LabPilot LangGraph Agent.

Graph 内部负责所有输出（thinking、tool_result、text）。
REPL 只负责调用 graph 并管理对话历史。
"""

import json
import time
from typing import Any, Dict, Union

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langgraph.graph import END

from src.agent.config import WORKDIR, TRANSCRIPT_DIR, TOKEN_THRESHOLD, MODEL_ID, NOTIFICATION_HUB_ENABLED, SYSTEM_PROMPT_TEMPLATE
from src.agent.llm import client
from src.agent.tools import TOOLS, SKILLS
from src.agent.graph_thinking import build_graph

from src.agent.memory import memory_system

# ==================== Message Serialization ====================

def message_to_dict(msg: Union[HumanMessage, AIMessage, ToolMessage, dict]) -> dict:
    """Convert a message to a JSON-serializable dict.

    Handles potential Pydantic models or other non-serializable objects.
    """
    if isinstance(msg, dict):
        return msg
    elif isinstance(msg, HumanMessage):
        return {"role": "user", "content": msg.content}
    elif isinstance(msg, AIMessage):
        content = msg.content
        if isinstance(content, str):
            return {"role": "assistant", "content": content}
        else:
            # content is a list of blocks (thinking, tool_use, text, etc.)
            # Use a safe serializer for complex content
            try:
                json.dumps(content)
                return {"role": "assistant", "content": content}
            except (TypeError, ValueError):
                # If content is not JSON serializable, convert to string
                return {"role": "assistant", "content": str(content)}
    elif isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "name": msg.name,
            "content": msg.content,
            "tool_call_id": msg.tool_call_id,
        }
    else:
        return {"role": "unknown", "content": str(msg)}


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
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        # Store (tool_name, input_params) tuple
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
    resp = client.messages.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000,
    )
    # summary = resp.content[0].text if hasattr(resp.content[0], 'text') else str(resp.content[0])
    # [新增优化]：正确提取真正的文本摘要，跳过 Thinking 块
    summary = ""
    if isinstance(resp.content, list):
        for block in resp.content:
            # 兼容带有思考块的模型 (Anthropic / MiniMax 等)
            if hasattr(block, "type") and block.type == "text":
                summary = block.text
                break
            elif isinstance(block, dict) and block.get("type") == "text":
                summary = block.get("text", "")
                break
        
        # 如果没找到类型为 text 的块，则 fallback 取最后一个块（通常最后才是结论）
        if not summary and resp.content:
            last_block = resp.content[-1]
            summary = getattr(last_block, "text", str(last_block))
    else:
        summary = str(resp.content)

    # [新增]将真正的摘要持久化到 ChromaDB
    memory_system.save_summary(summary, filepath=str(path))
    # =====================================
    return [{"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"}]


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
        from langchain_core.messages import SystemMessage

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

        # 添加用户消息到历史
        history.append({"role": "user", "content": query})

        # 检测工具循环
        loop_warning, loop_block = detect_tool_loop(history)

        # 如果 6+ 循环，直接阻断
        if loop_block:
            print(f"\033[31m{loop_block}\033[0m")
            history = [{"role": "user", "content": "Please respond with a brief status summary."}]
            return

        # 如果 3+ 循环，注入警告
        if loop_warning:
            history.append({"role": "user", "content": loop_warning})

        # 自动压缩检查
        if estimate_tokens(history) > TOKEN_THRESHOLD:
            print("[auto-compact triggered]", flush=True)
            history = auto_compact(history)

        # 转换为 LangChain messages
        langchain_messages = []

        # Base system prompt (Skills descriptions — LangChain doesn't auto-inject these)
        base_system = SYSTEM_PROMPT_TEMPLATE.format(
            workdir=WORKDIR,
            skills=SKILLS.descriptions(),
        )
        langchain_messages.append(SystemMessage(content=base_system))

        # 注入 Core Memory 作为 system message（只有变化时才注入）
        compiled_memory = getattr(run_agent_query, '_compiled_memory', '')
        if compiled_memory:
            langchain_messages.append(SystemMessage(content=compiled_memory))

        for msg in history:
            if isinstance(msg, HumanMessage):
                langchain_messages.append(msg)
            elif isinstance(msg, AIMessage):
                langchain_messages.append(msg)
            elif isinstance(msg, ToolMessage):
                langchain_messages.append(msg)
            elif isinstance(msg, dict):
                if msg.get("role") == "user":
                    langchain_messages.append(HumanMessage(content=msg.get("content", "")))
                elif msg.get("role") == "assistant":
                    # 构建带 content 的 AIMessage
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        langchain_messages.append(AIMessage(content=content))
                    else:
                        langchain_messages.append(AIMessage(content=content))
                elif msg.get("role") == "tool":
                    langchain_messages.append(ToolMessage(
                        content=msg.get("content", ""),
                        name=msg.get("name", ""),
                        tool_call_id=msg.get("tool_call_id", ""),
                    ))

        # 调用 graph
        try:
            result = graph.invoke(
                {"messages": langchain_messages},
                config={"recursion_limit": 100},
            )

            # 从 result 中提取完整的 messages 列表
            result_messages = result.get("messages", [])

            # 累积到 history（确保所有消息都是可序列化的 dict）
            for msg in result_messages[len(history):]:
                history.append(message_to_dict(msg))

        except Exception as e:
            print(f"Error: {e}")

        print()

    # 注册 WebSocket 回调
    if NOTIFICATION_HUB_ENABLED:
        notification_queue.set_trigger_callback(run_agent_query)
        notification_queue.set_idle(True)

    while True:
        if NOTIFICATION_HUB_ENABLED:
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