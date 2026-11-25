import asyncio

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p

    @property
    def text(self):
        return ""


class _FakeClient:
    calls = []
    resp = _FakeResp({})
    fail = False

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        if _FakeClient.fail:
            raise ConnectionError("endpoint down")
        _FakeClient.calls.append({"url": url, "kw": kw})
        return _FakeClient.resp


@pytest.fixture
def fake_http(monkeypatch):
    from fusion_artifacts_engine import long_context

    _FakeClient.calls = []
    _FakeClient.fail = False
    _FakeClient.resp = _FakeResp([])
    monkeypatch.setattr(long_context.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _mk_eng(tmp_path, **cfg):
    return ArtifactEngine(
        ArtifactEngineConfig(storage_root=tmp_path / "art", ipc_enabled=False, **cfg)
    )


MD = "# T\n\nbody content about quarterly metrics\n"


def _rag_hit(aid, vnum, score=0.91, text="quarterly metrics deck"):
    return {
        "id": f"chunk_{aid}_{vnum}",
        "score": score,
        "content": text,
        "metadata": {
            "artifact_id": aid,
            "artifact_version": vnum,
            "artifact_type": "markdown",
            "name": "Q metrics",
            "archived_at": 1000.0,
        },
    }


async def test_search_library_merges_rag_and_metadata(tmp_path, fake_http):
    engine = _mk_eng(tmp_path)
    h = RPCHandler(engine)
    try:
        a1, _v, _ref = await engine.create_artifact("s1", "Local doc", "markdown", MD)
        a2, _v, _ref = await engine.create_artifact("s1", "Archived deck", "markdown", MD)
        await engine.create_version(a2.id, MD + "v2\n")
        fake_http.resp = _FakeResp([_rag_hit(a2.id, 1)])
        out = await h.dispatch("artifact.search_library", {"query": "quarterly metrics"})
        assert out["rag_status"] == "ok"
        ids = {r["artifact_id"] for r in out["results"]}
        assert {a1.id, a2.id} <= ids
        rag_rows = [r for r in out["results"] if r["source"] == "rag"]
        assert len(rag_rows) == 1
        assert rag_rows[0]["version"] == 1
        assert rag_rows[0]["score"] == 0.91
        assert "quarterly metrics" in rag_rows[0]["snippet"]
    finally:
        engine.close()


async def test_search_library_rag_ranked_first(tmp_path, fake_http):
    engine = _mk_eng(tmp_path)
    try:
        a1, _v, _ref = await engine.create_artifact("s1", "Old name", "markdown", MD)
        fake_http.resp = _FakeResp([_rag_hit(a1.id, 2)])
        out = await engine_ranked(engine, a1.id)
        assert out["results"][0]["source"] == "rag"
        assert out["results"][0]["version"] == 2
    finally:
        engine.close()


async def engine_ranked(engine, aid):
    from fusion_artifacts_engine.long_context import search_library

    return await search_library(engine, "metrics")


async def test_search_library_rag_down_degrades(tmp_path, fake_http):
    fake_http.fail = True
    engine = _mk_eng(tmp_path)
    h = RPCHandler(engine)
    try:
        a1, _v, _ref = await engine.create_artifact("s1", "Local only", "markdown", MD)
        out = await h.dispatch("artifact.search_library", {"query": "anything"})
        assert out["rag_status"] == "unavailable"
        assert [r["artifact_id"] for r in out["results"]] == [a1.id]
        assert all(r["source"] == "metadata" for r in out["results"])
    finally:
        engine.close()


async def test_search_library_disabled(tmp_path, fake_http):
    engine = _mk_eng(tmp_path, long_context_rag_enabled=False)
    try:
        await engine.create_artifact("s1", "Doc", "markdown", MD)
        out = await engine_search(engine)
        assert out["rag_status"] == "disabled"
        assert all(r["source"] == "metadata" for r in out["results"])
        assert fake_http.calls == []
    finally:
        engine.close()


async def engine_search(engine):
    from fusion_artifacts_engine.long_context import search_library

    return await search_library(engine, "metrics")


async def test_search_library_requires_query(tmp_path, fake_http):
    engine = _mk_eng(tmp_path)
    h = RPCHandler(engine)
    try:
        with pytest.raises(ValueError):
            await h.dispatch("artifact.search_library", {"query": "  "})
    finally:
        engine.close()


async def test_archive_failure_recorded_and_retryable(tmp_path, fake_http):
    # #129 acceptance: archive failure visible in metadata + retryable from the API
    engine = _mk_eng(tmp_path)
    h = RPCHandler(engine)
    try:
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        fake_http.fail = True
        await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD + "v2"})
        while h._bg_tasks:
            await asyncio.gather(*list(h._bg_tasks))
            await asyncio.sleep(0)
        meta = engine.storage.get_artifact(art.id).metadata or {}
        status = meta["rag_archive"]
        assert status["status"] == "failed"
        assert status["failed"] == [1]
        assert "endpoint down" in status["last_error"]
        # retry with rag back up
        fake_http.fail = False
        fake_http.resp = _FakeResp({}, status=200)
        out = await h.dispatch("artifact.retry_archive", {"artifact_id": art.id})
        assert out["retried"] == [1]
        assert out["failed"] == []
        assert out["status"] == "ok"
        meta2 = engine.storage.get_artifact(art.id).metadata or {}
        assert meta2["archived_versions"] == [1]
        assert meta2["rag_archive"]["status"] == "ok"
        assert meta2["rag_archive"]["failed"] == []
    finally:
        engine.close()


async def test_retry_archive_http_rejection_surfaces(tmp_path, fake_http):
    engine = _mk_eng(tmp_path)
    h = RPCHandler(engine)
    try:
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        fake_http.resp = _FakeResp({"detail": "embed model down"}, status=503)
        out = await h.dispatch("artifact.retry_archive", {"artifact_id": art.id})
        # nothing pending in rag_archive -> nothing retried
        assert out["retried"] == []
    finally:
        engine.close()


async def test_retry_archive_unknown_artifact(tmp_path, fake_http):
    engine = _mk_eng(tmp_path)
    h = RPCHandler(engine)
    try:
        with pytest.raises(ValueError):
            await h.dispatch("artifact.retry_archive", {"artifact_id": "art_none"})
    finally:
        engine.close()
