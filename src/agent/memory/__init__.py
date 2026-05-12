"""Core Memory 模块 - Block 体系热点记忆"""

from src.agent.memory.core import CoreMemoryManager

# 全局单例，延迟初始化
core_memory_manager: CoreMemoryManager = None

def get_core_memory_manager() -> CoreMemoryManager:
    global core_memory_manager
    if core_memory_manager is None:
        core_memory_manager = CoreMemoryManager()
    return core_memory_manager
