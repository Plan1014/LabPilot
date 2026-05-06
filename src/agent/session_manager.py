"""Session management with Redis for short-term storage and JSON archiving."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import redis

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from src.agent.config import REDIS_URL, SESSION_TTL_DAYS, SESSIONS_DIR


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


# ==================== Redis Connection ====================

def get_redis_client() -> redis.Redis:
    """Create Redis client from REDIS_URL env var."""
    return redis.from_url(REDIS_URL, decode_responses=True)


# ==================== Session Data Structures ====================

def _history_key(session_id: str) -> str:
    return f"session:{session_id}:history"


def _meta_key(session_id: str) -> str:
    return f"session:{session_id}:meta"


def _session_list_key() -> str:
    return "session:list"


# ==================== Session CRUD ====================

def create_session() -> str:
    """Create a new session, return session_id."""
    import uuid
    session_id = str(uuid.uuid4())

    r = get_redis_client()
    meta = {
        "id": session_id,
        "title": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_message_at": datetime.now(timezone.utc).isoformat(),
        "message_count": 0,
    }
    pipe = r.pipeline()
    pipe.rpush(_history_key(session_id), json.dumps([]))
    pipe.hset(_meta_key(session_id), mapping=meta)
    pipe.sadd(_session_list_key(), session_id)
    pipe.expire(_history_key(session_id), SESSION_TTL_DAYS * 86400)
    pipe.expire(_meta_key(session_id), SESSION_TTL_DAYS * 86400)
    pipe.execute()
    return session_id


def get_session_meta(session_id: str) -> Optional[dict]:
    """Get session metadata."""
    r = get_redis_client()
    meta = r.hgetall(_meta_key(session_id))
    if not meta:
        return None
    meta["message_count"] = int(meta.get("message_count", 0))
    return meta


def get_session_history(session_id: str) -> list[dict]:
    """Get full message history for a session."""
    r = get_redis_client()
    raw = r.lrange(_history_key(session_id), 0, -1)
    result = []
    for item in raw:
        parsed = json.loads(item)
        # Skip empty list marker from initialization
        if isinstance(parsed, list) and len(parsed) == 0:
            continue
        result.append(parsed)
    return result


def append_to_session(session_id: str, messages: list[dict]) -> int:
    """Append messages to session history, return new message_count."""
    r = get_redis_client()
    pipe = r.pipeline()
    for msg in messages:
        pipe.rpush(_history_key(session_id), json.dumps(msg))
    pipe.hset(_meta_key(session_id), "last_message_at", datetime.now(timezone.utc).isoformat())
    pipe.hincrby(_meta_key(session_id), "message_count", len(messages))
    pipe.expire(_history_key(session_id), SESSION_TTL_DAYS * 86400)
    pipe.expire(_meta_key(session_id), SESSION_TTL_DAYS * 86400)
    results = pipe.execute()
    # message_count is the total, from hincrby result
    new_count = r.hget(_meta_key(session_id), "message_count")
    return int(new_count) if new_count else 0


def update_session_title(session_id: str, title: str) -> None:
    """Update session title (first user message summary)."""
    r = get_redis_client()
    r.hset(_meta_key(session_id), "title", title[:100])


def delete_session(session_id: str) -> bool:
    """Delete a session from Redis. Returns True if deleted."""
    r = get_redis_client()
    pipe = r.pipeline()
    pipe.delete(_history_key(session_id))
    pipe.delete(_meta_key(session_id))
    pipe.srem(_session_list_key(), session_id)
    results = pipe.execute()
    # True if any key was deleted
    return any(r > 0 for r in results[:2])


def list_sessions() -> list[dict]:
    """List all sessions, sorted by last_message_at desc."""
    r = get_redis_client()
    session_ids = r.smembers(_session_list_key())
    sessions = []
    for sid in session_ids:
        meta = r.hgetall(_meta_key(sid))
        if meta:
            meta["message_count"] = int(meta.get("message_count", 0))
            sessions.append(meta)
    sessions.sort(key=lambda s: s.get("last_message_at", ""), reverse=True)
    return sessions


# ==================== JSON Archiving ====================

def archive_session(session_id: str) -> Optional[Path]:
    """Write session to JSON archive file. Returns path if successful."""
    meta = get_session_meta(session_id)
    if not meta:
        return None

    history = get_session_history(session_id)

    # Generate title from first user message if not set
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
