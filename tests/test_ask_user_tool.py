import json

from langchain_core.utils.function_calling import convert_to_openai_function

from src.agent.tools import TOOLS


def _ask_user_tool():
    return next(t for t in TOOLS if t.name == "ask_user")


def test_ask_user_is_registered():
    assert _ask_user_tool().name == "ask_user"


def test_schema_has_no_ref_or_defs():
    """Ollama 对 $ref/$defs 嵌套 schema 支持不稳 —— schema 必须是扁平的。"""
    schema = json.dumps(convert_to_openai_function(_ask_user_tool()))
    assert "$ref" not in schema
    assert "$defs" not in schema


def test_schema_exposes_question_and_options():
    params = convert_to_openai_function(_ask_user_tool())["parameters"]
    assert set(params["properties"]) == {"question", "options"}
    assert params["properties"]["options"]["type"] == "array"


def test_description_explains_recommended_flag():
    desc = convert_to_openai_function(_ask_user_tool())["description"]
    assert "recommended" in desc
    assert "question" in desc
