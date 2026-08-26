import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler


@pytest.fixture
def handler():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    eng = ArtifactEngine(cfg)
    h = RPCHandler(eng)
    yield h, eng
    eng.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _wrap_to_thread(record):
    # 包裹真 asyncio.to_thread：记录每次调用被卸载的可调用对象，仍透传执行。
    # 这样既证明 handler 走 to_thread 卸载，又不破坏返回值正确性。
    real = asyncio.to_thread

    async def recording_to_thread(func, /, *args, **kwargs):
        record.append(func)
        return await real(func, *args, **kwargs)

    return recording_to_thread


async def test_get_offloads_storage_read_to_thread(handler, monkeypatch):
    # P2-2/M1+F2: 读路径经 asyncio.to_thread 卸载，不阻塞 event loop。
    h, eng = handler
    cr = await eng.create_artifact("s1", "a.py", "code", "print(1)")
    aid = cr[0].id

    record = []
    monkeypatch.setattr(
        "fusion_artifacts_engine.rpc.methods.asyncio.to_thread",
        _wrap_to_thread(record),
    )

    res = await h.dispatch("artifact.get", {"artifact_id": aid})
    assert res["artifact"]["id"] == aid
    assert any(
        getattr(fn, "__name__", "") == "get_artifact" for fn in record
    ), "artifact.get 必须 get_artifact 经 to_thread 卸载"


async def test_list_offloads_storage_read_to_thread(handler, monkeypatch):
    h, eng = handler
    await eng.create_artifact("s1", "a.py", "code", "print(1)")

    record = []
    monkeypatch.setattr(
        "fusion_artifacts_engine.rpc.methods.asyncio.to_thread",
        _wrap_to_thread(record),
    )

    res = await h.dispatch("artifact.list", {"session_id": "s1"})
    assert len(res["artifacts"]) == 1
    assert any(
        getattr(fn, "__name__", "") == "list_artifacts" for fn in record
    ), "artifact.list 必须 list_artifacts 经 to_thread 卸载"


async def test_create_uses_async_engine_and_async_publish(handler, monkeypatch):
    # P2-2: create 调的是 async engine.create_artifact（自身已非阻塞，RPC 层无需 to_thread）。
    # _publish 改为 async（不再同步回查阻塞）。验证：返回正确 + _publish 不抛（async 调通）。
    h, _eng = handler

    publish_called = []
    real_publish = h._publish

    async def spy_publish(event_type, aid, **extra):
        publish_called.append(event_type)
        return await real_publish(event_type, aid, **extra)

    monkeypatch.setattr(h, "_publish", spy_publish)

    res = await h.dispatch(
        "artifact.create",
        {"session_id": "s2", "name": "b.py", "type": "code", "content": "x = 1"},
    )
    aid = res["artifact"]["id"]
    assert aid.startswith("art_")
    assert "artifact.created" in publish_called, "_publish 须以 await 调用（async）"


async def test_delete_offloads_read_and_write_to_thread(handler, monkeypatch):
    h, eng = handler
    cr = await eng.create_artifact("s1", "a.py", "code", "print(1)")
    aid = cr[0].id

    record = []
    monkeypatch.setattr(
        "fusion_artifacts_engine.rpc.methods.asyncio.to_thread",
        _wrap_to_thread(record),
    )

    res = await h.dispatch("artifact.delete", {"artifact_id": aid})
    assert res["ok"] is True
    names = [getattr(fn, "__name__", "") for fn in record]
    assert "get_artifact" in names, "delete 需先经 to_thread 读删除前 artifact"
    assert "delete_artifact" in names, "delete 写须经 to_thread 卸载"


async def test_offload_runs_in_worker_thread(handler, monkeypatch):
    # P2-2 关键点：to_thread 把同步 storage 调用放到 worker 线程，event loop 不阻塞。
    # 替身记录调用时所在线程，断言 != 主线程（事件循环线程）。
    import threading

    h, eng = handler
    await eng.create_artifact("s1", "a.py", "code", "print(1)")
    main_tid = threading.get_ident()

    seen_tids = []
    real = asyncio.to_thread

    async def tracking_to_thread(func, /, *args, **kwargs):
        def wrapper(*a, **kw):
            seen_tids.append(threading.get_ident())
            return func(*a, **kw)

        return await real(wrapper, *args, **kwargs)

    monkeypatch.setattr(
        "fusion_artifacts_engine.rpc.methods.asyncio.to_thread",
        tracking_to_thread,
    )

    res = await h.dispatch("artifact.list", {"session_id": "s1"})
    assert res["artifacts"]
    assert seen_tids, "list 路径须经 to_thread"
    assert all(t != main_tid for t in seen_tids), "卸载须在 worker 线程执行，非 event loop 线程"
