"""Test LLM client with local LM Studio."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai.llm_client import get_client, chat, multi_turn, _strip_think

pytestmark = pytest.mark.ai


def test_strip_think():
    assert _strip_think("<think>reasoning here</think>Hello!") == "Hello!"
    assert _strip_think("No think tags") == "No think tags"
    assert _strip_think("<think>\nlong\nreasoning\n</think>\nAnswer") == "Answer"


@pytest.mark.timeout(120)
def test_chat_returns_text():
    client = get_client()
    # Reasoning models need generous token budgets (thinking consumes tokens)
    resp = chat(client, "You are helpful. Be concise.", "Say hello in one sentence.", max_tokens=500)
    assert isinstance(resp, str)
    assert len(resp) > 0
    assert "<think>" not in resp


@pytest.mark.timeout(120)
def test_multi_turn():
    client = get_client()
    messages = [
        {"role": "user", "content": "My name is Alice."},
        {"role": "assistant", "content": "Nice to meet you, Alice!"},
        {"role": "user", "content": "What is my name? Answer in one word."},
    ]
    resp = multi_turn(client, "You are helpful. Be concise.", messages, max_tokens=500)
    assert "Alice" in resp
