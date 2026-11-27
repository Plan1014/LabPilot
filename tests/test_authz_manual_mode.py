"""手动模式 —— 命令与写操作交给用户裁决。

auto：L1 拒 → L0 放 → L2 判官 →（拿不准）转人工
manual：L1 拒 → L0 只放行「只读 + 固定路径记忆写入」→ 其余全部转人工（判官不下场）

L1 在手动模式下照旧硬拒 —— 「改项目主体文件一律拒绝」是底线，不随模式变。
"""

import pytest

from src.agent import paths, session_manager
from src.agent.authz import authorize, ledger
from src.agent.authz import judge as judge_module


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    ledger.reset_session_grants()
    yield
    ledger.reset_session_grants()
    monkeypatch.setattr(session_manager, "_sql_conn", None)


def _no_judge(*a, **k):
    """判官不该被调用 —— 手动模式下没有它什么事。"""
    raise AssertionError("手动模式不该调判官")


@pytest.fixture
def no_judge(monkeypatch):
    monkeypatch.setattr(judge_module, "judge", _no_judge)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from src.agent.websocket_server import create_notification_hub_app

    return TestClient(create_notification_hub_app())


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


# ==================== 仍然放行的 ====================

def test_manual_mode_allows_read_only_tools(db):
    v = authorize("read_file", {"path": "src/x.py"}, mode="manual")
    assert v.decision == "allow"
    assert v.layer == "L0"


def test_manual_mode_allows_load_skill(db):
    v = authorize("load_skill", {"name": "pdh-locking"}, mode="manual")
    assert v.decision == "allow"


def test_manual_mode_allows_read_only_bash(db):
    v = authorize("bash", {"command": "ls tmp/"}, mode="manual")
    assert v.decision == "allow"
    assert v.layer == "L0"


def test_manual_mode_honors_a_grant_the_user_remembered(db):
    """用户自己点过「允许并记住」—— 手动模式也得认。"""
    ledger.record_grant("write_file:tmp/exp1/**", session_id="s1")
    v = authorize(
        "write_file", {"path": "tmp/exp1/a.txt", "content": "x"},
        session_id="s1", mode="manual",
    )
    assert v.decision == "allow"
    assert v.matched_rule == "ledger"


# ==================== 转人工的 ====================

def test_manual_mode_escalates_write_file_even_in_the_writable_zone(db):
    """可写区内的写也要人批 —— 手动模式的意义就在这儿。"""
    v = authorize("write_file", {"path": "tmp/a.txt", "content": "x"}, mode="manual")
    assert v.decision == "escalate"
    assert v.layer == "L3"


def test_manual_mode_escalates_a_non_read_only_bash(db):
    v = authorize("bash", {"command": "python tmp/x.py"}, mode="manual")
    assert v.decision == "escalate"
    assert v.layer == "L3"


def test_manual_mode_escalates_curl(db):
    """curl 也要人批 —— 即便它指向 skills 里的回环接口。"""
    v = authorize("bash", {"command": "curl http://127.0.0.1:8001/status"}, mode="manual")
    assert v.decision == "escalate"


def test_manual_mode_does_not_call_the_judge(db, no_judge):
    authorize("bash", {"command": "python tmp/x.py"}, mode="manual")


def test_manual_mode_question_shows_the_command(db):
    """卡片上要看得见要执行的是什么。"""
    v = authorize("bash", {"command": "python tmp/x.py"}, mode="manual")
    assert "python tmp/x.py" in v.question


def test_manual_mode_question_shows_the_target_path(db):
    v = authorize("write_file", {"path": "tmp/a.txt", "content": "x"}, mode="manual")
    assert "tmp/a.txt" in v.question


def test_manual_mode_carries_a_rememberable_pattern(db):
    """「允许并记住」要能点 —— 模式照常推导。"""
    v = authorize(
        "write_file", {"path": "tmp/exp1/a.txt", "content": "x"}, mode="manual"
    )
    assert v.pattern == "write_file:tmp/exp1/**"


# ==================== 底线不随模式变 ====================

def test_manual_mode_still_denies_writes_to_protected_dirs(db):
    v = authorize("write_file", {"path": "src/x.py", "content": "x"}, mode="manual")
    assert v.decision == "deny"
    assert v.layer == "L1"


def test_manual_mode_still_denies_git_rewriting(db):
    v = authorize("bash", {"command": "git checkout ."}, mode="manual")
    assert v.decision == "deny"
    assert v.layer == "L1"


# ==================== auto 模式不变 ====================

def test_auto_mode_still_uses_the_judge(db, monkeypatch):
    seen = []

    def _fake(tool_name, tool_input):
        seen.append((tool_name, tool_input))
        return judge_module.JudgeResult("allow", "只读", "")

    monkeypatch.setattr(judge_module, "judge", _fake)
    v = authorize("bash", {"command": "python tmp/x.py"})
    assert v.decision == "allow"
    assert v.layer == "L2"
    assert seen


def test_an_unknown_mode_falls_back_to_auto(db, monkeypatch):
    """脏值不能把闸门关掉 —— 认不出来就按 auto 走。"""
    monkeypatch.setattr(
        judge_module, "judge",
        lambda *a, **k: judge_module.JudgeResult("allow", "只读", ""),
    )
    v = authorize("bash", {"command": "python tmp/x.py"}, mode="whatever")
    assert v.decision == "allow"
    assert v.layer == "L2"


# ==================== 图与接口 ====================

def test_manual_mode_in_the_graph_asks_the_user(db, monkeypatch):
    """手动模式下跑图 → 挂起等用户裁决，不调判官。"""
    from langchain_core.messages import AIMessage

    from src.agent import graph_thinking

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

    monkeypatch.setattr(judge_module, "judge", _no_judge)
    monkeypatch.setattr(graph_thinking, "llm", FakeLLM())

    graph = graph_thinking.build_graph()
    result = graph.invoke(
        {"messages": [], "pending_tool_calls": [], "step_count": 0,
         "session_id": "s1", "authz_mode": "manual"}
    )

    assert result["pending_auth"] is not None
    assert result["pending_tool_calls"] == []


def test_auto_mode_in_the_graph_is_unchanged(db, monkeypatch):
    """默认还是 auto —— 判官说了算。"""
    from langchain_core.messages import AIMessage

    from src.agent import graph_thinking

    seen = []

    def _fake(tool_name, tool_input):
        seen.append(tool_name)
        return judge_module.JudgeResult("allow", "只读", "")

    monkeypatch.setattr(judge_module, "judge", _fake)

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

    graph = graph_thinking.build_graph()
    result = graph.invoke(
        {"messages": [], "pending_tool_calls": [], "step_count": 0, "session_id": "s1"}
    )

    assert seen == ["bash"]
    assert result["pending_auth"] is None


def test_the_query_route_carries_the_mode_into_the_graph(client, db, fake_graph):
    """前端切了模式 → 这个模式要真的到达图。"""
    r = client.post(
        "/session/query",
        json={"session_id": "s1", "query": "跑一下", "authz_mode": "manual"},
    )
    assert r.status_code == 200
    assert fake_graph.states[0]["authz_mode"] == "manual"


def test_the_query_route_defaults_to_auto(client, db, fake_graph):
    r = client.post("/session/query", json={"session_id": "s1", "query": "跑一下"})
    assert r.status_code == 200
    assert fake_graph.states[0]["authz_mode"] == "auto"


def test_the_respond_route_carries_the_mode_into_the_resumed_graph(
    client, db, fake_graph, workdir
):
    """提权恢复时模式也要带过去 —— 恢复后的新一轮同样受管。"""
    from src.agent.authz import pending as authz_pending

    authz_pending.save_batch(
        "s1",
        [{
            "tool_call_id": "c1", "tool_name": "read_file",
            "tool_input": {"path": "hello.txt"},
            "verdict": "escalate", "reason": "r", "pattern": None,
        }],
        [{
            "tool_call_id": "c1", "tool_name": "read_file",
            "tool_input": {"path": "hello.txt"},
            "question": "q", "reason": "r", "pattern": None,
        }],
    )
    r = client.post(
        "/auth/respond",
        json={"session_id": "s1", "tool_call_id": "c1", "choice": "allow_once",
              "authz_mode": "manual"},
    )
    assert r.status_code == 200
    assert fake_graph.states[0]["authz_mode"] == "manual"

