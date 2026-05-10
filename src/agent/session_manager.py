"""Session management with SQLite for short-term storage and JSON archiving.

SQLite is shared with memory.py (tasks.db). Sessions and messages tables
coexist with task_states table in the same DB file.
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from src.agent.config import WORKDIR, SESSIONS_DIR, SESSION_TTL_DAYS, TRANSCRIPT_DIR, TOKEN_THRESHOLD, MODEL_ID

MEMORY_DIR = WORKDIR / "data" / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_DB_PATH = MEMORY_DIR / "tasks.db"


# ==================== SQLite Connection ====================

_sql_lock = threading.Lock()
_sql_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _sql_conn
    if _sql_conn is None:
        _sql_conn = sqlite3.connect(str(SQLITE_DB_PATH), check_same_thread=False)
        _sql_conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(_sql_conn)
    return _sql_conn


def _init_schema(conn: sqlite3.Connection):
    """Create session tables if not exist."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_message_at REAL NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                name TEXT,
                tool_call_id TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, id)
        """)


# ==================== Message Serialization ====================

def message_to_dict(msg: Union[HumanMessage, AIMessage, ToolMessage, dict]) -> dict:
    """Convert a message to a JSON-serializable dict."""
    if isinstance(msg, dict):
        return msg
    elif isinstance(msg, HumanMessage):
        return {"role": "user", "content": msg.content}
    elif isinstance(msg, AIMessage):
        content = msg.content
        if isinstance(content, str):
            return {"role": "assistant", "content": content}
        else:
            try:
                json.dumps(content)
                return {"role": "assistant", "content": content}
            except (TypeError, ValueError):
                return {"role": "assistant", "content": str(content)}
    elif isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "name": msg.name,
            "content": msg.content,
            "tool_call_id": msg.tool_call_id,
        }
    else:
        return {"role": "unknown", "content": str(msg)}


# ==================== TTL Check ====================

def _is_expired(session_id: str) -> bool:
    """Check if a session has expired based on last_message_at."""
    conn = _get_conn()
    cursor = conn.execute(
        "SELECT last_message_at FROM sessions WHERE id = ?",
        (session_id,)
    )
    row = cursor.fetchone()
    if not row:
        return False
    age_days = (time.time() - row[0]) / 86400
    return age_days > SESSION_TTL_DAYS


def _delete_expired_sessions():
    """Delete all sessions older than SESSION_TTL_DAYS."""
    conn = _get_conn()
    cutoff = time.time() - SESSION_TTL_DAYS * 86400
    with conn:
        conn.execute("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE last_message_at < ?)", (cutoff,))
        conn.execute("DELETE FROM sessions WHERE last_message_at < ?", (cutoff,))


# ==================== Session CRUD ====================

def create_session() -> str:
    """Create a new session, return session_id."""
    import uuid
    session_id = str(uuid.uuid4())
    now = time.time()

    conn = _get_conn()
    with conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, last_message_at, message_count) VALUES (?, ?, ?, ?, ?)",
            (session_id, "", now, now, 0)
        )
    return session_id


def get_session_meta(session_id: str) -> Optional[dict]:
    """Get session metadata. Returns None if expired or not found."""
    if _is_expired(session_id):
        delete_session(session_id)
        return None

    conn = _get_conn()
    cursor = conn.execute(
        "SELECT id, title, created_at, last_message_at, message_count FROM sessions WHERE id = ?",
        (session_id,)
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "created_at": datetime.fromtimestamp(row[2], tz=timezone.utc).isoformat(),
        "last_message_at": datetime.fromtimestamp(row[3], tz=timezone.utc).isoformat(),
        "message_count": row[4],
    }


def _parse_content(content: str, role: str) -> str | list[dict]:
    """Parse stored content. For assistant, deserialize from JSON."""
    if role == "assistant":
        try:
            return json.loads(content)
        except Exception:
            return content
    return content


def get_session_history(session_id: str) -> list[dict]:
    """Get full message history for a session."""
    if _is_expired(session_id):
        delete_session(session_id)
        return []

    conn = _get_conn()
    cursor = conn.execute(
        "SELECT role, content, name, tool_call_id FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,)
    )
    result = []
    for row in cursor.fetchall():
        role = row[0]
        raw_content = row[1]
        content = _parse_content(raw_content, role)
        msg: dict = {"role": role, "content": content}
        if row[2]:
            msg["name"] = row[2]
        if row[3]:
            msg["tool_call_id"] = row[3]
        result.append(msg)
    return result


def append_to_session(session_id: str, messages: list[dict]) -> int:
    """Append messages to session history, return new message_count."""
    conn = _get_conn()
    now = time.time()
    with conn:
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content, default=str)
            elif not isinstance(content, str):
                content = str(content)
            conn.execute(
                "INSERT INTO messages (session_id, role, content, name, tool_call_id) VALUES (?, ?, ?, ?, ?)",
                (session_id, msg.get("role", ""), content, msg.get("name"), msg.get("tool_call_id"))
            )
        conn.execute(
            "UPDATE sessions SET last_message_at = ?, message_count = message_count + ? WHERE id = ?",
            (now, len(messages), session_id)
        )
    return get_session_meta(session_id)["message_count"]


def update_session_title(session_id: str, title: str) -> None:
    """Update session title (first user message summary)."""
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title[:100], session_id))


def delete_session(session_id: str) -> bool:
    """Delete a session from SQLite. Returns True if deleted."""
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cursor.rowcount > 0


def list_sessions() -> list[dict]:
    """List all sessions, sorted by last_message_at desc. Expired sessions are deleted."""
    _delete_expired_sessions()

    conn = _get_conn()
    cursor = conn.execute(
        "SELECT id, title, created_at, last_message_at, message_count FROM sessions ORDER BY last_message_at DESC"
    )
    sessions = []
    for row in cursor.fetchall():
        sessions.append({
            "id": row[0],
            "title": row[1],
            "created_at": datetime.fromtimestamp(row[2], tz=timezone.utc).isoformat(),
            "last_message_at": datetime.fromtimestamp(row[3], tz=timezone.utc).isoformat(),
            "message_count": row[4],
        })
    return sessions


def estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(str(messages)) // 4


# ==================== Layer 1: micro_compact ====================

KEEP_RECENT = 30
PRESERVE_RESULT_TOOLS = {"read_file"}


def _build_tool_name_map(messages: list) -> dict:
    """Build tool_use_id -> tool_name mapping from assistant messages."""
    tool_name_map = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name_map[block.get("id", "")] = block.get("name", "unknown")
            elif hasattr(block, "type") and block.type == "tool_use":
                tool_name_map[getattr(block, "id", "")] = getattr(block, "name", "unknown")
    return tool_name_map


def _compact_tool_results(messages: list) -> list:
    """Replace old tool_result content with short placeholders, keep last KEEP_RECENT.

    Handles both SSE format (role='tool' messages) and REPL format (tool_result blocks).
    """
    # Collect all tool messages — SSE uses role='tool', REPL uses content blocks
    tool_messages = []
    for msg_idx, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "tool":
            # SSE format: separate tool message
            tool_messages.append((msg_idx, msg))
        elif role in ("user", "assistant") and isinstance(content, list):
            # REPL format: tool_result blocks inside content
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_messages.append((msg_idx, part))

    if len(tool_messages) <= KEEP_RECENT:
        return messages

    tool_name_map = _build_tool_name_map(messages)
    to_clear = tool_messages[:-KEEP_RECENT]

    for _, result in to_clear:
        if isinstance(result, dict):
            content = result.get("content", "")
            if not isinstance(content, str) or len(content) <= 100:
                continue
            tool_id = result.get("tool_call_id", "") or result.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "unknown")
            if tool_name in PRESERVE_RESULT_TOOLS:
                continue
            result["content"] = f"[Previous: used {tool_name}]"
    return messages


# ==================== Layer 2: session_auto_compact ====================

def session_auto_compact(session_id: str) -> str:
    """Compress session history: save transcript, summarize, replace with summary msg.

    Returns the summary text.
    """
    history = get_session_history(session_id)
    if not history:
        return ""

    # Save full transcript
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in history:
            f.write(json.dumps(msg, default=str) + "\n")

    # LLM summarize
    from src.agent.llm import client
    conv_text = json.dumps(history, default=str)[-80000:]
    resp = client.messages.create(
        model=MODEL_ID,
        messages=[{
            "role": "user",
            "content": (
                "Summarize this conversation for continuity. Include: "
                "1) What was accomplished, 2) Current state, 3) Key decisions made. "
                "Be concise but preserve critical details.\n\n" + conv_text
            )
        }],
        max_tokens=2000,
    )
    # Extract text block
    summary = ""
    if isinstance(resp.content, list):
        for block in resp.content:
            if hasattr(block, "type") and block.type == "text":
                summary = block.text
                break
            elif isinstance(block, dict) and block.get("type") == "text":
                summary = block.get("text", "")
                break
        if not summary and resp.content:
            last_block = resp.content[-1]
            summary = getattr(last_block, "text", str(last_block))
    else:
        summary = str(resp.content)

    # Save to ChromaDB
    from src.agent.memory import memory_system
    memory_system.save_summary(summary, filepath=str(path))

    # Replace session history with single summary msg
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO messages (session_id, role, content, name, tool_call_id) VALUES (?, ?, ?, ?, ?)",
            (session_id, "user", f"[Conversation compressed. Transcript: {path}]\n\n{summary}", None, None)
        )
        conn.execute(
            "UPDATE sessions SET message_count = 1 WHERE id = ?",
            (session_id,)
        )

    return summary


# ==================== Layer 3: session_auto_compact (called by compact tool) ====================

def compact_session(session_id: str) -> str:
    """Manual compression — called by the compact tool. Returns summary."""
    return session_auto_compact(session_id)


# ==================== JSON Archiving ====================

def archive_session(session_id: str) -> Optional[Path]:
    """Write session to JSON archive file. Returns path if successful."""
    meta = get_session_meta(session_id)
    if not meta:
        return None

    history = get_session_history(session_id)

    title = meta.get("title", "")
    if not title:
        for msg in history:
            if msg.get("role") == "user":
                title = msg.get("content", "")[:100]
                break

    ended_at = datetime.now(timezone.utc).isoformat()
    archive_data = {
        "id": session_id,
        "title": title,
        "created_at": meta.get("created_at", ""),
        "ended_at": ended_at,
        "messages": history,
    }

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{date_str}-{session_id}.json"
    path = SESSIONS_DIR / filename

    with open(path, "w", encoding="utf-8") as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)

    return path
