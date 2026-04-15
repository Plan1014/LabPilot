"""LLM initialization for LabPilot LangGraph Agent."""

import os

from anthropic import Anthropic
from langchain_anthropic import ChatAnthropic

from src.agent.config import MODEL_ID

# Anthropic API credentials from environment
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL")

# LangChain chat model (used by create_react_agent)
llm = ChatAnthropic(
    model=MODEL_ID,
    anthropic_api_key=ANTHROPIC_API_KEY,
    base_url=ANTHROPIC_BASE_URL or None,
)

# Raw Anthropic client (used by auto_compact for summarization)
client = Anthropic(base_url=ANTHROPIC_BASE_URL)
