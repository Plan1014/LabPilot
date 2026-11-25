"""
分层长期记忆系统 (基于 ChromaDB + SQLite)
- ChromaDB: 负责事实记忆、对话摘要 (向量检索 + 原文提取)
- SQLite: 负责结构化的任务状态 (精确更新与查询)
"""

import time
import os
import sqlite3
from datetime import datetime
from pathlib import Path

# Force HF offline BEFORE importing sentence_transformers / huggingface_hub.
# Without these, the cached BAAI/bge-small-zh-v1.5 model still triggers a HEAD
# request to huggingface.co to check for revisions on every import.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings
from sentence_transformers import SentenceTransformer

from src.agent.config import WORKDIR
from src.agent.prompt_context import (
    FACT_CANDIDATES,
    RETRIEVED_MEMORY_LIMIT,
    SUMMARY_CANDIDATES,
    SUMMARY_MEMORY_LIMIT,
    FactEntry,
    RetrievedMemory,
    SummaryEntry,
    filter_by_distance,
)

MEMORY_DIR = WORKDIR / "data" / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

CHROMA_DB_PATH = str(MEMORY_DIR / "chroma_db")
SQLITE_DB_PATH = MEMORY_DIR / "tasks.db"

# ==================== 1. 翻译层 ====================
class BgeEmbeddingFunction(EmbeddingFunction):
    """使用本地 bge-small-zh-v1.5 模型进行文本向量化"""
    def __init__(self):
        self.model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        
    def __call__(self, input: Documents) -> Embeddings:
        embeddings = self.model.encode(input, normalize_embeddings=True)
        return embeddings.tolist()


# ==================== 2. 分层管理器 (Manager) ====================
class MemoryManager:
    """写入接口：将不同类型的数据存入对应的数据库"""
    def __init__(self):
        self.ef = BgeEmbeddingFunction()
        
        # --- 初始化 ChromaDB (事实与摘要) ---
        self.chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        self.facts_col = self.chroma_client.get_or_create_collection(
            name="facts", embedding_function=self.ef, metadata={"hnsw:space": "cosine"}
        )
        self.summaries_col = self.chroma_client.get_or_create_collection(
            name="summaries", embedding_function=self.ef, metadata={"hnsw:space": "cosine"}
        )

        # --- 初始化 SQLite (任务状态) ---
        self.sql_conn = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
        with self.sql_conn:
            self.sql_conn.execute("""
                CREATE TABLE IF NOT EXISTS task_states (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    result TEXT,
                    updated_at REAL NOT NULL
                )
            """)

    def save_fact(self, fact_text: str, source: str = "agent"):
        """保存事实记忆到 ChromaDB"""
        doc_id = f"fact_{int(time.time() * 1000)}"
        self.facts_col.add(
            documents=[fact_text],
            metadatas=[{"timestamp": time.time(), "source": source}],
            ids=[doc_id]
        )

    def save_summary(self, summary_text: str, filepath: str = ""):
        """保存对话摘要到 ChromaDB"""
        doc_id = f"sum_{int(time.time() * 1000)}"
        self.summaries_col.add(
            documents=[summary_text],
            metadatas=[{"timestamp": time.time(), "filepath": filepath}],
            ids=[doc_id]
        )

    def update_task_state(self, task_id: str, status: str, result: str = ""):
        """更新任务状态到 SQLite"""
        with self.sql_conn:
            self.sql_conn.execute("""
                INSERT INTO task_states (task_id, status, result, updated_at) 
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET 
                    status=excluded.status, 
                    result=excluded.result,
                    updated_at=excluded.updated_at
            """, (task_id, status, result, time.time()))


# ==================== 3. 检索与理解层 (Retriever) ====================
class MemoryRetriever:
    """读取接口：执行检索并为 Agent 组装上下文"""
    def __init__(self, manager: MemoryManager):
        self.facts_col = manager.facts_col
        self.summaries_col = manager.summaries_col
        self.sql_conn = manager.sql_conn

    # === 已移除：后台任务/事件记录的注入（2026-11-23）===
    # 原因：task_states.status 存的是通知事件类型（log_errors / lock_anomaly /
    # task_completed），不是任务生命周期状态，没有任何代码写 "running"，
    # 所以这段一直在把陈旧的日志事件当"正在运行的后台任务"喂给模型。
    # 后续：日志系统重做后，连接事件的日志应当【仅在连接期间】注入，
    #       并带上事件时间戳与当前时间戳，让模型自己判断新鲜度。
    #       见 docs/superpowers/specs/2026-11-23-prompt-assembly-fixes-design.md §10

    def _query(self, col, user_query, n_results, threshold, entry_cls):
        res = col.query(
            query_texts=[user_query],
            n_results=n_results,
            include=["documents", "distances", "metadatas"],
        )
        docs = (res.get("documents") or [[]])[0] or []
        dists = (res.get("distances") or [[]])[0] or []
        metas = (res.get("metadatas") or [[]])[0] or []
        entries = []
        for i, doc in enumerate(docs):
            distance = dists[i] if i < len(dists) else 0.0
            meta = metas[i] if i < len(metas) else {}
            entries.append(entry_cls(text=doc, distance=distance,
                                    timestamp=(meta or {}).get("timestamp")))
        return filter_by_distance(entries, threshold)

    def retrieve(self, user_query: str, threshold: float | None = None) -> RetrievedMemory:
        """检索事实与摘要。

        threshold=None 表示不过门槛（模型主动 search_memory 走这条）；
        传数字则只保留 distance <= threshold 的条目。
        """
        facts = self._query(self.facts_col, user_query, FACT_CANDIDATES,
                            threshold, FactEntry)[:RETRIEVED_MEMORY_LIMIT]
        summaries = self._query(self.summaries_col, user_query, SUMMARY_CANDIDATES,
                                threshold, SummaryEntry)[:SUMMARY_MEMORY_LIMIT]
        return RetrievedMemory(facts=facts, summaries=summaries)

    def retrieve_context(self, user_query: str) -> str:
        """纯文本上下文（给 search_memory 工具和 /memory/search 面板用）。

        注意：这里不过门槛 —— 显式检索是探询行为；自动注入走
        retrieve(threshold=relevance_threshold())。
        """
        retrieved = self.retrieve(user_query, threshold=None)
        parts = []
        if retrieved.summaries:
            parts.append("【相关历史对话摘要】\n" + retrieved.summaries[0].text)
        if retrieved.facts:
            parts.append("【相关事实记忆 (参数/结论)】\n"
                         + "\n".join(f"- {f.text}" for f in retrieved.facts))
        if not parts:
            return "当前无长期记忆记录。"

        return "\n\n".join(parts)

# 实例化全局单例
memory_system = MemoryManager()
memory_retriever = MemoryRetriever(memory_system)


# ==================== 4. Session 搜索 (Agent 调用) ====================

def search_sessions(query: str, days: int = 7) -> str:
    """按关键词和时间范围搜索历史 session。

    用于 Agent 主动加载历史对话上下文。
    """
    import sqlite3
    cutoff = time.time() - days * 86400
    conn = sqlite3.connect(str(SQLITE_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT id, title, created_at, last_message_at, message_count FROM sessions WHERE last_message_at > ? ORDER BY last_message_at DESC",
        (cutoff,)
    )
    rows = cursor.fetchall()
    conn.close()

    # 按关键词过滤（简单 LIKE）
    matched = []
    for row in rows:
        if query.lower() in row["title"].lower():
            matched.append(row)
        else:
            # 也搜一下对应 messages（取最近 1 条 user message）
            conn2 = sqlite3.connect(str(SQLITE_DB_PATH), check_same_thread=False)
            msg_cur = conn2.execute(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
                (row["id"],)
            )
            msg_row = msg_cur.fetchone()
            conn2.close()
            if msg_row and query.lower() in msg_row["content"].lower():
                matched.append(row)

    if not matched:
        return f"最近 {days} 天内没有找到相关 session。"

    lines = ["【匹配的 session 列表】"]
    for row in matched:
        ts = datetime.fromtimestamp(row["last_message_at"]).strftime("%Y-%m-%d %H:%M")
        title = row["title"] or "(无标题)"
        lines.append(f"- [{row['id'][:8]}] {title} ({ts})")
    return "\n".join(lines)


# ==================== 5. 全量查询 (前端面板) ====================

def list_all_facts(limit: int = 100, offset: int = 0) -> list[dict]:
    """返回所有事实记忆，分页。"""
    all_data = memory_system.facts_col.get()
    ids = all_data.get("ids", [])
    docs = all_data.get("documents", [])
    metas = all_data.get("metadatas", [])

    total = len(ids)
    page = []
    for i in range(offset, min(offset + limit, total)):
        page.append({
            "doc_id": ids[i],
            "content": docs[i],
            "timestamp": metas[i].get("timestamp"),
            "source": metas[i].get("source"),
        })
    return {"total": total, "items": page}


def list_all_summaries(limit: int = 100, offset: int = 0) -> list[dict]:
    """返回所有对话摘要，分页。"""
    all_data = memory_system.summaries_col.get()
    ids = all_data.get("ids", [])
    docs = all_data.get("documents", [])
    metas = all_data.get("metadatas", [])

    total = len(ids)
    page = []
    for i in range(offset, min(offset + limit, total)):
        page.append({
            "doc_id": ids[i],
            "content": docs[i],
            "timestamp": metas[i].get("timestamp"),
            "filepath": metas[i].get("filepath"),
        })
    return {"total": total, "items": page}


def delete_fact(doc_id: str) -> bool:
    """删除指定的事实记忆。"""
    try:
        memory_system.facts_col.delete(ids=[doc_id])
        return True
    except Exception:
        return False


def delete_summary(doc_id: str) -> bool:
    """删除指定的摘要记忆。"""
    try:
        memory_system.summaries_col.delete(ids=[doc_id])
        return True
    except Exception:
        return False