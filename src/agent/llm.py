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


def _openai_config() -> tuple[str, str | None, str, bool]:
    """Return (api_key, api_base, model, enable_thinking) for OpenAI mode."""
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
    enable_thinking = _truthy(os.getenv("OPENAI_ENABLE_THINKING"))
    return api_key, api_base, model, enable_thinking


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
    from langchain_core.outputs import ChatResult

    class ChatOpenAIWithThinking(ChatOpenAI):
        """ChatOpenAI that surfaces upstream `reasoning_content` as a thinking block.

        langchain_openai drops the non-standard `reasoning_content` field returned
        by Qwen3 / DeepSeek-R1 servers when ``extra_body={"enable_thinking": True}``
        is set (its module docstring explicitly warns about this). We intercept
        at the result-assembly step and inject the reasoning into the resulting
        ``AIMessage.content`` as a ``{"type": "thinking"}`` content block —
        mirroring langchain_anthropic's pattern — so the existing
        ``parse_content_blocks`` / ``print_thinking`` / ``message_to_dict`` chain
        can consume it uniformly.

        Only the non-streaming path is patched; this codebase calls the LLM via
        ``invoke()`` (see ``graph_thinking.py``) and streams thinking to the UI
        via ``print_thinking`` SSE events, not via LLM stream chunks.
        """

        capture_thinking: bool = True

        def _create_chat_result(self, response, generation_info=None) -> ChatResult:
            result = super()._create_chat_result(response, generation_info=generation_info)
            if not self.capture_thinking:
                return result
            try:
                choices = getattr(response, "choices", None) or []
            except Exception:
                return result
            # `result.generations` is `list[ChatGeneration]` (flat). Pair each
            # generation with its source choice (by index) for reasoning lookup.
            for idx, gen in enumerate(result.generations):
                if idx >= len(choices):
                    break
                reasoning = self._extract_reasoning(choices[idx])
                if not reasoning:
                    continue
                new_msg = self._inject_thinking(gen.message, reasoning)
                if new_msg is not None:
                    gen.message = new_msg
            return result

        @staticmethod
        def _extract_reasoning(choice) -> str | None:
            """Read `reasoning_content` from a Pydantic Choice (with extra='allow')."""
            try:
                msg = getattr(choice, "message", None)
                if msg is None:
                    return None
                # 1) Direct attribute (Pydantic exposes extras this way)
                reasoning = getattr(msg, "reasoning_content", None)
                if reasoning:
                    return reasoning
                # 2) Fallback via model_extra dict
                extra = getattr(msg, "model_extra", None)
                if isinstance(extra, dict):
                    reasoning = extra.get("reasoning_content")
                    if reasoning:
                        return reasoning
                # 3) Last resort: model_dump
                try:
                    dumped = msg.model_dump()
                    return dumped.get("reasoning_content")
                except Exception:
                    return None
            except Exception:
                return None

        @staticmethod
        def _inject_thinking(message, reasoning: str):
            """Return a copy of `message` with reasoning injected as a thinking block."""
            if not reasoning:
                return None
            content = getattr(message, "content", None)
            if isinstance(content, str):
                new_content = [{"type": "thinking", "thinking": reasoning, "index": 0}]
                if content:
                    new_content.append({"type": "text", "text": [content], "index": 1})
            elif isinstance(content, list):
                new_content = list(content) + [
                    {"type": "thinking", "thinking": reasoning, "index": 0}
                ]
            else:
                new_content = [{"type": "thinking", "thinking": reasoning, "index": 0}]
            try:
                return message.model_copy(update={"content": new_content})
            except Exception:
                return None

    api_key, api_base, model, enable_thinking = _openai_config()
    llm = ChatOpenAIWithThinking(
        api_key=api_key,
        base_url=api_base,
        model=model,
        extra_body={"enable_thinking": True} if enable_thinking else None,
        capture_thinking=enable_thinking,
    )
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