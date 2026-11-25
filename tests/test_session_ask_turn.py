from src.agent.session_manager import _compact_tool_results


def test_compact_preserves_ask_user_answer():
    """用户的回答不能被压缩成占位符 —— 模型要靠它继续往下走。"""
    history = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "ask_user",
             "input": {"question": "Q", "options": []}}]},
        {"role": "tool", "name": "ask_user", "content": "A" * 200, "tool_call_id": "c1"},
    ] + [
        {"role": "tool", "name": "bash", "content": "B" * 200, "tool_call_id": f"b{i}"}
        for i in range(40)
    ]
    out = _compact_tool_results(history)
    kept = [m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "c1"]
    assert kept and kept[0]["content"] == "A" * 200


def test_answer_row_matches_the_ask():
    """回答轮必须带 tool_call_id —— 前端靠它把回答与提问配对。"""
    from src.agent.ask_user import build_turn_row, find_pending_ask

    history = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "c1", "name": "ask_user",
         "input": {"question": "Q", "options": []}}]}]
    pending = find_pending_ask(history)
    row = build_turn_row(pending, "参数集 A")
    assert row["role"] == "tool"
    assert row["tool_call_id"] == pending["tool_call_id"] == "c1"


def test_answer_event_carries_tool_call_id():
    """回答轮要发一条 tool_result 事件，让前端把提问卡片标成已回答。"""
    from src.agent.ask_user import build_answer_event

    event = build_answer_event({
        "role": "tool", "name": "ask_user",
        "content": "参数集 A", "tool_call_id": "c1",
    })
    assert event["type"] == "tool_result"
    assert event["name"] == "ask_user"
    assert event["result"] == "参数集 A"
    assert event["tool_call_id"] == "c1"


def test_no_answer_event_for_plain_user_turn():
    from src.agent.ask_user import build_answer_event

    assert build_answer_event({"role": "user", "content": "继续"}) is None


def test_pending_ask_found_in_persisted_history():
    """落库后的 assistant 行里 tool_use 块仍能被认出来。"""
    from src.agent.ask_user import find_pending_ask

    history = [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": [
            {"type": "text", "text": ["我需要确认一下"], "index": 0},
            {"type": "tool_use", "id": "call_abc", "name": "ask_user", "index": 1,
             "input": {"question": "用哪套？", "options": [{"label": "A"}]}},
        ]},
    ]
    pending = find_pending_ask(history)
    assert pending is not None
    assert pending["tool_call_id"] == "call_abc"
    assert pending["question"] == "用哪套？"


def test_answered_ask_is_not_pending():
    from src.agent.ask_user import find_pending_ask

    history = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "ask_user",
             "input": {"question": "Q", "options": []}}]},
        {"role": "tool", "name": "ask_user", "content": "A", "tool_call_id": "c1"},
    ]
    assert find_pending_ask(history) is None
