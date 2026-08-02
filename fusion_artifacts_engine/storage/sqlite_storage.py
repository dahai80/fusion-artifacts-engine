import sqlite3
import json
import time
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional
from fusion_artifacts_engine.models import (
    Artifact, ArtifactVersion, ArtifactShare, ArtifactFolder,
    ArtifactTag, ArtifactEvent,
)
from fusion_artifacts_engine.storage.base import StorageDriver

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('code','markdown','html','react','data')),
    kind TEXT DEFAULT NULL CHECK(kind IS NULL OR kind IN ('app','code','document','game','tool','template')),
    project_id TEXT DEFAULT NULL,
    metadata TEXT DEFAULT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    summary TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    is_deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(type);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    content TEXT DEFAULT '',
    content_path TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    change_log TEXT DEFAULT '',
    source TEXT DEFAULT 'manual' CHECK(source IN ('manual','ai_generation')),
    created_at REAL NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_versions_artifact ON artifact_versions(artifact_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_artifact_version ON artifact_versions(artifact_id, version_num);

CREATE TABLE IF NOT EXISTS artifact_shares (
    share_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT,
    expires_at TEXT,
    revoked INTEGER DEFAULT 0,
    access_count INTEGER DEFAULT 0,
    last_access_at TEXT,
    UNIQUE(share_id)
);
CREATE INDEX IF NOT EXISTS idx_shares_artifact ON artifact_shares(artifact_id);

CREATE TABLE IF NOT EXISTS artifact_folders (
    folder_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT,
    project_id TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_folders_project ON artifact_folders(project_id);

CREATE TABLE IF NOT EXISTS artifact_tags (
    tag_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT
);

CREATE TABLE IF NOT EXISTS artifact_tag_map (
    artifact_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    PRIMARY KEY(artifact_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_tagmap_tag ON artifact_tag_map(tag_id);

CREATE TABLE IF NOT EXISTS artifact_events (
    event_id TEXT PRIMARY KEY,
    artifact_id TEXT,
    session_id TEXT,
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_artifact_time ON artifact_events(artifact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_session_time ON artifact_events(session_id, created_at);
"""

_MIGRATION_SQL = """
ALTER TABLE artifacts ADD COLUMN kind TEXT DEFAULT NULL CHECK(kind IS NULL OR kind IN ('app','code','document','game','tool','template'));
ALTER TABLE artifact_versions ADD COLUMN source TEXT DEFAULT 'manual' CHECK(source IN ('manual','ai_generation'));
ALTER TABLE artifacts ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE artifacts ADD COLUMN metadata TEXT DEFAULT NULL;
"""


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    keys = row.keys()
    meta_raw = row["metadata"] if "metadata" in keys else None
    metadata = None
    if meta_raw:
        try:
            metadata = json.loads(meta_raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid metadata JSON for artifact %s", row["id"])
            metadata = None
    return Artifact(
        id=row["id"],
        session_id=row["session_id"],
        name=row["name"],
        type=row["type"],
        kind=row["kind"] if "kind" in keys else None,
        project_id=row["project_id"] if "project_id" in keys else None,
        metadata=metadata,
        current_version=row["current_version"],
        summary=row["summary"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        is_deleted=bool(row["is_deleted"]),
        owner_user_id=row["owner_user_id"] if "owner_user_id" in keys else None,
        ownership_type=row["ownership_type"] if "ownership_type" in keys else "free",
        is_starred=bool(row["is_starred"]) if "is_starred" in keys else False,
        is_pinned=bool(row["is_pinned"]) if "is_pinned" in keys else False,
        pinned_chat_id=row["pinned_chat_id"] if "pinned_chat_id" in keys else None,
        share_id=row["share_id"] if "share_id" in keys else None,
        in_project_kb=bool(row["in_project_kb"]) if "in_project_kb" in keys else False,
        folder_id=row["folder_id"] if "folder_id" in keys else None,
        deleted_at=row["deleted_at"] if "deleted_at" in keys else None,
        content_hash=row["content_hash"] if "content_hash" in keys else None,
        active_in_session=row["active_in_session"] if "active_in_session" in keys else None,
        source_module=row["source_module"] if "source_module" in keys else None,
        workspace_id=row["workspace_id"] if "workspace_id" in keys else None,
        workflow_run_id=row["workflow_run_id"] if "workflow_run_id" in keys else None,
    )


def _version_from_row(row: sqlite3.Row) -> ArtifactVersion:
    keys = row.keys()
    return ArtifactVersion(
        id=row["id"],
        artifact_id=row["artifact_id"],
        version_num=row["version_num"],
        content=row["content"] or "",
        content_path=row["content_path"],
        size_bytes=row["size_bytes"],
        change_log=row["change_log"] or "",
        source=row["source"] if "source" in keys else "manual",
        created_at=row["created_at"],
        snapshot_type=row["snapshot_type"] if "snapshot_type" in keys else "auto",
        snapshot_label=row["snapshot_label"] if "snapshot_label" in keys else None,
        author=row["author"] if "author" in keys else None,
        parent_version=row["parent_version"] if "parent_version" in keys else None,
    )


def _share_from_row(row: sqlite3.Row) -> ArtifactShare:
    return ArtifactShare(
        share_id=row["share_id"],
        artifact_id=row["artifact_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked=bool(row["revoked"]),
        access_count=row["access_count"],
        last_access_at=row["last_access_at"],
    )


def _folder_from_row(row: sqlite3.Row) -> ArtifactFolder:
    return ArtifactFolder(
        folder_id=row["folder_id"],
        name=row["name"],
        parent_id=row["parent_id"],
        project_id=row["project_id"],
        created_at=row["created_at"],
    )


def _tag_from_row(row: sqlite3.Row) -> ArtifactTag:
    return ArtifactTag(
        tag_id=row["tag_id"],
        name=row["name"],
        color=row["color"],
    )


def _event_from_row(row: sqlite3.Row) -> ArtifactEvent:
    payload_raw = row["payload"]
    payload = None
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
        except (json.JSONDecodeError, TypeError):
            payload = None
    return ArtifactEvent(
        event_id=row["event_id"],
        artifact_id=row["artifact_id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        payload=payload,
        created_at=row["created_at"],
    )


class SQLiteStorage(StorageDriver):

    def __init__(self, db_path: Path, content_dir: Path, small_content_limit: int = 10240):
        self.db_path = db_path
        self.content_dir = content_dir
        self.small_content_limit = small_content_limit
        db_path.parent.mkdir(parents=True, exist_ok=True)
        content_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        self._migrate_kind_column()
        self._migrate_source_column()
        self._migrate_project_id_column()
        self._migrate_metadata_column()
        self._migrate_ownership_columns()
        self._migrate_lifecycle_columns()
        self._migrate_share_column()
        self._migrate_kb_column()
        self._migrate_snapshot_columns()
        self._migrate_source_module_columns()
        self._migrate_size_bytes_column()
        logger.info("SQLiteStorage initialized: db=%s content_dir=%s", db_path, content_dir)

    def _artifact_content_dir(self, artifact_id: str) -> Path:
        d = self.content_dir / artifact_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_content_file(self, artifact_id: str, version_num: int, content: str, ext: str = "txt") -> str:
        d = self._artifact_content_dir(artifact_id)
        path = d / f"v{version_num}.{ext}"
        path.write_text(content, encoding="utf-8")
        logger.debug("Wrote content file: %s", path)
        return str(path)

    def _read_content_file(self, content_path: str) -> str:
        p = Path(content_path)
        if p.exists():
            return p.read_text(encoding="utf-8")
        logger.error("Content file missing: %s", content_path)
        raise FileNotFoundError(f"Content file missing: {content_path}")

    # ── artifact CRUD ──────────────────────────────────────────

    def save_artifact(self, artifact: Artifact) -> None:
        with self._write_lock:
            meta_json = json.dumps(artifact.metadata) if artifact.metadata else None
            self._conn.execute(
                """INSERT INTO artifacts
                   (id, session_id, name, type, kind, project_id, metadata,
                    current_version, summary, created_at, updated_at, is_deleted,
                    owner_user_id, ownership_type, is_starred, is_pinned,
                    pinned_chat_id, share_id, in_project_kb, folder_id,
                    deleted_at, content_hash, active_in_session,
                    source_module, workspace_id, workflow_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     session_id=excluded.session_id,
                     name=excluded.name,
                     type=excluded.type,
                     kind=excluded.kind,
                     project_id=excluded.project_id,
                     metadata=excluded.metadata,
                     current_version=excluded.current_version,
                     summary=excluded.summary,
                     updated_at=excluded.updated_at,
                     is_deleted=excluded.is_deleted,
                     owner_user_id=excluded.owner_user_id,
                     ownership_type=excluded.ownership_type,
                     is_starred=excluded.is_starred,
                     is_pinned=excluded.is_pinned,
                     pinned_chat_id=excluded.pinned_chat_id,
                     share_id=excluded.share_id,
                     in_project_kb=excluded.in_project_kb,
                     folder_id=excluded.folder_id,
                     deleted_at=excluded.deleted_at,
                     content_hash=excluded.content_hash,
                     active_in_session=excluded.active_in_session,
                     source_module=excluded.source_module,
                     workspace_id=excluded.workspace_id,
                     workflow_run_id=excluded.workflow_run_id""",
                (artifact.id, artifact.session_id, artifact.name, artifact.type,
                 artifact.kind, artifact.project_id, meta_json, artifact.current_version,
                 artifact.summary, artifact.created_at, artifact.updated_at,
                 int(artifact.is_deleted), artifact.owner_user_id,
                 artifact.ownership_type, int(artifact.is_starred), int(artifact.is_pinned),
                 artifact.pinned_chat_id, artifact.share_id, int(artifact.in_project_kb),
                 artifact.folder_id, artifact.deleted_at, artifact.content_hash,
                 artifact.active_in_session,
                 artifact.source_module, artifact.workspace_id, artifact.workflow_run_id),
            )
            self._conn.commit()
        logger.info("Saved artifact: %s name=%s kind=%s", artifact.id, artifact.name, artifact.kind)

    def save_artifact_and_version(self, artifact: Artifact, version: ArtifactVersion) -> None:
        with self._write_lock:
            meta_json = json.dumps(artifact.metadata) if artifact.metadata else None
            self._conn.execute(
                """INSERT INTO artifacts
                   (id, session_id, name, type, kind, project_id, metadata,
                    current_version, summary, created_at, updated_at, is_deleted,
                    owner_user_id, ownership_type, is_starred, is_pinned,
                    pinned_chat_id, share_id, in_project_kb, folder_id,
                    deleted_at, content_hash, active_in_session,
                    source_module, workspace_id, workflow_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     session_id=excluded.session_id,
                     name=excluded.name,
                     type=excluded.type,
                     kind=excluded.kind,
                     project_id=excluded.project_id,
                     metadata=excluded.metadata,
                     current_version=excluded.current_version,
                     summary=excluded.summary,
                     updated_at=excluded.updated_at,
                     is_deleted=excluded.is_deleted,
                     owner_user_id=excluded.owner_user_id,
                     ownership_type=excluded.ownership_type,
                     is_starred=excluded.is_starred,
                     is_pinned=excluded.is_pinned,
                     pinned_chat_id=excluded.pinned_chat_id,
                     share_id=excluded.share_id,
                     in_project_kb=excluded.in_project_kb,
                     folder_id=excluded.folder_id,
                     deleted_at=excluded.deleted_at,
                     content_hash=excluded.content_hash,
                     active_in_session=excluded.active_in_session,
                     source_module=excluded.source_module,
                     workspace_id=excluded.workspace_id,
                     workflow_run_id=excluded.workflow_run_id""",
                (artifact.id, artifact.session_id, artifact.name, artifact.type,
                 artifact.kind, artifact.project_id, meta_json, artifact.current_version,
                 artifact.summary, artifact.created_at, artifact.updated_at,
                 int(artifact.is_deleted), artifact.owner_user_id,
                 artifact.ownership_type, int(artifact.is_starred), int(artifact.is_pinned),
                 artifact.pinned_chat_id, artifact.share_id, int(artifact.in_project_kb),
                 artifact.folder_id, artifact.deleted_at, artifact.content_hash,
                 artifact.active_in_session,
                 artifact.source_module, artifact.workspace_id, artifact.workflow_run_id),
            )
            content = version.content
            content_path = None
            if len(content.encode("utf-8")) > self.small_content_limit:
                ext = self._guess_ext(version.artifact_id, version.content)
                content_path = self._write_content_file(version.artifact_id, version.version_num, content, ext)
                content = ""
            self._conn.execute(
                """INSERT INTO artifact_versions
                   (artifact_id, version_num, content, content_path, size_bytes,
                    change_log, source, created_at, snapshot_type, snapshot_label,
                    author, parent_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (version.artifact_id, version.version_num, content, content_path,
                 version.size_bytes, version.change_log, version.source,
                 version.created_at, version.snapshot_type, version.snapshot_label,
                 version.author, version.parent_version),
            )
            self._conn.commit()
        logger.info("Saved artifact+version: %s v%d size=%d", artifact.id, version.version_num, version.size_bytes)

    def get_artifact(self, artifact_id: str, project_id: Optional[str] = None) -> Optional[Artifact]:
        if project_id is not None:
            cur = self._conn.execute("SELECT * FROM artifacts WHERE id = ? AND project_id = ?", (artifact_id, project_id))
        else:
            cur = self._conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _artifact_from_row(row)

    def list_artifacts(self, session_id: str, include_deleted: bool = False, project_id: Optional[str] = None, metadata_filter: Optional[dict] = None) -> list[Artifact]:
        conditions = ["session_id = ?"]
        params: list = [session_id]
        if not include_deleted:
            conditions.append("is_deleted = 0")
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if metadata_filter:
            for key, value in metadata_filter.items():
                json_path = f"$.{key}"
                conditions.append("json_extract(metadata, ?) = ?")
                params.extend([json_path, str(value) if not isinstance(value, str) else value])
        where = " AND ".join(conditions)
        cur = self._conn.execute(f"SELECT * FROM artifacts WHERE {where} ORDER BY updated_at DESC", params)
        return [_artifact_from_row(r) for r in cur.fetchall()]

    def list_all_artifacts(
        self,
        filters: Optional[dict] = None,
        sort: str = "updated_at",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Artifact], int]:
        conditions = ["is_deleted = 0"]
        params: list = []
        if filters:
            ownership_type = filters.get("ownership_type")
            if ownership_type:
                conditions.append("ownership_type = ?")
                params.append(ownership_type)
            project_id = filters.get("project_id")
            if project_id:
                conditions.append("project_id = ?")
                params.append(project_id)
            artifact_type = filters.get("type")
            if artifact_type:
                conditions.append("type = ?")
                params.append(artifact_type)
            kind = filters.get("kind")
            if kind:
                conditions.append("kind = ?")
                params.append(kind)
            is_starred = filters.get("is_starred")
            if is_starred is not None:
                conditions.append("is_starred = ?")
                params.append(int(is_starred))
            is_pinned = filters.get("is_pinned")
            if is_pinned is not None:
                conditions.append("is_pinned = ?")
                params.append(int(is_pinned))
            folder_id = filters.get("folder_id")
            if folder_id:
                conditions.append("folder_id = ?")
                params.append(folder_id)
            tag_id = filters.get("tag_id")
            if tag_id:
                conditions.append("id IN (SELECT artifact_id FROM artifact_tag_map WHERE tag_id = ?)")
                params.append(tag_id)
            in_project_kb = filters.get("in_project_kb")
            if in_project_kb is not None:
                conditions.append("in_project_kb = ?")
                params.append(int(in_project_kb))
            name_search = filters.get("name_search")
            if name_search:
                conditions.append("name LIKE ?")
                params.append(f"%{name_search}%")
            owner_user_id = filters.get("owner_user_id")
            if owner_user_id:
                conditions.append("owner_user_id = ?")
                params.append(owner_user_id)
            since = filters.get("since")
            if since is not None:
                conditions.append("created_at >= ?")
                params.append(float(since))
            until = filters.get("until")
            if until is not None:
                conditions.append("created_at <= ?")
                params.append(float(until))
        where = " AND ".join(conditions)
        count_cur = self._conn.execute(f"SELECT COUNT(*) FROM artifacts WHERE {where}", params)
        total = count_cur.fetchone()[0]
        sort_map = {"updated_at": "updated_at DESC", "created_at": "created_at DESC", "name": "name ASC", "starred": "is_starred DESC, updated_at DESC"}
        order = sort_map.get(sort, "updated_at DESC")
        offset = (page - 1) * page_size
        cur = self._conn.execute(
            f"SELECT * FROM artifacts WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        artifacts = [_artifact_from_row(r) for r in cur.fetchall()]
        logger.info("list_all_artifacts: %d/%d page=%d sort=%s", len(artifacts), total, page, sort)
        return artifacts, total

    def delete_artifact(self, artifact_id: str, soft_delete: bool = True, project_id: Optional[str] = None) -> bool:
        if project_id is not None:
            art = self.get_artifact(artifact_id, project_id=project_id)
            if art is None:
                logger.warning("Delete denied: artifact %s not found in project %s", artifact_id, project_id)
                return False
        if soft_delete:
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            with self._write_lock:
                cur = self._conn.execute(
                    "UPDATE artifacts SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE id = ?",
                    (now_iso, time.time(), artifact_id),
                )
                self._conn.commit()
            ok = cur.rowcount > 0
        else:
            with self._write_lock:
                cur = self._conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
                self._conn.commit()
            ok = cur.rowcount > 0
            if ok:
                content_dir = self.content_dir / artifact_id
                if content_dir.exists():
                    shutil.rmtree(content_dir, ignore_errors=True)
                    logger.debug("Cleaned up content dir: %s", content_dir)
        logger.info("Deleted artifact %s soft=%s ok=%s", artifact_id, soft_delete, ok)
        return ok

    def rename_artifact(self, artifact_id: str, new_name: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET name = ?, updated_at = ? WHERE id = ?",
                (new_name, time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Renamed artifact %s -> %s ok=%s", artifact_id, new_name, ok)
        return ok

    def star_artifact(self, artifact_id: str, starred: bool) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET is_starred = ?, updated_at = ? WHERE id = ?",
                (int(starred), time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Star artifact %s starred=%s ok=%s", artifact_id, starred, ok)
        return ok

    def pin_artifact(self, artifact_id: str, chat_id: Optional[str], pinned: bool) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET is_pinned = ?, pinned_chat_id = ?, updated_at = ? WHERE id = ?",
                (int(pinned), chat_id, time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Pin artifact %s pinned=%s chat=%s ok=%s", artifact_id, pinned, chat_id, ok)
        return ok

    def duplicate_artifact(self, artifact_id: str, new_id: str, new_name: Optional[str] = None) -> Optional[Artifact]:
        art = self.get_artifact(artifact_id)
        if art is None:
            return None
        now = time.time()
        dup = Artifact(
            id=new_id,
            session_id=art.session_id,
            name=new_name or f"{art.name} (copy)",
            type=art.type,
            kind=art.kind,
            project_id=art.project_id,
            metadata=art.metadata,
            current_version=1,
            summary=art.summary,
            created_at=now,
            updated_at=now,
            owner_user_id=art.owner_user_id,
            ownership_type=art.ownership_type,
            folder_id=art.folder_id,
        )
        ver = self.get_version(artifact_id, art.current_version)
        if ver is None:
            return None
        new_ver = ArtifactVersion(
            artifact_id=new_id,
            version_num=1,
            content=ver.content,
            size_bytes=ver.size_bytes,
            change_log=f"Duplicated from {artifact_id}",
            source="manual",
            created_at=now,
        )
        self.save_artifact_and_version(dup, new_ver)
        logger.info("Duplicated artifact %s -> %s", artifact_id, new_id)
        return dup

    def update_content_hash(self, artifact_id: str, content_hash: str) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifacts SET content_hash = ?, updated_at = ? WHERE id = ?",
                (content_hash, time.time(), artifact_id),
            )
            self._conn.commit()
        logger.debug("Updated content_hash for %s", artifact_id)

    def set_active_session(self, artifact_id: str, session_id: Optional[str]) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifacts SET active_in_session = ?, updated_at = ? WHERE id = ?",
                (session_id, time.time(), artifact_id),
            )
            self._conn.commit()

    # ── recycle bin ────────────────────────────────────────────

    def list_recycle(self, page: int = 1, page_size: int = 20) -> tuple[list[Artifact], int]:
        conditions = ["is_deleted = 1"]
        params: list = []
        where = " AND ".join(conditions)
        count_cur = self._conn.execute(f"SELECT COUNT(*) FROM artifacts WHERE {where}", params)
        total = count_cur.fetchone()[0]
        offset = (page - 1) * page_size
        cur = self._conn.execute(
            f"SELECT * FROM artifacts WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        artifacts = [_artifact_from_row(r) for r in cur.fetchall()]
        return artifacts, total

    def restore_artifact(self, artifact_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET is_deleted = 0, deleted_at = NULL, updated_at = ? WHERE id = ?",
                (time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Restored artifact %s ok=%s", artifact_id, ok)
        return ok

    def purge_expired(self, retention_days: int = 7) -> int:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT id FROM artifacts WHERE is_deleted = 1 AND deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            )
            expired_ids = [row["id"] for row in cur.fetchall()]
            count = 0
            for aid in expired_ids:
                self._conn.execute("DELETE FROM artifact_tag_map WHERE artifact_id = ?", (aid,))
                self._conn.execute("DELETE FROM artifact_events WHERE artifact_id = ?", (aid,))
                self._conn.execute("DELETE FROM artifact_versions WHERE artifact_id = ?", (aid,))
                self._conn.execute("DELETE FROM artifacts WHERE id = ?", (aid,))
                content_dir = self.content_dir / aid
                if content_dir.exists():
                    shutil.rmtree(content_dir, ignore_errors=True)
                count += 1
            self._conn.commit()
        logger.info("Purged %d expired artifacts (retention=%d days)", count, retention_days)
        return count

    # ── versions ───────────────────────────────────────────────

    def save_version(self, version: ArtifactVersion) -> None:
        content = version.content
        content_path = None
        if len(content.encode("utf-8")) > self.small_content_limit:
            ext = self._guess_ext(version.artifact_id, version.content)
            content_path = self._write_content_file(version.artifact_id, version.version_num, content, ext)
            content = ""
        with self._write_lock:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    self._conn.execute(
                        """INSERT INTO artifact_versions
                           (artifact_id, version_num, content, content_path, size_bytes,
                            change_log, source, created_at, snapshot_type, snapshot_label,
                            author, parent_version)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (version.artifact_id, version.version_num, content, content_path,
                         version.size_bytes, version.change_log, version.source,
                         version.created_at, version.snapshot_type, version.snapshot_label,
                         version.author, version.parent_version),
                    )
                    self._conn.commit()
                    logger.info("Saved version: %s v%d size=%d", version.artifact_id, version.version_num, version.size_bytes)
                    return
                except sqlite3.IntegrityError:
                    if attempt >= max_retries - 1:
                        raise
                    version.version_num = self.next_version_num(version.artifact_id)
                    logger.warning("Version collision for %s, retrying with v%d", version.artifact_id, version.version_num)

    def next_version_num(self, artifact_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(version_num), 0) + 1 FROM artifact_versions WHERE artifact_id = ?",
            (artifact_id,),
        )
        row = cur.fetchone()
        return row[0]

    def get_version(self, artifact_id: str, version_num: int) -> Optional[ArtifactVersion]:
        cur = self._conn.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? AND version_num = ?",
                                 (artifact_id, version_num))
        row = cur.fetchone()
        if row is None:
            return None
        ver = _version_from_row(row)
        if not ver.content and ver.content_path:
            try:
                ver.content = self._read_content_file(ver.content_path)
            except FileNotFoundError:
                ver.content = ""
        return ver

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        cur = self._conn.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY version_num DESC",
                                 (artifact_id,))
        results = []
        for r in cur.fetchall():
            ver = _version_from_row(r)
            if not ver.content and ver.content_path:
                try:
                    ver.content = self._read_content_file(ver.content_path)
                except FileNotFoundError:
                    ver.content = ""
            results.append(ver)
        return results

    def list_snapshots(self, artifact_id: str) -> list[ArtifactVersion]:
        cur = self._conn.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND snapshot_type IN ('named','manual') ORDER BY version_num DESC",
            (artifact_id,),
        )
        results = []
        for r in cur.fetchall():
            ver = _version_from_row(r)
            if not ver.content and ver.content_path:
                try:
                    ver.content = self._read_content_file(ver.content_path)
                except FileNotFoundError:
                    ver.content = ""
            results.append(ver)
        return results

    # ── shares ─────────────────────────────────────────────────

    def save_share(self, share: ArtifactShare) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO artifact_shares (share_id, artifact_id, created_by, created_at, expires_at, revoked, access_count, last_access_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(share_id) DO UPDATE SET
                     revoked=excluded.revoked,
                     access_count=excluded.access_count,
                     last_access_at=excluded.last_access_at""",
                (share.share_id, share.artifact_id, share.created_by, share.created_at,
                 share.expires_at, int(share.revoked), share.access_count, share.last_access_at),
            )
            self._conn.commit()
        logger.info("Saved share: %s artifact=%s", share.share_id, share.artifact_id)

    def get_share(self, share_id: str) -> Optional[ArtifactShare]:
        cur = self._conn.execute("SELECT * FROM artifact_shares WHERE share_id = ?", (share_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _share_from_row(row)

    def get_share_by_artifact(self, artifact_id: str) -> Optional[ArtifactShare]:
        cur = self._conn.execute(
            "SELECT * FROM artifact_shares WHERE artifact_id = ? AND revoked = 0 ORDER BY created_at DESC LIMIT 1",
            (artifact_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _share_from_row(row)

    def revoke_share(self, share_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifact_shares SET revoked = 1 WHERE share_id = ?", (share_id,),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Revoked share %s ok=%s", share_id, ok)
        return ok

    def increment_share_access(self, share_id: str) -> None:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifact_shares SET access_count = access_count + 1, last_access_at = ? WHERE share_id = ?",
                (now_iso, share_id),
            )
            self._conn.commit()

    # ── folders ────────────────────────────────────────────────

    def save_folder(self, folder: ArtifactFolder) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO artifact_folders (folder_id, name, parent_id, project_id, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(folder_id) DO UPDATE SET name=excluded.name, parent_id=excluded.parent_id""",
                (folder.folder_id, folder.name, folder.parent_id, folder.project_id, folder.created_at),
            )
            self._conn.commit()
        logger.info("Saved folder: %s name=%s", folder.folder_id, folder.name)

    def get_folder(self, folder_id: str) -> Optional[ArtifactFolder]:
        cur = self._conn.execute("SELECT * FROM artifact_folders WHERE folder_id = ?", (folder_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _folder_from_row(row)

    def list_folders(self, project_id: Optional[str] = None) -> list[ArtifactFolder]:
        if project_id:
            cur = self._conn.execute("SELECT * FROM artifact_folders WHERE project_id = ? ORDER BY name", (project_id,))
        else:
            cur = self._conn.execute("SELECT * FROM artifact_folders ORDER BY name")
        return [_folder_from_row(r) for r in cur.fetchall()]

    def rename_folder(self, folder_id: str, new_name: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifact_folders SET name = ? WHERE folder_id = ?", (new_name, folder_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Renamed folder %s -> %s ok=%s", folder_id, new_name, ok)
        return ok

    def delete_folder(self, folder_id: str) -> bool:
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifacts SET folder_id = NULL WHERE folder_id = ?", (folder_id,),
            )
            cur = self._conn.execute(
                "DELETE FROM artifact_folders WHERE folder_id = ?", (folder_id,),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Deleted folder %s ok=%s", folder_id, ok)
        return ok

    def move_to_folder(self, artifact_id: str, folder_id: Optional[str]) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET folder_id = ?, updated_at = ? WHERE id = ?",
                (folder_id, time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Moved artifact %s to folder %s ok=%s", artifact_id, folder_id, ok)
        return ok

    # ── tags ───────────────────────────────────────────────────

    def save_tag(self, tag: ArtifactTag) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO artifact_tags (tag_id, name, color)
                   VALUES (?, ?, ?)
                   ON CONFLICT(tag_id) DO UPDATE SET name=excluded.name, color=excluded.color""",
                (tag.tag_id, tag.name, tag.color),
            )
            self._conn.commit()
        logger.info("Saved tag: %s name=%s", tag.tag_id, tag.name)

    def get_tag(self, tag_id: str) -> Optional[ArtifactTag]:
        cur = self._conn.execute("SELECT * FROM artifact_tags WHERE tag_id = ?", (tag_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _tag_from_row(row)

    def get_tag_by_name(self, name: str) -> Optional[ArtifactTag]:
        cur = self._conn.execute("SELECT * FROM artifact_tags WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            return None
        return _tag_from_row(row)

    def list_tags(self) -> list[ArtifactTag]:
        cur = self._conn.execute("SELECT * FROM artifact_tags ORDER BY name")
        return [_tag_from_row(r) for r in cur.fetchall()]

    def add_artifact_tag(self, artifact_id: str, tag_id: str) -> bool:
        with self._write_lock:
            try:
                self._conn.execute(
                    "INSERT INTO artifact_tag_map (artifact_id, tag_id) VALUES (?, ?)",
                    (artifact_id, tag_id),
                )
                self._conn.commit()
                logger.info("Added tag %s to artifact %s", tag_id, artifact_id)
                return True
            except sqlite3.IntegrityError:
                logger.debug("Tag %s already on artifact %s", tag_id, artifact_id)
                return False

    def remove_artifact_tag(self, artifact_id: str, tag_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM artifact_tag_map WHERE artifact_id = ? AND tag_id = ?",
                (artifact_id, tag_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Removed tag %s from artifact %s ok=%s", tag_id, artifact_id, ok)
        return ok

    def list_artifact_tags(self, artifact_id: str) -> list[ArtifactTag]:
        cur = self._conn.execute(
            """SELECT t.* FROM artifact_tags t
               JOIN artifact_tag_map m ON t.tag_id = m.tag_id
               WHERE m.artifact_id = ? ORDER BY t.name""",
            (artifact_id,),
        )
        return [_tag_from_row(r) for r in cur.fetchall()]

    # ── events ─────────────────────────────────────────────────

    def save_event(self, event: ArtifactEvent) -> None:
        payload_json = json.dumps(event.payload) if event.payload else None
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO artifact_events (event_id, artifact_id, session_id, event_type, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event.event_id, event.artifact_id, event.session_id,
                 event.event_type, payload_json, event.created_at),
            )
            self._conn.commit()
        logger.info("Saved event: %s type=%s artifact=%s", event.event_id, event.event_type, event.artifact_id)

    def list_events(
        self,
        artifact_id: Optional[str] = None,
        session_id: Optional[str] = None,
        since_ts: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ArtifactEvent], int]:
        conditions = []
        params: list = []
        if artifact_id:
            conditions.append("artifact_id = ?")
            params.append(artifact_id)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if since_ts:
            conditions.append("created_at > ?")
            params.append(since_ts)
        where = " AND ".join(conditions) if conditions else "1=1"
        count_cur = self._conn.execute(f"SELECT COUNT(*) FROM artifact_events WHERE {where}", params)
        total = count_cur.fetchone()[0]
        offset = (page - 1) * page_size
        cur = self._conn.execute(
            f"SELECT * FROM artifact_events WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        events = [_event_from_row(r) for r in cur.fetchall()]
        return events, total

    def move_to_project_kb(self, artifact_id: str, project_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET in_project_kb = 1, project_id = ?, updated_at = ? WHERE id = ?",
                (project_id, time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Moved artifact %s to project_kb project=%s ok=%s", artifact_id, project_id, ok)
        return ok

    # ── migrations ─────────────────────────────────────────────

    def _guess_ext(self, artifact_id: str, content: str) -> str:
        art = self.get_artifact(artifact_id)
        if art:
            name = art.name.lower()
            if "." in name:
                return name.rsplit(".", 1)[-1]
        return "txt"

    def _migrate_kind_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "kind" not in columns:
            self._conn.executescript("ALTER TABLE artifacts ADD COLUMN kind TEXT DEFAULT NULL CHECK(kind IS NULL OR kind IN ('app','code','document','game','tool','template'));")
            self._conn.commit()
            logger.info("Migrated: added 'kind' column to artifacts table")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind)")
        self._conn.commit()

    def _migrate_source_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifact_versions)")
        columns = {row["name"] for row in cur.fetchall()}
        if "source" not in columns:
            self._conn.executescript("ALTER TABLE artifact_versions ADD COLUMN source TEXT DEFAULT 'manual' CHECK(source IN ('manual','ai_generation'));")
            self._conn.commit()
            logger.info("Migrated: added 'source' column to artifact_versions table")

    def _migrate_project_id_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "project_id" not in columns:
            self._conn.executescript("ALTER TABLE artifacts ADD COLUMN project_id TEXT DEFAULT NULL;")
            self._conn.commit()
            logger.info("Migrated: added 'project_id' column to artifacts table")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_project_id ON artifacts(project_id)")
        self._conn.commit()

    def _migrate_metadata_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "metadata" not in columns:
            self._conn.executescript("ALTER TABLE artifacts ADD COLUMN metadata TEXT DEFAULT NULL;")
            self._conn.commit()
            logger.info("Migrated: added 'metadata' column to artifacts table")

    def _migrate_ownership_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "owner_user_id" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN owner_user_id TEXT DEFAULT NULL;")
        if "ownership_type" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN ownership_type TEXT DEFAULT 'free';")
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info("Migrated: added ownership columns to artifacts (%d cols)", len(new_cols))
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_ownership ON artifacts(ownership_type, project_id)")
        self._conn.commit()

    def _migrate_lifecycle_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "is_starred" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN is_starred INTEGER DEFAULT 0;")
        if "is_pinned" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN is_pinned INTEGER DEFAULT 0;")
        if "pinned_chat_id" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN pinned_chat_id TEXT DEFAULT NULL;")
        if "folder_id" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN folder_id TEXT DEFAULT NULL;")
        if "deleted_at" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN deleted_at TEXT DEFAULT NULL;")
        if "content_hash" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN content_hash TEXT DEFAULT NULL;")
        if "active_in_session" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN active_in_session TEXT DEFAULT NULL;")
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info("Migrated: added lifecycle columns to artifacts (%d cols)", len(new_cols))
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_starred_pinned ON artifacts(is_starred, is_pinned)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_deleted ON artifacts(is_deleted, deleted_at)")
        self._conn.commit()

    def _migrate_share_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "share_id" not in columns:
            self._conn.executescript("ALTER TABLE artifacts ADD COLUMN share_id TEXT DEFAULT NULL;")
            self._conn.commit()
            logger.info("Migrated: added 'share_id' column to artifacts table")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_share_id ON artifacts(share_id) WHERE share_id IS NOT NULL")
        self._conn.commit()

    def _migrate_kb_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "in_project_kb" not in columns:
            self._conn.executescript("ALTER TABLE artifacts ADD COLUMN in_project_kb INTEGER DEFAULT 0;")
            self._conn.commit()
            logger.info("Migrated: added 'in_project_kb' column to artifacts table")

    def _migrate_snapshot_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifact_versions)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "snapshot_type" not in columns:
            new_cols.append("ALTER TABLE artifact_versions ADD COLUMN snapshot_type TEXT DEFAULT 'auto';")
        if "snapshot_label" not in columns:
            new_cols.append("ALTER TABLE artifact_versions ADD COLUMN snapshot_label TEXT DEFAULT NULL;")
        if "author" not in columns:
            new_cols.append("ALTER TABLE artifact_versions ADD COLUMN author TEXT DEFAULT NULL;")
        if "parent_version" not in columns:
            new_cols.append("ALTER TABLE artifact_versions ADD COLUMN parent_version INTEGER DEFAULT NULL;")
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info("Migrated: added snapshot columns to artifact_versions (%d cols)", len(new_cols))

    def _migrate_source_module_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "source_module" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN source_module TEXT DEFAULT NULL;")
        if "workspace_id" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN workspace_id TEXT DEFAULT NULL;")
        if "workflow_run_id" not in columns:
            new_cols.append("ALTER TABLE artifacts ADD COLUMN workflow_run_id TEXT DEFAULT NULL;")
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info("Migrated: added source_module columns (%d cols)", len(new_cols))

    def list_by_source(
        self,
        source_module: str,
        workspace_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
    ) -> list[Artifact]:
        conditions = ["is_deleted = 0", "source_module = ?"]
        params: list = [source_module]
        if workspace_id:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)
        if workflow_run_id:
            conditions.append("workflow_run_id = ?")
            params.append(workflow_run_id)
        where = " AND ".join(conditions)
        cur = self._conn.execute(f"SELECT * FROM artifacts WHERE {where} ORDER BY updated_at DESC", params)
        return [Artifact(**dict(row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
        logger.info("SQLiteStorage closed")

    def _migrate_size_bytes_column(self) -> None:
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(artifact_versions)").fetchall()]
            if "token_count" in cols and "size_bytes" not in cols:
                self._conn.execute("ALTER TABLE artifact_versions RENAME COLUMN token_count TO size_bytes")
                self._conn.commit()
                logger.info("Migrated artifact_versions: token_count -> size_bytes")
        except Exception as e:
            logger.warning("Migration size_bytes failed: %s", e)
