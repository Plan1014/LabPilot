"""Tool definitions for LabPilot LangGraph Agent.

All tools are @tool-decorated functions exposed to the LangGraph agent.
"""

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Callable, List

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.agent import paths
from src.agent.config import WORKDIR, SKILLS_DIR
from src.agent.memory.core import CoreMemoryManager


# ==================== Dangerous Command Guard ====================
# All patterns are anchored at word boundaries so that legitimate names like
# `mytaskkill.exe` or `shutdown_check.ps1` are NOT falsely blocked.
# Matching is case-insensitive (re.IGNORECASE) — Windows built-ins are
# case-insensitive on the command line.
_DANGEROUS_PATTERNS = [
    # ---- Linux / Unix file deletion (any form) ----
    r"\brm\s+",                              # rm / rm -rf / rm file
    r"\brmdir\s+",                           # rmdir (POSIX)
    r"\b(sudo|shutdown|reboot|halt|poweroff)\b",
    # ---- Windows file deletion (any form) ----
    r"\bdel\s+",                             # del / del /F /S /Q / del file
    r"\berase\s+",                           # DOS legacy
    r"\b(rd|Remove-Item|ri)\s+",             # rd / rmdir / PowerShell alias + 短名
    # ---- Windows disk / boot / privilege ----
    r"\b(format|diskpart)\b",               # Windows format / diskpart
    r"\bbcdedit\b",                          # boot config editor
    # ---- Process termination (agent 常见误用) ----
    r"\btaskkill\b",                          # 任意 taskkill（含 /F）都拦
    r"\bkill\s+-[9]\b",                      # kill -9 / SIGKILL
    r"\bkillall\b",
    # ---- Windows service / registry ----
    r"\bnet\s+(stop|start)\b",
    r"\bsc\s+(stop|delete)\b",
    r"\breg\s+delete\b",
    # ---- Generic output sink ----
    r">\s*/dev/",
]

_DANGEROUS_REGEX = re.compile("|".join(_DANGEROUS_PATTERNS), re.IGNORECASE)


def _is_dangerous_command(command: str) -> bool:
    """Return True if `command` matches a known destructive pattern.

    Called by the ``bash`` tool before subprocess execution.
    Word-boundary anchored and case-insensitive.
    """
    if not command:
        return False
    return bool(_DANGEROUS_REGEX.search(command))


def _dangerous_match(command: str) -> str:
    """Return the substring that triggered the dangerous guard (for error reporting).

    If no pattern matches, returns "".
    """
    if not command:
        return ""
    m = _DANGEROUS_REGEX.search(command)
    return m.group(0) if m else ""


# ==================== bash 命令级判定 ====================
# 这些判定住在 tools.py 而不是鉴权层（spec D13）：bash 是高自由度工具，命令语义
# 的判断必须和工具执行待在一起 —— 工具自己在执行前就会拒，闸门被绕过也照样生效。
#
# 两条铁律：
#   1. 只解析「写动词的目标参数」，绝不扫全文找路径 —— 否则 `echo "会改 src/x.py"`
#      这种字面量会造成误伤。
#   2. 只允许漏判，不允许错判。解析不出的形态（python -c 里拼的路径、含变量的
#      路径、sh -c 套娃）一律判「没命中」放行，交给上层判官与快照兜底。

# 只读命令表 —— 这些命令只看不改，agent 用 bash 读源码是合法退路，绝不能拦
READ_ONLY_EXECUTABLES: frozenset[str] = frozenset({
    "cat", "head", "tail", "less", "more", "wc", "ls", "dir", "stat", "file",
    "du", "df", "grep", "rg", "find", "tree", "diff", "cmp", "md5sum",
    "sha256sum", "echo", "type", "which", "pwd", "date",
})

# 重定向运算符：`>` `>>` `1>` `2>` `&>`
_REDIRECT_OP_RE = re.compile(r"^(?:\d*>>?|&>)$")

# 会改动文件的命令 → 目标参数取哪几个
#   "all"  = 全部位置参数（rm / mv —— mv 的源也算，把仓库里的文件搬走同样是破坏）
#   "last" = 最后一个位置参数（cp / sed -i / patch —— 只有目标是写）
#   "first"= 第一个位置参数（truncate / chmod）
_WRITE_VERB_TARGETS: dict[str, str] = {
    "tee": "all",
    "mv": "all",
    "rm": "all",
    "rmdir": "all",
    "del": "all",
    "erase": "all",
    "remove-item": "all",
    "cp": "last",
    "sed": "last",
    "patch": "last",
    "rsync": "last",
    "scp": "last",
    "install": "last",
    "truncate": "first",
    "chmod": "first",
    "chown": "first",
    "mkdir": "first",
    "touch": "all",
}


def split_bash_segments(command: str) -> list[str]:
    """把命令按 `&&` `||` `;` `|` 换行拆成子命令。

    引号内的分隔符不拆。空段丢弃。
    """
    if not command:
        return []
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "&" and i + 1 < n and command[i + 1] == "&":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|" and i + 1 < n and command[i + 1] == "|":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ("|", ";", "\n"):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _tokenize(segment: str) -> list[str]:
    """把一段命令切成 token。

    - 引号内的空格不分、引号本身去掉（`"a b"` → `a b` 一个 token）
    - 重定向运算符单独成 token（`>` `>>` `1>` `2>` `&>`）
    - 引号内的 `>` 不当运算符（`find . -name "x>y"` 不该被当成重定向）
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None

    def flush() -> None:
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch.isspace():
            flush()
            i += 1
            continue
        if ch == ">":
            op = ">"
            j = i + 1
            if j < n and segment[j] == ">":
                op = ">>"
                j += 1
            prefix = "".join(buf)
            buf.clear()
            if prefix and not (prefix.isdigit() or prefix == "&"):
                # 形如 `hi>out.txt` —— 前半段是普通 token，不是 fd 号
                tokens.append(prefix)
                tokens.append(">")
            else:
                tokens.append(prefix + op)
            i = j
            continue
        buf.append(ch)
        i += 1
    flush()
    return tokens


def _exe_name(token: str) -> str:
    """取可执行名：剥目录、剥 .exe/.cmd/.bat/.ps1 后缀，转小写。

    `C:\\Windows\\System32\\del.exe` → `del`
    """
    name = token.strip().strip("\"'")
    for sep in ("\\", "/"):
        if sep in name:
            name = name.rsplit(sep, 1)[-1]
    name = name.strip("\"'")
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.lower()


def _is_redirect_op(token: str) -> bool:
    return bool(_REDIRECT_OP_RE.match(token))


def _positional(args: list[str]) -> list[str]:
    """位置参数（去掉 `-` 开头的选项）。"""
    return [a for a in args if not a.startswith("-")]


def bash_write_targets(segment: str) -> list[str]:
    """返回该子命令的写目标（原始字符串）。

    只解析写动词的目标参数，不扫全文找路径。识别不出目标时返回空列表 ——
    漏判比错判安全。
    """
    if not segment:
        return []
    tokens = _tokenize(segment)
    if not tokens:
        return []

    targets: list[str] = []
    args: list[str] = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if _is_redirect_op(tok):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt and not nxt.startswith("&"):
                # `2>&1` 里的 `&1` 是 fd 复制，不是文件目标
                targets.append(nxt)
                i += 2
                continue
            i += 1
            continue
        args.append(tok)
        i += 1

    exe = _exe_name(tokens[0])

    if exe == "dd":
        # dd 的写目标藏在 `of=` 里，不是位置参数
        for a in args:
            if a.startswith("of="):
                targets.append(a[3:])
        return targets

    if exe == "find":
        # find 的写目标藏在 -fprint / -fprintf / -fls 里，不是位置参数。
        # 这三个选项会把匹配结果写进文件 —— `find` 在只读命令表里，
        # 但带着这些选项时它就是写命令。
        for i, a in enumerate(args):
            for opt in ("-fprint", "-fprintf", "-fls"):
                if a == opt:
                    if i + 1 < len(args):
                        targets.append(args[i + 1])
                elif a.startswith(opt + "="):
                    targets.append(a[len(opt) + 1:])
        return targets

    if exe == "diff":
        # diff 的写目标藏在 --output= 里
        for i, a in enumerate(args):
            if a == "--output":
                if i + 1 < len(args):
                    targets.append(args[i + 1])
            elif a.startswith("--output="):
                targets.append(a[len("--output="):])
        return targets

    mode = _WRITE_VERB_TARGETS.get(exe)
    if mode is None:
        return targets

    if exe == "sed" and not any(
        a == "--in-place" or (a.startswith("-i") and not a.startswith("--"))
        for a in args
    ):
        # sed 不带 -i 是读
        return targets

    positional = _positional(args)
    if not positional:
        return targets
    if mode == "all":
        targets.extend(positional)
    elif mode == "last":
        targets.append(positional[-1])
    else:  # first
        targets.append(positional[0])
    return targets


def bash_blocked_write_target(command: str) -> str | None:
    """命令里有写目标落在只读区 → 返回理由串；否则 None。

    理由串必须指出**是哪一段**、原文是什么、目标是谁 —— 不能是一句笼统的
    「命令危险」。复合命令按段判定：bash 是一个子进程，跑一半停不下来。
    """
    for idx, segment in enumerate(split_bash_segments(command), start=1):
        for target in bash_write_targets(segment):
            if paths.classify_target(target) == "repo_readonly":
                return (
                    f"命令第 {idx} 段 `{segment.strip()}` "
                    f"试图修改只读区域 {target}"
                )
    return None


def _is_read_only_segment(segment: str) -> bool:
    """这一段是否只读。"""
    tokens = _tokenize(segment)
    if not tokens:
        return False
    exe = _exe_name(tokens[0])
    for tok in tokens[1:]:
        if _is_redirect_op(tok):
            return False
    if exe == "git":
        return _is_read_only_git(tokens[1:])
    if exe not in READ_ONLY_EXECUTABLES:
        return False
    if exe == "find":
        for a in tokens[1:]:
            if a.startswith(("-delete", "-exec", "-ok", "-fprint", "-fprintf", "-fls")):
                return False
    if exe == "diff":
        for a in tokens[1:]:
            if a.startswith("--output"):
                return False
    return True


def _is_read_only_git(args: list[str]) -> bool:
    """git 子命令是不是只读的。

    只读子命令不该被拦，也不该花判官调用（spec D7a：agent 用 bash 读源码
    是合法退路，`git status` / `git log` 同理）。
    """
    if not args:
        return False
    # 跳过全局选项（`git --no-pager status`）
    rest: list[str] = []
    for a in args:
        if not rest and a.startswith("-"):
            continue
        rest.append(a)
    if not rest or rest[0].lower() not in _GIT_READ_ONLY_SUBCOMMANDS:
        return False
    for a in rest[1:]:
        low = a.lower()
        # `git diff --output=FILE` 会把结果写进文件
        if low.startswith("--output") or low == "-o":
            return False
    return True


# ==================== git 破坏性命令 ====================
# git 不改"文件路径"，它直接改工作区和历史 —— 效果比改文件更狠：
# 用户没提交的改动一旦没了，快照兜底也救不回来（.git 不在受保护集合里，
# 而 `git reset --hard` 抹掉的历史快照根本存不下来）。
#
# 判的是**子命令**，不是正则扫全文 —— git 的子命令是有限的几个词，
# 逐个查表比正则稳，也不会误伤 `echo "git reset --hard"` 这种字符串。

# 无论带什么参数都要拒的 git 子命令
_GIT_ALWAYS_DENY: dict[str, str] = {
    "push": "会把改动推到远端 —— 本项目禁止推送",
    "filter-branch": "会重写提交历史",
}

# git 只读子命令 —— 这些只看不改
_GIT_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "diff", "log", "show", "blame", "describe", "rev-parse",
    "ls-files", "ls-tree", "shortlog", "cat-file", "whatchanged", "name-rev",
})


def _git_arg_is_repo_path(arg: str) -> bool:
    """这个 git 参数是仓库里的路径，还是分支名？

    分支名可以带斜杠（`feat/x`），所以不能只看有没有 `/`：

    - `.` `..` → 路径
    - 带文件后缀 → 路径
    - 首段是仓库里真实存在的目录 → 路径（`src/agent/xxx`）
    - 其余（`feat/x`、纯分支名）→ 分支名
    """
    if arg in (".", ".."):
        return True
    parts = PurePosixPath(arg.replace("\\", "/")).parts
    if not parts:
        return False
    if PurePosixPath(arg.replace("\\", "/")).suffix:
        return True
    if len(parts) < 2:
        return False
    try:
        return (WORKDIR / parts[0]).is_dir()
    except OSError:
        return False


def _git_subcommand_reason(args: list[str]) -> str | None:
    """git 子命令会不会毁掉工作区或历史 → 理由串；不会 → None。"""
    if not args:
        return None
    sub = args[0].lower()
    rest = args[1:]
    low = [a.lower() for a in rest]

    if sub in _GIT_ALWAYS_DENY:
        return _GIT_ALWAYS_DENY[sub]

    if sub == "reset":
        if "--hard" in low:
            return "会丢弃工作区里未提交的改动（`git reset --hard`）"
        return None

    if sub == "clean":
        if any(a in ("-n", "--dry-run") for a in low):
            return None
        return "会删除未跟踪的文件（`git clean`）"

    if sub == "restore":
        # `git restore --staged` 只动暂存区，不丢工作区里的内容
        # （`--staged --worktree` 两个一起才是恢复工作区）
        if "--staged" in low and "--worktree" not in low:
            return None
        return "会丢弃工作区里未提交的改动（`git restore`）"

    if sub == "checkout":
        if "--orphan" in low:
            return "会清空工作区（`git checkout --orphan`）"
        if "--" in rest:
            return "会丢弃工作区里未提交的改动（`git checkout -- <路径>`）"
        for a in rest:
            if a.startswith("-"):
                continue
            if _git_arg_is_repo_path(a):
                return f"会丢弃 {a} 上未提交的改动（`git checkout`）"
        return None

    if sub == "switch":
        if "--discard-changes" in low:
            return "会丢弃工作区里未提交的改动（`git switch --discard-changes`）"
        if any(a in ("-C", "--force-create") for a in rest):
            return "会强制重建分支，丢掉该分支上的提交（`git switch -C`）"
        return None

    if sub == "branch":
        if any(a in ("-D", "--delete-force") for a in rest):
            return "会删掉分支上的提交（`git branch -D`）"
        return None

    if sub == "stash":
        if low and low[0] in ("drop", "clear"):
            return f"会丢掉暂存起来的改动（`git stash {low[0]}`）"
        return None

    if sub == "reflog":
        if low and low[0] == "expire":
            return "会清掉历史里可回溯的引用（`git reflog expire`）"
        return None

    if sub == "gc":
        if any(a.startswith("--prune=now") for a in low):
            return "会永久删除不可达对象（`git gc --prune=now`）"
        return None

    if sub == "remote":
        if low and low[0] in ("remove", "set-url"):
            return f"会改掉远端指向（`git remote {low[0]}`）"
        return None

    if sub == "worktree":
        if low and low[0] == "remove":
            return "会删掉工作树（`git worktree remove`）"
        return None

    return None


def git_blocked_reason(command: str) -> str | None:
    """命令里有会毁掉工作区/历史的 git 子命令 → 返回理由串；否则 None。

    按段判定，理由里说明是哪一段 —— 和 `bash_blocked_write_target` 同一套话术。
    """
    for idx, segment in enumerate(split_bash_segments(command), start=1):
        tokens = _tokenize(segment)
        if not tokens:
            continue
        if _exe_name(tokens[0]) != "git":
            continue
        reason = _git_subcommand_reason(tokens[1:])
        if reason:
            return f"命令第 {idx} 段 `{segment.strip()}` {reason}"
    return None


def is_read_only_bash(command: str) -> bool:
    """整条命令是否只读 —— 每一段都只读才算。

    只读命令不该被拦，也不该花判官调用（spec D7a）。
    """
    segments = split_bash_segments(command)
    if not segments:
        return False
    return all(_is_read_only_segment(s) for s in segments)


# ==================== Error Type Conventions ====================

ERROR_PREFIXES = [
    "Error:",
    "错误:",
    "写入失败",
    "检索失败",
    "压缩失败",
    "保存失败",
    "追加失败",
    "替换失败",
    "搜索失败",
    "危险行为拒绝执行",
]


def is_failure_result(result: str) -> bool:
    """检测工具返回是否表示失败.

    仅当返回串的**开头**（lstrip 后）匹配已知错误前缀才视为失败。
    子串匹配会把成功搜索结果里碰巧包含的 "Error:" / "检索失败" / "搜索失败"
    / "替换失败" 等文本误判为失败，触发无意义的 [System note]。
    """
    if not result or not result.strip():
        return True
    head = result.lstrip()
    return any(head.startswith(prefix) for prefix in ERROR_PREFIXES)


def err(prefix: str, detail: str = "") -> str:
    """生成标准格式的错误返回."""
    return f"{prefix}: {detail}" if detail else prefix


# 全局管理器实例（延迟初始化）
_core_memory_manager: CoreMemoryManager = None

def _get_core_manager() -> CoreMemoryManager:
    global _core_memory_manager
    if _core_memory_manager is None:
        _core_memory_manager = CoreMemoryManager()
    return _core_memory_manager


# ==================== Security ====================

def safe_path(p: str) -> Path:
    """Resolve path safely, preventing directory traversal attacks."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# ==================== Base Tools ====================

@tool
def bash(command: str, background: bool = False) -> str:
    """Execute a shell command in the agent's working directory.

    Args:
        command: The shell command to execute.
        background: If True, run in background (for long-running services).
                    Do NOT set to True for commands that should complete.

    Returns:
        The combined stdout and stderr output, truncated to 50,000 chars.
        For background processes, returns confirmation message.
    """
    if _is_dangerous_command(command):
        # 把命中的模式名带回去,让 Agent 能把规则记进 Core Memory
        matched = _dangerous_match(command)
        return err("Error", f"危险行为拒绝执行: {matched}")
    # 写目标保护：命令要改只读区（仓库自身）就拒绝 —— 在起进程之前就拒，
    # 命令根本没跑。理由里说清是哪一段、目标是谁。
    blocked = bash_blocked_write_target(command)
    if blocked:
        return err("Error", f"危险行为拒绝执行: {blocked}")
    # git 改写类命令：不动文件路径，但一样能把用户的改动弄没
    git_blocked = git_blocked_reason(command)
    if git_blocked:
        return err("Error", f"危险行为拒绝执行: {git_blocked}")
    try:
        if background:
            # Windows: hide console window for background process
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            subprocess.Popen(
                command,
                shell=True,
                cwd=WORKDIR,
                startupinfo=startupinfo,
            )
            return f"[Background] Process started: {command[:80]}"
        else:
            # Add -s flag to curl commands to suppress progress output
            if "curl" in command.lower() and "-s" not in command:
                command = command.replace("curl.exe", "curl.exe -s")
                command = command.replace("curl ", "curl -s ")
            r = subprocess.run(
                command,
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=120,
            )
            out = (r.stdout or "").strip()
            return out[:50000] if out else "(no output)"
    except Exception as e:
        return err("Error", str(e))


@tool
def read_file(path: str, mode: str = "full", head: int = 100, tail: int = 100) -> str:
    """Read file content with automatic encoding detection.

    Args:
        path: Relative path from working directory.
        mode: Reading mode.
            - "full" (default): read entire file
            - "head": read first `head` lines
            - "tail": read last `tail` lines
        head: Line count when mode="head" (default 100).
        tail: Line count when mode="tail" (default 100).

    Returns:
        File content as string, or error message.
    """
    fp = safe_path(path)
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            lines = fp.read_text(encoding=encoding, errors="replace").splitlines()
            total = len(lines)
            if mode == "head":
                n = max(0, min(head, total))
                out_lines = lines[:n]
                if n < total:
                    out_lines.append(f"... ({total - n} more)")
            elif mode == "tail":
                n = max(0, min(tail, total))
                out_lines = lines[-n:] if n else []
                if n < total:
                    out_lines.append(f"... ({total - n} more)")
            elif mode == "full":
                out_lines = lines
            else:
                return f"Error: unknown mode {mode!r} (use head|tail|full)"
            return "\n".join(out_lines)[:50000]
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return err("Error", str(e))
    return f"Error: unable to decode {path}"


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file (UTF-8 encoding).

    Args:
        path: Relative path from working directory.
        content: The content to write.

    Returns:
        Success message with byte count, or error message.
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return err("Error", str(e))


@tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replace the first occurrence of old_text with new_text in a file.

    Args:
        path: Relative path from working directory.
        old_text: The exact text to find and replace.
        new_text: The replacement text.

    Returns:
        Success message, or error if text not found.
    """
    fp = safe_path(path)
    content = None
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            content = fp.read_text(encoding=encoding, errors="replace")
            break
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return err("Error", str(e))
    if content is None:
        return f"Error: unable to decode {path}"
    if old_text not in content:
        return f"Error: Text not found in {path}"
    try:
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return err("Error", str(e))


# ==================== Skill Loader ====================

class SkillLoader:
    """Load skill definitions from SKILL.md files in the skills directory.

    Each SKILL.md may contain YAML frontmatter (--- ... ---) with metadata
    like name, description, and a body with the actual skill content.
    """

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._scan()

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        """解析 YAML frontmatter。

        要支持折叠块 `description: >` —— 仓库里的 SKILL.md 都用它写多行描述，
        逐行 split(":") 只能拿到字面量 ">"。
        """
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        raw_meta, body = match.group(1), match.group(2).strip()
        meta: dict = {}
        lines = raw_meta.strip().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if ":" not in line:
                i += 1
                continue
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if value in (">", "|", ">-", "|-", ">+", "|+"):
                folded = []
                i += 1
                while i < len(lines) and (not lines[i].strip()
                                         or lines[i].startswith((" ", "\t"))):
                    folded.append(lines[i].strip())
                    i += 1
                meta[key] = " ".join(x for x in folded if x)
                continue
            meta[key] = value
            i += 1
        return meta, body

    def _scan(self):
        """Scan skills directory and load all SKILL.md files."""
        self.skills.clear()
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = None
            for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                try:
                    text = f.read_text(encoding=enc, errors="replace")
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                continue

            meta, body = self._parse_frontmatter(text)

            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body}

    def load(self, name: str) -> str:
        """Load a skill by name, returning content wrapped in <skill> tags.

        Uses lazy loading: if skill not found in cache, re-scans the directory
        to pick up newly added skills since startup.
        """
        if name not in self.skills:
            self._scan()
        s = self.skills.get(name)
        if not s:
            available = ", ".join(self.skills.keys())
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f'<skill name="{name}">\n{s["body"]}\n</skill>'

    def descriptions(self) -> str:
        """Return a formatted string of all skill names and descriptions."""
        if not self.skills:
            return "(no skills)"
        return "\n".join(
            f"  - {n}: {s['meta'].get('description', '-')}"
            for n, s in self.skills.items()
        )


# Global skill loader instance
SKILLS = SkillLoader(SKILLS_DIR)


@tool
def load_skill(name: str) -> str:
    """Load specialized knowledge by skill name.

    Args:
        name: The name of the skill to load (matches SKILL.md filename or frontmatter name).

    Returns:
        Skill content wrapped in <skill> tags, or error message.
    """
    return SKILLS.load(name)


# ==================== Subagent System ====================

def create_subagent_tools(agent_type: str = "Explore") -> List:
    """Create the tool set for a subagent based on agent type.

    Args:
        agent_type: "Explore" (read-only) or "general-purpose" (read-write).

    Returns:
        List of tools to attach to the subagent.
    """
    base = [bash, read_file]
    if agent_type != "Explore":
        base += [write_file, edit_file]
    return base


def create_subagent(agent_type: str = "Explore") -> Callable:
    """Factory: create an isolated subagent as a callable prompt runner.

    Each subagent runs in its own ReAct loop with its own tool set.

    Args:
        agent_type: "Explore" (read-only) or "general-purpose" (read-write).

    Returns:
        A callable that takes a prompt string and returns the agent's response.
    """
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, AIMessage
    from langgraph.prebuilt import create_react_agent

    from src.agent.config import MODEL_ID
    from src.agent.llm import _openai_mode, _openai_config

    if _openai_mode():
        from langchain_openai import ChatOpenAI
        api_key, api_base, model = _openai_config()
        sub_llm = ChatOpenAI(api_key=api_key, base_url=api_base, model=model)
    else:
        from langchain_anthropic import ChatAnthropic
        sub_llm = ChatAnthropic(
            model=MODEL_ID,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or "sk-placeholder",
            base_url=os.getenv("ANTHROPIC_BASE_URL", "").strip() or None,
        )
    sub_tools = create_subagent_tools(agent_type)

    sub_graph = create_react_agent(
        model=sub_llm,
        tools=sub_tools,
        debug=False,
    )

    def run(prompt: str) -> str:
        result = sub_graph.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"max_iterations": 30, "max_tokens": 8000},
        )
        for msg in reversed(result.get("messages", [])):
            if isinstance(msg, AIMessage) and msg.content:
                return msg.content
        return "(no summary)"

    return run


@tool
def spawn_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """Spawn an isolated subagent to work independently on a task.

    The subagent completes its work and returns a summary of what it did.
    Use this for multi-step or isolated tasks that should not clutter the
    main agent's context.

    Args:
        prompt: A detailed description of the task for the subagent.
        agent_type: "Explore" for read-only tasks, "general-purpose" for
            tasks that need to write or edit files.

    Returns:
        The subagent's final response/summary.
    """
    subagent = create_subagent(agent_type)
    return subagent(prompt)

# ==================== 记忆管理工具 ====================
from src.agent.memory import memory_system, memory_retriever, search_sessions

@tool
def remember_fact(fact: str) -> str:
    """
    当实验成功、获得最佳参数、或者发现代码 Bug 及解决办法时，主动调用此工具将事实永久保存。
    
    Args:
        fact: 详细的事实描述（如："PDH锁定的最佳 PI 参数为 P=15, I=3"）
    """
    try:
        memory_system.save_fact(fact, source="tool")
        return "已成功将该事实写入长期记忆库。"
    except Exception as e:
        return err("写入失败", str(e))

@tool
def search_memory(query: str) -> str:
    """
    主动从系统的长期记忆中检索历史实验记录、成功的参数或历史总结。
    当你不确定之前的操作，或需要参考历史经验时调用。
    
    Args:
        query: 检索关键词或自然语言描述
    """
    try:
        # 直接复用 Retriever 的多路召回能力
        result = memory_retriever.retrieve_context(query)
        return result if result and "无长期记忆" not in result else "未检索到相关历史记录。"
    except Exception as e:
        return err("检索失败", str(e))

@tool
def search_sessions(query: str, days: int = 7) -> str:
    """
    按关键词搜索用户的历史 session，找到后可结合 get_session_history 使用。
    当用户提到"之前"、"昨天"、"上次"等模糊时间描述时调用。

    Args:
        query: 检索关键词
        days: 向前搜索的天数，默认 7 天
    """
    try:
        return search_sessions(query, days)
    except Exception as e:
        return err("检索失败", str(e))

@tool
def compact(focus: str = "") -> str:
    """
    手动压缩当前会话历史，将全量对话替换为摘要，保存到长期记忆。
    当对话变长、上下文快超限、或需要固化关键进展时调用。

    Args:
        focus: 可选，指定希望在摘要中保留的重点（如"保留PI参数"）
    """
    from src.agent.session_manager import compact_session
    from src.agent.session_manager import get_session_history
    try:
        # 从最近的 assistant msg 提取 session_id（hack：通过 message 里的 tool_call_id 或最后一个 session）
        # 实际上 compact 需要 session_id，这里简化处理 — 获取当前历史来估算
        summary = compact_session("")  # placeholder, real impl needs session_id
        return f"会话已压缩: {summary}"
    except Exception as e:
        return err("压缩失败", str(e))


def _get_current_session_id() -> str | None:
    """Try to get current session_id from context. Used by compact tool."""
    # This is a workaround — the tool doesn't have direct access to session_id
    # For now, return empty to let compact_session handle it
    return None


# ==================== Core Memory Tools ====================


@tool
def save_core_block(label: str, value: str) -> str:
    """创建或全量替换指定标签的 Core Memory Block。

    如果 Block 不存在则创建；如果已存在则全量替换。
    写入前检查标签是否在白名单中。

    Args:
        label: 标签路径，如 "task", "conclusion/parameter"
        value: 记忆内容
    """
    try:
        manager = _get_core_manager()
        block = manager.save_block(label, value)
        return f"Block '{label}' 已保存，共 {len(value)} 字符"
    except ValueError as e:
        return f"错误: {e}"
    except Exception as e:
        return err("保存失败", str(e))

@tool
def append_core_block(label: str, content: str) -> str:
    """追加内容到指定标签的 Core Memory Block。

    追加而非覆盖，适合持续更新的任务状态。
    如果 Block 不存在则创建。

    Args:
        label: 标签路径
        content: 要追加的内容
    """
    try:
        manager = _get_core_manager()
        block = manager.append_block(label, content)
        return f"Block '{label}' 已追加内容，当前共 {len(block.value)} 字符"
    except ValueError as e:
        return f"错误: {e}"
    except Exception as e:
        return err("追加失败", str(e))

@tool
def replace_core_block(label: str, old_content: str, new_content: str) -> str:
    """精确替换 Block 中的内容。

    old_content 必须精确匹配，否则返回错误。
    适用于修正错误、保存关键进展。

    Args:
        label: 标签路径
        old_content: 要替换的原有内容（必须精确匹配）
        new_content: 替换后的新内容
    """
    try:
        manager = _get_core_manager()
        block = manager.replace_block(label, old_content, new_content)
        return f"Block '{label}' 已更新"
    except ValueError as e:
        return f"错误: {e}"
    except Exception as e:
        return err("替换失败", str(e))

@tool
def search_core_blocks(query: str) -> str:
    """搜索 Core Memory Blocks。

    按关键词模糊匹配 label 和 value。
    用于 Agent 查询当前记忆状态。

    Args:
        query: 搜索关键词
    """
    try:
        manager = _get_core_manager()
        blocks = manager.get_all_blocks()
        query_lower = query.lower()

        matched = []
        for block in blocks:
            if query_lower in block.label.lower() or query_lower in block.value.lower():
                # 搜索结果应显示完整内容，不截断
                matched.append(f"[{block.label}]: {block.value}")

        if not matched:
            return f"未找到匹配 '{query}' 的 Block"

        return "\n".join(["【匹配的 Core Memory Blocks】"] + matched)
    except Exception as e:
        return err("搜索失败", str(e))


# ==================== Ask User Tool ====================


class AskUserArgs(BaseModel):
    """ask_user 的参数。

    刻意用 ``list[dict]`` 而不是嵌套 pydantic 模型：嵌套模型会在 JSON schema
    里生成 $defs/$ref，而上游是本地 Ollama，对嵌套 schema 的支持不稳定。
    每一项的形状写在 options 的 description 里。
    """

    question: str = Field(description="需要用户决定什么。一句话说清决策点。")
    options: list[dict] = Field(
        default_factory=list,
        description=(
            "候选项数组，2-4 项，按推荐度从高到低排列。每项形如 "
            '{"label": "选项文本", "description": "一句话说明为什么选它", '
            '"recommended": true}。recommended 最多一项为 true，'
            "应标在你认为最优的选项上（并把它放在第一位）。"
        ),
    )


@tool(args_schema=AskUserArgs)
def ask_user(question: str, options: list[dict]) -> str:
    """向用户提问并等待回答。

    当出现下列情况时调用本工具，而不是自己猜测：
    - 存在多个合理方案，选择会显著改变后续执行路径
    - 缺少只有用户才知道的信息（阈值、器件编号、实验偏好）
    - 不可逆操作需要用户确认

    规则：
    - 本工具必须是你这一轮唯一的工具调用；调用后本轮即结束，系统会等待用户回答
    - options 给 2-4 项，把你认为最优的选项放在第一位，并给它 recommended: true
    - 每个选项的 description 用一句话说明为什么选它
    - question 一句话说清要用户决定什么

    示例：
    question: "用哪套标定参数继续？"
    options:
      [{"label": "参数集 A", "description": "上次实验验证过，数据可复现", "recommended": true},
       {"label": "参数集 B", "description": "更新但尚未验证"}]
    """
    # 正常路径不会执行到这里：agent loop 在执行工具之前就拦截了 ask_user，
    # 并把用户的回答作为 tool_result 回填。走到这里说明拦截失效。
    return "Error: ask_user 必须由 agent loop 拦截，当前轮不应执行。"


# ==================== Tool List ====================

TOOLS: List[Callable] = [
    bash,
    read_file,
    write_file,
    edit_file,
    load_skill,
    # spawn_subagent,  # 临时禁用 2026-08-28 — 重新启用请取消注释
    remember_fact,
    search_memory,
    search_sessions,
    # Core Memory tools
    save_core_block,
    append_core_block,
    replace_core_block,
    search_core_blocks,
    ask_user,
]

# 给「会真的执行工具」的 agent 用的工具表（create_react_agent / server 模式）。
#
# ask_user 必须排除：那条路径直接跑工具函数，而 ask_user 的正常路径是由
# graph_thinking 的 agent loop 在执行前拦截、停下等用户回答的。放进这里，
# 模型只会拿到一句「必须由 agent loop 拦截」的报错串，提问功能等于不存在。
TOOLS_FOR_REACT: List[Callable] = [
    t for t in TOOLS if getattr(t, "name", None) != "ask_user"
]
