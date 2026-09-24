"""LLM initialization for LabPilot LangGraph Agent.

Two modes:
- Anthropic mode (default): ChatAnthropic + native Anthropic SDK.
- OpenAI mode: when ``OPENAI_API_KEY`` is set and ``ANTHROPIC_API_KEY`` is
  not, connects directly to the OpenAI-compatible upstream
  (DeepSeek / Qwen / GLM / Ollama / vLLM) via ChatOpenAI. No local proxy.
"""

import os

from dotenv import load_dotenv

load_dotenv(override=True)

from src.agent.config import MODEL_ID  # noqa: E402


def _truthy(value: str | None, default: bool = False) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _openai_mode() -> bool:
    """True iff we should connect via OpenAI-compatible protocol."""
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key and not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return True
    return bool(openai_key and _truthy(os.getenv("LITELLM_AUTOSTART")))


def _openai_config() -> tuple[str, str | None, str]:
    """Return (api_key, api_base, model) for OpenAI mode."""
    api_base = os.getenv("LITELLM_MODEL_API_BASE", "").strip() or None
    placeholders = {"", "sk-...", "sk-xxxx", "your_api_key_here", "sk-placeholder"}
    explicit_key = os.getenv("LITELLM_MODEL_API_KEY", "").strip()
    api_key = explicit_key if explicit_key and explicit_key not in placeholders \
        else os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("LITELLM_MODEL", "").strip()
    if not model:
        raise RuntimeError(
            "OPENAI_API_KEY is set but LITELLM_MODEL is empty. "
            "Set LITELLM_MODEL in .env, e.g. `qwen2.5-14b-instruct` or `gpt-4o`."
        )
    # Strip LiteLLM provider prefix (e.g. `openai/qwen3-8b` → `qwen3-8b`)
    # so the model name is what the upstream API expects.
    for prefix in ("openai/", "anthropic/", "gemini/", "azure/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    return api_key, api_base, model


def summarize_text(prompt: str, max_tokens: int = 2000) -> str:
    """Single-turn text completion. Unified across both modes."""
    if _openai_mode():
        from openai import OpenAI
        api_key, api_base, model = _openai_config()
        c = OpenAI(api_key=api_key, base_url=api_base)
        resp = c.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    from anthropic import Anthropic
    c = Anthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY") or "sk-placeholder",
        base_url=os.getenv("ANTHROPIC_BASE_URL", "").strip() or None,
    )
    resp = c.messages.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    if isinstance(resp.content, list):
        for block in resp.content:
            if hasattr(block, "text"):
                return block.text
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return str(resp.content)


if _openai_mode():
    from langchain_openai import ChatOpenAI
    api_key, api_base, model = _openai_config()
    llm = ChatOpenAI(api_key=api_key, base_url=api_base, model=model)
    # Kept for symmetry with Anthropic mode; legacy callers should use
    # summarize_text() instead of poking client.messages directly.
    client = None
    from langchain_anthropic import ChatAnthropic  # noqa: E402  # for type-checker only
else:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or "sk-placeholder"
    ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
    from langchain_anthropic import ChatAnthropic
    from anthropic import Anthropic
    llm = ChatAnthropic(
        model=MODEL_ID,
        anthropic_api_key=ANTHROPIC_API_KEY,
        base_url=ANTHROPIC_BASE_URL or None,
    )
    client = Anthropic(api_key=ANTHROPIC_API_KEY, base_url=ANTHROPIC_BASE_URL)