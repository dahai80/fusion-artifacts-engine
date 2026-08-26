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
    # sse_max_connections 小、max_workers 小，便于用少量连接验证隔离与上限。
    # heartbeat=1 使流上有数据，但本测试从不读 body——只取 header 状态码后即关流，
    # 故不会因 heartbeat 触发 httpx 读超时重置而挂死（httpx.get 会读全部 body=挂死，禁用）。
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
    # 用流上下文管理器开 SSE：只取 header 状态码，不迭代 body（SSE body 无限，
    # httpx.get 会挂死）。返回 (stream_cm, resp)，调用方负责 resp 所在 cm 的 __exit__。
    cm = httpx.stream("GET", f"{base}/api/v1/events/stream", timeout=8.0)
    resp = cm.__enter__()
    return cm, resp


def test_sse_does_not_exhaust_rpc_worker_slots():
    # P2-1/H3/F1: SSE 长连接不再独占 RPC worker 信号量。
    # max_workers=2，开 2 个 SSE（占满 sse 槽），此时仍应能发 RPC——证明 worker 槽已归还。
    srv, eng, port, tmp = _make_server(sse_max_connections=2, max_workers=2)
    streams = []
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(2):
            cm, resp = _open_sse(base)
            assert resp.status_code == 200, "SSE 连接应成功"
            streams.append(cm)
        time.sleep(0.3)
        # 2 个 SSE 占满 sse_max_connections=2；若 SSE 仍独占 worker（旧行为），
        # max_workers=2 已耗尽 → RPC 会被 503 拒绝。隔离后 RPC 必须仍 200。
        rpc = httpx.post(
            base,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            timeout=5.0,
        )
        assert rpc.status_code == 200, "RPC 必须在 SSE 占满 sse 槽后仍可用"
        assert "result" in rpc.json()
        # 再发一个 RPC，确认 worker 槽稳定（非一次性）
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
    # P2-1: 超过 sse_max_connections 的 SSE 连接被 503 拒绝
    srv, eng, port, tmp = _make_server(sse_max_connections=1, max_workers=8)
    cm1 = None
    try:
        base = f"http://127.0.0.1:{port}"
        cm1, r1 = _open_sse(base)
        assert r1.status_code == 200, "第一个 SSE 应成功"
        time.sleep(0.3)
        # 第二个 SSE 超出上限 → 503（503 是有限 JSON body，httpx.get 安全）
        r2 = httpx.get(f"{base}/api/v1/events/stream", timeout=5.0)
        assert r2.status_code == 503, "超 sse_max_connections 必须 503"
        assert "limit" in r2.json().get("error", "").lower()
    finally:
        if cm1 is not None:
            try:
                cm1.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)


def test_sse_slot_released_on_disconnect_allows_reconnect():
    # P2-1: SSE 断开后 sse 槽归还，新连接可立即接入
    srv, eng, port, tmp = _make_server(sse_max_connections=1, max_workers=8)
    cm2 = None
    try:
        base = f"http://127.0.0.1:{port}"
        cm1, r1 = _open_sse(base)
        assert r1.status_code == 200
        time.sleep(0.2)
        cm1.__exit__(None, None, None)
        time.sleep(0.3)
        # 槽已归还，新连接应 200 而非 503
        cm2, r2 = _open_sse(base)
        assert r2.status_code == 200, "断开后槽应归还，新连接可接入"
    finally:
        for cm in (cm2,):
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)


def test_sse_max_connections_zero_falls_back_legacy():
    # P2-1: sse_max_connections=0 = 回退旧行为（SSE 继续占 worker 槽，不设独立上限）
    srv, eng, port, tmp = _make_server(sse_max_connections=0, max_workers=8)
    cm = None
    try:
        assert srv._server._sse_sem is None, "0 时不应创建 _sse_sem"
        base = f"http://127.0.0.1:{port}"
        # 回退模式下 SSE 仍可连（不会因 sse_sem 判断被拒）。用 stream 取 header 即关，不读 body。
        cm, r = _open_sse(base)
        assert r.status_code == 200, "回退模式 SSE 应可连"
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)


def test_worker_sem_balance_after_sse_handoff():
    # P2-1: SSE 移交后 _worker_sem 计数正确（不泄漏、不过释放）
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
        # 移交后 worker 槽应回到 initial（SSE 不占 worker 槽）
        assert worker_sem._value == initial, "SSE 移交后 worker_sem 应恢复满值"
        # sse_sem 占 1 槽
        assert sse_sem._value == 1, "sse_sem 应被占 1 槽"
        cm.__exit__(None, None, None)
        cm = None
        time.sleep(0.3)
        # 断开后两边都归还
        assert worker_sem._value == initial, "worker_sem 仍满值"
        assert sse_sem._value == 2, "sse_sem 应归还到满值"
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        _cleanup(srv, eng, tmp)
