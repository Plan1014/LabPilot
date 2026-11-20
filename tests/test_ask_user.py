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
