import socket
import time

import httpx

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.metrics import reset_metrics
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _server(tmp_path, **cfg_overrides):
    port = _free_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        allow_no_auth=True,
        **cfg_overrides,
    )
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    return server, eng, port


# ── 运维1: rate-limit 集成 (do_POST JSON-RPC) ────────────────


def test_jsonrpc_rate_limit_returns_429(tmp_path):
    # rps=1, burst=1 — 第二个请求应被限流
    server, eng, port = _server(tmp_path, rate_limit_rps=1, rate_limit_burst=1)
    try:
        url = f"http://127.0.0.1:{port}/"
        headers = {"Content-Type": "application/json"}
        r1 = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers, timeout=5)
        assert r1.status_code == 200
        # 立即发第二个——桶未补充，应 429
        r2 = httpx.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "ping"}, headers=headers, timeout=5)
        assert r2.status_code == 429
        body = r2.json()
        assert body["error"]["code"] == -32003
        assert body["error"]["retryable"] is True
    finally:
        server.stop()
        eng.close()


def test_jsonrpc_rate_limit_rps_zero_unlimited(tmp_path):
    # rps=0 = 无限，连发不限流
    server, eng, port = _server(tmp_path, rate_limit_rps=0, rate_limit_burst=0)
    try:
        url = f"http://127.0.0.1:{port}/"
        headers = {"Content-Type": "application/json"}
        for _ in range(10):
            r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers, timeout=5)
            assert r.status_code == 200
    finally:
        server.stop()
        eng.close()


# ── 运维1: public share rate-limit (do_GET) ────────────────


def test_public_share_rate_limit_returns_429(tmp_path):
    server, eng, port = _server(tmp_path, public_rate_limit_rps=1, public_rate_limit_burst=1)
    try:
        url = f"http://127.0.0.1:{port}/api/v1/share/shr_nonexistent"
        # 第一个请求消耗桶（404 但过了限流），第二个应 429
        r1 = httpx.get(url, timeout=5)
        assert r1.status_code in (404, 410)  # share 不存在，但未被限流
        r2 = httpx.get(url, timeout=5)
        assert r2.status_code == 429
        assert r2.json()["code"] == -32003
    finally:
        server.stop()
        eng.close()


# ── 运维2: /metrics 集成 ────────────────


def test_metrics_endpoint_prometheus_content(tmp_path):
    server, eng, port = _server(tmp_path)
    try:
        # 先发一个 RPC 触发计数器
        httpx.post(
            f"http://127.0.0.1:{port}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        r = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
        text = r.text
        assert "rpc_requests_total" in text
        assert "rpc_active_conns" in text
        # counter 类型声明
        assert "# TYPE rpc_requests_total counter" in text
        assert "# TYPE rpc_active_conns gauge" in text
    finally:
        server.stop()
        eng.close()


def test_metrics_disabled_returns_404(tmp_path):
    server, eng, port = _server(tmp_path, metrics_enabled=False)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
        assert r.status_code == 404
        assert r.json()["error"] == "metrics disabled"
    finally:
        server.stop()
        eng.close()


# ── 运维5: readyz storage 不可用 → 503 ────────────────


def test_readyz_503_when_storage_fails(tmp_path):
    server, eng, port = _server(tmp_path)
    try:
        # 篡改 storage.health_check 抛异常，模拟存储不可用
        eng.storage.health_check = lambda: False
        r = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5)
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["storage"] is False
    finally:
        server.stop()
        eng.close()


def test_readyz_503_when_storage_raises(tmp_path):
    # storage.health_check 抛异常（非返回 False）—— except 分支仍 503
    server, eng, port = _server(tmp_path)
    try:
        eng.storage.health_check = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        r = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5)
        assert r.status_code == 503
        assert r.json()["checks"]["storage"] is False
    finally:
        server.stop()
        eng.close()


# ── 运维6: inject/interact JSON-RPC 集成层 ────────────────


def test_jsonrpc_inject_returns_not_implemented(tmp_path):
    server, eng, port = _server(tmp_path)
    try:
        r = httpx.post(
            f"http://127.0.0.1:{port}/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "artifact.inject",
                "params": {"messages": [{"role": "user", "content": "hi"}], "output_budget": 1000},
            },
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["error"]["code"] == -32005
        assert "not implemented" in body["error"]["message"].lower()
    finally:
        server.stop()
        eng.close()


def test_jsonrpc_interact_returns_not_implemented(tmp_path):
    server, eng, port = _server(tmp_path)
    try:
        r = httpx.post(
            f"http://127.0.0.1:{port}/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "artifact.interact",
                "params": {"artifact_id": "art_x", "action": "click", "payload": {}},
            },
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["error"]["code"] == -32005
    finally:
        server.stop()
        eng.close()
        reset_metrics()
