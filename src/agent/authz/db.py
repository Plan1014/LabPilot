"""鉴权台账的独立存储。

台账不能跟会话历史共用一个文件 —— 会话库在 `data/` 里，那是 agent 可写区，
agent 自己就能往 `auth_grants` 里塞一条授权，给自己签通行证（评审 I1）。

所以台账单独放 `.labpilot/authz.db`：那个目录不在 `WRITABLE_DIRS` 里，
`write_file` / `edit_file` 对它的写入按 L1 拒绝，bash 写它也按只读区拦。

路径跟着 `paths.WORKDIR` 走 —— 测试把工作区指到临时目录，台账就跟着走。
"""

import sqlite3
import threading
from pathlib import Path

from src.agent import paths

# 台账文件相对于工作区的位置。不在 tmp/ 或 data/ 里 —— 那两个是 agent 可写区。
DB_RELATIVE_PATH = Path(".labpilot") / "authz.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def db_path() -> Path:
    """台账文件的位置。"""
    return paths.WORKDIR / DB_RELATIVE_PATH


def get_conn() -> sqlite3.Connection:
    """台账连接。路径变了就换连接 —— 换了工作区就不会读到旧库。"""
    global _conn, _conn_path
    path = db_path()
    with _lock:
        if _conn is None or _conn_path != path:
            if _conn is not None:
                _conn.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(path), check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            _init_schema(_conn)
            _conn_path = path
        return _conn


def _init_schema(conn: sqlite3.Connection):
    """建台账的表。"""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_grants (
                id TEXT PRIMARY KEY,
                pattern TEXT NOT NULL,
                tool TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at REAL NOT NULL,
                session_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_batches (
                batch_id TEXT PRIMARY KEY,
                session_id TEXT,
                calls TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_pending (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                session_id TEXT,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_input TEXT NOT NULL,
                question TEXT,
                reason TEXT,
                pattern TEXT,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_auth_pending_session
            ON auth_pending(session_id)
        """)
