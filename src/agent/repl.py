"""REPL interaction layer for LabPilot LangGraph Agent.

This module provides the REPL mode for terminal-based interaction.
It creates its own agent instance (without checkpointer) and manages
conversation history manually.
"""

import json
import time
from typing import Any, Dict

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.prebuilt import create_react_agent

from src.agent.config import WORKDIR, TRANSCRIPT_DIR, TOKEN_THRESHOLD, MODEL_ID, WEBSOCKET_ENABLED
from src.agent.llm import llm, client
from src.agent.tools import TOOLS, SKILLS


# ==================== ReAct Tracing Callback ====================

class ToolCallbackHandler(BaseCallbackHandler):
    """ReAct loop tracer — prints each tool call and result with step numbers."""

    step_count: int = 0
    tool_call_sequence: list = []  # Track tool call sequence for loop detection

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        self.step_count += 1
        tool_name = serialized.get("name", serialized.get("id", "?"))
        self.tool_call_sequence.append(tool_name)
        display_input = input_str[:500] + "..." if len(input_str) > 500 else input_str
        print(f"\n\033[33m[Step {self.step_count}] Calling tool: {tool_name}\033[0m")
        if display_input:
            print(f"  Input: {display_input}")

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            output_str = str(output.content) if hasattr(output, "content") else str(output)
        except Exception:
            output_str = str(output)
        display_output = output_str[:300] + "..." if len(output_str) > 300 else output_str
        print(f"\033[32m[Step {self.step_count}] Result\033[0m: {display_output}")

    def reset(self) -> None:
        self.step_count = 0
        self.tool_call_sequence = []

    def get_recent_tool_sequence(self, n: int = 6) -> list:
        """Get the last n tool calls for loop detection."""
        return self.tool_call_sequence[-n:]


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


def detect_tool_loop_from_history(history: list) -> tuple:
    """Detect tool loops from conversation history.

    Returns (warning, block):
    - warning: warning message if 3+ consecutive identical calls detected, else ""
    - block: block message if 6+ consecutive identical calls detected, else ""

    The order is: if block is triggered, return only block (not warning).
    """
    # Extract tool calls from history
    tool_calls = []
    for msg in history:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls.append(block.get("name", ""))

    if len(tool_calls) < 3:
        return ("", "")

    # Check for 6+ consecutive identical tool calls (block)
    if len(tool_calls) >= 6:
        last_6 = tool_calls[-6:]
        if all(t == last_6[0] for t in last_6):
            return ("", LOOP_BLOCK_PROMPT)

    # Check for 3+ consecutive identical tool calls (warning)
    last_3 = tool_calls[-3:]
    if all(t == last_3[0] for t in last_3):
        return (LOOP_WARNING_PROMPT, "")

    return ("", "")


# ==================== Context Compression ====================

def estimate_tokens(messages: list) -> int:
    """Rough token estimation for a message list."""
    return len(json.dumps(messages, default=str)) // 4


def microcompact(messages: list) -> None:
    """Light compression: keep last 3 tool results, mark older ones as [cleared]."""
    indices = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    indices.append(part)
    if len(indices) <= 3:
        return
    for part in indices[:-3]:
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            part["content"] = "[cleared]"


def auto_compact(messages: list) -> list:
    """Heavy compression: save full history to file, replace with LLM summary."""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")

    conv_text = json.dumps(messages, default=str)[-80000:]
    resp = client.messages.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000,
    )
    summary = resp.content[0].text
    return [{"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"}]


# ==================== REPL Agent Factory ====================

def create_repl_agent():
    """Create the REPL-mode agent (no checkpointer, manual history)."""
    system_prompt = f"""You are a lab agent at {WORKDIR}. Use tools to solve tasks.
Use spawn_subagent for multi-step or isolated work.
Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""

    return create_react_agent(
        model=llm,
        tools=TOOLS,
        prompt=system_prompt,
        checkpointer=None,  # REPL manages history manually
        debug=False,
    )


# ==================== Response Parsing ====================

def parse_and_print_response(response_messages: list) -> None:
    """Extract thinking and text blocks from response and print them."""
    for msg in reversed(response_messages):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            thinking_parts = []
            text_parts = []

            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "thinking":
                            thinking_parts.append(block.get("thinking", ""))
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
            else:
                text_parts.append(str(content))

            # Print thinking first (in gray)
            if thinking_parts:
                print("\033[90m[Thinking]\033[0m")
                for t in thinking_parts:
                    print(f"  {t}")
                print()

            # Then print text
            for t in text_parts:
                print(t)
            break


# ==================== REPL Main ====================

def main() -> None:
    """Run the REPL interaction loop."""
    # Start WebSocket server for Linien GUI notifications
    if WEBSOCKET_ENABLED:
        from src.agent.websocket_server import (
            start_websocket_server_thread,
            get_notification_queue,
            WEBSOCKET_PORT,
        )
        start_websocket_server_thread(WEBSOCKET_PORT)
        notification_queue = get_notification_queue()

    agent = create_repl_agent()

    print(f"LabPilot LangGraph Agent (model: {MODEL_ID})")
    print(f"Workspace: {WORKDIR}")
    print("Commands: /compact (compact), /history (show), /help, q (quit)")
    print()

    history = []

    def run_agent_query(query: str):
        """Run a single agent query. Called by user input or WebSocket notifications."""
        nonlocal history

        # Add user message to history
        history.append({"role": "user", "content": query})

        # Microcompact check
        microcompact(history)

        # Auto-compact check
        if estimate_tokens(history) > TOKEN_THRESHOLD:
            print("[auto-compact triggered]")
            history = auto_compact(history)

        # Detect tool loops - check for warning and block
        loop_warning, loop_block = detect_tool_loop_from_history(history)

        # If block triggered (6+ identical calls), skip invoke
        if loop_block:
            print(f"\033[31m{loop_block}\033[0m")
            # Clear the problematic history entry
            history = [{"role": "user", "content": "Please respond with a brief status summary."}]
            return

        # If warning triggered (3+ identical calls), inject warning before invoke
        if loop_warning:
            # Find position to insert warning (before last assistant message)
            history.insert(-1, {"role": "user", "content": loop_warning})
            print(f"\033[33m{loop_warning}\033[0m")

        # Invoke agent
        try:
            langchain_messages = [
                HumanMessage(content=m["content"]) if isinstance(m, dict) else m
                for m in history
            ]

            callback = ToolCallbackHandler()
            result = agent.invoke(
                {"messages": langchain_messages},
                config={"max_iterations": 100, "max_tokens": 8000, "callbacks": [callback]},
            )
            response_messages = result.get("messages", [])

            # Update history with full message list
            history = [
                {"role": "user", "content": langchain_messages[0].content}
            ]

            parse_and_print_response(response_messages)

        except Exception as e:
            print(f"Error: {e}")

        print()

    # Register trigger callback with notification queue
    if WEBSOCKET_ENABLED:
        notification_queue.set_trigger_callback(run_agent_query)
        notification_queue.set_idle(True)  # Start in idle state

    while True:
        # Mark as idle before waiting for input (enables WebSocket-triggered processing)
        if WEBSOCKET_ENABLED:
            notification_queue.set_idle(True)

        try:
            query = input("\033[36mLabPilot >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        # Mark as busy when starting to process
        if WEBSOCKET_ENABLED:
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

        if query.strip() == "/history":
            print(f"[history: {len(history)} messages]")
            for i, m in enumerate(history):
                role = m.get("role", "?")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = str(content)[:100]
                print(f"  [{i}] {role}: {content[:80]}...")
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
