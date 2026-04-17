# LabPilot

光学实验 agent 助手 —— 基于 LangGraph 的 ReAct 智能体。通过FastAPI的api控制和Skill技能系统，实现对实验的智能控制。

未来计划添加记忆系统和其他Skill。

## 快速开始

### 环境要求

- Python 3.11+
- Anthropic API Key（用于调用 Claude 模型）

### 安装

```bash
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 为 `.env`，填入以下环境变量：

```env
ANTHROPIC_API_KEY=sk-ant-xxxxx
ANTHROPIC_BASE_URL=https://api.anthropic.com
MODEL_ID=claude-sonnet-4-20250514
```

### 启动

**REPL 交互模式（终端）：**

```bash
python -m src.agent.repl
```

**LangGraph Server 模式（API 服务）：**

```bash
python -m langgraph_cli dev --port 8123
```

服务启动后访问 http://127.0.0.1:8123 查看交互界面。

---

## 项目结构

```
LabPilot/
├── src/agent/
│   ├── __init__.py      # LangGraph graph 导出
│   ├── config.py        # 配置（模型、工作目录、技能路径）
│   ├── graph.py         # Server 模式图定义
│   ├── llm.py           # LLM 初始化（ChatAnthropic）
│   ├── repl.py          # REPL 交互入口
│   ├── state.py         # 状态定义
│   └── tools.py         # 工具定义（bash/read/write/edit/subagent/load_skill）
├── skills/              # 技能定义（SKILL.md）
├── .env                 # 环境变量
├── .env.example         # 环境变量模板
├── agent_langgraph.py   # LangGraph CLI 入口
├── langgraph.json      # LangGraph CLI 配置
└── requirements.txt     # 依赖
```

---

## 核心功能

### 工具集

| 工具               | 功能                          |
| ------------------ | ----------------------------- |
| `bash`           | 执行 shell 命令               |
| `read_file`      | 读取文件（自动编码检测）      |
| `write_file`     | 写入文件（UTF-8）             |
| `edit_file`      | 替换文件中第一处指定文本      |
| `load_skill`     | 加载技能知识（SKILL.md）      |
| `spawn_subagent` | 派生独立子 agent 处理复杂任务 |

### 子 Agent

通过 `spawn_subagent` 派生两种类型的子 agent：

- **Explore**（只读）：用于代码探索、搜索、理解
- **general-purpose**（读写）：用于代码修改、文件编辑

### 技能系统

在 `skills/` 目录下放置 `SKILL.md` 文件，定义专业化知识。当前内置技能：

- **pdh-locking**：PDH（Pound-Drever-Hall）光学腔锁定系统控制技能

  控制 FastAPI 服务（http://127.0.0.1:8000），支持：

  - PI 参数计算（异步任务）
  - 锁定/解锁状态控制
  - PID 参数配置（kp/ki/kd: 0-8191）
  - 调制参数配置（频率/幅度）
  - 波形导出
  - 功率监控

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
