import pytest

pytestmark = pytest.mark.slow


def _ask_history():
    """一段「模型提问了、还没人回答」的历史。"""
    return [{
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": "c1",
            "name": "ask_user",
            "input": {"question": "用哪套？", "options": [{"label": "A"}]},
        }],
    }]


def test_append_user_turn_is_tool_row_when_ask_pending():
    """有待答提问时，这一行输入必须作为 tool_result 回填，否则模型无法从断点继续。"""
    from src.agent.repl import append_user_turn

    history = _ask_history()
    row = append_user_turn(history, "用 A")

    assert row["role"] == "tool"
    assert row["name"] == "ask_user"
    assert row["tool_call_id"] == "c1"
    assert row["content"] == "用 A"
    assert history[-1] is row


def test_append_user_turn_is_user_row_when_no_pending_ask():
    from src.agent.repl import append_user_turn

    history = []
    row = append_user_turn(history, "继续干活")

    assert row == {"role": "user", "content": "继续干活"}
    assert history == [{"role": "user", "content": "继续干活"}]


def test_notification_idle_blocked_while_ask_pending():
    """提问待答时不能进入 idle —— 否则通知会触发 agent，通知文本被当成回答吞掉。"""
    from src.agent.repl import can_set_idle

    assert can_set_idle([]) is True
    assert can_set_idle(_ask_history()) is False


def test_notification_idle_allowed_after_answer():
    from src.agent.repl import can_set_idle

    history = _ask_history()
    history.append({"role": "tool", "name": "ask_user", "content": "A",
                    "tool_call_id": "c1"})
    assert can_set_idle(history) is True
