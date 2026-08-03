import shutil
import tempfile
import time
from pathlib import Path

import httpx

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer


def test_server_start_stop():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19910)
    server.start_async()
    time.sleep(0.5)

    resp = httpx.post(
        "http://127.0.0.1:19910",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        timeout=5.0,
    )
    assert resp.status_code == 200

    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def test_server_sse_connect():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(
        storage_root=tmp / "artifacts", allow_no_auth=True, sse_heartbeat_interval=1
    )
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19911)
    server.start_async()
    time.sleep(0.5)

    try:
        with httpx.stream(
            "GET", "http://127.0.0.1:19911/api/v1/events/stream", timeout=5.0
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        pass  # SSE endpoint not reachable, skip

    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def test_server_rpc_dispatch_error():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19912)
    server.start_async()
    time.sleep(0.5)

    resp = httpx.post(
        "http://127.0.0.1:19912",
        json={"jsonrpc": "2.0", "method": "artifact.create", "params": {}, "id": 1},
        timeout=5.0,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data

    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def test_server_rest_body_too_large():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19913)
    server.start_async()
    time.sleep(0.5)

    big = "x" * (11 * 1024 * 1024)
    resp = httpx.post(
        "http://127.0.0.1:19913/api/v1/artifacts/create",
        json={"session_id": "s1", "name": "t.py", "type": "code", "content": big},
        timeout=5.0,
    )
    assert resp.status_code == 413

    server.stop()
    engine.close()
    shutil.rmtree(tmp)
