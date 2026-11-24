"""调试输出：把「实际发给模型的那一整套输入」原样呈现出来。

原则：**忠实、完整**。模型收到的每一样东西都在这里 —— 提示词正文、核心记忆、
检索记忆、对话历史，以及随 API 的 ``tools`` 字段一起发出去的**工具完整定义**
（描述 + 参数 schema）。工具定义不在提示词正文里，但它同样是模型收到的输入。

排版按终端来：不用方框，缩进分节，长文本让终端自己换行。

第一轮打印完整的一份；之后每轮只打印**增量**（新增消息 + 变了的内容块），
免得每轮都刷一遍完整历史。HTTP 接口永远返回完整的一份。
"""

import json
import os
from datetime import datetime, timezone

from langchain_core.utils.function_calling import convert_to_openai_tool

from src.agent.prompt_context import _content_to_text

PROMPT_DEBUG_ENV = "PROMPT_DEBUG"

_TRUTHY = {"1", "true", "yes", "on"}

# 每个 state_key 记住上次打印到哪 —— 用来算增量
_LAST: dict = {}


def debug_enabled() -> bool:
    """PROMPT_DEBUG 环境开关 —— 只由环境变量控制，前端不用改。"""
    return os.getenv(PROMPT_DEBUG_ENV, "").strip().lower() in _TRUTHY


def reset_state(key=None) -> None:
    """清掉增量状态（测试用）。"""
    if key is None:
        _LAST.clear()
    else:
        _LAST.pop(key, None)


# ==================== 工具定义 ====================

def tools_schema(tools) -> list:
    """工具 → API 的 tools 字段那一份（name / description / parameters）。

    用的是真正发出去的那个转换器 —— 打印的和模型收到的是同一份。
    """
    schemas = []
    for tool in tools or []:
        try:
            schemas.append(convert_to_openai_tool(tool))
        except Exception:
            schemas.append({
                "type": "function",
                "function": {
                    "name": getattr(tool, "name", "?"),
                    "description": getattr(tool, "description", "") or "",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
    return schemas


def _render_tools(tools) -> str:
    """工具定义的终端渲染。"""
    if not tools:
        return ""
    schemas = tools_schema(tools)
    lines = [f"TOOLS ({len(schemas)}) —— 随 API 的 tools 字段一起发给模型，"
             "不在提示词正文里"]
    for schema in schemas:
        fn = schema.get("function", schema)
        lines.append(f"  {fn.get('name', '?')}")
        desc = (fn.get("description") or "").strip()
        if desc:
            desc_lines = desc.split("\n")
            lines.append(f"    description: {desc_lines[0]}")
            for extra in desc_lines[1:]:
                lines.append(f"      {extra}")
        params = fn.get("parameters")
        if params:
            lines.append("    parameters:")
            body = json.dumps(params, ensure_ascii=False, indent=2)
            lines.extend("      " + line for line in body.split("\n"))
    return "\n".join(lines)


# ==================== 消息渲染 ====================

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


def _indent(text: str, prefix: str = "    ") -> str:
    out = []
    for line in text.split("\n"):
        out.append(prefix + line if line else "")
    return "\n".join(out)


def _message_section(index: int, msg, content: str) -> str:
    return f"[{index}] {_role_of(msg)} · {len(content)} 字符\n{_indent(content)}"


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


def _split(messages):
    """拆成 系统提示词 / 核心记忆 / 检索记忆 / 对话消息。"""
    system = core = retrieved = None
    convo = []
    for i, msg in enumerate(messages or [], 1):
        content = _body(msg)
        if i == 1:
            system = (i, msg, content)
        elif content.startswith("<CoreMemory>"):
            core = (i, msg, content)
        elif content.startswith("<RetrievedMemory>"):
            retrieved = (i, msg, content)
        else:
            convo.append((i, msg, content))
    return system, core, retrieved, convo


def _without_time(content: str) -> str:
    """去掉 "Current time:" 那一行 —— 只变了时间不算内容变了。"""
    return "\n".join(line for line in content.split("\n")
                     if not line.startswith("Current time:"))


def _state_key(meta: dict, state_key) -> str:
    if state_key is not None:
        return str(state_key)
    meta = meta or {}
    return str(meta.get("session_id") or meta.get("state_key") or "default")


def render_prompt(messages, meta=None, tools=None, *, full=False, state_key=None,
                  track=True):
    """渲染成给人看的文本。

    full=True → 完整的一份（第一轮、或接口调用）。
    否则按 state_key 记住的上次状态，只打印增量。
    track=False → 纯渲染，不动增量状态（接口用，不影响正常轮次的增量）。
    """
    meta = dict(meta or {})
    msgs = list(messages or [])
    key = _state_key(meta, state_key)
    if not track:
        full = True
    prev = None if full else _LAST.get(key)

    system, core, retrieved, convo = _split(msgs)
    tools_text = _render_tools(tools)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"── PROMPT ── {now}"]
    lines.extend(format_meta(meta))

    if prev is None:
        lines.append("（完整输入）")
        if system:
            lines.append(_message_section(*system))
        if core:
            lines.append(_message_section(*core))
        if retrieved:
            lines.append(_message_section(*retrieved))
        for item in convo:
            lines.append(_message_section(*item))
        new_convo = len(convo)
    else:
        lines.append("（本轮增量 —— 相对上次打印）")
        new_convo = convo[prev["n_convo"]:]
        for item in new_convo:
            lines.append(_message_section(*item))
        # 没变的内容块只留一行说明，别再刷一遍
        for label, block, old in (("系统提示词", system, prev.get("system")),
                                  ("核心记忆", core, prev.get("core")),
                                  ("检索记忆", retrieved, prev.get("retrieved"))):
            if block is None:
                continue
            changed = old is None or _without_time(block[2]) != _without_time(old)
            if changed:
                lines.append(_message_section(*block))
            else:
                lines.append(f"[{label}] 未变（{len(block[2])} 字符）")

    if tools_text:
        if prev is not None and prev.get("tools") == tools_text:
            lines.append(f"[工具定义] 未变（{len(tools)} 个）")
        else:
            lines.append(tools_text)

    if prev is None:
        lines.append(f"── 完整输入 · {len(msgs)} 条消息 ──")
    else:
        lines.append(f"── 本轮新增 {len(new_convo)} 条消息 ──")

    if track:
        _LAST[key] = {
            "n_convo": len(convo),
            "system": system[2] if system else None,
            "core": core[2] if core else None,
            "retrieved": retrieved[2] if retrieved else None,
            "tools": tools_text,
        }
    return "\n".join(lines)


def format_prompt(messages, meta=None, tools=None, *, full=True, state_key=None) -> str:
    """渲染成给人看的文本（完整的一份，不动增量状态）。"""
    return render_prompt(messages, meta, tools, full=full, state_key=state_key,
                         track=False)


def dump_prompt(messages, meta=None, tools=None, *, full=False, state_key=None,
                track=True) -> None:
    """打印到后端终端。"""
    print(render_prompt(messages, meta, tools, full=full, state_key=state_key,
                        track=track))


def maybe_dump(messages, meta=None, tools=None, *, full=False, state_key=None,
               track=True) -> bool:
    """PROMPT_DEBUG 打开时打印，返回是否打了。"""
    if not debug_enabled():
        return False
    dump_prompt(messages, meta, tools, full=full, state_key=state_key, track=track)
    return True
