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
    # Wrap the real asyncio.to_thread: record each callable offloaded through it, while still passing execution through.
    # This both proves handlers offload via to_thread and keeps return values correct.
    real = asyncio.to_thread

    async def recording_to_thread(func, /, *args, **kwargs):
        record.append(func)
        return await real(func, *args, **kwargs)

    return recording_to_thread


async def test_get_offloads_storage_read_to_thread(handler, monkeypatch):
    # P2-2/M1+F2: read paths are offloaded via asyncio.to_thread and do not block the event loop.
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
    ), "artifact.get must fetch get_artifact offloaded via to_thread"


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
    ), "artifact.list must fetch list_artifacts offloaded via to_thread"


async def test_create_uses_async_engine_and_async_publish(handler, monkeypatch):
    # P2-2: create calls the async engine.create_artifact (non-blocking itself, RPC layer needs no to_thread).
    # _publish is now async (no more blocking synchronous read-back). Verify: correct return + _publish does not raise (async path works).
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
    assert "artifact.created" in publish_called, "_publish must be invoked with await (async)"


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
    assert "get_artifact" in names, "delete must first read the pre-delete artifact via to_thread"
    assert "delete_artifact" in names, "delete write must be offloaded via to_thread"


async def test_offload_runs_in_worker_thread(handler, monkeypatch):
    # P2-2 key point: to_thread runs synchronous storage calls on a worker thread so the event loop is not blocked.
    # The stub records the thread it was called on; assert != main thread (the event-loop thread).
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
    assert seen_tids, "list path must go through to_thread"
    assert all(t != main_tid for t in seen_tids), "offloaded work must run on a worker thread, not the event-loop thread"
