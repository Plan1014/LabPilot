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

# WebSocket server configuration
WEBSOCKET_PORT = int(os.getenv("WEBSOCKET_PORT", "8001"))
WEBSOCKET_ENABLED = os.getenv("WEBSOCKET_ENABLED", "true").lower() == "true"
