"""Core Memory 实现 - Block 体系热点记忆

参照 Letta: letta/schemas/block.py, letta/services/block_manager.py
"""

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from src.agent.memory.constants import (
    CORE_MEMORY_BLOCK_CHAR_LIMIT,
    LABEL_WHITELIST,
)


@dataclass
class Block:
    """Core Memory Block 数据结构"""
    id: str
    label: str
    value: str
    description: str = ""
    limit: int = CORE_MEMORY_BLOCK_CHAR_LIMIT
    read_only: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class BlockHistory:
    """Block 版本历史"""
    id: str
    block_id: str
    old_value: str
    new_value: str
    created_at: float


class CoreMemoryManager:
    """Core Memory 管理器 - SQLite 存储 + XML 编译"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from src.agent.config import WORKDIR
            data_dir = WORKDIR / "data" / "memory"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / "blocks.db")

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        """初始化 blocks 和 block_history 表"""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS blocks (
                    id TEXT PRIMARY KEY,
                    label TEXT UNIQUE NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    "limit" INTEGER NOT NULL DEFAULT 4000,
                    read_only INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS block_history (
                    id TEXT PRIMARY KEY,
                    block_id TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    created_at REAL NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_blocks_label ON blocks(label)
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_history_block_id ON block_history(block_id)
            """)

    def _validate_label(self, label: str):
        """验证标签是否在白名单中"""
        if label not in LABEL_WHITELIST:
            raise ValueError(f"Label '{label}' not in whitelist. Allowed: {sorted(LABEL_WHITELIST)}")

    def _block_from_row(self, row: sqlite3.Row) -> Block:
        """从数据库行转为 Block 对象"""
        return Block(
            id=row["id"],
            label=row["label"],
            value=row["value"],
            description=row["description"],
            limit=row["limit"],
            read_only=bool(row["read_only"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_block(self, label: str) -> Optional[Block]:
        """按标签获取 Block"""
        cursor = self.conn.execute(
            "SELECT * FROM blocks WHERE label = ?", (label,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._block_from_row(row)

    def get_all_blocks(self) -> List[Block]:
        """获取所有 Block"""
        cursor = self.conn.execute("SELECT * FROM blocks ORDER BY label")
        return [self._block_from_row(row) for row in cursor.fetchall()]

    def save_block(self, label: str, value: str, description: str = "", read_only: bool = False) -> Block:
        """创建或全量替换 Block（自动记录历史）"""
        self._validate_label(label)
        now = time.time()

        existing = self.get_block(label)
        old_value = existing.value if existing else ""

        truncated_value = value[:CORE_MEMORY_BLOCK_CHAR_LIMIT]

        block_id = existing.id if existing else str(uuid.uuid4())

        with self.conn:
            self.conn.execute("""
                INSERT INTO blocks (id, label, value, description, "limit", read_only, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(label) DO UPDATE SET
                    value = excluded.value,
                    description = excluded.description,
                    read_only = excluded.read_only,
                    updated_at = excluded.updated_at
            """, (block_id, label, truncated_value, description, CORE_MEMORY_BLOCK_CHAR_LIMIT, int(read_only), now, now))

            if old_value != truncated_value:
                self.conn.execute("""
                    INSERT INTO block_history (id, block_id, old_value, new_value, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (str(uuid.uuid4()), block_id, old_value, truncated_value, now))

        return self.get_block(label)

    def append_block(self, label: str, content: str) -> Block:
        """追加内容到 Block（如果 Block 不存在则创建）"""
        self._validate_label(label)
        existing = self.get_block(label)

        if existing is None:
            return self.save_block(label, content)

        new_value = existing.value + "\n" + content
        new_value = new_value[:CORE_MEMORY_BLOCK_CHAR_LIMIT]
        return self.save_block(label, new_value, description=existing.description)

    def replace_block(self, label: str, old_content: str, new_content: str) -> Block:
        """精确替换 Block 中的内容"""
        self._validate_label(label)
        existing = self.get_block(label)

        if existing is None:
            raise ValueError(f"Block '{label}' does not exist")

        if old_content not in existing.value:
            raise ValueError(f"old_content not found in block '{label}'")

        new_value = existing.value.replace(old_content, new_content, 1)
        return self.save_block(label, new_value, description=existing.description)

    def get_block_history(self, label: str) -> List[BlockHistory]:
        """获取 Block 的历史记录"""
        block = self.get_block(label)
        if block is None:
            return []

        cursor = self.conn.execute(
            "SELECT * FROM block_history WHERE block_id = ? ORDER BY created_at DESC",
            (block.id,)
        )
        return [
            BlockHistory(
                id=row["id"],
                block_id=row["block_id"],
                old_value=row["old_value"] or "",
                new_value=row["new_value"] or "",
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        ]

    def compile(self) -> str:
        """渲染为标准 XML 格式（参照 Letta: letta/schemas/memory.py:143）"""
        blocks = self.get_all_blocks()
        renderable = [b for b in blocks if not b.read_only]

        if not renderable:
            return "<memory_blocks>\n</memory_blocks>"

        lines = ["<memory_blocks>"]
        for block in renderable:
            label = block.label.replace("/", "_")
            lines.append(f"<{label}>")
            if block.description:
                lines.append(f"<description>\n{block.description}\n</description>")
            lines.append(f"<value>\n{block.value}\n</value>")
            lines.append(f"</{label}>")
        lines.append("</memory_blocks>")

        return "\n".join(lines)

    def close(self):
        """关闭数据库连接"""
        self.conn.close()