# LabPilot

光学实验 agent 助手 —— 基于 LangGraph 的 ReAct 智能体。通过 FastAPI 的 API 控制和 Skill 技能系统，实现对实验的智能控制。

未来计划添加记忆系统和其他 Skill。

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+（用于 Tauri 前端）
- Rust 1.70+（用于 Tauri 桌面应用）
- Anthropic API Key（用于调用 Claude 模型）

### 1. Python 环境

```bash
pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入以下环境变量：

```env
ANTHROPIC_API_KEY=sk-ant-xxxxx
ANTHROPIC_BASE_URL=https://api.anthropic.com
MODEL_ID=claude-sonnet-4-20250514
```

### 3. Tauri 桌面应用（可选）

Tauri 前端提供图形化界面，集成了 SSE 流式输出和 WebSocket 实时通知。

**安装 C++ Build Tools（仅 Windows）**

1. 下载 [Visual Studio Build Tools](https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022)
2. 安装时勾选 "C++ Build Tools"
3. 安装完成后，打开 VS Build Tools 终端（x64 Native Tools Command Prompt）

**安装 Rust**

```bash
# 从 https://rustup.rs 安装 Rust
rustup default stable
```

**启动前端**

```powershell
# 复制并修改配置
cp start-tauri.example.ps1 start-tauri.ps1
# 编辑 start-tauri.ps1，填入你的 MSVC 路径

# 启动（同时运行 Python 后端 + Tauri 前端）
.\start-tauri.ps1
```

### 4. 启动方式

**桌面应用模式（推荐）：**
```powershell
.\start-tauri.ps1
```
自动启动 Python 后端（端口 8000）+ Tauri 桌面应用。

**REPL 交互模式（终端）：**
```bash
python -m src.agent.repl
```

**LangGraph Server 模式（API 服务）：**
```bash
python -m langgraph_cli dev --port 8123
```

---

## 项目结构

```
LabPilot/
├── src/agent/
│   ├── __init__.py      # LangGraph graph 导出
│   ├── config.py        # 配置（模型、工作目录、技能路径）
│   ├── graph.py         # Server 模式图定义
│   ├── graph_thinking.py # ReAct 图定义（SSE 事件发射）
│   ├── llm.py           # LLM 初始化（ChatAnthropic）
│   ├── repl.py          # REPL 交互入口
│   ├── state.py         # 状态定义
│   ├── tools.py         # 工具定义（bash/read/write/edit/subagent/load_skill）
│   └── websocket_server.py  # NotificationHub WebSocket 服务
├── frontend/            # Tauri 桌面应用
│   ├── src/             # React 前端源码
│   │   ├── components/  # UI 组件
│   │   ├── hooks/       # SSE / WebSocket hooks
│   │   └── types/       # TypeScript 类型
│   └── src-tauri/       # Tauri Rust 后端
├── instrument/          # 仪器控制服务
│   └── pna/              # PNA 相位噪声分析仪服务
├── skills/              # 技能定义（SKILL.md）
├── data/                # 实验数据（PNA_data 等）
├── .env                 # 环境变量
├── .env.example         # 环境变量模板
├── start-tauri.ps1      # 启动脚本（个人用，gitignore）
├── start-tauri.example.ps1 # 启动脚本模板
├── langgraph.json      # LangGraph CLI 配置
└── requirements.txt     # Python 依赖
```

---

## 核心功能

### NotificationHub（端口 8000）

集中式通知调度器，连接各仪器服务于 Agent 的桥梁。

**架构：**
```
  8001: PDH-Locking 服务  ──┐
  8002: PNA 服务         ──┼── HTTP POST /notify ──► NotificationHub (8000) ──► Agent (WebSocket)
  ...                      │                         │
                          └─────────────────────────┘
```

**工作流程：**
1. Agent 启动时连接 `ws://127.0.0.1:8000/ws`
2. 各仪器服务完成任务后 POST 到 `http://127.0.0.1:8000/notify`
3. NotificationHub 通过 WebSocket 将通知推送给 Agent
4. Agent 自动触发处理，报告用户

**通知格式：**
```json
{
  "source": "pna",
  "task_id": "abc123",
  "type": "task_completed",
  "result": {"csv_path": "...", "trace_points": 801},
  "timestamp": "2026-04-24T12:00:00Z"
}
```

**环境变量：**
- `NOTIFICATION_HUB_PORT`：监听端口（默认 8000）
- `NOTIFICATION_HUB_ENABLED`：是否启用（默认 true）

### 工具集

| 工具               | 功能                          |
| ------------------ | ----------------------------- |
| `bash`           | 执行 shell 命令（支持后台模式）|
| `read_file`      | 读取文件（自动编码检测）      |
| `write_file`     | 写入文件（UTF-8）             |
| `edit_file`      | 替换文件中第一处指定文本      |
| `load_skill`     | 加载技能知识（SKILL.md）      |
| `spawn_subagent` | 派生独立子 agent 处理复杂任务 |

> **bash 后台模式**：`bash(command="...", background=True)` 用于启动长期运行的服务（如 PNA），避免阻塞 agent。

### 子 Agent

通过 `spawn_subagent` 派生两种类型的子 agent：

- **Explore**（只读）：用于代码探索、搜索、理解
- **general-purpose**（读写）：用于代码修改、文件编辑

### 技能系统

在 `skills/` 目录下放置 `SKILL.md` 文件，定义专业化知识。当前内置技能：

- **pdh-locking**：PDH（Pound-Drever-Hall）光学腔锁定系统控制技能

  控制 FastAPI 服务（http://127.0.0.1:8001），支持：

  - PI 参数计算（异步任务）
  - 锁定/解锁状态控制
  - PID 参数配置（kp/ki/kd: 0-8191）
  - 调制参数配置（频率/幅度）
  - 波形导出
  - 功率监控

- **pna**：相位噪声分析仪（Phase Noise Analyzer）测量技能

  控制 Rohde & Schwarz PNA（http://127.0.0.1:8002），特点：

  - 服务启动时建立持久连接，测量时复用
  - 异步测量，结果通过 NotificationHub 推送
  - CSV 格式输出（Frequency_Hz, Power_dBm）
  - 使用 `bash(command="python -m instrument.pna.main", background=True)` 启动

---

## REPL 命令

| 命令             | 说明             |
| ---------------- | ---------------- |
| `/help`        | 显示可用工具     |
| `/history`     | 显示对话历史     |
| `/compact`     | 手动压缩历史记录 |
| `q` / `exit` | 退出 REPL        |

---

## 上下文管理

- **Micro-compact**：保留最近 3 个工具结果，旧的标记为 `[cleared]`
- **Auto-compact**：当 token 超过阈值（默认 100,000）时，自动将历史保存到 `.transcripts/` 并用 LLM 摘要替换
