import errno
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path

logger = logging.getLogger(__name__)

# P2-5/H12(trace): Optional OTel tracing. Module-level aliases referenced by the @tracing.traced decorator.
# The tracing module no-ops internally when OTel is missing/disabled, so importing at top level is safe.
from fusion_artifacts_engine import tracing as _tracing  # noqa: E402

_EXT_SAFE_RE = re.compile(r"[^A-Za-z0-9]")
_META_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _log_rmtree_error(func, path, exc_info):  # noqa: ANN001
    # M-4: rmtree failures must be visible; never swallow them silently
    logger.error("rmtree failed on %s during %s: %s", path, func.__name__, exc_info[1])


def _coerce_bool(name: str, value) -> int:
    # M-5: Coerce user-controlled filters values — accept bool/0/1/"true"/"false", raise ValueError for anything else
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value.lower() in ("true", "1"):
        return 1
    if isinstance(value, str) and value.lower() in ("false", "0"):
        return 0
    raise ValueError(f"Invalid boolean filter {name}={value!r}")


def _coerce_float(name: str, value) -> float:
    # M-5: since/until must be convertible to float; ISO strings etc. fail with an explicit error instead of a 500
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid numeric filter {name}={value!r}") from e

from fusion_artifacts_engine.models import (
    Artifact,
    ArtifactEvent,
    ArtifactFolder,
    ArtifactShare,
    ArtifactTag,
    ArtifactVersion,
)
from fusion_artifacts_engine.rpc.errors import (
    ConflictError,
    NotFoundError,
    ResourceLimitError,
)


def _translate_sqlite_error(e: sqlite3.Error):
    # P0-3/H8: Map SQLite write-path OperationalError by message —
    # "database is locked"/"database table is locked" -> ConflictError(-32002, retryable);
    # "disk I/O error"/"database disk image is malformed"/"disk full"→ResourceLimitError(-32003)；
    # Re-raise other OperationalError as-is (corrupted connections etc. handled upstream).
    if isinstance(e, sqlite3.OperationalError):
        msg = str(e).lower()
        if "locked" in msg:
            logger.warning("SQLite write locked, mapped to ConflictError: %s", e)
            return ConflictError(f"Database busy, retry: {e}")
        if "disk" in msg or "i/o" in msg or "full" in msg:
            logger.error("SQLite disk/I/O error, mapped to ResourceLimitError: %s", e)
            return ResourceLimitError(f"Storage I/O failure: {e}")
    return e


def _enforce_content_limit(content: str, limit: int) -> None:
    # P0-5/H9: Reject writes whose content bytes exceed max_content_bytes (0 = unlimited)
    if limit <= 0:
        return
    size = len(content.encode("utf-8"))
    if size > limit:
        logger.warning("Content rejected: %d bytes > max_content_bytes %d", size, limit)
        raise ResourceLimitError(
            f"Content too large: {size} bytes exceeds max_content_bytes {limit}"
        )


def _enforce_metadata_limit(metadata: dict | None, limit: int) -> None:
    # P0-5/H9: Reject writes whose metadata JSON bytes exceed max_metadata_bytes (0 = unlimited)
    if limit <= 0 or not metadata:
        return
    size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    if size > limit:
        logger.warning("Metadata rejected: %d bytes > max_metadata_bytes %d", size, limit)
        raise ResourceLimitError(
            f"Metadata too large: {size} bytes exceeds max_metadata_bytes {limit}"
        )


def _clamp_pagination(page, page_size, max_size: int = 500) -> tuple[int, int]:
    # P1-5/M5: Harden pagination inputs — page>=1, page_size<=max_size, non-int falls back to defaults via try/except.
    # Prevents page_size=10**9 full-table DoS, page<=0 negative offsets, and page="abc" TypeError.
    try:
        page = int(page) if page is not None else 1
    except (ValueError, TypeError):
        page = 1
    try:
        page_size = int(page_size) if page_size is not None else 20
    except (ValueError, TypeError):
        page_size = 20
    page = max(1, page)
    page_size = max(1, min(page_size, max_size))
    return page, page_size


def _decode_cursor(cursor: str) -> tuple[float | None, str]:
    # P2-7/F6: Parse a "<float>:<id>" cursor. Invalid input returns (None, "") to fall back to OFFSET.
    # The id segment may contain any character except ':', so split only at the first ':'.
    if not cursor or ":" not in cursor:
        return None, ""
    head, _, tail = cursor.partition(":")
    try:
        return float(head), tail
    except (ValueError, TypeError):
        return None, ""


def _encode_cursor(col_val: float, row_id: str) -> str:
    # P2-7/F6: Encode a "<float>:<id>" cursor. The RPC layer builds it from the result last row and returns it to the client for the next request.
    return f"{col_val}:{row_id}"


# F5: Chunked read/write for large content. write_text/read_text encode/decode the full string in one shot;
# a 10MB content doubles in memory (str + bytes). Chunking encodes/decodes in 1MB units +
# streams to disk, avoiding transient memory equal to the content size per I/O.
_CHUNK_IO_BYTES = 1024 * 1024


def _write_text_chunked(path, content: str) -> None:
    encoded = content.encode("utf-8")
    try:
        with open(path, "wb") as f:
            for off in range(0, len(encoded), _CHUNK_IO_BYTES):
                f.write(encoded[off:off + _CHUNK_IO_BYTES])
    except OSError as e:
        # P0-4: Map disk-full/quota-exceeded to ResourceLimitError (retryable/degradable).
        if e.errno in (errno.ENOSPC, errno.EDQUOT):
            logger.error("disk full writing content file %s: %s", path, e)
            try:
                os.unlink(path)
            except OSError:
                pass
            raise ResourceLimitError(f"disk full writing content: {e}") from e
        raise


def _read_text_chunked(path) -> str:
    chunks = []
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(_CHUNK_IO_BYTES)
                if not block:
                    break
                chunks.append(block)
    except OSError as e:
        logger.error("Failed reading content file %s: %s", path, e)
        raise
    return b"".join(chunks).decode("utf-8")


from fusion_artifacts_engine.storage.base import StorageDriver

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('code','markdown','html','react','svg','mermaid','data')),
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
    token_count INTEGER NOT NULL DEFAULT 0,
    section_index TEXT DEFAULT NULL,
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
    created_at REAL,
    expires_at TEXT,
    revoked INTEGER DEFAULT 0,
    access_count INTEGER DEFAULT 0,
    max_accesses INTEGER,
    last_access_at REAL,
    permission TEXT DEFAULT 'view' CHECK(permission IN ('view','remix')),
    UNIQUE(share_id),
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_shares_artifact ON artifact_shares(artifact_id);

CREATE TABLE IF NOT EXISTS artifact_folders (
    folder_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT,
    project_id TEXT,
    created_at REAL,
    FOREIGN KEY (parent_id) REFERENCES artifact_folders(folder_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_folders_project ON artifact_folders(project_id);

CREATE TABLE IF NOT EXISTS artifact_tags (
    tag_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT,
    scope TEXT,
    UNIQUE(name, scope)
);

CREATE TABLE IF NOT EXISTS artifact_tag_map (
    artifact_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    PRIMARY KEY(artifact_id, tag_id),
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES artifact_tags(tag_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tagmap_tag ON artifact_tag_map(tag_id);

CREATE TABLE IF NOT EXISTS artifact_events (
    event_id TEXT PRIMARY KEY,
    artifact_id TEXT,
    session_id TEXT,
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_events_artifact_time ON artifact_events(artifact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_session_time ON artifact_events(session_id, created_at);
"""

def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    keys = row.keys()
    meta_raw = row["metadata"] if "metadata" in keys else None
    metadata = None
    if meta_raw:
        try:
            metadata = json.loads(meta_raw)
        except (json.JSONDecodeError, TypeError):
            # M-3: Corrupt JSON must fail loudly; distinguish "no metadata" from "corrupt metadata"
            logger.error(
                "Corrupt metadata JSON for artifact %s: %s", row["id"], meta_raw[:200]
            )
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
        active_in_session=row["active_in_session"]
        if "active_in_session" in keys
        else None,
        source_module=row["source_module"] if "source_module" in keys else None,
        workspace_id=row["workspace_id"] if "workspace_id" in keys else None,
        workflow_run_id=row["workflow_run_id"] if "workflow_run_id" in keys else None,
        parent_artifact_id=row["parent_artifact_id"]
        if "parent_artifact_id" in keys
        else None,
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
        token_count=row["token_count"] if "token_count" in keys else 0,
        section_index=row["section_index"] if "section_index" in keys else None,
        change_log=row["change_log"] or "",
        source=row["source"] if "source" in keys else "manual",
        created_at=row["created_at"],
        snapshot_type=row["snapshot_type"] if "snapshot_type" in keys else "auto",
        snapshot_label=row["snapshot_label"] if "snapshot_label" in keys else None,
        author=row["author"] if "author" in keys else None,
        parent_version=row["parent_version"] if "parent_version" in keys else None,
    )


def _ts_to_float(raw) -> float | None:
    # E1: Read timestamps uniformly as float epoch. Compatible with legacy ISO strings and None.
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (ValueError, TypeError):
        logger.warning("Unparseable timestamp %r, fallback to None", raw)
        return None


def _share_from_row(row: sqlite3.Row) -> ArtifactShare:
    keys = row.keys()
    return ArtifactShare(
        share_id=row["share_id"],
        artifact_id=row["artifact_id"],
        created_by=row["created_by"],
        created_at=_ts_to_float(row["created_at"]),
        expires_at=row["expires_at"],
        revoked=bool(row["revoked"]),
        access_count=row["access_count"],
        max_accesses=row["max_accesses"] if "max_accesses" in keys else None,
        last_access_at=_ts_to_float(row["last_access_at"]),
        permission=row["permission"] if "permission" in keys else "view",
    )


def _folder_from_row(row: sqlite3.Row) -> ArtifactFolder:
    return ArtifactFolder(
        folder_id=row["folder_id"],
        name=row["name"],
        parent_id=row["parent_id"],
        project_id=row["project_id"],
        created_at=_ts_to_float(row["created_at"]),
    )


def _tag_from_row(row: sqlite3.Row) -> ArtifactTag:
    return ArtifactTag(
        tag_id=row["tag_id"],
        name=row["name"],
        color=row["color"],
        scope=row["scope"] if "scope" in row.keys() else None,
    )


def _event_from_row(row: sqlite3.Row) -> ArtifactEvent:
    payload_raw = row["payload"]
    payload = None
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
        except (json.JSONDecodeError, TypeError):
            # M-3: Log ERROR for corrupt payload JSON
            logger.error(
                "Corrupt event payload JSON for event %s: %s",
                row["event_id"],
                payload_raw[:200],
            )
            payload = None
    return ArtifactEvent(
        event_id=row["event_id"],
        artifact_id=row["artifact_id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        payload=payload,
        created_at=_ts_to_float(row["created_at"]),
    )


class SQLiteStorage(StorageDriver):
    def __init__(
        self, db_path: Path, content_dir: Path, small_content_limit: int = 10240,
        max_versions_per_artifact: int = 0, disk_space_warning_pct: int = 0,
        max_content_bytes: int = 0, max_metadata_bytes: int = 0,
        wal_checkpoint_interval: int = 300,
        metadata_indexed_keys: list[str] | None = None,
    ):
        self.db_path = db_path
        self.content_dir = content_dir
        self.small_content_limit = small_content_limit
        # R9: Per-artifact version cap, 0 = unlimited. Over-cap evicts the oldest non-snapshot version (prevents unbounded disk growth)
        self.max_versions_per_artifact = max_versions_per_artifact
        # Ops-4: Disk watermark warning percentage (0-100), 0 = disable pre-check. Pre-check before write; reject writes over threshold
        self.disk_space_warning_pct = max(0, int(disk_space_warning_pct))
        # P0-5/H9: Per-version content byte cap, 0 = unlimited. Reject over-limit writes to prevent OOM/disk exhaustion
        self.max_content_bytes = max(0, int(max_content_bytes))
        # P0-5/H9: Per-artifact metadata JSON byte cap, 0 = unlimited. Reject over-limit writes
        self.max_metadata_bytes = max(0, int(max_metadata_bytes))
        # P2-7/F6: High-frequency metadata filter field names. Only safe identifiers allowed (prevents DDL injection).
        # Builds a json_extract expression index per key so metadata_filter uses the index instead of a full scan.
        self.metadata_indexed_keys = [
            k for k in (metadata_indexed_keys or []) if _META_KEY_RE.fullmatch(k)
        ]
        db_path.parent.mkdir(parents=True, exist_ok=True)
        content_dir.mkdir(parents=True, exist_ok=True)
        # H1: Write-pool decision — under SQLite WAL, BEGIN IMMEDIATE serializes writes with a DB-level lock;
        # multiple write connections cannot write in parallel (physical limit). So write pool size=1
        # (single self._conn + _write_lock) is the correct architecture. The real H1 pain point was 'file I/O holding _write_lock and stretching lock hold time',
        # fixed by H2 (file writes moved outside the transaction/lock). Read-side concurrency is handled by the E7 read-only pool.
        # Introduce a real write pool later when switching to Postgres/object storage (H8).
        self._write_lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # E7: Read-only connection pool — reuse read-only connections to avoid repeated connect/close on hot reads.
        # WAL allows concurrent reads; pool size 4 balances concurrency and connection overhead.
        self._read_pool_size = 4
        self._read_pool: queue.Queue = queue.Queue(maxsize=self._read_pool_size)
        for _ in range(self._read_pool_size):
            rc = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
            rc.row_factory = sqlite3.Row
            self._read_pool.put(rc)
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        # R5: Version-gated migrations — create schema_meta first, then run unrecorded migrations one by one from the registry.
        # Applied migrations are recorded in schema_meta.applied_migrations and skipped, avoiding a full re-check on every startup.
        # Migration methods must still be idempotent (first upgrade of existing DBs without applied_migrations records).
        self._migrate_schema_meta()
        self._run_migrations_gated()
        # P2-7/F6: Create the configured metadata expression indexes. Idempotent (IF NOT EXISTS);
        # after a config key change the old index is kept (harmless); new keys are created on the fly at runtime.
        self._ensure_metadata_indexes()
        # P1-4: Background periodic WAL checkpoint thread. PASSIVE mode does not block reads/writes and controls -wal file growth,
        # making scripts/backup.sh's sqlite3 .backup reliable (needs a checkpointed WAL for a complete snapshot).
        # interval=0 disables it (relies on SQLite's default 1000-page auto-checkpoint). Daemon thread; exits via stop event.
        self._wal_checkpoint_interval = max(0, int(wal_checkpoint_interval))
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread: threading.Thread | None = None
        if self._wal_checkpoint_interval > 0:
            self._checkpoint_thread = threading.Thread(
                target=self._checkpoint_loop,
                name="wal-checkpoint",
                daemon=True,
            )
            self._checkpoint_thread.start()
        logger.info(
            "SQLiteStorage initialized: db=%s content_dir=%s wal_checkpoint=%ss",
            db_path, content_dir, self._wal_checkpoint_interval or "disabled",
        )

    @contextmanager
    def _read_conn(self):
        # E7: Borrow a read-only connection from the pool and return it after use. WAL allows concurrent reads; reuse saves connect/close overhead.
        # If the pool is empty, wait with a timeout (read concurrency bounded by pool size, preventing connection leaks). On close or pool starvation we must
        # fail fast instead of blocking forever — background long-context tasks may only be run by the event loop
        # after close; an unbounded get() would hang the whole process (exposed by #100 acceptance tests).
        if self._conn is None:
            raise RuntimeError("storage closed: read pool unavailable")
        try:
            conn = self._read_pool.get(timeout=10)
        except queue.Empty:
            raise RuntimeError("read pool starved for 10s (possible leak)")
        try:
            yield conn
        finally:
            self._read_pool.put(conn)

    def _checkpoint_loop(self) -> None:
        # P1-4: Background periodic PASSIVE checkpoint. PASSIVE does not block reads/writes; it merges committed WAL frames back into the main DB.
        # Controls unbounded -wal growth (default auto-checkpoint only at 1000 pages, which piles up under long transactions).
        while not self._checkpoint_stop.wait(self._wal_checkpoint_interval):
            try:
                self._run_checkpoint("PASSIVE")
            except Exception as e:
                logger.warning("WAL PASSIVE checkpoint failed: %s", e)

    def _run_checkpoint(self, mode: str = "PASSIVE") -> None:
        # P1-4: Run one wal_checkpoint. PASSIVE = non-blocking merge; TRUNCATE = merge then truncate the -wal file.
        # TRUNCATE is used on close to guarantee an empty -wal after shutdown (backup/migration only needs to copy meta.db).
        with self._write_lock:
            cur = self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
            row = cur.fetchone()
            # (busy, log, checkpointed): busy=1 means reads/writes still in flight (normal for PASSIVE), not an error
            logger.debug("WAL checkpoint(%s): busy=%s log=%s ckpt=%s", mode, row[0], row[1], row[2])

    def _artifact_content_dir(self, artifact_id: str) -> Path:
        d = self.content_dir / artifact_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_content_file(
        self, artifact_id: str, version_num: int, content: str, ext: str = "txt"
    ) -> str:
        d = self._artifact_content_dir(artifact_id)
        path = d / f"v{version_num}.{ext}"
        # C-10/C-6 defense in depth: reject any path escaping content_dir
        if not path.resolve().is_relative_to(d.resolve()):
            logger.error(
                "Refusing content write outside content_dir: %s (ext=%s)", path, ext
            )
            raise ValueError(f"Unsafe content path: {path}")
        _write_text_chunked(path, content)
        logger.debug("Wrote content file: %s", path)
        return str(path)

    def _write_content_file_tmp(
        self, artifact_id: str, content: str, ext: str = "txt"
    ) -> tuple[str, str, str]:
        # H2: Write the file to a temp path (outside the transaction), rename to the final path after the transaction commits.
        # On rollback delete the temp file — no orphans. Returns (tmp_path, final_path_template, ext).
        # The final path contains a {version_num} placeholder, filled with the real version number before rename after commit.
        d = self._artifact_content_dir(artifact_id)
        tmp_name = f".tmp_v{uuid.uuid4().hex}.{ext}"
        tmp_path = d / tmp_name
        if not tmp_path.resolve().is_relative_to(d.resolve()):
            logger.error("Refusing tmp content write outside content_dir: %s", tmp_path)
            raise ValueError(f"Unsafe content path: {tmp_path}")
        _write_text_chunked(tmp_path, content)
        logger.debug("Wrote tmp content file: %s", tmp_path)
        return str(tmp_path), f"v{{version_num}}.{ext}", ext


    def _read_content_file(self, content_path: str) -> str:
        p = Path(content_path)
        if p.exists():
            return _read_text_chunked(p)
        logger.error("Content file missing: %s", content_path)
        raise FileNotFoundError(f"Content file missing: {content_path}")

    # ── artifact CRUD ──────────────────────────────────────────

    def check_disk_space(self) -> tuple[int, int, float] | None:
        # Ops-4: Pre-write disk check. Returns (used_pct, free_bytes, total) or None (disabled).
        # 0 = monitoring disabled. The caller decides whether to reject the write when over threshold.
        if self.disk_space_warning_pct <= 0:
            return None
        try:
            usage = shutil.disk_usage(str(self.db_path.parent))
        except OSError as e:
            logger.warning("disk_usage check failed: %s", e)
            return None
        used_pct = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        return (used_pct, usage.free, usage.total)

    def ensure_disk_available(self) -> None:
        # Ops-4: Pre-write check — reject the write and raise ResourceLimitError when disk usage exceeds the threshold.
        # Threshold 0 disables it. Callers invoke this at the write-path entry.
        if self.disk_space_warning_pct <= 0:
            return
        info = self.check_disk_space()
        if info is None:
            return
        used_pct, _free, _total = info
        if used_pct >= self.disk_space_warning_pct:
            logger.error(
                "disk space warning: %.1f%% used >= %d%% threshold, rejecting write",
                used_pct, self.disk_space_warning_pct,
            )
            raise ResourceLimitError(
                f"disk space at {used_pct}% (threshold {self.disk_space_warning_pct}%)"
            )

    def save_artifact(self, artifact: Artifact) -> None:
        # Ops-4: Pre-write disk check; reject writes over threshold
        self.ensure_disk_available()
        # P0-5/H9: metadata byte-cap validation
        _enforce_metadata_limit(artifact.metadata, self.max_metadata_bytes)
        with self._write_lock:
            meta_json = json.dumps(artifact.metadata) if artifact.metadata else None
            try:
                self._conn.execute(
                    """INSERT INTO artifacts
                       (id, session_id, name, type, kind, project_id, metadata,
                        current_version, summary, created_at, updated_at, is_deleted,
                        owner_user_id, ownership_type, is_starred, is_pinned,
                        pinned_chat_id, share_id, in_project_kb, folder_id,
                        deleted_at, content_hash, active_in_session,
                        source_module, workspace_id, workflow_run_id, parent_artifact_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                         owner_user_id=excluded.owner_user_id,
                         ownership_type=excluded.ownership_type,
                         is_starred=excluded.is_starred,
                         is_pinned=excluded.is_pinned,
                         pinned_chat_id=excluded.pinned_chat_id,
                         share_id=excluded.share_id,
                         in_project_kb=excluded.in_project_kb,
                         folder_id=excluded.folder_id,
                         content_hash=excluded.content_hash,
                         active_in_session=excluded.active_in_session,
                         source_module=excluded.source_module,
                         workspace_id=excluded.workspace_id,
                         workflow_run_id=excluded.workflow_run_id,
                         parent_artifact_id=excluded.parent_artifact_id""",
                    (
                        artifact.id,
                        artifact.session_id,
                        artifact.name,
                        artifact.type,
                        artifact.kind,
                        artifact.project_id,
                        meta_json,
                        artifact.current_version,
                        artifact.summary,
                        artifact.created_at,
                        artifact.updated_at,
                        int(artifact.is_deleted),
                        artifact.owner_user_id,
                        artifact.ownership_type,
                        int(artifact.is_starred),
                        int(artifact.is_pinned),
                        artifact.pinned_chat_id,
                        artifact.share_id,
                        int(artifact.in_project_kb),
                        artifact.folder_id,
                        artifact.deleted_at,
                        artifact.content_hash,
                        artifact.active_in_session,
                        artifact.source_module,
                        artifact.workspace_id,
                        artifact.workflow_run_id,
                        artifact.parent_artifact_id,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error as e:
                raise _translate_sqlite_error(e) from e
        logger.info(
            "Saved artifact: %s name=%s kind=%s",
            artifact.id,
            artifact.name,
            artifact.kind,
        )

    @_tracing.traced("db.storage.duration", db_operation="save_artifact_and_version")
    def save_artifact_and_version(
        self, artifact: Artifact, version: ArtifactVersion
    ) -> None:
        # Ops-4: Pre-write disk check; reject writes over threshold
        self.ensure_disk_available()
        # P0-5/H9: metadata + content byte-cap validation
        _enforce_metadata_limit(artifact.metadata, self.max_metadata_bytes)
        _enforce_content_limit(version.content, self.max_content_bytes)
        # P1-8/H10: Two-phase content file write — write to a temp path (outside the transaction) first, rename to final after commit.
        # Rollback deletes the temp file, no orphans; file I/O stays outside BEGIN IMMEDIATE, shortening _write_lock hold time.
        with self._write_lock:
            meta_json = json.dumps(artifact.metadata) if artifact.metadata else None
            tmp_path = None
            final_template = None
            content = version.content
            if len(content.encode("utf-8")) > self.small_content_limit:
                ext = self._guess_ext(version.artifact_id, version.content)
                tmp_path, final_template, _ext = self._write_content_file_tmp(
                    version.artifact_id, content, ext
                )
                content = ""
            try:
                self._conn.execute(
                    """INSERT INTO artifacts
                       (id, session_id, name, type, kind, project_id, metadata,
                        current_version, summary, created_at, updated_at, is_deleted,
                        owner_user_id, ownership_type, is_starred, is_pinned,
                        pinned_chat_id, share_id, in_project_kb, folder_id,
                        deleted_at, content_hash, active_in_session,
                        source_module, workspace_id, workflow_run_id, parent_artifact_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                         owner_user_id=excluded.owner_user_id,
                         ownership_type=excluded.ownership_type,
                         is_starred=excluded.is_starred,
                         is_pinned=excluded.is_pinned,
                         pinned_chat_id=excluded.pinned_chat_id,
                         share_id=excluded.share_id,
                         in_project_kb=excluded.in_project_kb,
                         folder_id=excluded.folder_id,
                         content_hash=excluded.content_hash,
                         active_in_session=excluded.active_in_session,
                         source_module=excluded.source_module,
                         workspace_id=excluded.workspace_id,
                         workflow_run_id=excluded.workflow_run_id,
                         parent_artifact_id=excluded.parent_artifact_id""",
                    (
                        artifact.id,
                        artifact.session_id,
                        artifact.name,
                        artifact.type,
                        artifact.kind,
                        artifact.project_id,
                        meta_json,
                        artifact.current_version,
                        artifact.summary,
                        artifact.created_at,
                        artifact.updated_at,
                        int(artifact.is_deleted),
                        artifact.owner_user_id,
                        artifact.ownership_type,
                        int(artifact.is_starred),
                        int(artifact.is_pinned),
                        artifact.pinned_chat_id,
                        artifact.share_id,
                        int(artifact.in_project_kb),
                        artifact.folder_id,
                        artifact.deleted_at,
                        artifact.content_hash,
                        artifact.active_in_session,
                        artifact.source_module,
                        artifact.workspace_id,
                        artifact.workflow_run_id,
                        artifact.parent_artifact_id,
                    ),
                )
                content_path = None
                if tmp_path is not None:
                    content_path = str(
                        self.content_dir / version.artifact_id
                        / final_template.format(version_num=version.version_num)
                    )
                self._conn.execute(
                    """INSERT INTO artifact_versions
                       (artifact_id, version_num, content, content_path, size_bytes,
                        token_count, section_index,
                        change_log, source, created_at, snapshot_type, snapshot_label,
                        author, parent_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        version.artifact_id,
                        version.version_num,
                        content,
                        content_path,
                        version.size_bytes,
                        version.token_count,
                        version.section_index,
                        version.change_log,
                        version.source,
                        version.created_at,
                        version.snapshot_type,
                        version.snapshot_label,
                        version.author,
                        version.parent_version,
                    ),
                )
                self._conn.commit()
                # P1-8/H10: After successful commit, rename the temp file to the final path
                if tmp_path is not None and content_path is not None:
                    try:
                        Path(tmp_path).rename(content_path)
                        tmp_path = None
                    except OSError as e:
                        logger.error(
                            "save_artifact_and_version rename failed %s -> %s: %s; "
                            "orphan/tmp left for GC", tmp_path, content_path, e,
                        )
                        tmp_path = None
            except sqlite3.Error as e:
                # P1-8/H10: Transaction failed — clean up the temp file, no orphans
                if tmp_path is not None:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                raise _translate_sqlite_error(e) from e
        logger.info(
            "Saved artifact+version: %s v%d size=%d tokens=%d",
            artifact.id,
            version.version_num,
            version.size_bytes,
            version.token_count,
        )

    def get_artifact(
        self, artifact_id: str, project_id: str | None = None
    ) -> Artifact | None:
        # P2-5/H12(trace): db.storage.duration marks the read path. tracing.span is a no-op when tracing is disabled.
        from fusion_artifacts_engine import tracing

        with tracing.span(
            "db.storage.duration", db_operation="get_artifact",
            db_artifact_id=artifact_id,
        ):
            with self._read_conn() as conn:
                if project_id is not None:
                    cur = conn.execute(
                        "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
                        (artifact_id, project_id),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
                    )
                row = cur.fetchone()
                if row is None:
                    return None
                return _artifact_from_row(row)

    def list_artifacts(
        self,
        session_id: str,
        include_deleted: bool = False,
        project_id: str | None = None,
        metadata_filter: dict | None = None,
    ) -> list[Artifact]:
        # P2-5/H12(trace): db.storage.duration marks the read path.
        from fusion_artifacts_engine import tracing

        with tracing.span(
            "db.storage.duration", db_operation="list_artifacts",
            db_session_id=session_id,
        ):
            conditions = ["session_id = ?"]
            params: list = [session_id]
            if not include_deleted:
                conditions.append("is_deleted = 0")
            if project_id is not None:
                conditions.append("project_id = ?")
                params.append(project_id)
            if metadata_filter:
                for key, value in metadata_filter.items():
                    # P-4: Validate that the key contains only safe identifier characters, rejecting JSONPath injection (. [ " whitespace)
                    if not _META_KEY_RE.fullmatch(key):
                        logger.warning("Reject metadata filter key (unsafe): %s", key)
                        continue
                    json_path = f"$.{key}"
                    conditions.append("json_extract(metadata, ?) = ?")
                    # L-15: Bind native types, bool as int, to avoid str() turning numeric filters into never-matching predicates
                    bind_val = int(value) if isinstance(value, bool) else value
                    params.extend([json_path, bind_val])
            where = " AND ".join(conditions)
            with self._read_conn() as conn:
                cur = conn.execute(
                    f"SELECT * FROM artifacts WHERE {where} ORDER BY updated_at DESC",
                    params,
                )
                return [_artifact_from_row(r) for r in cur.fetchall()]

    def list_all_artifacts(
        self,
        filters: dict | None = None,
        sort: str = "updated_at",
        page: int = 1,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Artifact], int]:
        # P1-5/M5: Harden pagination inputs
        page, page_size = _clamp_pagination(page, page_size)
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
                params.append(_coerce_bool("is_starred", is_starred))
            is_pinned = filters.get("is_pinned")
            if is_pinned is not None:
                conditions.append("is_pinned = ?")
                params.append(_coerce_bool("is_pinned", is_pinned))
            folder_id = filters.get("folder_id")
            if folder_id:
                conditions.append("folder_id = ?")
                params.append(folder_id)
            tag_id = filters.get("tag_id")
            if tag_id:
                conditions.append(
                    "id IN (SELECT artifact_id FROM artifact_tag_map WHERE tag_id = ?)"
                )
                params.append(tag_id)
            in_project_kb = filters.get("in_project_kb")
            if in_project_kb is not None:
                conditions.append("in_project_kb = ?")
                params.append(_coerce_bool("in_project_kb", in_project_kb))
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
                params.append(_coerce_float("since", since))
            until = filters.get("until")
            if until is not None:
                conditions.append("created_at <= ?")
                params.append(_coerce_float("until", until))
        where = " AND ".join(conditions)
        # P2-7/F6: Keyset pagination — single-column DESC sort on updated_at/created_at supports cursors;
        # WHERE (sort_col, id) < (cursor_val, cursor_id) replaces OFFSET deep pagination (avoids scan + dropped rows).
        # Cursor format "<float>:<id>"; falls back to OFFSET when invalid or the sort is unsupported (backward compatible).
        keyset_cols = {"updated_at": "updated_at", "created_at": "created_at"}
        use_keyset = False
        cursor_params: list = []
        cursor_cond = ""
        if cursor and sort in keyset_cols:
            col_val, row_id = _decode_cursor(cursor)
            if col_val is not None and row_id:
                col = keyset_cols[sort]
                cursor_cond = f" AND ({col}, id) < (?, ?)"
                cursor_params = [col_val, row_id]
                use_keyset = True
        with self._read_conn() as conn:
            count_cur = conn.execute(
                f"SELECT COUNT(*) FROM artifacts WHERE {where}", params
            )
            total = count_cur.fetchone()[0]
            sort_map = {
                "updated_at": "updated_at DESC",
                "created_at": "created_at DESC",
                "name": "name ASC",
                "starred": "is_starred DESC, updated_at DESC",
            }
            order = sort_map.get(sort, "updated_at DESC")
            if use_keyset:
                # keyset: add id DESC to the sort as tiebreaker (guarantees strict cursor ordering, no gaps or dupes).
                keyset_order = f"{keyset_cols[sort]} DESC, id DESC"
                cur = conn.execute(
                    f"SELECT * FROM artifacts WHERE {where}{cursor_cond} "
                    f"ORDER BY {keyset_order} LIMIT ?",
                    params + cursor_params + [page_size],
                )
            else:
                offset = (page - 1) * page_size
                cur = conn.execute(
                    f"SELECT * FROM artifacts WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                    params + [page_size, offset],
                )
            artifacts = [_artifact_from_row(r) for r in cur.fetchall()]
        logger.info(
            "list_all_artifacts: %d/%d page=%d sort=%s",
            len(artifacts),
            total,
            page,
            sort,
        )
        return artifacts, total

    def sum_token_counts(
        self,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[int, list[dict]]:
        # P-2: One SQL JOIN artifacts -> artifact_versions(current_version) to aggregate tokens,
        # replacing per-artifact get_version_content N+1 round trips and loading full content into memory.
        conditions = ["a.is_deleted = 0"]
        params: list = []
        if session_id is not None:
            conditions.append("a.session_id = ?")
            params.append(session_id)
        if project_id is not None:
            conditions.append("a.project_id = ?")
            params.append(project_id)
        where = " AND ".join(conditions)
        sql = (
            "SELECT a.id, a.name, COALESCE(v.token_count, 0) AS tc "
            "FROM artifacts a "
            "LEFT JOIN artifact_versions v "
            "ON v.artifact_id = a.id AND v.version_num = a.current_version "
            f"WHERE {where}"
        )
        rows: list[dict] = []
        total = 0
        with self._read_conn() as conn:
            cur = conn.execute(sql, params)
            for r in cur.fetchall():
                tc = int(r["tc"]) if r["tc"] is not None else 0
                total += tc
                rows.append({"artifact_id": r["id"], "name": r["name"], "tokens": tc})
        logger.info(
            "sum_token_counts: session=%s project=%s count=%d total=%d",
            session_id,
            project_id,
            len(rows),
            total,
        )
        return total, rows

    def delete_artifact(
        self,
        artifact_id: str,
        soft_delete: bool = True,
        project_id: str | None = None,
    ) -> bool:
        # P2-5/H12(trace): db.storage.duration marks the write path.
        from fusion_artifacts_engine import tracing

        with tracing.span(
            "db.storage.duration", db_operation="delete_artifact",
            db_artifact_id=artifact_id, db_soft_delete=soft_delete,
        ):
            if project_id is not None:
                art = self.get_artifact(artifact_id, project_id=project_id)
                if art is None:
                    logger.warning(
                        "Delete denied: artifact %s not found in project %s",
                        artifact_id,
                        project_id,
                    )
                    return False
            if soft_delete:
                from datetime import datetime

                now_iso = datetime.now(UTC).isoformat()
                with self._write_lock:
                    cur = self._conn.execute(
                        "UPDATE artifacts SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE id = ?",
                        (now_iso, time.time(), artifact_id),
                    )
                    self._conn.commit()
                ok = cur.rowcount > 0
            else:
                with self._write_lock:
                    # Hard delete clears FK-less related tables first to avoid orphan rows (P2-5).
                    self._conn.execute(
                        "DELETE FROM artifact_tag_map WHERE artifact_id = ?", (artifact_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM artifact_events WHERE artifact_id = ?", (artifact_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM artifact_shares WHERE artifact_id = ?", (artifact_id,)
                    )
                    cur = self._conn.execute(
                        "DELETE FROM artifacts WHERE id = ?", (artifact_id,)
                    )
                    self._conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    content_dir = self.content_dir / artifact_id
                    if content_dir.exists():
                        shutil.rmtree(content_dir, onerror=_log_rmtree_error)
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

    def update_artifact_summary(
        self, artifact_id: str, summary: str, metadata_patch: dict | None = None
    ) -> bool:
        # v0.7.1 #91: Rolling summary write-back. summary goes into the artifact.summary column (ref_text is carried
        # automatically by the existing pipeline); metadata_patch is merged into the metadata JSON (atomic in a single transaction).
        with self._write_lock:
            row = self._conn.execute(
                "SELECT metadata FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                logger.warning("update_artifact_summary: %s not found", artifact_id)
                return False
            current = {}
            if row[0]:
                try:
                    current = json.loads(row[0])
                except (ValueError, TypeError):
                    current = {}
            if metadata_patch:
                current.update(metadata_patch)
            metadata_json = json.dumps(current, ensure_ascii=False) if current else None
            _enforce_metadata_limit(current, self.max_metadata_bytes)
            cur = self._conn.execute(
                "UPDATE artifacts SET summary = ?, metadata = ?, updated_at = ? WHERE id = ?",
                (summary or None, metadata_json, time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Updated artifact summary %s ok=%s", artifact_id, ok)
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

    def pin_artifact(self, artifact_id: str, chat_id: str | None, pinned: bool) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET is_pinned = ?, pinned_chat_id = ?, updated_at = ? WHERE id = ?",
                (int(pinned), chat_id, time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info(
            "Pin artifact %s pinned=%s chat=%s ok=%s", artifact_id, pinned, chat_id, ok
        )
        return ok

    def duplicate_artifact(
        self,
        artifact_id: str,
        new_id: str,
        new_name: str | None = None,
        parent_artifact_id: str | None = None,
    ) -> Artifact | None:
        art = self.get_artifact(artifact_id)
        if art is None:
            return None
        # L-14: Refuse to overwrite an existing artifact that happens to share the ID
        if self.get_artifact(new_id) is not None:
            logger.error("Duplicate target id %s already exists", new_id)
            raise ValueError(f"Artifact id already exists: {new_id}")
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
            parent_artifact_id=parent_artifact_id,
        )
        ver = self.get_version(artifact_id, art.current_version)
        if ver is None:
            return None
        new_ver = ArtifactVersion(
            artifact_id=new_id,
            version_num=1,
            content=ver.content,
            size_bytes=ver.size_bytes,
            token_count=ver.token_count,
            section_index=ver.section_index,
            change_log=f"Duplicated from {artifact_id}",
            source=ver.source,
            created_at=now,
            snapshot_type=ver.snapshot_type,
            author=ver.author,
            parent_version=ver.parent_version,
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

    def set_active_session(self, artifact_id: str, session_id: str | None) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifacts SET active_in_session = ?, updated_at = ? WHERE id = ?",
                (session_id, time.time(), artifact_id),
            )
            self._conn.commit()

    def set_artifact_share_id(self, artifact_id: str, share_id: str) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifacts SET share_id = ?, updated_at = ? WHERE id = ?",
                (share_id, time.time(), artifact_id),
            )
            self._conn.commit()
        logger.debug("Linked share %s to artifact %s", share_id, artifact_id)

    # ── recycle bin ────────────────────────────────────────────

    def list_recycle(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[Artifact], int]:
        # P1-5/M5: Harden pagination inputs
        page, page_size = _clamp_pagination(page, page_size)
        conditions = ["is_deleted = 1"]
        params: list = []
        where = " AND ".join(conditions)
        with self._read_conn() as conn:
            count_cur = conn.execute(
                f"SELECT COUNT(*) FROM artifacts WHERE {where}", params
            )
            total = count_cur.fetchone()[0]
            offset = (page - 1) * page_size
            cur = conn.execute(
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
        from datetime import datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT id FROM artifacts WHERE is_deleted = 1 AND deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            )
            expired_ids = [row["id"] for row in cur.fetchall()]
            purged_dirs = []
            count = 0
            for aid in expired_ids:
                self._conn.execute(
                    "DELETE FROM artifact_tag_map WHERE artifact_id = ?", (aid,)
                )
                self._conn.execute(
                    "DELETE FROM artifact_events WHERE artifact_id = ?", (aid,)
                )
                # L-9: Missing artifact_shares deletion left orphan rows — added
                self._conn.execute(
                    "DELETE FROM artifact_shares WHERE artifact_id = ?", (aid,)
                )
                self._conn.execute(
                    "DELETE FROM artifact_versions WHERE artifact_id = ?", (aid,)
                )
                self._conn.execute("DELETE FROM artifacts WHERE id = ?", (aid,))
                content_dir = self.content_dir / aid
                if content_dir.exists():
                    purged_dirs.append(content_dir)
                count += 1
            self._conn.commit()
            # Commit before deleting from disk: on commit failure no orphan rows point at missing files (P2-6)
            # E5: No ignore_errors=True silent swallowing; onerror logs so disk/permission problems are visible
            for content_dir in purged_dirs:
                def _on_rm_err(func, fpath, exc_info):
                    logger.warning("Failed to purge content %s: %s", fpath, exc_info[1])
                shutil.rmtree(content_dir, onerror=_on_rm_err)
        logger.info(
            "Purged %d expired artifacts (retention=%d days)", count, retention_days
        )
        return count

    # ── versions ───────────────────────────────────────────────

    @_tracing.traced("db.storage.duration", db_operation="save_version")
    def save_version(self, version: ArtifactVersion) -> None:
        # Ops-4: Pre-write disk check; reject writes over threshold
        self.ensure_disk_available()
        # C-11/C-12/H2: File writes go to a temp path outside the transaction; rename to final after commit.
        # On retry with a new version_num only the final path is recomputed (tmp file unchanged), avoiding overwriting historical versions and cross-numbering;
        # stale version numbers never hit disk (single tmp file), eliminating orphans entirely.
        with self._write_lock:
            max_retries = 3
            tmp_path = None
            final_template = None
            content = version.content
            if len(content.encode("utf-8")) > self.small_content_limit:
                ext = self._guess_ext(version.artifact_id, version.content)
                tmp_path, final_template, _ext = self._write_content_file_tmp(
                    version.artifact_id, content, ext
                )
                content = ""
            for attempt in range(max_retries):
                content_path = None
                if tmp_path is not None:
                    content_path = str(
                        self.content_dir / version.artifact_id
                        / final_template.format(version_num=version.version_num)
                    )
                    if not Path(content_path).resolve().is_relative_to(
                        (self.content_dir / version.artifact_id).resolve()
                    ):
                        if attempt >= max_retries - 1:
                            raise ValueError(f"Unsafe content path: {content_path}")
                        version.version_num = self.next_version_num(version.artifact_id)
                        continue
                try:
                    self._conn.execute(
                        """INSERT INTO artifact_versions
                           (artifact_id, version_num, content, content_path, size_bytes,
                            token_count, section_index,
                            change_log, source, created_at, snapshot_type, snapshot_label,
                            author, parent_version)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            version.artifact_id,
                            version.version_num,
                            content,
                            content_path,
                            version.size_bytes,
                            version.token_count,
                            version.section_index,
                            version.change_log,
                            version.source,
                            version.created_at,
                            version.snapshot_type,
                            version.snapshot_label,
                            version.author,
                            version.parent_version,
                        ),
                    )
                    self._conn.commit()
                    # H2: After successful commit, rename the temp file to the final path
                    if tmp_path is not None and content_path is not None:
                        try:
                            Path(tmp_path).rename(content_path)
                            tmp_path = None
                        except OSError as e:
                            logger.error(
                                "save_version rename failed %s -> %s: %s; "
                                "orphan/tmp left for GC", tmp_path, content_path, e,
                            )
                            tmp_path = None
                    # R9: After the write, evict oldest non-snapshot versions per the version cap
                    self._enforce_version_limit(version.artifact_id)
                    logger.info(
                        "Saved version: %s v%d size=%d tokens=%d",
                        version.artifact_id,
                        version.version_num,
                        version.size_bytes,
                        version.token_count,
                    )
                    return
                except sqlite3.IntegrityError:
                    if attempt >= max_retries - 1:
                        # Retries exhausted — clean up the tmp file, no orphans
                        if tmp_path is not None:
                            try:
                                Path(tmp_path).unlink(missing_ok=True)
                            except OSError:
                                pass
                        raise
                    version.version_num = self.next_version_num(version.artifact_id)
                    logger.warning(
                        "Version collision for %s, retrying with v%d",
                        version.artifact_id,
                        version.version_num,
                    )
            # Loop ended normally (theoretically unreachable) — clean up tmp
            if tmp_path is not None:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def next_version_num(self, artifact_id: str) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(version_num), 0) + 1 FROM artifact_versions WHERE artifact_id = ?",
                (artifact_id,),
            )
            row = cur.fetchone()
            return row[0]

    def _enforce_version_limit(self, artifact_id: str) -> None:
        # R9: When over max_versions_per_artifact, evict the oldest non-snapshot versions (including their disk files).
        # Must be called under _write_lock. Snapshots (snapshot_type='named') do not count toward the cap and are never evicted.
        # The current version (artifacts.current_version) is never evicted, preventing deletion of a live version.
        if self.max_versions_per_artifact <= 0:
            return
        cur = self._conn.execute(
            "SELECT current_version FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        current_version = cur[0] if cur else None
        rows = self._conn.execute(
            "SELECT version_num, content_path FROM artifact_versions "
            "WHERE artifact_id = ? AND (snapshot_type IS NULL OR snapshot_type != 'named') "
            "ORDER BY version_num ASC",
            (artifact_id,),
        ).fetchall()
        # Evictable count = non-snapshot version count - max (only evict when over max)
        excess = len(rows) - self.max_versions_per_artifact
        if excess <= 0:
            return
        purged = 0
        for row in rows:
            if excess <= 0:
                break
            vnum = row["version_num"]
            if vnum == current_version:
                continue
            content_path = row["content_path"]
            self._conn.execute(
                "DELETE FROM artifact_versions WHERE artifact_id = ? AND version_num = ?",
                (artifact_id, vnum),
            )
            if content_path:
                try:
                    Path(content_path).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to purge old version file %s: %s", content_path, e)
            excess -= 1
            purged += 1
        if purged:
            # E11/R9: DELETE implicitly opens a transaction; commit explicitly, otherwise the next BEGIN IMMEDIATE fails with
            # "cannot start a transaction within a transaction"
            self._conn.commit()
            logger.info("Version limit enforced: %s evicted %d old version(s)", artifact_id, purged)

    def gc_orphan_files(self) -> int:
        # H2: Orphan file GC — scan content_dir, delete files no DB row points at + leftover .tmp_ files.
        # Set-difference between the content_path set referenced by DB rows and the on-disk file set; the excess are orphans.
        # Returns the number of cleaned files. Callers should trigger this off-peak or periodically.
        with self._write_lock:
            valid_paths: set[str] = set()
            for r in self._conn.execute(
                "SELECT content_path FROM artifact_versions "
                "WHERE content_path IS NOT NULL AND content_path != ''"
            ).fetchall():
                valid_paths.add(r["content_path"])
        removed = 0
        if not self.content_dir.exists():
            return 0
        for art_dir in self.content_dir.iterdir():
            if not art_dir.is_dir():
                continue
            for f in art_dir.iterdir():
                if not f.is_file():
                    continue
                # .tmp_ prefix = two-phase write leftovers (failed rename / process crash); delete directly
                if f.name.startswith(".tmp_"):
                    try:
                        f.unlink()
                        removed += 1
                    except OSError as e:
                        logger.warning("GC failed to remove tmp %s: %s", f, e)
                    continue
                if str(f) not in valid_paths:
                    try:
                        f.unlink()
                        removed += 1
                        logger.info("GC removed orphan content file: %s", f)
                    except OSError as e:
                        logger.warning("GC failed to remove orphan %s: %s", f, e)
        if removed:
            logger.info("GC orphan files: removed %d", removed)
        return removed

    @_tracing.traced("db.storage.duration", db_operation="create_version_atomic")
    def create_version_atomic(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        content_hash: str,
        summary: str | None,
        expected_content_hash: str | None = None,
    ) -> int:
        # Ops-4: Pre-write disk check; reject writes over threshold
        self.ensure_disk_available()
        # P0-5/H9: content byte-cap validation
        _enforce_content_limit(version.content, self.max_content_bytes)
        # C-8: Optimistic-lock validation + write in a single transaction; BEGIN IMMEDIATE serializes concurrent writes,
        # current_version/content_hash re-read and validated inside the transaction, version_num allocated,
        # the version row written and artifact.current_version/content_hash updated, committed atomically.
        # H2: File write moved out of the transaction — write a temp file first (outside), rename to the final path after commit.
        # On rollback delete the temp file, no orphans. Returns the actually allocated version_num.
        with self._write_lock:
            # H2 phase 1: file write to a temp path outside the transaction (version_num unknown, write tmp first)
            tmp_path = None
            final_template = None
            content = version.content
            if len(content.encode("utf-8")) > self.small_content_limit:
                ext = self._guess_ext(version.artifact_id, version.content)
                tmp_path, final_template, _ext = self._write_content_file_tmp(
                    version.artifact_id, content, ext
                )
                content = ""
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT current_version, content_hash, summary FROM artifacts WHERE id = ?",
                    (artifact.id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Artifact not found: {artifact.id}")
                db_content_hash = row["content_hash"]
                db_summary = row["summary"]
                if expected_content_hash is not None and db_content_hash != expected_content_hash:
                    # P0-2: Map optimistic-lock conflicts to ConflictError(-32002, retryable), not -32602.
                    raise ConflictError(
                        f"Optimistic lock failed: expected hash {expected_content_hash},"
                        f" got {db_content_hash}"
                    )
                new_version_num = (
                    self._conn.execute(
                        "SELECT COALESCE(MAX(version_num), 0) + 1 FROM artifact_versions WHERE artifact_id = ?",
                        (artifact.id,),
                    ).fetchone()[0]
                )
                version.version_num = new_version_num
                content_path = None
                if tmp_path is not None:
                    # H2 phase 2: only record the final path inside the transaction (actual rename happens after commit)
                    content_path = str(
                        self.content_dir / version.artifact_id
                        / final_template.format(version_num=new_version_num)
                    )
                    if not Path(content_path).resolve().is_relative_to(
                        (self.content_dir / version.artifact_id).resolve()
                    ):
                        raise ValueError(f"Unsafe content path: {content_path}")
                self._conn.execute(
                    """INSERT INTO artifact_versions
                       (artifact_id, version_num, content, content_path, size_bytes,
                        token_count, section_index, change_log, source, created_at,
                        snapshot_type, snapshot_label, author, parent_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        version.artifact_id,
                        new_version_num,
                        content,
                        content_path,
                        version.size_bytes,
                        version.token_count,
                        version.section_index,
                        version.change_log,
                        version.source,
                        version.created_at,
                        version.snapshot_type,
                        version.snapshot_label,
                        version.author,
                        version.parent_version,
                    ),
                )
                final_summary = summary if summary else db_summary
                self._conn.execute(
                    """UPDATE artifacts SET current_version = ?, updated_at = ?,
                       content_hash = ?, summary = ? WHERE id = ?""",
                    (new_version_num, version.created_at, content_hash, final_summary, artifact.id),
                )
                self._conn.execute("COMMIT")
                # H2 phase 3: after successful commit, rename the temp file to the final path
                if tmp_path is not None and content_path is not None:
                    try:
                        Path(tmp_path).rename(content_path)
                    except OSError as e:
                        # rename failure: the DB row already points at content_path but the file is at tmp_path.
                        # Do not roll back the DB (already committed); gc_orphan_files will clean up, and the next read of that version
                        # of content_path hits FileNotFoundError — log ERROR for easier diagnosis
                        logger.error(
                            "Failed to rename tmp content %s -> %s: %s; "
                            "DB row committed, orphan/tmp file left for GC",
                            tmp_path, content_path, e,
                        )
                # R9: After commit, evict oldest non-snapshot versions per the cap (independent of the transaction; eviction failure does not affect the version write)
                try:
                    self._enforce_version_limit(artifact.id)
                except Exception as e:
                    logger.warning("Version limit enforcement failed for %s: %s", artifact.id, e)
                artifact.current_version = new_version_num
                artifact.updated_at = version.created_at
                artifact.content_hash = content_hash
                if final_summary and not artifact.summary:
                    artifact.summary = final_summary
                logger.info(
                    "Atomic version: %s v%d size=%d hash=%s",
                    artifact.id, new_version_num, version.size_bytes, content_hash,
                )
                return new_version_num
            except Exception as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                # H2: On rollback delete the temp file, no orphans
                if tmp_path is not None:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning("Failed to clean tmp content %s: %s", tmp_path, e)
                # P0-3/H8: Map SQLite OperationalError by message to retryable ConflictError / ResourceLimitError
                if isinstance(exc, sqlite3.Error):
                    raise _translate_sqlite_error(exc) from exc
                raise

    def _hydrate_version_content(self, artifact_id: str, ver: ArtifactVersion) -> None:
        if not ver.content and ver.content_path:
            try:
                ver.content = self._read_content_file(ver.content_path)
            except FileNotFoundError:
                # L-16: A missing content file is data corruption; do not fabricate an empty version — raise and let the engine/caller decide
                logger.error(
                    "Corrupt version: content file missing for %s v%s at %s",
                    artifact_id,
                    ver.version_num,
                    ver.content_path,
                )
                raise

    def get_version(self, artifact_id: str, version_num: int) -> ArtifactVersion | None:
        with self._read_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM artifact_versions WHERE artifact_id = ? AND version_num = ?",
                (artifact_id, version_num),
            )
            row = cur.fetchone()
        if row is None:
            return None
        ver = _version_from_row(row)
        self._hydrate_version_content(artifact_id, ver)
        return ver

    def list_versions(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]:
        # P-3: Pagination + optionally skipping content file reads avoids unbounded full scans into memory
        # P1-5: Route through _clamp_pagination uniformly — page>=1, page_size<=500, non-int falls back to defaults
        page, page_size = _clamp_pagination(page, page_size)
        offset = (page - 1) * page_size
        with self._read_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM artifact_versions WHERE artifact_id = ? "
                "ORDER BY version_num DESC LIMIT ? OFFSET ?",
                (artifact_id, page_size, offset),
            )
            rows = cur.fetchall()
        results = []
        for r in rows:
            ver = _version_from_row(r)
            if include_content:
                try:
                    self._hydrate_version_content(artifact_id, ver)
                except FileNotFoundError:
                    continue
            results.append(ver)
        if offset > 0 and not results:
            logger.debug("list_versions page %d empty for %s", page, artifact_id)
        return results

    def list_snapshots(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]:
        # P1-5: Route through _clamp_pagination uniformly — page>=1, page_size<=500, non-int falls back to defaults
        page, page_size = _clamp_pagination(page, page_size)
        offset = (page - 1) * page_size
        with self._read_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM artifact_versions WHERE artifact_id = ? "
                "AND snapshot_type IN ('named','manual') "
                "ORDER BY version_num DESC LIMIT ? OFFSET ?",
                (artifact_id, page_size, offset),
            )
            rows = cur.fetchall()
        results = []
        for r in rows:
            ver = _version_from_row(r)
            if include_content:
                try:
                    self._hydrate_version_content(artifact_id, ver)
                except FileNotFoundError:
                    continue
            results.append(ver)
        return results

    # ── shares ─────────────────────────────────────────────────

    def save_share(self, share: ArtifactShare) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO artifact_shares (share_id, artifact_id, created_by, created_at, expires_at, revoked, access_count, max_accesses, last_access_at, permission)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(share_id) DO UPDATE SET
                     artifact_id=excluded.artifact_id,
                     created_by=excluded.created_by,
                     expires_at=excluded.expires_at,
                     revoked=excluded.revoked,
                     access_count=excluded.access_count,
                     max_accesses=excluded.max_accesses,
                     last_access_at=excluded.last_access_at,
                     permission=excluded.permission""",
                (
                    share.share_id,
                    share.artifact_id,
                    share.created_by,
                    share.created_at,
                    share.expires_at,
                    int(share.revoked),
                    share.access_count,
                    share.max_accesses,
                    share.last_access_at,
                    share.permission,
                ),
            )
            self._conn.commit()
        logger.info("Saved share: %s artifact=%s", share.share_id, share.artifact_id)

    def get_share(self, share_id: str) -> ArtifactShare | None:
        with self._read_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM artifact_shares WHERE share_id = ?", (share_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _share_from_row(row)

    def get_share_by_artifact(self, artifact_id: str) -> ArtifactShare | None:
        with self._read_conn() as conn:
            cur = conn.execute(
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
                "UPDATE artifact_shares SET revoked = 1 WHERE share_id = ?",
                (share_id,),
            )
            # L-12: Clear artifacts.share_id in sync to keep the unique index from blocking new shares
            self._conn.execute(
                "UPDATE artifacts SET share_id = NULL WHERE share_id = ?",
                (share_id,),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Revoked share %s ok=%s", share_id, ok)
        return ok

    def update_share_permission(self, share_id: str, permission: str) -> bool:
        # #77: Change permission on the same link — view/remix takes effect on next open
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifact_shares SET permission = ? WHERE share_id = ? AND revoked = 0",
                (permission, share_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Updated share %s permission=%s ok=%s", share_id, permission, ok)
        return ok
        return ok

    def increment_share_access(self, share_id: str, delta: int = 1) -> None:
        # E1: Write last_access_at as float epoch, consistent with the REAL column definition
        # R2: delta supports batched flushing (public GET memory buffer aggregated then written once)
        if delta <= 0:
            return
        now_ts = time.time()
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifact_shares SET access_count = access_count + ?, last_access_at = ? WHERE share_id = ?",
                (delta, now_ts, share_id),
            )
            self._conn.commit()

    def try_increment_share_access(self, share_id: str, max_accesses: int | None) -> bool:
        # P1-2/H5: Atomic check-and-increment — a single conditional UPDATE completes atomically under _write_lock
        # "increment only if under the cap", eliminating the TOCTOU in get_shared_artifact of reading access_count then incrementing.
        # Returns True = counter incremented (allowed), False = cap reached or share missing (denied).
        now_ts = time.time()
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifact_shares SET access_count = access_count + 1, last_access_at = ? "
                "WHERE share_id = ? "
                "AND (max_accesses IS NULL OR access_count < max_accesses)",
                (now_ts, share_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── folders ────────────────────────────────────────────────

    def save_folder(self, folder: ArtifactFolder) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO artifact_folders (folder_id, name, parent_id, project_id, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(folder_id) DO UPDATE SET name=excluded.name, parent_id=excluded.parent_id""",
                (
                    folder.folder_id,
                    folder.name,
                    folder.parent_id,
                    folder.project_id,
                    folder.created_at,
                ),
            )
            self._conn.commit()
        logger.info("Saved folder: %s name=%s", folder.folder_id, folder.name)

    def get_folder(self, folder_id: str) -> ArtifactFolder | None:
        with self._read_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM artifact_folders WHERE folder_id = ?", (folder_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _folder_from_row(row)

    def list_folders(self, project_id: str | None = None) -> list[ArtifactFolder]:
        with self._read_conn() as conn:
            if project_id:
                cur = conn.execute(
                    "SELECT * FROM artifact_folders WHERE project_id = ? ORDER BY name",
                    (project_id,),
                )
            else:
                cur = conn.execute("SELECT * FROM artifact_folders ORDER BY name")
            rows = cur.fetchall()
        return [_folder_from_row(r) for r in rows]

    def rename_folder(self, folder_id: str, new_name: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifact_folders SET name = ? WHERE folder_id = ?",
                (new_name, folder_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Renamed folder %s -> %s ok=%s", folder_id, new_name, ok)
        return ok

    def delete_folder(self, folder_id: str) -> bool:
        # L-13: Collect descendant folders so child folders never keep a dangling parent_id pointing at a deleted folder
        with self._write_lock:
            self._conn.execute(
                "UPDATE artifacts SET folder_id = NULL WHERE folder_id = ?",
                (folder_id,),
            )
            descendant_ids = [folder_id]
            pending = [folder_id]
            while pending:
                cur = self._conn.execute(
                    "SELECT folder_id FROM artifact_folders WHERE parent_id IN ({})".format(
                        ",".join("?" * len(pending))
                    ),
                    pending,
                )
                children = [row["folder_id"] for row in cur.fetchall()]
                if not children:
                    break
                descendant_ids.extend(children)
                pending = children
            # Nullify folder_id of direct/indirect descendant artifacts
            self._conn.execute(
                "UPDATE artifacts SET folder_id = NULL WHERE folder_id IN ({})".format(
                    ",".join("?" * len(descendant_ids))
                ),
                descendant_ids,
            )
            cur = self._conn.execute(
                "DELETE FROM artifact_folders WHERE folder_id IN ({})".format(
                    ",".join("?" * len(descendant_ids))
                ),
                descendant_ids,
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Deleted folder %s ok=%s", folder_id, ok)
        return ok

    def move_to_folder(self, artifact_id: str, folder_id: str | None) -> bool:
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
                """INSERT INTO artifact_tags (tag_id, name, color, scope)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(tag_id) DO UPDATE SET name=excluded.name,
                   color=excluded.color, scope=excluded.scope""",
                (tag.tag_id, tag.name, tag.color, tag.scope),
            )
            self._conn.commit()
        logger.info("Saved tag: %s name=%s scope=%s", tag.tag_id, tag.name, tag.scope)

    def get_tag(self, tag_id: str) -> ArtifactTag | None:
        with self._read_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM artifact_tags WHERE tag_id = ?", (tag_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _tag_from_row(row)

    def get_tag_by_name(self, name: str, scope: str | None = None) -> ArtifactTag | None:
        # L-7: Query by scope; scope=None falls back to global (compatible with scope-less callers)
        with self._read_conn() as conn:
            if scope is None:
                cur = conn.execute(
                    "SELECT * FROM artifact_tags WHERE name = ? AND scope IS NULL",
                    (name,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM artifact_tags WHERE name = ? AND scope = ?",
                    (name, scope),
                )
            row = cur.fetchone()
        if row is None:
            return None
        return _tag_from_row(row)

    def list_tags(self, scope: str | None = None) -> list[ArtifactTag]:
        # L-7: list_tags filters by scope; returns all when not given (backward compatible)
        with self._read_conn() as conn:
            if scope is None:
                cur = conn.execute("SELECT * FROM artifact_tags ORDER BY name")
            else:
                cur = conn.execute(
                    "SELECT * FROM artifact_tags WHERE scope = ? ORDER BY name",
                    (scope,),
                )
            rows = cur.fetchall()
        return [_tag_from_row(r) for r in rows]

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
        with self._read_conn() as conn:
            cur = conn.execute(
                """SELECT t.* FROM artifact_tags t
                   JOIN artifact_tag_map m ON t.tag_id = m.tag_id
                   WHERE m.artifact_id = ? ORDER BY t.name""",
                (artifact_id,),
            )
            rows = cur.fetchall()
        return [_tag_from_row(r) for r in rows]

    # ── events ─────────────────────────────────────────────────

    def save_event(self, event: ArtifactEvent) -> None:
        payload_json = json.dumps(event.payload) if event.payload else None
        # A-8: created_at unified as REAL epoch; legacy models passing ISO strings are converted too
        created_at = self._coerce_since_ts(event.created_at) if event.created_at else time.time()
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO artifact_events (event_id, artifact_id, session_id, event_type, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.artifact_id,
                    event.session_id,
                    event.event_type,
                    payload_json,
                    created_at,
                ),
            )
            self._conn.commit()
        logger.info(
            "Saved event: %s type=%s artifact=%s",
            event.event_id,
            event.event_type,
            event.artifact_id,
        )

    def _coerce_since_ts(self, since_ts) -> float:
        # A-8: since_ts accepts both REAL epoch and ISO strings, normalized to REAL for comparison with REAL columns
        try:
            return float(since_ts)
        except (TypeError, ValueError):
            pass
        from datetime import datetime

        try:
            return datetime.fromisoformat(str(since_ts)).timestamp()
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid since_ts={since_ts!r}") from e

    def list_events(
        self,
        artifact_id: str | None = None,
        session_id: str | None = None,
        since_ts: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ArtifactEvent], int]:
        # P1-5/M5: Harden pagination inputs
        page, page_size = _clamp_pagination(page, page_size)
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
            params.append(self._coerce_since_ts(since_ts))
        where = " AND ".join(conditions) if conditions else "1=1"
        with self._read_conn() as conn:
            count_cur = conn.execute(
                f"SELECT COUNT(*) FROM artifact_events WHERE {where}", params
            )
            total = count_cur.fetchone()[0]
            offset = (page - 1) * page_size
            cur = conn.execute(
                f"SELECT * FROM artifact_events WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            )
            rows = cur.fetchall()
        events = [_event_from_row(r) for r in rows]
        return events, total

    def move_to_project_kb(self, artifact_id: str, project_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET in_project_kb = 1, project_id = ?, updated_at = ? WHERE id = ?",
                (project_id, time.time(), artifact_id),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info(
            "Moved artifact %s to project_kb project=%s ok=%s",
            artifact_id,
            project_id,
            ok,
        )
        return ok

    # ── migrations ─────────────────────────────────────────────

    def _guess_ext(self, artifact_id: str, content: str) -> str:
        # C-10: The extension comes from user-supplied name; restrict to a safe charset and reject path-traversal fragments
        art = self.get_artifact(artifact_id)
        if art and "." in art.name:
            raw = art.name.rsplit(".", 1)[-1]
            ext = _EXT_SAFE_RE.sub("", raw)[:8].lower()
            if ext:
                return ext
        return "txt"

    def _migrate_kind_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "kind" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifacts ADD COLUMN kind TEXT DEFAULT NULL CHECK(kind IS NULL OR kind IN ('app','code','document','game','tool','template'));"
            )
            self._conn.commit()
            logger.info("Migrated: added 'kind' column to artifacts table")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind)"
        )
        self._conn.commit()

    def _migrate_source_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifact_versions)")
        columns = {row["name"] for row in cur.fetchall()}
        if "source" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifact_versions ADD COLUMN source TEXT DEFAULT 'manual' CHECK(source IN ('manual','ai_generation'));"
            )
            self._conn.commit()
            logger.info("Migrated: added 'source' column to artifact_versions table")

    def _migrate_project_id_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "project_id" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifacts ADD COLUMN project_id TEXT DEFAULT NULL;"
            )
            self._conn.commit()
            logger.info("Migrated: added 'project_id' column to artifacts table")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_project_id ON artifacts(project_id)"
        )
        self._conn.commit()

    def _migrate_metadata_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "metadata" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifacts ADD COLUMN metadata TEXT DEFAULT NULL;"
            )
            self._conn.commit()
            logger.info("Migrated: added 'metadata' column to artifacts table")

    def _migrate_ownership_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "owner_user_id" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN owner_user_id TEXT DEFAULT NULL;"
            )
        if "ownership_type" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN ownership_type TEXT DEFAULT 'free';"
            )
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info(
                "Migrated: added ownership columns to artifacts (%d cols)",
                len(new_cols),
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_ownership ON artifacts(ownership_type, project_id)"
        )
        self._conn.commit()

    def _migrate_lifecycle_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "is_starred" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN is_starred INTEGER DEFAULT 0;"
            )
        if "is_pinned" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN is_pinned INTEGER DEFAULT 0;"
            )
        if "pinned_chat_id" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN pinned_chat_id TEXT DEFAULT NULL;"
            )
        if "folder_id" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN folder_id TEXT DEFAULT NULL;"
            )
        if "deleted_at" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN deleted_at TEXT DEFAULT NULL;"
            )
        if "content_hash" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN content_hash TEXT DEFAULT NULL;"
            )
        if "active_in_session" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN active_in_session TEXT DEFAULT NULL;"
            )
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info(
                "Migrated: added lifecycle columns to artifacts (%d cols)",
                len(new_cols),
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_starred_pinned ON artifacts(is_starred, is_pinned)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_deleted ON artifacts(is_deleted, deleted_at)"
        )
        self._conn.commit()

    def _migrate_share_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "share_id" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifacts ADD COLUMN share_id TEXT DEFAULT NULL;"
            )
            self._conn.commit()
            logger.info("Migrated: added 'share_id' column to artifacts table")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_share_id ON artifacts(share_id) WHERE share_id IS NOT NULL"
        )
        self._conn.commit()

    def _migrate_kb_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "in_project_kb" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifacts ADD COLUMN in_project_kb INTEGER DEFAULT 0;"
            )
            self._conn.commit()
            logger.info("Migrated: added 'in_project_kb' column to artifacts table")

    def _migrate_snapshot_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifact_versions)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "snapshot_type" not in columns:
            new_cols.append(
                "ALTER TABLE artifact_versions ADD COLUMN snapshot_type TEXT DEFAULT 'auto';"
            )
        if "snapshot_label" not in columns:
            new_cols.append(
                "ALTER TABLE artifact_versions ADD COLUMN snapshot_label TEXT DEFAULT NULL;"
            )
        if "author" not in columns:
            new_cols.append(
                "ALTER TABLE artifact_versions ADD COLUMN author TEXT DEFAULT NULL;"
            )
        if "parent_version" not in columns:
            new_cols.append(
                "ALTER TABLE artifact_versions ADD COLUMN parent_version INTEGER DEFAULT NULL;"
            )
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info(
                "Migrated: added snapshot columns to artifact_versions (%d cols)",
                len(new_cols),
            )

    def _migrate_source_module_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        new_cols = []
        if "source_module" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN source_module TEXT DEFAULT NULL;"
            )
        if "workspace_id" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN workspace_id TEXT DEFAULT NULL;"
            )
        if "workflow_run_id" not in columns:
            new_cols.append(
                "ALTER TABLE artifacts ADD COLUMN workflow_run_id TEXT DEFAULT NULL;"
            )
        if new_cols:
            self._conn.executescript(" ".join(new_cols))
            self._conn.commit()
            logger.info(
                "Migrated: added source_module columns (%d cols)", len(new_cols)
            )

    def list_by_source(
        self,
        source_module: str,
        workspace_id: str | None = None,
        workflow_run_id: str | None = None,
        page: int = 1,
        page_size: int = 200,
    ) -> list[Artifact]:
        # P-3: Pagination avoids unbounded scans
        # P1-5: Route through _clamp_pagination uniformly — page>=1, page_size<=500, non-int falls back to defaults
        page, page_size = _clamp_pagination(page, page_size)
        conditions = ["is_deleted = 0", "source_module = ?"]
        params: list = [source_module]
        if workspace_id:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)
        if workflow_run_id:
            conditions.append("workflow_run_id = ?")
            params.append(workflow_run_id)
        where = " AND ".join(conditions)
        offset = (page - 1) * page_size
        with self._read_conn() as conn:
            cur = conn.execute(
                f"SELECT * FROM artifacts WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            )
            rows = cur.fetchall()
        return [_artifact_from_row(row) for row in rows]

    def close(self) -> None:
        # Idempotent: repeated close (e.g. test closes explicitly, then fixture teardown closes again) must not crash.
        # _conn already None means previously closed; return directly.
        if self._conn is None:
            logger.debug("SQLiteStorage.close() no-op: already closed")
            return
        # P1-4: Stop the checkpoint background thread first, then run the final TRUNCATE checkpoint, ensuring an empty -wal after shutdown.
        if self._checkpoint_thread is not None:
            self._checkpoint_stop.set()
            self._checkpoint_thread.join(timeout=5)
            self._checkpoint_thread = None
        try:
            self._run_checkpoint("TRUNCATE")
        except Exception as e:
            logger.warning("WAL final TRUNCATE checkpoint failed: %s", e)
        # E7: Close the pool's read-only connections (non-blocking get; a miss means it is borrowed, skip)
        while True:
            try:
                rc = self._read_pool.get_nowait()
                rc.close()
            except queue.Empty:
                break
        self._conn.close()
        self._conn = None
        logger.info("SQLiteStorage closed")

    def health_check(self) -> bool:
        # Ops-5: /readyz probe dependency — SELECT 1 verifies DB connectivity and content_dir writability.
        # No real writes, liveness only. Returns False on failure instead of raising (the caller maps it to a status code).
        try:
            with self._write_lock:
                row = self._conn.execute("SELECT 1").fetchone()
                if row is None or row[0] != 1:
                    return False
            return self.content_dir.exists()
        except Exception:  # noqa: BLE001
            logger.exception("storage health_check failed")
            return False

    def _migrate_token_count_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifact_versions)")
        columns = {row["name"] for row in cur.fetchall()}
        if "token_count" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifact_versions ADD COLUMN token_count INTEGER NOT NULL DEFAULT 0;"
            )
            self._conn.commit()
            logger.info(
                "Migrated: added 'token_count' column to artifact_versions table"
            )

    def _migrate_section_index_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(artifact_versions)")
        columns = {row["name"] for row in cur.fetchall()}
        if "section_index" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifact_versions ADD COLUMN section_index TEXT DEFAULT NULL;"
            )
            self._conn.commit()
            logger.info(
                "Migrated: added 'section_index' column to artifact_versions table"
            )

    def _migrate_size_bytes_column(self) -> None:
        try:
            cols = [
                r[1]
                for r in self._conn.execute(
                    "PRAGMA table_info(artifact_versions)"
                ).fetchall()
            ]
            if "token_count" in cols and "size_bytes" not in cols:
                self._conn.execute(
                    "ALTER TABLE artifact_versions RENAME COLUMN token_count TO size_bytes"
                )
                self._conn.commit()
                logger.info("Migrated artifact_versions: token_count -> size_bytes")
        except Exception as e:  # noqa: BLE001
            logger.warning("Migration size_bytes failed: %s", e)

    def _rebuild_table_keep_data(self, table: str, new_ddl: str) -> None:
        # Generic table-rebuild helper: the new table coexists with the old, data is copied, then the old is dropped and the new renamed into place.
        # Key constraint: never rename the old table — ALTER TABLE RENAME rewrites other tables' FK references
        # to the old table name; once the old table is dropped the references dangle (production defect in the v0.7.6 first-cut migration).
        # new_ddl must create the table under the rebuild name; the caller is responsible for swapping table names and CHECK clauses etc.
        rebuild = f"{table}__rebuild"
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute(f'DROP TABLE IF EXISTS "{rebuild}"')
            self._conn.execute(new_ddl)
            cols = [
                r["name"] for r in self._conn.execute(f'PRAGMA table_info("{rebuild}")').fetchall()
            ]
            col_list = ", ".join(f'"{c}"' for c in cols)
            idx_ddls = [
                r[0]
                for r in self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND tbl_name=? AND sql IS NOT NULL",
                    (table,),
                ).fetchall()
            ]
            self._conn.execute(
                f'INSERT INTO "{rebuild}" ({col_list}) SELECT {col_list} FROM "{table}"'
            )
            self._conn.execute(f'DROP TABLE "{table}"')
            self._conn.execute(f'ALTER TABLE "{rebuild}" RENAME TO "{table}"')
            for ddl in idx_ddls:
                self._conn.execute(ddl)
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            logger.error("Rebuild table %s failed: %s", table, e)
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_artifact_type_check(self) -> None:
        # #100-1: The legacy artifacts.type CHECK constraint lacks 'svg'/'mermaid', so inserts are
        # rejected with IntegrityError. SQLite cannot ALTER a CHECK, so the table must be rebuilt. The rebuild uses
        # _rebuild_table_keep_data (new/old tables coexist with data copied; the old table is never renamed),
        # index DDL is captured verbatim from sqlite_master (expression indexes included) and recreated one by one after the swap.
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='artifacts'"
        ).fetchone()
        old_check = "type IN ('code','markdown','html','react','data')"
        if row is None or not row[0] or old_check not in row[0]:
            return
        old_ddl = row[0]
        new_ddl = old_ddl.replace(
            old_check,
            "type IN ('code','markdown','html','react','svg','mermaid','data')",
        )
        new_ddl = new_ddl.replace(
            "CREATE TABLE artifacts", "CREATE TABLE artifacts__rebuild", 1
        )
        logger.info("Migrating artifacts.type CHECK: adding svg/mermaid")
        self._rebuild_table_keep_data("artifacts", new_ddl)
        logger.info("Migrated artifacts.type CHECK done: svg/mermaid now accepted")

    def _migrate_artifact_type_fk_repair(self) -> None:
        # Fixes the production defect in the v0.7.6 first-cut migration: that version swapped tables via ALTER TABLE RENAME, and SQLite
        # rewrote child-table FK references to the temp name artifacts_type_check_tmp (in quoted form
        # "artifacts_type_check_tmp"); after the temp table was dropped the references dangled and child-table inserts failed with
        # "no such table: main.artifacts_type_check_tmp". This migration rebuilds every child table with
        # dangling references, pointing REFERENCES back at artifacts (handles both quoted and unquoted DDL).
        broken = "artifacts_type_check_tmp"
        rows = self._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE ?",
            (f"%{broken}%",),
        ).fetchall()
        if not rows:
            return
        for r in rows:
            name, sql = r["name"], r["sql"]
            fixed = sql.replace(f'"{broken}"', "artifacts").replace(broken, "artifacts")
            quoted = f'CREATE TABLE "{name}"'
            if quoted in fixed:
                fixed = fixed.replace(quoted, f'CREATE TABLE "{name}__rebuild"', 1)
            else:
                fixed = fixed.replace(
                    f"CREATE TABLE {name}", f"CREATE TABLE {name}__rebuild", 1
                )
            logger.info("Repairing dangling FK references on table %s", name)
            self._rebuild_table_keep_data(name, fixed)
        logger.info("Repaired %d tables with dangling FK references", len(rows))

    _SCHEMA_VERSION = 2

    def _migrate_schema_meta(self) -> None:
        # A-6: schema_meta records schema_version, enabling the migration framework and version detection
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value INTEGER)"
        )
        # R5: applied_migrations records applied migration names, enabling version-based skipping
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS applied_migrations "
            "(name TEXT PRIMARY KEY, applied_at REAL)"
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (self._SCHEMA_VERSION,),
            )
            self._conn.commit()
        else:
            db_ver = row[0]
            if db_ver > self._SCHEMA_VERSION:
                # A-6: DB version newer than the code — refuse to start, avoiding silent corruption from an unknown schema
                raise RuntimeError(
                    f"DB schema_version {db_ver} > code {self._SCHEMA_VERSION}; "
                    "downgrade unsupported"
                )

    # R5: Migration registry — order is execution order. Append new migrations at the end; never reorder existing ones.
    _MIGRATIONS: list[tuple[str, str]] = [
        ("migrate_kind_column", "_migrate_kind_column"),
        ("migrate_source_column", "_migrate_source_column"),
        ("migrate_project_id_column", "_migrate_project_id_column"),
        ("migrate_metadata_column", "_migrate_metadata_column"),
        ("migrate_ownership_columns", "_migrate_ownership_columns"),
        ("migrate_lifecycle_columns", "_migrate_lifecycle_columns"),
        ("migrate_share_column", "_migrate_share_column"),
        ("migrate_kb_column", "_migrate_kb_column"),
        ("migrate_snapshot_columns", "_migrate_snapshot_columns"),
        ("migrate_source_module_columns", "_migrate_source_module_columns"),
        ("migrate_size_bytes_column", "_migrate_size_bytes_column"),
        ("migrate_token_count_column", "_migrate_token_count_column"),
        ("migrate_section_index_column", "_migrate_section_index_column"),
        ("migrate_event_timestamps", "_migrate_event_timestamps"),
        ("migrate_share_max_accesses_and_timestamps", "_migrate_share_max_accesses_and_timestamps"),
        ("migrate_tag_scope_column", "_migrate_tag_scope_column"),
        ("migrate_remix_lineage", "_migrate_remix_lineage"),
        ("migrate_share_permission", "_migrate_share_permission"),
        ("migrate_artifact_type_check", "_migrate_artifact_type_check"),
        ("migrate_artifact_type_fk_repair2", "_migrate_artifact_type_fk_repair"),
    ]

    def _run_migrations_gated(self) -> None:
        # R5: Run only migrations not recorded in applied_migrations, then record them. Avoids a full re-check on every startup.
        applied = {
            r["name"]
            for r in self._conn.execute("SELECT name FROM applied_migrations").fetchall()
        }
        for name, method_name in self._MIGRATIONS:
            if name in applied:
                continue
            getattr(self, method_name)()
            self._conn.execute(
                "INSERT OR IGNORE INTO applied_migrations (name, applied_at) VALUES (?, ?)",
                (name, time.time()),
            )
            self._conn.commit()
            logger.debug("Migration applied and recorded: %s", name)


    def _migrate_remix_lineage(self) -> None:
        # #77: remix lineage — artifacts.parent_artifact_id (which artifact this derives from, NULL = original)
        cur = self._conn.execute("PRAGMA table_info(artifacts)")
        columns = {row["name"] for row in cur.fetchall()}
        if "parent_artifact_id" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifacts ADD COLUMN parent_artifact_id TEXT DEFAULT NULL;"
            )
            self._conn.commit()
            logger.info("Migrated: added 'parent_artifact_id' column to artifacts table")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts(parent_artifact_id)"
        )
        self._conn.commit()

    def _migrate_share_permission(self) -> None:
        # #77: share permission level — 'view' (read-only, default) / 'remix' (derivable)
        cur = self._conn.execute("PRAGMA table_info(artifact_shares)")
        columns = {row["name"] for row in cur.fetchall()}
        if "permission" not in columns:
            self._conn.executescript(
                "ALTER TABLE artifact_shares ADD COLUMN permission TEXT DEFAULT 'view' "
                "CHECK(permission IN ('view','remix'));"
            )
            self._conn.commit()
            logger.info("Migrated: added 'permission' column to artifact_shares table")

    def _migrate_event_timestamps(self) -> None:
        # A-8: artifact_events.created_at unified as REAL epoch. Legacy ISO strings converted to epoch.
        from datetime import datetime

        rows = self._conn.execute(
            "SELECT event_id, created_at FROM artifact_events WHERE created_at IS NOT NULL"
        ).fetchall()
        updates = []
        for r in rows:
            raw = r["created_at"]
            if raw is None:
                continue
            try:
                float(raw)
                continue
            except (TypeError, ValueError):
                pass
            try:
                ts = datetime.fromisoformat(str(raw)).timestamp()
            except (ValueError, TypeError):
                logger.warning(
                    "Unparseable event created_at %s for %s, fallback to now",
                    raw,
                    r["event_id"],
                )
                ts = time.time()
            updates.append((ts, r["event_id"]))
        if updates:
            with self._write_lock:
                self._conn.executemany(
                    "UPDATE artifact_events SET created_at = ? WHERE event_id = ?",
                    updates,
                )
                self._conn.commit()
            logger.info("Migrated %d event timestamps ISO -> REAL", len(updates))

    def _ensure_metadata_indexes(self) -> None:
        # P2-7/F6: Create json_extract expression indexes for the configured high-frequency metadata fields.
        # metadata_filter uses the index instead of a full scan. Keys already passed _META_KEY_RE validation in __init__;
        # this second check guards against internal misuse. Index name idx_meta_<key>, created idempotently.
        if not self.metadata_indexed_keys:
            return
        created = []
        for key in self.metadata_indexed_keys:
            if not _META_KEY_RE.fullmatch(key):
                logger.warning("Skip metadata index, unsafe key: %s", key)
                continue
            idx_name = f"idx_meta_{key}"
            expr = f"json_extract(metadata, '$.{key}')"
            self._conn.execute(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON artifacts({expr})'
            )
            created.append(idx_name)
        if created:
            self._conn.commit()
            logger.info("Ensured %d metadata indexes: %s", len(created), created)

    def _migrate_share_max_accesses_and_timestamps(self) -> None:
        # E1/E2: Add max_accesses column to artifact_shares; created_at/last_access_at
        # migrated from TEXT ISO to REAL epoch (schema_version 2).
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(artifact_shares)").fetchall()
        }
        if "max_accesses" not in cols:
            self._conn.execute(
                "ALTER TABLE artifact_shares ADD COLUMN max_accesses INTEGER"
            )
            self._conn.commit()
            logger.info("Added max_accesses column to artifact_shares")
        # ISO -> REAL epoch for shares
        self._coerce_text_ts_to_real(
            "artifact_shares", "share_id", ["created_at", "last_access_at"]
        )
        # folder created_at migrated the same way
        self._coerce_text_ts_to_real("artifact_folders", "folder_id", ["created_at"])

    def _coerce_text_ts_to_real(
        self, table: str, id_col: str, ts_cols: list[str]
    ) -> None:
        # E1: Convert created_at-like columns of TEXT (ISO string) rows to REAL epoch; rows already float are skipped.
        from datetime import datetime

        for col in ts_cols:
            rows = self._conn.execute(
                f"SELECT {id_col}, {col} FROM {table} WHERE {col} IS NOT NULL"
            ).fetchall()
            updates = []
            for r in rows:
                raw = r[col]
                try:
                    float(raw)
                    continue
                except (TypeError, ValueError):
                    pass
                try:
                    ts = datetime.fromisoformat(str(raw)).timestamp()
                except (ValueError, TypeError):
                    logger.warning(
                        "Unparseable %s.%s=%r for %s, fallback to now",
                        table, col, raw, r[id_col],
                    )
                    ts = time.time()
                updates.append((ts, r[id_col]))
            if updates:
                with self._write_lock:
                    self._conn.executemany(
                        f"UPDATE {table} SET {col} = ? WHERE {id_col} = ?",
                        updates,
                    )
                    self._conn.commit()
                logger.info(
                    "Migrated %d %s.%s ISO -> REAL", len(updates), table, col
                )

    def _migrate_tag_scope_column(self) -> None:
        # L-7: Add scope column to artifact_tags; name uniqueness becomes a (name, scope) composite unique
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(artifact_tags)").fetchall()
        }
        if "scope" not in cols:
            self._conn.execute("ALTER TABLE artifact_tags ADD COLUMN scope TEXT")
            self._conn.commit()
            logger.info("Added scope column to artifact_tags")
        # Legacy DBs have name as a single-column UNIQUE constraint, conflicting with the new UNIQUE(name, scope) — rebuild the index.
        # SQLite cannot ALTER constraints directly; dropping the auto index name (sqlite_autoindex_*) requires rebuilding the table.
        # Detect and rebuild here: if a single-column name unique index exists, rebuild the table with composite uniqueness.
        idx_rows = self._conn.execute(
            "PRAGMA index_list(artifact_tags)"
        ).fetchall()
        needs_rebuild = False
        for ir in idx_rows:
            # origin='u' = UNIQUE constraint autoindex; inspect its columns
            if ir["origin"] == "u":
                info = self._conn.execute(
                    f"PRAGMA index_info({ir['name']})"
                ).fetchall()
                cols_in = [c["name"] for c in info]
                if cols_in == ["name"]:
                    needs_rebuild = True
                    break
        if needs_rebuild:
            self._conn.executescript(
                """
                BEGIN;
                CREATE TABLE IF NOT EXISTS artifact_tags_new (
                    tag_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    color TEXT,
                    scope TEXT,
                    UNIQUE(name, scope)
                );
                INSERT OR IGNORE INTO artifact_tags_new (tag_id, name, color, scope)
                SELECT tag_id, name, color, scope FROM artifact_tags;
                DROP TABLE artifact_tags;
                ALTER TABLE artifact_tags_new RENAME TO artifact_tags;
                CREATE INDEX IF NOT EXISTS idx_tags_scope ON artifact_tags(scope);
                COMMIT;
                """
            )
            self._conn.commit()
            logger.info("Rebuilt artifact_tags with UNIQUE(name, scope)")
        else:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tags_scope ON artifact_tags(scope)"
            )
            self._conn.commit()
