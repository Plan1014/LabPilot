"""Server 模式（LangGraph Studio）的系统提示词不能是 import 那一刻的快照。

graph.py 在模块导入时构造系统提示词，里面的 "Current time" 从此永远不变。
"""

import datetime as dt
import importlib

from langchain_core.messages import HumanMessage, SystemMessage


def _server_graph_module():
    # src.agent.__init__ 把 graph 导出成编译后的图对象，模块要用 importlib 取。
    return importlib.import_module("src.agent.graph")


def test_server_mode_prompt_time_is_computed_at_call_time(monkeypatch):
    server_graph = _server_graph_module()
    from src.agent import prompt_context

    class FakeDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 23, 12, 0, 0)

    monkeypatch.setattr(prompt_context, "datetime", FakeDateTime)

    out = server_graph.dynamic_system_prompt({"messages": [HumanMessage(content="hi")]})

    assert isinstance(out[0], SystemMessage)
    assert "2026-11-23 12:00:00 UTC" in out[0].content
    assert isinstance(out[1], HumanMessage)
    assert out[1].content == "hi"


def test_server_mode_prompt_lists_tools_dynamically():
    server_graph = _server_graph_module()

    out = server_graph.dynamic_system_prompt({"messages": []})
    assert "<AvailableTools>" in out[0].content
    assert "bash" in out[0].content
