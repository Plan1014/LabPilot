#!/usr/bin/env python3
"""LabPilot LangGraph Agent — REPL entry point.

This is a thin wrapper that delegates to src.agent.repl.main().
For server mode, use: langgraph-cli dev --config langgraph.json
"""

from src.agent.repl import main

if __name__ == "__main__":
    main()
