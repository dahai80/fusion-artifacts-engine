import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import NotImplementedError
from fusion_artifacts_engine.rpc.methods import RPCHandler


@pytest.fixture
def engine_with_tmp(tmp_path):
    cfg = ArtifactEngineConfig(storage_root=str(tmp_path), sync_root=str(tmp_path))
    return ArtifactEngine(config=cfg)


# ── artifact.render ─────────────────────────────────────────


async def test_render_creates_artifact(engine_with_tmp):
    engine = engine_with_tmp
    content = "<!DOCTYPE html>\n<html><body>" + ("x" * 2000) + "</body></html>"
    result = await engine.render_artifact(content, session_id="s1")
    assert result["created"] is True
    assert result["render_type"] is not None
    assert "artifact" in result
    assert "ref_text" in result


async def test_render_below_threshold(engine_with_tmp):
    engine = engine_with_tmp
    result = await engine.render_artifact("short", session_id="s1")
    assert result["created"] is False
    assert result["reason"] == "below_threshold"


async def test_render_empty_content(engine_with_tmp):
    engine = engine_with_tmp
    result = await engine.render_artifact("", session_id="s1")
    assert result["created"] is False
    assert result["reason"] == "empty_content"


async def test_render_with_lang_hint(engine_with_tmp):
    engine = engine_with_tmp
    content = "x" * 2000
    result = await engine.render_artifact(content, session_id="s1", lang_hint="python")
    assert result["created"] is True
    assert result["artifact"]["name"].endswith(".py")


# ── artifact.check_safety ───────────────────────────────────


def test_check_safety_safe(engine_with_tmp):
    engine = engine_with_tmp
    messages = [{"role": "user", "content": "hello"}]
    result = engine.check_safety(messages, output_budget=10000)
    assert result["safe"] is True
    assert result["current_tokens"] > 0
    assert result["remaining_tokens"] > 0


def test_check_safety_unsafe(engine_with_tmp):
    engine = engine_with_tmp
    messages = [{"role": "user", "content": "x" * 10000}]
    result = engine.check_safety(messages, output_budget=10)
    assert result["safe"] is False
    assert result["remaining_tokens"] < 0


def test_check_safety_default_budget(engine_with_tmp):
    engine = engine_with_tmp
    messages = [{"role": "user", "content": "hello"}]
    result = engine.check_safety(messages, output_budget=None)
    assert result["safe"] is True
    assert result["remaining_tokens"] == 200000 - result["current_tokens"]


# ── artifact.inject ─────────────────────────────────────────


def test_inject_passthrough(engine_with_tmp):
    engine = engine_with_tmp
    messages = [{"role": "user", "content": "hello world"}]
    result = engine.inject(messages, output_budget=10000)
    assert result["messages"] == messages
    assert result["total_tokens"] > 0
    assert result["safe"] is True


def test_inject_unsafe(engine_with_tmp):
    engine = engine_with_tmp
    messages = [{"role": "user", "content": "x" * 10000}]
    result = engine.inject(messages, output_budget=10)
    assert result["safe"] is False


# ── artifact.interact ───────────────────────────────────────


async def test_interact_records_event(engine_with_tmp):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1", name="doc.md", artifact_type="markdown", content="hello"
    )
    result = engine.interact_artifact(artifact.id, action="click", payload={"x": 1})
    assert result["ok"] is True
    assert result["artifact_id"] == artifact.id
    assert result["action"] == "click"
    assert "event_id" in result
    events, total = engine.list_events(artifact_id=artifact.id)
    assert total >= 1
    assert any(e.event_type == "artifact.interaction" for e in events)


def test_interact_artifact_not_found(engine_with_tmp):
    engine = engine_with_tmp
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.interact_artifact("nonexistent", action="click")


# ── artifact.sync ───────────────────────────────────────────


async def test_sync_artifact_to_code(engine_with_tmp, tmp_path):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1", name="doc.md", artifact_type="markdown", content="line1\nline2\n"
    )
    out_file = tmp_path / "out" / "synced.md"
    result = await engine.sync_artifact_file(
        artifact.id, str(out_file), "artifact_to_code"
    )
    assert result["ok"] is True
    assert result["direction"] == "artifact_to_code"
    assert out_file.read_text() == "line1\nline2\n"


async def test_sync_code_to_artifact(engine_with_tmp, tmp_path):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1", name="doc.md", artifact_type="markdown", content="original\n"
    )
    src_file = tmp_path / "src.md"
    src_file.write_text("updated from file\n")
    result = await engine.sync_artifact_file(
        artifact.id, str(src_file), "code_to_artifact"
    )
    assert result["ok"] is True
    ver = engine.get_version_content(artifact.id)
    assert ver.content == "updated from file\n"
    assert ver.version_num == 2


async def test_sync_invalid_direction(engine_with_tmp, tmp_path):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1", name="doc.md", artifact_type="markdown", content="x"
    )
    with pytest.raises(ValueError, match="direction must be"):
        await engine.sync_artifact_file(artifact.id, str(tmp_path / "f"), "bad_dir")


async def test_sync_artifact_not_found(engine_with_tmp, tmp_path):
    engine = engine_with_tmp
    with pytest.raises(ValueError, match="Artifact not found"):
        await engine.sync_artifact_file(
            "nonexistent", str(tmp_path / "f"), "artifact_to_code"
        )


async def test_sync_code_to_artifact_file_missing(engine_with_tmp, tmp_path):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1", name="doc.md", artifact_type="markdown", content="x"
    )
    with pytest.raises(ValueError, match="File not found"):
        await engine.sync_artifact_file(
            artifact.id, str(tmp_path / "missing.md"), "code_to_artifact"
        )


# ── RPC dispatch end-to-end ─────────────────────────────────


async def test_render_rpc_dispatch(engine_with_tmp):
    engine = engine_with_tmp
    handler = RPCHandler(engine)
    content = "<!DOCTYPE html>\n<html>" + ("y" * 2000) + "</html>"
    result = await handler.dispatch(
        "artifact.render",
        {"content": content, "session_id": "s1", "lang_hint": "html"},
    )
    assert result["created"] is True


async def test_check_safety_rpc_dispatch(engine_with_tmp):
    engine = engine_with_tmp
    handler = RPCHandler(engine)
    result = await handler.dispatch(
        "artifact.check_safety",
        {"messages": [{"role": "user", "content": "hi"}], "output_budget": 1000},
    )
    assert "safe" in result


async def test_inject_rpc_dispatch(engine_with_tmp):
    # 运维6: artifact.inject 占位方法下线，RPC 层显式拒绝 (-32005)
    engine = engine_with_tmp
    handler = RPCHandler(engine)
    with pytest.raises(NotImplementedError) as exc:
        await handler.dispatch(
            "artifact.inject",
            {"messages": [{"role": "user", "content": "hi"}], "output_budget": 1000},
        )
    assert exc.value.code == -32005


async def test_interact_rpc_dispatch(engine_with_tmp):
    # 运维6: artifact.interact 占位方法下线，RPC 层显式拒绝 (-32005)
    engine = engine_with_tmp
    handler = RPCHandler(engine)
    artifact, _, _ = await engine.create_artifact(
        session_id="s1", name="doc.md", artifact_type="markdown", content="x"
    )
    with pytest.raises(NotImplementedError) as exc:
        await handler.dispatch(
            "artifact.interact",
            {"artifact_id": artifact.id, "action": "click", "payload": {}},
        )
    assert exc.value.code == -32005


async def test_sync_rpc_dispatch(engine_with_tmp, tmp_path):
    engine = engine_with_tmp
    handler = RPCHandler(engine)
    artifact, _, _ = await engine.create_artifact(
        session_id="s1", name="doc.md", artifact_type="markdown", content="data\n"
    )
    out_file = tmp_path / "synced.md"
    result = await handler.dispatch(
        "artifact.sync",
        {
            "artifact_id": artifact.id,
            "file_path": str(out_file),
            "direction": "artifact_to_code",
        },
    )
    assert result["ok"] is True
    assert out_file.read_text() == "data\n"
