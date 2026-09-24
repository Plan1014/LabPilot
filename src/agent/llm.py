"""LLM initialization for LabPilot LangGraph Agent.

Provider selection logic (see .env.example for variables):

  1. If ``OPENAI_API_KEY`` (or other non-Anthropic key) is set AND
     ``LITELLM_AUTOSTART`` is true  -> start a LiteLLM Proxy locally on
     ``LITELLM_PROXY_PORT`` that translates Anthropic protocol to the
     configured upstream provider, then connect LabPilot through it.
     This lets us reuse the existing ``ChatAnthropic`` + ``tool_use``
     parser code unchanged for *any* OpenAI-compatible model
     (DeepSeek / Qwen / GLM / Ollama / vLLM / OpenAI itself).

  2. Otherwise -> connect directly to Anthropic (or ``ANTHROPIC_BASE_URL``
     if set, e.g. a self-hosted Anthropic-compatible gateway).

In both branches LabPilot speaks the **Anthropic protocol**; the only
difference is whether the base URL points at upstream Anthropic or at
the local LiteLLM Proxy.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env file (override existing env vars) — must run before reading MODEL_ID etc.
load_dotenv(override=True)

from src.agent.config import MODEL_ID  # noqa: E402  (load_dotenv must run first)


# ================= Provider detection =================

def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _has_openai_key() -> bool:
    """True iff a non-Anthropic LLM key is configured and we should route
    through LiteLLM Proxy.

    The check is intentionally simple: if ``OPENAI_API_KEY`` is present
    *and* ``ANTHROPIC_API_KEY`` is not, we go through LiteLLM. Users who
    want both can flip ``LITELLM_AUTOSTART=false`` to force direct mode.
    """
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if openai_key and not anthropic_key:
        return True
    # Explicit override: LITELLM_AUTOSTART=true forces proxy even when
    # both keys are present (useful for A/B testing).
    if _truthy(os.getenv("LITELLM_AUTOSTART")) and openai_key:
        return True
    return False


LITELLM_PROXY_PORT = int(os.getenv("LITELLM_PROXY_PORT", "4000"))
LITELLM_AUTOSTART = _truthy(os.getenv("LITELLM_AUTOSTART"), default=True)
LITELLM_HOST = os.getenv("LITELLM_HOST", "127.0.0.1")
LITELLM_STARTUP_TIMEOUT = float(os.getenv("LITELLM_STARTUP_TIMEOUT", "30"))


# ================= LiteLLM Proxy lifecycle =================

_proxy_process: subprocess.Popen | None = None


def _write_litellm_config(config_path: Path) -> None:
    """Generate a minimal litellm_config.yaml from env vars.

    Reads:
      LITELLM_MODEL         e.g. ``openai/qwen2.5-14b-instruct`` or
                            ``ollama/llama3.1`` or ``openai/gpt-4o``
      LITELLM_MODEL_API_BASE upstream API base (optional)
      LITELLM_MODEL_API_KEY upstream API key (optional; falls back to
                            OPENAI_API_KEY env)
    """
    model = os.getenv("LITELLM_MODEL", "").strip()
    if not model:
        raise RuntimeError(
            "OPENAI_API_KEY is set but LITELLM_MODEL is empty. "
            "Set LITELLM_MODEL in .env, e.g. `openai/qwen2.5-14b-instruct` "
            "or `openai/gpt-4o` or `ollama/llama3.1`."
        )

    api_base = os.getenv("LITELLM_MODEL_API_BASE", "").strip()
    api_key = os.getenv("LITELLM_MODEL_API_KEY", "").strip() or os.getenv(
        "OPENAI_API_KEY", ""
    ).strip()

    # `litellm --model` accepts a single model via CLI, but using a config
    # file is more readable and lets us set api_base / api_key properly.
    lines = ["model_list:"]
    lines.append(f"  - model_name: {MODEL_ID}")
    litellm_params = [f"      model: {model}"]
    if api_base:
        litellm_params.append(f"      api_base: {api_base}")
    if api_key:
        litellm_params.append(f"      api_key: {api_key}")
    lines.extend(litellm_params)
    lines.append("")

    config_path.write_text("\n".join(lines), encoding="utf-8")


def _wait_for_proxy(port: int, timeout: float) -> bool:
    """Poll http://127.0.0.1:{port}/health/liveliness until ok or timeout."""
    import urllib.request
    import urllib.error

    deadline = time.time() + timeout
    url = f"http://{LITELLM_HOST}:{port}/health/liveliness"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.3)
    return False


def ensure_litellm_proxy(log=print) -> str:
    """Start LiteLLM Proxy in the background if not already running.

    Returns the base URL (``http://127.0.0.1:PORT``) LabPilot should
    point ``ANTHROPIC_BASE_URL`` at. If the proxy is already listening
    on the target port we reuse it (idempotent).
    """
    global _proxy_process
    base_url = f"http://{LITELLM_HOST}:{LITELLM_PROXY_PORT}"

    # Already running? Don't spawn a second one.
    if _wait_for_proxy(LITELLM_PROXY_PORT, timeout=0.5):
        log(f"[litellm] proxy already up at {base_url}")
        return base_url

    if not LITELLM_AUTOSTART:
        # Caller asked us NOT to autostart — assume proxy is managed externally.
        log(f"[litellm] autostart disabled, assuming proxy at {base_url}")
        return base_url

    log(f"[litellm] starting proxy on port {LITELLM_PROXY_PORT} ...")

    # Write a transient config file next to .env or in CWD.
    config_path = Path(os.getenv("LITELLM_CONFIG_PATH", "litellm_config.yaml"))
    _write_litellm_config(config_path)

    cmd = [
        sys.executable,
        "-m",
        "litellm",
        "--config",
        str(config_path),
        "--host",
        LITELLM_HOST,
        "--port",
        str(LITELLM_PROXY_PORT),
        # Don't pollute parent stdout; pipe and drop.
        # Use `litellm --detailed_debug` if troubleshooting.
    ]

    creationflags = 0
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP so Ctrl+C / signals don't kill proxy
        # when LabPilot exits.
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        _proxy_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "Failed to spawn LiteLLM Proxy. Did you `uv add 'litellm[proxy]'` "
            "(or `pip install 'litellm[proxy]'`)?"
        ) from e

    if not _wait_for_proxy(LITELLM_PROXY_PORT, LITELLM_STARTUP_TIMEOUT):
        # Cleanup and fail loudly with actionable message.
        try:
            _proxy_process.terminate()
        except Exception:
            pass
        raise RuntimeError(
            f"LiteLLM Proxy did not become ready on {base_url} within "
            f"{LITELLM_STARTUP_TIMEOUT}s. Check that LITELLM_MODEL and "
            f"upstream API key/base are correct, then retry."
        )

    log(f"[litellm] proxy ready at {base_url} (pid={_proxy_process.pid})")
    return base_url


# ================= Resolve effective base URL =================

def _resolve_anthropic_base_url() -> str | None:
    """Pick the base URL the Anthropic client should hit.

    Priority:
      1. Explicit ``ANTHROPIC_BASE_URL`` (use as-is; user knows what they
         want, e.g. a self-hosted Anthropic-compatible gateway).
      2. OpenAI key detected -> spawn LiteLLM Proxy, return its URL.
      3. None -> default Anthropic API endpoint.
    """
    explicit = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if explicit:
        return explicit

    if _has_openai_key():
        return ensure_litellm_proxy()

    return None


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or "sk-placeholder"
ANTHROPIC_BASE_URL = _resolve_anthropic_base_url()


# ================= LangChain chat model =================

from langchain_anthropic import ChatAnthropic  # noqa: E402  (after env setup)

llm = ChatAnthropic(
    model=MODEL_ID,
    anthropic_api_key=ANTHROPIC_API_KEY,
    base_url=ANTHROPIC_BASE_URL or None,
)


# ================= Raw client (used by auto_compact) =================

from anthropic import Anthropic  # noqa: E402

client = Anthropic(
    api_key=ANTHROPIC_API_KEY,
    base_url=ANTHROPIC_BASE_URL,
)
