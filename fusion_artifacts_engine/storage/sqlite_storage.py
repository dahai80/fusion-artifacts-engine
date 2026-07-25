import sqlite3
import logging
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
    token_count INTEGER NOT NULL DEFAULT 0,
    change_log TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_versions_artifact ON artifact_versions(artifact_id);
"""


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        session_id=row["session_id"],
        name=row["name"],
        type=row["type"],
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
        created_at=row["created_at"],
    )


class SQLiteStorage(StorageDriver):

    def __init__(self, db_path: Path, content_dir: Path, small_content_limit: int = 10240):
        self.db_path = db_path
        self.content_dir = content_dir
        self.small_content_limit = small_content_limit
        db_path.parent.mkdir(parents=True, exist_ok=True)
        content_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.info(f"SQLiteStorage initialized: db={db_path} content_dir={content_dir}")

    def _artifact_content_dir(self, artifact_id: str) -> Path:
        d = self.content_dir / artifact_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_content_file(self, artifact_id: str, version_num: int, content: str, ext: str = "txt") -> str:
        d = self._artifact_content_dir(artifact_id)
        path = d / f"v{version_num}.{ext}"
        path.write_text(content, encoding="utf-8")
        logger.debug(f"Wrote content file: {path}")
        return str(path)

    def _read_content_file(self, content_path: str) -> str:
        p = Path(content_path)
        if p.exists():
            return p.read_text(encoding="utf-8")
        logger.warning(f"Content file missing: {content_path}")
        return ""

    def save_artifact(self, artifact: Artifact) -> None:
        self._conn.execute(
            """INSERT INTO artifacts
               (id, session_id, name, type, current_version, summary, created_at, updated_at, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 session_id=excluded.session_id,
                 name=excluded.name,
                 type=excluded.type,
                 current_version=excluded.current_version,
                 summary=excluded.summary,
                 updated_at=excluded.updated_at,
                 is_deleted=excluded.is_deleted""",
            (artifact.id, artifact.session_id, artifact.name, artifact.type,
             artifact.current_version, artifact.summary, artifact.created_at,
             artifact.updated_at, int(artifact.is_deleted)),
        )
        self._conn.commit()
        logger.info(f"Saved artifact: {artifact.id} name={artifact.name}")

    def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        cur = self._conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _artifact_from_row(row)

    def list_artifacts(self, session_id: str, include_deleted: bool = False) -> list[Artifact]:
        if include_deleted:
            cur = self._conn.execute("SELECT * FROM artifacts WHERE session_id = ? ORDER BY updated_at DESC", (session_id,))
        else:
            cur = self._conn.execute("SELECT * FROM artifacts WHERE session_id = ? AND is_deleted = 0 ORDER BY updated_at DESC", (session_id,))
        return [_artifact_from_row(r) for r in cur.fetchall()]

    def delete_artifact(self, artifact_id: str, soft_delete: bool = True) -> bool:
        if soft_delete:
            cur = self._conn.execute("UPDATE artifacts SET is_deleted = 1, updated_at = ? WHERE id = ?",
                                     (__import__("time").time(), artifact_id))
        else:
            cur = self._conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        self._conn.commit()
        ok = cur.rowcount > 0
        logger.info(f"Deleted artifact {artifact_id} soft={soft_delete} ok={ok}")
        return ok

    def save_version(self, version: ArtifactVersion) -> None:
        content = version.content
        content_path = None
        if len(content.encode("utf-8")) > self.small_content_limit:
            ext = self._guess_ext(version.artifact_id, version.content)
            content_path = self._write_content_file(version.artifact_id, version.version_num, content, ext)
            content = ""
        self._conn.execute(
            """INSERT INTO artifact_versions
               (artifact_id, version_num, content, content_path, token_count, change_log, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (version.artifact_id, version.version_num, content, content_path,
             version.token_count, version.change_log, version.created_at),
        )
        self._conn.commit()
        logger.info(f"Saved version: {version.artifact_id} v{version.version_num} tokens={version.token_count}")

    def get_version(self, artifact_id: str, version_num: int) -> Optional[ArtifactVersion]:
        cur = self._conn.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? AND version_num = ?",
                                 (artifact_id, version_num))
        row = cur.fetchone()
        if row is None:
            return None
        ver = _version_from_row(row)
        if not ver.content and ver.content_path:
            ver.content = self._read_content_file(ver.content_path)
        return ver

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        cur = self._conn.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY version_num DESC",
                                 (artifact_id,))
        return [_version_from_row(r) for r in cur.fetchall()]

    def _guess_ext(self, artifact_id: str, content: str) -> str:
        art = self.get_artifact(artifact_id)
        if art:
            name = art.name.lower()
            if "." in name:
                return name.rsplit(".", 1)[-1]
        return "txt"

    def close(self) -> None:
        self._conn.close()
        logger.info("SQLiteStorage closed")
