"""LangGraph Agent with thinking block support.

Graph structure:
  generate ──(有 tool_use)──► execute_tools ──► generate
           │
           └──(无 tool_use)──► END

All output (thinking, tool_result, text) happens inside the graph.
"""

import threading
from dataclasses import dataclass, field
from typing import Annotated, Literal, Optional, Sequence, Callable

from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END, add_messages

from src.agent.llm import llm
from src.agent.tools import TOOLS


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


# ==================== Graph Nodes ====================

def generate(state: InterleavedState) -> dict:
    """Call the LLM and parse content blocks into the state.

    Uses llm.bind_tools(TOOLS) for proper tool format handling.
    Prints thinking blocks immediately upon receipt.
    """
    step = state.step_count + 1

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

        print_tool_result(str(result), state.step_count)

        tool_msg = ToolMessage(
            content=str(result),
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
                ──(else)──► done (END)
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
