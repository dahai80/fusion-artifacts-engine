import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19896


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


def rpc(method, params=None, req_id=1):
    resp = httpx.post(
        f"http://127.0.0.1:{PORT}",
        json={"jsonrpc": "2.0", "method": method, "params": params or {}, "id": req_id},
        timeout=5.0,
    )
    return resp.json()


def test_create_with_project_id(rpc_server):
    r = rpc(
        "artifact.create",
        {
            "session_id": "s_proj",
            "name": "proj_art.py",
            "type": "code",
            "content": "print('proj')",
            "project_id": "proj-1",
        },
    )
    assert "result" in r
    assert r["result"]["artifact"]["project_id"] == "proj-1"


def test_create_without_project_id(rpc_server):
    r = rpc(
        "artifact.create",
        {
            "session_id": "s_proj",
            "name": "no_proj.py",
            "type": "code",
            "content": "print('no proj')",
        },
    )
    assert "result" in r
    assert r["result"]["artifact"]["project_id"] is None


def test_list_filter_by_project_id(rpc_server):
    rpc(
        "artifact.create",
        {
            "session_id": "s_filter",
            "name": "pa.py",
            "type": "code",
            "content": "a",
            "project_id": "proj-a",
        },
    )
    rpc(
        "artifact.create",
        {
            "session_id": "s_filter",
            "name": "pb.py",
            "type": "code",
            "content": "b",
            "project_id": "proj-b",
        },
    )
    rpc(
        "artifact.create",
        {
            "session_id": "s_filter",
            "name": "pn.py",
            "type": "code",
            "content": "n",
        },
    )
    ra = rpc("artifact.list", {"session_id": "s_filter", "project_id": "proj-a"})
    assert all(a["project_id"] == "proj-a" for a in ra["result"]["artifacts"])
    rb = rpc("artifact.list", {"session_id": "s_filter", "project_id": "proj-b"})
    assert all(a["project_id"] == "proj-b" for a in rb["result"]["artifacts"])
    rall = rpc("artifact.list", {"session_id": "s_filter"})
    assert len(rall["result"]["artifacts"]) >= 3


def test_get_artifact_with_project_id(rpc_server):
    r = rpc(
        "artifact.create",
        {
            "session_id": "s_get_proj",
            "name": "gp.py",
            "type": "code",
            "content": "x",
            "project_id": "proj-get",
        },
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.get", {"artifact_id": art_id})
    assert r2["result"]["artifact"]["project_id"] == "proj-get"
