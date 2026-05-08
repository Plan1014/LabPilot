"""Tool definitions for LabPilot LangGraph Agent.

All tools are @tool-decorated functions exposed to the LangGraph agent.
"""

import re
import subprocess
from pathlib import Path
from typing import Callable, List

from langchain_core.tools import tool

from src.agent.config import WORKDIR, SKILLS_DIR


# ==================== Security ====================

def safe_path(p: str) -> Path:
    """Resolve path safely, preventing directory traversal attacks."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# ==================== Base Tools ====================

@tool
def bash(command: str, background: bool = False) -> str:
    """Execute a shell command in the agent's working directory.

    Args:
        command: The shell command to execute.
        background: If True, run in background (for long-running services).
                    Do NOT set to True for commands that should complete.

    Returns:
        The combined stdout and stderr output, truncated to 50,000 chars.
        For background processes, returns confirmation message.
    """
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        if background:
            # Windows: hide console window for background process
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            subprocess.Popen(
                command,
                shell=True,
                cwd=WORKDIR,
                startupinfo=startupinfo,
            )
            return f"[Background] Process started: {command[:80]}"
        else:
            r = subprocess.run(
                command,
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=120,
            )
            out = (r.stdout + r.stderr).strip()
            return out[:50000] if out else "(no output)"
    except Exception as e:
        return f"Error: {e}"


@tool
def read_file(path: str, limit: int = None) -> str:
    """Read file content with automatic encoding detection.

    Args:
        path: Relative path from working directory.
        limit: Optional line count limit (truncates with marker).

    Returns:
        File content as string, or error message.
    """
    fp = safe_path(path)
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
    """Write content to a file (UTF-8 encoding).

    Args:
        path: Relative path from working directory.
        content: The content to write.

    Returns:
        Success message with byte count, or error message.
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


@tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replace the first occurrence of old_text with new_text in a file.

    Args:
        path: Relative path from working directory.
        old_text: The exact text to find and replace.
        new_text: The replacement text.

    Returns:
        Success message, or error if text not found.
    """
    fp = safe_path(path)
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


# ==================== Skill Loader ====================

class SkillLoader:
    """Load skill definitions from SKILL.md files in the skills directory.

    Each SKILL.md may contain YAML frontmatter (--- ... ---) with metadata
    like name, description, and a body with the actual skill content.
    """

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

                # Parse frontmatter
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
        """Return a formatted string of all skill names and descriptions."""
        if not self.skills:
            return "(no skills)"
        return "\n".join(
            f"  - {n}: {s['meta'].get('description', '-')}"
            for n, s in self.skills.items()
        )

    def load(self, name: str) -> str:
        """Load a skill by name, returning content wrapped in <skill> tags."""
        s = self.skills.get(name)
        if not s:
            available = ", ".join(self.skills.keys())
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f'<skill name="{name}">\n{s["body"]}\n</skill>'


# Global skill loader instance
SKILLS = SkillLoader(SKILLS_DIR)


@tool
def load_skill(name: str) -> str:
    """Load specialized knowledge by skill name.

    Args:
        name: The name of the skill to load (matches SKILL.md filename or frontmatter name).

    Returns:
        Skill content wrapped in <skill> tags, or error message.
    """
    return SKILLS.load(name)


# ==================== Subagent System ====================

def create_subagent_tools(agent_type: str = "Explore") -> List:
    """Create the tool set for a subagent based on agent type.

    Args:
        agent_type: "Explore" (read-only) or "general-purpose" (read-write).

    Returns:
        List of tools to attach to the subagent.
    """
    base = [bash, read_file]
    if agent_type != "Explore":
        base += [write_file, edit_file]
    return base


def create_subagent(agent_type: str = "Explore") -> Callable:
    """Factory: create an isolated subagent as a callable prompt runner.

    Each subagent runs in its own ReAct loop with its own tool set.

    Args:
        agent_type: "Explore" (read-only) or "general-purpose" (read-write).

    Returns:
        A callable that takes a prompt string and returns the agent's response.
    """
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, AIMessage
    from langgraph.prebuilt import create_react_agent

    from src.agent.config import MODEL_ID
    from src.agent.llm import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL

    sub_llm = ChatAnthropic(
        model=MODEL_ID,
        anthropic_api_key=ANTHROPIC_API_KEY,
        base_url=ANTHROPIC_BASE_URL or None,
    )
    sub_tools = create_subagent_tools(agent_type)

    sub_graph = create_react_agent(
        model=sub_llm,
        tools=sub_tools,
        debug=False,
    )

    def run(prompt: str) -> str:
        result = sub_graph.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"max_iterations": 30, "max_tokens": 8000},
        )
        for msg in reversed(result.get("messages", [])):
            if isinstance(msg, AIMessage) and msg.content:
                return msg.content
        return "(no summary)"

    return run


@tool
def spawn_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """Spawn an isolated subagent to work independently on a task.

    The subagent completes its work and returns a summary of what it did.
    Use this for multi-step or isolated tasks that should not clutter the
    main agent's context.

    Args:
        prompt: A detailed description of the task for the subagent.
        agent_type: "Explore" for read-only tasks, "general-purpose" for
            tasks that need to write or edit files.

    Returns:
        The subagent's final response/summary.
    """
    subagent = create_subagent(agent_type)
    return subagent(prompt)

# ==================== 记忆管理工具 ====================
from src.agent.memory import memory_system, memory_retriever

@tool
def remember_fact(fact: str) -> str:
    """
    当实验成功、获得最佳参数、或者发现代码 Bug 及解决办法时，主动调用此工具将事实永久保存。
    
    Args:
        fact: 详细的事实描述（如："PDH锁定的最佳 PI 参数为 P=15, I=3"）
    """
    try:
        memory_system.save_fact(fact, source="tool")
        return "已成功将该事实写入长期记忆库。"
    except Exception as e:
        return f"写入失败: {e}"

@tool
def search_memory(query: str) -> str:
    """
    主动从系统的长期记忆中检索历史实验记录、成功的参数或历史总结。
    当你不确定之前的操作，或需要参考历史经验时调用。
    
    Args:
        query: 检索关键词或自然语言描述
    """
    try:
        # 直接复用 Retriever 的多路召回能力
        result = memory_retriever.retrieve_context(query)
        return result if result and "无长期记忆" not in result else "未检索到相关历史记录。"
    except Exception as e:
        return f"检索失败: {e}"

# ==================== Tool List ====================

TOOLS: List[Callable] = [
    bash,
    read_file,
    write_file,
    edit_file,
    load_skill,
    spawn_subagent,
    remember_fact,
    search_memory, 
]
