import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine import ai_client
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import BusinessRuleError
from fusion_artifacts_engine.rpc.methods import RPCHandler


@pytest.fixture
def handler():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    eng = ArtifactEngine(cfg)
    h = RPCHandler(eng)
    yield h, eng


async def test_ai_generate_uses_configured_model(handler, monkeypatch):
    h, _ = handler
    seen = {}

    async def fake_chat(base_url, model, messages, **kw):
        seen["base_url"] = base_url
        seen["model"] = model
        seen["messages"] = messages
        return "generated content"

    monkeypatch.setattr(ai_client, "chat", fake_chat)
    result = await h.dispatch("ai.generate", {"prompt": "写一个函数", "model": "m1"})
    assert result["text"] == "generated content"
    assert result["model"] == "m1"
    assert seen["messages"][-1]["content"] == "写一个函数"


async def test_ai_generate_auto_picks_first_model(handler, monkeypatch):
    h, _ = handler

    async def fake_list(base_url, timeout=10.0, api_key=None):
        return ["mlx-model-a", "mlx-model-b"]

    async def fake_chat(base_url, model, messages, **kw):
        return "ok"

    monkeypatch.setattr(ai_client, "list_models", fake_list)
    monkeypatch.setattr(ai_client, "chat", fake_chat)
    result = await h.dispatch("ai.generate", {"prompt": "hi"})
    assert result["model"] == "mlx-model-a"


async def test_ai_generate_empty_prompt_rejected(handler):
    h, _ = handler
    with pytest.raises(ValueError):
        await h.dispatch("ai.generate", {"prompt": "  "})


async def test_ai_generate_backend_down_maps_to_business_rule(handler, monkeypatch):
    h, _ = handler

    async def fake_list(base_url, timeout=10.0, api_key=None):
        raise ConnectionError("refused")

    monkeypatch.setattr(ai_client, "list_models", fake_list)
    with pytest.raises(BusinessRuleError):
        await h.dispatch("ai.generate", {"prompt": "hi"})


async def test_ai_list_models_returns_default(handler, monkeypatch):
    h, eng = handler
    eng.config.ai_model = "preferred"

    async def fake_list(base_url, timeout=10.0, api_key=None):
        return ["preferred", "other"]

    monkeypatch.setattr(ai_client, "list_models", fake_list)
    result = await h.dispatch("ai.list_models", {})
    assert result["models"] == ["preferred", "other"]
    assert result["default"] == "preferred"


async def test_ai_generate_system_message_prepended(handler, monkeypatch):
    h, _ = handler

    async def fake_chat(base_url, model, messages, **kw):
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "sys prompt"
        return "ok"

    monkeypatch.setattr(ai_client, "chat", fake_chat)
    await h.dispatch("ai.generate", {"prompt": "hi", "system": "sys prompt", "model": "m1"})
