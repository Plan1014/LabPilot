"""待批批次 —— 提权挂起时的状态。

**不复用 `ask_user` 的回路**（spec D4）：`ask_user` 的回答回到**模型**，
闸门的回答必须回到**闸门** —— 决定放不放行。混用会让模型以为「用户批准了
这个 bash」而 bash 从没执行。

批次原子性：一轮里的 tool_call 作为一个批次裁决。只要有一个调用 escalate，
**整批挂起**，本轮不执行任何工具，全部调用进待批队列。恢复时按原始顺序
为每个调用补一条 tool_result —— 一个 tool_use 缺了配对的 tool_result，
上游 API 会直接报 400。

`status` 列同时承担「裁决结果」的存储：`pending` → `allow_once` /
`allow_always` / `deny`。
"""

import json
import time
import uuid

VALID_CHOICES = ("allow_once", "allow_always", "deny")


def _conn():
    from src.agent import session_manager

    return session_manager._get_conn()


def save_batch(session_id: str, calls: list[dict], pending: list[dict]) -> str:
    """写 auth_batches + auth_pending，返回 batch_id。

    `calls` 是**整批**裁决结果（按原始顺序）：
    `{"tool_call_id", "tool_name", "tool_input", "verdict", "reason", "pattern"}`。

    `pending` 是其中需要用户裁决的那几条（`verdict == "escalate"`），
    额外带 `question`。
    """
    batch_id = uuid.uuid4().hex
    now = time.time()
    conn = _conn()
    with conn:
        conn.execute(
            """
            INSERT INTO auth_batches (batch_id, session_id, calls, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (batch_id, session_id, json.dumps(calls, ensure_ascii=False), now),
        )
        for p in pending:
            conn.execute(
                """
                INSERT INTO auth_pending
                    (id, batch_id, session_id, tool_call_id, tool_name, tool_input,
                     question, reason, pattern, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    uuid.uuid4().hex,
                    batch_id,
                    session_id,
                    p.get("tool_call_id", ""),
                    p.get("tool_name", ""),
                    json.dumps(p.get("tool_input") or {}, ensure_ascii=False),
                    p.get("question", ""),
                    p.get("reason", ""),
                    p.get("pattern"),
                    now,
                ),
            )
    return batch_id


def list_pending(session_id: str) -> list[dict]:
    """该会话所有 status='pending' 的行（按创建顺序）。"""
    conn = _conn()
    rows = conn.execute(
        """
        SELECT id, batch_id, session_id, tool_call_id, tool_name, tool_input,
               question, reason, pattern, status, created_at
        FROM auth_pending
        WHERE session_id = ? AND status = 'pending'
        ORDER BY created_at, id
        """,
        (session_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def record_decision(
    tool_call_id: str, choice: str, session_id: str | None = None
) -> dict | None:
    """记一次裁决。找不到待批记录 → None。

    `session_id` 给了就只认这个会话下的记录 —— 提权是「谁点的谁批」，
    不校验归属的话，任意会话都能替别人放行（评审 I3）。
    """
    if choice not in VALID_CHOICES:
        raise ValueError(f"invalid choice: {choice!r}")
    sql = "UPDATE auth_pending SET status = ? WHERE tool_call_id = ? AND status = 'pending'"
    params: list = [choice, tool_call_id]
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    conn = _conn()
    with conn:
        cur = conn.execute(sql, params)
        if cur.rowcount == 0:
            return None
    return get_pending_by_tool_call_id(tool_call_id)


def get_pending_by_tool_call_id(tool_call_id: str) -> dict | None:
    conn = _conn()
    row = conn.execute(
        """
        SELECT id, batch_id, session_id, tool_call_id, tool_name, tool_input,
               question, reason, pattern, status, created_at
        FROM auth_pending WHERE tool_call_id = ?
        """,
        (tool_call_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def batch_is_resolved(batch_id: str) -> bool:
    """批次里没有待批项 → True。"""
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM auth_pending WHERE batch_id = ? AND status = 'pending'",
        (batch_id,),
    ).fetchone()
    return bool(row and row[0] == 0)


def get_batch(batch_id: str) -> dict | None:
    """取整批的裁决结果。找不到 → None。"""
    conn = _conn()
    row = conn.execute(
        "SELECT batch_id, session_id, calls, created_at FROM auth_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if not row:
        return None
    try:
        calls = json.loads(row[2])
    except (ValueError, TypeError):
        calls = []
    return {
        "batch_id": row[0],
        "session_id": row[1],
        "calls": calls,
        "created_at": row[3],
    }


def _row_to_dict(row) -> dict:
    try:
        tool_input = json.loads(row[5])
    except (ValueError, TypeError):
        tool_input = {}
    return {
        "id": row[0],
        "batch_id": row[1],
        "session_id": row[2],
        "tool_call_id": row[3],
        "tool_name": row[4],
        "tool_input": tool_input,
        "question": row[6],
        "reason": row[7],
        "pattern": row[8],
        "status": row[9],
        "created_at": row[10],
    }
