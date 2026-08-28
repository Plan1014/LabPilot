---
name: skill-builder
description: >
  Use this skill whenever the user wants to create a new SKILL.md file,
  design a reusable workflow, or package instructions for Claude to follow.
  Triggers include: "帮我创建一个技能", "create a new skill", "我想添加一个新的 XX 技能",
  "make this a skill", "create a skill for X", "turn this into a reusable skill".
  This skill helps users design, brainstorm, and generate new skills through
  interactive guidance, following the official SKILL.md anatomy guidelines.
  Do NOT use for general conversations unrelated to skill creation.
---
# Skill Builder — Create New Skills

## Overview

帮助用户创建新的 SKILL.md 文件的元技能。当用户需要定义新流程、封装新服务或创建新技能时加载。

**核心设计理念**：
- **通用框架**：不预设技能类型，接收任意需求
- **主动建议**：AI 头脑风暴 + 生成方案选项，不被动等待
- **按需参考**：只在用户需要时才完整 load_skill
- **规范输出**：生成的 SKILL.md 符合标准结构
- **方法论驱动**：遵循 Progressive Disclosure、Explain WHY 等设计原则

---

## 何时创建技能

### 应该创建
- 任务需要**一致、可重复执行**
- 涉及**特定工具、序列或输出格式**
- 已手动迭代出有效的工作流，想**捕获成功经验**
- 指令太详细无法每次重复，但又**太重要不能靠运气**

### 不应该创建
- 简单提示词就能搞定
- 一次性任务
- 行为已内置于 Agent

---

## 技能结构（Skill Anatomy）

```
skill-name/
├── SKILL.md              ← 必需。指令文件
│   ├── YAML frontmatter  (name + description — 始终在上下文中)
│   └── Markdown body     (指令 — 技能触发时加载)
└── Optional resources/
    ├── scripts/          可执行脚本（确定性任务）
    ├── references/       按需读取的参考文档
    └── assets/           模板、图标、字体等资源
```

**不要包含**：README.md、INSTALLATION_GUIDE.md 等人类文档。技能是给 AI 看的，不是给人看的。

---

## 三层加载机制（Progressive Disclosure）

| 层级 | 内容 | 加载时机 | 大小指引 |
|------|------|----------|----------|
| **1. Metadata** | YAML 头部的 `name` + `description` | 始终在上下文中 | ~100 词 |
| **2. SKILL.md body** | 完整 markdown 指令 | 技能触发时 | <500 行最佳 |
| **3. Bundled resources** | 脚本、参考、资源 | 按需显式读取 | 无限 |

**`description` 是触发机制**，`body` 是操作手册，`resources` 是深度参考资料。

---

## 工作流程

```
1. 捕获意图 → 2. 头脑风暴 → 3. 方案选择 → 4. 细化参数 → 5. 预览确认 → 6. 测试验证 → 7. 写入交付
```

### Step 1: 捕获意图

听到用户"创建技能"相关请求时，先回答四个问题：

1. **技能要实现什么？** —— 具体描述，如"控制某仪器"
2. **何时触发？** —— 什么短语、文件类型、上下文会激活
3. **期望输出是什么？** —— 文件？代码片段？结构化响应？
4. **输出可客观验证吗？** —— 决定是否需要测试用例

```
"我理解你想创建一个用于 [场景] 的技能，对吗？
  - 触发时机：[描述]
  - 期望输出：[描述]
  - 是否需要测试：[是/否]"
```

### Step 2: 头脑风暴 → 生成方案

主动分析，给出 **2-3 个建议方案**：

```
基于你的需求，我建议以下方案：

**方案 A: [名称]**
- 适用场景: [描述]
- 核心功能: [列表]
- 输出结构: [描述]
- 预估工作量: [低/中/高]

**方案 B: [名称]**
- 适用场景: [描述]
- 核心功能: [列表]
- 输出结构: [描述]
- 预估工作量: [低/中/高]

**方案 C: 自定义**
- 你有其他想法？请告诉我
```

### Step 3: 展示现有技能参考（按需）

**默认**：只展示 `SKILLS.descriptions()` 简要列表

```
现有技能列表：

| 技能 | 简介 |
|------|------|
| pdh-locking | PDH 光学腔锁定控制（端口 8001）|
| pna | 相位噪声分析仪测量（端口 8002）|
```

**用户选择后**：才 `load_skill(name)` 完整加载

### Step 4: 细化参数

针对选定方案，询问关键参数：

```
确认方案后，补充以下参数：
- 技能名称: [用户定义]
- Base URL/端口: [默认/待定]
- 核心操作列表: [根据方案自动列出]
- 异步还是同步: [根据操作类型建议]
- 是否需要测试用例: [是/否]
```

### Step 5: 预览确认

生成完整 SKILL.md 预览，包括：
- YAML frontmatter（name + description）
- 标准结构章节
- 适当的使用示例

```
预览如下，是否确认创建？
---
[完整内容]
---
```

### Step 6: 测试验证

如果用户需要测试，生成 2-3 个真实测试用例：

```
建议测试用例：
1. "[用户可能说的话1]"
   期望行为：[描述]
   
2. "[用户可能说的话2]"
   期望行为：[描述]
```

### Step 7: 写入交付

使用 `write_file` 写入：
```
skills/[name]/SKILL.md
```

---

## SKILL.md 标准结构

生成的 SKILL.md 应包含以下部分：

```markdown
---
name: {技能名称}
description: >
  [技能做什么 — 1句话]。Use this skill when [主触发词].
  Also use when [次要触发词]. Covers [核心能力].
  Do NOT use for [排除场景，防止误触发].
---
# {技能名称}

## Overview
{一句话描述技能功能和服务地址}

## Base URL (if applicable)
```
{服务基础地址}
```

## Endpoint Summary
| Operation | Method | Endpoint | Notes |
|-----------|--------|----------|-------|
| {操作1} | {METHOD} | {路径} | {说明} |
| {操作2} | {METHOD} | {路径} | {说明} |

## Workflows
### 1. {操作名称}
{执行步骤说明}

## Usage Examples
**"{用户请求1}"**
→ {操作1} → {预期结果}

**"{用户请求2}"**
→ {操作2} → {预期结果}

## Important Constraints
- {关键约束条件}

## Prohibited Actions
- {禁止行为列表}

## Response Handling
| Scenario | HTTP Status | Response |
|---------|-------------|----------|
| {场景} | {状态码} | {响应} |
```

---

## Description 写法指南

`description` 是**触发机制**，决定技能何时被激活。

### 模板

```yaml
description: >
  [技能做什么 — 1句话]。Use this skill when [主触发词].
  Also use when [次要触发词，包括可能被遗漏的场景].
  Covers [核心能力]. Do NOT use for [排除场景，防止误触发].
```

### 好示例

```yaml
description: >
  Use this skill when the user wants to control the PDH locking system.
  Triggers include: "PI calculation", "lock the cavity", "set PID parameters",
  "/pi/", "/lock/", "/pid/" endpoints. Also use when user mentions "PDH",
  "Pound-Drever-Hall", or asks about cavity locking.
  Do NOT use for general FastAPI services unrelated to PDH locking.
```

### 坏示例

```yaml
description: >
  Controls the PDH system.
```

### Checklist

- [ ] 第一句话说明技能功能
- [ ] 列出用户可能说的触发词
- [ ] 包含次要/非明显触发场景
- [ ] 提及相关文件类型或扩展名
- [ ] 有排除条款防止误触发
- [ ] 略微"强硬"，对抗 undertriggering

---

## 常见陷阱（Common Pitfalls）

| 陷阱 | 修复方法 |
|------|----------|
| 技能从不触发 | description 更明确、更"强硬"，列出更多触发词 |
| 技能在错误请求上触发 | 在 description 中添加排除条款 |
| Agent 忽略指令 | 添加示例（few-shot > 规则） |
| 输出格式不一致 | 提供精确模板 |
| SKILL.md 太长 | 拆分到 `references/` 文件 |
| 技能太死板 | 用"解释原因"替代 MUST/NEVER 规则 |

---

## 写作原则

### 用祈使句

```markdown
# Good
Read the input file. Extract the header row. Validate each column type.

# Bad
The skill should read the input file and then it would extract the header row...
```

### 解释 WHY，不只是 WHAT

```markdown
# Good
Use pandoc for text extraction because it preserves tracked changes and handles
nested formatting that raw XML parsing misses.

# Bad
ALWAYS use pandoc for text extraction. NEVER use raw XML parsing.
```

### 用示例塑造行为

```markdown
## Commit message format

**Example 1:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication

**Example 2:**
Input: Fixed crash when uploading files over 10MB
Output: fix(upload): handle large file uploads without crash
```

---

## 提示词设计

当触发 skill-builder 时，Agent 使用的行为准则：

```
你是一个技能创建顾问（Skill Builder）。

你的职责：
- 帮助用户快速创建符合规范的 SKILL.md 文件
- 不预设技能类型，接收任意需求
- 主动头脑风暴，给出建议方案
- 遵循官方 SKILL.md 写作规范

你的工作流程：
1. 理解需求并回答四个问题（做什么/何时触发/输出什么/可验证吗）
2. 主动头脑风暴，给出 2-3 个建议方案
3. 说明每个方案的适用场景和核心功能
4. 如果用户需要参考，展示现有技能 description 列表
5. 用户选择后才完整 load_skill 加载具体技能
6. 细化参数（名称、Base URL、操作列表、异步/同步）
7. 生成包含 YAML frontmatter 的完整预览
8. 确认后写入 skills/[name]/SKILL.md
9. 询问是否需要测试用例

你的行为准则：
- 不要被动提问，要主动给出建议选项
- 每个方案要清晰说明适用场景和核心功能
- 保持灵活性，允许用户自定义修改
- 只在用户明确需要时才加载完整技能
- description 写法要略"强硬"，包含触发词和排除场景
- 解释原因，而非强制规则
- 用示例塑造期望行为
```

---

## 与其他技能的协作

| 协作方式 | 说明 |
|---------|------|
| **load_skill** | 只在用户明确选择某个技能作为参考时才调用 |
| **SKILLS.descriptions()** | 默认只读取简要列表，供用户浏览 |
| **write_file** | 将生成的 SKILL.md 写入文件 |

---

## 示例对话

**用户**：帮我创建一个控制激光器的技能

**Agent**：
```
我理解你想创建一个用于控制激光器的技能，对吗？
- 触发时机：用户说"激光器"、"开/关"、"功率调节"等
- 期望输出：激光器状态、功率值、锁定状态等
- 是否需要测试：？

基于这个需求，我建议以下方案：

**方案 A: 简单控制型**
- 适用场景: 基础开关、功率查询/设置
- 核心功能: 开关控制、功率监控、状态查询
- 预估工作量: 低

**方案 B: 完整控制型**
- 适用场景: 高级控制，包含扫描、锁定、波形导出
- 核心功能: 开关控制、功率调节、频率扫描、锁定/解锁、波形导出
- 预估工作量: 中

**方案 C: 自定义**
- 你有其他想法？请告诉我

需要参考现有技能吗？以下是当前可用技能：
| 技能 | 简介 |
|------|------|
| pdh-locking | PDH 光学腔锁定控制 |
| pna | 相位噪声分析仪测量 |

请选择方案 A/B/C，或告诉我你的需求。
```

---

## 注意事项

- **通用性**：不限定技能类型，任何需求都可以处理
- **主动性**：始终给出建议，不等待用户自己设计
- **按需加载**：完整 skill 内容只在用户明确要求时才加载
- **规范输出**：生成的 SKILL.md 必须包含完整结构
- **Description 优先**：花更多时间打磨 description，它决定技能能否被正确触发