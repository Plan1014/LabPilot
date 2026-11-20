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


def test_response_tool_call_id_collision_is_renamed(monkeypatch):
    """上游每轮都从 call_0 重新编号时，新提问的 id 不能撞上历史里的旧 id。

    撞上了，回答会配到旧的提问上 —— 用户根本不会被问到，提问静默消失。
    """
    from langchain_core.messages import HumanMessage, ToolMessage

    from src.agent import graph_thinking

    fake = _fake_llm({
        "id": "call_0", "name": "ask_user", "type": "tool_call",
        "args": {"question": "第二个问题？", "options": []},
    })
    monkeypatch.setattr(graph_thinking, "llm", fake)

    history = [
        HumanMessage(content="先问一次"),
        AIMessage(content="", tool_calls=[
            {"id": "call_0", "name": "ask_user", "args": {"question": "旧问题？"}},
        ]),
        ToolMessage(content="旧回答", tool_call_id="call_0", name="ask_user"),
    ]

    graph = graph_thinking.build_graph()
    result = graph.invoke({
        "messages": history, "pending_tool_calls": [], "step_count": 0,
    })

    new_id = result["pending_ask"]["tool_call_id"]
    assert new_id != "call_0"
    # 落库的 assistant 消息必须带上同一个新 id，否则回答配不上
    last = result["messages"][-1]
    assert [tc["id"] for tc in last.tool_calls] == [new_id]
