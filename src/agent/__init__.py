"""LabPilot Agent package — exported for langgraph-cli and REPL.

Graph export:
    langgraph.json references "./src/agent/__init__.py:graph"
    so langgraph-cli imports this module and reads the `graph` variable.
"""

from src.agent.graph import graph
from src.agent.tools import TOOLS, SKILLS, SkillLoader
from src.agent.llm import llm, client
from src.agent.config import WORKDIR, MODEL_ID, TOKEN_THRESHOLD

__all__ = [
    "graph",
    "TOOLS",
    "SKILLS",
    "SkillLoader",
    "llm",
    "client",
    "WORKDIR",
    "MODEL_ID",
    "TOKEN_THRESHOLD",
]
