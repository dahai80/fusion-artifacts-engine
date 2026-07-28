import sqlite3
import json
import time
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional
from fusion_artifacts_engine.models import Artifact, ArtifactVersion
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
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_project_id ON artifacts(project_id);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    content TEXT DEFAULT '',
    content_path TEXT,
    token_count INTEGER NOT NULL DEFAULT 0,
    change_log TEXT DEFAULT '',
    source TEXT DEFAULT 'manual' CHECK(source IN ('manual','ai_generation')),
    created_at REAL NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_versions_artifact ON artifact_versions(artifact_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_artifact_version ON artifact_versions(artifact_id, version_num);
"""

_MIGRATION_SQL = """
ALTER TABLE artifacts ADD COLUMN kind TEXT DEFAULT NULL CHECK(kind IS NULL OR kind IN ('app','code','document','game','tool','template'));
ALTER TABLE artifact_versions ADD COLUMN source TEXT DEFAULT 'manual' CHECK(source IN ('manual','ai_generation'));
ALTER TABLE artifacts ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE artifacts ADD COLUMN metadata TEXT DEFAULT NULL;
"""


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    meta_raw = row["metadata"] if "metadata" in row.keys() else None
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
        kind=row["kind"] if "kind" in row.keys() else None,
        project_id=row["project_id"] if "project_id" in row.keys() else None,
        metadata=metadata,
        current_version=row["current_version"],
        summary=row["summary"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        is_deleted=bool(row["is_deleted"]),
    )


def _version_from_row(row: sqlite3.Row) -> ArtifactVersion:
    return ArtifactVersion(
        id=row["id"],
        artifact_id=row["artifact_id"],
        version_num=row["version_num"],
        content=row["content"] or "",
        content_path=row["content_path"],
        token_count=row["token_count"],
        change_log=row["change_log"] or "",
        source=row["source"] if "source" in row.keys() else "manual",
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

    def save_artifact(self, artifact: Artifact) -> None:
        with self._write_lock:
            meta_json = json.dumps(artifact.metadata) if artifact.metadata else None
            self._conn.execute(
                """INSERT INTO artifacts
                   (id, session_id, name, type, kind, project_id, metadata, current_version, summary, created_at, updated_at, is_deleted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                     is_deleted=excluded.is_deleted""",
                (artifact.id, artifact.session_id, artifact.name, artifact.type,
                 artifact.kind, artifact.project_id, meta_json, artifact.current_version, artifact.summary,
                 artifact.created_at, artifact.updated_at, int(artifact.is_deleted)),
            )
            self._conn.commit()
        logger.info("Saved artifact: %s name=%s kind=%s project_id=%s", artifact.id, artifact.name, artifact.kind, artifact.project_id)

    def save_artifact_and_version(self, artifact: Artifact, version: ArtifactVersion) -> None:
        with self._write_lock:
            meta_json = json.dumps(artifact.metadata) if artifact.metadata else None
            self._conn.execute(
                """INSERT INTO artifacts
                   (id, session_id, name, type, kind, project_id, metadata, current_version, summary, created_at, updated_at, is_deleted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                     is_deleted=excluded.is_deleted""",
                (artifact.id, artifact.session_id, artifact.name, artifact.type,
                 artifact.kind, artifact.project_id, meta_json, artifact.current_version, artifact.summary,
                 artifact.created_at, artifact.updated_at, int(artifact.is_deleted)),
            )
            content = version.content
            content_path = None
            if len(content.encode("utf-8")) > self.small_content_limit:
                ext = self._guess_ext(version.artifact_id, version.content)
                content_path = self._write_content_file(version.artifact_id, version.version_num, content, ext)
                content = ""
            self._conn.execute(
                """INSERT INTO artifact_versions
                   (artifact_id, version_num, content, content_path, token_count, change_log, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (version.artifact_id, version.version_num, content, content_path,
                 version.token_count, version.change_log, version.source, version.created_at),
            )
            self._conn.commit()
        logger.info("Saved artifact+version: %s v%d tokens=%d source=%s", artifact.id, version.version_num, version.token_count, version.source)

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

    def delete_artifact(self, artifact_id: str, soft_delete: bool = True, project_id: Optional[str] = None) -> bool:
        if project_id is not None:
            art = self.get_artifact(artifact_id, project_id=project_id)
            if art is None:
                logger.warning("Delete denied: artifact %s not found in project %s", artifact_id, project_id)
                return False
        if soft_delete:
            with self._write_lock:
                cur = self._conn.execute("UPDATE artifacts SET is_deleted = 1, updated_at = ? WHERE id = ?",
                                         (time.time(), artifact_id))
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
                           (artifact_id, version_num, content, content_path, token_count, change_log, source, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (version.artifact_id, version.version_num, content, content_path,
                         version.token_count, version.change_log, version.source, version.created_at),
                    )
                    self._conn.commit()
                    logger.info("Saved version: %s v%d tokens=%d", version.artifact_id, version.version_num, version.token_count)
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
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_project_id ON artifacts(project_id)")
            self._conn.commit()
            logger.info("Migrated: added 'project_id' column to artifacts table")

    def _migrate_metadata_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "metadata" not in columns:
            self._conn.executescript("ALTER TABLE artifacts ADD COLUMN metadata TEXT DEFAULT NULL;")
            self._conn.commit()
            logger.info("Migrated: added 'metadata' column to artifacts table")

    def close(self) -> None:
        self._conn.close()
        logger.info("SQLiteStorage closed")
