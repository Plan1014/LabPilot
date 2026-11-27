"""提权回路（服务端）的测试。

覆盖：`/auth/pending`、`/auth/grants`、`/auth/respond`、`/debug/authz`，
以及「用户有待批记录时直接敲了文字 → 视为 deny」。
"""

import pytest
from fastapi.testclient import TestClient

from src.agent import session_manager
from src.agent.authz import ledger
from src.agent.authz import pending as authz_pending


@pytest.fixture
def client():
    from src.agent.websocket_server import create_notification_hub_app

    return TestClient(create_notification_hub_app())


@pytest.fixture
def db(tmp_path, monkeypatch):
    """会话库指向临时文件 —— 测试不该往用户的 tasks.db 里写。"""
    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)
    ledger.reset_session_grants()
    yield
    ledger.reset_session_grants()
    monkeypatch.setattr(session_manager, "_sql_conn", None)


@pytest.fixture
def fake_graph(monkeypatch):
    """把图换成假的 —— 这层测试不调模型。"""
    from src.agent import graph_thinking

    class FakeGraph:
        def __init__(self):
            self.states = []

        async def astream_events(self, state, config=None, stream_mode=None):
            self.states.append(state)
            if False:  # 让它是个 async generator
                yield {}

    g = FakeGraph()
    monkeypatch.setattr(graph_thinking, "build_graph", lambda: g)
    return g


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """工具的 WORKDIR 指向临时目录 —— read_file 读得到测试文件。"""
    from src.agent import tools

    (tmp_path / "hello.txt").write_text("你好，世界", encoding="utf-8")
    monkeypatch.setattr(tools, "WORKDIR", tmp_path)
    return tmp_path


def _make_batch(session_id="s1", *, verdict="escalate"):
    """造一个只有一条待批调用的批次。"""
    calls = [
        {
            "tool_call_id": "c1",
            "tool_name": "read_file",
            "tool_input": {"path": "hello.txt"},
            "verdict": verdict,
            "reason": "不确定",
            "pattern": "read_file:hello.txt",
        }
    ]
    pending = [
        {
            "tool_call_id": "c1",
            "tool_name": "read_file",
            "tool_input": {"path": "hello.txt"},
            "question": "要读取 hello.txt 吗？",
            "reason": "不确定",
            "pattern": "read_file:hello.txt",
        }
    ]
    return authz_pending.save_batch(session_id, calls, pending)


def _sse_events(text):
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            import json

            out.append(json.loads(line[6:]))
    return out


def _history(session_id):
    return session_manager.get_session_history(session_id)


# ==================== /auth/pending ====================

def test_pending_endpoint_lists_pending_calls(client, db):
    _make_batch("s1")
    r = client.get("/auth/pending", params={"session_id": "s1"})
    assert r.status_code == 200
    pending = r.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["tool_call_id"] == "c1"
    assert pending[0]["tool_name"] == "read_file"
    assert pending[0]["status"] == "pending"


def test_pending_endpoint_is_empty_for_other_session(client, db):
    _make_batch("s1")
    r = client.get("/auth/pending", params={"session_id": "s2"})
    assert r.json()["pending"] == []


# ==================== /auth/grants ====================

def test_grants_endpoint_lists_grants(client, db):
    ledger.record_grant("bash:python tmp/exp1/run.py", session_id="s1")
    r = client.get("/auth/grants", params={"session_id": "s1"})
    assert r.status_code == 200
    grants = r.json()["grants"]
    assert any(g["pattern"] == "bash:python tmp/exp1/run.py" for g in grants)


def test_revoke_grant_removes_it(client, db):
    ledger.record_grant("bash:python tmp/exp1/run.py", session_id="s1")
    grants = client.get("/auth/grants", params={"session_id": "s1"}).json()["grants"]
    gid = grants[0]["id"]
    r = client.delete(f"/auth/grants/{gid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    after = client.get("/auth/grants", params={"session_id": "s1"}).json()["grants"]
    assert after == []


# ==================== /auth/respond ====================

def test_respond_rejects_invalid_choice(client, db):
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "maybe"},
    )
    assert r.status_code == 400


def test_respond_with_unknown_tool_call_id_is_404(client, db):
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "nope", "choice": "allow_once"},
    )
    assert r.status_code == 404


def test_respond_rejects_a_pending_call_belonging_to_another_session(client, db):
    """I3：待批记录挂在别的会话下 → 不能替它做裁决。

    不校验会话归属的话，任意会话都能放行别人的操作 —— 提权回路就漏了。
    """
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s2", "tool_call_id": "c1", "choice": "allow_once"},
    )
    assert r.status_code == 403
    # 原记录没被动过，也没有给 s2 留下授权
    assert authz_pending.get_pending_by_tool_call_id("c1")["status"] == "pending"
    assert ledger.list_grants(session_id="s2") == []


def test_respond_records_decision(client, db):
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_once"},
    )
    assert r.status_code == 200
    row = authz_pending.get_pending_by_tool_call_id("c1")
    assert row["status"] == "allow_once"


def test_batch_resolves_only_when_all_decided(client, db):
    """两条待批 —— 裁决一条不算解决。"""
    calls = [
        {"tool_call_id": "c1", "tool_name": "read_file", "tool_input": {"path": "a.txt"},
         "verdict": "escalate", "reason": "r", "pattern": None},
        {"tool_call_id": "c2", "tool_name": "read_file", "tool_input": {"path": "b.txt"},
         "verdict": "escalate", "reason": "r", "pattern": None},
    ]
    pending = [
        {"tool_call_id": "c1", "tool_name": "read_file", "tool_input": {"path": "a.txt"},
         "question": "q1", "reason": "r", "pattern": None},
        {"tool_call_id": "c2", "tool_name": "read_file", "tool_input": {"path": "b.txt"},
         "question": "q2", "reason": "r", "pattern": None},
    ]
    batch_id = authz_pending.save_batch("s1", calls, pending)

    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_once"},
    )
    assert r.status_code == 200
    assert authz_pending.batch_is_resolved(batch_id) is False
    # 批次没解决 → 只回一个 done，不重跑图
    assert [e["type"] for e in _sse_events(r.text)] == ["done"]


def test_allow_always_records_grant(client, db):
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_always"},
    )
    assert r.status_code == 200
    patterns = [g["pattern"] for g in ledger.list_grants(session_id="s1")]
    assert "read_file:hello.txt" in patterns


def test_allow_once_does_not_record_grant(client, db):
    _make_batch("s1")
    client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_once"},
    )
    assert ledger.list_grants(session_id="s1") == []


def test_deny_produces_denial_tool_result(client, db, fake_graph):
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "deny"},
    )
    assert r.status_code == 200
    rows = [m for m in _history("s1") if m.get("role") == "tool"]
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == "c1"
    assert rows[0]["content"].startswith("授权拒绝：")


def test_allow_produces_tool_output(client, db, fake_graph, workdir):
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_once"},
    )
    assert r.status_code == 200
    rows = [m for m in _history("s1") if m.get("role") == "tool"]
    assert len(rows) == 1
    assert "你好，世界" in rows[0]["content"]


def test_every_tool_use_gets_a_tool_result(client, db, fake_graph, workdir):
    """批里每个 tool_use 都得有配对的 tool_result —— 少一条上游就报 400。"""
    calls = [
        {"tool_call_id": "c1", "tool_name": "read_file", "tool_input": {"path": "hello.txt"},
         "verdict": "escalate", "reason": "r", "pattern": None},
        {"tool_call_id": "c2", "tool_name": "read_file", "tool_input": {"path": "hello.txt"},
         "verdict": "allow", "reason": "ok", "pattern": None},
    ]
    pending = [
        {"tool_call_id": "c1", "tool_name": "read_file", "tool_input": {"path": "hello.txt"},
         "question": "q", "reason": "r", "pattern": None},
    ]
    authz_pending.save_batch("s1", calls, pending)

    client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_once"},
    )

    rows = [m for m in _history("s1") if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in rows] == ["c1", "c2"]


def test_respond_resumes_the_graph_with_the_tool_result(client, db, fake_graph, workdir):
    """裁决完 → 图要带着 tool_result 继续跑（模型从 tool_use 断点接上）。"""
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_once"},
    )
    assert r.status_code == 200
    assert len(fake_graph.states) == 1
    msgs = fake_graph.states[0]["messages"]
    tool_msgs = [m for m in msgs if getattr(m, "type", "") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert "你好，世界" in tool_msgs[0].content


def test_respond_emits_tool_result_event_with_the_decision(client, db, fake_graph):
    """前端靠这条事件把待批卡片标成已裁决 —— 必须带 tool_call_id 和选择。"""
    _make_batch("s1")
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "deny"},
    )
    assert r.status_code == 200
    events = _sse_events(r.text)
    results = [e for e in events if e.get("type") == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "c1"
    assert results[0]["auth_choice"] == "deny"


# ==================== /debug/authz ====================

def test_debug_authz_endpoint_returns_recent_verdicts(client, db, monkeypatch):
    from src.agent.authz import audit, authorize

    audit.reset_recent()
    authorize("read_file", {"path": "src/x.py"}, session_id="s1")

    r = client.get("/debug/authz", params={"n": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["verdicts"]
    assert data["verdicts"][-1]["tool"] == "read_file"
    assert "log_path" in data


# ==================== 待批时敲字 ====================

def test_escalation_reaches_the_frontend_as_auth_request(client, db, monkeypatch):
    """判官说「拿不准」→ 前端要收到一张待批卡片。"""
    from langchain_core.messages import AIMessage

    from src.agent import graph_thinking
    from src.agent import memory as memory_mod
    from src.agent.authz import judge as judge_module
    from src.agent.prompt_context import RetrievedMemory

    class _StubRetriever:
        def retrieve(self, query, threshold=None):
            return RetrievedMemory(facts=[], summaries=[])

    monkeypatch.setattr(memory_mod, "memory_retriever", _StubRetriever())

    class FakeJudge:
        def __call__(self, tool_name, tool_input):
            return judge_module.JudgeResult("escalate", "不确定", "要执行吗？")

    monkeypatch.setattr(judge_module, "judge", FakeJudge())

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[
                    {"id": "c1", "name": "bash", "type": "tool_call",
                     "args": {"command": "python tmp/x.py"}},
                ])
            return AIMessage(content="好了")

    monkeypatch.setattr(graph_thinking, "llm", FakeLLM())

    r = client.post("/session/query", json={"session_id": "s1", "query": "跑一下"})
    assert r.status_code == 200
    events = _sse_events(r.text)
    reqs = [e for e in events if e.get("type") == "auth_request"]
    assert len(reqs) == 1
    assert reqs[0]["tool_call_id"] == "c1"
    assert reqs[0]["question"] == "要执行吗？"
    # 待批记录落库
    assert len(authz_pending.list_pending("s1")) == 1


def test_typed_text_while_pending_is_treated_as_deny(client, db, fake_graph, monkeypatch):
    """有待批记录时直接敲字 → 最早的待批项判 deny（spec §5.4）。"""
    from src.agent import memory as memory_mod
    from src.agent.prompt_context import RetrievedMemory

    class _StubRetriever:
        def retrieve(self, query, threshold=None):
            return RetrievedMemory(facts=[], summaries=[])

    monkeypatch.setattr(memory_mod, "memory_retriever", _StubRetriever())

    _make_batch("s1")
    r = client.post("/session/query", json={"session_id": "s1", "query": "算了，不用了"})
    assert r.status_code == 200
    row = authz_pending.get_pending_by_tool_call_id("c1")
    assert row["status"] == "deny"
    tool_rows = [m for m in _history("s1") if m.get("role") == "tool"]
    assert tool_rows
    assert tool_rows[0]["content"].startswith("授权拒绝：")


def test_query_route_escalation_becomes_a_card_not_a_server_terminal_prompt(
    client, db, monkeypatch
):
    """C3：`POST /query` 这条路没有会话 → escalate 会去问**服务端终端**。

    浏览器里的用户永远等不到卡片，问的却是守着服务端控制台的人。
    现在 `/query` 也拿到会话 → 卡片照常发给客户端，服务端终端不该被问。
    """
    import builtins

    from langchain_core.messages import AIMessage

    from src.agent import graph_thinking
    from src.agent import memory as memory_mod
    from src.agent.authz import judge as judge_module
    from src.agent.prompt_context import RetrievedMemory

    class _StubRetriever:
        def retrieve(self, query, threshold=None):
            return RetrievedMemory(facts=[], summaries=[])

    monkeypatch.setattr(memory_mod, "memory_retriever", _StubRetriever())

    class FakeJudge:
        def __call__(self, tool_name, tool_input):
            return judge_module.JudgeResult("escalate", "不确定", "要执行吗？")

    monkeypatch.setattr(judge_module, "judge", FakeJudge())

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[
                    {"id": "c1", "name": "bash", "type": "tool_call",
                     "args": {"command": "python tmp/x.py"}},
                ])
            return AIMessage(content="好了")

    monkeypatch.setattr(graph_thinking, "llm", FakeLLM())

    def _no_stdin(prompt=""):
        raise AssertionError("escalate 不该去问服务端终端")

    monkeypatch.setattr(builtins, "input", _no_stdin)

    r = client.post("/query", json={"query": "跑一下"})
    assert r.status_code == 200
    events = _sse_events(r.text)
    reqs = [e for e in events if e.get("type") == "auth_request"]
    assert len(reqs) == 1
    assert reqs[0]["tool_call_id"] == "c1"
    # 客户端拿得到会话号 —— 它要靠这个号去 /auth/respond 回卡
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["session_id"]
    # 待批记录落库，且挂在真实会话下（不是 NULL）
    assert len(authz_pending.list_pending(done[0]["session_id"])) == 1


# ==================== 多 escalate 批次（评审 C4）====================
# 一轮里两个调用都要人裁决时，两张卡片都得出来。
# 后端的事件去重键是 `type:step:name:content:result:input` —— auth_request
# 事件里这些字段全空、step 恒为 0，于是同一批的两个事件键完全相同，
# 第二个在进 SSE 队列之前就被丢了。用户看不到第二张卡，批次也收敛不了。

def test_two_escalations_in_one_turn_produce_two_auth_request_events(
    client, db, monkeypatch
):
    from langchain_core.messages import AIMessage

    from src.agent import graph_thinking
    from src.agent import memory as memory_mod
    from src.agent.authz import judge as judge_module
    from src.agent.prompt_context import RetrievedMemory

    class _StubRetriever:
        def retrieve(self, query, threshold=None):
            return RetrievedMemory(facts=[], summaries=[])

    monkeypatch.setattr(memory_mod, "memory_retriever", _StubRetriever())

    class FakeJudge:
        def __call__(self, tool_name, tool_input):
            return judge_module.JudgeResult("escalate", "不确定", "要执行吗？")

    monkeypatch.setattr(judge_module, "judge", FakeJudge())

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[
                    {"id": "c1", "name": "bash", "type": "tool_call",
                     "args": {"command": "python tmp/x.py"}},
                    {"id": "c2", "name": "bash", "type": "tool_call",
                     "args": {"command": "python tmp/y.py"}},
                ])
            return AIMessage(content="好了")

    monkeypatch.setattr(graph_thinking, "llm", FakeLLM())

    r = client.post("/session/query", json={"session_id": "s1", "query": "跑两个"})
    assert r.status_code == 200
    reqs = [e for e in _sse_events(r.text) if e.get("type") == "auth_request"]
    assert len(reqs) == 2
    assert {e["tool_call_id"] for e in reqs} == {"c1", "c2"}
    # 两条待批记录都落库 —— 卡片少了批次就收敛不了
    assert len(authz_pending.list_pending("s1")) == 2


def test_auth_request_events_are_not_deduped_by_step():
    from src.agent.websocket_server import _event_dedup_key

    a = {"type": "auth_request", "tool_call_id": "c1", "question": "第一个", "step": 0}
    b = {"type": "auth_request", "tool_call_id": "c2", "question": "第二个", "step": 0}
    assert _event_dedup_key(a) != _event_dedup_key(b)


def test_identical_tool_events_are_still_deduped():
    from src.agent.websocket_server import _event_dedup_key

    a = {"type": "tool_call", "step": 1, "name": "bash", "input": {"command": "ls"}}
    b = {"type": "tool_call", "step": 1, "name": "bash", "input": {"command": "ls"}}
    assert _event_dedup_key(a) == _event_dedup_key(b)
