import shutil
import socket
import tempfile
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


def _make_server(sse_max_connections, max_workers=4):
    # sse_max_connections small, max_workers small, so a handful of connections can verify isolation and the cap.
    # heartbeat=1 keeps data on the stream, but this test never reads the body — it takes the header status code then closes the stream,
    # so heartbeat can never trigger httpx read-timeout resets and hang (httpx.get reads the whole body = hang, forbidden).
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(
        storage_root=tmp / "artifacts",
        allow_no_auth=True,
        sse_heartbeat_interval=1,
        server_max_workers=max_workers,
        sse_max_connections=sse_max_connections,
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


def _open_sse(base):
    # Open SSE with a stream context manager: take only the header status code, never iterate the body (SSE body is infinite,
    # httpx.get would hang). Returns (stream_cm, resp); the caller is responsible for __exit__ of the cm holding resp.
    cm = httpx.stream("GET", f"{base}/api/v1/events/stream", timeout=8.0)
    resp = cm.__enter__()
    return cm, resp


def test_sse_does_not_exhaust_rpc_worker_slots():
    # P2-1/H3/F1: long-lived SSE connections no longer monopolize the RPC worker semaphore.
    # max_workers=2, open 2 SSE streams (filling the sse slots); RPC must still be sendable — proving worker slots were returned.
    srv, eng, port, tmp = _make_server(sse_max_connections=2, max_workers=2)
    streams = []
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(2):
            cm, resp = _open_sse(base)
            assert resp.status_code == 200, "SSE connection should succeed"
            streams.append(cm)
        time.sleep(0.3)
        # 2 SSE streams fill sse_max_connections=2; if SSE still monopolized workers (old behavior),
        # max_workers=2 would be exhausted → RPC would be rejected with 503. After isolation RPC must still be 200.
        rpc = httpx.post(
            base,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            timeout=5.0,
        )
        assert rpc.status_code == 200, "RPC must remain usable after SSE fills all sse slots"
        assert "result" in rpc.json()
        # Send another RPC to confirm the worker slot is stable (not a one-off)
        rpc2 = httpx.post(
            base,
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            timeout=5.0,
        )
        assert rpc2.status_code == 200
    finally:
        for cm in streams:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)


def test_sse_connection_cap_returns_503():
    # P2-1: SSE connections beyond sse_max_connections are rejected with 503
    srv, eng, port, tmp = _make_server(sse_max_connections=1, max_workers=8)
    cm1 = None
    try:
        base = f"http://127.0.0.1:{port}"
        cm1, r1 = _open_sse(base)
        assert r1.status_code == 200, "first SSE should succeed"
        time.sleep(0.3)
        # Second SSE exceeds the cap → 503 (503 has a finite JSON body, safe for httpx.get)
        r2 = httpx.get(f"{base}/api/v1/events/stream", timeout=5.0)
        assert r2.status_code == 503, "exceeding sse_max_connections must be 503"
        assert "limit" in r2.json().get("error", "").lower()
    finally:
        if cm1 is not None:
            try:
                cm1.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)


def test_sse_slot_released_on_disconnect_allows_reconnect():
    # P2-1: after SSE disconnects the sse slot is returned and new connections are admitted immediately
    srv, eng, port, tmp = _make_server(sse_max_connections=1, max_workers=8)
    cm2 = None
    try:
        base = f"http://127.0.0.1:{port}"
        cm1, r1 = _open_sse(base)
        assert r1.status_code == 200
        time.sleep(0.2)
        cm1.__exit__(None, None, None)
        time.sleep(0.3)
        # Slot returned, so the new connection should get 200 instead of 503
        cm2, r2 = _open_sse(base)
        assert r2.status_code == 200, "slot should be returned after disconnect, allowing a new connection"
    finally:
        for cm in (cm2,):
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)


def test_sse_max_connections_zero_falls_back_legacy():
    # P2-1: sse_max_connections=0 = fall back to old behavior (SSE keeps occupying worker slots, no separate cap)
    srv, eng, port, tmp = _make_server(sse_max_connections=0, max_workers=8)
    cm = None
    try:
        assert srv._server._sse_sem is None, "_sse_sem must not be created when the cap is 0"
        base = f"http://127.0.0.1:{port}"
        # In fallback mode SSE can still connect (no sse_sem gate rejects it). Use stream, take the header then close, never read the body.
        cm, r = _open_sse(base)
        assert r.status_code == 200, "SSE should be connectable in fallback mode"
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)


def test_worker_sem_balance_after_sse_handoff():
    # P2-1: after SSE handoff _worker_sem counts correctly (no leak, no over-release)
    srv, eng, port, tmp = _make_server(sse_max_connections=2, max_workers=4)
    cm = None
    try:
        base = f"http://127.0.0.1:{port}"
        worker_sem = srv._server._worker_sem
        sse_sem = srv._server._sse_sem
        initial = worker_sem._value
        cm, r = _open_sse(base)
        assert r.status_code == 200
        time.sleep(0.3)
        # After handoff the worker slot count should return to initial (SSE does not occupy worker slots)
        assert worker_sem._value == initial, "worker_sem should be back at full value after SSE handoff"
        # sse_sem holds 1 slot
        assert sse_sem._value == 1, "sse_sem should have 1 slot occupied"
        cm.__exit__(None, None, None)
        cm = None
        time.sleep(0.3)
        # After disconnect both semaphores are returned
        assert worker_sem._value == initial, "worker_sem still at full value"
        assert sse_sem._value == 2, "sse_sem should be returned to full value"
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)
