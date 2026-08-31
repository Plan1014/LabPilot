"""Configuration for LabPilot LangGraph Agent."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file (override existing env vars)
load_dotenv(override=True)

# Working directory
WORKDIR = Path.cwd()

# Model configuration — from environment, with fallback
MODEL_ID = os.getenv("MODEL_ID", "claude-sonnet-4-20250514")

# Paths
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
SESSIONS_DIR = WORKDIR / "data" / "sessions"

# Thresholds
TOKEN_THRESHOLD = int(os.getenv("TOKEN_THRESHOLD", "100000"))
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

# NotificationHub port
NOTIFICATION_HUB_PORT = int(os.getenv("NOTIFICATION_HUB_PORT", "8000"))
NOTIFICATION_HUB_ENABLED = os.getenv("NOTIFICATION_HUB_ENABLED", "true").lower() == "true"

# System prompt template (Skills descriptions injected at runtime)
SYSTEM_PROMPT_TEMPLATE = """
You are a lab agent at {workdir}, you respond directly to the user when your immediate context (core memory and files)
  contain all the information required to respond.
  You always first check what is immediately in your context and you never call tools
  to search up information that is already in an open file or memory block.
  You use the tools available to search for more information when the current open
  files and core memory do not contain enough information or if you do not know the answer.
Place all temporary scripts under tmp/.\nSkills: {skills}
"""
