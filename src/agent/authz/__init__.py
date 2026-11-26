"""工具鉴权闸门。

四层，**拒绝优先**：

- **L1 黑名单**（确定性拒绝）—— 写目标落在受保护区。
- **L0 白名单**（确定性放行）—— 只读工具、可写区、台账里已授权的模式。
- **L2 灰名单**（判官裁决）—— 独立模型看这一次工具调用本身。
- **L3 转人工**—— 判官拿不准就问用户。

L1 命中就永远不看 L0 —— 否则一条白名单就能把黑名单绕过去。
"""

import time
from dataclasses import dataclass
from typing import Literal

from . import audit
from . import judge as judge_module
from . import ledger
from . import patterns
from . import rules

__all__ = ["Verdict", "authorize"]


@dataclass
class Verdict:
    decision: Literal["allow", "deny", "escalate"]
    layer: str  # "L0" | "L1" | "L2" | "L3"
    reason: str
    matched_rule: str | None = None
    question: str = ""
    pattern: str | None = None  # 仅 escalate 时可能非空（「允许并记住」的模式）


def authorize(
    tool_name: str, tool_input: dict, *, session_id: str | None = None
) -> Verdict:
    """裁决一个工具调用。"""
    started = time.monotonic()

    def finish(v: Verdict) -> Verdict:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        audit.log_verdict(
            v,
            tool_name=tool_name,
            session_id=session_id,
            judge_model=judge_module.judge_model_id(),
            elapsed_ms=elapsed_ms,
        )
        return v

    tool_input = tool_input or {}

    # ---- L1：黑名单（拒绝优先） ----
    denied = rules.find_denied_write_target(tool_name, tool_input)
    if denied:
        return finish(Verdict("deny", "L1", denied, matched_rule="write_target"))

    # ---- L0：白名单 ----
    granted = ledger.match_grant(tool_name, tool_input, session_id=session_id)
    if granted:
        return finish(
            Verdict(
                "allow",
                "L0",
                f"命中已授权模式 {granted}",
                matched_rule="ledger",
            )
        )

    whitelisted, rule_name = rules.is_l0_whitelisted(tool_name, tool_input)
    if whitelisted:
        return finish(Verdict("allow", "L0", "命中白名单规则", matched_rule=rule_name))

    # ---- L2：判官 ----
    result = judge_module.judge(tool_name, tool_input)
    if result.decision == "allow":
        return finish(Verdict("allow", "L2", result.reason))
    if result.decision == "deny":
        return finish(Verdict("deny", "L2", result.reason))
    return finish(
        Verdict(
            "escalate",
            "L2",
            result.reason,
            question=result.question,
            # 模式由确定性推导给出 —— 判官不参与模式决定（spec D8）
            pattern=patterns.derive_pattern(tool_name, tool_input),
        )
    )
