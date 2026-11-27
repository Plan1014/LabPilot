"""判定日志 —— 每次闸门裁决都留一条记录。

日志失败**绝不能影响放行判定**：写不进去就跳过，判定照常。
"""

import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from src.agent.authz import Verdict

from src.agent import paths

# 审计日志的落点 —— 跟授权台账（`.labpilot/authz.db`）同一个不可写区。
# 放 `data/` 里的话，agent 就能改自己的审计记录 —— 审计也就没意义了。
LOG_RELATIVE_PATH = Path(".labpilot") / "authz.log"

# 测试可以把它指向别的文件；None = 按工作区算（跟台账同一套规则）。
AUTHZ_LOG_PATH: Path | None = None

_RECENT: deque = deque(maxlen=500)


def _now_stamps() -> tuple[float, str, str]:
    """三个时间：epoch、本地时间、UTC。

    本地时间跟着**跑这份代码的机器**走 —— 用户在中国看到 +0800，
    换个时区的机器看就是当地的偏移，不用自己换算。
    本地时间带偏移量（ISO 8601），UTC 那份不带歧义 —— 换算用哪个都行。
    """
    now = time.time()
    local = datetime.fromtimestamp(now).astimezone()
    utc = datetime.fromtimestamp(now, tz=UTC)
    return now, local.isoformat(timespec="seconds"), utc.isoformat(timespec="seconds")


def log_verdict(
    verdict: "Verdict",
    *,
    tool_name: str,
    session_id: str | None,
    judge_model: str | None,
    elapsed_ms: int,
) -> None:
    """记一条判定。写文件失败 → 静默忽略。"""
    ts, ts_local, ts_utc = _now_stamps()
    entry = {
        "ts": ts,
        "ts_local": ts_local,
        "ts_utc": ts_utc,
        "session_id": session_id,
        "tool": tool_name,
        "layer": verdict.layer,
        "decision": verdict.decision,
        "reason": verdict.reason,
        "matched_rule": verdict.matched_rule,
        "judge_model": judge_model,
        # 判官这一趟的痕迹 ——「判官到底说了什么、这次是谁答的」
        "judge_model_used": verdict.judge_model_used,
        "judge_fallback": verdict.judge_fallback,
        "judge_raw": verdict.judge_raw,
        "elapsed_ms": elapsed_ms,
    }
    _RECENT.append(entry)
    try:
        path = authz_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
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
    """审计日志落在哪儿。

    默认跟着工作区走（`paths.WORKDIR`）—— 跟授权台账同一套规则，
    测试把工作区指到临时目录，日志就跟着走。
    """
    if AUTHZ_LOG_PATH is not None:
        return AUTHZ_LOG_PATH
    return paths.WORKDIR / LOG_RELATIVE_PATH


def reset_recent() -> None:
    """清空内存环形缓冲（测试隔离用）。"""
    _RECENT.clear()
