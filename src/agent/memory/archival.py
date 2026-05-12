"""Archival Memory 兼容层

指向现有的 memory.py 实现，保持向后兼容。
未来可将 src/agent/memory.py 重命名为 archival.py。
"""

# Re-export all Archival Memory symbols from the original memory module
from src.agent.memory import (
    memory_system,
    memory_retriever,
    search_sessions,
    list_all_facts,
    list_all_summaries,
    delete_fact,
    delete_summary,
)

# Note: save_fact is a method on memory_system.save_fact()
#       remember_fact and search_memory are in tools.py (Agent tools)

__all__ = [
    "memory_system",
    "memory_retriever",
    "search_sessions",
    "list_all_facts",
    "list_all_summaries",
    "delete_fact",
    "delete_summary",
]