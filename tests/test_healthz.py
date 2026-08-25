import socket
import time

import httpx

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _server(tmp_path):
    port = _free_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        allow_no_auth=True,
    )
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    return server, eng, port


def test_healthz_returns_200(tmp_path):
    server, eng, port = _server(tmp_path)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["check"] == "liveness"
    finally:
        server.stop()
        eng.close()


def test_readyz_healthy_returns_200(tmp_path):
    server, eng, port = _server(tmp_path)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["checks"]["storage"] is True
        assert body["checks"]["event_bus"] is True
    finally:
        server.stop()
        eng.close()


def test_readyz_not_ready_when_event_bus_closed(tmp_path):
    server, eng, port = _server(tmp_path)
    try:
        eng.event_bus.shutdown()
        r = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5)
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["event_bus"] is False
    finally:
        server.stop()
        eng.close()


def test_healthz_no_auth_required(tmp_path):
    # healthz/readyz 应无鉴权（K8s probe 无 API key）
    server, eng, port = _server(tmp_path)
    try:
        # 显式不传 X-API-Key
        r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5, headers={})
        assert r.status_code == 200
    finally:
        server.stop()
        eng.close()


def test_storage_health_check_true(tmp_path):
    eng = ArtifactEngine(
        ArtifactEngineConfig(
            storage_root=tmp_path / "artifacts",
            server_host="127.0.0.1",
            server_port=0,
        )
    )
    try:
        assert eng.storage.health_check() is True
    finally:
        eng.close()


def test_event_bus_is_closed_toggle():
    from fusion_artifacts_engine.rpc.event_bus import EventBus

    bus = EventBus()
    assert bus.is_closed() is False
    bus.shutdown()
    assert bus.is_closed() is True
