import shutil
import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


@pytest.fixture
def engine(tmp_dir):
    cfg = ArtifactEngineConfig(storage_root=tmp_dir / "artifacts")
    eng = ArtifactEngine(cfg)
    yield eng
    eng.close()


# ── helpers ──


@pytest.mark.asyncio
async def test_truncate_summary():
    from fusion_artifacts_engine.engine import _truncate_summary

    assert _truncate_summary("short") == "short"
    long = "a" * 300
    result = _truncate_summary(long)
    assert len(result) <= 200
    assert "\n" not in result


@pytest.mark.asyncio
async def test_auto_changelog():
    from fusion_artifacts_engine.engine import _auto_changelog

    assert "+3 lines" in _auto_changelog("a\n", "a\nb\nc\nd\n")
    assert "-2 lines" in _auto_changelog("a\nb\nc\n", "a\n")
    assert "content modified" in _auto_changelog("abc", "xyz")
    assert "no change" in _auto_changelog("same", "same")


@pytest.mark.asyncio
async def test_size_bytes():
    from fusion_artifacts_engine.engine import _size_bytes

    assert _size_bytes("hello") == 5
    assert _size_bytes("你好") == 6


# ── create_external ──


@pytest.mark.asyncio
async def test_create_external_artifact(engine):
    art, _ver, _ref = await engine.create_external_artifact(
        source_module="fusion-mlx",
        workspace_id="ws-001",
        name="model_output.py",
        artifact_type="code",
        content="print('hello')",
        workflow_run_id="run-123",
    )
    assert art.source_module == "fusion-mlx"
    assert art.workspace_id == "ws-001"
    assert art.workflow_run_id == "run-123"
    assert art.session_id == "ext_fusion-mlx_ws-001"


@pytest.mark.asyncio
async def test_create_external_with_project_kb(engine):
    art, _ver, _ref = await engine.create_external_artifact(
        source_module="fusion-mlx",
        workspace_id="ws-002",
        name="kb.py",
        artifact_type="code",
        content="x",
        project_id="proj-kb",
    )
    assert art.in_project_kb is True


@pytest.mark.asyncio
async def test_create_external_auto_kind(engine):
    art, _, _ = await engine.create_external_artifact(
        source_module="mod",
        workspace_id="ws",
        name="page.html",
        artifact_type="html",
        content="<html></html>",
    )
    assert art.kind == "app"


# ── list_by_source ──


@pytest.mark.asyncio
async def test_list_by_source(engine):
    await engine.create_external_artifact("mod-a", "ws1", "a.py", "code", "a")
    await engine.create_external_artifact("mod-a", "ws2", "b.py", "code", "b")
    await engine.create_external_artifact("mod-b", "ws1", "c.py", "code", "c")
    result = engine.list_by_source("mod-a")
    assert len(result) == 2
    result2 = engine.list_by_source("mod-a", workspace_id="ws2")
    assert len(result2) == 1


# ── get_version_content ──


@pytest.mark.asyncio
async def test_get_version_content_missing_artifact(engine):
    result = engine.get_version_content("art_nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_version_content_specific_version(engine):
    art, _, _ = await engine.create_artifact("s1", "f.py", "code", "v1")
    await engine.create_version(art.id, "v2")
    v1 = engine.get_version_content(art.id, 1)
    assert v1 is not None
    assert v1.content == "v1"


# ── should_create_artifact ──


def test_should_create_artifact_code(engine):
    long_code = "\n".join(["x"] * 35)
    assert engine.should_create_artifact(long_code, "code") is True
    assert engine.should_create_artifact("short", "code") is False


def test_should_create_artifact_text(engine):
    long_text = "a" * 2000
    assert engine.should_create_artifact(long_text, "text") is True
    assert engine.should_create_artifact("short", "text") is False


# ── export_code ──


@pytest.mark.asyncio
async def test_export_code_with_language(engine):
    art, _, _ = await engine.create_artifact("s1", "hello.py", "code", "print('hi')")
    result = engine.export_code(art.id, "python")
    assert result["language"] == "python"
    assert result["ext"] == "py"
    assert result["code"] == "print('hi')"


@pytest.mark.asyncio
async def test_export_code_no_language_infers_from_name(engine):
    art, _, _ = await engine.create_artifact("s1", "app.js", "code", "console.log(1)")
    result = engine.export_code(art.id, "")
    assert result["ext"] == "js"
    assert result["language"] == "javascript"


@pytest.mark.asyncio
async def test_export_code_no_ext_infers_from_type(engine):
    art, _, _ = await engine.create_artifact("s1", "README", "markdown", "# hello")
    result = engine.export_code(art.id, "")
    assert result["ext"] == "md"


@pytest.mark.asyncio
async def test_export_code_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.export_code("art_nonexistent")


# ── import_code ──


@pytest.mark.asyncio
async def test_import_code_python(engine):
    art, _ver, _ref = await engine.import_code("s1", "def foo(): pass", "python")
    assert art.type == "code"
    assert art.name.endswith(".py")


@pytest.mark.asyncio
async def test_import_code_html(engine):
    art, _, _ = await engine.import_code("s1", "<html></html>", "html")
    assert art.type == "html"


@pytest.mark.asyncio
async def test_import_code_with_name(engine):
    art, _, _ = await engine.import_code("s1", "code", "python", name="custom.py")
    assert art.name == "custom.py"


@pytest.mark.asyncio
async def test_import_code_default_name(engine):
    art, _, _ = await engine.import_code("s1", "code", "")
    assert art.name == "imported.txt"


# ── watchers ──


@pytest.mark.asyncio
async def test_register_and_unregister_watcher(engine):
    art, _, _ = await engine.create_artifact("s1", "w.py", "code", "x")
    engine.register_watcher(art.id, "w1")
    engine.register_watcher(art.id, "w1")  # duplicate ignored
    assert len(engine._watchers[art.id]) == 1
    engine.unregister_watcher(art.id, "w1")
    assert art.id not in engine._watchers


@pytest.mark.asyncio
async def test_get_watch_events(engine):
    art, _, _ = await engine.create_artifact("s1", "w.py", "code", "v1")
    await engine.create_version(art.id, "v2")
    events = engine.get_watch_events(art.id, since_version=1)
    assert len(events) == 1
    assert events[0]["version"] == 2


# ── get_artifact_raw_content ──


@pytest.mark.asyncio
async def test_get_artifact_raw_content_html(engine):
    art, _, _ = await engine.create_artifact("s1", "page.html", "html", "<html></html>")
    raw = engine.get_artifact_raw_content(art.id)
    assert raw is not None
    assert raw["content_type"] == "text/html"


@pytest.mark.asyncio
async def test_get_artifact_raw_content_markdown(engine):
    art, _, _ = await engine.create_artifact("s1", "doc.md", "markdown", "# Hello")
    raw = engine.get_artifact_raw_content(art.id)
    assert raw is not None
    assert raw["content_type"] == "text/markdown"


@pytest.mark.asyncio
async def test_get_artifact_raw_content_not_found(engine):
    raw = engine.get_artifact_raw_content("art_nonexistent")
    assert raw is None


# ── rename ──


@pytest.mark.asyncio
async def test_rename_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "old.py", "code", "x")
    ok = engine.rename_artifact(art.id, "new.py")
    assert ok
    got = engine.get_artifact(art.id)
    assert got.name == "new.py"


@pytest.mark.asyncio
async def test_rename_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.rename_artifact("art_nonexistent", "x")


# ── star ──


@pytest.mark.asyncio
async def test_star_unstar_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "s.py", "code", "x")
    ok = engine.star_artifact(art.id, True)
    assert ok
    got = engine.get_artifact(art.id)
    assert got.is_starred is True
    ok2 = engine.star_artifact(art.id, False)
    assert ok2


@pytest.mark.asyncio
async def test_star_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.star_artifact("art_nonexistent", True)


# ── pin ──


@pytest.mark.asyncio
async def test_pin_unpin_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "p.py", "code", "x")
    ok = engine.pin_artifact(art.id, chat_id="chat1", pinned=True)
    assert ok
    got = engine.get_artifact(art.id)
    assert got.is_pinned is True


@pytest.mark.asyncio
async def test_pin_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.pin_artifact("art_nonexistent")


# ── duplicate ──


@pytest.mark.asyncio
async def test_duplicate_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "orig.py", "code", "x")
    dup = engine.duplicate_artifact(art.id, new_name="copy.py")
    assert dup is not None
    assert dup.name == "copy.py"
    assert dup.id != art.id


@pytest.mark.asyncio
async def test_duplicate_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.duplicate_artifact("art_nonexistent")


# ── list_all ──


@pytest.mark.asyncio
async def test_list_all_artifacts(engine):
    await engine.create_artifact("s1", "a.py", "code", "a")
    await engine.create_artifact("s2", "b.py", "code", "b")
    _artifacts, total = engine.list_all_artifacts()
    assert total >= 2


# ── recycle bin ──


@pytest.mark.asyncio
async def test_list_recycle_empty(engine):
    _artifacts, total = engine.list_recycle()
    assert total == 0


@pytest.mark.asyncio
async def test_restore_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "r.py", "code", "x")
    engine.delete_artifact(art.id, soft_delete=True)
    ok = engine.restore_artifact(art.id)
    assert ok
    got = engine.get_artifact(art.id)
    assert got.is_deleted is False


@pytest.mark.asyncio
async def test_purge_expired(engine):
    art, _, _ = await engine.create_artifact("s1", "exp.py", "code", "x")
    engine.delete_artifact(art.id, soft_delete=True)
    count = engine.purge_expired()
    assert isinstance(count, int)


# ── optimistic lock ──


@pytest.mark.asyncio
async def test_create_version_optimistic_lock_fail(engine):
    art, _, _ = await engine.create_artifact("s1", "lock.py", "code", "v1")
    await engine.create_version(art.id, "v2")
    art = engine.storage.get_artifact(art.id)
    # P0-2: 乐观锁失败映射 ConflictError(-32002, 可重试) 而非 ValueError(-32603)
    from fusion_artifacts_engine.rpc.errors import ConflictError

    with pytest.raises(ConflictError, match="Optimistic lock failed"):
        await engine.create_version(art.id, "v3", expected_content_hash="wrong_hash")


@pytest.mark.asyncio
async def test_create_version_auto_changelog(engine):
    art, _, _ = await engine.create_artifact("s1", "log.py", "code", "line1\n")
    v2, _ = await engine.create_version(art.id, "line1\nline2\nline3\n")
    assert "lines" in v2.change_log or "modified" in v2.change_log


@pytest.mark.asyncio
async def test_create_version_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        await engine.create_version("art_nonexistent", "x")


# ── share ──


@pytest.mark.asyncio
async def test_create_share(engine):
    art, _, _ = await engine.create_artifact("s1", "sh.py", "code", "share me")
    share = engine.create_share(art.id, created_by="user1")
    assert share.share_id.startswith("shr_")
    assert share.artifact_id == art.id


@pytest.mark.asyncio
async def test_create_share_idempotent(engine):
    art, _, _ = await engine.create_artifact("s1", "sh2.py", "code", "x")
    s1 = engine.create_share(art.id)
    s2 = engine.create_share(art.id)
    assert s1.share_id == s2.share_id


@pytest.mark.asyncio
async def test_create_share_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.create_share("art_nonexistent")


@pytest.mark.asyncio
async def test_get_shared_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "gsh.py", "code", "shared content")
    share = engine.create_share(art.id)
    result = engine.get_shared_artifact(share.share_id)
    assert result is not None
    assert "content" in result
    assert result["content"] == "shared content"


@pytest.mark.asyncio
async def test_get_shared_not_found(engine):
    result = engine.get_shared_artifact("shr_nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_revoke_share(engine):
    art, _, _ = await engine.create_artifact("s1", "rev.py", "code", "x")
    share = engine.create_share(art.id)
    ok = engine.revoke_share(share.share_id)
    assert ok
    result = engine.get_shared_artifact(share.share_id)
    assert result is None


# ── snapshots ──


@pytest.mark.asyncio
async def test_create_snapshot(engine):
    art, _, _ = await engine.create_artifact("s1", "snap.py", "code", "v1")
    snap = await engine.create_snapshot(art.id, label="milestone", author="user1")
    assert snap.snapshot_type == "named"
    assert snap.snapshot_label == "milestone"
    assert snap.author == "user1"


@pytest.mark.asyncio
async def test_list_snapshots(engine):
    art, _, _ = await engine.create_artifact("s1", "sn.py", "code", "v1")
    await engine.create_snapshot(art.id, label="s1")
    snapshots = engine.list_snapshots(art.id)
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_create_snapshot_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        await engine.create_snapshot("art_nonexistent", label="x")


# ── folders ──


@pytest.mark.asyncio
async def test_create_folder(engine):
    folder = engine.create_folder("Test Folder", parent_id=None, project_id=None)
    assert folder.folder_id.startswith("fld_")
    assert folder.name == "Test Folder"


@pytest.mark.asyncio
async def test_list_folders(engine):
    engine.create_folder("F1")
    engine.create_folder("F2")
    folders = engine.list_folders()
    assert len(folders) >= 2


@pytest.mark.asyncio
async def test_rename_folder(engine):
    folder = engine.create_folder("Old Name")
    ok = engine.rename_folder(folder.folder_id, "New Name")
    assert ok


@pytest.mark.asyncio
async def test_delete_folder(engine):
    folder = engine.create_folder("To Delete")
    ok = engine.delete_folder(folder.folder_id)
    assert ok


@pytest.mark.asyncio
async def test_move_to_folder(engine):
    art, _, _ = await engine.create_artifact("s1", "mf.py", "code", "x")
    folder = engine.create_folder("My Folder")
    ok = engine.move_to_folder(art.id, folder.folder_id)
    assert ok


# ── tags ──


@pytest.mark.asyncio
async def test_add_tag(engine):
    art, _, _ = await engine.create_artifact("s1", "tag.py", "code", "x")
    tag = engine.add_tag(art.id, "important")
    assert tag.name == "important"


@pytest.mark.asyncio
async def test_add_tag_reuses_existing(engine):
    art1, _, _ = await engine.create_artifact("s1", "t1.py", "code", "x")
    art2, _, _ = await engine.create_artifact("s1", "t2.py", "code", "y")
    tag1 = engine.add_tag(art1.id, "shared-tag")
    tag2 = engine.add_tag(art2.id, "shared-tag")
    assert tag1.tag_id == tag2.tag_id


@pytest.mark.asyncio
async def test_remove_tag(engine):
    art, _, _ = await engine.create_artifact("s1", "rt.py", "code", "x")
    engine.add_tag(art.id, "removeme")
    ok = engine.remove_tag(art.id, "removeme")
    assert ok


@pytest.mark.asyncio
async def test_remove_tag_not_found(engine):
    art, _, _ = await engine.create_artifact("s1", "rtnf.py", "code", "x")
    ok = engine.remove_tag(art.id, "nonexistent")
    assert ok is False


@pytest.mark.asyncio
async def test_list_tags(engine):
    art, _, _ = await engine.create_artifact("s1", "lt.py", "code", "x")
    engine.add_tag(art.id, "tag-a")
    tags = engine.list_tags()
    assert len(tags) >= 1


@pytest.mark.asyncio
async def test_list_artifact_tags(engine):
    art, _, _ = await engine.create_artifact("s1", "lat.py", "code", "x")
    engine.add_tag(art.id, "t1")
    engine.add_tag(art.id, "t2")
    tags = engine.list_artifact_tags(art.id)
    assert len(tags) == 2


@pytest.mark.asyncio
async def test_add_tag_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.add_tag("art_nonexistent", "tag")


# ── events ──


@pytest.mark.asyncio
async def test_emit_event(engine):
    event = engine.emit_event(
        "artifact.created",
        artifact_id="art_123",
        session_id="s1",
        payload={"key": "val"},
    )
    assert event.event_id.startswith("evt_")
    assert event.event_type == "artifact.created"


@pytest.mark.asyncio
async def test_list_events(engine):
    engine.emit_event("test.event", session_id="s1")
    engine.emit_event("test.event", session_id="s1")
    _events, total = engine.list_events(session_id="s1")
    assert total >= 2


# ── project KB ──


@pytest.mark.asyncio
async def test_move_to_project_kb(engine):
    art, _, _ = await engine.create_artifact(
        "s1", "kb.py", "code", "x", project_id="p1"
    )
    ok = engine.move_to_project_kb(art.id, "p1")
    assert ok


@pytest.mark.asyncio
async def test_move_to_project_kb_not_found(engine):
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.move_to_project_kb("art_nonexistent", "p1")


# ── create_artifact with auto-infer ──


@pytest.mark.asyncio
async def test_create_artifact_auto_summary(engine):
    art, _, _ = await engine.create_artifact(
        "s1", "auto.py", "code", "some content here"
    )
    assert art.summary == "some content here"


@pytest.mark.asyncio
async def test_create_artifact_auto_kind(engine):
    art, _, _ = await engine.create_artifact("s1", "page.html", "html", "<html></html>")
    assert art.kind == "app"


@pytest.mark.asyncio
async def test_create_artifact_with_metadata(engine):
    art, _, _ = await engine.create_artifact(
        "s1",
        "meta.py",
        "code",
        "x",
        metadata={"framework": "flask"},
    )
    assert art.metadata == {"framework": "flask"}
