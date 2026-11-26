"""闸门编排的测试。

顺序是 **拒绝优先**：L1 黑名单 → L0 白名单 → L2 判官 → L3 转人工。
L1 命中就永远不看白名单 —— 否则一条白名单能把黑名单绕过去。
"""

import json

import pytest

from src.agent import paths, session_manager
from src.agent.authz import audit
from src.agent.authz import judge as judge_module
from src.agent.authz import ledger
from src.agent.authz import authorize, Verdict


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    ledger.reset_session_grants()
    audit.reset_recent()
    yield
    ledger.reset_session_grants()
    audit.reset_recent()
    monkeypatch.setattr(session_manager, "_sql_conn", None)


class FakeJudge:
    """假的判官 —— 返回预置结果。"""

    def __init__(self, decision="allow", reason="ok", question=""):
        self.decision = decision
        self.reason = reason
        self.question = question
        self.calls = []

    def __call__(self, tool_name, tool_input):
        self.calls.append((tool_name, tool_input))
        return judge_module.JudgeResult(self.decision, self.reason, self.question)


def _install_judge(monkeypatch, fake):
    monkeypatch.setattr(judge_module, "judge", fake)
    return fake


# ==================== L1 ====================

def test_l1_deny_beats_l0_allow(db):
    v = authorize("write_file", {"path": "src/x.py"})
    assert v.decision == "deny"
    assert v.layer == "L1"


def test_l1_deny_for_bash_write_to_protected(db):
    v = authorize("bash", {"command": "rm -rf src/agent"})
    assert v.decision == "deny"
    assert v.layer == "L1"


def test_l0_allow_for_readonly_tool(db):
    v = authorize("read_file", {"path": "src/x.py"})
    assert v.decision == "allow"
    assert v.layer == "L0"
    assert v.matched_rule == "read_only_tool"


def test_l0_allow_for_write_in_writable_zone(db, tmp_path):
    (tmp_path / "tmp").mkdir()
    v = authorize("write_file", {"path": "tmp/a.txt", "content": "x"})
    assert v.decision == "allow"
    assert v.layer == "L0"


# ==================== 台账 ====================

def test_ledger_hit_is_l0_allow(db):
    ledger.record_grant("write_file:tmp/exp1/**")
    v = authorize("write_file", {"path": "tmp/exp1/a.txt"})
    assert v.decision == "allow"
    assert v.layer == "L0"
    assert v.matched_rule == "ledger"


# ==================== L2 ====================

def test_l2_judge_allow(db, monkeypatch):
    fake = _install_judge(monkeypatch, FakeJudge("allow", "只读"))
    v = authorize("bash", {"command": "python tmp/x.py"})
    assert v.decision == "allow"
    assert v.layer == "L2"
    assert fake.calls


def test_l2_judge_deny(db, monkeypatch):
    _install_judge(monkeypatch, FakeJudge("deny", "破坏"))
    v = authorize("bash", {"command": "python tmp/x.py"})
    assert v.decision == "deny"
    assert v.layer == "L2"


def test_l2_judge_escalate_carries_question(db, monkeypatch):
    _install_judge(monkeypatch, FakeJudge("escalate", "不确定", "要执行吗？"))
    v = authorize("bash", {"command": "python tmp/x.py"})
    assert v.decision == "escalate"
    assert v.layer == "L2"
    assert v.question == "要执行吗？"


def test_escalate_pattern_is_derived_not_from_judge(db, monkeypatch):
    _install_judge(monkeypatch, FakeJudge("escalate", "不确定", "要执行吗？"))
    v = authorize("bash", {"command": "python tmp/x.py"})
    # 模式由 derive_pattern 推导，不来自判官输出
    assert v.pattern == "bash:python tmp/x.py"


def test_escalate_pattern_is_none_when_not_derivable(db, monkeypatch):
    _install_judge(monkeypatch, FakeJudge("escalate", "不确定", "要执行吗？"))
    v = authorize("bash", {"command": "python a.py && wc -l b.txt"})
    assert v.decision == "escalate"
    assert v.pattern is None


def test_missing_judge_model_escalates(db, monkeypatch):
    monkeypatch.delenv(judge_module.JUDGE_MODEL_ENV, raising=False)
    monkeypatch.delenv(judge_module.FALLBACK_MODEL_ENV, raising=False)
    v = authorize("bash", {"command": "python tmp/x.py"})
    assert v.decision == "escalate"


# ==================== 判定日志 ====================

def test_verdict_is_written_to_audit_log(db, tmp_path, monkeypatch):
    log_path = tmp_path / "authz.log"
    monkeypatch.setattr(audit, "AUTHZ_LOG_PATH", log_path)
    authorize("read_file", {"path": "src/x.py"}, session_id="s1")
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "read_file"
    assert entry["decision"] == "allow"
    assert entry["layer"] == "L0"
    assert entry["session_id"] == "s1"


def test_audit_log_is_jsonl(db, tmp_path, monkeypatch):
    log_path = tmp_path / "authz.log"
    monkeypatch.setattr(audit, "AUTHZ_LOG_PATH", log_path)
    authorize("read_file", {"path": "src/x.py"})
    authorize("read_file", {"path": "src/y.py"})
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


def test_audit_failure_does_not_block_allow(db, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUTHZ_LOG_PATH", tmp_path / "nonexistent-dir" / "a.log")
    v = authorize("read_file", {"path": "src/x.py"})
    assert v.decision == "allow"


def test_recent_returns_recent_verdicts(db, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUTHZ_LOG_PATH", tmp_path / "authz.log")
    authorize("read_file", {"path": "src/x.py"})
    authorize("read_file", {"path": "src/y.py"})
    recent = audit.recent(10)
    assert len(recent) == 2
    assert recent[-1]["tool"] == "read_file"


def test_verdict_dataclass_defaults():
    v = Verdict(decision="allow", layer="L0", reason="r")
    assert v.matched_rule is None
    assert v.question == ""
    assert v.pattern is None
