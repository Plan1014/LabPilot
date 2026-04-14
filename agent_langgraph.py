#!/usr/bin/env python3
"""
agent_langgraph.py - 基于 LangGraph 的 Agent（仅保留 Subagent）

从 refer.py 重构而来，基于 LangGraph。
仅保留 subagent 子 agent 模块，无 inbox、teammate、auto-claim 等多 agent 逻辑。

    +------------------------------------------------------------------+
    |                        LANGGRAPH AGENT                            |
    |                                                                   |
    |  System prompt (skills, task-first)                               |
    |                                                                   |
    |  LangGraph StateGraph:                                            |
    |  +--------+----------+----------+---------+-----------+          |
    |  | bash   | read     | write    | edit    | load_skill|          |
    |  | spawn_ |          |          |         |           |          |
    |  | subagent          |          |         |           |          |
    |  +--------+----------+----------+---------+-----------+          |
    |                                                                   |
    |  Subagent: spawn -> work -> return summary                       |
    +------------------------------------------------------------------+

    REPL 命令: /compact /help
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

from anthropic import Anthropic
from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import tool
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

# 加载环境变量
load_dotenv(override=True)

# 工作目录
WORKDIR = Path.cwd()

# 创建 LangChain chat model
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL")
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-20250514")

llm = ChatAnthropic(
    model=MODEL,
    anthropic_api_key=ANTHROPIC_API_KEY,
    base_url=ANTHROPIC_BASE_URL or None,
)

# 原始客户端（用于压缩功能）
client = Anthropic(base_url=ANTHROPIC_BASE_URL)

# 技能目录、转录目录、token 阈值
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOKEN_THRESHOLD = 100000


# ==================== ReAct 追踪回调 ====================

class ToolCallbackHandler(BaseCallbackHandler):
    """ReAct 循环可视化回调

    在每个工具调用时打印：
    - 工具名称和输入参数
    - 工具执行结果（截断到 300 字符）
    """

    step_count: int = 0

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        self.step_count += 1
        tool_name = serialized.get("name", serialized.get("id", "?"))
        # 截断过长的输入
        display_input = input_str[:500] + "..." if len(input_str) > 500 else input_str
        print(f"\n\033[33m[Step {self.step_count}] Calling tool: {tool_name}\033[0m")
        if display_input:
            print(f"  Input: {display_input}")

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        # output 可能是 ToolMessage 对象或字符串
        try:
            output_str = str(output.content) if hasattr(output, "content") else str(output)
        except Exception:
            output_str = str(output)
        display_output = output_str[:300] + "..." if len(output_str) > 300 else output_str
        print(f"\033[32m[Step {self.step_count}] Result\033[0m: {display_output}")

    def reset(self) -> None:
        self.step_count = 0


# ==================== 基础工具 ====================

def safe_path(p: str) -> Path:
    """安全路径解析，防止路径遍历攻击"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


@tool
def bash(command: str) -> str:
    """执行 shell 命令"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


@tool
def read_file(path: str, limit: int = None) -> str:
    """读取文件内容，尝试多种编码"""
    fp = safe_path(path)
    # 按优先级尝试常见编码
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            lines = fp.read_text(encoding=encoding, errors="replace").splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
            return "\n".join(lines)[:50000]
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return f"Error: {e}"
    return f"Error: unable to decode {path}"


@tool
def write_file(path: str, content: str) -> str:
    """写入文件内容（UTF-8 编码）"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


@tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """替换文件中的指定文本"""
    fp = safe_path(path)
    # 按优先级尝试多种编码读取
    content = None
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            content = fp.read_text(encoding=encoding, errors="replace")
            break
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return f"Error: {e}"
    if content is None:
        return f"Error: unable to decode {path}"
    if old_text not in content:
        return f"Error: Text not found in {path}"
    try:
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# ==================== 技能加载器 ====================

class SkillLoader:
    """加载 skills 目录下的 SKILL.md 文件"""
    def __init__(self, skills_dir: Path):
        self.skills = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = None
                for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                    try:
                        text = f.read_text(encoding=enc, errors="replace")
                        break
                    except UnicodeDecodeError:
                        continue
                if text is None:
                    continue
                # 解析 frontmatter (--- ... ---)
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        """返回所有技能的描述"""
        if not self.skills:
            return "(no skills)"
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        """加载指定名称的技能"""
        s = self.skills.get(name)
        if not s:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


SKILLS = SkillLoader(SKILLS_DIR)


@tool
def load_skill(name: str) -> str:
    """加载 specialized knowledge by name"""
    return SKILLS.load(name)


# ==================== 上下文压缩 ====================

def estimate_tokens(messages: list) -> int:
    """估算消息列表的 token 数量（粗略）"""
    return len(json.dumps(messages, default=str)) // 4


def microcompact(messages: list):
    """微压缩：保留最后 3 个工具结果，之前的标记为 [cleared]"""
    indices = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    indices.append(part)
    if len(indices) <= 3:
        return
    for part in indices[:-3]:
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            part["content"] = "[cleared]"


def auto_compact(messages: list) -> list:
    """自动压缩：将对话历史保存到文件，用摘要替换"""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    # 保存完整历史到 jsonl 文件
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    # 压缩最后 80000 字符的内容
    conv_text = json.dumps(messages, default=str)[-80000:]
    resp = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000,
    )
    summary = resp.content[0].text
    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"},
    ]


# ==================== Subagent (子 Agent) ====================

def create_subagent_tools(agent_type: str = "Explore"):
    """根据 agent_type 创建子 agent 的工具集

    - Explore: 只读工具 (bash, read_file)
    - general-purpose: 读写工具 (bash, read_file, write_file, edit_file)
    """
    base_tools = [bash, read_file]
    if agent_type != "Explore":
        base_tools += [write_file, edit_file]
    return base_tools


def create_subagent(agent_type: str = "Explore"):
    """工厂函数：创建子 agent 的 StateGraph

    每个子 agent 是独立的 LangGraph REACT agent，
    有自己的工具集，在自己的上下文中运行。
    """
    sub_tools = create_subagent_tools(agent_type)

    # 使用 create_react_agent 创建子 agent
    sub_graph = create_react_agent(
        model=llm,
        tools=sub_tools,
        debug=False,
    )

    def run(prompt: str) -> str:
        """执行子 agent 并返回最终结果"""
        result = sub_graph.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"max_iterations": 30, "max_tokens": 8000}
        )
        # 从结果中提取最后一个 AI 消息的文本
        messages = result.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                return msg.content
        return "(no summary)"

    return run


@tool
def spawn_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """Spawn 子 agent 进行独立探索或工作

    子 agent 独立运行，拥有自己的工具集，完成后返回摘要。

    Args:
        prompt: 子 agent 的任务描述
        agent_type: "Explore" (只读) 或 "general-purpose" (读写)
    """
    subagent = create_subagent(agent_type)
    return subagent(prompt)


# ==================== LangGraph Agent 主入口 ====================

def create_agent(tools: list):
    """创建主 Agent

    使用 create_react_agent 构建 LangGraph agent，
    内部处理 model + tools 的路由逻辑。
    """
    # 系统提示词
    system_prompt = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
Use spawn_subagent for multi-step or isolated work.
Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""

    # 创建 REACT agent
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
        debug=False,
    )

    return agent


# ==================== REPL 交互界面 ====================

def main():
    # 收集所有工具
    all_tools = [bash, read_file, write_file, edit_file, load_skill, spawn_subagent]

    # 创建 agent
    agent = create_agent(all_tools)

    print(f"LabPilot LangGraph Agent (model: {MODEL})")
    print(f"Workspace: {WORKDIR}")
    print("Commands: /compact (compact context), /help, q (quit)")
    print()

    history = []

    while True:
        try:
            query = input("\033[36mLabPilot >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        if query.strip() == "/help":
            print("Available tools:")
            for t in all_tools:
                print(f"  - {t.name}: {t.description.split(chr(10))[0]}")
            print()
            continue

        if query.strip() == "/compact":
            if history:
                print("[manual compact via /compact]")
                history = auto_compact(history)
                print(f"Compressed to {len(history)} messages")
            else:
                print("No history to compact")
            continue

        # 添加用户消息到历史
        history.append({"role": "user", "content": query})

        # 微压缩检查
        microcompact(history)

        # 检查 token 数量，必要时自动压缩
        if estimate_tokens(history) > TOKEN_THRESHOLD:
            print("[auto-compact triggered]")
            history = auto_compact(history)

        # 调用 agent（带回调以追踪 ReAct 循环）
        try:
            langchain_messages = [
                HumanMessage(content=m["content"]) if isinstance(m, dict) else m
                for m in history
            ]

            # 每次循环重置计数器
            callback = ToolCallbackHandler()
            result = agent.invoke(
                {"messages": langchain_messages},
                config={"max_iterations": 100, "max_tokens": 8000, "callbacks": [callback]}
            )
            response_messages = result.get("messages", [])

            # 解析模型响应，分离 thinking 和 text
            for msg in reversed(response_messages):
                if isinstance(msg, AIMessage) and msg.content:
                    content = msg.content
                    thinking_parts = []
                    text_parts = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "thinking":
                                    thinking_parts.append(block.get("thinking", ""))
                                elif block.get("type") == "text":
                                    text_parts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                text_parts.append(block)
                    else:
                        text_parts.append(str(content))

                    # 先打印 thinking
                    if thinking_parts:
                        print("\033[90m[Thinking]\033[0m")
                        for t in thinking_parts:
                            print(f"  {t}")
                        print()

                    # 再打印 text
                    for t in text_parts:
                        print(t)
                    break

        except Exception as e:
            print(f"Error: {e}")

        print()


if __name__ == "__main__":
    main()
