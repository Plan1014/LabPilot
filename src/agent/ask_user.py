"""ask_user —— 向用户提问的工具逻辑。

这里只放纯逻辑（不依赖 LLM / 数据库 / 网络），方便单测覆盖：

- ``normalize_options``    把模型给的原始 options 归一化成固定形状
- ``build_ask_payload``    从一个 tool_use 块解析出提问内容
- ``detect_ask``          在一批 tool_use 里认出提问
- ``find_pending_ask``    在会话历史里找出还没被回答的提问
- ``sanitize_ask_message``把响应里多余的 tool_use 丢掉，保证提问的 id 可配对
- ``build_turn_row``      把用户输入变成「回答」或「普通提问」两种历史行
- ``format_ask_cli``      CLI 侧的提问展示文本
"""

from __future__ import annotations

import hashlib
from typing import Any

# 工具名。与 src/agent/tools.py 里注册的工具保持一致。
ASK_USER_TOOL_NAME = "ask_user"

# 选项数量上限 —— 超过就截断，太多选项用户也不会认真看。
MAX_OPTIONS = 4


def normalize_options(raw: Any) -> list[dict]:
    """把模型给的 options 归一化成固定形状。

    规则：
    - ``label`` 必须是非空字符串，否则整项丢弃
    - ``description`` 非字符串则置空串
    - ``recommended`` 只认 True，且最多保留第一项为 True
    - 最多保留 ``MAX_OPTIONS`` 项
    """
    if not isinstance(raw, list):
        return []

    options: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        description = item.get("description")
        options.append({
            "label": label.strip(),
            "description": description.strip() if isinstance(description, str) else "",
            "recommended": item.get("recommended") is True,
        })

    options = options[:MAX_OPTIONS]

    # recommended 最多保留一项（模型标了多个时只认第一个）
    seen_recommended = False
    for opt in options:
        if not opt["recommended"]:
            continue
        if seen_recommended:
            opt["recommended"] = False
        else:
            seen_recommended = True

    return options


def _synthesize_id(question: str) -> str:
    """给没有 id 的 tool_use 生成一个稳定 id。

    必须确定性：同一个问题算出的 id 永远一样，否则回答配不上提问。
    """
    digest = hashlib.sha1(question.encode("utf-8")).hexdigest()[:8]
    return f"ask_{digest}"


def build_ask_payload(tool_use: dict) -> dict | None:
    """从一个 tool_use 块解析出提问内容。

    ``tool_use`` 形如 ``{"id": ..., "name": ..., "input": {...}}``。
    不是 ask_user、或者 question 为空时返回 None。
    """
    if not isinstance(tool_use, dict):
        return None
    if tool_use.get("name") != ASK_USER_TOOL_NAME:
        return None

    tool_input = tool_use.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    question = tool_input.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    question = question.strip()

    tool_call_id = tool_use.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        tool_call_id = _synthesize_id(question)

    return {
        "tool_call_id": tool_call_id,
        "question": question,
        "options": normalize_options(tool_input.get("options")),
    }


def detect_ask(tool_uses: list[dict]) -> dict | None:
    """在一批 tool_use 里认出提问，返回它的 payload（没有则 None）。"""
    if not isinstance(tool_uses, list):
        return None
    for tool_use in tool_uses:
        payload = build_ask_payload(tool_use)
        if payload is not None:
            return payload
    return None


def find_pending_ask(history: list[dict]) -> dict | None:
    """在会话历史里找出最后一个还没被回答的提问。

    ``history`` 是 session_manager 落库的 dict 列表：
    - assistant 行：``{"role": "assistant", "content": [块列表]}``，块里有
      ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``
    - tool 行：``{"role": "tool", "name": ..., "content": ..., "tool_call_id": ...}``

    配对只看 tool_call_id —— 同一个问题被问两次时，两个 id 不同，
    不能靠问题文本配对。
    """
    if not isinstance(history, list):
        return None

    answered = {
        row.get("tool_call_id")
        for row in history
        if isinstance(row, dict) and row.get("role") == "tool"
    }

    pending: dict | None = None
    for row in history:
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        content = row.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use" or block.get("name") != ASK_USER_TOOL_NAME:
                continue
            tool_call_id = block.get("id")
            if tool_call_id in answered:
                continue
            pending = {
                "tool_call_id": tool_call_id,
                "question": (block.get("input") or {}).get("question", ""),
                "options": normalize_options((block.get("input") or {}).get("options")),
            }
    return pending


def build_turn_row(pending_ask: dict | None, user_text: str) -> dict:
    """把用户输入变成一条历史行。

    有待答提问时，这一行输入就是那个提问的回答 —— 作为 tool_result 回填，
    模型才能从 tool_use 的断点继续；否则是普通用户消息。
    """
    if pending_ask:
        return {
            "role": "tool",
            "name": ASK_USER_TOOL_NAME,
            "content": user_text,
            "tool_call_id": pending_ask.get("tool_call_id", ""),
        }
    return {"role": "user", "content": user_text}


def build_answer_event(turn_row: dict) -> dict | None:
    """把「回答轮」的历史行翻译成一条 SSE 事件。

    普通用户轮返回 None —— 那种情况前端自己就有用户气泡了。
    回答轮返回一条带 ``tool_call_id`` 的 ``tool_result`` 事件，
    前端靠它把提问卡片标记成已回答。
    """
    if not isinstance(turn_row, dict) or turn_row.get("role") != "tool":
        return None
    return {
        "type": "tool_result",
        "name": turn_row.get("name", ASK_USER_TOOL_NAME),
        "result": turn_row.get("content", ""),
        "tool_call_id": turn_row.get("tool_call_id", ""),
        "step": 0,
    }


def sanitize_ask_message(response, ask: dict):
    """把响应里多余的 tool_use 丢掉，只留下提问。

    为什么必须丢：模型偶尔会「一边提问一边调工具」。那些兄弟 tool_use 没有
    对应的 tool_result，留在历史里会让下一轮上游 API 直接 400。

    同时保证提问的 tool_call id 非空 —— 回答要靠它配对，部分 OpenAI 兼容
    上游不返回 id。函数就地修改并返回同一个 response（不新建消息，避免
    丢掉 thinking 块）。
    """
    tool_call_id = ask.get("tool_call_id") or ""
    content = getattr(response, "content", None)

    if isinstance(content, list):
        # Anthropic 模式：content 里就是 tool_use 块
        kept = []
        for block in content:
            if not isinstance(block, dict):
                kept.append(block)
                continue
            if block.get("type") != "tool_use":
                kept.append(block)
                continue
            if block.get("name") != ASK_USER_TOOL_NAME:
                continue  # 兄弟工具 —— 丢掉
            if not block.get("id"):
                block["id"] = tool_call_id or _synthesize_id(
                    str((block.get("input") or {}).get("question", ""))
                )
            tool_call_id = block["id"]
            kept.append(block)
        response.content = kept
    else:
        # OpenAI 模式：tool_calls 是消息上的属性
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        kept_calls = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            if tc.get("name") != ASK_USER_TOOL_NAME:
                continue  # 兄弟工具 —— 丢掉
            if not tc.get("id"):
                tc["id"] = tool_call_id or _synthesize_id(
                    str((tc.get("args") or {}).get("question", ""))
                )
            tool_call_id = tc["id"]
            kept_calls.append(tc)
        if kept_calls:
            response.tool_calls = kept_calls

    ask["tool_call_id"] = tool_call_id
    return response


def format_ask_cli(ask: dict) -> str:
    """CLI 侧的提问展示文本。

    recommended 的选项排在最前面并标注「(推荐)」，与前端卡片保持一致。
    """
    question = ask.get("question", "")
    options = list(ask.get("options") or [])

    # recommended 排到第一位（稳定排序：其余保持原顺序）
    ordered = sorted(
        enumerate(options),
        key=lambda pair: 0 if pair[1].get("recommended") else 1,
    )
    ordered = [opt for _, opt in ordered]

    lines = [f"[提问] {question}"]
    for idx, opt in enumerate(ordered, start=1):
        label = opt.get("label", "")
        description = opt.get("description", "")
        badge = " (推荐)" if opt.get("recommended") else ""
        suffix = f" — {description}" if description else ""
        lines.append(f"  {idx}. {label}{badge}{suffix}")
    lines.append("  → 直接输入你的回答即可：")
    return "\n".join(lines)
