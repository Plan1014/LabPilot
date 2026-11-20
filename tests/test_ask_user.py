from src.agent.ask_user import normalize_options


def test_normalize_options_keeps_shape():
    assert normalize_options([
        {"label": " A ", "description": " 因为 ", "recommended": True},
        {"label": "B", "extra": "丢弃"},
    ]) == [
        {"label": "A", "description": "因为", "recommended": True},
        {"label": "B", "description": "", "recommended": False},
    ]


def test_normalize_options_only_keeps_first_recommended():
    out = normalize_options([
        {"label": "A", "recommended": True},
        {"label": "B", "recommended": True},
    ])
    assert [o["recommended"] for o in out] == [True, False]


def test_normalize_options_caps_at_four():
    assert len(normalize_options([{"label": str(i)} for i in range(6)])) == 4


def test_normalize_options_drops_invalid_entries():
    assert normalize_options([{"label": ""}, {"nope": 1}, "x"]) == []


from src.agent.ask_user import build_ask_payload, detect_ask  # noqa: E402


def test_build_ask_payload():
    assert build_ask_payload({
        "id": "call_1", "name": "ask_user",
        "input": {"question": "用哪套？", "options": [{"label": "A"}]},
    }) == {
        "tool_call_id": "call_1",
        "question": "用哪套？",
        "options": [{"label": "A", "description": "", "recommended": False}],
    }


def test_build_ask_payload_rejects_non_ask_tool():
    assert build_ask_payload({"id": "x", "name": "bash", "input": {}}) is None


def test_build_ask_payload_rejects_missing_question():
    assert build_ask_payload({"id": "x", "name": "ask_user", "input": {"options": []}}) is None


def test_build_ask_payload_synthesizes_missing_id():
    payload = build_ask_payload({
        "id": "", "name": "ask_user",
        "input": {"question": "用哪套？", "options": []},
    })
    assert payload["tool_call_id"]
    # 同一个问题必须得到同一个 id（否则配不上回答）
    again = build_ask_payload({
        "id": None, "name": "ask_user",
        "input": {"question": "用哪套？", "options": []},
    })
    assert again["tool_call_id"] == payload["tool_call_id"]


def test_detect_ask_finds_first_ask_user():
    uses = [
        {"id": "1", "name": "bash", "input": {}},
        {"id": "2", "name": "ask_user",
         "input": {"question": "Q", "options": [{"label": "A"}]}},
    ]
    assert detect_ask(uses)["tool_call_id"] == "2"


def test_detect_ask_returns_none_without_ask():
    assert detect_ask([{"id": "1", "name": "bash", "input": {}}]) is None


from src.agent.ask_user import find_pending_ask, build_turn_row  # noqa: E402

HIST = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_1", "name": "ask_user",
         "input": {"question": "Q1", "options": [{"label": "A"}]}},
    ]},
]


def test_find_pending_ask_returns_unanswered_ask():
    assert find_pending_ask(HIST)["tool_call_id"] == "call_1"


def test_find_pending_ask_ignores_answered_ask():
    hist = HIST + [{"role": "tool", "name": "ask_user", "content": "A",
                    "tool_call_id": "call_1"}]
    assert find_pending_ask(hist) is None


def test_find_pending_ask_ignores_other_tools():
    hist = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "b1", "name": "bash", "input": {}}]}]
    assert find_pending_ask(hist) is None


def test_find_pending_ask_returns_latest_of_two_asks():
    hist = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "ask_user",
             "input": {"question": "Q1", "options": []}}]},
        {"role": "tool", "name": "ask_user", "content": "A", "tool_call_id": "call_1"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_2", "name": "ask_user",
             "input": {"question": "Q2", "options": []}}]},
    ]
    assert find_pending_ask(hist)["tool_call_id"] == "call_2"


def test_find_pending_ask_on_empty_history():
    assert find_pending_ask([]) is None


def test_build_turn_row_uses_tool_row_when_ask_pending():
    row = build_turn_row({"tool_call_id": "call_1"}, "参数集 A")
    assert row == {"role": "tool", "name": "ask_user", "content": "参数集 A",
                   "tool_call_id": "call_1"}


def test_build_turn_row_uses_user_row_otherwise():
    assert build_turn_row(None, "帮我看看") == {"role": "user", "content": "帮我看看"}


from langchain_core.messages import AIMessage  # noqa: E402
from src.agent.ask_user import sanitize_ask_message  # noqa: E402


def test_sanitize_drops_sibling_tool_calls():
    resp = AIMessage(content="", tool_calls=[
        {"id": "b1", "name": "bash", "args": {"command": "ls"}, "type": "tool_call"},
        {"id": "a1", "name": "ask_user",
         "args": {"question": "Q", "options": [{"label": "A"}]}, "type": "tool_call"},
    ])
    ask = {"tool_call_id": "a1", "question": "Q", "options": []}
    out = sanitize_ask_message(resp, ask)
    assert [tc["name"] for tc in out.tool_calls] == ["ask_user"]
    assert out.tool_calls[0]["args"]["question"] == "Q"


def test_sanitize_assigns_id_when_missing():
    resp = AIMessage(content="", tool_calls=[
        {"id": "", "name": "ask_user",
         "args": {"question": "Q", "options": []}, "type": "tool_call"},
    ])
    ask = {"tool_call_id": "", "question": "Q", "options": []}
    out = sanitize_ask_message(resp, ask)
    assert out.tool_calls[0]["id"]
    assert ask["tool_call_id"] == out.tool_calls[0]["id"]


def test_sanitize_keeps_text_and_drops_sibling_blocks_in_content_mode():
    resp = AIMessage(content=[
        {"type": "text", "text": "我需要确认一下"},
        {"type": "tool_use", "id": "b1", "name": "bash", "input": {"command": "ls"}},
        {"type": "tool_use", "id": "a1", "name": "ask_user",
         "input": {"question": "Q", "options": []}},
    ])
    ask = {"tool_call_id": "a1", "question": "Q", "options": []}
    out = sanitize_ask_message(resp, ask)
    names = [b.get("name") for b in out.content if b.get("type") == "tool_use"]
    assert names == ["ask_user"]
    assert any(b.get("type") == "text" for b in out.content)


from src.agent.ask_user import format_ask_cli  # noqa: E402


def test_format_ask_cli_lists_options():
    out = format_ask_cli({
        "tool_call_id": "c1",
        "question": "用哪套标定参数？",
        "options": [
            {"label": "参数集 A", "description": "已验证", "recommended": True},
            {"label": "参数集 B", "description": "未验证", "recommended": False},
        ],
    })
    assert "用哪套标定参数？" in out
    assert "参数集 A" in out and "参数集 B" in out
    assert "已验证" in out
    assert "(推荐)" in out


def test_format_ask_cli_moves_recommended_option_first():
    out = format_ask_cli({
        "tool_call_id": "c1",
        "question": "Q?",
        "options": [
            {"label": "B", "description": "", "recommended": False},
            {"label": "A", "description": "", "recommended": True},
        ],
    })
    assert out.index("A") < out.index("B")


def test_format_ask_cli_without_options():
    out = format_ask_cli({"tool_call_id": "c1", "question": "Q?", "options": []})
    assert "Q?" in out
    assert "1." not in out


# ---------------------------------------------------------------------------
# 整支评审的修复（Important 1 / 2 / Minor 4）
# ---------------------------------------------------------------------------


def test_find_pending_ask_ignores_other_tools_with_same_id():
    """别的工具的 tool_result 不能把提问算成「已回答」。

    有些 OpenAI 兼容上游每轮都从 call_0 开始编号；bash 的 tool_result
    撞上提问的 id 时，提问会被当成已回答 —— 用户根本不会被问到。
    """
    history = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_0", "name": "ask_user",
             "input": {"question": "用哪套？"}},
        ]},
        {"role": "tool", "name": "bash", "content": "out", "tool_call_id": "call_0"},
    ]
    ask = find_pending_ask(history)
    assert ask is not None
    assert ask["tool_call_id"] == "call_0"


def test_find_pending_ask_tolerates_malformed_input():
    """历史里出现脏数据不能让整个会话 500。

    input 不是 dict 时，提问仍要被认出来（question 退化成空串即可）。
    """
    history = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "ask_user", "input": "not-a-dict"},
        ]},
    ]
    ask = find_pending_ask(history)
    assert ask is not None
    assert ask["tool_call_id"] == "c1"


def test_sanitize_filters_sibling_in_anthropic_shape():
    """Anthropic 模式：tool_calls 属性也要同步过滤。

    langchain_anthropic 会把 content 里的 tool_use 同步到 tool_calls 属性；
    只清 content 的话，兄弟工具会被 message_to_dict 重新合成回历史，
    下一轮上游因 tool_use 没有对应 tool_result 而 400。
    """
    from src.agent.session_manager import message_to_dict

    response = AIMessage(
        content=[
            {"type": "text", "text": "顺便查一下"},
            {"type": "tool_use", "id": "a1", "name": "bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "a2", "name": "ask_user", "input": {"question": "用哪套？"}},
        ],
        tool_calls=[
            {"id": "a1", "name": "bash", "args": {"command": "ls"}},
            {"id": "a2", "name": "ask_user", "args": {"question": "用哪套？"}},
        ],
    )
    ask = detect_ask([
        {"id": "a2", "name": "ask_user", "input": {"question": "用哪套？"}},
    ])
    sanitize_ask_message(response, ask)

    assert [tc["name"] for tc in response.tool_calls] == ["ask_user"]
    persisted = message_to_dict(response)["content"]
    used = [b for b in persisted if b.get("type") == "tool_use"]
    assert [(b["name"], b["id"]) for b in used] == [("ask_user", "a2")]


def test_sanitize_syncs_synthesized_id_into_tool_calls():
    """上游没给 id 时，补出来的 id 必须写回 tool_calls 属性。

    否则 message_to_dict 会再合成一个 id 为空的 tool_use 块，
    回答配不上提问，前端卡片永远停在「等待回答」。
    """
    from src.agent.session_manager import message_to_dict

    response = AIMessage(
        content=[{"type": "tool_use", "name": "ask_user", "input": {"question": "用哪套？"}}],
        tool_calls=[{"id": "", "name": "ask_user", "args": {"question": "用哪套？"}}],
    )
    ask = detect_ask([{"name": "ask_user", "input": {"question": "用哪套？"}}])
    sanitize_ask_message(response, ask)

    assert response.tool_calls[0]["id"] == ask["tool_call_id"]
    assert ask["tool_call_id"] != ""
    persisted = message_to_dict(response)["content"]
    used = [b for b in persisted if b.get("type") == "tool_use"]
    assert [b["id"] for b in used] == [ask["tool_call_id"]]
