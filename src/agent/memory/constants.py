"""Core Memory 常量定义"""

# 标签白名单 - Agent 只能操作这些标签
LABEL_WHITELIST = frozenset([
    "task",
    "task/running",
    "conclusion",
    "conclusion/parameter",
    "conclusion/technique",
    "persona",
    "error",
])

# Block 字符上限
CORE_MEMORY_BLOCK_CHAR_LIMIT = 4000

# 数据库路径（运行时由 CoreMemoryManager 设置）
BLOCKS_DB_PATH = None

# Pending Cache 触发阈值 (Trigger A): 同 session 的 pending 累积条数达到此值时异步 drain
PENDING_THRESHOLD = 30
