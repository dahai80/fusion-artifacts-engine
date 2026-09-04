import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

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


def _make_server():
    # allow_no_auth=True 让 SSE 握手免 key，测试聚焦 scope 过滤而非鉴权。
    # heartbeat=1 保证流上周期有数据，_drain 读到目标事件或超时即关流。
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(
        storage_root=tmp / "artifacts",
        allow_no_auth=True,
        sse_heartbeat_interval=1,
        server_max_workers=8,
        sse_max_connections=8,
    )
    eng = ArtifactEngine(cfg)
    port = _free_port()
    srv = ArtifactRPCServer(eng, host="127.0.0.1", port=port)
    srv.start_async()
    time.sleep(0.4)
    return srv, eng, port, tmp


def _cleanup(srv, eng, tmp):
    try:
        srv.stop()
    except Exception:
        pass
    try:
        eng.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _drain_until(base, path, predicate, timeout=5.0):
    # 开 SSE 流，读到第一个满足 predicate 的 event dict 返回；超时返回 None。
    cm = httpx.stream("GET", f"{base}{path}", timeout=timeout)
    resp = cm.__enter__()
    found = None
    try:
        buf = b""
        deadline = time.monotonic() + timeout
        for raw in resp.iter_bytes():
            buf += raw
            while b"\n\n" in buf:
                chunk, buf = buf.split(b"\n\n", 1)
                data_line = None
                for line in chunk.split(b"\n"):
                    if line.startswith(b"data: "):
                        data_line = line[6:]
                        break
                if data_line:
                    try:
                        ev = json.loads(data_line.decode())
                    except Exception:
                        continue
                    if predicate(ev):
                        found = ev
                        break
            if found is not None or time.monotonic() > deadline:
                break
    except Exception:
        pass
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass
    return found


def test_artifact_scoped_sse_passes_matching_event():
    srv, eng, port, tmp = _make_server()
    base = f"http://127.0.0.1:{port}"
    try:
        received = []
        done = threading.Event()

        def reader():
            got = _drain_until(
                base,
                "/api/v1/artifacts/art_aaa/events",
                lambda e: e.get("artifact_id") == "art_aaa",
                timeout=3.0,
            )
            received.append(got)
            done.set()

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.6)
        eng.event_bus.publish(
            "artifact.updated",
            {"event_type": "artifact.updated", "artifact_id": "art_aaa", "kind": "code"},
        )
        done.wait(timeout=4.0)
        t.join(timeout=2.0)
        assert received and received[0] is not None
        assert received[0]["artifact_id"] == "art_aaa"
    finally:
        _cleanup(srv, eng, tmp)


def test_artifact_scoped_sse_filters_other_artifact():
    srv, eng, port, tmp = _make_server()
    base = f"http://127.0.0.1:{port}"
    try:
        received = []
        done = threading.Event()

        def reader():
            got = _drain_until(
                base,
                "/api/v1/artifacts/art_aaa/events",
                lambda e: e.get("artifact_id") == "art_aaa",
                timeout=3.0,
            )
            received.append(got)
            done.set()

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.6)
        # art_bbb 事件——scope 流不应收到
        eng.event_bus.publish(
            "artifact.updated",
            {"event_type": "artifact.updated", "artifact_id": "art_bbb", "kind": "code"},
        )
        time.sleep(0.4)
        # art_aaa 事件——应收到
        eng.event_bus.publish(
            "artifact.updated",
            {"event_type": "artifact.updated", "artifact_id": "art_aaa", "kind": "code"},
        )
        done.wait(timeout=4.0)
        t.join(timeout=2.0)
        assert received and received[0] is not None
        assert received[0]["artifact_id"] == "art_aaa"
    finally:
        _cleanup(srv, eng, tmp)


def test_session_scoped_sse_passes_matching_event():
    srv, eng, port, tmp = _make_server()
    base = f"http://127.0.0.1:{port}"
    try:
        received = []
        done = threading.Event()

        def reader():
            got = _drain_until(
                base,
                "/api/v1/sessions/sess-X/events",
                lambda e: e.get("session_id") == "sess-X",
                timeout=3.0,
            )
            received.append(got)
            done.set()

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.6)
        eng.event_bus.publish(
            "artifact.created",
            {"event_type": "artifact.created", "session_id": "sess-X", "kind": "code"},
        )
        done.wait(timeout=4.0)
        t.join(timeout=2.0)
        assert received and received[0] is not None
        assert received[0]["session_id"] == "sess-X"
    finally:
        _cleanup(srv, eng, tmp)


def test_session_scoped_sse_filters_other_session():
    srv, eng, port, tmp = _make_server()
    base = f"http://127.0.0.1:{port}"
    try:
        received = []
        done = threading.Event()

        def reader():
            got = _drain_until(
                base,
                "/api/v1/sessions/sess-X/events",
                lambda e: e.get("session_id") == "sess-X",
                timeout=3.0,
            )
            received.append(got)
            done.set()

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.6)
        # 别的 session 事件——应被过滤
        eng.event_bus.publish(
            "artifact.created",
            {"event_type": "artifact.created", "session_id": "sess-Y", "kind": "code"},
        )
        time.sleep(0.4)
        # 目标 session 事件——应收到
        eng.event_bus.publish(
            "artifact.created",
            {"event_type": "artifact.created", "session_id": "sess-X", "kind": "code"},
        )
        done.wait(timeout=4.0)
        t.join(timeout=2.0)
        assert received and received[0] is not None
        assert received[0]["session_id"] == "sess-X"
    finally:
        _cleanup(srv, eng, tmp)


def test_scope_sse_endpoints_not_404():
    # 握手应返回 200（SSE 流头），不是 404。
    srv, eng, port, tmp = _make_server()
    base = f"http://127.0.0.1:{port}"
    try:
        cm_a = httpx.stream("GET", f"{base}/api/v1/artifacts/art_aaa/events", timeout=3.0)
        resp_a = cm_a.__enter__()
        cm_s = httpx.stream("GET", f"{base}/api/v1/sessions/sess-A/events", timeout=3.0)
        resp_s = cm_s.__enter__()
        try:
            assert resp_a.status_code == 200
            assert resp_s.status_code == 200
        finally:
            try:
                cm_a.__exit__(None, None, None)
            except Exception:
                pass
            try:
                cm_s.__exit__(None, None, None)
            except Exception:
                pass
    finally:
        _cleanup(srv, eng, tmp)
