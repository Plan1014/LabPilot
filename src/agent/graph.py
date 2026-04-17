"""LangGraph Agent — compiled graph for server mode.

langgraph-cli imports this module and reads the `graph` variable to expose
the agent via the LangGraph Studio API.

Note: Server-mode persistence is handled automatically by the langgraph API
platform. Do NOT pass a custom checkpointer here.
"""

from langgraph.prebuilt import create_react_agent

from src.agent.config import WORKDIR
from src.agent.llm import llm
from src.agent.tools import TOOLS, SKILLS


# System prompt used by the server agent
system_prompt = f"""You are a lab agent at {WORKDIR}. Use tools to solve tasks.
Use spawn_subagent for multi-step or isolated work.
Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""


def _build_graph():
    """Build and return the compiled server-mode graph."""
    return create_react_agent(
        model=llm,
        tools=TOOLS,
        prompt=system_prompt,
        # No custom checkpointer — langgraph API handles persistence
        debug=False,
    )


# Module-level graph — langgraph-cli reads this name specifically
graph = _build_graph()
