"""鉴权 agent（判官）的测试。

判官是**唯一**的 LLM 调用点，也是整层闸门里唯一可能被绕过的一环 —— 所以它
必须 fail-closed：解析失败、超时、模型没配，全部转人工，**绝不默认放行**。
"""

import json

import pytest

from src.agent.authz import judge


class FakeClient:
    """假的 chat client —— 只实现 invoke。"""

    def __init__(self, content=None, raises=None):
        self.content = content
        self.raises = raises
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        from langchain_core.messages import AIMessage

        return AIMessage(content=self.content)


@pytest.fixture(autouse=True)
def clean_failures():
    judge.reset_failures()
    yield
    judge.reset_failures()


def _install(monkeypatch, primary, fallback=None):
    """把 _make_client 换成假的。primary/fallback 可以是 FakeClient 或异常。"""
    made = []

    def fake_make_client(model_id):
        made.append(model_id)
        if fallback is not None and model_id == "fallback-model":
            return fallback
        return primary

    monkeypatch.setattr(judge, "_make_client", fake_make_client)
    monkeypatch.setenv(judge.JUDGE_MODEL_ENV, "judge-model")
    if fallback is not None:
        monkeypatch.setenv(judge.FALLBACK_MODEL_ENV, "fallback-model")
    else:
        monkeypatch.delenv(judge.FALLBACK_MODEL_ENV, raising=False)
    return made


# ==================== 输出解析 ====================

def test_allow_decision_parsed():
    raw = json.dumps({"decision": "allow", "reason": "只读", "question": ""})
    r = judge.parse_judge_output(raw)
    assert r is not None and r.decision == "allow" and r.reason == "只读"


def test_json_wrapped_in_markdown_fence_is_parsed():
    raw = "```json\n{\"decision\": \"deny\", \"reason\": \"破坏\"}\n```"
    r = judge.parse_judge_output(raw)
    assert r is not None and r.decision == "deny"


def test_decision_case_insensitive():
    raw = json.dumps({"decision": "ALLOW", "reason": "x"})
    r = judge.parse_judge_output(raw)
    assert r is not None and r.decision == "allow"


def test_invalid_decision_value_parses_to_none():
    assert judge.parse_judge_output(json.dumps({"decision": "maybe"})) is None


def test_garbage_output_parses_to_none():
    assert judge.parse_judge_output("not json at all") is None
    assert judge.parse_judge_output("") is None


# ==================== 降级阶梯 ====================

def test_unparseable_output_escalates_not_allows(monkeypatch):
    _install(monkeypatch, FakeClient(content="garbage"))
    r = judge.judge("bash", {"command": "rm -rf /"})
    assert r.decision == "escalate"


def test_timeout_escalates_not_allows(monkeypatch):
    _install(monkeypatch, FakeClient(raises=TimeoutError("boom")))
    r = judge.judge("bash", {"command": "rm -rf /"})
    assert r.decision == "escalate"


def test_judge_allow_is_returned(monkeypatch):
    client = FakeClient(content=json.dumps({"decision": "allow", "reason": "只读"}))
    _install(monkeypatch, client)
    r = judge.judge("bash", {"command": "cat src/x.py"})
    assert r.decision == "allow"


def test_fallback_model_used_after_primary_failure(monkeypatch):
    primary = FakeClient(raises=RuntimeError("down"))
    fallback = FakeClient(content=json.dumps({"decision": "allow", "reason": "ok"}))
    made = _install(monkeypatch, primary, fallback)
    r = judge.judge("bash", {"command": "ls"})
    assert r.decision == "allow"
    assert len(made) == 2


def test_both_models_failing_escalates(monkeypatch):
    primary = FakeClient(raises=RuntimeError("down"))
    fallback = FakeClient(raises=RuntimeError("also down"))
    _install(monkeypatch, primary, fallback)
    r = judge.judge("bash", {"command": "ls"})
    assert r.decision == "escalate"


def test_three_consecutive_failures_skip_llm_call(monkeypatch):
    client = FakeClient(raises=RuntimeError("down"))
    _install(monkeypatch, client)
    for _ in range(3):
        assert judge.judge("bash", {"command": "ls"}).decision == "escalate"
    assert judge.consecutive_failures() == 3
    calls_before = len(client.calls)
    r = judge.judge("bash", {"command": "ls"})
    assert r.decision == "escalate"
    assert len(client.calls) == calls_before  # 第 4 次没再调 LLM


def test_success_resets_failure_counter(monkeypatch):
    bad = FakeClient(raises=RuntimeError("down"))
    _install(monkeypatch, bad)
    judge.judge("bash", {"command": "ls"})
    assert judge.consecutive_failures() == 1
    good = FakeClient(content=json.dumps({"decision": "allow", "reason": "ok"}))
    _install(monkeypatch, good)
    judge.judge("bash", {"command": "ls"})
    assert judge.consecutive_failures() == 0


def test_missing_model_id_escalates_without_llm_call(monkeypatch):
    monkeypatch.delenv(judge.JUDGE_MODEL_ENV, raising=False)
    monkeypatch.delenv(judge.FALLBACK_MODEL_ENV, raising=False)
    called = []
    monkeypatch.setattr(judge, "_make_client", lambda m: called.append(m))
    r = judge.judge("bash", {"command": "ls"})
    assert r.decision == "escalate"
    assert called == []


def test_escalate_carries_question():
    raw = json.dumps({"decision": "escalate", "reason": "不确定", "question": "要执行吗？"})
    r = judge.parse_judge_output(raw)
    assert r is not None and r.question == "要执行吗？"


# ==================== client 构造 ====================

def test_client_uses_json_response_format_and_no_thinking(monkeypatch):
    import langchain_openai

    recorded = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_MODEL", "qwen3:8b")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_AUTOSTART", raising=False)

    judge._make_client("judge-model")

    assert recorded["model"] == "judge-model"
    assert recorded["response_format"] == {"type": "json_object"}
    assert recorded["extra_body"] == {"enable_thinking": False}
    assert recorded["timeout"] == judge.JUDGE_TIMEOUT_S


def test_user_message_contains_only_the_tool_call():
    msg = judge.build_judge_user_message("bash", {"command": "cat src/x.py"})
    assert "bash" in msg
    assert "cat src/x.py" in msg
    # 不能带会话历史 / 用户原话
    assert "用户" not in msg
