import asyncio

import pytest

from fusion_artifacts_engine import long_context
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler

MD = "# Alpha\n\nalpha body\n\n## Sub\n\nsub body\n\n# Beta\n\nbeta body\n"


@pytest.fixture
def env(tmp_path):
    engine = ArtifactEngine(ArtifactEngineConfig(storage_root=tmp_path / "art", ipc_enabled=False))
    handler = RPCHandler(engine)
    try:
        yield engine, handler
    finally:
        engine.close()


def _mk_eng(tmp_path, **cfg):
    return ArtifactEngine(
        ArtifactEngineConfig(storage_root=tmp_path / "art", ipc_enabled=False, **cfg)
    )


class TestTocView:
    async def test_toc_structure(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        res = await h.dispatch("artifact.toc", {"artifact_id": art.id})
        assert res["version"] == 1
        assert res["total_lines"] == 12
        anchors = [e["anchor"] for e in res["toc"]]
        assert anchors == ["Alpha", "Sub", "Beta"]
        sub = res["toc"][1]
        assert sub["start_line"] == 5 and sub["end_line"] == 8
        assert sub["level"] == 2 and sub["tokens"] > 0

    async def test_toc_cached_until_new_version(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        await h.dispatch("artifact.toc", {"artifact_id": art.id})
        assert (art.id, 1) in h._toc_cache
        await engine.create_version(art.id, "# Only\n\nx\n")
        res = await h.dispatch("artifact.toc", {"artifact_id": art.id})
        assert res["version"] == 2
        assert [e["anchor"] for e in res["toc"]] == ["Only"]

    async def test_toc_explicit_version(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        await engine.create_version(art.id, "# Only\n\nx\n")
        res = await h.dispatch("artifact.toc", {"artifact_id": art.id, "version": 1})
        assert res["version"] == 1
        assert len(res["toc"]) == 3

    async def test_view_section(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        res = await h.dispatch("artifact.view", {"artifact_id": art.id, "section": "Sub"})
        assert res["content"] == "## Sub\n\nsub body\n"
        assert res["start_line"] == 5 and res["lines"] == 4
        assert res["total_lines"] == 12 and res["total_bytes"] > 0

    async def test_view_line_range(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        res = await h.dispatch("artifact.view", {"artifact_id": art.id, "offset": 2, "limit": 2})
        assert res["content"] == "alpha body\n"
        assert res["truncated"] is True and res["offset"] == 2
        tail = await h.dispatch("artifact.view", {"artifact_id": art.id, "offset": 9})
        assert "beta body" in tail["content"] and tail["truncated"] is False

    async def test_view_requires_selector(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        with pytest.raises(ValueError, match="requires 'section' or 'offset'"):
            await h.dispatch("artifact.view", {"artifact_id": art.id})

    async def test_view_missing_section_not_found(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        from fusion_artifacts_engine.rpc.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await h.dispatch("artifact.view", {"artifact_id": art.id, "section": "Nope"})

    async def test_view_out_of_bounds_offset(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        res = await h.dispatch("artifact.view", {"artifact_id": art.id, "offset": 999})
        assert res["content"] == "" and res["total_lines"] == 12
        assert res["truncated"] is False

    async def test_view_data_artifact_binary_like(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "Data", "data", '{"k": 1, "rows": 2}')
        res = await h.dispatch("artifact.toc", {"artifact_id": art.id})
        assert res["toc"] == []
        from fusion_artifacts_engine.rpc.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await h.dispatch("artifact.view", {"artifact_id": art.id, "section": "k"})
        rng = await h.dispatch("artifact.view", {"artifact_id": art.id, "offset": 0})
        assert rng["total_lines"] == 1 and "k" in rng["content"]

    async def test_toc_version_not_found(self, env):
        engine, h = env
        art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD)
        from fusion_artifacts_engine.rpc.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await h.dispatch("artifact.toc", {"artifact_id": art.id, "version": 99})


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)

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
    _FakeClient.resp = _FakeResp(
        {"choices": [{"message": {"content": "Rolling summary: added Alpha/Beta sections."}}]}
    )
    monkeypatch.setattr(long_context.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


async def _drain(handler):
    # Long-context hooks are create_task background tasks — tests must drain before asserting
    while handler._bg_tasks:
        await asyncio.gather(*list(handler._bg_tasks))
        await asyncio.sleep(0)


def _mk_lc_eng(tmp_path, **cfg):
    return ArtifactEngine(
        ArtifactEngineConfig(storage_root=tmp_path / "art", ipc_enabled=False, **cfg)
    )


MD2 = "# T\n\nbody v1 content\n"


class TestRollingSummary:
    async def test_summary_generated_at_threshold(self, tmp_path, fake_http):
        engine = _mk_lc_eng(tmp_path, long_context_summary_threshold=3)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            for i in range(2):
                await engine.create_version(art.id, MD2 + f"\nappend {i}\n")
            meta = engine.storage.get_artifact(art.id).metadata or {}
            assert "summary_version" not in meta
            await h.dispatch(
                "artifact.update", {"artifact_id": art.id, "content": MD2 + "\nfinal\n"}
            )
            await _drain(h)
            chats = [c for c in fake_http.calls if "chat/completions" in c["url"]]
            assert len(chats) == 1
            art2 = engine.storage.get_artifact(art.id)
            assert art2.summary.startswith("Rolling summary")
            assert art2.metadata["summary_version"] == 4
        finally:
            engine.close()

    async def test_summary_refresh_each_new_version_and_dedup_same_version(
        self, tmp_path, fake_http
    ):
        engine = _mk_lc_eng(tmp_path, long_context_summary_threshold=2)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "a"})
            await _drain(h)
            await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "b"})
            await _drain(h)
            # Acceptance requires "a new version refreshes the summary" — one generation each for v2 and v3
            chats = [c for c in fake_http.calls if "chat/completions" in c["url"]]
            assert len(chats) == 2
            # Idempotent per version number: calling again directly produces no new calls
            art2 = engine.storage.get_artifact(art.id)
            out = await long_context.maybe_generate_summary(engine, art2, art2.current_version)
            assert out is None
            assert len([c for c in fake_http.calls if "chat/completions" in c["url"]]) == 2
        finally:
            engine.close()

    async def test_summary_below_threshold_no_call(self, tmp_path, fake_http):
        engine = _mk_lc_eng(tmp_path, long_context_summary_threshold=10)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "a"})
            await _drain(h)
            assert [c for c in fake_http.calls if "chat/completions" in c["url"]] == []
        finally:
            engine.close()

    async def test_summary_disabled_threshold_zero(self, tmp_path, fake_http):
        engine = _mk_lc_eng(tmp_path, long_context_summary_threshold=0)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            await engine.create_version(art.id, MD2 + "a")
            await engine.create_version(art.id, MD2 + "b")
            out = await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "c"})
            await _drain(h)
            assert [c for c in fake_http.calls if "chat/completions" in c["url"]] == []
            assert out["version"]["version_num"] == 4
        finally:
            engine.close()

    async def test_summary_mlx_failure_degrades(self, tmp_path, fake_http):
        fake_http.fail = True
        engine = _mk_lc_eng(tmp_path, long_context_summary_threshold=2)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            out = await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "a"})
            await _drain(h)
            assert out["version"]["version_num"] == 2
            assert "summary_version" not in (engine.storage.get_artifact(art.id).metadata or {})
        finally:
            engine.close()


class TestRagArchive:
    async def test_superseded_version_archived(self, tmp_path, fake_http):
        engine = _mk_lc_eng(tmp_path)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "new"})
            await _drain(h)
            arch = [c for c in fake_http.calls if "/documents/ingest" in c["url"]]
            assert len(arch) == 1
            body = arch[0]["kw"]["json"]
            assert body["doc_name"] == f"{art.id}_v1.txt"
            assert body["metadata"]["artifact_id"] == art.id
            assert body["metadata"]["artifact_version"] == 1
            meta = engine.storage.get_artifact(art.id).metadata
            assert meta["archived_versions"] == [1]
        finally:
            engine.close()

    async def test_archive_idempotent_same_version(self, tmp_path, fake_http):
        engine = _mk_lc_eng(tmp_path)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            first = await long_context.maybe_archive_superseded(engine, art, 1)
            again = await long_context.maybe_archive_superseded(engine, art, 1)
            assert first == f"{art.id}_v1" and again is None
            arch = [c for c in fake_http.calls if "/documents/ingest" in c["url"]]
            assert len(arch) == 1
        finally:
            engine.close()

    async def test_archive_rag_down_degrades(self, tmp_path, fake_http):
        fake_http.fail = True
        engine = _mk_lc_eng(tmp_path)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            out = await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "a"})
            await _drain(h)
            assert out["version"]["version_num"] == 2
            assert "archived_versions" not in (engine.storage.get_artifact(art.id).metadata or {})
        finally:
            engine.close()

    async def test_archive_disabled(self, tmp_path, fake_http):
        engine = _mk_lc_eng(tmp_path, long_context_rag_enabled=False)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "a"})
            await _drain(h)
            assert [c for c in fake_http.calls if "ingest" in c["url"]] == []
        finally:
            engine.close()

    async def test_archive_http_4xx_no_metadata_write(self, tmp_path, fake_http):
        fake_http.resp = _FakeResp({}, status=404)
        engine = _mk_lc_eng(tmp_path)
        h = RPCHandler(engine)
        try:
            art, _v, _ref = await engine.create_artifact("s1", "D", "markdown", MD2)
            await h.dispatch("artifact.update", {"artifact_id": art.id, "content": MD2 + "a"})
            await _drain(h)
            assert "archived_versions" not in (engine.storage.get_artifact(art.id).metadata or {})
        finally:
            engine.close()


class TestStorageSummaryUpdate:
    def test_update_artifact_summary_roundtrip(self, tmp_path):
        engine = _mk_lc_eng(tmp_path)
        try:
            import time as _t

            art = _mk_artifact("s1", "D", "markdown", MD2, _t.time())
            art.id = "art_unitsummary01"
            engine.storage.save_artifact(art)
            ok = engine.storage.update_artifact_summary(
                art.id, "new summary", metadata_patch={"summary_version": 3, "k": "v"}
            )
            assert ok is True
            got = engine.storage.get_artifact(art.id)
            assert got.summary == "new summary"
            assert got.metadata["summary_version"] == 3 and got.metadata["k"] == "v"
            assert engine.storage.update_artifact_summary("art_missing", "x") is False
        finally:
            engine.close()


def _mk_artifact(session, name, art_type, content, now):
    from fusion_artifacts_engine.models import Artifact

    return Artifact(
        id="",
        session_id=session,
        name=name,
        type=art_type,
        kind="document",
        content=content,
        summary="",
        created_at=now,
        updated_at=now,
    )
