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
import queue
import threading
from datetime import datetime
from typing import Optional, Callable, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

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


class SessionQueryRequest(BaseModel):
    """Payload for /session/query endpoint."""
    query: str
    session_id: Optional[str] = None


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
            return f"[WebSocket] {ts_str}{source_str}{msg_type}: {content}"
        lines = [f"[WebSocket] {len(messages)} notifications:"]
        for msg in messages:
            msg_type = msg.get("type", "unknown")
            source = msg.get("source", "")
            timestamp = msg.get("timestamp", "")
            result = msg.get("result", {})
            content = self._format_result(result)
            ts_str = f"[{timestamp}] " if timestamp else ""
            source_str = f"[{source}] " if source else ""
            lines.append(f"  - {ts_str}{source_str}{msg_type}: {content}")
        return "\n".join(lines)

    def _format_result(self, result) -> str:
        if isinstance(result, dict):
            if result.get("status") == "success":
                p = result.get("P")
                i = result.get("I")
                if p is not None and i is not None:
                    return f"P={p}, I={i}"
                return str(result)
            return result.get("message", str(result))
        return str(result)

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


# ==================== Session Router ====================

def create_session_router():
    """Create the session management router."""
    from fastapi import APIRouter
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
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

    @router.delete("/{session_id}")
    async def delete_session_endpoint(session_id: str):
        """Delete a session."""
        deleted = delete_session(session_id)
        return {"status": "deleted" if deleted else "not_found"}

    @router.post("/query")
    async def session_query(req: SessionQueryRequest):
        """Stream SSE events with session history management."""
        from src.agent.graph_thinking import build_graph, set_event_emitter
        from src.agent.session_manager import message_to_dict

        # Get or create session
        if req.session_id:
            session_id = req.session_id
            history = get_session_history(session_id)
        else:
            session_id = create_session()
            history = []

        # Set session_id in response headers for frontend to pick up
        graph = build_graph()
        event_queue: queue.Queue = queue.Queue()
        seen_keys: set[str] = set()

        # Generate title from first user message if this is a new session
        if not history:
            first_title = req.query[:100] if len(req.query) > 100 else req.query
            update_session_title(session_id, first_title)

        def emitter(event: dict):
            key = f"{event.get('type')}:{event.get('content', '')}:{event.get('name', '')}:{event.get('result', '')}"
            if key not in seen_keys:
                seen_keys.add(key)
                event_queue.put(event)

        # Track new messages added during this stream (for saving to Redis)
        new_messages: list[dict] = []
        # Start with user message
        user_msg_dict = {"role": "user", "content": req.query}
        new_messages.append(user_msg_dict)

        async def event_generator():
            set_event_emitter(emitter)
            try:
                # Build messages list from session history
                langchain_messages = []
                for msg in history:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user":
                        langchain_messages.append(HumanMessage(content=content))
                    elif role == "assistant":
                        langchain_messages.append(AIMessage(content=content))
                    elif role == "tool":
                        langchain_messages.append(ToolMessage(
                            content=content,
                            name=msg.get("name", ""),
                            tool_call_id=msg.get("tool_call_id", ""),
                        ))

                # Append current user message
                langchain_messages.append(HumanMessage(content=req.query))

                # Stream events
                async for event in graph.astream_events(
                    {"messages": langchain_messages, "pending_tool_calls": [], "step_count": 0},
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
                            for msg in chain_output[len(langchain_messages):]:
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
                yield f"data: {json.dumps({'type': 'done', 'step': 0, 'session_id': session_id})}\n\n"
            finally:
                set_event_emitter(None)
                seen_keys.clear()

        # Return the streaming response; messages will be saved after iteration completes
        return StreamingResponse(
            _stream_and_save(event_generator(), session_id, new_messages),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Session-Id": session_id,
            },
        )

    return router


async def _stream_and_save(generator, session_id: str, new_messages: list[dict]):
    """Wrap an async generator to save messages to Redis after completion."""
    from src.agent.session_manager import append_to_session, archive_session
    import traceback
    try:
        async for chunk in generator:
            yield chunk
        # Stream completed; save new messages to Redis
        if new_messages:
            try:
                append_to_session(session_id, new_messages)
            except Exception as e:
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

    return router


def create_sse_router():
    """Create the SSE query router (lazy import to avoid circular deps)."""
    from fastapi import APIRouter
    from langchain_core.messages import HumanMessage

    router = APIRouter()

    @router.post("/query")
    async def query_agent(req: QueryRequest):
        """Stream SSE events from agent graph execution."""
        from src.agent.graph_thinking import (
            build_graph, set_event_emitter, InterleavedState
        )
        graph = build_graph()
        event_queue: queue.Queue = queue.Queue()
        seen_keys: set[str] = set()

        def emitter(event: dict):
            key = f"{event.get('type')}:{event.get('content', '')}:{event.get('name', '')}:{event.get('result', '')}"
            if key not in seen_keys:
                seen_keys.add(key)
                event_queue.put(event)

        async def event_generator():
            set_event_emitter(emitter)
            try:
                initial_state = {
                    "messages": [HumanMessage(content=req.query)],
                    "pending_tool_calls": [],
                    "step_count": 0,
                }
                async for event in graph.astream_events(
                    initial_state,
                    config={"recursion_limit": 100},
                    stream_mode="values",
                ):
                    event_type = event.get("event", "")
                    if event_type == "on_chain_end":
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
                yield f"data: {json.dumps({'type': 'done', 'step': 0})}\n\n"
            finally:
                set_event_emitter(None)
                seen_keys.clear()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
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
