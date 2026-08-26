import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import PermissionError
from fusion_artifacts_engine.rpc.methods import RPCHandler


@pytest.fixture
def engine_with_tmp(tmp_path):
    cfg = ArtifactEngineConfig(storage_root=str(tmp_path), sync_root=str(tmp_path))
    return ArtifactEngine(config=cfg)


@pytest.fixture
def handler(engine_with_tmp):
    return RPCHandler(engine_with_tmp)


async def _make_owned(engine, owner_user_id, name="doc.md"):
    art, _, _ = await engine.create_artifact(
        session_id="s1",
        name=name,
        artifact_type="markdown",
        content="hello",
        owner_user_id=owner_user_id,
    )
    return art


# ── allow: caller == owner ────────────────────────────────


async def test_get_owner_allowed(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1")
    result = await handler.dispatch(
        "artifact.get", {"artifact_id": art.id, "caller_user_id": "u1"}
    )
    assert result["artifact"]["id"] == art.id


async def test_update_owner_allowed(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    result = await handler.dispatch(
        "artifact.update",
        {
            "artifact_id": art.id,
            "content": "new",
            "caller_user_id": "u1",
        },
    )
    assert result["version"]["version_num"] == 2


async def test_delete_owner_allowed(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    result = await handler.dispatch(
        "artifact.delete",
        {"artifact_id": art.id, "caller_user_id": "u1"},
    )
    assert result["ok"] is True


# ── deny: caller != owner ─────────────────────────────────


async def test_get_wrong_owner_denied(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1")
    with pytest.raises(PermissionError) as exc:
        await handler.dispatch(
            "artifact.get", {"artifact_id": art.id, "caller_user_id": "u2"}
        )
    assert exc.value.code == -32006


async def test_update_wrong_owner_denied(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    with pytest.raises(PermissionError) as exc:
        await handler.dispatch(
            "artifact.update",
            {
                "artifact_id": art.id,
                "content": "evil",
                "caller_user_id": "u2",
            },
        )
    assert exc.value.code == -32006
    # 确认内容未被改写
    ver = engine_with_tmp.get_version_content(art.id)
    assert ver.content == "hello"


async def test_delete_wrong_owner_denied(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    with pytest.raises(PermissionError) as exc:
        await handler.dispatch(
            "artifact.delete",
            {"artifact_id": art.id, "caller_user_id": "u2"},
        )
    assert exc.value.code == -32006
    # 确认产物仍在
    got = engine_with_tmp.get_artifact(art.id)
    assert got is not None


async def test_patch_wrong_owner_denied(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    with pytest.raises(PermissionError) as exc:
        await handler.dispatch(
            "artifact.patch",
            {
                "artifact_id": art.id,
                "operation": "append",
                "content": "evil",
                "caller_user_id": "u2",
            },
        )
    assert exc.value.code == -32006


async def test_rollback_wrong_owner_denied(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    # 先建 v2 以便回滚有目标
    await handler.dispatch(
        "artifact.update",
        {"artifact_id": art.id, "content": "v2", "caller_user_id": "u1"},
    )
    with pytest.raises(PermissionError) as exc:
        await handler.dispatch(
            "artifact.version_rollback",
            {
                "artifact_id": art.id,
                "target_version": 1,
                "caller_user_id": "u2",
            },
        )
    assert exc.value.code == -32006


async def test_get_content_wrong_owner_denied(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    with pytest.raises(PermissionError) as exc:
        await handler.dispatch(
            "artifact.get_content",
            {"artifact_id": art.id, "caller_user_id": "u2"},
        )
    assert exc.value.code == -32006


# ── skip: no caller_user_id (single-tenant back-compat) ──


async def test_get_no_caller_skips_check(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1")
    result = await handler.dispatch("artifact.get", {"artifact_id": art.id})
    assert result["artifact"]["id"] == art.id


async def test_update_no_caller_skips_check(handler, engine_with_tmp):
    art = await _make_owned(engine_with_tmp, "u1", name="d.md")
    result = await handler.dispatch(
        "artifact.update", {"artifact_id": art.id, "content": "x"}
    )
    assert result["version"]["version_num"] == 2


# ── skip: owner_user_id is None ───────────────────────────


async def test_get_no_owner_skips_check(handler, engine_with_tmp):
    # 默认 create_artifact 不传 owner_user_id → owner=None，任何 caller 放行
    art, _, _ = await engine_with_tmp.create_artifact(
        session_id="s1", name="d.md", artifact_type="markdown", content="x"
    )
    result = await handler.dispatch(
        "artifact.get", {"artifact_id": art.id, "caller_user_id": "anyone"}
    )
    assert result["artifact"]["id"] == art.id
