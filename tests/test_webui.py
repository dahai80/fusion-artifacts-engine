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
    # dist 目录指到临时目录（不动真实 webui_dist），写最小 index + asset
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
    # #76: GET / 返回 index.html，带 CSP + no-store（入口页不可缓存）
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
    # #76: hash 文件名 asset 长缓存
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
    # #76: 路径穿越必须 404，不能读到 dist 外文件
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
    # #76: 无 dist（最小安装）时 GET / 保持 404，RPC/REST 不受影响
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
        # JSON-RPC POST / 仍然正常
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
    # #76: 包内真实构建产物存在（CI 防丢——dist 是 package-data，漏 commit 即菜单 404）
    dist = server_mod._WEBUI_DIST
    assert (dist / "index.html").is_file(), "webui_dist/index.html missing — run `bun run build` in webui/"
    assets = list((dist / "assets").glob("*.js"))
    assert assets, "webui_dist/assets/*.js missing"
