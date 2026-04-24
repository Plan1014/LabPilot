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

# Thresholds
TOKEN_THRESHOLD = 100000

# NotificationHub port
NOTIFICATION_HUB_PORT = int(os.getenv("NOTIFICATION_HUB_PORT", "8000"))
NOTIFICATION_HUB_ENABLED = os.getenv("NOTIFICATION_HUB_ENABLED", "true").lower() == "true"
