"""Archival Memory 兼容层

指向现有的 memory.py 迁移到 memory/archival_store.py 实现。
"""

# 直接导入，因为 archival_store.py 在 memory 包内，不会触发 __init__.py 的循环导入
from src.agent.memory.archival_store import (
    memory_system,
    memory_retriever,
    search_sessions,
    # save_fact 是 memory_system 的方法，不是独立函数
# memory_system.save_fact(...) 调用即可
    list_all_facts,
    list_all_summaries,
    delete_fact,
    delete_summary,
)

__all__ = [
    "memory_system",
    "memory_retriever",
    "search_sessions",
    "list_all_facts",
    "list_all_summaries",
    "delete_fact",
    "delete_summary",
]