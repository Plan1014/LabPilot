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
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatResult, ChatGenerationChunk

    class ChatOpenAIWithThinking(ChatOpenAI):
        """ChatOpenAI that surfaces upstream `reasoning_content` as a thinking block.

        langchain_openai drops the non-standard `reasoning_content` field returned
        by Qwen3 / DeepSeek-R1 servers when ``extra_body={"enable_thinking": True}``
        is set. We intercept at both result-assembly paths:

        - **Non-streaming** (``_create_chat_result``): read reasoning off the
          full ``ChatCompletion`` and inject into the resulting ``AIMessage.content``.
        - **Streaming** (``_stream`` / ``_astream``): accumulate reasoning from
          raw ``delta.reasoning_content`` across chunks, then inject into the
          last chunk's message. Required because ``graph.astream_events`` forces
          streaming via a v1 callback handler (``_should_stream``), bypassing
          the non-streaming path entirely.

        In both cases the reasoning is materialized as a
        ``{"type": "thinking", "thinking": "...", "index": 0}`` content block —
        mirroring langchain_anthropic — so the existing
        ``parse_content_blocks`` / ``print_thinking`` / ``message_to_dict`` chain
        can consume it uniformly.
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

        def _stream(self, *args, **kwargs):
            """Override to capture reasoning_content from raw OpenAI SDK stream.

            ``graph.astream_events`` forces streaming via a v1 callback handler
            (see ``ChatOpenAI._should_stream``), bypassing ``_create_chat_result``
            entirely. LangChain's ``_convert_delta_to_message_chunk`` drops the
            non-standard ``reasoning_content`` field — we must read it from the
            raw chunks ourselves and inject the assembled reasoning into the
            last chunk's message as a ``{"type": "thinking"}`` content block.
            """
            if not self.capture_thinking:
                yield from super()._stream(*args, **kwargs)
                return
            yield from self._stream_with_thinking(*args, **kwargs)

        async def _astream(self, *args, **kwargs):
            """Async mirror of :meth:`_stream`."""
            if not self.capture_thinking:
                async for chunk in super()._astream(*args, **kwargs):
                    yield chunk
                return
            async for chunk in self._astream_with_thinking(*args, **kwargs):
                yield chunk

        @staticmethod
        def _extract_reasoning_from_chunk(raw_chunk) -> str:
            """Pull `reasoning_content` from a raw OpenAI SDK chunk's delta."""
            for ch in getattr(raw_chunk, "choices", None) or []:
                rc = getattr(getattr(ch, "delta", None), "reasoning_content", None)
                if rc:
                    return rc
            return ""

        def _stream_with_thinking(self, messages, stop=None, run_manager=None, **kwargs):
            """Sync streaming that intercepts reasoning_content from raw deltas."""
            self._ensure_sync_client_available()
            kwargs["stream"] = True
            payload = self._get_request_payload(messages, stop=stop, **kwargs)
            collected, reasoning = self._collect_thinking_stream(
                self.client.create(**payload)
            )
            yield from self._emit_with_thinking(collected, reasoning, run_manager)

        async def _astream_with_thinking(self, messages, stop=None, run_manager=None, **kwargs):
            """Async mirror of :meth:`_stream_with_thinking`."""
            kwargs["stream"] = True
            payload = self._get_request_payload(messages, stop=stop, **kwargs)
            stream = await self.async_client.create(**payload)
            collected, reasoning = await self._acollect_thinking_stream(stream)
            for chunk in self._emit_with_thinking(collected, reasoning, run_manager):
                yield chunk

        def _collect_thinking_stream(self, response):
            """Drive a sync OpenAI SDK stream; return (chunks, reasoning_str)."""
            collected: list[ChatGenerationChunk] = []
            reasoning_buf: list[str] = []
            default_chunk_class: type = AIMessageChunk
            for raw_chunk in response:
                rc = self._extract_reasoning_from_chunk(raw_chunk)
                if rc:
                    reasoning_buf.append(rc)
                dumped = raw_chunk if isinstance(raw_chunk, dict) else raw_chunk.model_dump()
                gc = self._convert_chunk_to_generation_chunk(dumped, default_chunk_class, {})
                if gc is not None:
                    default_chunk_class = gc.message.__class__
                    collected.append(gc)
            return collected, "".join(reasoning_buf)

        async def _acollect_thinking_stream(self, response):
            """Async mirror of :meth:`_collect_thinking_stream`."""
            collected: list[ChatGenerationChunk] = []
            reasoning_buf: list[str] = []
            default_chunk_class: type = AIMessageChunk
            async for raw_chunk in response:
                rc = self._extract_reasoning_from_chunk(raw_chunk)
                if rc:
                    reasoning_buf.append(rc)
                dumped = raw_chunk if isinstance(raw_chunk, dict) else raw_chunk.model_dump()
                gc = self._convert_chunk_to_generation_chunk(dumped, default_chunk_class, {})
                if gc is not None:
                    default_chunk_class = gc.message.__class__
                    collected.append(gc)
            return collected, "".join(reasoning_buf)

        def _emit_with_thinking(self, collected, full_reasoning, run_manager):
            """Yield collected chunks; inject thinking into the last chunk's message."""
            if full_reasoning and collected:
                last = collected[-1]
                injected = self._inject_thinking(last.message, full_reasoning)
                if injected is not None:
                    collected[-1] = ChatGenerationChunk(
                        message=injected,
                        generation_info=last.generation_info,
                    )
            for chunk in collected:
                if run_manager:
                    logprobs = (chunk.generation_info or {}).get("logprobs")
                    run_manager.on_llm_new_token(
                        chunk.text, chunk=chunk, logprobs=logprobs
                    )
                yield chunk

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