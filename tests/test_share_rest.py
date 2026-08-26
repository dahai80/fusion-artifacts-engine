import shutil
import socket
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer


def _free_port():
    # P0-9/M26: 固定端口在并发/重跑下撞 TIME_WAIT → Errno 48。动态取空闲端口。
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def share_server():
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=port)
    server.start_async()
    time.sleep(0.5)
    yield server, engine, base
    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def rpc(base, method, params=None, req_id=1):
    return httpx.post(
        base,
        json={"jsonrpc": "2.0", "method": method, "params": params or {}, "id": req_id},
        timeout=5.0,
    ).json()


def rest_get(base, path, **kwargs):
    return httpx.get(f"{base}{path}", timeout=5.0, **kwargs)


def _create_artifact_and_share(base, content="render me", atype="html"):
    art_id = rpc(
        base,
        "artifact.create",
        {"session_id": "s_share", "name": "s.html", "type": atype, "content": content},
    )["result"]["artifact"]["id"]
    share_resp = rpc(base, "artifact.create_share", {"artifact_id": art_id, "created_by": "u1"})
    share_id = share_resp["result"]["share"]["share_id"]
    return art_id, share_id


def test_share_rest_ok(share_server):
    _server, _engine, base = share_server
    art_id, share_id = _create_artifact_and_share(base, content="<html>hello</html>")
    resp = rest_get(base, f"/api/v1/share/{share_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["artifact"]["id"] == art_id
    assert "rendered_html" in data
    assert "content" not in data
    assert "<iframe" in data["rendered_html"]
    assert data["content_type"] == "text/html"


def test_share_rest_not_found(share_server):
    _server, _engine, base = share_server
    resp = rest_get(base, "/api/v1/share/shr_nonexistent")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"].lower()


def test_share_rest_revoked_gone(share_server):
    _server, _engine, base = share_server
    _art_id, share_id = _create_artifact_and_share(base)
    rpc(base, "artifact.revoke_share", {"share_id": share_id})
    resp = rest_get(base, f"/api/v1/share/{share_id}")
    assert resp.status_code == 410
    assert resp.json()["reason"] == "revoked"


def test_share_rest_expired_gone(share_server):
    _server, engine, base = share_server
    art_id = rpc(
        base,
        "artifact.create",
        {"session_id": "s_share_exp", "name": "exp.html", "type": "html", "content": "x"},
    )["result"]["artifact"]["id"]
    # L-6 后 create_share 拒绝过去日期；直接构造过期 share 测试读取路径 fail-closed
    import uuid

    from fusion_artifacts_engine.models import ArtifactShare
    past_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    share_id = f"shr_{uuid.uuid4().hex[:12]}"
    engine.storage.save_share(
        ArtifactShare(
            share_id=share_id,
            artifact_id=art_id,
            created_by="u1",
            created_at=time.time(),
            expires_at=past_iso,
        )
    )
    engine.storage.set_artifact_share_id(art_id, share_id)
    resp = rest_get(base, f"/api/v1/share/{share_id}")
    assert resp.status_code == 410
    assert resp.json()["reason"] == "expired"


def test_share_rest_public_no_auth(share_server):
    _server, engine, base = share_server
    _art_id, share_id = _create_artifact_and_share(base)
    engine.config.allow_no_auth = False
    try:
        resp = rest_get(base, f"/api/v1/share/{share_id}")
        assert resp.status_code == 200
    finally:
        engine.config.allow_no_auth = True


def test_share_rest_markdown_rendered(share_server):
    _server, _engine, base = share_server
    _art_id, share_id = _create_artifact_and_share(base, content="# Title", atype="markdown")
    resp = rest_get(base, f"/api/v1/share/{share_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["content_type"] == "text/html"
    assert "<h1>Title</h1>" in data["rendered_html"]
    assert "content" not in data
