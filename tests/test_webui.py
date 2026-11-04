import json
import socket
import time
import urllib.error
import urllib.request

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc import server as server_mod
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer


def _ephemeral_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _start(tmp_path, monkeypatch):
    # Point the dist dir at a temp dir (leave real webui_dist untouched); write a minimal index + asset
    fake_dist = tmp_path / "dist"
    (fake_dist / "assets").mkdir(parents=True)
    (fake_dist / "index.html").write_text("<html><body>fae-webui-test</body></html>")
    (fake_dist / "assets" / "app.js").write_text("console.log('ok');")
    monkeypatch.setattr(server_mod, "_WEBUI_DIST", fake_dist)

    port = _ephemeral_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        allow_no_auth=True,
    )
    eng = ArtifactEngine(config)
    srv = ArtifactRPCServer(eng, port=port)
    srv.start_async()
    time.sleep(0.5)
    return srv, eng, port


def test_webui_index_served(tmp_path, monkeypatch):
    # #76: GET / serves index.html with CSP + no-store (entry page must not be cached)
    srv, eng, port = _start(tmp_path, monkeypatch)
    try:
        status, headers, body = _get(port, "/")
        assert status == 200
        assert b"fae-webui-test" in body
        assert "text/html" in headers.get("Content-Type", "")
        assert "no-store" in headers.get("Cache-Control", "")
        assert "frame-ancestors" in headers.get("Content-Security-Policy", "")
        assert "http://127.0.0.1:11432" in headers.get("Content-Security-Policy", "")
    finally:
        srv.stop()
        eng.close()


def test_webui_asset_served_immutable(tmp_path, monkeypatch):
    # #76: hashed-filename assets get long caching
    srv, eng, port = _start(tmp_path, monkeypatch)
    try:
        status, headers, body = _get(port, "/assets/app.js")
        assert status == 200
        assert b"console.log" in body
        assert "immutable" in headers.get("Cache-Control", "")
        assert "javascript" in headers.get("Content-Type", "")
    finally:
        srv.stop()
        eng.close()


def test_webui_path_traversal_rejected(tmp_path, monkeypatch):
    # #76: path traversal must 404, no reads outside dist
    srv, eng, port = _start(tmp_path, monkeypatch)
    try:
        status, _, _ = _get(port, "/assets/..%2f..%2fconfig.py")
        assert status == 404
        status, _, _ = _get(port, "/assets/%2e%2e%2f%2e%2e%2fengine.py")
        assert status == 404
    finally:
        srv.stop()
        eng.close()


def test_webui_missing_dist_keeps_404(tmp_path, monkeypatch):
    # #76: with no dist (minimal install) GET / stays 404, RPC/REST unaffected
    monkeypatch.setattr(
        server_mod, "_WEBUI_DIST", tmp_path / "no-such-dist"
    )
    port = _ephemeral_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        allow_no_auth=True,
    )
    eng = ArtifactEngine(config)
    srv = ArtifactRPCServer(eng, port=port)
    srv.start_async()
    time.sleep(0.5)
    try:
        status, _, _ = _get(port, "/")
        assert status == 404
        # JSON-RPC POST / still works
        body = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        assert data["result"]["pong"] is True
    finally:
        srv.stop()
        eng.close()


def test_webui_real_dist_shipped():
    # #76: real build artifacts ship inside the package (CI guard against loss — dist is package-data; a missed commit breaks the menu with 404s)
    dist = server_mod._WEBUI_DIST
    assert (dist / "index.html").is_file(), "webui_dist/index.html missing — run `bun run build` in webui/"
    assets = list((dist / "assets").glob("*.js"))
    assert assets, "webui_dist/assets/*.js missing"
