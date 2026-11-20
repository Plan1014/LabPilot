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


def test_react_tools_exclude_ask_user():
    """create_react_agent 那条路径不能带 ask_user。

    它的工具是「真的会被执行」的 —— ask_user 跑起来只会返回一句报错串，
    模型拿不到用户回答，提问功能等于不存在。
    """
    from src.agent.tools import TOOLS_FOR_REACT

    names = [t.name for t in TOOLS_FOR_REACT]
    assert "ask_user" not in names
    assert "bash" in names  # 其余工具照常


def test_server_graph_uses_react_tools():
    """src/agent/graph.py 必须用过滤后的工具表。"""
    import inspect

    import src.agent.graph as server_graph

    source = inspect.getsource(server_graph)
    assert "TOOLS_FOR_REACT" in source
    assert "tools=TOOLS," not in source
