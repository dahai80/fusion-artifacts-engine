import shutil
import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.models import ArtifactShare
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage


async def _make_engine(tmp: Path) -> ArtifactEngine:
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts")
    return ArtifactEngine(cfg)


@pytest.fixture
def env():
    tmp = Path(tempfile.mkdtemp())
    eng = ArtifactEngine(ArtifactEngineConfig(storage_root=tmp / "artifacts"))
    yield eng
    eng.close()
    shutil.rmtree(tmp, ignore_errors=True)


async def test_remix_creates_child_with_lineage(env):
    # #77: remix 派生副本 — parent_artifact_id 血统 + 内容复制为独立产物
    art, _, _ = await env.create_artifact(
        session_id="s1", name="board v1", artifact_type="html", content="<h1>kanban</h1>"
    )
    child = env.remix_artifact(art.id)
    assert child is not None
    assert child.parent_artifact_id == art.id
    assert child.id != art.id
    assert "remix" in child.name
    # 持久化后血统仍在
    got = env.storage.get_artifact(child.id)
    assert got.parent_artifact_id == art.id
    # 内容复制到 v1
    ver = env.get_version_content(child.id)
    assert ver.content == "<h1>kanban</h1>"


async def test_remix_missing_artifact_raises(env):
    with pytest.raises(ValueError):
        env.remix_artifact("art_nope")


async def test_share_permission_default_view(env):
    art, _, _ = await env.create_artifact(
        session_id="s1", name="doc", artifact_type="markdown", content="# hi"
    )
    share = env.create_share(art.id, created_by=None)
    assert share.permission == "view"


async def test_share_permission_upsert_same_link(env):
    # #77: 同链接改权限 — 重复 create_share 换 permission，share_id 不变
    art, _, _ = await env.create_artifact(
        session_id="s1", name="doc", artifact_type="markdown", content="# hi"
    )
    s1 = env.create_share(art.id, created_by=None, permission="view")
    s2 = env.create_share(art.id, created_by=None, permission="remix")
    assert s2.share_id == s1.share_id
    assert s2.permission == "remix"
    # 存储层已更新
    got = env.storage.get_share(s1.share_id)
    assert got.permission == "remix"


async def test_share_permission_invalid_rejected(env):
    art, _, _ = await env.create_artifact(
        session_id="s1", name="doc", artifact_type="markdown", content="# hi"
    )
    with pytest.raises(ValueError):
        env.create_share(art.id, created_by=None, permission="editor")


async def test_public_share_payload_includes_permission(env):
    art, _, _ = await env.create_artifact(
        session_id="s1", name="doc", artifact_type="markdown", content="# hi"
    )
    env.create_share(art.id, created_by=None, permission="remix")
    share_id = env.storage.get_share_by_artifact(art.id).share_id
    pub = env.get_public_share(share_id)
    assert pub["status"] == "ok"
    assert pub["share"]["permission"] == "remix"


def test_share_model_permission_validator():
    with pytest.raises(ValueError):
        ArtifactShare(share_id="shr_x", artifact_id="art_x", permission="admin")


def test_migration_adds_columns_to_existing_db():
    # #77: 既有 DB（无新列）升级 — 迁移幂等补列
    tmp = Path(tempfile.mkdtemp())
    try:
        db = tmp / "meta.db"
        import sqlite3

        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
                type TEXT NOT NULL, current_version INTEGER NOT NULL DEFAULT 1,
                summary TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE artifact_shares (
                share_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, created_by TEXT,
                created_at REAL, expires_at TEXT, revoked INTEGER DEFAULT 0,
                access_count INTEGER DEFAULT 0, max_accesses INTEGER, last_access_at REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO artifacts (id, session_id, name, type, created_at, updated_at) "
            "VALUES ('art_old', 's', 'old', 'code', 1.0, 1.0)"
        )
        conn.commit()
        conn.close()
        # 打开即触发迁移
        storage = SQLiteStorage(db_path=db, content_dir=tmp / "content")
        cur = storage._conn.execute("PRAGMA table_info(artifacts)").fetchall()
        cols = {r["name"] for r in cur}
        assert "parent_artifact_id" in cols
        cur = storage._conn.execute("PRAGMA table_info(artifact_shares)").fetchall()
        scols = {r["name"] for r in cur}
        assert "permission" in scols
        # 旧行读取不炸，parent 为 None，share 默认 view
        art = storage.get_artifact("art_old")
        assert art.parent_artifact_id is None
        storage.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
