"""调试接口：能拿到「这一轮会发什么」，并且 PROMPT_DEBUG=1 时自动打到终端。"""

import pytest
from fastapi.testclient import TestClient

from src.agent.prompt_debug import PROMPT_DEBUG_ENV


@pytest.fixture
def client():
    from src.agent.websocket_server import create_notification_hub_app
    return TestClient(create_notification_hub_app())


@pytest.fixture
def stub_memory(monkeypatch):
    """把检索换成假的 —— 调试接口不该真的去查向量库。"""
    import src.agent.memory as memory_mod
    from src.agent.prompt_context import FactEntry, RetrievedMemory

    class _StubRetriever:
        def retrieve(self, query, threshold=None):
            return RetrievedMemory(facts=[FactEntry(text="注入的事实", distance=0.3)])

        def retrieve_context(self, query):
            return "注入的事实"

    monkeypatch.setattr(memory_mod, "memory_retriever", _StubRetriever())


@pytest.fixture
def isolated_session_store(tmp_path, monkeypatch):
    """会话库指向临时文件 —— 测试不该往用户的 tasks.db 里写。"""
    import src.agent.session_manager as session_manager

    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)
    yield


def test_post_debug_prompt_returns_assembled_messages(client, stub_memory):
    r = client.post("/debug/prompt", json={"query": "帮我备份实验数据"})
    assert r.status_code == 200
    data = r.json()

    assert data["messages"], "应当返回拼装好的消息列表"
    assert data["text"], "应当返回可直接复制的终端文本"
    last = data["messages"][-1]
    assert "<CurrentRequest>帮我备份实验数据</CurrentRequest>" in last["content"]
    assert data["meta"]["query"] == "帮我备份实验数据"
    assert data["meta"]["threshold"] is not None


def test_post_debug_prompt_includes_core_and_retrieved(client, stub_memory):
    data = client.post("/debug/prompt", json={"query": "帮我备份实验数据"}).json()
    body = "\n".join(m["content"] for m in data["messages"])
    assert "<RetrievedMemory>" in body
    assert "注入的事实" in body


def test_get_session_prompt_returns_session_history(client, stub_memory):
    r = client.get("/session/unknown-session-id/prompt")
    assert r.status_code == 200
    data = r.json()
    assert data["meta"]["session_id"] == "unknown-session-id"
    # 未知会话没有历史 —— 至少还有系统提示词
    assert data["messages"][0]["content"]


def test_debug_endpoint_does_not_call_the_model(client, stub_memory, monkeypatch):
    """调试接口只拼装，不调模型。"""
    import src.agent.graph_thinking as graph_thinking

    def _boom(*args, **kwargs):
        raise AssertionError("调试接口不该调模型")

    monkeypatch.setattr(graph_thinking, "build_graph", _boom)
    r = client.post("/debug/prompt", json={"query": "只拼不调"})
    assert r.status_code == 200


def test_prompt_is_printed_to_terminal_when_debug_enabled(client, stub_memory, monkeypatch, capsys):
    monkeypatch.setenv(PROMPT_DEBUG_ENV, "1")
    client.post("/debug/prompt", json={"query": "打到终端的问题"})
    out = capsys.readouterr().out
    assert "打到终端的问题" in out


def test_prompt_is_not_printed_when_debug_disabled(client, stub_memory, monkeypatch, capsys):
    monkeypatch.delenv(PROMPT_DEBUG_ENV, raising=False)
    client.post("/debug/prompt", json={"query": "不该出现的问题"})
    out = capsys.readouterr().out
    assert "不该出现的问题" not in out


def test_session_query_prints_prompt_to_terminal(
    client, stub_memory, monkeypatch, capsys, isolated_session_store
):
    """PROMPT_DEBUG=1 时，正常问一轮也会把完整输入打到后端终端。"""
    import src.agent.graph_thinking as graph_thinking

    class _FakeGraph:
        async def astream_events(self, initial_state, config=None, stream_mode=None):
            yield {"event": "on_chain_end", "data": {}}

    monkeypatch.setattr(graph_thinking, "build_graph", lambda: _FakeGraph())
    monkeypatch.setenv(PROMPT_DEBUG_ENV, "1")

    r = client.post("/session/query", json={"query": "会话里的问题", "session_id": "dbg-1"})
    assert r.status_code == 200

    out = capsys.readouterr().out
    assert "<CurrentRequest>会话里的问题</CurrentRequest>" in out
    assert "PROMPT" in out
