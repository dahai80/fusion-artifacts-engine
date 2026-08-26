import errno
import json
import logging
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

_EXT_SAFE_RE = re.compile(r"[^A-Za-z0-9]")
_META_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _log_rmtree_error(func, path, exc_info):  # noqa: ANN001
    # M-4: rmtree 失败必须可见，勿静默吞
    logger.error("rmtree failed on %s during %s: %s", path, func.__name__, exc_info[1])


def _coerce_bool(name: str, value) -> int:
    # M-5: 用户可控 filters 值强转——接受 bool/0/1/"true"/"false"，其余显式 ValueError
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
    # M-5: since/until 必须可转 float，ISO 串等明确报错而非 500
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
    # P0-3/H8: SQLite 写路径 OperationalError 按消息映射——
    # "database is locked"/"database table is locked"→ConflictError(-32002, 可重试)；
    # "disk I/O error"/"database disk image is malformed"/"disk full"→ResourceLimitError(-32003)；
    # 其余 OperationalError 原样上抛（连接损坏等由上层兜底）。
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
    # P0-5/H9: 内容字节超 max_content_bytes 拒绝写入（0=不限）
    if limit <= 0:
        return
    size = len(content.encode("utf-8"))
    if size > limit:
        logger.warning("Content rejected: %d bytes > max_content_bytes %d", size, limit)
        raise ResourceLimitError(
            f"Content too large: {size} bytes exceeds max_content_bytes {limit}"
        )


def _enforce_metadata_limit(metadata: dict | None, limit: int) -> None:
    # P0-5/H9: metadata JSON 字节超 max_metadata_bytes 拒绝写入（0=不限）
    if limit <= 0 or not metadata:
        return
    size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    if size > limit:
        logger.warning("Metadata rejected: %d bytes > max_metadata_bytes %d", size, limit)
        raise ResourceLimitError(
            f"Metadata too large: {size} bytes exceeds max_metadata_bytes {limit}"
        )


def _clamp_pagination(page, page_size, max_size: int = 500) -> tuple[int, int]:
    # P1-5/M5: 分页入参硬化——page>=1、page_size<=max_size、非整数 try/except 回退默认。
    # 防 page_size=10**9 拉全表 DoS、page<=0 负偏移、page="abc" TypeError。
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


from fusion_artifacts_engine.storage.base import StorageDriver

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
            # M-3: 损坏 JSON 必须大声告警，区分"无 metadata"与"损坏 metadata"
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
    # E1: 统一时间戳读出 float epoch。兼容旧 ISO 串与 None。
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
            # M-3: 损坏 payload JSON 记 ERROR
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
    ):
        self.db_path = db_path
        self.content_dir = content_dir
        self.small_content_limit = small_content_limit
        # R9: 单 artifact 版本上限，0=不限。超限淘汰最旧非快照版本（防磁盘无限增长）
        self.max_versions_per_artifact = max_versions_per_artifact
        # 运维4: 磁盘水位告警百分比（0-100），0=禁用预检。写前预检，超阈拒绝写
        self.disk_space_warning_pct = max(0, int(disk_space_warning_pct))
        # P0-5/H9: 单版本内容字节上限，0=不限。超限拒绝写入防 OOM/磁盘耗尽
        self.max_content_bytes = max(0, int(max_content_bytes))
        # P0-5/H9: 单 artifact metadata JSON 字节上限，0=不限。超限拒绝写入
        self.max_metadata_bytes = max(0, int(max_metadata_bytes))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        content_dir.mkdir(parents=True, exist_ok=True)
        # H1: 写连接池决策——SQLite WAL 下 BEGIN IMMEDIATE 由 DB 级锁序列化写，
        # 多写连接无法并行写（物理上限）。故写连接池 size=1（单 self._conn + _write_lock）
        # 是正确架构。审计 H1 的真正痛点是「文件 I/O 持 _write_lock 拖长持锁时长」，
        # 已由 H2 修复（文件写移出事务/锁外）。读侧并发由 E7 只读连接池承担。
        # 未来换 Postgres/对象存储时（H8）再引入真写连接池。
        self._write_lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # E7: 只读连接池——复用只读连接，避免高频读反复 connect/close。
        # WAL 允许并发读，池大小 4 兼顾并发与连接开销。
        self._read_pool_size = 4
        self._read_pool: queue.Queue = queue.Queue(maxsize=self._read_pool_size)
        for _ in range(self._read_pool_size):
            rc = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
            rc.row_factory = sqlite3.Row
            self._read_pool.put(rc)
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        # R5: 迁移按版本门控——先建 schema_meta，再经注册表逐个跑未记录的迁移。
        # 已跑过的迁移记入 schema_meta.applied_migrations，跳过，避免每次启动全量重检查。
        # 迁移方法仍需幂等（兼容无 applied_migrations 记录的既有 DB 首次升级）。
        self._migrate_schema_meta()
        self._run_migrations_gated()
        # P1-4: WAL 周期 checkpoint 后台线程。PASSIVE 模式不阻塞读写，控制 -wal 文件增长，
        # 让 scripts/backup.sh 的 sqlite3 .backup（需 WAL 已 checkpoint 才能完整快照）可靠。
        # interval=0 禁用（依赖 SQLite 默认 1000 页自动 checkpoint）。daemon 线程，stop event 退出。
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
        # E7: 从只读连接池借连接，用完归还。WAL 允许并发读；复用连接省 connect/close 开销。
        # 池为空则阻塞等待（读并发受池大小约束，防连接泄漏）。
        conn = self._read_pool.get()
        try:
            yield conn
        finally:
            self._read_pool.put(conn)

    def _checkpoint_loop(self) -> None:
        # P1-4: 后台周期 PASSIVE checkpoint。PASSIVE 不阻塞读写，把已提交的 WAL 帧合并回主库。
        # 控制 -wal 文件无限增长（默认 1000 页才自动 checkpoint，长事务下会堆积）。
        while not self._checkpoint_stop.wait(self._wal_checkpoint_interval):
            try:
                self._run_checkpoint("PASSIVE")
            except Exception as e:
                logger.warning("WAL PASSIVE checkpoint failed: %s", e)

    def _run_checkpoint(self, mode: str = "PASSIVE") -> None:
        # P1-4: 执行一次 wal_checkpoint。PASSIVE=非阻塞合并；TRUNCATE=合并后截断 -wal 文件。
        # TRUNCATE 在 close 时用，确保停机后 -wal 文件清空（备份/迁移只拷 meta.db 即可）。
        with self._write_lock:
            cur = self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
            row = cur.fetchone()
            # (busy, log, checkpointed)：busy=1 表示有读写未完成（PASSIVE 正常），不报错
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
        # C-10/C-6 纵深防御：拒绝任何穿越出 content_dir 的路径
        if not path.resolve().is_relative_to(d.resolve()):
            logger.error(
                "Refusing content write outside content_dir: %s (ext=%s)", path, ext
            )
            raise ValueError(f"Unsafe content path: {path}")
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            # P0-4: 磁盘满/配额超限映射为 ResourceLimitError（可重试/降级），而非 -32603。
            if e.errno in (errno.ENOSPC, errno.EDQUOT):
                logger.error("disk full writing content file %s: %s", path, e)
                raise ResourceLimitError(f"disk full writing content: {e}") from e
            raise
        logger.debug("Wrote content file: %s", path)
        return str(path)

    def _write_content_file_tmp(
        self, artifact_id: str, content: str, ext: str = "txt"
    ) -> tuple[str, str, str]:
        # H2: 文件先写临时路径（事务外），事务提交成功后再 rename 到最终路径。
        # 回滚则删临时文件——不留孤儿。返回 (tmp_path, final_path_template, ext)。
        # final 路径含占位 {version_num}，提交后用实际版本号填充再 rename。
        d = self._artifact_content_dir(artifact_id)
        tmp_name = f".tmp_v{uuid.uuid4().hex}.{ext}"
        tmp_path = d / tmp_name
        if not tmp_path.resolve().is_relative_to(d.resolve()):
            logger.error("Refusing tmp content write outside content_dir: %s", tmp_path)
            raise ValueError(f"Unsafe content path: {tmp_path}")
        try:
            tmp_path.write_text(content, encoding="utf-8")
        except OSError as e:
            # P0-4: 磁盘满/配额超限映射为 ResourceLimitError（可重试/降级）。
            if e.errno in (errno.ENOSPC, errno.EDQUOT):
                logger.error("disk full writing tmp content file %s: %s", tmp_path, e)
                raise ResourceLimitError(f"disk full writing content: {e}") from e
            raise
        logger.debug("Wrote tmp content file: %s", tmp_path)
        return str(tmp_path), f"v{{version_num}}.{ext}", ext


    def _read_content_file(self, content_path: str) -> str:
        p = Path(content_path)
        if p.exists():
            return p.read_text(encoding="utf-8")
        logger.error("Content file missing: %s", content_path)
        raise FileNotFoundError(f"Content file missing: {content_path}")

    # ── artifact CRUD ──────────────────────────────────────────

    def check_disk_space(self) -> tuple[int, int, float] | None:
        # 运维4: 写前磁盘预检。返回 (used_pct, free_bytes, total) 或 None（禁用）。
        # 0=禁用监控。超阈值由调用方判定是否拒绝写。
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
        # 运维4: 写前预检——磁盘使用率超阈值则拒绝写并抛 ResourceLimitError。
        # 阈值 0 禁用。调用方在写路径入口调用。
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
        # 运维4: 写前磁盘预检，超阈值拒绝写
        self.ensure_disk_available()
        # P0-5/H9: metadata 字节上限校验
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
                         workflow_run_id=excluded.workflow_run_id""",
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

    def save_artifact_and_version(
        self, artifact: Artifact, version: ArtifactVersion
    ) -> None:
        # 运维4: 写前磁盘预检，超阈值拒绝写
        self.ensure_disk_available()
        # P0-5/H9: metadata + 内容字节上限校验
        _enforce_metadata_limit(artifact.metadata, self.max_metadata_bytes)
        _enforce_content_limit(version.content, self.max_content_bytes)
        # P1-8/H10: 内容文件两阶段写——先写临时路径（事务外），提交成功后 rename 到最终。
        # 回滚删临时文件，不留孤儿；文件 I/O 不在 BEGIN IMMEDIATE 内，缩短 _write_lock 持锁。
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
                         workflow_run_id=excluded.workflow_run_id""",
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
                # P1-8/H10: 提交成功后 rename 临时文件到最终路径
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
                # P1-8/H10: 事务失败——清理临时文件，不留孤儿
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
        conditions = ["session_id = ?"]
        params: list = [session_id]
        if not include_deleted:
            conditions.append("is_deleted = 0")
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if metadata_filter:
            for key, value in metadata_filter.items():
                # P-4: 校验 key 仅含安全标识符字符，拒绝 JSONPath 注入（. [ " 空白）
                if not _META_KEY_RE.fullmatch(key):
                    logger.warning("Reject metadata filter key (unsafe): %s", key)
                    continue
                json_path = f"$.{key}"
                conditions.append("json_extract(metadata, ?) = ?")
                # L-15: 原生类型绑定，bool 用 int，避免 str() 把数字过滤变永不命中
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
    ) -> tuple[list[Artifact], int]:
        # P1-5/M5: 分页入参硬化
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
        # P-2: 一条 SQL JOIN artifacts→artifact_versions(current_version) 聚合 token，
        # 替代逐 artifact get_version_content 的 N+1 往返与全量内容入内存。
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
                # 硬删先清无 FK 的关联表，避免孤儿行 (P2-5)。
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
        self, artifact_id: str, new_id: str, new_name: str | None = None
    ) -> Artifact | None:
        art = self.get_artifact(artifact_id)
        if art is None:
            return None
        # L-14: 拒绝覆盖碰巧同 ID 的已有 artifact
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
        # P1-5/M5: 分页入参硬化
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
                # L-9: 漏删 artifact_shares 留孤儿行——补上
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
            # 先 commit 再删盘：commit 失败则不留孤儿行指向缺失文件 (P2-6)
            # E5: 不用 ignore_errors=True 静默吞错；onerror 记日志，让磁盘/权限问题可见
            for content_dir in purged_dirs:
                def _on_rm_err(func, fpath, exc_info):
                    logger.warning("Failed to purge content %s: %s", fpath, exc_info[1])
                shutil.rmtree(content_dir, onerror=_on_rm_err)
        logger.info(
            "Purged %d expired artifacts (retention=%d days)", count, retention_days
        )
        return count

    # ── versions ───────────────────────────────────────────────

    def save_version(self, version: ArtifactVersion) -> None:
        # 运维4: 写前磁盘预检，超阈值拒绝写
        self.ensure_disk_available()
        # C-11/C-12/H2: 文件写事务外临时路径，提交后 rename 到最终路径。
        # 重试改 version_num 时只重算最终路径（tmp 文件不变），避免覆盖历史版本与串号，
        # 旧版本号不再写盘（tmp 单文件），彻底消除孤儿。
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
                    # H2: 提交成功后 rename 临时文件到最终路径
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
                    # R9: 写入后按版本上限淘汰最旧非快照版本
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
                        # 重试耗尽——清理 tmp 文件，不留孤儿
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
            # 循环正常结束（理论不会到这）——清理 tmp
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
        # R9: 超过 max_versions_per_artifact 时淘汰最旧非快照版本（含其磁盘文件）。
        # 必须在 _write_lock 内调。快照（snapshot_type='named'）不计入上限、不被淘汰。
        # 当前版本（artifacts.current_version）绝不被淘汰，避免误删活版本。
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
        # 可淘汰数 = 非快照版本数 - max（已超 max 才淘汰）
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
            # E11/R9: DELETE 隐式开事务，显式提交，否则下次 BEGIN IMMEDIATE 报
            # "cannot start a transaction within a transaction"
            self._conn.commit()
            logger.info("Version limit enforced: %s evicted %d old version(s)", artifact_id, purged)

    def gc_orphan_files(self) -> int:
        # H2: 孤儿文件 GC——扫描 content_dir，删无 DB 行指向的文件 + 残留 .tmp_ 文件。
        # DB 行指向的 content_path 集合与磁盘文件集合做差，多余即孤儿。
        # 返回清理的文件数。调用方应在低峰期或周期触发。
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
                # .tmp_ 前缀=两阶段写残留（rename 失败/进程崩溃），直接清
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

    def create_version_atomic(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        content_hash: str,
        summary: str | None,
        expected_content_hash: str | None = None,
    ) -> int:
        # 运维4: 写前磁盘预检，超阈值拒绝写
        self.ensure_disk_available()
        # P0-5/H9: 内容字节上限校验
        _enforce_content_limit(version.content, self.max_content_bytes)
        # C-8: 乐观锁校验+写入收进单事务，BEGIN IMMEDIATE 序列化并发写，
        # 事务内重读 current_version/content_hash 校验，分配 version_num，
        # 写 version 行并更新 artifact.current_version/content_hash，原子提交。
        # H2: 文件写移出事务——先写临时文件（事务外），事务提交后 rename 到最终路径。
        # 回滚则删临时文件，不留孤儿。返回实际分配的 version_num。
        with self._write_lock:
            # H2 阶段1：文件写事务外临时路径（version_num 未知，先写 tmp）
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
                    # P0-2: 乐观锁冲突映射 ConflictError(-32002, 可重试)，而非 -32602。
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
                    # H2 阶段2：事务内只记最终路径（实际 rename 在提交后）
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
                # H2 阶段3：提交成功后 rename 临时文件到最终路径
                if tmp_path is not None and content_path is not None:
                    try:
                        Path(tmp_path).rename(content_path)
                    except OSError as e:
                        # rename 失败：DB 行已指向 content_path 但文件在 tmp_path。
                        # 不回滚 DB（已提交）；gc_orphan_files 会清理，且下次读该版本
                        # content_path 会 FileNotFoundError——记 ERROR 便于定位
                        logger.error(
                            "Failed to rename tmp content %s -> %s: %s; "
                            "DB row committed, orphan/tmp file left for GC",
                            tmp_path, content_path, e,
                        )
                # R9: 提交后按版本上限淘汰最旧非快照版本（独立于事务，淘汰失败不影响版本写入）
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
                # H2: 回滚则删临时文件，不留孤儿
                if tmp_path is not None:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning("Failed to clean tmp content %s: %s", tmp_path, e)
                # P0-3/H8: SQLite OperationalError 按消息映射为可重试 ConflictError / ResourceLimitError
                if isinstance(exc, sqlite3.Error):
                    raise _translate_sqlite_error(exc) from exc
                raise

    def _hydrate_version_content(self, artifact_id: str, ver: ArtifactVersion) -> None:
        if not ver.content and ver.content_path:
            try:
                ver.content = self._read_content_file(ver.content_path)
            except FileNotFoundError:
                # L-16: 内容文件缺失是数据损坏，不伪造空版本——上抛让引擎/调用方决定
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
        # P-3: 分页 + 可选跳过内容文件读，避免无界扫描全量入内存
        # P1-5: 统一走 _clamp_pagination——page>=1、page_size<=500、非 int 兜底默认
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
        # P1-5: 统一走 _clamp_pagination——page>=1、page_size<=500、非 int 兜底默认
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
                """INSERT INTO artifact_shares (share_id, artifact_id, created_by, created_at, expires_at, revoked, access_count, max_accesses, last_access_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(share_id) DO UPDATE SET
                     artifact_id=excluded.artifact_id,
                     created_by=excluded.created_by,
                     expires_at=excluded.expires_at,
                     revoked=excluded.revoked,
                     access_count=excluded.access_count,
                     max_accesses=excluded.max_accesses,
                     last_access_at=excluded.last_access_at""",
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
            # L-12: 同步置空 artifacts.share_id，避免唯一索引占位阻塞新分享
            self._conn.execute(
                "UPDATE artifacts SET share_id = NULL WHERE share_id = ?",
                (share_id,),
            )
            self._conn.commit()
        ok = cur.rowcount > 0
        logger.info("Revoked share %s ok=%s", share_id, ok)
        return ok

    def increment_share_access(self, share_id: str, delta: int = 1) -> None:
        # E1: last_access_at 写 float epoch，与列定义 REAL 一致
        # R2: delta 支持批量刷盘（公开 GET 内存缓冲聚合后一次写）
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
        # P1-2/H5: 原子 check-and-increment——单条条件 UPDATE 在 _write_lock 下原子完成
        # "未达上限才 +1"，消除 get_shared_artifact 里先读 access_count 再 increment 的 TOCTOU。
        # 返回 True=已增计数（允许），False=已达上限或 share 不存在（拒绝）。
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
        # L-13: 收集后代 folder，避免子文件夹留悬挂 parent_id 指向已删 folder
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
            # nullify 直接/间接后代 artifact 的 folder_id
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
        # L-7: 按 scope 查询；scope=None 时回退全局（兼容无作用域调用）
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
        # L-7: list_tags 按 scope 过滤；不传则返回全部（向后兼容）
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
        # A-8: created_at 统一 REAL epoch；旧 model 传 ISO 串也转 epoch
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
        # A-8: since_ts 兼容 REAL epoch 与 ISO 串，统一转 REAL 与 REAL 列比较
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
        # P1-5/M5: 分页入参硬化
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
        # C-10: 扩展名取自用户输入 name，必须限定安全字符集，拒绝路径穿越片段
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
        # P-3: 分页避免无界扫描
        # P1-5: 统一走 _clamp_pagination——page>=1、page_size<=500、非 int 兜底默认
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
        # P1-4: 先停 checkpoint 后台线程，再做最终 TRUNCATE checkpoint，确保停机后 -wal 清空。
        if self._checkpoint_thread is not None:
            self._checkpoint_stop.set()
            self._checkpoint_thread.join(timeout=5)
            self._checkpoint_thread = None
        try:
            self._run_checkpoint("TRUNCATE")
        except Exception as e:
            logger.warning("WAL final TRUNCATE checkpoint failed: %s", e)
        # E7: 关连接池里的只读连接（非阻塞取出，取不到说明正被借出，跳过）
        while True:
            try:
                rc = self._read_pool.get_nowait()
                rc.close()
            except queue.Empty:
                break
        self._conn.close()
        logger.info("SQLiteStorage closed")

    def health_check(self) -> bool:
        # 运维5: /readyz 探针依赖——SELECT 1 验证 DB 连接可用，content_dir 可写。
        # 不做实质写入，仅探活。失败返回 False 不抛（调用方按状态码处理）。
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

    _SCHEMA_VERSION = 2

    def _migrate_schema_meta(self) -> None:
        # A-6: schema_meta 记录 schema_version，支持迁移框架与版本检测
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value INTEGER)"
        )
        # R5: applied_migrations 记录已跑过的迁移名，支持按版本跳过
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
                # A-6: DB 版本高于代码——拒绝启动，避免未知 schema 静默损坏
                raise RuntimeError(
                    f"DB schema_version {db_ver} > code {self._SCHEMA_VERSION}; "
                    "downgrade unsupported"
                )

    # R5: 迁移注册表——顺序即执行顺序。新增迁移追加到末尾，勿改既有顺序。
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
    ]

    def _run_migrations_gated(self) -> None:
        # R5: 仅跑未记入 applied_migrations 的迁移，跑完记录。避免每次启动全量重检查。
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


    def _migrate_event_timestamps(self) -> None:
        # A-8: artifact_events.created_at 统一 REAL epoch。旧 ISO 串转 epoch。
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

    def _migrate_share_max_accesses_and_timestamps(self) -> None:
        # E1/E2: artifact_shares 加 max_accesses 列；created_at/last_access_at
        # 由 TEXT ISO 迁移为 REAL epoch（schema_version 2）。
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
        # folder created_at 同样迁移
        self._coerce_text_ts_to_real("artifact_folders", "folder_id", ["created_at"])

    def _coerce_text_ts_to_real(
        self, table: str, id_col: str, ts_cols: list[str]
    ) -> None:
        # E1: 把 TEXT(ISO 串) 行的 created_at 类列转 REAL epoch；已是 float 的跳过。
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
        # L-7: artifact_tags 加 scope 列，name 唯一约束改为 (name, scope) 复合唯一
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(artifact_tags)").fetchall()
        }
        if "scope" not in cols:
            self._conn.execute("ALTER TABLE artifact_tags ADD COLUMN scope TEXT")
            self._conn.commit()
            logger.info("Added scope column to artifact_tags")
        # 旧库 name 是 UNIQUE 单列约束，与新增 UNIQUE(name, scope) 冲突——重建索引。
        # SQLite 无法直接 ALTER 约束，删除自动索引名（sqlite_autoindex_*）需重建表。
        # 这里检测并重建：若存在单列 name 唯一索引则重建表为复合唯一。
        idx_rows = self._conn.execute(
            "PRAGMA index_list(artifact_tags)"
        ).fetchall()
        needs_rebuild = False
        for ir in idx_rows:
            # origin='u' = UNIQUE constraint autoindex；查其列
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
