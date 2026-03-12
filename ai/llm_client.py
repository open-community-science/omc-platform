"""Shared LLM client — works with LM Studio locally, or cloud APIs in production."""
from openai import OpenAI
from typing import Optional


# Default local LM Studio endpoint
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "qwen/qwen3-coder-30b"


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


def chat(
    client: OpenAI,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
) -> str:
    """Single-turn chat completion. Returns the response text."""
    response = client.chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content


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
    response = client.chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=all_messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content
