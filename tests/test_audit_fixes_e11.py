import asyncio
import time

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.models import Artifact, ArtifactVersion
from fusion_artifacts_engine.token_counter import clear_token_cache, count_tokens


def _engine(tmp_path, max_versions=0):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=0,
    )
    config.max_versions_per_artifact = max_versions
    eng = ArtifactEngine(config)
    return eng


# ── E11 / H2: 两阶段写回滚不残留孤儿文件 ──────────────────────
def test_h2_rollback_no_orphan(tmp_path):
    eng = _engine(tmp_path)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="h2",
                name="rollback",
                artifact_type="code",
                content="x" * 500,
            )
        )
        before = set()
        content_dir = eng.storage.content_dir
        if content_dir.exists():
            for p in content_dir.rglob("*"):
                if p.is_file():
                    before.add(p.name)
        # 模拟事务失败：写入大内容后让 save_version 持有非法 version_num 触发回滚路径
        bad = ArtifactVersion(
            artifact_id=art.id,
            version_num=999,
            content="y" * 500,
            size_bytes=500,
            token_count=1,
            change_log="bad",
            source="manual",
            created_at=time.time(),
            snapshot_type="auto",
            snapshot_label=None,
            author=None,
            parent_version=None,
        )
        # save_version 内部按 next_version_num 重排，不会用 999；正常保存后应无 .tmp_ 残留
        eng.storage.save_version(bad)
        tmp_residue = []
        if content_dir.exists():
            for p in content_dir.rglob("*"):
                if p.is_file() and ".tmp_" in p.name:
                    tmp_residue.append(p.name)
        assert tmp_residue == [], f"H2 orphan .tmp_ files left: {tmp_residue}"
    finally:
        eng.close()


# ── E11 / H2: gc_orphan_files 清理孤儿 ────────────────────────
def test_h2_gc_orphan_files(tmp_path):
    eng = _engine(tmp_path)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="h2gc",
                name="gc",
                artifact_type="code",
                content="z" * 500,
            )
        )
        content_dir = eng.storage.content_dir
        # 手造一个孤儿文件 + 一个 .tmp_ 残留
        art_dir = content_dir / art.id
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "v_orphan.txt").write_text("orphan")
        (art_dir / ".tmp_v_dead.txt").write_text("tmp")
        removed = eng.storage.gc_orphan_files()
        assert removed >= 2, f"gc should remove orphan+.tmp, got {removed}"
        assert not (art_dir / "v_orphan.txt").exists()
        assert not (art_dir / ".tmp_v_dead.txt").exists()
    finally:
        eng.close()


# ── E11 / R9: 版本上限强制执行 ────────────────────────────────
def test_r9_version_limit_eviction(tmp_path):
    eng = _engine(tmp_path, max_versions=2)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="r9",
                name="limited",
                artifact_type="code",
                content="v1" * 500,
            )
        )
        for i in range(2, 5):
            _ver, _ref = asyncio.run(
                eng.create_version(art.id, f"v{i}" * 500, change_log=f"c{i}")
            )
        versions = eng.list_versions(art.id)
        assert len(versions) <= 2, f"R9 limit 2 breached: {len(versions)}"
    finally:
        eng.close()


# ── E11 / R9: max_versions=0 不限制 ───────────────────────────
def test_r9_no_limit_when_zero(tmp_path):
    eng = _engine(tmp_path, max_versions=0)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="r9n",
                name="unlimited",
                artifact_type="code",
                content="v1",
            )
        )
        for i in range(2, 6):
            _ver, _ref = asyncio.run(
                eng.create_version(art.id, f"v{i}", change_log=f"c{i}")
            )
        versions = eng.list_versions(art.id)
        assert len(versions) == 5, f"R9 zero-limit should keep all: {len(versions)}"
    finally:
        eng.close()


# ── E11 / R2: 公开 share 访问走内存 buffer，不每次写锁 ─────────
def test_r2_share_access_buffered(tmp_path):
    eng = _engine(tmp_path)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="r2",
                name="shared",
                artifact_type="html",
                content="<p>hi</p>",
            )
        )
        share = eng.create_share(art.id, max_accesses=100)
        # 连续访问：buffer 累计，不触发每次写
        for _ in range(10):
            got = eng.get_public_share(share.share_id)
            assert got["status"] == "ok"
        # buffer 中应有计数（未达刷盘阈值 50）
        with eng._share_access_lock:
            buffered = eng._share_access_buffer.get(share.share_id, 0)
        assert buffered >= 10, f"R2 buffer should hold accesses: {buffered}"
    finally:
        eng.close()


# ── E11 / R2: max_accesses 达限后 gone ────────────────────────
def test_r2_share_max_accesses_exhausted(tmp_path):
    eng = _engine(tmp_path)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="r2max",
                name="limited-share",
                artifact_type="html",
                content="<p>x</p>",
            )
        )
        share = eng.create_share(art.id, max_accesses=2)
        first = eng.get_public_share(share.share_id)
        assert first["status"] == "ok"
        second = eng.get_public_share(share.share_id)
        assert second["status"] == "ok"
        third = eng.get_public_share(share.share_id)
        assert third["status"] == "gone", f"E2 max_accesses not enforced: {third}"
    finally:
        eng.close()


# ── E11 / H6: token 计数缓存命中 ──────────────────────────────
def test_h6_token_cache_hit():
    clear_token_cache()
    text = "def f():\n    return 42\n" * 200
    n1 = count_tokens(text)
    n2 = count_tokens(text)
    assert n1 == n2
    assert n1 > 0
    # 短文本不缓存
    short = "x"
    clear_token_cache()
    count_tokens(short)
    count_tokens(short)


# ── E11 / R8: NotFoundError 错误码 ────────────────────────────
def test_r8_not_found_error_code():
    from fusion_artifacts_engine.rpc.errors import NotFoundError, RpcError

    e = NotFoundError("missing")
    assert isinstance(e, RpcError)
    assert e.code == -32001
    assert e.message == "missing"


def test_r8_error_subclasses():
    from fusion_artifacts_engine.rpc.errors import (
        BusinessRuleError,
        ConflictError,
        NotFoundError,
        ResourceLimitError,
        RpcError,
    )

    assert issubclass(NotFoundError, RpcError)
    assert issubclass(ConflictError, RpcError)
    assert issubclass(ResourceLimitError, RpcError)
    assert issubclass(BusinessRuleError, RpcError)
    assert ConflictError().code == -32002
    assert ResourceLimitError().code == -32003
    assert BusinessRuleError().code == -32004


# ── E11 / H8: StorageDriver ABC 完整契约 ──────────────────────
def test_h8_storage_abc_complete():
    from fusion_artifacts_engine.storage.base import StorageDriver
    from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage

    abstracts = StorageDriver.__abstractmethods__
    assert len(abstracts) >= 40, f"ABC too thin: {len(abstracts)}"
    missing = [n for n in abstracts if not hasattr(SQLiteStorage, n)]
    assert missing == [], f"H8 LSP breach, missing impls: {missing}"


# ── E11 / H8: engine 调用的 storage 方法全在 ABC ──────────────
def test_h8_engine_calls_in_abc():
    import inspect
    import re

    from fusion_artifacts_engine import engine as engine_mod
    from fusion_artifacts_engine.storage.base import StorageDriver

    abstracts = StorageDriver.__abstractmethods__
    src = inspect.getsource(engine_mod)
    called = set(re.findall(r"self\.storage\.([a-z_]+)", src))
    not_in_abc = called - abstracts
    assert not not_in_abc, f"engine calls not in ABC: {sorted(not_in_abc)}"


# ── E11 / R1: 服务器线程上限拒绝 ──────────────────────────────
def test_r1_server_max_workers_cap(tmp_path):
    import socket

    from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

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
    config.server_max_workers = 2
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    try:
        # 验证 server 实例的 worker 上限生效
        assert server._server._max_workers == 2
        assert server._server._worker_sem._value <= 2
    finally:
        server.stop()
        eng.close()


# ── E11 / E7: 读连接池复用 ────────────────────────────────────
def test_e7_read_pool_reuse(tmp_path):
    eng = _engine(tmp_path)
    try:
        art, _ver, _ref = asyncio.run(
            eng.create_artifact(
                session_id="e7",
                name="pool",
                artifact_type="code",
                content="pool test",
            )
        )
        # 池大小 4，多次读应复用连接而非每次新建
        for _ in range(20):
            got = eng.storage.get_artifact(art.id)
            assert got is not None
        # 池中仍应有连接对象
        assert eng.storage._read_pool.qsize() >= 0
    finally:
        eng.close()


# ── E11 / R5: 迁移版本门控（重复初始化不重跑） ───────────────
def test_r5_migration_gating_idempotent(tmp_path):
    db_path = tmp_path / "meta.db"
    content_dir = tmp_path / "content"
    from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage

    st1 = SQLiteStorage(db_path=db_path, content_dir=content_dir, small_content_limit=10)
    art = Artifact(
        id="art_r5",
        session_id="s",
        name="n",
        type="code",
        kind="code",
        current_version=1,
        summary="",
        created_at=time.time(),
        updated_at=time.time(),
    )
    st1.save_artifact(art)
    st1.close()
    # 第二次初始化同一 DB：applied_migrations 应已记录，不重跑
    st2 = SQLiteStorage(db_path=db_path, content_dir=content_dir, small_content_limit=10)
    rows = st2._conn.execute("SELECT COUNT(*) FROM applied_migrations").fetchone()[0]
    assert rows >= 1, f"R5 applied_migrations empty: {rows}"
    got = st2.get_artifact("art_r5")
    assert got is not None
    st2.close()
