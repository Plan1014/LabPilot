"""工作区的路径区域策略 —— 唯一知道「哪里能写」的地方。

放在 `src/agent/` 顶层而不是 `src/agent/authz/` 里，是因为 `tools.py`（bash 工具
自己）和 `authz/*`（闸门层）都要用同一份定义，而 `authz/*` 又要 import `tools.py`
（D13：bash 的命令级判定住在 tools.py）。区域定义若放在 authz 包里，
`tools.py → authz → tools` 就成了循环依赖。

三层区域：
- 可写区 `tmp/` `data/` —— agent 可以随便写。
- 只读保护区 `src/` `skills/` `instrument/` `tests/` `frontend/src/` `docs/`
  以及若干根文件 —— 改动它们等于改 LabPilot 自身，一律拒绝。
- 其余（仓库里新加的目录、WORKDIR 之外的路径）—— 保守起见按只读处理。

原则：**解析不出来就判不可写**（fail-closed）。宁可漏判一次合法写入，
也不放过一次越界写入。

**基准要解开符号链接**：checkout 可能是个符号链接（Windows 上是 junction），
而路径解析（`Path.resolve()`）会把它解开。两边形态不一致时
`is_relative_to` 恒 False —— 所有路径都被判成 outside，`write_file` /
`edit_file` 全被 L1 拒（假阳性：用户根本没法写文件）。所以比路径时
一律用 `_base()`（解开后的工作区），不是 `WORKDIR` 本身。
"""

from pathlib import Path

from src.agent.config import WORKDIR as _CONFIG_WORKDIR

# 模块级再存一份，让测试能 monkeypatch 替换成 tmp_path
WORKDIR: Path = _CONFIG_WORKDIR

# agent 可以写入的目录
WRITABLE_DIRS: tuple[str, ...] = ("tmp", "data")

# 只读保护区（仓库自身）
PROTECTED_DIRS: tuple[str, ...] = (
    "src",
    "skills",
    "instrument",
    "tests",
    "frontend/src",
    "docs",
)

# 只读保护区里的根级文件（不是目录，要精确匹配）
PROTECTED_ROOT_FILES: tuple[str, ...] = (
    ".env",
    "pyproject.toml",
    "langgraph.json",
    "start-dev.ps1",
    "README.md",
)


def _base() -> Path:
    """工作区基准 —— 解开符号链接 / junction 之后的 WORKDIR。

    checkout 可能是个链接（Windows 上是 junction）。路径解析
    （`Path.resolve()`）会把它解开，所以比路径时两边都得是解开后的
    形态 —— 拿没解开的 WORKDIR 去比，`is_relative_to` 恒 False，
    所有路径都判成 outside，`write_file` / `edit_file` 全被 L1 拒
    （假阳性：用户根本没法写文件）。
    """
    try:
        return WORKDIR.resolve()
    except (OSError, RuntimeError):
        return WORKDIR


def resolve_in_workdir(raw: str) -> Path | None:
    """把 raw 解析成 WORKDIR 内的绝对路径。

    绝对路径不在 WORKDIR 之内、含 `..` 逃逸、或解析抛错 → None（拿不准就不判）。
    """
    if not raw:
        return None
    base = _base()
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        if not p.is_relative_to(base):
            return None
    except ValueError:  # 老版本 Python 没有 is_relative_to
        if not str(p).startswith(str(base)):
            return None
    return p


def relative_path_of(resolved: Path) -> str | None:
    """已解析的路径相对工作区的正斜杠形式；不在工作区内 → None。"""
    try:
        return Path(resolved).relative_to(_base()).as_posix()
    except ValueError:
        return None


def relative_to_workdir(raw: str) -> str | None:
    """raw 相对工作区的正斜杠形式；解析不出或在工作区外 → None。

    「允许并记住」的模式串、授权台账的匹配都靠它 —— 推不出相对路径
    就等于「记住」失灵（授权记下来了，下次却不命中）。
    """
    p = resolve_in_workdir(raw)
    if p is None:
        return None
    return relative_path_of(p)


def is_writable_path(raw: str) -> bool:
    """解析后落在 tmp/ 或 data/ 之内 → True；其余（含解析失败）→ False。"""
    p = resolve_in_workdir(raw)
    if p is None:
        return False
    base = _base()
    return any(p.is_relative_to(base / d) for d in WRITABLE_DIRS)


def is_protected_path(raw: str) -> bool:
    """解析后落在 PROTECTED_DIRS 或 PROTECTED_ROOT_FILES 之内 → True。"""
    p = resolve_in_workdir(raw)
    if p is None:
        return False
    base = _base()
    for d in PROTECTED_DIRS:
        if p.is_relative_to(base / d):
            return True
    for name in PROTECTED_ROOT_FILES:
        if p == base / name:
            return True
    return False


def classify_target(raw: str) -> str:
    """返回 "writable" | "repo_readonly" | "outside"。

    解析不出（在 WORKDIR 外）→ "outside"。注意 "outside" 不等于禁止写 ——
    WORKDIR 之外的路径由 bash 工具的其它规则与判官去管，这里只是分类。
    """
    p = resolve_in_workdir(raw)
    if p is None:
        return "outside"
    if is_writable_path(raw):
        return "writable"
    return "repo_readonly"
