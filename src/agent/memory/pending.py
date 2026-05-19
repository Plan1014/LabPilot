"""SQLite-based pending summary cache for cross-session memory aggregation"""

import json
import sqlite3
import threading
from typing import List, Optional

_memory_lock = threading.Lock()


class PendingCache:
    """SQLite-based pending summary cache for cross-session memory aggregation"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from src.agent.config import WORKDIR
            data_dir = WORKDIR / "data" / "memory"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / "pending_cache.db")

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        """Create pending_cache table if not exists"""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    messages_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def save_pending(self, messages: list) -> int:
        """Save pending messages, returns row id"""
        with _memory_lock:
            cursor = self.conn.execute(
                "INSERT INTO pending_cache (messages_json) VALUES (?)",
                (json.dumps(messages, ensure_ascii=False),)
            )
            self.conn.commit()
            return cursor.lastrowid

    def get_pending(self) -> List[dict]:
        """Get all pending message lists, merged into single list"""
        with _memory_lock:
            cursor = self.conn.execute(
                "SELECT messages_json FROM pending_cache ORDER BY created_at"
            )
            result = []
            for row in cursor.fetchall():
                result.extend(json.loads(row["messages_json"]))
            # 保护：最多保留最近 100 条，避免跨 session 累积爆炸
            return result[-100:] if len(result) > 100 else result

    def clear_pending(self):
        """Clear all pending messages"""
        with _memory_lock:
            with self.conn:
                self.conn.execute("DELETE FROM pending_cache")

    def close(self):
        """Close database connection"""
        self.conn.close()