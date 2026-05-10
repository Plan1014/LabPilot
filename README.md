# LabPilot

光学实验 agent 助手 —— 基于 LangGraph 的 ReAct 智能体。通过 FastAPI 的 SSE 流式输出和 Skill 技能系统，实现对实验的智能控制。

## 核心功能

- **三层对话压缩**：micro_compact（工具结果占位符）→ auto_compact（LLM 总结摘要）→ compact（手动压缩）
- **长期记忆系统**：ChromaDB 向量存储事实记忆 + 对话摘要，支持语义检索
- **SSE 流式对话**：前端实时流式输出，支持多会话管理和历史切换
- **WebSocket 通知**：PDH/PNA 等设备任务完成实时推送
- **Skill 技能系统**：通过 SKILL.md 定义领域知识，Agent 按需加载

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+（用于 Tauri 前端）
- Rust 1.70+（用于 Tauri 桌面应用）
- Anthropic API Key 或兼容 API

### 1. Python 环境

```bash
pip install -r requirements.txt
# 首次启动会自动下载 sentence-transformers 模型（约 60MB）
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入以下环境变量：

```env
ANTHROPIC_API_KEY=sk-ant-xxxxx
ANTHROPIC_BASE_URL=https://api.anthropic.com
MODEL_ID=claude-sonnet-4-20250514
```

### 3. 启动方式

三种启动方式：

**方式一：REPL 交互（纯终端）**
```bash
venv\Scripts\python.exe -m src.agent.repl
```
直接进入命令行 REPL，支持 `/compact` 手动压缩、`/history` 查看历史。

**方式二：Dev 模式（后端 + Vite 前端）**
```powershell
.\start-dev.ps1
```
启动 Python 后端（端口 8000）+ Vite 前端（端口 1420），适合开发调试。

**方式三：Tauri 桌面应用**
```powershell
.\start-tauri.ps1
```
启动 Python 后端 + Tauri 桌面应用，需配置 MSVC Build Tools 和 Rust 环境。

> 手动启动后端：`venv\Scripts\python.exe -m uvicorn src.agent.websocket_server:create_notification_hub_app --factory --host 127.0.0.1 --port 8000`

---

## 项目结构

```
LabPilot/
├── src/agent/
│   ├── __init__.py          # LangGraph graph 导出
│   ├── config.py            # 配置（模型、工作目录、阈值）
│   ├── graph.py             # Server 模式图定义
│   ├── graph_thinking.py     # ReAct 图定义（SSE 事件发射）
│   ├── llm.py               # LLM 初始化（ChatAnthropic）
│   ├── memory.py            # 长期记忆系统（ChromaDB + SQLite）
│   ├── repl.py              # REPL 交互入口
│   ├── session_manager.py   # 会话存储（SQLite，三层压缩）
│   ├── state.py             # 状态定义
│   ├── tools.py             # 工具定义（bash/read/write/edit/subagent/load_skill/remember_fact/search_memory/search_sessions/compact）
│   └── websocket_server.py  # NotificationHub（SSE + WebSocket）
├── frontend/                # Tauri 桌面应用
│   ├── src/                 # React 前端源码
│   │   ├── components/       # UI 组件（ChatWindow, MemoryModal, HistoryModal...）
│   │   ├── hooks/           # SSE / WebSocket hooks
│   │   └── types/           # TypeScript 类型
│   └── src-tauri/           # Tauri Rust 后端
├── skills/                  # 技能定义（SKILL.md）
├── data/
│   ├── memory/              # ChromaDB + SQLite（记忆系统）
│   └── sessions/           # JSON 归档会话
├── .transcripts/           # 压缩后的对话记录
├── .env                    # 环境变量
├── .env.example            # 环境变量模板
├── start-dev.ps1            # 启动脚本
├── requirements.txt       # Python 依赖
└── README.md
```

---

## 后端 API（端口 8000）

### 会话管理

| 接口 | 方法 | 说明 |
|------|------|------|
| `/session/query` | POST | SSE 流式查询，支持 session_id |
| `/session/list` | GET | 会话列表（按最近活动时间排序） |
| `/session/{id}` | GET | 获取会话元信息 |
| `/session/{id}/history` | GET | 加载历史会话 |
| `/session/{id}` | DELETE | 删除会话 |

### 记忆系统

| 接口 | 方法 | 说明 |
|------|------|------|
| `/memory/facts` | GET | 事实记忆列表（分页） |
| `/memory/summaries` | GET | 对话摘要列表（分页） |
| `/memory/facts/{doc_id}` | DELETE | 删除事实记忆 |
| `/memory/summaries/{doc_id}` | DELETE | 删除摘要记忆 |
| `/memory/search?query=` | GET | 语义检索记忆 |

### 设备通知

| 接口 | 方法 | 说明 |
|------|------|------|
| `/notify` | POST | 接收设备任务完成通知 |
| `/ws` | WebSocket | Agent WebSocket 连接 |

---

## 三层对话压缩

### Layer 1: micro_compact（每次请求自动执行）

保留最近 30 个工具结果，旧的替换为短占位符 `[Previous: used {tool_name}]`。`read_file` 结果始终保留原文（参考材料）。

### Layer 2: auto_compact（token 超过阈值时自动执行）

当对话 token 数超过阈值（默认 100,000）时：
1. 将完整对话保存到 `.transcripts/`
2. 调用 LLM 生成摘要
3. 摘要持久化到 ChromaDB（长期记忆）
4. 会话历史替换为单条 summary 消息

### Layer 3: compact 工具（Agent 主动触发）

Agent 可通过 `compact` 工具主动压缩当前会话，触发时机由 Agent 自行判断。

---

## 核心架构

### NotificationHub（端口 8000）

```
  8001: PDH-Locking 服务  ──┐
  8002: PNA 服务         ──┼── HTTP POST /notify ──► NotificationHub (8000) ──► Agent (WebSocket)
  ...                      │                         │
                          └─────────────────────────┘
```

### 存储层次

- **SQLite**（`data/memory/tasks.db`）：会话历史 + 任务状态（TTL 30 天）
- **ChromaDB**（`data/memory/chroma_db/`）：事实记忆 + 对话摘要（向量检索）
- **文件系统**（`.transcripts/`）：压缩后的对话原文归档
- **JSON**（`data/sessions/`）：会话 JSON 归档

### 长期记忆 RAG

Agent 可通过以下工具访问长期记忆：

- `remember_fact(fact)` — 记住关键事实（参数、Bug 解法等）
- `search_memory(query)` — 语义检索历史记忆
- `search_sessions(query, days)` — 搜索历史会话
- `compact()` — 主动压缩当前会话

---

## 工具集

| 工具 | 功能 |
|------|------|
| `bash` | 执行 shell 命令（支持后台模式） |
| `read_file` | 读取文件（自动编码检测） |
| `write_file` | 写入文件（UTF-8） |
| `edit_file` | 替换文件中第一处指定文本 |
| `load_skill` | 加载技能知识（SKILL.md） |
| `spawn_subagent` | 派生独立子 agent 处理复杂任务 |
| `remember_fact` | 将关键事实写入长期记忆 |
| `search_memory` | 语义检索长期记忆 |
| `search_sessions` | 搜索历史会话 |
| `compact` | 手动压缩当前会话历史 |

---

## 技能系统

在 `skills/` 目录下放置 `SKILL.md` 文件，定义专业化知识。当前内置技能：

- **pdh-locking**：PDH（Pound-Dreber-Hall）光学腔锁定系统控制技能
- **pna**：相位噪声分析仪测量技能

---

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示可用工具 |
| `/history` | 显示对话历史 |
| `/compact` | 手动压缩历史记录 |
| `q` / `exit` | 退出 REPL |

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MODEL_ID` | `claude-sonnet-4-20250514` | 模型 ID |
| `NOTIFICATION_HUB_PORT` | `8000` | API 监听端口 |
| `NOTIFICATION_HUB_ENABLED` | `true` | 是否启用 NotificationHub |
| `SESSION_TTL_DAYS` | `30` | 会话保留天数 |
| `TOKEN_THRESHOLD` | `100000` | 触发 auto_compact 的 token 阈值 |
