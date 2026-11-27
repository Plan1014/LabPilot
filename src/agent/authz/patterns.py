"""「允许并记住」模式的确定性推导 + 窄/宽分界。

模式是**用户授权过的操作范围**。窄模式可以持久化到台账（下次直接放行），
宽模式只能留在会话内存里 —— 因为宽模式会扩大到用户没同意过的操作：
`bash:curl http://127.0.0.1:8001/**` 能把任意 body POST 到仪器服务（包括
`/lock/manual`、`/pid/set`），用户当初批准的是「这一个操作」。

推导全部在 Python 侧做，鉴权 agent 不参与（spec D8：agent 只给 allow/deny/
escalate，「允许并记住」这个模式是用户授权出来的，不是模型想出来的）。
"""

from urllib.parse import urlparse

from src.agent import paths, tools

# 已知的本机服务端口 —— 只有指向这些端口的精确 URL 才算窄
KNOWN_LOOPBACK_PORTS: tuple[int, ...] = (8001, 8002)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})


def derive_pattern(tool_name: str, tool_input: dict) -> str | None:
    """推出 '<tool>:<payload>' 形式的模式；推不出来 → None。

    推不出来的情况一律返回 None —— 卡片上「允许并记住」这一项就该置灰。
    """
    tool_input = tool_input or {}

    if tool_name in ("write_file", "edit_file"):
        path = tool_input.get("path") or ""
        resolved = paths.resolve_in_workdir(path)
        if resolved is None or not paths.is_writable_path(path):
            return None
        parent = resolved.parent
        rel = paths.relative_path_of(parent)
        if rel is None:
            return None
        return f"{tool_name}:{rel}/**"

    if tool_name == "bash":
        command = tool_input.get("command") or ""
        segments = tools.split_bash_segments(command)
        # 复合命令的作用域没法用单一前缀表达 —— 不给「记住」选项
        if len(segments) != 1:
            return None
        tokens = tools._tokenize(segments[0])
        if not tokens:
            return None
        exe = tools._exe_name(tokens[0])
        arg = _first_path_or_url_arg(tokens[1:])
        if arg is None:
            return None
        if "://" in arg:
            url = arg
            try:
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                port = parsed.port
            except ValueError:
                return None
            if parsed.scheme not in ("http", "https"):
                return None
            if host not in _LOOPBACK_HOSTS or port not in KNOWN_LOOPBACK_PORTS:
                return None
            return f"bash:curl {url}"
        resolved = paths.resolve_in_workdir(arg)
        if resolved is None or not paths.is_writable_path(arg):
            return None
        rel = paths.relative_path_of(resolved)
        if rel is None:
            return None
        return f"bash:{exe} {rel}"

    return None


def _first_path_or_url_arg(args: list[str]) -> str | None:
    """取第一个「像路径或 URL」的参数；没有 → None。"""
    for a in args:
        if a.startswith("-"):
            continue
        if "://" in a or "/" in a or "\\" in a:
            return a
    return None


def is_narrow_pattern(pattern: str) -> bool:
    """窄 → True（可持久化）；宽 → False（仅会话内）。"""
    if not pattern or ":" not in pattern:
        return False
    tool, _, payload = pattern.partition(":")
    payload = payload.strip()
    if not payload:
        return False

    if tool in ("write_file", "edit_file"):
        return _is_narrow_dir_pattern(payload)
    if tool == "bash":
        return _is_narrow_bash_pattern(payload)
    return False


def _is_narrow_dir_pattern(payload: str) -> bool:
    """`tmp/exp1/**` 窄；`tmp/**` 宽；带其它通配的一律宽。"""
    if payload.endswith("/**"):
        dir_part = payload[: -len("/**")]
    elif "**" in payload or "*" in payload or "?" in payload:
        return False
    else:
        dir_part = payload
    if not dir_part:
        return False
    # 必须整体在可写区内
    if not paths.is_writable_path(dir_part):
        return False
    # 挂在可写区根下的（tmp/**）覆盖整棵树 —— 宽
    return len([p for p in dir_part.split("/") if p]) >= 2


def _is_narrow_bash_pattern(payload: str) -> bool:
    """`<exe> <arg>`：精确到具体脚本或具体接口才窄。"""
    if "*" in payload or "?" in payload:
        return False
    parts = payload.split(None, 1)
    if len(parts) != 2:
        return False
    _exe, arg = parts
    arg = arg.strip()
    if not arg:
        return False
    if "://" in arg:
        try:
            parsed = urlparse(arg)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme in ("http", "https")
            and host in _LOOPBACK_HOSTS
            and port in KNOWN_LOOPBACK_PORTS
        )
    return paths.is_writable_path(arg)
