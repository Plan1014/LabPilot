"""NotificationHub - Central notification dispatcher on port 8000.

Receives task completion notifications from services (PDH, PNA, etc.)
via HTTP POST and broadcasts them to connected Agent WebSocket clients.

Architecture:
  - 8000: NotificationHub (this service)
  - 8001: PDH-Locking service
  - 8002: PNA service
  - ...: Additional services

All services POST to http://127.0.0.1:8000/notify when tasks complete.
Agent connects to ws://127.0.0.1:8000/ws to receive notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from datetime import datetime
from typing import Optional, Callable, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger("websocket_server")
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

for noisy in ["httpx", "httpcore", "charset_normalizer"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

from src.agent.memory import memory_system

# Load config from environment
NOTIFICATION_HUB_PORT = int(os.getenv("NOTIFICATION_HUB_PORT", "8000"))
NOTIFICATION_HUB_ENABLED = os.getenv("NOTIFICATION_HUB_ENABLED", "true").lower() == "true"


# ==================== Pydantic Models ====================

class NotifyRequest(BaseModel):
    """Payload from services when a task completes."""
    source: str
    task_id: str
    type: str
    result: Optional[dict] = None
    error: Optional[str] = None
    timestamp: Optional[str] = None


class QueryRequest(BaseModel):
    """Payload for /query endpoint."""
    query: str
    session_id: Optional[str] = None  # 省略 → 新建会话


class SessionQueryRequest(BaseModel):
    """Payload for /session/query endpoint."""
    query: str
    session_id: Optional[str] = None


class AuthRespondRequest(BaseModel):
    """Payload for /auth/respond endpoint."""
    session_id: str
    tool_call_id: str
    choice: str


# ==================== NotificationQueue ====================

class NotificationQueue:
    """Thread-safe notification queue for WebSocket messages.

    Decouples WebSocket message reception from agent processing.
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._is_idle = True
        self._trigger_callback: Optional[Callable[[str], None]] = None
        self._processing = False

    def set_trigger_callback(self, callback: Callable[[str], None]):
        self._trigger_callback = callback

    def set_idle(self, is_idle: bool):
        with self._lock:
            was_idle = self._is_idle
            self._is_idle = is_idle
        if not was_idle and is_idle:
            self._process_all()

    def put(self, message: dict):
        with self._lock:
            if self._is_idle and not self._processing:
                self._trigger(message)
            else:
                self._queue.put(message)

    def _trigger(self, messages):
        if self._trigger_callback:
            with self._lock:
                self._processing = True
            try:
                user_text = self.format_for_user(messages)
                self._trigger_callback(user_text)
            finally:
                with self._lock:
                    self._processing = False

    def _process_all(self):
        if self._processing:
            return
        messages = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if messages:
            self._trigger(messages)

    def clear(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def format_for_user(self, messages) -> str:
        if isinstance(messages, dict):
            messages = [messages]
        if len(messages) == 1:
            msg = messages[0]
            msg_type = msg.get("type", "unknown")
            source = msg.get("source", "")
            timestamp = msg.get("timestamp", "")
            result = msg.get("result", {})
            content = self._format_result(result)
            ts_str = f"[{timestamp}] " if timestamp else ""
            source_str = f"[{source}] " if source else ""
            boot_suffix = self._format_pdh_boot_suffix(source, result)
            return f"[WebSocket] {ts_str}{source_str}{msg_type}: {content}{boot_suffix}"
        lines = [f"[WebSocket] {len(messages)} notifications:"]
        for msg in messages:
            msg_type = msg.get("type", "unknown")
            source = msg.get("source", "")
            timestamp = msg.get("timestamp", "")
            result = msg.get("result", {})
            content = self._format_result(result)
            ts_str = f"[{timestamp}] " if timestamp else ""
            source_str = f"[{source}] " if source else ""
            boot_suffix = self._format_pdh_boot_suffix(source, result)
            lines.append(f"  - {ts_str}{source_str}{msg_type}: {content}{boot_suffix}")
        return "\n".join(lines)

    # ---- PDH-Locking 事件格式化 (参看 skills/pdh-locking/SKILL.md) ----

    @staticmethod
    def _format_pdh_boot_suffix(source: str, result) -> str:
        """对 source='pdh-locking' 事件追加 linien 端开机锚点信息。

        返回 '' 表示不是 pdh-locking 或 result 不含 since_boot_seconds。
        """
        if source != "pdh-locking" or not isinstance(result, dict):
            return ""
        since = result.get("since_boot_seconds")
        if since is None:
            return ""
        anchor = result.get("boot_anchor", "")
        suffix = f" (linien 开机后 {since}s"
        if anchor:
            suffix += f", anchor={anchor}"
        suffix += ")"
        return suffix

    def _format_result(self, result) -> str:
        if not isinstance(result, dict):
            return str(result)
        # 兼容旧 PI 计算任务 (source='pdh-locking' 但 event_type 不存在)
        if result.get("status") == "success":
            p = result.get("P")
            i = result.get("I")
            if p is not None and i is not None:
                return f"P={p}, I={i}"
            return result.get("message", str(result))
        # PDH-Locking 新事件类型 (event_type 由 linien 端 notifier 写入)
        event_type = result.get("event_type")
        if event_type == "lock_anomaly":
            return self._format_lock_anomaly(result)
        if event_type == "relock_failed":
            return result.get("message", str(result))
        if event_type == "log_errors":
            return self._format_log_errors(result)
        # 兜底: 取 message 字段
        return result.get("message", str(result))

    @staticmethod
    def _format_lock_anomaly(result: dict) -> str:
        count = result.get("count", "?")
        window = result.get("window_seconds", "?")
        acq = result.get("lock_acquired", 0)
        lst = result.get("lock_lost", 0)
        suggestion = result.get("suggestion", "")
        stats = f"{count} events in {window}s, {acq} acquired, {lst} lost"
        return f"{suggestion} ({stats})" if suggestion else stats

    @staticmethod
    def _format_log_errors(result: dict) -> str:
        n = result.get("new_count", 0)
        truncated = result.get("truncated", False)
        suffix = " (truncated)" if truncated else ""
        return f"{n} new WARNING/ERROR entries{suffix}"

    def is_empty(self) -> bool:
        return self._queue.empty()

    def size(self) -> int:
        return self._queue.qsize()


notification_queue = NotificationQueue()


# ==================== ConnectionManager ====================

class ConnectionManager:
    """Thread-safe WebSocket connection manager."""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = threading.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        with self._lock:
            self.active_connections.add(websocket)

    async def disconnect(self, websocket: WebSocket):
        with self._lock:
            self.active_connections.discard(websocket)

    async def broadcast(self, message: dict):
        with self._lock:
            connections = list(self.active_connections)
        disconnected = []
        for conn in connections:
            try:
                await conn.send_json(message)
            except Exception:
                disconnected.append(conn)
        for conn in disconnected:
            self.active_connections.discard(conn)


_manager = ConnectionManager()


# ==================== 事件去重键 ====================

def _event_dedup_key(event: dict) -> str:
    """事件去重键。同一批 SSE 事件里重复的事件只发一次。

    键里带 `step`，所以 agent 连续调三次 `bash` 不会被折叠成一张卡 ——
    每次 tool_call 都带 graph_thinking.generate 的 state.step_count。

    **`auth_request` 单独按 `tool_call_id` 编键**：这类事件里 name/content/
    result/input 全空，`step` 又是恒定的 0 —— 一批里两个 escalate 就会算出
    同一个键，第二张卡片在进 SSE 队列之前就被丢掉，用户永远看不到它，
    批次也收敛不了。tool_call_id 才是它们的天然身份。
    """
    event_type = event.get("type")
    if event_type == "done":
        # 只有第一个 done 有意义，后面的带的是过期 session_id
        return "done"
    if event_type == "auth_request":
        return f"auth_request:{event.get('tool_call_id', '')}"
    step = event.get("step", -1)
    return (
        f"{event_type}:{step}:{event.get('name', '')}:{event.get('content', '')}"
        f":{event.get('result', '')}:{event.get('input', '')}"
    )


# ==================== Pending Drain Helpers (fix/pending-session-id) ====================

def _spawn_drain_others_thread(current_session_id: str) -> None:
    """Trigger B: 后台异步 drain 所有"其它" WebSocket session 的 pending.

    调用点: session_query 入口 (req.session_id 确定后立即触发).
    设计:
      - 显式排除 session_id IS NULL (legacy/REPL),不误清
      - 显式排除 == current_session_id (本 session 由 Trigger A 处理)
      - 异步 daemon 线程,不阻塞 SSE 流
      - single-flight 由 memory_agent._drain_lock 处理,这里不重复加锁
      - 失败静默 (符合 memory agent 设计原则)
    """
    def _drain():
        try:
            from src.agent.memory import process_pending_others
            result = process_pending_others(current_session_id)
            if result:
                logger.info(
                    f"[Memory Agent] Trigger B drained for current={current_session_id[:8]}: {result[:200]}"
                )
        except Exception:
            pass

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()


# ==================== Prompt Debug ====================

def assemble_debug_prompt(query: str, session_id: Optional[str] = None,
                          include_query: bool = True):
    """拼出「这一轮会发给模型的东西」，不调模型。

    调试接口和 PROMPT_DEBUG 自动打印共用这一份装配逻辑 ——
    打印出来的必须就是真发出去的那一份。
    """
    from src.agent.session_manager import get_session_history, _compact_tool_results
    from src.agent.tools import TOOLS, SKILLS
    from src.agent.memory.core import CoreMemoryManager
    from src.agent.memory import memory_retriever
    from src.agent.prompt_context import (
        RetrievedMemory, build_prompt_messages, relevance_threshold,
    )
    from src.agent.ask_user import find_pending_ask, build_turn_row

    history = list(get_session_history(session_id)) if session_id else []
    history = _compact_tool_results(history)

    core_memory = CoreMemoryManager().compile()
    threshold = relevance_threshold()
    if query:
        retrieved = memory_retriever.retrieve(query, threshold=threshold)
    else:
        retrieved = RetrievedMemory()

    if include_query and query:
        history = history + [build_turn_row(find_pending_ask(history), query)]

    messages = build_prompt_messages(
        history=history,
        core_memory=core_memory,
        retrieved=retrieved,
        tools=TOOLS,
        skills=SKILLS.descriptions(),
    )
    meta = {
        "session_id": session_id,
        "query": query,
        "threshold": threshold,
        "tool_count": len(TOOLS),
        "retrieved_facts": len(retrieved.facts),
        "retrieved_summaries": len(retrieved.summaries),
        "history_rows": len(history),
    }
    return messages, meta


def debug_payload(messages, meta, tools=None) -> dict:
    """调试接口的返回体 —— 完整的一份：提示词正文 + 工具完整定义。"""
    from src.agent.prompt_debug import format_prompt, messages_to_dicts, tools_schema
    return {
        "meta": meta,
        "messages": messages_to_dicts(messages),
        "tools": tools_schema(tools or []),
        "text": format_prompt(messages, meta, tools),
    }


# ==================== Session Router ====================

def create_session_router():
    """Create the session management router."""
    from fastapi import APIRouter
    from src.agent.session_manager import (
        create_session, get_session_meta, get_session_history,
        append_to_session, update_session_title, delete_session,
        list_sessions, archive_session,
    )

    router = APIRouter()

    @router.get("/list")
    async def session_list():
        """List all sessions."""
        sessions = list_sessions()
        return {"sessions": sessions}

    @router.get("/{session_id}")
    async def get_session(session_id: str):
        """Get session metadata."""
        meta = get_session_meta(session_id)
        if not meta:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Session not found")
        return meta

    @router.get("/{session_id}/history")
    async def get_session_history_endpoint(session_id: str):
        """Get full message history for a session."""
        history = get_session_history(session_id)
        if not history:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Session not found")
        return {"id": session_id, "messages": history}

    @router.get("/{session_id}/prompt")
    async def get_session_prompt_endpoint(session_id: str):
        """调试：按这个会话现有历史拼出「这一轮会发给模型的东西」。不调模型。"""
        from src.agent.tools import TOOLS
        messages, meta = assemble_debug_prompt("", session_id, include_query=False)
        from src.agent.prompt_debug import maybe_dump
        maybe_dump(messages, meta, tools=TOOLS, full=True, track=False)
        return debug_payload(messages, meta, TOOLS)

    @router.delete("/{session_id}")
    async def delete_session_endpoint(session_id: str):
        """Delete a session."""
        deleted = delete_session(session_id)
        return {"status": "deleted" if deleted else "not_found"}

    @router.post("/query")
    async def session_query(req: SessionQueryRequest):
        """Stream SSE events with session history management."""
        logger.info("[/session/query] called — session_id=%s query_len=%d", req.session_id, len(req.query))
        from src.agent.ask_user import find_pending_ask, build_turn_row, build_answer_event
        from src.agent.session_manager import (
            _compact_tool_results,
            estimate_tokens, session_auto_compact,
        )

        # Get or create session
        if req.session_id:
            session_id = req.session_id
            history = get_session_history(session_id)
        else:
            session_id = create_session()
            history = []

        # === Trigger B: 后台异步清掉其它 WebSocket session 的 pending ===
        # 每次 session_query 都触发: 切回 session 时也能清理掉之前遗留的其它 session pending
        # 注意: REPL 完全不走这里,此触发仅在 WebSocket 上下文存在
        _spawn_drain_others_thread(current_session_id=session_id)
        # === Trigger B 触发结束 ===

        # Layer 1: micro_compact — replace old tool_results with placeholders
        history = _compact_tool_results(history)

        # === Core Memory 注入（缓存失效策略）===
        from src.agent.memory.core import CoreMemoryManager

        # 延迟初始化（在模块级变量缓存）
        if not hasattr(session_query, '_core_memory_manager'):
            session_query._core_memory_manager = CoreMemoryManager()
            session_query._last_memory_str = ""

        core_memory_manager = session_query._core_memory_manager

        # 编译当前 memory
        new_memory_str = core_memory_manager.compile()

        # 缓存失效检测
        if new_memory_str != session_query._last_memory_str:
            session_query._last_memory_str = new_memory_str

        compiled_memory = new_memory_str
        # === Core Memory 注入结束 ===

        # === RAG 检索 fact 库 ===
        # 自动注入的记忆要过相关性门槛；门槛外的噪音宁可不注入。
        from src.agent.memory import memory_retriever
        from src.agent.prompt_context import relevance_threshold
        rag_retrieved = memory_retriever.retrieve(
            req.query, threshold=relevance_threshold()
        )
        # === RAG 检索结束 ===

        # === 待批批次：有待批记录时用户直接敲字 → 视为 deny（spec §5.4）===
        recovery_rows = _deny_earliest_pending_if_typed(session_id, req.query)

        # 有待答提问时，本次 query 是对上一问的回答 —— 作为 tool_result 回填，
        # 模型从 tool_use 断点继续；否则是普通用户消息。
        pending_ask = find_pending_ask(history)
        turn_row = build_turn_row(pending_ask, req.query)

        # Track new messages added during this stream (for saving to Redis)
        new_messages: list[dict] = list(recovery_rows)
        new_messages.append(turn_row)

        # 回答轮不覆盖会话标题
        if not history and turn_row["role"] == "user":
            first_title = req.query[:100] if len(req.query) > 100 else req.query
            update_session_title(session_id, first_title)

        # 回答轮补一条 tool_result 事件 —— 前端靠 tool_call_id 把提问卡片
        # 标记成已回答。
        answer_event = build_answer_event(turn_row)

        return await _stream_agent_turn(
            session_id=session_id,
            history=history,
            turn_rows=list(recovery_rows) + [turn_row],
            compiled_memory=compiled_memory,
            retrieved=rag_retrieved,
            new_messages=new_messages,
            extra_events=(
                ([answer_event] if answer_event else [])
                + _tool_row_events(recovery_rows)
            ),
            query_label=req.query,
        )

    return router


async def _stream_agent_turn(
    *,
    session_id: str,
    history: list[dict],
    turn_rows: list[dict],
    compiled_memory: str = "",
    retrieved=None,
    new_messages: list[dict] | None = None,
    extra_events: list[dict] | None = None,
    query_label: str = "",
) -> StreamingResponse:
    """跑一轮 agent，把 SSE 吐给前端。

    `turn_rows` —— 本轮要进模型的消息行（用户消息 / tool_result）。
    `new_messages` —— 本轮结束后落库的行；默认与 `turn_rows` 相同。
    `extra_events` —— 进 SSE 队列的前置事件（如 ask_user 的回答事件）。

    普通对话和提权恢复走同一条路 —— 恢复出来的 tool_result 和普通轮的用户
    消息在模型眼里没有区别。
    """
    from src.agent.graph_thinking import build_graph, set_event_emitter
    from src.agent.prompt_context import build_prompt_messages, relevance_threshold
    from src.agent.session_manager import message_to_dict
    from src.agent.tools import SKILLS, TOOLS

    graph = build_graph()
    event_queue: queue.Queue = queue.Queue()
    seen_keys: set[str] = set()
    if new_messages is None:
        new_messages = list(turn_rows)

    def emitter(event: dict):
        key = _event_dedup_key(event)
        if key not in seen_keys:
            seen_keys.add(key)
            event_queue.put(event)

    async def event_generator():
        set_event_emitter(emitter)
        try:
            # 统一拼装：系统提示词（动态工具索引）+ Core Memory + 检索记忆 +
            # 历史/当前轮，每条都带来源标签。
            langchain_messages = build_prompt_messages(
                history=list(history) + list(turn_rows),
                core_memory=compiled_memory,
                retrieved=retrieved,
                tools=TOOLS,
                skills=SKILLS.descriptions(),
            )

            # PROMPT_DEBUG=1 —— 把这一轮完整输入打到后端终端（第二轮起只打增量）
            from src.agent.prompt_debug import maybe_dump
            maybe_dump(langchain_messages, {
                "session_id": session_id,
                "query": query_label,
                "threshold": relevance_threshold(),
                "tool_count": len(TOOLS),
                "retrieved_facts": len(getattr(retrieved, "facts", []) or []),
                "retrieved_summaries": len(getattr(retrieved, "summaries", []) or []),
                "history_rows": len(history) + len(turn_rows),
            }, tools=TOOLS, state_key=session_id)

            for ev in extra_events or []:
                event_queue.put(ev)

            # Stream events
            async for event in graph.astream_events(
                {"messages": langchain_messages, "pending_tool_calls": [], "step_count": 0, "session_id": session_id},
                config={"recursion_limit": 100},
                stream_mode="values",
            ):
                event_type = event.get("event", "")
                if event_type == "on_chain_end":
                    # Capture new messages from this event
                    chain_output = event.get("data", {}).get("output", {})
                    if isinstance(chain_output, dict) and "messages" in chain_output:
                        msgs = chain_output["messages"]
                        if isinstance(msgs, list):
                            for msg in msgs[len(langchain_messages):]:
                                new_messages.append(message_to_dict(msg))
                    elif isinstance(chain_output, list):
                        for msg in msgs[len(langchain_messages):]:
                            new_messages.append(message_to_dict(msg))

                    # Drain queue → sort by step → yield immediately
                    pending: list[dict] = []
                    while True:
                        try:
                            pending.append(event_queue.get_nowait())
                        except queue.Empty:
                            break
                    pending.sort(key=lambda e: e.get("step", 0))
                    for ev in pending:
                        yield f"data: {json.dumps(ev)}\n\n"
            # 最后再 drain 一次 —— 回答事件是在循环开始前 put 的，
            # 最后一个 on_chain_end 之后产生的事件也不能丢。
            pending: list[dict] = []
            while True:
                try:
                    pending.append(event_queue.get_nowait())
                except queue.Empty:
                    break
            for ev in pending:
                yield f"data: {json.dumps(ev)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'step': 0, 'session_id': session_id})}\n\n"
        finally:
            set_event_emitter(None)
            seen_keys.clear()

    # Return the streaming response; messages will be saved after iteration completes
    return StreamingResponse(
        _stream_and_save(event_generator(), session_id, new_messages, history),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )


# ==================== 鉴权路由 ====================

def _resolve_batch_tool_rows(batch: dict) -> list[dict]:
    """批次裁决完了 → 为每个调用产出一条 tool_result 行（原始顺序）。

    一个 tool_use 缺了配对的 tool_result，上游 API 会直接报 400 —— 这里
    必须一条不落，包括被拒绝的那几条。

    行里额外带一个 `auth_choice` —— 前端靠它把待批卡片标成「已允许/已拒绝」。
    落库时这个键会被忽略（append_to_session 只取 role/content/name/tool_call_id）。
    """
    from src.agent.authz import pending as authz_pending
    from src.agent.graph_thinking import run_single_tool

    rows: list[dict] = []
    for call in batch.get("calls") or []:
        tool_call_id = call.get("tool_call_id") or ""
        tool_name = call.get("tool_name") or ""
        tool_input = call.get("tool_input") or {}
        verdict = call.get("verdict")
        choice = "allow_once"
        if verdict == "allow":
            content = run_single_tool(tool_name, tool_input)
        elif verdict == "deny":
            choice = "deny"
            content = f"授权拒绝：{call.get('reason') or '该操作被拒绝'}"
        else:
            # escalate —— 看用户的裁决
            row = authz_pending.get_pending_by_tool_call_id(tool_call_id)
            choice = (row or {}).get("status") or "deny"
            if choice == "deny":
                content = "授权拒绝：用户拒绝了该操作"
            else:
                content = run_single_tool(tool_name, tool_input)
        rows.append(
            {
                "role": "tool",
                "content": content,
                "name": tool_name,
                "tool_call_id": tool_call_id,
                "auth_choice": choice,
            }
        )
    return rows


def _tool_row_events(rows: list[dict]) -> list[dict]:
    """把补齐的 tool_result 行变成 SSE 事件 —— 前端据此关掉待批卡片。"""
    return [
        {
            "type": "tool_result",
            "result": r.get("content", ""),
            "name": r.get("name", ""),
            "tool_call_id": r.get("tool_call_id", ""),
            "auth_choice": r.get("auth_choice", "allow_once"),
            "step": 0,
        }
        for r in rows
    ]


def _deny_earliest_pending_if_typed(session_id: str, query: str) -> list[dict]:
    """用户有待批记录时直接敲了文字 → 最早的待批项判 deny（spec §5.4）。

    不去解析自由文本 —— 猜用户的意思比拒绝更危险。返回要补进本轮的
    tool_result 行；批次还没解决就 409，让用户先在卡片上选。
    """
    from src.agent.authz import pending as authz_pending

    if not (query or "").strip():
        return []
    rows = authz_pending.list_pending(session_id)
    if not rows:
        return []
    earliest = rows[0]
    authz_pending.record_decision(earliest["tool_call_id"], "deny")
    if not authz_pending.batch_is_resolved(earliest["batch_id"]):
        remaining = len(authz_pending.list_pending(session_id))
        raise HTTPException(
            status_code=409,
            detail=f"还有 {remaining} 个操作待确认，请先在卡片上选择",
        )
    batch = authz_pending.get_batch(earliest["batch_id"])
    if not batch:
        return []
    return _resolve_batch_tool_rows(batch)


async def _done_only_stream():
    """只回一个 done —— 批次还没全部裁决完时，前端不用等图。"""
    yield f"data: {json.dumps({'type': 'done', 'step': 0})}\n\n"


def create_auth_router():
    """鉴权路由 —— 待批查询、授权台账、提权回复。"""
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/pending")
    async def auth_pending(session_id: str):
        """该会话还没裁决的调用。"""
        from src.agent.authz import pending as authz_pending

        return {"pending": authz_pending.list_pending(session_id)}

    @router.get("/grants")
    async def auth_grants(session_id: str | None = None, scope: str | None = None):
        """授权台账 —— 用户批准过什么。"""
        from src.agent.authz import ledger

        return {"grants": ledger.list_grants(session_id=session_id, scope=scope)}

    @router.delete("/grants/{grant_id}")
    async def auth_revoke(grant_id: str):
        """撤销一条授权。"""
        from src.agent.authz import ledger

        return {"ok": ledger.revoke_grant(grant_id)}

    @router.post("/respond")
    async def auth_respond(req: AuthRespondRequest):
        """用户裁决一个待批调用。批次全部裁决完 → 补齐 tool_result 并重跑图。"""
        from src.agent.authz import ledger
        from src.agent.authz import pending as authz_pending
        from src.agent.session_manager import get_session_history

        if req.choice not in authz_pending.VALID_CHOICES:
            raise HTTPException(
                status_code=400, detail=f"invalid choice: {req.choice!r}"
            )

        row = authz_pending.record_decision(req.tool_call_id, req.choice)
        if row is None:
            raise HTTPException(status_code=404, detail="没有这个待批调用")

        # 「允许并记住」—— 模式由用户授权，不是模型给的
        if req.choice == "allow_always" and row.get("pattern"):
            ledger.record_grant(row["pattern"], session_id=req.session_id)

        if not authz_pending.batch_is_resolved(row["batch_id"]):
            return StreamingResponse(
                _done_only_stream(), media_type="text/event-stream"
            )

        batch = authz_pending.get_batch(row["batch_id"])
        if not batch:
            raise HTTPException(status_code=404, detail="批次不存在")
        tool_rows = _resolve_batch_tool_rows(batch)
        history = get_session_history(req.session_id)
        return await _stream_agent_turn(
            session_id=req.session_id,
            history=history,
            turn_rows=tool_rows,
            new_messages=list(tool_rows),
            extra_events=_tool_row_events(tool_rows),
            query_label="",
        )

    return router


async def _stream_and_save(generator, session_id: str, new_messages: list[dict], pre_compacted_history: list[dict]):
    """Wrap an async generator to save messages to SQLite after completion."""
    from src.agent.session_manager import append_to_session, archive_session, estimate_tokens
    from src.agent.config import TOKEN_THRESHOLD
    import traceback
    try:
        async for chunk in generator:
            yield chunk
        # Stream completed; save new messages
        if new_messages:
            try:
                append_to_session(session_id, new_messages)
            except Exception as e:
                traceback.print_exc()
        # Layer 2: auto_compact if token exceeds threshold
        try:
            from src.agent.session_manager import session_auto_compact
            total = estimate_tokens(pre_compacted_history) + estimate_tokens(new_messages)
            if total > TOKEN_THRESHOLD:
                session_auto_compact(session_id)
        except Exception:
            traceback.print_exc()
        # Archive to JSON
        try:
            archive_session(session_id)
        except Exception as e:
            traceback.print_exc()
    except Exception:
        # Stream error; still try to save partial messages
        if new_messages:
            try:
                append_to_session(session_id, new_messages)
            except Exception:
                pass
        raise


# ==================== SSE Streaming ====================

def create_memory_router():
    """Create the memory management router for frontend panel."""
    from fastapi import APIRouter
    from src.agent.memory import (
        list_all_facts, list_all_summaries,
        delete_fact, delete_summary,
        search_sessions as memory_search_sessions,
    )
    from src.agent.session_manager import list_sessions
    from src.agent.memory.core import CoreMemoryManager, Block

    _core_mgr: CoreMemoryManager | None = None

    def _get_core_mgr() -> CoreMemoryManager:
        nonlocal _core_mgr
        if _core_mgr is None:
            _core_mgr = CoreMemoryManager()
        return _core_mgr

    router = APIRouter()

    @router.get("/facts")
    async def get_facts(limit: int = 100, offset: int = 0):
        """List all stored facts."""
        return list_all_facts(limit=limit, offset=offset)

    @router.get("/summaries")
    async def get_summaries(limit: int = 100, offset: int = 0):
        """List all stored conversation summaries."""
        return list_all_summaries(limit=limit, offset=offset)

    @router.delete("/facts/{doc_id}")
    async def remove_fact(doc_id: str):
        """Delete a specific fact."""
        deleted = delete_fact(doc_id)
        return {"status": "deleted" if deleted else "not_found"}

    @router.delete("/summaries/{doc_id}")
    async def remove_summary(doc_id: str):
        """Delete a specific summary."""
        deleted = delete_summary(doc_id)
        return {"status": "deleted" if deleted else "not_found"}

    @router.get("/search")
    async def search_memory_endpoint(query: str):
        """Search memory via vector retrieval."""
        from src.agent.memory import memory_retriever
        result = memory_retriever.retrieve_context(query)
        return {"result": result}

    @router.get("/sessions/search")
    async def search_sessions_endpoint(query: str, days: int = 7):
        """Search historical sessions by keyword."""
        result = memory_search_sessions(query, days)
        return {"result": result}

    @router.get("/blocks")
    async def get_blocks():
        """List all Core Memory blocks (hot memory)."""
        blocks = _get_core_mgr().get_all_blocks()
        return {
            "items": [
                {
                    "label": b.label,
                    "value": b.value,
                    "description": b.description,
                    "limit": b.limit,
                    "read_only": b.read_only,
                    "created_at": b.created_at,
                    "updated_at": b.updated_at,
                }
                for b in blocks
            ]
        }

    @router.delete("/blocks/{label}")
    async def delete_block(label: str):
        """Delete a Core Memory block by label."""
        deleted = _get_core_mgr().delete_block(label)
        return {"status": "deleted" if deleted else "not_found"}

    return router


def create_sse_router():
    """Create the SSE query router (lazy import to avoid circular deps)."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/query")
    async def query_agent(req: QueryRequest):
        """Stream SSE events from agent graph execution."""
        logger.info("[/query] called — query_len=%d", len(req.query))
        from src.agent.session_manager import create_session, get_session_history

        # === Core Memory 注入（缓存失效策略）===
        from src.agent.memory.core import CoreMemoryManager

        if not hasattr(query_agent, '_core_memory_manager'):
            query_agent._core_memory_manager = CoreMemoryManager()
            query_agent._last_memory_str = ""

        core_memory_manager = query_agent._core_memory_manager
        compiled_memory = core_memory_manager.compile()
        # === Core Memory 注入结束 ===

        # === RAG 检索 fact 库 ===
        from src.agent.memory import memory_retriever
        from src.agent.prompt_context import relevance_threshold
        rag_retrieved = memory_retriever.retrieve(
            req.query, threshold=relevance_threshold()
        )
        # === RAG 检索结束 ===

        # 这条路也得有会话 —— 没有会话，escalate 就没人能裁决：待批批次存不下，
        # 卡片也发不到客户端，只会去问服务端终端（评审 C3）。
        session_id = req.session_id or create_session()
        history = get_session_history(session_id) if req.session_id else []

        return await _stream_agent_turn(
            session_id=session_id,
            history=history,
            turn_rows=[{"role": "user", "content": req.query}],
            compiled_memory=compiled_memory,
            retrieved=rag_retrieved,
            query_label=req.query,
        )

    return router


# ==================== FastAPI App ====================

def create_notification_hub_app() -> FastAPI:
    from fastapi import APIRouter
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="NotificationHub", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:1420",
            "http://127.0.0.1:1420",
            "tauri://localhost",
            "tauri://127.0.0.1",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # WebSocket + notify router
    notify_router = APIRouter()

    @notify_router.post("/notify")
    async def receive_notification(req: NotifyRequest):
        """Receive task completion notification and broadcast to all agents."""
        message = {
            "source": req.source,
            "task_id": req.task_id,
            "type": req.type,
            "result": req.result,
            "error": req.error,
            "timestamp": req.timestamp or datetime.utcnow().isoformat() + "Z",
        }
        # Queue for agent processing
        result_str = str(req.result) if req.result else (req.error or "")
        memory_system.update_task_state(
            task_id=req.task_id,
            status=req.type,
            result=result_str
        )
        notification_queue.put(message)
        await _manager.broadcast(message)
        return {"status": "received"}

    @notify_router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await _manager.connect(websocket)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                    if data == "ping":
                        await websocket.send_text("pong")
                except asyncio.TimeoutError:
                    try:
                        await websocket.send_text("ping")
                    except Exception:
                        break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await _manager.disconnect(websocket)

    app.include_router(notify_router)

    # Session router
    session_router = create_session_router()
    app.include_router(session_router, prefix="/session")

    # SSE query router (separate to avoid circular imports)
    sse_router = create_sse_router()
    app.include_router(sse_router)

    # Memory router
    memory_router = create_memory_router()
    app.include_router(memory_router, prefix="/memory")

    # 鉴权 router —— 待批批次、授权台账、提权回复
    app.include_router(create_auth_router(), prefix="/auth")

    # 调试 router —— 看「实际发给模型的完整输入」
    debug_router = APIRouter()

    @debug_router.post("/prompt")
    async def debug_prompt(req: SessionQueryRequest):
        """调试：给一条 query，现场拼出这一轮会发的内容。不调模型。"""
        from src.agent.tools import TOOLS
        messages, meta = assemble_debug_prompt(
            req.query, req.session_id, include_query=True
        )
        from src.agent.prompt_debug import maybe_dump
        maybe_dump(messages, meta, tools=TOOLS, full=True, track=False)
        return debug_payload(messages, meta, TOOLS)

    @debug_router.get("/authz")
    async def debug_authz(n: int = 100):
        """最近的鉴权判定 —— 看闸门到底放了什么、拒了什么。"""
        from src.agent.authz import audit

        return {"verdicts": audit.recent(n), "log_path": str(audit.authz_log_path())}

    app.include_router(debug_router, prefix="/debug")

    return app


# ==================== Thread Launcher ====================

def start_notification_hub_thread(port: int = NOTIFICATION_HUB_PORT) -> Optional[threading.Thread]:
    """Start NotificationHub server in background thread."""
    if not NOTIFICATION_HUB_ENABLED:
        return None

    import uvicorn

    app = create_notification_hub_app()

    def run():
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def get_notification_queue() -> NotificationQueue:
    return notification_queue


def get_connection_manager() -> ConnectionManager:
    return _manager
