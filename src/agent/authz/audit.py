"""判定日志 —— 每次闸门裁决都留一条记录。

日志失败**绝不能影响放行判定**：写不进去就跳过，判定照常。
"""

import json
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from src.agent.authz import Verdict

from src.agent.config import WORKDIR

AUTHZ_LOG_PATH: Path = WORKDIR / "data" / "memory" / "authz.log"

_RECENT: deque = deque(maxlen=500)


def log_verdict(
    verdict: "Verdict",
    *,
    tool_name: str,
    session_id: str | None,
    judge_model: str | None,
    elapsed_ms: int,
) -> None:
    """记一条判定。写文件失败 → 静默忽略。"""
    entry = {
        "ts": time.time(),
        "session_id": session_id,
        "tool": tool_name,
        "layer": verdict.layer,
        "decision": verdict.decision,
        "reason": verdict.reason,
        "matched_rule": verdict.matched_rule,
        "judge_model": judge_model,
        "elapsed_ms": elapsed_ms,
    }
    _RECENT.append(entry)
    try:
        AUTHZ_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUTHZ_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent(n: int = 100) -> list[dict]:
    """最近的判定记录（内存环形缓冲）。"""
    if n <= 0:
        return []
    items = list(_RECENT)
    return items[-n:]


def authz_log_path() -> Path:
    return AUTHZ_LOG_PATH


def reset_recent() -> None:
    """清空内存环形缓冲（测试隔离用）。"""
    _RECENT.clear()
