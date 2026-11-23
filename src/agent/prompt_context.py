"""提示词拼装：来源标签、工具名索引、检索记忆渲染、历史消息构造。

模块级只 import stdlib 和 src.agent.config（config 是叶子，只依赖 dotenv）——
不能 import src.agent.tools / src.agent.memory，否则会形成
archival_store → prompt_context → tools → src.agent.memory → archival → archival_store
的循环。
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.agent.config import SYSTEM_PROMPT_TEMPLATE, WORKDIR

DEFAULT_RELEVANCE_THRESHOLD = 0.45
MEMORY_RELEVANCE_THRESHOLD_ENV = "MEMORY_RELEVANCE_THRESHOLD"

FACT_CANDIDATES = 5
SUMMARY_CANDIDATES = 3
RETRIEVED_MEMORY_LIMIT = 3
SUMMARY_MEMORY_LIMIT = 1


@dataclass
class FactEntry:
    text: str
    distance: float
    timestamp: float | None = None


@dataclass
class SummaryEntry:
    text: str
    distance: float
    timestamp: float | None = None


@dataclass
class RetrievedMemory:
    facts: list[FactEntry] = field(default_factory=list)
    summaries: list[SummaryEntry] = field(default_factory=list)


def filter_by_distance(entries, threshold):
    """按 cosine distance 过滤。threshold=None 表示不过门槛。"""
    if threshold is None:
        return list(entries)
    return [e for e in entries if e.distance <= threshold]


def relevance_threshold() -> float:
    """自动注入的记忆的相关性门槛（cosine distance，越小越相关）。"""
    raw = os.getenv(MEMORY_RELEVANCE_THRESHOLD_ENV, "").strip()
    if not raw:
        return DEFAULT_RELEVANCE_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_RELEVANCE_THRESHOLD


def tool_name_index(tools) -> str:
    """从工具列表生成一行名字索引。

    工具清单必须动态生成 —— 加工具自动出现，不写死。
    """
    names = []
    for t in tools or []:
        name = getattr(t, "name", "")
        if name:
            names.append(name)
    return ", ".join(names)


def build_system_prompt(tools, skills: str, now=None) -> str:
    """系统提示词 = 基础模板 + 来源说明 + 工具名索引 + 当前时间。"""
    now = now or datetime.now(timezone.utc)
    return SYSTEM_PROMPT_TEMPLATE.format(
        workdir=str(WORKDIR),
        skills=skills,
        tools=tool_name_index(tools),
        now=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def label_user_message(text: str) -> str:
    return f"<UserMessage>{text}</UserMessage>"


def label_user_answer(text: str, name: str) -> str:
    return f'<UserAnswer name="{name}">{text}</UserAnswer>'


def label_tool_result(text: str, name: str) -> str:
    return f'<ToolResult name="{name}">{text}</ToolResult>'


def _fmt_epoch(ts: float | None) -> str:
    """epoch 秒 → YYYY-MM-DD；没有时间戳就返回空串（调用方省略该字段）。"""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def render_retrieved_memory(retrieved: RetrievedMemory, now=None) -> str:
    """渲染 <RetrievedMemory> 区块；没有内容时返回空串（调用方整块跳过）。"""
    if retrieved is None:
        return ""
    lines = []
    if retrieved.summaries:
        lines.append("【相关历史对话摘要】")
        for s in retrieved.summaries:
            lines.append(f"- (相似度 {s.distance:.3f}{_stamp_suffix(s.timestamp)}) {s.text}")
    if retrieved.facts:
        lines.append("【相关事实记忆 (参数/结论)】")
        for f in retrieved.facts:
            lines.append(f"- (相似度 {f.distance:.3f}{_stamp_suffix(f.timestamp)}) {f.text}")
    if not lines:
        return ""
    now = now or datetime.now(timezone.utc)
    head = (f"检索时间 {now.strftime('%Y-%m-%d %H:%M:%S UTC')}，"
            "以下内容由系统自动附加，可能陈旧或无关，仅在与当前问题相关时采用：")
    return "<RetrievedMemory>\n" + head + "\n" + "\n".join(lines) + "\n</RetrievedMemory>"


def _stamp_suffix(ts: float | None) -> str:
    stamp = _fmt_epoch(ts)
    return f", 记录于 {stamp}" if stamp else ""
