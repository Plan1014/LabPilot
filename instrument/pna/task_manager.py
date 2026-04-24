"""Async task manager for PNA measurements."""

import uuid
from datetime import datetime
from typing import Optional, Dict
from threading import Lock


class TaskManager:
    """Thread-safe in-memory task storage."""

    def __init__(self):
        self._tasks: Dict[str, dict] = {}
        self._lock = Lock()

    def create_task(self) -> str:
        """Create a new pending task and return its ID."""
        task_id = str(uuid.uuid4())[:8]
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "created_at": datetime.utcnow(),
                "completed_at": None,
                "result": None,
                "error": None,
                "csv_path": None,
            }
        return task_id

    def get_task(self, task_id: str) -> Optional[dict]:
        """Retrieve task by ID. Returns None if not found."""
        with self._lock:
            return self._tasks.get(task_id)

    def update_task(self, task_id: str, **kwargs):
        """Update task fields."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(kwargs)

    def complete_task(self, task_id: str, result: dict, csv_path: str):
        """Mark task as completed with result."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update({
                    "status": "completed",
                    "completed_at": datetime.utcnow(),
                    "result": result,
                    "csv_path": csv_path,
                })

    def fail_task(self, task_id: str, error: str):
        """Mark task as failed with error message."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update({
                    "status": "failed",
                    "completed_at": datetime.utcnow(),
                    "error": error,
                })

    def cancel_task(self, task_id: str) -> bool:
        """Attempt to cancel a task. Returns True if cancelled, False if not found or already completed."""
        with self._lock:
            if task_id not in self._tasks:
                return False
            status = self._tasks[task_id]["status"]
            if status in ("pending", "running"):
                self._tasks[task_id]["status"] = "cancelled"
                self._tasks[task_id]["completed_at"] = datetime.utcnow()
                return True
            return False

    def list_tasks(self) -> Dict[str, dict]:
        """Return all tasks."""
        with self._lock:
            return dict(self._tasks)

    def has_running_task(self) -> bool:
        """Check if there's any task currently running."""
        with self._lock:
            return any(t["status"] == "running" for t in self._tasks.values())


# Global instance
task_manager = TaskManager()