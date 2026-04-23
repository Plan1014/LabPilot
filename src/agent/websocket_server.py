"""WebSocket server for real-time task notifications from Linien GUI."""

import asyncio
import json
import threading
import queue
from datetime import datetime
from typing import Set, Optional, Callable
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
import os

# Load config from environment
WEBSOCKET_PORT = int(os.getenv("WEBSOCKET_PORT", "8001"))
WEBSOCKET_ENABLED = os.getenv("WEBSOCKET_ENABLED", "true").lower() == "true"


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
        self._lock = threading.RLock()  # RLock allows re-entrant locking from same thread
        self._is_idle = True
        self._trigger_callback: Optional[Callable[[str], None]] = None
        self._processing = False  # Flag to prevent re-entrant processing

    def set_trigger_callback(self, callback: Callable[[str], None]):
        """Set callback to trigger agent with user message."""
        self._trigger_callback = callback

    def set_idle(self, is_idle: bool):
        """Set REPL idle state. When transitioning to idle, process queue."""
        with self._lock:
            was_idle = self._is_idle
            self._is_idle = is_idle

        # When transitioning from busy to idle, process all queued notifications merged
        if not was_idle and is_idle:
            self._process_all()

    def put(self, message: dict):
        """Add a notification to the queue.

        If idle, immediately trigger agent. Otherwise, queue for later.
        """
        with self._lock:
            if self._is_idle and not self._processing:
                self._trigger(message)
            else:
                self._queue.put(message)

    def _trigger(self, messages):
        """Trigger agent with formatted notification message(s)."""
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
        """Drain and merge all queued notifications into a single trigger."""
        if self._processing:
            return

        # Drain all messages from queue
        messages = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if messages:
            self._trigger(messages)

    def clear(self):
        """Clear the queue of all pending notifications."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def format_for_user(self, messages) -> str:
        """Format notification(s) as user-facing text.

        Accepts a single dict or a list of dicts.
        Multiple messages are merged into one notification.
        Format: [WebSocket] {count} notifications:\n  - {type}: {content}\n  - ...
        """
        # Normalize to list
        if isinstance(messages, dict):
            messages = [messages]

        if len(messages) == 1:
            msg = messages[0]
            msg_type = msg.get("type", "unknown")
            timestamp = msg.get("timestamp", "")
            result = msg.get("result", {})
            content = self._format_result(result)
            ts_str = f"[{timestamp}] " if timestamp else ""
            return f"[WebSocket] {ts_str}{msg_type}: {content}"

        # Multiple messages: merge them
        lines = [f"[WebSocket] {len(messages)} notifications:"]
        for msg in messages:
            msg_type = msg.get("type", "unknown")
            timestamp = msg.get("timestamp", "")
            result = msg.get("result", {})
            content = self._format_result(result)
            ts_str = f"[{timestamp}] " if timestamp else ""
            lines.append(f"  - {ts_str}{msg_type}: {content}")

        return "\n".join(lines)

    def _format_result(self, result) -> str:
        """Format a single result dict to user-facing string."""
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
        """Check if queue is empty."""
        return self._queue.empty()

    def size(self) -> int:
        """Get approximate queue size."""
        return self._queue.qsize()


# Global notification queue instance
notification_queue = NotificationQueue()


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

    def handle_notification(self, task_id: str, result: dict):
        """Handle incoming notification from Linien GUI.

        Queues the notification. If REPL is idle, triggers agent immediately.
        """
        message = {
            "type": "task_completed",
            "task_id": task_id,
            "status": "completed",
            "result": result,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        notification_queue.put(message)


# Global connection manager
_manager = ConnectionManager()


def create_websocket_router() -> FastAPI:
    """Create FastAPI app with WebSocket endpoint."""
    from fastapi import APIRouter
    router = APIRouter()

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await _manager.connect(websocket)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=30.0
                    )
                    if data == "ping":
                        await websocket.send_text("pong")
                    else:
                        try:
                            msg = json.loads(data)
                            if msg.get("type") == "task_completed":
                                task_id = msg.get("task_id")
                                result = msg.get("result", {})
                                _manager.handle_notification(task_id, result)
                                await websocket.send_json({"status": "received"})
                        except json.JSONDecodeError:
                            pass
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

    @router.get("/ws/status")
    async def ws_status():
        with _manager._lock:
            count = len(_manager.active_connections)
        return {
            "websocket_enabled": WEBSOCKET_ENABLED,
            "active_connections": count,
            "pending_notifications": notification_queue.size()
        }

    return router


def start_websocket_server_thread(port: int = WEBSOCKET_PORT) -> Optional[threading.Thread]:
    """Start WebSocket server in background thread."""
    if not WEBSOCKET_ENABLED:
        return None

    import uvicorn

    app = FastAPI()
    app.include_router(create_websocket_router())

    def run():
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def get_notification_queue() -> NotificationQueue:
    """Get the global notification queue instance."""
    return notification_queue


def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager instance."""
    return _manager
