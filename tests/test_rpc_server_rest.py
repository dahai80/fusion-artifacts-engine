import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19898


@pytest.fixture(scope="module")
def rest_server():
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


BASE = f"http://127.0.0.1:{PORT}"


def rpc(method, params=None, req_id=1):
    return httpx.post(
        BASE,
        json={"jsonrpc": "2.0", "method": method, "params": params or {}, "id": req_id},
        timeout=5.0,
    ).json()


def rest_get(path, **kwargs):
    return httpx.get(f"{BASE}{path}", timeout=5.0, **kwargs)


def rest_post(path, data=None, **kwargs):
    return httpx.post(f"{BASE}{path}", json=data or {}, timeout=5.0, **kwargs)


def _create_artifact(session_id="s_rest", name="r.py", atype="code", content="rest"):
    r = rpc(
        "artifact.create",
        {"session_id": session_id, "name": name, "type": atype, "content": content},
    )
    return r["result"]["artifact"]["id"]


# ── REST GET: list artifacts by session ──


def test_rest_list_artifacts_by_session(rest_server):
    _create_artifact("s_rest_list", "rl.py")
    resp = rest_get("/api/v1/artifacts?session_id=s_rest_list")
    assert resp.status_code == 200
    data = resp.json()
    assert "artifacts" in data
    assert "total" in data


# ── REST GET: list all artifacts (no session) ──


def test_rest_list_all_artifacts(rest_server):
    resp = rest_get("/api/v1/artifacts")
    assert resp.status_code == 200
    data = resp.json()
    assert "artifacts" in data


# ── REST GET: get single artifact ──


def test_rest_get_artifact(rest_server):
    art_id = _create_artifact("s_rest_get", "get.py")
    resp = rest_get(f"/api/v1/artifacts/{art_id}")
    assert resp.status_code == 200
    assert resp.json()["artifact"]["id"] == art_id


# ── REST GET: artifact not found ──


def test_rest_get_artifact_not_found(rest_server):
    resp = rest_get("/api/v1/artifacts/nonexistent_id")
    assert resp.status_code == 404


# ── REST GET: list versions ──


def test_rest_list_versions(rest_server):
    art_id = _create_artifact("s_rest_ver", "ver.py")
    rpc("artifact.update", {"artifact_id": art_id, "content": "v2"})
    resp = rest_get(f"/api/v1/artifacts/{art_id}/versions")
    assert resp.status_code == 200
    assert "versions" in resp.json()


# ── REST GET: get specific version ──


def test_rest_get_version(rest_server):
    art_id = _create_artifact("s_rest_ver2", "ver2.py")
    resp = rest_get(f"/api/v1/artifacts/{art_id}/versions/1")
    assert resp.status_code == 200
    assert "version" in resp.json()


# ── REST GET: version not found ──


def test_rest_get_version_not_found(rest_server):
    art_id = _create_artifact("s_rest_ver3", "ver3.py")
    resp = rest_get(f"/api/v1/artifacts/{art_id}/versions/999")
    assert resp.status_code == 404


# ── REST GET: invalid version number ──


def test_rest_get_version_invalid(rest_server):
    art_id = _create_artifact("s_rest_ver4", "ver4.py")
    resp = rest_get(f"/api/v1/artifacts/{art_id}/versions/abc")
    assert resp.status_code == 400


# ── REST GET: 404 unknown path ──


def test_rest_get_unknown(rest_server):
    resp = rest_get("/api/v1/unknown")
    assert resp.status_code == 404


def test_rest_get_root_404(rest_server):
    resp = rest_get("/unknown")
    assert resp.status_code == 404


# ── REST POST: create artifact via REST ──
# REST create uses /api/v1/artifacts/create path (path_parts[3]="create", len=4)


def test_rest_create_artifact(rest_server):
    resp = rest_post(
        "/api/v1/artifacts/create",
        {
            "session_id": "s_rest_create",
            "name": "create.py",
            "type": "code",
            "content": "new",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "artifact" in data
    assert "version" in data
    assert "ref_text" in data


# ── REST POST: update artifact version via REST ──


def test_rest_update_artifact(rest_server):
    art_id = _create_artifact("s_rest_update", "update.py")
    resp = rest_post(
        f"/api/v1/artifacts/{art_id}",
        {"content": "updated content v2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "ref_text" in data
    assert data["version"]["version_num"] == 2


# ── REST POST: delete artifact via REST ──


def test_rest_delete_artifact(rest_server):
    art_id = _create_artifact("s_rest_delete", "delete.py")
    resp = rest_post(
        f"/api/v1/artifacts/{art_id}",
        {"action": "delete", "soft_delete": True},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── REST POST: 404 unknown path ──


def test_rest_post_unknown(rest_server):
    resp = rest_post("/api/v1/unknown/path", {})
    assert resp.status_code == 404


# ── REST POST: invalid JSON ──


def test_rest_post_invalid_json(rest_server):
    resp = httpx.post(
        f"{BASE}/api/v1/artifacts/create",
        content=b"not json",
        headers={"Content-Type": "application/json"},
        timeout=5.0,
    )
    assert resp.status_code == 400


# ── JSON-RPC: parse error ──


def test_rpc_parse_error(rest_server):
    resp = httpx.post(
        BASE,
        content=b"not json",
        headers={"Content-Type": "application/json"},
        timeout=5.0,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["code"] == -32700


# ── JSON-RPC: invalid request ──


def test_rpc_invalid_request(rest_server):
    resp = httpx.post(BASE, json={"not_jsonrpc": True}, timeout=5.0)
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["code"] == -32600


# ── JSON-RPC: body too large ──


def test_rpc_body_too_large(rest_server):
    big = "x" * (11 * 1024 * 1024)
    resp = httpx.post(
        BASE,
        json={"jsonrpc": "2.0", "method": "ping", "id": 1, "params": {"d": big}},
        timeout=5.0,
    )
    assert resp.status_code == 413


# ── REST: list with project_id ──


def test_rest_list_with_project(rest_server):
    rpc(
        "artifact.create",
        {
            "session_id": "s_rest_proj",
            "name": "proj.py",
            "type": "code",
            "content": "x",
            "project_id": "proj1",
        },
    )
    resp = rest_get("/api/v1/artifacts?session_id=s_rest_proj&project_id=proj1")
    assert resp.status_code == 200


# ── REST: list with include_deleted ──


def test_rest_list_include_deleted(rest_server):
    art_id = _create_artifact("s_rest_inc_del", "inc_del.py")
    rpc("artifact.delete", {"artifact_id": art_id, "soft_delete": True})
    resp = rest_get("/api/v1/artifacts?session_id=s_rest_inc_del&include_deleted=true")
    assert resp.status_code == 200
