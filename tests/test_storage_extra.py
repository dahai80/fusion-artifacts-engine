import shutil
import tempfile
import time
from pathlib import Path

import pytest

from fusion_artifacts_engine.models import (
    Artifact,
    ArtifactEvent,
    ArtifactFolder,
    ArtifactShare,
    ArtifactTag,
    ArtifactVersion,
)
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage


@pytest.fixture
def storage():
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "test.db"
    content_dir = tmp / "content"
    s = SQLiteStorage(db_path, content_dir)
    yield s
    s.close()
    shutil.rmtree(tmp)


def _make_artifact(
    artifact_id="art1", session_id="sess1", name="test.py", atype="code", **kw
):
    now = time.time()
    return Artifact(
        id=artifact_id,
        session_id=session_id,
        name=name,
        type=atype,
        current_version=1,
        summary="",
        created_at=now,
        updated_at=now,
        **kw,
    )


def _make_version(artifact_id="art1", version_num=1, content="hello"):
    return ArtifactVersion(
        artifact_id=artifact_id,
        version_num=version_num,
        content=content,
        size_bytes=len(content.encode("utf-8")),
        change_log="",
        source="manual",
        created_at=time.time(),
    )


# ── save_artifact_and_version ──


def test_save_artifact_and_version(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    got = storage.get_artifact("art1")
    assert got is not None
    assert got.name == "test.py"
    v = storage.get_version("art1", 1)
    assert v is not None
    assert v.content == "hello"


# ── save_artifact (update) ──


def test_save_artifact_update(storage):
    art = _make_artifact()
    storage.save_artifact(art)
    art2 = _make_artifact(name="renamed.py")
    storage.save_artifact(art2)
    got = storage.get_artifact("art1")
    assert got.name == "renamed.py"


# ── large content goes to file ──


def test_large_content_to_file(storage):
    art = _make_artifact()
    big_content = "x" * 20000
    ver = _make_version(content=big_content)
    storage.save_artifact_and_version(art, ver)
    v = storage.get_version("art1", 1)
    assert v.content == big_content


# ── list_artifacts with metadata_filter ──


def test_list_artifacts_metadata_filter(storage):
    art = _make_artifact(metadata={"lang": "python"})
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    results = storage.list_artifacts("sess1", metadata_filter={"lang": "python"})
    assert len(results) == 1


# ── list_all_artifacts with filters ──


def test_list_all_with_filters(storage):
    art = _make_artifact(kind="app", project_id="p1", is_starred=True, is_pinned=True)
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)

    _arts, total = storage.list_all_artifacts(filters={"kind": "app"})
    assert total >= 1

    _arts2, total2 = storage.list_all_artifacts(filters={"type": "code"})
    assert total2 >= 1

    _arts3, total3 = storage.list_all_artifacts(filters={"is_starred": True})
    assert total3 >= 1

    _arts4, total4 = storage.list_all_artifacts(filters={"is_pinned": True})
    assert total4 >= 1

    _arts5, total5 = storage.list_all_artifacts(filters={"project_id": "p1"})
    assert total5 >= 1


def test_list_all_name_search(storage):
    art = _make_artifact(name="unique_name.py")
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    _arts, total = storage.list_all_artifacts(filters={"name_search": "unique"})
    assert total >= 1


def test_list_all_sort_options(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)

    for sort_key in ["updated_at", "created_at", "name", "starred"]:
        _arts, total = storage.list_all_artifacts(sort=sort_key)
        assert total >= 1


# ── delete artifact ──


def test_delete_artifact_soft(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.delete_artifact("art1", soft_delete=True)
    assert ok is True
    got = storage.get_artifact("art1")
    assert got.is_deleted is True


def test_delete_artifact_hard(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.delete_artifact("art1", soft_delete=False)
    assert ok is True
    got = storage.get_artifact("art1")
    assert got is None


def test_delete_with_project_id(storage):
    art = _make_artifact(project_id="proj1")
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.delete_artifact("art1", project_id="proj1")
    assert ok is True


def test_delete_wrong_project_id(storage):
    art = _make_artifact(project_id="proj1")
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.delete_artifact("art1", project_id="wrong")
    assert ok is False


# ── rename / star / pin ──


def test_rename_artifact(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.rename_artifact("art1", "new_name.py")
    assert ok is True
    got = storage.get_artifact("art1")
    assert got.name == "new_name.py"


def test_star_artifact(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.star_artifact("art1", True)
    assert ok is True
    got = storage.get_artifact("art1")
    assert got.is_starred is True


def test_pin_artifact(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.pin_artifact("art1", "chat1", True)
    assert ok is True
    got = storage.get_artifact("art1")
    assert got.is_pinned is True


# ── duplicate ──


def test_duplicate_artifact(storage):
    art = _make_artifact()
    ver = _make_version(content="dup me")
    storage.save_artifact_and_version(art, ver)
    dup = storage.duplicate_artifact("art1", "art2", "copy.py")
    assert dup is not None
    assert dup.name == "copy.py"
    v = storage.get_version("art2", 1)
    assert v.content == "dup me"


def test_duplicate_not_found(storage):
    dup = storage.duplicate_artifact("nonexistent", "art2")
    assert dup is None


# ── content hash / active session ──


def test_update_content_hash(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    storage.update_content_hash("art1", "abc123")
    got = storage.get_artifact("art1")
    assert got.content_hash == "abc123"


def test_set_active_session(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    storage.set_active_session("art1", "new_session")
    got = storage.get_artifact("art1")
    assert got.active_in_session == "new_session"


# ── recycle bin ──


def test_list_recycle(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    storage.delete_artifact("art1", soft_delete=True)
    _arts, total = storage.list_recycle()
    assert total >= 1


def test_restore_artifact(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    storage.delete_artifact("art1", soft_delete=True)
    ok = storage.restore_artifact("art1")
    assert ok is True
    got = storage.get_artifact("art1")
    assert got.is_deleted is False


def test_purge_expired(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    storage.delete_artifact("art1", soft_delete=True)
    count = storage.purge_expired(retention_days=0)
    assert count >= 1


# ── versions ──


def test_save_version_collision_retry(storage):
    art = _make_artifact()
    ver1 = _make_version()
    storage.save_artifact_and_version(art, ver1)
    ver2 = _make_version(version_num=1, content="v2")
    storage.save_version(ver2)
    v = storage.get_version("art1", 2)
    assert v is not None
    assert v.content == "v2"


def test_next_version_num(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    n = storage.next_version_num("art1")
    assert n == 2


def test_list_versions(storage):
    art = _make_artifact()
    ver1 = _make_version()
    storage.save_artifact_and_version(art, ver1)
    ver2 = _make_version(version_num=2, content="v2")
    storage.save_version(ver2)
    versions = storage.list_versions("art1")
    assert len(versions) == 2


def test_list_snapshots(storage):
    art = _make_artifact()
    ver1 = _make_version()
    storage.save_artifact_and_version(art, ver1)
    ver2 = ArtifactVersion(
        artifact_id="art1",
        version_num=2,
        content="snap",
        size_bytes=4,
        change_log="",
        source="manual",
        created_at=time.time(),
        snapshot_type="named",
        snapshot_label="milestone",
    )
    storage.save_version(ver2)
    snaps = storage.list_snapshots("art1")
    assert len(snaps) == 1
    assert snaps[0].snapshot_type == "named"


# ── shares ──


def test_share_crud(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    share = ArtifactShare(
        share_id="shr1",
        artifact_id="art1",
        created_by="user1",
        created_at="2026-01-01",
        expires_at=None,
        revoked=False,
        access_count=0,
        last_access_at=None,
    )
    storage.save_share(share)
    got = storage.get_share("shr1")
    assert got is not None
    assert got.artifact_id == "art1"

    by_art = storage.get_share_by_artifact("art1")
    assert by_art is not None

    storage.increment_share_access("shr1")
    got2 = storage.get_share("shr1")
    assert got2.access_count == 1

    ok = storage.revoke_share("shr1")
    assert ok is True
    got3 = storage.get_share("shr1")
    assert got3.revoked is True


def test_get_share_not_found(storage):
    assert storage.get_share("nonexistent") is None


def test_get_share_by_artifact_not_found(storage):
    assert storage.get_share_by_artifact("nonexistent") is None


def test_revoke_share_not_found(storage):
    ok = storage.revoke_share("nonexistent")
    assert ok is False


# ── folders ──


def test_folder_crud(storage):
    folder = ArtifactFolder(
        folder_id="f1",
        name="Test",
        parent_id=None,
        project_id="p1",
        created_at="2026-01-01",
    )
    storage.save_folder(folder)
    got = storage.get_folder("f1")
    assert got is not None
    assert got.name == "Test"

    folders = storage.list_folders(project_id="p1")
    assert len(folders) >= 1

    all_folders = storage.list_folders()
    assert len(all_folders) >= 1

    ok = storage.rename_folder("f1", "Renamed")
    assert ok is True
    got2 = storage.get_folder("f1")
    assert got2.name == "Renamed"

    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok2 = storage.move_to_folder("art1", "f1")
    assert ok2 is True

    ok3 = storage.delete_folder("f1")
    assert ok3 is True


def test_get_folder_not_found(storage):
    assert storage.get_folder("nonexistent") is None


# ── tags ──


def test_tag_crud(storage):
    tag = ArtifactTag(tag_id="t1", name="important", color="#ff0000")
    storage.save_tag(tag)
    got = storage.get_tag("t1")
    assert got is not None
    assert got.name == "important"

    by_name = storage.get_tag_by_name("important")
    assert by_name is not None

    tags = storage.list_tags()
    assert len(tags) >= 1

    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.add_artifact_tag("art1", "t1")
    assert ok is True

    ok_dup = storage.add_artifact_tag("art1", "t1")
    assert ok_dup is False

    art_tags = storage.list_artifact_tags("art1")
    assert len(art_tags) == 1

    ok2 = storage.remove_artifact_tag("art1", "t1")
    assert ok2 is True

    ok3 = storage.remove_artifact_tag("art1", "t1")
    assert ok3 is False


def test_get_tag_not_found(storage):
    assert storage.get_tag("nonexistent") is None


def test_get_tag_by_name_not_found(storage):
    assert storage.get_tag_by_name("nonexistent") is None


# ── events ──


def test_event_crud(storage):
    event = ArtifactEvent(
        event_id="e1",
        artifact_id=None,
        session_id="s1",
        event_type="test",
        payload={"k": "v"},
        created_at="2026-01-01T00:00:00",
    )
    storage.save_event(event)
    _events, total = storage.list_events(session_id="s1")
    assert total >= 1

    _events2, total2 = storage.list_events(artifact_id=None)
    assert total2 >= 1

    _events3, total3 = storage.list_events(since_ts="2025-01-01")
    assert total3 >= 1


def test_list_events_no_filters(storage):
    _events, total = storage.list_events()
    assert total >= 0


# ── move_to_project_kb ──


def test_move_to_project_kb(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    ok = storage.move_to_project_kb("art1", "proj1")
    assert ok is True
    got = storage.get_artifact("art1")
    assert got.in_project_kb is True


# ── list_by_source ──


def test_list_by_source(storage):
    art = _make_artifact(
        source_module="fusion-mlx", workspace_id="ws1", workflow_run_id="run1"
    )
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    results = storage.list_by_source("fusion-mlx")
    assert len(results) >= 1

    results2 = storage.list_by_source("fusion-mlx", workspace_id="ws1")
    assert len(results2) >= 1

    results3 = storage.list_by_source("fusion-mlx", workflow_run_id="run1")
    assert len(results3) >= 1


# ── get_artifact with project_id ──


def test_get_artifact_with_project_id(storage):
    art = _make_artifact(project_id="proj1")
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    got = storage.get_artifact("art1", project_id="proj1")
    assert got is not None

    got2 = storage.get_artifact("art1", project_id="wrong")
    assert got2 is None


# ── list_artifacts include_deleted ──


def test_list_artifacts_include_deleted(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    storage.delete_artifact("art1", soft_delete=True)
    results = storage.list_artifacts("sess1", include_deleted=True)
    assert len(results) >= 1

    results2 = storage.list_artifacts("sess1", include_deleted=False)
    assert len(results2) == 0


# ── list_all_artifacts with owner_user_id / since / until / folder_id / tag_id / in_project_kb ──


def test_list_all_owner_filter(storage):
    art = _make_artifact(owner_user_id="u1", ownership_type="owned")
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    _arts, total = storage.list_all_artifacts(filters={"owner_user_id": "u1"})
    assert total >= 1

    _arts2, total2 = storage.list_all_artifacts(filters={"ownership_type": "owned"})
    assert total2 >= 1


def test_list_all_since_until(storage):
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    now = time.time()
    _arts, total = storage.list_all_artifacts(
        filters={"since": now - 100, "until": now + 100}
    )
    assert total >= 1


def test_list_all_folder_filter(storage):
    folder = ArtifactFolder(
        folder_id="f1",
        name="Test",
        parent_id=None,
        project_id="p1",
        created_at="2026-01-01",
    )
    storage.save_folder(folder)
    art = _make_artifact(folder_id="f1")
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    _arts, total = storage.list_all_artifacts(filters={"folder_id": "f1"})
    assert total >= 1


def test_list_all_tag_filter(storage):
    tag = ArtifactTag(tag_id="t1", name="test", color=None)
    storage.save_tag(tag)
    art = _make_artifact()
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    storage.add_artifact_tag("art1", "t1")
    _arts, total = storage.list_all_artifacts(filters={"tag_id": "t1"})
    assert total >= 1


def test_list_all_kb_filter(storage):
    art = _make_artifact(in_project_kb=True)
    ver = _make_version()
    storage.save_artifact_and_version(art, ver)
    _arts, total = storage.list_all_artifacts(filters={"in_project_kb": True})
    assert total >= 1


# ── content file missing ──


def test_content_file_missing(storage):
    art = _make_artifact()
    big_content = "x" * 20000
    ver = _make_version(content=big_content)
    storage.save_artifact_and_version(art, ver)
    content_dir = storage.content_dir / "art1"
    for f in content_dir.iterdir():
        f.unlink()
    v = storage.get_version("art1", 1)
    assert v.content == ""
