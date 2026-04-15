"""State definitions for LabPilot LangGraph Agent.

Note: create_react_agent manages its own internal state (messages, is_last_step).
This module is provided for extensibility — add custom state fields here if needed.
"""

from typing import Sequence
from dataclasses import dataclass, field

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from langgraph.managed import IsLastStep
from typing_extensions import Annotated


# Minimal input state — mirrors create_react_agent's expected input
@dataclass
class InputState:
    """Input state for the agent."""
    messages: Annotated[Sequence[AnyMessage], add_messages] = field(default_factory=list)


@dataclass
class State(InputState):
    """Full agent state — extends InputState with managed fields."""
    is_last_step: IsLastStep = field(default=False)
