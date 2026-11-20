"""LabPilot Agent package — exported for langgraph-cli and REPL.

Graph export:
    langgraph.json references "./src/agent/__init__.py:graph"
    so langgraph-cli imports this module and reads the `graph` variable.

惰性导出（PEP 562）：
    顶层 import 只装映射表，不真正加载子模块。`src.agent.graph` 会连带拉起
    LLM / 向量库（chromadb + sentence-transformers），冷启动约 50 秒 ——
    任何只想 import 一个轻量子模块的地方（比如测试）都不该为此买单。
    `from src.agent import graph` 之类的用法保持不变。
"""

import importlib
from typing import Any

# 名字 -> (子模块, 子模块里的属性)
_LAZY_EXPORTS = {
    "graph": ("src.agent.graph", "graph"),
    "TOOLS": ("src.agent.tools", "TOOLS"),
    "SKILLS": ("src.agent.tools", "SKILLS"),
    "SkillLoader": ("src.agent.tools", "SkillLoader"),
    "llm": ("src.agent.llm", "llm"),
    "WORKDIR": ("src.agent.config", "WORKDIR"),
    "MODEL_ID": ("src.agent.config", "MODEL_ID"),
    "TOKEN_THRESHOLD": ("src.agent.config", "TOKEN_THRESHOLD"),
}

__all__ = [
    "graph",
    "TOOLS",
    "SKILLS",
    "SkillLoader",
    "llm",
    "WORKDIR",
    "MODEL_ID",
    "TOKEN_THRESHOLD",
]


def __getattr__(name: str) -> Any:
    """PEP 562 惰性属性加载。"""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value  # 缓存，后续访问直接命中
    return value


def __dir__() -> list:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
