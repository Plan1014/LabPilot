"""LangGraph Agent — compiled graph for server mode.

langgraph-cli imports this module and reads the `graph` variable to expose
the agent via the LangGraph Studio API.

Note: Server-mode persistence is handled automatically by the langgraph API
platform. Do NOT pass a custom checkpointer here.
"""

from langgraph.prebuilt import create_react_agent

from src.agent.config import WORKDIR
from src.agent.llm import llm
from src.agent.tools import TOOLS_FOR_REACT, SKILLS


# System prompt used by the server agent
system_prompt = f"""You are a lab agent at {WORKDIR}. Use tools to solve tasks.
# Use spawn_subagent for multi-step or isolated work.  # 临时禁用 2026-08-28
Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""


def _build_graph():
    """Build and return the compiled server-mode graph."""
    return create_react_agent(
        model=llm,
        # TOOLS_FOR_REACT 不含 ask_user —— 这条路径会真的执行工具，
        # 而提问必须由 graph_thinking 的 agent loop 拦截后停下等用户回答。
        tools=TOOLS_FOR_REACT,
        prompt=system_prompt,
        # No custom checkpointer — langgraph API handles persistence
        debug=False,
    )


# Module-level graph — langgraph-cli reads this name specifically
graph = _build_graph()
