import asyncio
import json
import socket
import time
import urllib.error
import urllib.request

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

API_KEY = "fae-share-page-test-key"


def _ephemeral_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _engine(tmp_path, port):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        api_key=API_KEY,
        gateway_enabled=True,
        share_public_base_url="http://share.example.test",
    )
    return ArtifactEngine(config)


def _make_artifact(engine):
    artifact, _version, _ref = asyncio.run(
        engine.create_artifact(
            "s-share-page",
            "Share Page Demo",
            "markdown",
            "# hello from share page",
        )
    )
    return artifact


def _serve(engine, port):
    server = ArtifactRPCServer(engine, port=port)
    server.start_async()
    time.sleep(0.5)
    return server


def test_share_page_view_anonymous(tmp_path):
    port = _ephemeral_port()
    engine = _engine(tmp_path, port)
    artifact = _make_artifact(engine)
    share = engine.create_share(artifact.id, permission="view")
    server = _serve(engine, port)
    try:
        status, body = _get(f"http://127.0.0.1:{port}/s/{share.share_id}")
        assert status == 200
        assert "hello from share page" in body
        assert "Remix to my library" not in body
    finally:
        server.stop()
        engine.close()


def test_share_page_remix_permission_has_entry(tmp_path):
    port = _ephemeral_port()
    engine = _engine(tmp_path, port)
    artifact = _make_artifact(engine)
    share = engine.create_share(artifact.id, permission="remix")
    server = _serve(engine, port)
    try:
        status, body = _get(f"http://127.0.0.1:{port}/s/{share.share_id}")
        assert status == 200
        assert "Remix to my library" in body
        assert f"#/s/{share.share_id}?remix=1" in body
    finally:
        server.stop()
        engine.close()


def test_share_page_revoked_is_gone(tmp_path):
    port = _ephemeral_port()
    engine = _engine(tmp_path, port)
    artifact = _make_artifact(engine)
    share = engine.create_share(artifact.id, permission="view")
    engine.revoke_share(share.share_id)
    server = _serve(engine, port)
    try:
        status, body = _get(f"http://127.0.0.1:{port}/s/{share.share_id}")
        assert status == 410
        assert "hello from share page" not in body
        assert "expired" in body
    finally:
        server.stop()
        engine.close()


def test_share_page_unknown_is_404(tmp_path):
    port = _ephemeral_port()
    engine = _engine(tmp_path, port)
    server = _serve(engine, port)
    try:
        status, body = _get(f"http://127.0.0.1:{port}/s/shr_000000000000")
        assert status == 404
        assert "does not exist" in body
        status, _ = _get(f"http://127.0.0.1:{port}/s/not-a-share")
        assert status == 404
    finally:
        server.stop()
        engine.close()


def test_share_base_endpoint(tmp_path):
    port = _ephemeral_port()
    engine = _engine(tmp_path, port)
    server = _serve(engine, port)
    try:
        status, body = _get(
            f"http://127.0.0.1:{port}/api/v1/share-base",
            headers={"X-API-Key": API_KEY},
        )
        assert status == 200
        assert json.loads(body)["base_url"] == "http://share.example.test"
        status, _ = _get(f"http://127.0.0.1:{port}/api/v1/share-base")
        assert status == 401
    finally:
        server.stop()
        engine.close()


def test_create_share_returns_page_url(tmp_path):
    port = _ephemeral_port()
    engine = _engine(tmp_path, port)
    artifact = _make_artifact(engine)
    share = engine.create_share(artifact.id, permission="view")
    base = engine.config.share_base_url
    assert base == "http://share.example.test"
    assert f"{base}/s/{share.share_id}".endswith(f"/s/{share.share_id}")
