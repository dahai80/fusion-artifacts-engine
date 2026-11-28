import shutil
import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler


@pytest.fixture
def handler():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    eng = ArtifactEngine(cfg)
    h = RPCHandler(eng)
    yield h, eng
    eng.close()
    shutil.rmtree(tmp)


def _params(**overrides):
    base = {
        "session_id": "sess-1",
        "name": "Notes",
        "type": "markdown",
        "content": "# hello\n",
    }
    base.update(overrides)
    return base


async def test_create_with_idempotency_key_stamps_metadata(handler):
    h, eng = handler
    r = await h.dispatch("artifact.create", _params(idempotency_key="op-1"))
    arts = eng.storage.list_artifacts("sess-1")
    assert len(arts) == 1
    assert arts[0].metadata["_idempotency_key"] == "op-1"
    assert r["artifact"]["id"] == arts[0].id


async def test_replay_same_key_returns_original_artifact(handler):
    h, eng = handler
    first = await h.dispatch("artifact.create", _params(idempotency_key="op-1"))
    replay = await h.dispatch(
        "artifact.create",
        _params(idempotency_key="op-1", content="# DIFFERENT BODY\n"),
    )
    assert replay["artifact"]["id"] == first["artifact"]["id"]
    assert replay["deduplicated"] is True
    # the replayed content must NOT have been written
    assert len(eng.storage.list_artifacts("sess-1")) == 1
    stored = await h.dispatch(
        "artifact.get_content", {"artifact_id": first["artifact"]["id"]}
    )
    assert "# hello" in stored["content"]


async def test_same_key_different_session_creates_new(handler):
    h, eng = handler
    await h.dispatch("artifact.create", _params(idempotency_key="op-1"))
    r2 = await h.dispatch(
        "artifact.create", _params(idempotency_key="op-1", session_id="sess-2")
    )
    assert "deduplicated" not in r2
    assert len(eng.storage.list_artifacts("sess-1")) == 1
    assert len(eng.storage.list_artifacts("sess-2")) == 1


async def test_no_key_creates_normally(handler):
    h, eng = handler
    await h.dispatch("artifact.create", _params())
    r2 = await h.dispatch("artifact.create", _params())
    assert "deduplicated" not in r2
    assert len(eng.storage.list_artifacts("sess-1")) == 2
