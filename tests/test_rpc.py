import time
import tempfile
import shutil
from pathlib import Path
import pytest
import httpx
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19894


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


def test_ping(rpc_server):
    r = rpc("ping")
    assert r["result"]["pong"] is True


def test_create_and_get(rpc_server):
    r = rpc("artifact.create", {
        "session_id": "s_rpc",
        "name": "test.py",
        "type": "code",
        "content": "print('hello')",
        "summary": "test",
    })
    assert "result" in r
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.get", {"artifact_id": art_id})
    assert r2["result"]["artifact"]["name"] == "test.py"


def test_list(rpc_server):
    rpc("artifact.create", {
        "session_id": "s_rpc_list",
        "name": "list_test.py",
        "type": "code",
        "content": "code",
    })
    r = rpc("artifact.list", {"session_id": "s_rpc_list"})
    assert len(r["result"]["artifacts"]) >= 1


def test_update(rpc_server):
    r = rpc("artifact.create", {
        "session_id": "s_rpc_upd",
        "name": "upd.py",
        "type": "code",
        "content": "v1",
    })
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.update", {"artifact_id": art_id, "content": "v2", "change_log": "update"})
    assert r2["result"]["version"]["version_num"] == 2


def test_get_content(rpc_server):
    r = rpc("artifact.create", {
        "session_id": "s_rpc_gc",
        "name": "gc.py",
        "type": "code",
        "content": "the content",
    })
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.get_content", {"artifact_id": art_id})
    assert r2["result"]["content"] == "the content"


def test_delete(rpc_server):
    r = rpc("artifact.create", {
        "session_id": "s_rpc_del",
        "name": "del.py",
        "type": "code",
        "content": "bye",
    })
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.delete", {"artifact_id": art_id})
    assert r2["result"]["ok"] is True




def test_unknown_method(rpc_server):
    r = rpc("no.such.method")
    assert r["error"]["code"] == -32601
