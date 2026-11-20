import pytest
from langchain_core.messages import AIMessage

pytestmark = pytest.mark.slow


def _fake_llm(tool_call):
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("提问之后不应该再调用 LLM")
            return AIMessage(content="", tool_calls=[tool_call])

    return FakeLLM()


def test_graph_stops_after_ask_user(monkeypatch):
    from src.agent import graph_thinking

    fake = _fake_llm({
        "id": "call_1", "name": "ask_user", "type": "tool_call",
        "args": {"question": "用哪套？", "options": [{"label": "A", "recommended": True}]},
    })
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0})

    assert fake.calls == 1
    assert result["pending_ask"]["question"] == "用哪套？"
    assert result["pending_ask"]["tool_call_id"] == "call_1"
    assert result["pending_tool_calls"] == []


def test_sibling_tool_calls_are_dropped(monkeypatch):
    """模型偶尔「一边提问一边调工具」—— 兄弟 tool_use 必须丢掉。

    留着它下一轮上游会因 tool_use 没有对应 tool_result 而报 400。
    """
    from src.agent import graph_thinking

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("提问之后不应该再调用 LLM")
            return AIMessage(content="", tool_calls=[
                {"id": "b1", "name": "bash", "type": "tool_call",
                 "args": {"command": "ls"}},
                {"id": "call_1", "name": "ask_user", "type": "tool_call",
                 "args": {"question": "Q", "options": []}},
            ])

    fake = FakeLLM()
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0})

    names = [
        tc["name"]
        for m in result["messages"]
        if isinstance(m, AIMessage)
        for tc in (getattr(m, "tool_calls", None) or [])
    ]
    assert names == ["ask_user"]
