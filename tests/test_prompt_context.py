from datetime import datetime, timezone

from src.agent.prompt_context import (
    build_system_prompt,
    label_tool_result,
    label_user_answer,
    label_user_message,
    relevance_threshold,
    tool_name_index,
)


class _FakeTool:
    def __init__(self, name):
        self.name = name


def test_tool_name_index_lists_names_in_order():
    tools = [_FakeTool("bash"), _FakeTool("read_file"), _FakeTool("ask_user")]
    assert tool_name_index(tools) == "bash, read_file, ask_user"


def test_tool_name_index_skips_nameless_tools():
    assert tool_name_index([_FakeTool("bash"), object()]) == "bash"


def test_system_prompt_lists_tools_dynamically():
    prompt = build_system_prompt([_FakeTool("bash"), _FakeTool("brand_new_tool")], "- a")
    assert "brand_new_tool" in prompt


def test_system_prompt_explains_message_sources():
    prompt = build_system_prompt([_FakeTool("bash")], "- a")
    for tag in ("<UserMessage>", "<UserAnswer>", "<CoreMemory>",
                "<RetrievedMemory>", "<ToolResult>"):
        assert tag in prompt


def test_system_prompt_contains_current_time():
    now = datetime(2026, 11, 23, 16, 20, 0, tzinfo=timezone.utc)
    prompt = build_system_prompt([_FakeTool("bash")], "- a", now=now)
    assert "2026-11-23" in prompt


def test_labels_wrap_content():
    assert label_user_message("hi") == "<UserMessage>hi</UserMessage>"
    assert label_user_answer("用本地镜像备份", "ask_user") == \
        '<UserAnswer name="ask_user">用本地镜像备份</UserAnswer>'
    assert label_tool_result("ok", "bash") == '<ToolResult name="bash">ok</ToolResult>'


def test_relevance_threshold_default_is_045(monkeypatch):
    monkeypatch.delenv("MEMORY_RELEVANCE_THRESHOLD", raising=False)
    assert relevance_threshold() == 0.45


def test_relevance_threshold_reads_env(monkeypatch):
    monkeypatch.setenv("MEMORY_RELEVANCE_THRESHOLD", "0.6")
    assert relevance_threshold() == 0.6


def test_relevance_threshold_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("MEMORY_RELEVANCE_THRESHOLD", "abc")
    assert relevance_threshold() == 0.45
