"""其余执行入口的闸门覆盖（server 模式 / 记忆子 agent / REPL）。

这三条路径不走 `graph_thinking.authorize_node` —— 它们各自直接建 agent，
所以必须各自套一层闸门，否则从这些入口发起的工具调用是裸奔的。
"""

import builtins

import pytest

from src.agent import tools
from src.agent.authz import wrap_tools_with_gate
from src.agent.authz.judge import JudgeResult


@pytest.fixture
def gated_read_file(tmp_path, monkeypatch):
    """一个被闸门套住的 read_file，指向临时目录。"""
    from src.agent import paths

    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(tools, "WORKDIR", tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "src").mkdir()
    target = tmp_path / "tmp" / "a.txt"
    target.write_text("hello", encoding="utf-8")

    wrapped = wrap_tools_with_gate([tools.read_file])
    return wrapped[0], target


# ==================== wrap_tools_with_gate ====================

def test_wrapped_tool_allows_readonly(gated_read_file):
    """读 tmp/ 里的文件 —— L0 放行，工具照常执行。"""
    wrapped, target = gated_read_file
    out = wrapped.invoke({"path": str(target)})
    assert "hello" in out


def test_wrapped_tool_keeps_name_and_description(gated_read_file):
    """套了闸门的工具，名字与说明不能变 —— 模型是按名字发起调用的。"""
    wrapped, _ = gated_read_file
    assert wrapped.name == tools.read_file.name
    assert wrapped.description == tools.read_file.description


def test_wrapped_tool_denies_write_to_src(tmp_path, monkeypatch):
    """写受保护区 —— L1 拒绝，文件不能被写出来。"""
    from src.agent import paths

    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(tools, "WORKDIR", tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "src").mkdir()

    wrapped = wrap_tools_with_gate([tools.write_file])[0]
    out = wrapped.invoke({"path": "src/x.py", "content": "boom"})

    assert "拒绝" in out
    assert not (tmp_path / "src" / "x.py").exists()


def test_wrapped_tool_denies_on_escalate(tmp_path, monkeypatch):
    """判官判 escalate —— 这条路径没有人工审批，fail-closed 转成拒绝。

    用 bash 而不是 write_file：写 tmp/ 是 L0 白名单，根本到不了判官。
    `python tmp/x.py` 不是只读命令、又没点名写目标 —— 正好落到 L2。
    """
    from src.agent import authz, paths

    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(tools, "WORKDIR", tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "src").mkdir()

    monkeypatch.setattr(
        authz.judge_module,
        "judge",
        lambda tool_name, tool_input: JudgeResult(
            decision="escalate",
            reason="拿不准",
            question="要执行吗？",
        ),
    )

    wrapped = wrap_tools_with_gate([tools.bash])[0]
    out = wrapped.invoke({"command": "python tmp/evil.py"})

    assert "不支持人工审批" in out
    assert not (tmp_path / "tmp" / "evil.py").exists()


def test_wrapped_tool_result_is_plain_string(gated_read_file):
    """结果必须是普通字符串 —— 抛异常会被 create_react_agent 变成 error tool_result，
    文案不如我们自己写的清楚。"""
    wrapped, target = gated_read_file
    out = wrapped.invoke({"path": str(target)})
    assert isinstance(out, str)


def test_wrapped_tool_never_raises_on_deny(tmp_path, monkeypatch):
    """拒绝不能靠抛异常表达 —— 抛出去就变成「工具崩了」，不是「被拒绝」。"""
    from src.agent import paths

    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(tools, "WORKDIR", tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "src").mkdir()

    wrapped = wrap_tools_with_gate([tools.write_file])[0]
    out = wrapped.invoke({"path": "src/x.py", "content": "boom"})
    assert isinstance(out, str)


# ==================== 记忆子 agent ====================

def test_memory_agent_tool_list_has_no_bash_or_write_file():
    """记忆子 agent 只该拿着记忆工具 —— 原来它拿着含 bash 的全量工具表。"""
    from src.agent.memory.memory_agent import MEMORY_TOOLS

    names = {getattr(t, "name", None) or getattr(t, "__name__", "") for t in MEMORY_TOOLS}
    assert "bash" not in names
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "spawn_subagent" not in names


def test_memory_agent_tool_list_has_the_memory_tools():
    from src.agent.memory.memory_agent import MEMORY_TOOLS

    names = {getattr(t, "name", None) or getattr(t, "__name__", "") for t in MEMORY_TOOLS}
    for expected in (
        "read_file",
        "load_skill",
        "search_memory",
        "search_sessions",
        "search_core_blocks",
        "remember_fact",
        "save_core_block",
        "append_core_block",
        "replace_core_block",
    ):
        assert expected in names


# ==================== REPL 审批 ====================

def test_repl_prompt_returns_choice(monkeypatch):
    """REPL 里问用户 —— 1/2/3 映射到 allow_once / allow_always / deny。"""
    from src.agent.graph_thinking import Verdict, _prompt_cli_decision

    verdict = Verdict("escalate", "L2", "理由", question="要执行吗？")

    for raw, expected in (
        ("1", "allow_once"),
        ("2", "allow_always"),
        ("3", "deny"),
    ):
        monkeypatch.setattr(builtins, "input", lambda _prompt="", _raw=raw: _raw)
        assert _prompt_cli_decision(verdict) == expected


def test_repl_prompt_treats_invalid_input_as_deny(monkeypatch):
    """非法输入按拒绝处理 —— 宁可少做一次，不可误做一次。"""
    from src.agent.graph_thinking import Verdict, _prompt_cli_decision

    monkeypatch.setattr(builtins, "input", lambda _prompt="": "y")
    verdict = Verdict("escalate", "L2", "理由", question="要执行吗？")
    assert _prompt_cli_decision(verdict) == "deny"


def test_repl_prompt_treats_eof_as_deny(monkeypatch):
    """Ctrl-D / Ctrl-C 也按拒绝处理。"""
    from src.agent.graph_thinking import Verdict, _prompt_cli_decision

    def _boom(_prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", _boom)
    verdict = Verdict("escalate", "L2", "理由", question="要执行吗？")
    assert _prompt_cli_decision(verdict) == "deny"
