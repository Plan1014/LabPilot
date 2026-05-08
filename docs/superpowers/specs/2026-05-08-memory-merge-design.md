# Memory 分支合并设计文档

**日期**: 2026-05-08
**状态**: 已确认，待执行
**分支**: memory → feature/frontend-sse-streaming → main

---

## 背景

两个分支分别解决不同问题：

| 分支 | 解决的问题 |
|------|-----------|
| `memory` | 跨对话长期记忆（RAG，ChromaDB + SQLite） |
| `feature/frontend-sse-streaming` | 前端 SSE 流式对话 + Redis 会话管理 |

两者存在部分文件冲突，且 `memory` 分支的 RAG 全局注入设计不适合 SSE session 隔离场景，需要合并前修正。

---

## 核心架构决策

### 1. Session 存储：Redis → SQLite

**原因**: 减少部署依赖（无需 Redis 服务），SQLite 足够支撑个人使用的高并发场景。

**决策**: 复用 `memory.py` 现有的 SQLite 连接（`MEMORY_DIR / "tasks.db"`），新建 `sessions` 和 `messages` 表，与 `task_states` 表共存于同一 DB 文件。

### 2. RAG 注入：全局注入 → 工具召回

**原因**: SSE 会话应保持隔离，全局自动注入会导致跨 session 的上下文污染。

**决策**:
- 移除 `graph_thinking.py` 中的自动 RAG 注入（`memory_retriever.retrieve_context` 调用）
- 保留 `remember_fact` / `search_memory` 工具，Agent 主动调用
- 新增 `search_sessions` 工具，按时间/关键词搜索历史 session

### 3. 前端记忆面板

**原因**: 用户需要可视化查看/管理已记住的知识。

**决策**:
- 前端新增记忆面板（只读展示 + 删除操作）
- 数据源：后端新增 `list_all_facts` / `list_all_summaries` API
- 记忆加载逻辑由后端实现，前端仅做展示

---

## 改动清单

### Phase 1: memory 分支（后端，独立完成）

#### 1.1 `src/agent/memory.py`（已有，新增内容）

**新增表**: `sessions`, `messages`

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL NOT NULL,
    last_message_at REAL NOT NULL,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    name TEXT,
    tool_call_id TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

**新增方法**:
- `create_session() -> str` — 创建 session，返回 session_id
- `get_session_history(session_id) -> list[dict]` — 读取某 session 的全量消息
- `append_to_session(session_id, messages)` — 追加消息
- `list_sessions() -> list[dict]` — 列出所有 session（按 last_message_at 倒序）
- `delete_session(session_id) -> bool`
- `search_sessions(query, days)` — 按关键词/时间搜索 session
- `list_all_facts(limit, offset)` — 列出所有事实（分页）
- `list_all_summaries(limit, offset)` — 列出所有摘要（分页）
- `delete_fact(doc_id)` — 删除某条事实
- `delete_summary(doc_id)` — 删除某条摘要

#### 1.2 `src/agent/session_manager.py`（已有，替换存储层）

- 删除 Redis 依赖和所有 `redis.*` 调用
- 改用 `memory.py` 的 SQLite 连接
- 保留原有公开接口：`create_session`, `get_session_history`, `append_to_session`, `list_sessions`, `delete_session`
- 移除 `archive_session`（由 memory 系统处理）
- 移除 `get_session_meta`（合并到 `list_sessions` 返回值）

#### 1.3 `src/agent/config.py`（已有，新增配置）

```python
# 删除
# REDIS_URL, SESSION_TTL_DAYS

# 新增
SESSIONS_DIR = WORKDIR / "data" / "sessions"
SESSION_TTL_DAYS = 30  # 惰性删除阈值
```

#### 1.4 `src/agent/graph_thinking.py`（已有，修改）

**移除**:
- `from src.agent.memory import memory_retriever` import
- `generate()` 函数中的 RAG 注入逻辑（~15行）

**保留**:
- `remember_fact` / `search_memory` 工具调用链路

#### 1.5 `src/agent/tools.py`（memory 分支新增，保留）

新增工具:
- `remember_fact(fact)` — 写入长期记忆
- `search_memory(query)` — 向量检索召回
- `search_sessions(query, days)` — 搜索历史 session

#### 1.6 `src/agent/websocket_server.py`（已有，修改）

**移除**（memory 分支的改动）:
- `from src.agent.memory import memory_system` import
- `/notify` 中的 `memory_system.update_task_state()` 调用

**合并时保留**（feature/frontend-sse-streaming 版本）:
- 完整的 SSE `/query` 和 `/session/query` 路由
- Redis session 相关的所有逻辑（会被新的 session_manager.py 替代）

#### 1.7 `src/agent/repl.py`（已有，修改）

**移除**:
- `memory_system.save_summary(summary, filepath=...)` 调用（在 `auto_compact` 中）

**保留**:
- `remember_fact` / `search_memory` 工具

#### 1.8 `requirements.txt`（已有，修改）

```diff
- redis
+ chromadb
+ sentence-transformers
```

#### 1.9 `src/agent/memory.py`（已有，修改）

**修改** `MemoryManager`:
- 导出 `get_all_facts()`, `get_all_summaries()`, `delete_fact(doc_id)`, `delete_summary(doc_id)`
- ChromaDB `get()` 方法支持按 limit/offset 分页

**新增 TTL 清理**:
- `delete_expired_sessions()` — 惰性删除 + 后台定时清理线程

---

### Phase 2: feature/frontend-sse-streaming 分支（前端）

#### 2.1 前端记忆面板（Tauri）

**页面**: 新增 `/memory` 路由或侧边栏面板

**功能**:
- `GET /memory/facts` — 展示所有已记住的事实列表
- `GET /memory/summaries` — 展示所有对话摘要列表
- `DELETE /memory/facts/{doc_id}` — 删除某条事实
- `DELETE /memory/summaries/{doc_id}` — 删除某条摘要
- 搜索框：调用 `search_memory(query)` 实时检索

**UI**:
- 两个 Tab：事实 / 摘要
- 每条记录显示：内容、时间戳、来源
- 支持删除操作（确认弹窗）

---

## 文件冲突矩阵

| 文件 | memory 分支 | frontend-sse-streaming | 合并策略 |
|------|------------|----------------------|---------|
| `src/agent/memory.py` | 新增 | 不存在 | 直接使用 |
| `src/agent/session_manager.py` | 未改动 | 已修改（Redis） | 替换为 SQLite 版本 |
| `src/agent/graph_thinking.py` | +RAG 注入 | 未改动 | 移除 RAG 注入，使用 frontend 版本 |
| `src/agent/websocket_server.py` | +memory_system 调用 | 完整 SSE 路由 | 使用 frontend 版本 + 移除 memory_system |
| `src/agent/repl.py` | +memory_system.save_summary | 未改动 | 移除 save_summary 调用 |
| `src/agent/tools.py` | +新工具 | 未改动 | 直接合并 |
| `src/agent/config.py` | 未改动 | 已修改 | 合并配置项 |
| `requirements.txt` | +新依赖 | +Redis 移除 | 合并依赖列表 |

---

## 执行顺序

```
Step 1: memory 分支 — session_manager.py Redis → SQLite
Step 2: memory 分支 — graph_thinking.py 移除 RAG 全局注入
Step 3: memory 分支 — memory.py 新增 list_all_* API
Step 4: memory 分支 — tools.py 新增 search_sessions 工具
Step 5: memory 分支 — websocket_server.py 移除 memory_system 调用
Step 6: memory 分支 — repl.py 移除 save_summary 调用
Step 7: memory 分支 — requirements.txt 更新依赖
Step 8: memory 分支 — 验证（REPL 测试）
Step 9: 合并到 feature/frontend-sse-streaming（解决残留冲突）
Step 10: 合并到 main
Step 11: feature/frontend-sse-streaming 分支 — 前端记忆面板 UI
Step 12: 合并前端分支到 main
```

---

## 关键约束

1. **memory 分支必须独立验证通过后再合并** — REPL 模式测试所有工具链
2. **Session 隔离原则** — SSE 会话不使用全局记忆注入，只通过工具召回
3. **SQLite 共用同一个 DB 文件** — `memory.py` 和 `session_manager.py` 共用 `MEMORY_DIR / "tasks.db"`
4. **前端暂不改动** — Phase 2 等后端接口稳定后再接入
