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

    received = b""
    try:
        with httpx.stream(
            "GET", "http://127.0.0.1:19911/api/v1/events/stream", timeout=5.0
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            for chunk in resp.iter_text():
                received += chunk.encode()
                if b"heartbeat" in received:
                    break
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        pass  # SSE endpoint not reachable, skip

    assert b"heartbeat" in received, "SSE must deliver heartbeat frames after connect"

    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def test_server_sse_delivers_event():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(
        storage_root=tmp / "artifacts", allow_no_auth=True, sse_heartbeat_interval=1
    )
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19914)
    server.start_async()
    time.sleep(0.5)

    received = b""
    try:
        with httpx.stream(
            "GET", "http://127.0.0.1:19914/api/v1/events/stream", timeout=5.0
        ) as resp:
            assert resp.status_code == 200
            time.sleep(0.3)
            rpc = httpx.post(
                "http://127.0.0.1:19914/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "artifact.emit_event",
                    "params": {
                        "event_type": "sse.acceptance",
                        "payload": {"marker": "live"},
                    },
                },
                timeout=5.0,
            )
            assert rpc.status_code == 200
            for chunk in resp.iter_text():
                received += chunk.encode()
                if b"sse.acceptance" in received:
                    break
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        pass

    assert b"event: artifact" in received, "SSE must frame events as event: artifact"
    assert b"sse.acceptance" in received, "SSE must deliver emitted event to subscribers"

    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def test_server_sse_kind_filter():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(
        storage_root=tmp / "artifacts", allow_no_auth=True, sse_heartbeat_interval=1
    )
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19915)
    server.start_async()
    time.sleep(0.5)

    received = b""
    doc_id = ""
    code_id = ""
    try:
        with httpx.stream(
            "GET",
            "http://127.0.0.1:19915/api/v1/events/stream?kind=document",
            timeout=5.0,
        ) as resp:
            assert resp.status_code == 200
            time.sleep(0.3)
            create = httpx.post(
                "http://127.0.0.1:19915/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "artifact.create",
                    "params": {
                        "session_id": "s1",
                        "name": "doc-art",
                        "type": "markdown",
                        "kind": "document",
                        "content": "# doc",
                    },
                },
                timeout=5.0,
            )
            assert create.status_code == 200
            doc_id = create.json()["result"]["artifact"]["id"]
            create_code = httpx.post(
                "http://127.0.0.1:19915/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "artifact.create",
                    "params": {
                        "session_id": "s1",
                        "name": "code-art",
                        "type": "code",
                        "kind": "tool",
                        "content": "print('x')",
                    },
                },
                timeout=5.0,
            )
            assert create_code.status_code == 200
            code_id = create_code.json()["result"]["artifact"]["id"]
            for chunk in resp.iter_text():
                received += chunk.encode()
                if doc_id.encode() in received:
                    break
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        pass

    assert (
        doc_id.encode() in received
    ), "SSE kind=document must deliver document artifact event"
    assert (
        code_id.encode() not in received
    ), "SSE kind=document must filter out non-document artifacts"

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
