"""调试输出：把「实际发给模型的那一整套输入」原样呈现出来。

两种用法：

1. 环境变量 ``PROMPT_DEBUG=1`` —— 每一轮组装完自动打印到后端终端
   （REPL 与 HTTP 两条路都打）。
2. HTTP 接口 —— ``POST /debug/prompt`` 现场拼一次，
   ``GET /session/{id}/prompt`` 按已有会话复盘。

打印的就是最终送进模型的消息列表：不截断、不改写、不加工，
模型看到什么，这里就是什么。
"""

import os
import unicodedata
from datetime import datetime, timezone

from src.agent.prompt_context import _content_to_text

PROMPT_DEBUG_ENV = "PROMPT_DEBUG"

_TRUTHY = {"1", "true", "yes", "on"}

# 框线总宽（含左右两根竖线）
WIDTH = 65


def debug_enabled() -> bool:
    """PROMPT_DEBUG 环境开关 —— 只由环境变量控制，前端不用改。"""
    return os.getenv(PROMPT_DEBUG_ENV, "").strip().lower() in _TRUTHY


def _display_width(text: str) -> int:
    """显示宽度：CJK 字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _box_line(text: str) -> str:
    """一行框内文字（超出宽度就不补，框线会撑开）。"""
    pad = max(0, WIDTH - 3 - _display_width(text))
    return "│ " + text + " " * pad + "│"


def format_meta(meta: dict) -> list:
    """把诊断信息整理成 header 行。缺省的值不出现。"""
    meta = meta or {}
    lines = []
    if meta.get("session_id"):
        lines.append(f"session={meta['session_id']}")
    if meta.get("query"):
        lines.append(f'query="{meta["query"]}"')
    if meta.get("model"):
        lines.append(f"model={meta['model']}")
    if meta.get("threshold") is not None:
        lines.append(f"阈值={meta['threshold']}")
    stats = []
    if meta.get("tool_count") is not None:
        stats.append(f"工具 {meta['tool_count']} 个")
    if meta.get("retrieved_facts") is not None:
        stats.append(f"检索注入 {meta['retrieved_facts']} 条事实")
    if meta.get("retrieved_summaries") is not None:
        stats.append(f"{meta['retrieved_summaries']} 条摘要")
    if meta.get("history_rows") is not None:
        stats.append(f"历史 {meta['history_rows']} 行")
    if stats:
        lines.append(" · ".join(stats))
    return lines


def _role_of(msg) -> str:
    kind = getattr(msg, "type", "") or ""
    return {"human": "user", "ai": "assistant"}.get(kind, kind)


def _body(msg) -> str:
    """消息正文。

    只有 tool_use 的助手消息 content 是空的 —— 调试时要看得出它调了什么，
    不能显示成一片空白。
    """
    content = _content_to_text(getattr(msg, "content", msg))
    if content:
        return content
    calls = getattr(msg, "tool_calls", None) or []
    names = [c.get("name", "") for c in calls if isinstance(c, dict) and c.get("name")]
    if names:
        return "[tool_use: " + ", ".join(names) + "]"
    return content


def messages_to_dicts(messages) -> list:
    """消息列表 → 可 JSON 序列化的 {type, role, content}。"""
    out = []
    for msg in messages or []:
        row = {
            "type": getattr(msg, "type", "") or "",
            "role": _role_of(msg),
            "content": _body(msg),
        }
        name = getattr(msg, "name", None)
        if name:
            row["name"] = name
        call_id = getattr(msg, "tool_call_id", None)
        if call_id:
            row["tool_call_id"] = call_id
        out.append(row)
    return out


def format_prompt(messages, meta=None) -> str:
    """渲染成给人看的框。"""
    meta = dict(meta or {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    head = f"┌─ PROMPT ── {now} "
    lines = [head + "─" * max(0, WIDTH - 1 - _display_width(head)) + "┐"]
    for meta_line in format_meta(meta):
        lines.append(_box_line(meta_line))
    lines.append("├" + "─" * (WIDTH - 2) + "┤")

    total = 0
    count = 0
    for i, msg in enumerate(messages or [], 1):
        content = _body(msg)
        total += len(content)
        count += 1
        role = _role_of(msg)
        lines.append(_box_line(f"[{i}] {role} · {len(content)} 字符"))
        for content_line in content.split("\n"):
            lines.append(_box_line(content_line))

    footer = f"└─ {count} 条消息 · {total} 字符 "
    lines.append(footer + "─" * max(0, WIDTH - 1 - _display_width(footer)) + "┘")
    return "\n".join(lines)


def dump_prompt(messages, meta=None) -> None:
    """打印到后端终端。"""
    print(format_prompt(messages, meta))


def maybe_dump(messages, meta=None) -> bool:
    """PROMPT_DEBUG 打开时打印，返回是否打了。"""
    if not debug_enabled():
        return False
    dump_prompt(messages, meta)
    return True
