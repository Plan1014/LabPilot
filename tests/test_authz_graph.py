"""图节点集成（`authorize_node`）的测试。

批次原子性：只要有一个调用 escalate，**整批挂起**，本轮不执行任何工具。
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from src.agent import session_manager
from src.agent.authz import judge as judge_module
from src.agent.authz import pending as authz_pending


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.agent import paths

    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)
    # 台账跟着工作区走 —— 指到临时目录，测试不该往用户的库里写
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    yield
    monkeypatch.setattr(session_manager, "_sql_conn", None)


class FakeJudge:
    def __init__(self, decision="escalate", reason="不确定", question="要执行吗？"):
        self.decision = decision
        self.reason = reason
        self.question = question

    def __call__(self, tool_name, tool_input):
        return judge_module.JudgeResult(self.decision, self.reason, self.question)


def _fake_llm(tool_calls, extra_calls=1):
    """假 LLM：第一轮给 tool_calls，之后给纯文本。"""
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=list(tool_calls))
            return AIMessage(content="好了")

    return FakeLLM()


def _tool_messages(result):
    return [m for m in result if isinstance(m, ToolMessage)]


# ==================== allow / deny ====================

def test_allow_passes_through_to_execute_tools(db, monkeypatch):
    from src.agent import graph_thinking

    fake = _fake_llm([
        {"id": "c1", "name": "read_file", "type": "tool_call", "args": {"path": "README.md"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0})

    tool_msgs = _tool_messages(result["messages"])
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"


def test_deny_produces_tool_result_and_does_not_execute(db, monkeypatch):
    from src.agent import graph_thinking
    from src.agent.config import WORKDIR

    fake = _fake_llm([
        {"id": "c1", "name": "write_file", "type": "tool_call",
         "args": {"path": "src/evil.py", "content": "boom"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0})

    tool_msgs = _tool_messages(result["messages"])
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert tool_msgs[0].content.startswith("授权拒绝：")
    # 工具没有被真的执行
    assert not (WORKDIR / "src" / "evil.py").exists()


def test_deny_tool_result_is_not_treated_as_failure():
    from src.agent.tools import is_failure_result

    assert is_failure_result("授权拒绝：写入目标 src/x.py 不在可写区内") is False


# ==================== escalate ====================

def test_escalate_stops_the_turn_and_emits_auth_request(db, monkeypatch):
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    events = []
    graph_thinking.set_event_emitter(lambda e: events.append(e))

    graph = graph_thinking.build_graph()
    result = graph.invoke(
        {"messages": [], "pending_tool_calls": [], "step_count": 0, "session_id": "s1"}
    )

    reqs = [e for e in events if e.get("type") == "auth_request"]
    assert len(reqs) == 1
    assert reqs[0]["tool_call_id"] == "c1"
    assert reqs[0]["question"]
    assert result["pending_auth"] is not None
    assert result["pending_auth"]["batch_id"] == reqs[0]["batch_id"]


def test_escalate_leaves_no_unpaired_tool_use(db, monkeypatch):
    """挂起时不产 tool_result —— 等用户裁决后补齐。"""
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke(
        {"messages": [], "pending_tool_calls": [], "step_count": 0, "session_id": "s1"}
    )

    assert _tool_messages(result["messages"]) == []
    # assistant 消息里的 tool_use 仍在
    tool_use_ids = [
        tc["id"]
        for m in result["messages"]
        if isinstance(m, AIMessage)
        for tc in (getattr(m, "tool_calls", None) or [])
    ]
    assert tool_use_ids == ["c1"]


def test_sibling_calls_are_not_executed_when_one_escalates(db, monkeypatch):
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    fake = _fake_llm([
        {"id": "c1", "name": "read_file", "type": "tool_call", "args": {"path": "README.md"}},
        {"id": "c2", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke(
        {"messages": [], "pending_tool_calls": [], "step_count": 0, "session_id": "s1"}
    )

    # 整批挂起 —— 连 allow 的兄弟调用也没执行
    assert _tool_messages(result["messages"]) == []
    assert result["pending_tool_calls"] == []
    assert result["pending_auth"] is not None


def test_graph_stops_after_escalate(db, monkeypatch):
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("escalate 之后不应该再调用 LLM")
            return AIMessage(content="", tool_calls=[
                {"id": "c1", "name": "bash", "type": "tool_call",
                 "args": {"command": "python tmp/x.py"}},
            ])

    fake = FakeLLM()
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    graph.invoke(
        {"messages": [], "pending_tool_calls": [], "step_count": 0, "session_id": "s1"}
    )
    assert fake.calls == 1


def test_escalate_writes_pending_rows(db, monkeypatch):
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    graph.invoke(
        {"messages": [], "pending_tool_calls": [], "step_count": 0, "session_id": "s1"}
    )

    rows = authz_pending.list_pending("s1")
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == "c1"
    assert rows[0]["tool_name"] == "bash"
    assert rows[0]["status"] == "pending"


# ==================== REPL 分支 ====================

def test_repl_prompts_and_continues(db, monkeypatch):
    """REPL（interactive=True）→ 终端里问，不写库。"""
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    monkeypatch.setattr(graph_thinking, "_prompt_cli_decision", lambda v: "allow_once")
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0, "interactive": True})

    # 批准 → 工具执行，产 tool_result
    tool_msgs = _tool_messages(result["messages"])
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert result["pending_auth"] is None


def test_repl_deny_produces_denial_tool_result(db, monkeypatch):
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    monkeypatch.setattr(graph_thinking, "_prompt_cli_decision", lambda v: "deny")
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0, "interactive": True})

    tool_msgs = _tool_messages(result["messages"])
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content.startswith("授权拒绝：")


def test_repl_allow_always_records_grant(db, monkeypatch):
    from src.agent import graph_thinking
    from src.agent.authz import ledger

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    monkeypatch.setattr(graph_thinking, "_prompt_cli_decision", lambda v: "allow_always")
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0, "interactive": True})

    patterns = [g["pattern"] for g in ledger.list_grants()]
    assert "bash:python tmp/x.py" in patterns


def test_repl_wide_grant_is_recorded_under_the_repl_identity(db, monkeypatch):
    """REPL 的宽授权要挂在自己的号上（评审 M5）。

    「没有会话」那个桶是 server 模式的闸门包装器在用的 —— 落进去就等于把
    终端里批准的授权带去了另一个执行入口。

    宽模式今天推不出来（`derive_pattern` 只推窄模式），所以这里直接把模式
    换成宽的 —— 钉的是「授权归到谁名下」这条路由规则，不是模式的推导。
    """
    from src.agent import graph_thinking
    from src.agent.authz import ledger
    from src.agent.authz import patterns as authz_patterns

    command = "curl http://127.0.0.1:9999/x"

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    monkeypatch.setattr(graph_thinking, "_prompt_cli_decision", lambda v: "allow_always")
    monkeypatch.setattr(
        authz_patterns, "derive_pattern", lambda *a, **k: f"bash:{command}"
    )
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call", "args": {"command": command}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0, "interactive": True})

    # 挂在 REPL 自己的号上
    assert ledger.match_grant("bash", {"command": command}, session_id="repl") == f"bash:{command}"
    # 「没有会话」的路径（server 模式）捡不到
    assert ledger.match_grant("bash", {"command": command}, session_id=None) is None


def test_escalate_without_session_and_without_interactive_is_denied(db, monkeypatch):
    """既没有会话（弹不了卡）也不是终端（问不了人）→ 没人能裁决 → 关闭式拒绝。

    以前这条路会退化成「问服务端终端」（评审 C3）。
    """
    from src.agent import graph_thinking

    monkeypatch.setattr(judge_module, "judge", FakeJudge())
    monkeypatch.setattr(
        graph_thinking,
        "_prompt_cli_decision",
        lambda v: pytest.fail("没有终端就不该问终端"),
    )
    fake = _fake_llm([
        {"id": "c1", "name": "bash", "type": "tool_call",
         "args": {"command": "python tmp/x.py"}},
    ])
    monkeypatch.setattr(graph_thinking, "llm", fake)

    graph = graph_thinking.build_graph()
    result = graph.invoke({"messages": [], "pending_tool_calls": [], "step_count": 0})

    tool_msgs = _tool_messages(result["messages"])
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content.startswith("授权拒绝：")
    assert result["pending_auth"] is None
