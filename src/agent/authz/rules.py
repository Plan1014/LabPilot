"""闸门层的 L0 / L1 判定规则。

**这里不重新实现 bash 的命令分析**（spec D13）。写目标解析、只读命令表、复合
命令拆分都住在 `src/agent/tools.py` —— 那是 bash 工具自己的实现，工具执行前
就会拒一次，闸门层只**调用**它、不复制一份。复制两份意味着以后改一处漏一处，
而两边不一致的时候没人知道该信谁。

本模块负责的是「闸门怎么用那些答案」：
- L1：路径型工具（write_file / edit_file）的目标落在保护区 → 拒绝。
- L0：只读工具、固定路径的记忆工具、可写区内的文件工具、只读 bash、
      skills 里的 curl。
"""

import re
from pathlib import Path
from urllib.parse import urlparse

from src.agent import paths, tools
from src.agent.config import SKILLS_DIR as _CONFIG_SKILLS_DIR

# 模块级再存一份，让测试能 monkeypatch 替换
SKILLS_DIR: Path = _CONFIG_SKILLS_DIR

# 只读工具 —— 只看不改，一律放行
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "load_skill",
    "search_memory",
    "search_sessions",
    "search_core_blocks",
})

# 写入固定系统路径的记忆工具 —— 目标写死在 data/memory 下，不是用户可控路径。
# 拦掉等于把记忆系统整个瘫掉。
FIXED_PATH_WRITE_TOOLS: frozenset[str] = frozenset({
    "remember_fact",
    "save_core_block",
    "append_core_block",
    "replace_core_block",
})

# 手动模式下仍然免审批的规则 —— 只看不改的那部分，外加写死路径的记忆写入。
#
# 手动模式（manual）的意思是「命令与写操作由用户裁决」，不是「每个工具调用
# 都点一次」。只读工具和只读 bash 放行；记忆工具写的是固定路径（data/memory），
# 不是用户可控的路径，卡住它等于把记忆系统瘫掉。
# 不在名单里的：writable_zone（写文件）、skill_curl（curl 仪器接口）——
# 这两类正是手动模式要交给用户裁决的。
MANUAL_EXEMPT_RULES: frozenset[str] = frozenset({
    "read_only_tool",
    "read_only_bash",
    "fixed_path_memory_tool",
})

# URL 前缀白名单只认回环地址 —— skills 里的 curl 指向本机的仪器服务
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})

_URL_RE = re.compile(r"https?://[^\s\"'`<>）)\]}]+", re.IGNORECASE)


def find_denied_write_target(tool_name: str, tool_input: dict) -> str | None:
    """命中受保护写入 → 返回可读的理由串；未命中 → None。

    - `write_file` / `edit_file`：目标不在可写区（tmp/ 与 data/）就拒。
    - `bash`：直接问 `tools.bash_blocked_write_target` —— 命令分析不在这里做。
    """
    if tool_name in ("write_file", "edit_file"):
        path = (tool_input or {}).get("path") or ""
        if paths.classify_target(path) != "writable":
            return f"写入目标 {path} 不在可写区（tmp/ 与 data/）内"
        return None

    if tool_name == "bash":
        command = (tool_input or {}).get("command") or ""
        reason = tools.bash_blocked_write_target(command)
        if reason:
            return reason
        # git 改写类命令也归 L1 —— 判官看不见工作区状态，不该让它来决定
        # 用户的未提交改动能不能丢。
        return tools.git_blocked_reason(command)

    return None


def is_l0_whitelisted(tool_name: str, tool_input: dict) -> tuple[bool, str | None]:
    """返回 (是否白名单, 命中的规则名)。"""
    tool_input = tool_input or {}

    if tool_name in READ_ONLY_TOOLS:
        return True, "read_only_tool"

    if tool_name in FIXED_PATH_WRITE_TOOLS:
        return True, "fixed_path_memory_tool"

    if tool_name in ("write_file", "edit_file"):
        path = tool_input.get("path") or ""
        if paths.is_writable_path(path):
            return True, "writable_zone"
        return False, None

    if tool_name == "bash":
        command = tool_input.get("command") or ""
        # background=True 的 bash 会打穿快照兜底（spec §5.6），一律交给 L2 判官
        if tool_input.get("background"):
            return False, None
        if tools.is_read_only_bash(command):
            return True, "read_only_bash"
        if match_skill_curl(command, extract_skill_curl_prefixes(SKILLS_DIR)):
            return True, "skill_curl"

    return False, None


def extract_skill_curl_prefixes(skills_dir: Path) -> list[str]:
    """扫描 skills/*/SKILL.md，取出所有回环地址 URL 的 `scheme://host:port` 前缀。

    按前缀而不是整条命令文本匹配 —— skill 里写的是 `curl.exe -X POST http://...`，
    agent 实际可能发出 `curl -X POST http://...` 或加个 `-s`，按前缀能覆盖这些变体。
    """
    prefixes: list[str] = []
    seen: set[str] = set()
    if not skills_dir or not skills_dir.exists():
        return prefixes
    for skill_md in skills_dir.glob("*/SKILL.md"):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for url in _URL_RE.findall(text):
            prefix = _url_prefix(url)
            if prefix and prefix not in seen:
                seen.add(prefix)
                prefixes.append(prefix)
    return prefixes


def _url_prefix(url: str) -> str | None:
    """取 `scheme://host[:port]` 前缀；非回环地址 → None。

    skill 是散文，URL 后面常跟着句号 —— `http://127.0.0.1:8001.` 的端口不是
    合法整数，直接问 urlparse 会抛 ValueError。所以先剥掉尾部标点，
    并且整个解析都兜住异常（解析不了就不登记 —— 一条坏 URL 不该掀翻整个提取）。
    """
    url = url.rstrip(".,;:!?")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if host not in _LOOPBACK_HOSTS:
        return None
    if port:
        return f"{parsed.scheme}://{host}:{port}"
    return f"{parsed.scheme}://{host}"


def match_skill_curl(command: str, prefixes: list[str]) -> bool:
    """命令的每一段都是 curl、且 URL 落在某个前缀里 → True。

    每段都要满足 —— 否则 `curl 白名单URL && curl evil.com` 会整条漏过去。

    两条收紧（评审 C1 / I2）：
    1. **带输出 / 上传选项的 curl 不算命中** —— 白名单的语义是「skills 里的
       只读探活请求」，`curl -o src/x.txt <白名单URL>` 已经是在往磁盘写东西。
    2. **URL 比的是解析后的 (scheme, host, port)，不是字符串前缀** ——
       `http://127.0.0.1:8001@evil.com/` 的真实主机是 evil.com，但它以
       `http://127.0.0.1:8001` 开头，字符串前缀会放行它。
    """
    if not command or not prefixes:
        return False
    segments = tools.split_bash_segments(command)
    if not segments:
        return False
    for segment in segments:
        tokens = tools._tokenize(segment)
        if not tokens:
            return False
        exe = tools._exe_name(tokens[0])
        if exe != "curl":
            return False
        if tools.curl_has_output_option(tokens[1:]):
            return False
        url = None
        for tok in tokens[1:]:
            if tok.lower().startswith("http://") or tok.lower().startswith("https://"):
                url = tok
                break
        if url is None:
            return False
        if not _url_in_prefixes(url, prefixes):
            return False
    return True


# 协议没写端口时的默认端口 —— 比的时候补上，`http://127.0.0.1` 与
# `http://127.0.0.1:80` 是同一个地方。
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _parse_netloc(url: str) -> tuple[str, str, int] | None:
    """解析成 (scheme, host, port)；解析不出来 → None。"""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 0)
    return (scheme, host, port)


def _url_in_prefixes(url: str, prefixes: list[str]) -> bool:
    """URL 的 (scheme, host, port) 与某个前缀一致 → True。"""
    target = _parse_netloc(url)
    if target is None:
        return False
    for p in prefixes:
        parsed = _parse_netloc(p)
        if parsed is not None and parsed == target:
            return True
    return False
