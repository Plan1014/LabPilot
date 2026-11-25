"""调试输出：把「实际发给模型的那一整套输入」原样呈现出来。

原则：忠实、完整 —— 模型收到的东西全在这里，终端排版要适合终端。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.agent.prompt_debug import (
    PROMPT_DEBUG_ENV,
    debug_enabled,
    dump_prompt,
    format_meta,
    format_prompt,
    reset_state,
    tools_schema,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_state()
    yield
    reset_state()


class _FakeTool:
    """带完整描述和参数 schema 的假工具。"""

    def __init__(self, name, description="", args_schema=None):
        self.name = name
        self.description = description
        self.args_schema = args_schema


def test_debug_enabled_reads_env(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(PROMPT_DEBUG_ENV, value)
        assert debug_enabled() is True


def test_debug_enabled_is_off_by_default(monkeypatch):
    monkeypatch.delenv(PROMPT_DEBUG_ENV, raising=False)
    assert debug_enabled() is False


def test_debug_enabled_is_off_for_zero(monkeypatch):
    monkeypatch.setenv(PROMPT_DEBUG_ENV, "0")
    assert debug_enabled() is False


def test_format_prompt_has_no_box_borders():
    """终端里用不着方框 —— 右边框对不齐，中文宽度还乱。"""
    text = format_prompt([HumanMessage(content="你好")])
    for box_char in "┌┐└┘├┤│":
        assert box_char not in text


def test_format_prompt_shows_every_message_in_order():
    messages = [
        SystemMessage(content="你是实验助手"),
        SystemMessage(content="<CoreMemory>核心记忆</CoreMemory>"),
        HumanMessage(content="<CurrentRequest>帮我备份</CurrentRequest>"),
        AIMessage(content="好的"),
    ]
    text = format_prompt(messages, {"session_id": "s1", "query": "帮我备份"})

    assert "你是实验助手" in text
    assert "<CoreMemory>核心记忆</CoreMemory>" in text
    assert "<CurrentRequest>帮我备份</CurrentRequest>" in text
    assert "好的" in text
    assert text.index("你是实验助手") < text.index("<CurrentRequest>")
    assert text.index("<CurrentRequest>") < text.index("好的")


def test_format_prompt_shows_role_and_index():
    text = format_prompt([SystemMessage(content="x"), HumanMessage(content="y")])
    assert "[1] system" in text
    assert "[2] user" in text


def test_format_prompt_shows_meta_line():
    text = format_prompt(
        [HumanMessage(content="x")],
        {"session_id": "abc", "query": "问题", "threshold": 0.45,
         "tool_count": 13, "retrieved_facts": 2, "history_rows": 5},
    )
    assert "session=abc" in text
    assert "问题" in text
    assert "0.45" in text
    assert "13" in text
    assert "5" in text


def test_format_prompt_counts_characters():
    text = format_prompt([HumanMessage(content="12345")])
    assert "5 字符" in text


def test_format_prompt_renders_block_content_as_text():
    """Anthropic 模式的 content 是 block 列表 —— 不能显示成 Python repr。"""
    text = format_prompt([HumanMessage(content=[{"type": "text", "text": ["你好"]}])])
    assert "你好" in text
    assert "[{'type'" not in text


def test_dump_prompt_prints_to_stdout(capsys):
    dump_prompt([HumanMessage(content="打到终端的内容")], {"query": "q"})
    out = capsys.readouterr().out
    assert "打到终端的内容" in out
    assert "PROMPT" in out


def test_format_meta_skips_missing_values():
    assert format_meta({}) == []
    assert format_meta({"session_id": "s"}) == ["session=s"]


def test_tool_use_message_is_not_blank():
    """只带 tool_use 的助手消息 content 是空的 —— 调试时要看得见它调了什么。"""
    msg = AIMessage(content="", tool_calls=[
        {"name": "bash", "args": {"command": "ls"}, "id": "call_1", "type": "tool_call"},
    ])
    text = format_prompt([msg])
    assert "tool_use: bash" in text


# --- 工具的完整定义 ---

def test_tools_schema_returns_openai_format():
    from langchain_core.tools import tool

    @tool
    def sample(query: str, limit: int = 3) -> str:
        """搜索一下"""
        return "x"

    schemas = tools_schema([sample])
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "sample"
    assert fn["description"]
    assert "query" in fn["parameters"]["properties"]


def test_format_prompt_prints_full_tool_definitions():
    """工具的完整定义也是「送给模型的输入」—— 必须打印出来。"""
    from langchain_core.tools import tool

    @tool
    def sample(query: str) -> str:
        """搜索实验记录用的工具"""
        return "x"

    text = format_prompt([HumanMessage(content="hi")], tools=[sample])

    assert "TOOLS (1)" in text
    assert "sample" in text
    assert "搜索实验记录用的工具" in text
    assert "query" in text


def test_format_prompt_marks_that_tools_are_not_in_the_prompt_text():
    from langchain_core.tools import tool

    @tool
    def sample(query: str) -> str:
        """工具说明"""
        return "x"

    text = format_prompt([HumanMessage(content="hi")], tools=[sample])
    assert "tools" in text.lower()


# --- 增量打印 ---

def test_second_dump_prints_only_the_new_messages(capsys):
    first = [
        SystemMessage(content="系统提示词"),
        HumanMessage(content="<CurrentRequest>第一轮</CurrentRequest>"),
    ]
    second = first + [
        AIMessage(content="第一轮的回复"),
        HumanMessage(content="<CurrentRequest>第二轮</CurrentRequest>"),
    ]

    dump_prompt(first, {"session_id": "s"}, state_key="s")
    capsys.readouterr()
    dump_prompt(second, {"session_id": "s"}, state_key="s")
    out = capsys.readouterr().out

    assert "第二轮" in out
    assert "第一轮的回复" in out
    assert "<CurrentRequest>第一轮</CurrentRequest>" not in out


def test_second_dump_marks_unchanged_blocks(capsys):
    msgs = [
        SystemMessage(content="系统提示词正文"),
        SystemMessage(content="<CoreMemory>核心</CoreMemory>"),
        HumanMessage(content="<CurrentRequest>问题</CurrentRequest>"),
    ]
    dump_prompt(msgs, {"session_id": "s"}, state_key="s")
    capsys.readouterr()
    dump_prompt(msgs + [HumanMessage(content="<CurrentRequest>下一个问题</CurrentRequest>")],
                {"session_id": "s"}, state_key="s")
    out = capsys.readouterr().out

    assert "下一个问题" in out
    assert "系统提示词正文" not in out
    assert "未变" in out


def test_full_dump_prints_everything_again(capsys):
    msgs = [SystemMessage(content="系统提示词"), HumanMessage(content="问题")]
    dump_prompt(msgs, {"session_id": "s"}, state_key="s")
    capsys.readouterr()
    dump_prompt(msgs, {"session_id": "s"}, state_key="s", full=True)
    out = capsys.readouterr().out

    assert "系统提示词" in out
    assert "问题" in out


def test_delta_state_is_per_key(capsys):
    msgs = [SystemMessage(content="系统提示词"), HumanMessage(content="问题")]
    dump_prompt(msgs, {"session_id": "a"}, state_key="a")
    capsys.readouterr()
    dump_prompt(msgs, {"session_id": "b"}, state_key="b")
    out = capsys.readouterr().out

    assert "系统提示词" in out, "另一个 key 应当重新打印完整的一份"
