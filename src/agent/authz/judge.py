"""鉴权 agent（判官）—— L2 灰名单的唯一 LLM 调用点。

三条硬规矩：

1. **不复用主模型**。判官是分类任务，小模型就够；主模型后续要往更强的方向换。
   判官只认 `AUTHZ_MODEL_ID`，没配就进入降级模式（全部 L2 转人工），**不静默
   回落到主模型**。
2. **fail-closed**。超时、报错、JSON 解析不出来 —— 一律 `escalate` 转人工，
   绝不默认放行。判官挂了不等于放行。
3. **不参与工具执行**。判官没有工具，看不到会话历史，只看一个工具调用本身。
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from src.agent.authz.judge_prompt import JUDGE_SYSTEM_PROMPT, build_judge_user_message

JUDGE_MODEL_ENV = "AUTHZ_MODEL_ID"
FALLBACK_MODEL_ENV = "AUTHZ_FALLBACK_MODEL_ID"
JUDGE_TIMEOUT_S = 30
FAILURE_THRESHOLD = 3

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)


@dataclass
class JudgeResult:
    decision: Literal["allow", "deny", "escalate"]
    reason: str
    question: str


_consecutive_failures = 0


def judge_model_id() -> str | None:
    """判官模型 ID；没配 → None（闸门据此进入降级模式）。"""
    return (os.getenv(JUDGE_MODEL_ENV, "") or "").strip() or None


def fallback_model_id() -> str | None:
    """备用判官模型 ID；没配 → None。"""
    return (os.getenv(FALLBACK_MODEL_ENV, "") or "").strip() or None


def consecutive_failures() -> int:
    return _consecutive_failures


def reset_failures() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


def _bump_failure() -> None:
    global _consecutive_failures
    _consecutive_failures += 1


def _make_client(model_id: str) -> Any:
    """给判官自建一个 chat client —— 不复用主 agent 的 `llm` 单例。

    判官要的是：独立模型、关 thinking、尽量保证 JSON 输出。
    """
    from src.agent.llm import _openai_config, _openai_mode

    if _openai_mode():
        from langchain_openai import ChatOpenAI

        # `_openai_config()` 返回 4 个值 (api_key, api_base, model, enable_thinking)
        api_key, api_base, _model, _enable_thinking = _openai_config()
        return ChatOpenAI(
            api_key=api_key,
            base_url=api_base,
            model=model_id,
            timeout=JUDGE_TIMEOUT_S,
            response_format={"type": "json_object"},
            # 判官要严格 JSON，开 thinking 耗时翻倍且污染输出
            extra_body={"enable_thinking": False},
        )

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=model_id,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or "sk-placeholder",
        base_url=(os.getenv("ANTHROPIC_BASE_URL", "") or "").strip() or None,
        timeout=JUDGE_TIMEOUT_S,
    )


def _call_model(client: Any, tool_name: str, tool_input: dict) -> str | None:
    """调一次模型拿原始文本；任何异常 → None（调用方按失败处理）。"""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        response = client.invoke(
            [
                SystemMessage(JUDGE_SYSTEM_PROMPT),
                HumanMessage(build_judge_user_message(tool_name, tool_input)),
            ]
        )
    except Exception:  # noqa: BLE001 —— 判官调不通就当失败，绝不能把异常掀到主 agent
        return None
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                parts.append(str(block.text))
        return "".join(parts)
    return str(content) if content is not None else None


def parse_judge_output(raw: str) -> JudgeResult | None:
    """解析判官输出。解析不出来 → None（调用方转人工）。"""
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw.strip()).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision", "")).strip().lower()
    if decision not in ("allow", "deny", "escalate"):
        return None
    reason = data.get("reason")
    question = data.get("question")
    return JudgeResult(
        decision=decision,
        reason=reason if isinstance(reason, str) else "",
        question=question if isinstance(question, str) else "",
    )


def judge(tool_name: str, tool_input: dict) -> JudgeResult:
    """裁决一个工具调用。

    降级阶梯：主模型 → 备用模型 → 转人工；连续失败 ≥ 3 次后直接转人工，
    不再等超时。
    """
    model_id = judge_model_id()
    if not model_id:
        _bump_failure()
        return JudgeResult(
            "escalate",
            "未配置 AUTHZ_MODEL_ID，自动审批不可用",
            "自动审批服务未配置，请人工决定是否执行这个操作。",
        )

    if _consecutive_failures >= FAILURE_THRESHOLD:
        return JudgeResult(
            "escalate",
            "自动审批连续失败，已转为人工",
            "自动审批服务不可用，请人工决定是否执行这个操作。",
        )

    result = _try_model(model_id, tool_name, tool_input)

    if result is None:
        fallback = fallback_model_id()
        if fallback and fallback != model_id:
            result = _try_model(fallback, tool_name, tool_input)

    if result is None:
        _bump_failure()
        return JudgeResult(
            "escalate",
            "自动审批不可用",
            "自动审批服务不可用，请人工决定是否执行这个操作。",
        )

    reset_failures()
    return result


def _try_model(model_id: str, tool_name: str, tool_input: dict) -> JudgeResult | None:
    try:
        client = _make_client(model_id)
    except Exception:  # noqa: BLE001 —— 建不起客户端（没配 key、模型名错）也是失败
        return None
    raw = _call_model(client, tool_name, tool_input)
    if raw is None:
        return None
    return parse_judge_output(raw)
