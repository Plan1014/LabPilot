"""SQLite-based pending summary cache for cross-session memory aggregation.

Schema:
    id INTEGER PRIMARY KEY AUTOINCREMENT
    session_id TEXT         -- NULL = legacy / REPL (REPL 设计为不写 pending)
    messages_json TEXT NOT NULL
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

Trigger 设计 (见 fix/pending-session-id 分支):
  - Trigger A (同 session 阈值): generate() 末尾, count_by_session(current) >= N 时触发
  - Trigger B (管其它): session_query 入口, list_other_session_ids + 逐个 drain
  - REPL 旁路: state.session_id=None 时 _save_to_pending 早返回,REPL 不写 pending
"""

import json
import sqlite3
import threading
from typing import List, Optional

_memory_lock = threading.Lock()


class PendingCache:
    """SQLite-based pending summary cache for cross-session memory aggregation."""

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
        """Create pending_cache table if not exists, migrate if needed."""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    messages_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        # 迁移: 旧表没有 session_id 列则加上 (默认 NULL)
        cursor = self.conn.execute("PRAGMA table_info(pending_cache)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "session_id" not in columns:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE pending_cache ADD COLUMN session_id TEXT"
                )

    def save_pending(self, messages: list, session_id: Optional[str] = None) -> int:
        """Save pending messages with optional session attribution.

        Args:
            messages: list of message dicts to save
            session_id: WebSocket session_id. None 表示无归属 (legacy).
                注意: REPL 调用方应在调用前自己早返回,不应依赖此参数。

        Returns:
            inserted row id
        """
        with _memory_lock:
            cursor = self.conn.execute(
                "INSERT INTO pending_cache (session_id, messages_json) VALUES (?, ?)",
                (session_id, json.dumps(messages, ensure_ascii=False)),
            )
            self.conn.commit()
            return cursor.lastrowid

    def get_pending(self) -> List[dict]:
        """Get all pending message lists, merged into single list (legacy API).

        新代码应使用 get_pending_by_session. 此接口忽略 session_id,仅供兼容。
        """
        with _memory_lock:
            cursor = self.conn.execute(
                "SELECT messages_json FROM pending_cache ORDER BY id"
            )
            result = []
            for row in cursor.fetchall():
                result.extend(json.loads(row["messages_json"]))
            # 保护：最多保留最近 100 条，避免跨 session 累积爆炸
            return result[-100:] if len(result) > 100 else result

    def clear_pending(self):
        """Clear all pending messages (legacy API, 不区分 session)."""
        with _memory_lock:
            with self.conn:
                self.conn.execute("DELETE FROM pending_cache")

    # ============ Session-based API (新, 见 fix/pending-session-id) ============

    def count_by_session(self, session_id: str) -> int:
        """Count pending rows for given session_id (Trigger A 用)."""
        with _memory_lock:
            cursor = self.conn.execute(
                "SELECT COUNT(*) FROM pending_cache WHERE session_id = ?",
                (session_id,),
            )
            return cursor.fetchone()[0]

    def get_pending_by_session(self, session_id: str) -> List[dict]:
        """Get pending messages for given session_id, ordered by id."""
        with _memory_lock:
            cursor = self.conn.execute(
                "SELECT messages_json FROM pending_cache "
                "WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
            result = []
            for row in cursor.fetchall():
                result.extend(json.loads(row["messages_json"]))
            return result

    def clear_pending_by_session(self, session_id: str) -> None:
        """Delete pending rows for given session_id."""
        with _memory_lock:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM pending_cache WHERE session_id = ?",
                    (session_id,),
                )

    def list_other_session_ids(self, exclude: str) -> List[str]:
        """List distinct session_ids that are NOT NULL and != exclude.

        Trigger B 用: 找出所有"其它 WebSocket session" 的 pending。
        显式排除 NULL —— 旧 legacy 行不会被这次设计误动。
        """
        with _memory_lock:
            cursor = self.conn.execute(
                "SELECT DISTINCT session_id FROM pending_cache "
                "WHERE session_id IS NOT NULL AND session_id != ?",
                (exclude,),
            )
            return [row["session_id"] for row in cursor.fetchall()]

    def close(self):
        """Close database connection."""
        self.conn.close()