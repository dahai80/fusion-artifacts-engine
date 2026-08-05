import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19897


@pytest.fixture(scope="module")
def share_server():
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


def _create_artifact_and_share(content="render me", atype="html"):
    art_id = rpc(
        "artifact.create",
        {"session_id": "s_share", "name": "s.html", "type": atype, "content": content},
    )["result"]["artifact"]["id"]
    share_resp = rpc("artifact.create_share", {"artifact_id": art_id, "created_by": "u1"})
    share_id = share_resp["result"]["share"]["share_id"]
    return art_id, share_id


def test_share_rest_ok(share_server):
    _server, engine = share_server
    art_id, share_id = _create_artifact_and_share(content="<html>hello</html>")
    resp = rest_get(f"/api/v1/share/{share_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["artifact"]["id"] == art_id
    assert data["content"] == "<html>hello</html>"
    assert data["content_type"] == "text/html"


def test_share_rest_not_found(share_server):
    resp = rest_get("/api/v1/share/shr_nonexistent")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"].lower()


def test_share_rest_revoked_gone(share_server):
    _server, engine = share_server
    _art_id, share_id = _create_artifact_and_share()
    rpc("artifact.revoke_share", {"share_id": share_id})
    resp = rest_get(f"/api/v1/share/{share_id}")
    assert resp.status_code == 410
    assert resp.json()["reason"] == "revoked"


def test_share_rest_expired_gone(share_server):
    _server, engine = share_server
    art_id = rpc(
        "artifact.create",
        {"session_id": "s_share_exp", "name": "exp.html", "type": "html", "content": "x"},
    )["result"]["artifact"]["id"]
    past_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    share_resp = rpc(
        "artifact.create_share",
        {"artifact_id": art_id, "created_by": "u1", "expires_at": past_iso},
    )
    share_id = share_resp["result"]["share"]["share_id"]
    resp = rest_get(f"/api/v1/share/{share_id}")
    assert resp.status_code == 410
    assert resp.json()["reason"] == "expired"


def test_share_rest_public_no_auth(share_server):
    _server, engine = share_server
    _art_id, share_id = _create_artifact_and_share()
    engine.config.allow_no_auth = False
    try:
        resp = rest_get(f"/api/v1/share/{share_id}")
        assert resp.status_code == 200
    finally:
        engine.config.allow_no_auth = True


def test_share_rest_markdown_content_type(share_server):
    _server, engine = share_server
    _art_id, share_id = _create_artifact_and_share(content="# Title", atype="markdown")
    resp = rest_get(f"/api/v1/share/{share_id}")
    assert resp.status_code == 200
    assert resp.json()["content_type"] == "text/markdown"
