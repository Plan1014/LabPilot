"""授权台账 —— 用户批准过的操作范围。

两种粒度：

- **窄模式**（精确到某个目录 / 脚本 / 接口）→ 持久化到 `auth_grants` 表，
  下次会话也直接放行。
- **宽模式**（会扩大到用户当初没同意过的操作）→ 只留在会话内存里，
  关掉会话就没了。

匹配是确定性的，不问模型 —— 用户批准过什么，就精确放行什么。
"""

import fnmatch
import time
import uuid

from src.agent import paths
from src.agent.authz.patterns import is_narrow_pattern

# 会话内的宽模式：session_id -> [pattern, ...]
_session_grants: dict[str, list[str]] = {}


def reset_session_grants() -> None:
    """清空会话内授权（测试隔离用）。"""
    _session_grants.clear()


def _conn():
    from src.agent import session_manager

    return session_manager._get_conn()


# ==================== 记录 ====================


def record_grant(pattern: str, *, session_id: str | None = None) -> None:
    """记一条授权。窄 → 持久化；宽 → 只进会话内存。"""
    if not pattern:
        return
    if is_narrow_pattern(pattern):
        _record_persistent(pattern, session_id)
    else:
        _session_grants.setdefault(session_id or "", []).append(pattern)


def _record_persistent(pattern: str, session_id: str | None) -> None:
    conn = _conn()
    with conn:
        conn.execute(
            """
            INSERT INTO auth_grants (id, pattern, tool, scope, created_at, session_id)
            VALUES (?, ?, ?, 'persistent', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                pattern,
                pattern.split(":", 1)[0],
                time.time(),
                session_id,
            ),
        )


# ==================== 查询 ====================


def list_grants(
    *, session_id: str | None = None, scope: str | None = None
) -> list[dict]:
    """列出授权。scope 为 None 时持久化 + 当前会话都返回。"""
    out: list[dict] = []
    if scope in (None, "persistent"):
        conn = _conn()
        rows = conn.execute(
            "SELECT id, pattern, tool, scope, created_at FROM auth_grants ORDER BY created_at"
        ).fetchall()
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "pattern": r[1],
                    "tool": r[2],
                    "scope": r[3],
                    "created_at": r[4],
                }
            )
    if scope in (None, "session"):
        for p in _session_grants.get(session_id or "", []):
            out.append(
                {
                    "id": f"session:{session_id or ''}:{p}",
                    "pattern": p,
                    "tool": p.split(":", 1)[0],
                    "scope": "session",
                    "created_at": None,
                }
            )
    return out


def revoke_grant(grant_id: str) -> bool:
    """撤销一条授权；返回是否真删掉了。"""
    conn = _conn()
    with conn:
        cur = conn.execute("DELETE FROM auth_grants WHERE id = ?", (grant_id,))
    return cur.rowcount > 0


def clear_session_grants(session_id: str) -> None:
    """清掉某会话内的宽模式授权。"""
    _session_grants.pop(session_id or "", None)


# ==================== 匹配 ====================


def match_grant(
    tool_name: str, tool_input: dict, *, session_id: str | None = None
) -> str | None:
    """命中返回模式串；未命中返回 None。

    持久化授权全局有效；会话授权只在本会话有效。
    """
    for grant in _persistent_patterns():
        if _pattern_matches(grant, tool_name, tool_input):
            return grant
    for grant in _session_grants.get(session_id or "", []):
        if _pattern_matches(grant, tool_name, tool_input):
            return grant
    return None


def _persistent_patterns() -> list[str]:
    conn = _conn()
    rows = conn.execute("SELECT pattern FROM auth_grants").fetchall()
    return [r[0] for r in rows]


def _pattern_matches(pattern: str, tool_name: str, tool_input: dict) -> bool:
    if not pattern or ":" not in pattern:
        return False
    tool, _, payload = pattern.partition(":")
    if tool != tool_name:
        return False
    payload = payload.strip()
    if not payload:
        return False

    if tool_name in ("write_file", "edit_file"):
        target = tool_input.get("path") or ""
        rel = _relative_to_workdir(target)
        return rel is not None and fnmatch.fnmatchcase(rel, payload)

    if tool_name == "bash":
        command = tool_input.get("command") or ""
        from src.agent import tools

        segments = tools.split_bash_segments(command)
        if len(segments) != 1:
            # 复合命令的作用域没法用单一前缀表达 —— 不命中
            return False
        segment = segments[0].strip()
        if not segment:
            return False
        if segment == payload or segment.startswith(payload + " "):
            return True
        return fnmatch.fnmatchcase(segment, payload)

    return False


def _relative_to_workdir(raw: str) -> str | None:
    resolved = paths.resolve_in_workdir(raw)
    if resolved is None:
        return None
    try:
        return resolved.relative_to(paths.WORKDIR).as_posix()
    except ValueError:
        return None
