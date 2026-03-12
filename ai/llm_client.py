"""Shared LLM client — works with LM Studio locally, or cloud APIs in production."""
import re
import time
import logging
from openai import OpenAI, BadRequestError
from typing import Optional

log = logging.getLogger(__name__)

# Default local LM Studio endpoint
DEFAULT_BASE_URL = "http://10.151.49.182:1234/v1"
DEFAULT_MODEL = "qwen3-coder-30b-a3b-instruct"

# Reasoning models use tokens for <think> blocks before the actual response.
# Scale up requested tokens to compensate.
REASONING_TOKEN_MULTIPLIER = 3

# Retry config for transient LM Studio errors (model unloaded, loading, etc.)
MAX_RETRIES = 5
RETRY_DELAY = 15  # seconds

# Patterns to strip <think>...</think> blocks from reasoning models
_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)


def _strip_think(text: str) -> str:
    """Strip <think> reasoning blocks from model output.

    Handles both closed (<think>...</think>content) and truncated (<think>...EOF)
    blocks from reasoning models like DeepSeek-R1.
    """
    if not text:
        return ""
    # First strip complete blocks
    result = _THINK_CLOSED_RE.sub("", text)
    # Then strip any remaining unclosed block (truncated output)
    result = _THINK_OPEN_RE.sub("", result)
    return result.strip()


def get_client(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> OpenAI:
    """Get an OpenAI-compatible client.

    For LM Studio: base_url="http://127.0.0.1:1234/v1", api_key="lm-studio"
    For OpenAI:    base_url=None (default), api_key=<real key>
    For Anthropic: use the anthropic SDK directly (different response format)
    """
    return OpenAI(
        base_url=base_url or DEFAULT_BASE_URL,
        api_key=api_key or "lm-studio",
    )


def _is_reasoning_model(model: str) -> bool:
    """Check if the model is a reasoning model that uses <think> blocks."""
    return any(tag in model.lower() for tag in ["deepseek-r1", "qwq", "reasoning"])


def _effective_tokens(max_tokens: int, model: str | None) -> int:
    """Scale token budget for reasoning models that spend tokens on thinking."""
    m = model or DEFAULT_MODEL
    if _is_reasoning_model(m):
        return max_tokens * REASONING_TOKEN_MULTIPLIER
    return max_tokens


def _call_with_retry(create_fn, **kwargs):
    """Call the OpenAI create function with retry on transient errors."""
    for attempt in range(MAX_RETRIES):
        try:
            return create_fn(**kwargs)
        except BadRequestError as e:
            msg = str(e).lower()
            if any(k in msg for k in ["unloaded", "loading", "failed to load", "canceled", "cancelled"]):
                if attempt < MAX_RETRIES - 1:
                    log.warning(f"Model transient error (attempt {attempt + 1}): {e}. Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                    continue
            raise
    raise RuntimeError("Exhausted retries")


def chat(
    client: OpenAI,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
) -> str:
    """Single-turn chat completion. Returns the response text."""
    response = _call_with_retry(
        client.chat.completions.create,
        model=model or DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=_effective_tokens(max_tokens, model),
        temperature=temperature,
    )
    return _strip_think(response.choices[0].message.content)


def multi_turn(
    client: OpenAI,
    system: str,
    messages: list[dict],
    model: Optional[str] = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
) -> str:
    """Multi-turn chat completion. Messages should be [{"role": ..., "content": ...}, ...]."""
    all_messages = [{"role": "system", "content": system}] + messages
    response = _call_with_retry(
        client.chat.completions.create,
        model=model or DEFAULT_MODEL,
        messages=all_messages,
        max_tokens=_effective_tokens(max_tokens, model),
        temperature=temperature,
    )
    return _strip_think(response.choices[0].message.content)
