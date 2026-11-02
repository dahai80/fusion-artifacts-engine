import asyncio
import json
import socket
import time
import urllib.error
import urllib.request

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

API_KEY = "fae-remix-e2e-key"


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


def _post_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _setup(tmp_path, port):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        api_key=API_KEY,
        gateway_enabled=True,
        gateway_url="http://127.0.0.1:11432",
    )
    engine = ArtifactEngine(config)
    artifact, _v, _ref = asyncio.run(
        engine.create_artifact(
            "s-remix-e2e", "Remix E2E Source", "markdown", "# remix me"
        )
    )
    share = engine.create_share(artifact.id, permission="remix")
    server = ArtifactRPCServer(engine, port=port)
    server.start_async()
    time.sleep(0.5)
    return engine, server, artifact, share


def _stop(engine, server):
    server.stop()
    engine.close()


def test_share_page_offers_gateway_deep_link(tmp_path):
    port = _ephemeral_port()
    engine, server, _artifact, share = _setup(tmp_path, port)
    try:
        status, body = _get(f"http://127.0.0.1:{port}/s/{share.share_id}")
        assert status == 200
        assert (
            f"http://127.0.0.1:11432/shell/fusion-artifacts/"
            f"#/s/{share.share_id}?remix=1" in body
        )
    finally:
        _stop(engine, server)


def test_public_share_json_feeds_gui_remix(tmp_path):
    port = _ephemeral_port()
    engine, server, artifact, share = _setup(tmp_path, port)
    try:
        status, body = _get(
            f"http://127.0.0.1:{port}/api/v1/share/{share.share_id}"
        )
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["artifact"]["id"] == artifact.id
        assert data["share"]["permission"] == "remix"
        assert "remix me" in data["rendered_html"]
    finally:
        _stop(engine, server)


def test_authenticated_remix_lands_in_library_with_lineage(tmp_path):
    port = _ephemeral_port()
    engine, server, artifact, _share = _setup(tmp_path, port)
    try:
        base = f"http://127.0.0.1:{port}"
        auth = {"X-API-Key": API_KEY}
        status, resp = _post_json(
            f"{base}/",
            {"jsonrpc": "2.0", "id": 1, "method": "artifact.remix",
             "params": {"artifact_id": artifact.id}},
            headers=auth,
        )
        assert status == 200
        child_id = resp["result"]["artifact"]["id"]
        assert child_id != artifact.id
        _status, body = _get(f"{base}/api/v1/artifacts", headers=auth)
        listed = json.loads(body)
        names = [a["name"] for a in listed["artifacts"]]
        assert any("Remix E2E Source" in n for n in names)
        _status, resp = _post_json(
            f"{base}/",
            {"jsonrpc": "2.0", "id": 2, "method": "artifact.get",
             "params": {"artifact_id": child_id}},
            headers=auth,
        )
        assert resp["result"]["artifact"]["parent_artifact_id"] == artifact.id
    finally:
        _stop(engine, server)


def test_unauthenticated_remix_rejected(tmp_path):
    port = _ephemeral_port()
    engine, server, artifact, _share = _setup(tmp_path, port)
    try:
        status, resp = _post_json(
            f"http://127.0.0.1:{port}/",
            {"jsonrpc": "2.0", "id": 1, "method": "artifact.remix",
             "params": {"artifact_id": artifact.id}},
        )
        assert status == 401
        assert resp["error"]["code"] == -32600
    finally:
        _stop(engine, server)
