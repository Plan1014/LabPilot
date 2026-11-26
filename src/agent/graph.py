"""LangGraph Agent — compiled graph for server mode.

langgraph-cli imports this module and reads the `graph` variable to expose
the agent via the LangGraph Studio API.

Note: Server-mode persistence is handled automatically by the langgraph API
platform. Do NOT pass a custom checkpointer here.
"""

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from src.agent.authz import wrap_tools_with_gate
from src.agent.llm import llm
from src.agent.prompt_context import build_system_prompt
from src.agent.tools import TOOLS_FOR_REACT, SKILLS


def dynamic_system_prompt(state):
    """每次调用时重新生成系统提示词。

    系统提示词里有 "Current time" —— 冻结在 import 那一刻的话，
    server 模式跑久了模型拿到的时间就是错的。
    """
    if isinstance(state, dict):
        messages = list(state.get("messages") or [])
    else:
        messages = list(getattr(state, "messages", None) or [])
    system = SystemMessage(
        content=build_system_prompt(TOOLS_FOR_REACT, SKILLS.descriptions())
    )
    return [system] + messages


def _build_graph():
    """Build and return the compiled server-mode graph."""
    return create_react_agent(
        model=llm,
        # TOOLS_FOR_REACT 不含 ask_user —— 这条路径会真的执行工具，
        # 而提问必须由 graph_thinking 的 agent loop 拦截后停下等用户回答。
        #
        # 套一层闸门：这条路径没有人工审批界面，判官判 escalate 只能
        # fail-closed 转成拒绝。TOOLS_FOR_REACT 本身不动 —— 别处还在用。
        tools=wrap_tools_with_gate(TOOLS_FOR_REACT),
        prompt=dynamic_system_prompt,
        # No custom checkpointer — langgraph API handles persistence
        debug=False,
    )


# Module-level graph — langgraph-cli reads this name specifically
graph = _build_graph()
