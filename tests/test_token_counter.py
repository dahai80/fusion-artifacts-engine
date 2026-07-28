import pytest
from fusion_artifacts_engine.token_counter import TokenCounter


@pytest.fixture
def counter():
    return TokenCounter(mlx_url="http://localhost:9999")


def test_count_sync_tiktoken(counter):
    result = counter.count_sync("Hello world, this is a test.")
    assert result > 0


def test_count_sync_heuristic(monkeypatch):
    c = TokenCounter(mlx_url="http://localhost:9999")
    c._tiktoken_enc = None
    monkeypatch.setitem(__import__("sys").modules, "tiktoken", None)
    c._count_via_tiktoken("test")
    c._tiktoken_enc = None


@pytest.mark.asyncio
async def test_count_fallback_to_tiktoken(counter):
    result = await counter.count("Hello world")
    assert result > 0


@pytest.mark.asyncio
async def test_count_messages(counter):
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    total = await counter.count_messages(messages)
    assert total > 0


@pytest.mark.asyncio
async def test_check_safety(counter):
    safe, total, remaining = await counter.check_safety(
        [{"role": "user", "content": "hello"}],
        max_context=100000,
        reserve_output=8192,
    )
    assert safe is True
    assert total > 0
    assert remaining > 0
