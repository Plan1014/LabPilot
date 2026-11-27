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

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool

from . import audit, ledger, patterns, rules
from . import judge as judge_module

__all__ = ["REPL_SESSION_ID", "Verdict", "authorize", "wrap_tools_with_gate"]

# 终端会话的身份。REPL 没有浏览器会话号，用这个固定的号 ——
# 落进「没有会话」的公共桶就等于把终端里批准的授权带去了别的执行入口。
REPL_SESSION_ID = "repl"


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


def _session_id_of(config: RunnableConfig | None) -> str | None:
    """从运行配置里取会话标识。

    server 模式（LangGraph）每个 thread 一个会话 —— 授权要按 thread 隔离，
    不能让所有 thread 共用一个「没有会话」的桶（评审 I4）。
    """
    if not config:
        return None
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _make_gated_tool(t):
    """把一个工具包成「先问闸门再执行」的工具。

    名字、说明、参数 schema 全部照抄原工具 —— 模型是按名字发起调用的，
    改了就等于换了个工具。
    """
    original = t
    name = getattr(t, "name", None) or getattr(t, "__name__", "tool")
    description = getattr(t, "description", "") or ""
    args_schema = getattr(t, "args_schema", None)

    def _gated(config: RunnableConfig = None, **kwargs):
        verdict = authorize(name, kwargs, session_id=_session_id_of(config))
        if verdict.decision == "allow":
            return original.invoke(kwargs)
        if verdict.decision == "escalate":
            # 这条执行路径没有人工审批界面 —— 判官拿不准就只能不放行
            # （fail-closed）。不抛异常：抛出去会被 create_react_agent
            # 变成「工具崩了」，用户看不出是自己被拦了。
            return (
                "Error: 该执行路径不支持人工审批，已拒绝。"
                f"原因：{verdict.reason}"
            )
        return f"Error: 操作被拒绝。原因：{verdict.reason}"

    _gated.__name__ = name
    return StructuredTool.from_function(
        func=_gated,
        name=name,
        description=description,
        args_schema=args_schema,
        return_direct=getattr(t, "return_direct", False),
    )


def wrap_tools_with_gate(tools: list) -> list:
    """给一组工具套上闸门。

    L0/L1/L2 正常生效；escalate → 转成 deny（fail-closed）。

    给**没有**人工审批界面的执行路径用（server 模式、记忆子 agent）。
    有卡片可弹的路径走 `graph_thinking.authorize_node` —— 那里能真的
    把 escalate 交给用户裁决。
    """
    return [_make_gated_tool(t) for t in tools]
