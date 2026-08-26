import asyncio
import json
import threading
import time
import urllib.request

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer


@pytest.fixture
def engine(tmp_path):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=0,
    )
    eng = ArtifactEngine(config)
    yield eng
    eng.close()


def test_concurrent_create_artifacts(engine):
    results = []
    errors = []

    def create_art(i):
        try:
            art, _ver, _ref = asyncio.run(
                engine.create_artifact(
                    session_id="conc_test",
                    name=f"artifact_{i}",
                    artifact_type="code",
                    content=f"print('hello {i}')",
                )
            )
            results.append(art.id)
        except (OSError, ValueError, RuntimeError) as e:
            errors.append(e)

    threads = [threading.Thread(target=create_art, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(errors) == 0, f"Errors: {errors}"
    assert len(results) == 10
    assert len(set(results)) == 10, "Duplicate artifact IDs detected"


def test_concurrent_write_lock_serializes(engine):
    errors = []
    art, _ver, _ref = asyncio.run(
        engine.create_artifact(
            session_id="lock_test",
            name="locked_artifact",
            artifact_type="code",
            content="v1",
        )
    )

    def write_op(i):
        try:
            storage = engine.storage
            with storage._write_lock:
                ver_num = storage.next_version_num(art.id)
                now = time.time()
                storage._conn.execute(
                    """INSERT INTO artifact_versions
                       (artifact_id, version_num, content, content_path, size_bytes,
                        change_log, source, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        art.id,
                        ver_num,
                        f"v{i}",
                        None,
                        len(f"v{i}".encode()),
                        f"concurrent v{i}",
                        "manual",
                        now,
                    ),
                )
                storage._conn.execute(
                    "UPDATE artifacts SET current_version = ?, updated_at = ? WHERE id = ?",
                    (ver_num, now, art.id),
                )
                storage._conn.commit()
        except (OSError, ValueError, RuntimeError) as e:
            errors.append(e)

    threads = [threading.Thread(target=write_op, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(errors) == 0, f"Errors: {errors}"
    versions = engine.list_versions(art.id)
    assert len(versions) == 4


def test_rpc_server_ping(tmp_path):
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

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

    try:
        req = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}).encode()
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/",
            data=req,
            timeout=5,
        )
        data = json.loads(resp.read())
        assert data["result"]["pong"] is True
    finally:
        server.stop()
        eng.close()


def test_rpc_concurrent_requests(tmp_path):
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

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

    results = []
    errors = []

    def ping(i):
        try:
            req = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": i}).encode()
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/",
                data=req,
                timeout=5,
            )
            data = json.loads(resp.read())
            results.append(data.get("result", {}).get("pong", False))
        except (OSError, ValueError, RuntimeError) as e:
            errors.append(e)

    threads = [threading.Thread(target=ping, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        assert len(errors) == 0, f"Errors: {errors}"
        assert all(results)
    finally:
        server.stop()
        eng.close()


def test_share_max_accesses_atomic_under_burst(tmp_path):
    # P1-2/H5: max_accesses 在并发突发下必须精确不超限。
    # 修复前 get_shared_artifact 先读 access_count 再 increment，TOCTOU 窗口让
    # N 并发请求全过检查后各自 +1 → 超出 max_accesses。修复后原子条件 UPDATE 保证精确。
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        allow_no_auth=True,
    )
    eng = ArtifactEngine(config)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="share_burst",
                name="burst.py",
                artifact_type="code",
                content="x",
            )
        )
        share = eng.create_share(art.id, max_accesses=5)

        results = []

        def access_one(_i):
            res = eng.get_shared_artifact(share.share_id)
            results.append(res is not None)

        threads = [threading.Thread(target=access_one, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        served = sum(results)
        # 精确 5 次成功，15 次因达上限拒绝；修复前可能 >5
        assert served == 5, f"max_accesses=5 but served {served} under 20 concurrent"
    finally:
        eng.close()


def test_public_share_max_accesses_atomic_under_burst(tmp_path):
    # P1-2/H5: 公开端点 get_public_share 的内存缓冲 check-and-increment 同样须原子。
    # try_consume_access 把 check 与 buffer 增计数合并到同一 _lock 段，突发下精确不超限。
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        allow_no_auth=True,
    )
    eng = ArtifactEngine(config)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="pub_burst",
                name="pubburst.py",
                artifact_type="code",
                content="x",
            )
        )
        share = eng.create_share(art.id, max_accesses=5)

        statuses = []

        def access_one(_i):
            res = eng.share_mgr.get_public_share(share.share_id)
            statuses.append(res.get("status"))

        threads = [threading.Thread(target=access_one, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        ok_count = sum(1 for s in statuses if s == "ok")
        gone_count = sum(1 for s in statuses if s == "gone")
        # 精确 5 次 ok，15 次 gone(exhausted)；修复前缓冲 TOCTOU 可能 >5
        assert ok_count == 5, f"public max_accesses=5 but ok={ok_count} under 20 concurrent"
        assert gone_count == 15, f"expected 15 gone, got {gone_count}"
    finally:
        eng.close()
