"""Core Memory + Archival Memory 模块"""

from src.agent.memory.core import CoreMemoryManager, Block, BlockHistory
from src.agent.memory.memory_agent import memory_agent_summarize, process_pending_cache, process_pending_self, process_pending_others
from src.agent.memory.pending import PendingCache

# 全局单例
core_memory_manager: CoreMemoryManager = None

def get_core_memory_manager() -> CoreMemoryManager:
    global core_memory_manager
    if core_memory_manager is None:
        core_memory_manager = CoreMemoryManager()
    return core_memory_manager

# Archival Memory 兼容导入
from src.agent.memory.archival import (
    memory_system,
    memory_retriever,
    search_sessions,
    list_all_facts,
    list_all_summaries,
    delete_fact,
    delete_summary,
)

__all__ = [
    "CoreMemoryManager",
    "Block",
    "BlockHistory",
    "get_core_memory_manager",
    "core_memory_manager",
    "memory_system",
    "memory_retriever",
    "search_sessions",
    "list_all_facts",
    "list_all_summaries",
    "delete_fact",
    "delete_summary",
    "memory_agent_summarize",
    "process_pending_cache",
    "process_pending_self",
    "process_pending_others",
    "PendingCache",
]