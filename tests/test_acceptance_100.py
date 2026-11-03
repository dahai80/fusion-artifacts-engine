import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19921


@pytest.fixture
def handler():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    eng = ArtifactEngine(cfg)
    h = RPCHandler(eng)
    yield h, eng
    eng.close()
    shutil.rmtree(tmp)


@pytest.fixture(scope="module")
def rpc_server():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=PORT)
    server.start_async()
    time.sleep(0.5)
    yield server, engine
    server.stop()
    engine.close()
    shutil.rmtree(tmp)


# ── #100-1: svg / mermaid 类型 ────────────────────────────────


async def test_create_svg_artifact(handler):
    h, _ = handler
    r = await h.dispatch(
        "artifact.create",
        {"session_id": "s1", "name": "diagram", "type": "svg", "content": "<svg/>"},
    )
    assert r["artifact"]["type"] == "svg"
    assert r["artifact"]["kind"] == "app"


async def test_create_mermaid_artifact(handler):
    h, _ = handler
    r = await h.dispatch(
        "artifact.create",
        {"session_id": "s1", "name": "flow", "type": "mermaid", "content": "graph TD; A-->B;"},
    )
    assert r["artifact"]["type"] == "mermaid"
    assert r["artifact"]["kind"] == "document"


# ── #100-2: 事件持久化 ────────────────────────────────────────


async def test_events_persisted_after_create_and_update(handler):
    h, eng = handler
    r = await h.dispatch(
        "artifact.create",
        {"session_id": "s1", "name": "doc", "type": "markdown", "content": "# v1"},
    )
    aid = r["artifact"]["id"]
    await h.dispatch(
        "artifact.update",
        {"artifact_id": aid, "content": "# v2", "source": "manual"},
    )
    events, total = eng.list_events(artifact_id=aid)
    assert total >= 2
    types = {e.event_type for e in events}
    assert "artifact.created" in types
    assert "artifact.updated" in types


async def test_version_dump_excludes_internal_id(handler):
    h, _ = handler
    r = await h.dispatch(
        "artifact.create",
        {"session_id": "s1", "name": "doc", "type": "markdown", "content": "# v1"},
    )
    assert "id" not in r["version"]
    vl = await h.dispatch(
        "artifact.version_list", {"artifact_id": r["artifact"]["id"]}
    )
    for v in vl["versions"]:
        assert "id" not in v


# ── #100-3: REST 集合路径 + 事件读端点 ────────────────────────


def test_rest_post_collection_creates(rpc_server):
    resp = httpx.post(
        f"http://127.0.0.1:{PORT}/api/v1/artifacts",
        json={
            "session_id": "s_rest",
            "name": "rest-create",
            "type": "markdown",
            "content": "# via rest",
        },
        timeout=5.0,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["artifact"]["name"] == "rest-create"


def test_rest_get_artifact_events_nonempty(rpc_server):
    r = httpx.post(
        f"http://127.0.0.1:{PORT}/api/v1/artifacts",
        json={
            "session_id": "s_rest",
            "name": "rest-events",
            "type": "markdown",
            "content": "# ev",
        },
        timeout=5.0,
    ).json()
    aid = r["artifact"]["id"]
    # /api/v1/artifacts/{id}/events 已被 SSE 流（#55）占用，REST 读走集合路径
    resp = httpx.get(
        f"http://127.0.0.1:{PORT}/api/v1/events",
        params={"artifact_id": aid},
        timeout=5.0,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(e["event_type"] == "artifact.created" for e in body["events"])
