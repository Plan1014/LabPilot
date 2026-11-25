from src.agent.config import SYSTEM_PROMPT_TEMPLATE


def test_system_prompt_has_no_ask_user_text_protocol():
    """提问是工具调用，不是提示词里的文本约定 —— prompt 里不该有任何 ask_user 标签。"""
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        workdir="C:/x", skills="- a", tools="bash", now="2026-11-23 00:00:00 UTC"
    )
    assert "<ask_user" not in prompt
    assert "ask_user_answer" not in prompt


def test_config_module_has_no_ask_user_contract():
    import src.agent.config as cfg
    assert not hasattr(cfg, "ASK_USER_CONTRACT")
    assert not hasattr(cfg, "build_system_prompt")
