import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer
from fusion_artifacts_engine.storage.sqlite_storage import _SCHEMA_SQL

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


# ── #100-1: svg / mermaid types ──────────────────────────────


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


# ── #100-2: event persistence ────────────────────────────────


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


# ── #100-3: REST collection path + event read endpoint ───────


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
    # /api/v1/artifacts/{id}/events is taken by the SSE stream (#55); REST reads go through the collection path
    resp = httpx.get(
        f"http://127.0.0.1:{PORT}/api/v1/events",
        params={"artifact_id": aid},
        timeout=5.0,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(e["event_type"] == "artifact.created" for e in body["events"])


# ── #100-1 migration regression: relaxed CHECK on old DBs + child table data
#    integrity (the first rename-table migration rewrote child-table FK
#    references to a dropped temp table name, breaking all production inserts;
#    see the fk_repair test) ──


def _seed_db(root: Path, ddl: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "meta.db")
    conn.executescript(ddl)
    now = time.time()
    conn.execute(
        "INSERT INTO artifacts (id, session_id, name, type, created_at, updated_at) "
        "VALUES ('art_old1', 's1', 'old', 'markdown', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO artifact_versions (artifact_id, version_num, content, created_at) "
        "VALUES ('art_old1', 1, 'old v1', ?)",
        (now,),
    )
    conn.commit()
    conn.close()


async def test_migration_relaxes_check_keeps_children(tmp_path):
    ddl = _SCHEMA_SQL.replace(
        "type IN ('code','markdown','html','react','svg','mermaid','data')",
        "type IN ('code','markdown','html','react','data')",
    )
    root = tmp_path / "artifacts"
    _seed_db(root, ddl)
    eng = ArtifactEngine(ArtifactEngineConfig(storage_root=root, allow_no_auth=True))
    h = RPCHandler(eng)
    try:
        versions = eng.list_versions("art_old1")
        assert len(versions) == 1
        assert versions[0].content == "old v1"
        r = await h.dispatch(
            "artifact.create",
            {"session_id": "s1", "name": "svg2", "type": "svg", "content": "<svg/>"},
        )
        assert r["artifact"]["type"] == "svg"
        await h.dispatch(
            "artifact.update", {"artifact_id": "art_old1", "content": "new"}
        )
        assert len(eng.list_versions("art_old1")) == 2
    finally:
        eng.close()
        shutil.rmtree(tmp_path)


async def test_migration_fk_repair(tmp_path):
    # Real production corruption shape: ALTER RENAME rewrote references with quotes
    ddl = _SCHEMA_SQL.replace(
        'REFERENCES artifacts(id)', 'REFERENCES "artifacts_type_check_tmp"(id)'
    )
    root = tmp_path / "artifacts"
    _seed_db(root, ddl)
    eng = ArtifactEngine(ArtifactEngineConfig(storage_root=root, allow_no_auth=True))
    h = RPCHandler(eng)
    try:
        assert len(eng.list_versions("art_old1")) == 1
        await h.dispatch(
            "artifact.update", {"artifact_id": "art_old1", "content": "v2"}
        )
        assert len(eng.list_versions("art_old1")) == 2
    finally:
        eng.close()
        shutil.rmtree(tmp_path)
