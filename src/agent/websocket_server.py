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

import asyncio
import json
import threading
import queue
from datetime import datetime
from typing import Set, Optional, Callable
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
import os

# Load config from environment
NOTIFICATION_HUB_PORT = int(os.getenv("NOTIFICATION_HUB_PORT", "8000"))
NOTIFICATION_HUB_ENABLED = os.getenv("NOTIFICATION_HUB_ENABLED", "true").lower() == "true"


# ==================== Pydantic Models ====================

class NotifyRequest(BaseModel):
    """Payload from services when a task completes."""
    source: str  # "pdh-locking", "pna", etc.
    task_id: str
    type: str  # "task_completed", "task_failed"
    result: Optional[dict] = None
    error: Optional[str] = None
    timestamp: Optional[str] = None


# ==================== NotificationQueue ====================

class NotificationQueue:
    """Thread-safe notification queue for WebSocket messages.

    Decouples WebSocket message reception from agent processing.

    Trigger logic:
    - If REPL is idle: immediately trigger agent
    - If REPL is busy: queue for later processing
    - When transitioning busy->idle: drain all queued messages and merge into one trigger
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


# Global notification queue instance
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
        """Broadcast notification to all connected WebSocket clients."""
        with self._lock:
            connections = list(self.active_connections)

        disconnected = []
        for conn in connections:
            try:
                await conn.send_json(message)
            except Exception:
                disconnected.append(conn)

        # Clean up disconnected clients
        for conn in disconnected:
            self.active_connections.discard(conn)


# Global connection manager
_manager = ConnectionManager()


# ==================== FastAPI App ====================

def create_notification_hub_app() -> FastAPI:
    from fastapi import APIRouter
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="NotificationHub", lifespan=lifespan)
    router = APIRouter()

    # HTTP endpoint for services to POST notifications
    @router.post("/notify")
    async def receive_notification(req: NotifyRequest):
        """Receive task completion notification from a service and broadcast to all agents."""
        message = {
            "source": req.source,
            "task_id": req.task_id,
            "type": req.type,
            "result": req.result,
            "error": req.error,
            "timestamp": req.timestamp or datetime.utcnow().isoformat() + "Z",
        }

        # Queue for agent processing
        notification_queue.put(message)

        # Broadcast to all connected agents via WebSocket
        await _manager.broadcast(message)

        return {"status": "received"}

    # WebSocket endpoint for agents to connect
    @router.websocket("/ws")
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

    app.include_router(router)
    return app


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