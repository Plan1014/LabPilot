"""调试输出：把「实际发给模型的那一整套输入」原样呈现出来。"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.agent.prompt_debug import (
    PROMPT_DEBUG_ENV,
    debug_enabled,
    dump_prompt,
    format_meta,
    format_prompt,
)


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
    # 顺序：system 在前，当前请求在最后
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
