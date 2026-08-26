import asyncio
import difflib
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import ClassVar

from fusion_artifacts_engine.auto_identifier import should_create_artifact
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.models import (
    Artifact,
    ArtifactEvent,
    ArtifactFolder,
    ArtifactShare,
    ArtifactTag,
    ArtifactVersion,
    infer_kind,
)
from fusion_artifacts_engine.ref_parser import generate_ref_text
from fusion_artifacts_engine.rpc.errors import ConflictError, NotFoundError, ResourceLimitError
from fusion_artifacts_engine.rpc.event_bus import EventBus
from fusion_artifacts_engine.section_index import build_sections_with_tokens as _build_secs
from fusion_artifacts_engine.section_index import delete_section as _delete_sec
from fusion_artifacts_engine.section_index import (
    extract_sections,
    normalize_anchor,
)
from fusion_artifacts_engine.section_index import find_section_bounds as _find_bounds
from fusion_artifacts_engine.section_index import replace_section as _replace_sec
from fusion_artifacts_engine.share import ShareManager
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage
from fusion_artifacts_engine.token_budget import (
    check_safety as _check_safety_fn,
)
from fusion_artifacts_engine.token_budget import (
    context_budget as _context_budget_fn,
)
from fusion_artifacts_engine.token_budget import (
    inject as _inject_fn,
)
from fusion_artifacts_engine.token_counter import count_tokens
from fusion_artifacts_engine.utils import generate_artifact_id

logger = logging.getLogger(__name__)

_SUMMARY_MAX_LEN = 200


def _truncate_summary(content: str) -> str:
    return content[:_SUMMARY_MAX_LEN].replace("\n", " ").strip()


def _auto_changelog(old_content: str, new_content: str) -> str:
    # 基于真实行级 diff 统计增删，非仅行数算术 (P2-11)。
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    parts = []
    if added:
        parts.append(f"+{added} lines")
    if removed:
        parts.append(f"-{removed} lines")
    if not parts:
        if old_content != new_content:
            parts.append("content modified")
        else:
            parts.append("no change")
    elif added == removed and old_content != new_content:
        # 同行数内容替换：diff 报 +n/-n，补充 content modified 标记替换语义 (P2-11)。
        parts.append("content modified")
    return ", ".join(parts)


def _size_bytes(content: str) -> int:
    return len(content.encode("utf-8"))


# H3/H7: _sanitize_html / _render_share_html / _SHARE_DOC_CSP / _html_escape 已抽到 render.py。
# _sanitize_html 改用 lxml Cleaner（真实 HTML 解析树），正则仅作 lxml 缺失时降级。


class ArtifactEngine:
    def __init__(self, config: ArtifactEngineConfig | None = None):
        self.config = config or ArtifactEngineConfig()
        self.storage = SQLiteStorage(
            db_path=self.config.db_path,
            content_dir=self.config.content_dir,
            small_content_limit=self.config.small_content_limit,
            max_versions_per_artifact=self.config.max_versions_per_artifact,
            disk_space_warning_pct=self.config.disk_space_warning_pct,
            max_content_bytes=self.config.max_content_bytes,
            max_metadata_bytes=self.config.max_metadata_bytes,
            wal_checkpoint_interval=self.config.wal_checkpoint_interval, metadata_indexed_keys=self.config.metadata_indexed_keys,
        )
        # A-1/R6: _watchers 仅作注册簿记录（audit-only registry），无主动投递路径。
        # 变更通知实际走 EventBus → SSE（engine.event_bus.publish）。_watchers 不参与推送，
        # 仅为 watch RPC 保留「谁注册过」的记录。未来要加推送应基于 EventBus 扩展，
        # 勿在 _watchers 上补投递逻辑（会产生与 SSE 的重复推送）。
        # ThreadingMixIn 多线程改写，加锁防 dict changed size during iteration 竞态。
        self._watchers: dict[str, list[str]] = {}
        self._watchers_lock = threading.Lock()
        # A-2: EventBus 注入 engine 实例，避免模块级单例跨 engine 串流
        self.event_bus = EventBus()
        # H7: share 访问控制 + R2 内存缓冲抽到 ShareManager
        self.share_mgr = ShareManager(self)
        logger.info(
            "ArtifactEngine initialized: storage_root=%s", self.config.storage_root
        )

    def _publish_disk_alarm(self, message: str) -> None:
        # 运维4: 磁盘水位告警事件，经 EventBus 投递给 SSE 订阅者（运维侧监听）
        try:
            self.event_bus.publish(
                "disk_full_alarm",
                {"kind": "system.alarm", "message": message, "severity": "critical"},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to publish disk alarm event")
        logger.critical("DISK ALARM: %s", message)

    def shutdown_share_buffer(self) -> None:
        # H7: 委托 ShareManager.shutdown（R2 最终 flush 防计数丢失）
        self.share_mgr.shutdown()


    async def create_artifact(
        self,
        session_id: str,
        name: str,
        artifact_type: str,
        content: str,
        summary: str = "",
        change_log: str = "Initial version",
        kind: str | None = None,
        project_id: str | None = None,
        metadata: dict | None = None,
        owner_user_id: str | None = None, ownership_type: str | None = None,
    ) -> tuple[Artifact, ArtifactVersion, str]:
        artifact_id = generate_artifact_id(self.config.artifact_id_prefix)
        now = time.time()
        if not summary:
            summary = _truncate_summary(content)
        if kind is None:
            kind = infer_kind(artifact_type)
        artifact = Artifact(
            id=artifact_id,
            session_id=session_id,
            name=name,
            type=artifact_type,
            kind=kind,
            project_id=project_id,
            metadata=metadata, current_version=1,
            summary=summary,
            created_at=now, updated_at=now,
            owner_user_id=owner_user_id, ownership_type=ownership_type if ownership_type is not None else "free",
        )
        size = _size_bytes(content)
        sections = extract_sections(content, artifact_type)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=1,
            content=content,
            size_bytes=size,
            token_count=count_tokens(content),
            section_index=json.dumps(sections) if sections else None,
            change_log=change_log,
            created_at=now,
        )
        # H5: 阻塞 storage 写卸到线程池，不独占事件循环
        # 运维4: 写前磁盘预检可能抛 ResourceLimitError，发告警事件后透传
        try:
            await asyncio.to_thread(self.storage.save_artifact_and_version, artifact, version)
        except ResourceLimitError as e:
            self._publish_disk_alarm(str(e))
            raise
        ref_text = generate_ref_text(artifact_id, name, artifact_type, 1, size, summary)
        logger.info(
            "Created artifact: %s name=%s size=%d tokens=%d sections=%d",
            artifact_id,
            name,
            size,
            version.token_count,
            len(sections),
        )
        return artifact, version, ref_text

    async def create_external_artifact(
        self,
        source_module: str,
        workspace_id: str,
        name: str,
        artifact_type: str,
        content: str,
        workflow_run_id: str | None = None,
        summary: str = "",
        kind: str | None = None,
        project_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[Artifact, ArtifactVersion, str]:
        session_id = f"ext_{source_module}_{workspace_id}"
        artifact_id = generate_artifact_id(self.config.artifact_id_prefix)
        now = time.time()
        if not summary:
            summary = _truncate_summary(content)
        if kind is None:
            kind = infer_kind(artifact_type)
        artifact = Artifact(
            id=artifact_id,
            session_id=session_id,
            name=name,
            type=artifact_type,
            kind=kind,
            project_id=project_id,
            metadata=metadata,
            current_version=1,
            summary=summary,
            created_at=now,
            updated_at=now,
            source_module=source_module,
            workspace_id=workspace_id,
            workflow_run_id=workflow_run_id,
        )
        size = _size_bytes(content)
        sections = extract_sections(content, artifact_type)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=1,
            content=content,
            size_bytes=size,
            token_count=count_tokens(content),
            section_index=json.dumps(sections) if sections else None,
            change_log="Created by external module",
            created_at=now,
        )
        # H5: 阻塞 storage 写卸到线程池
        # 运维4: 写前磁盘预检可能抛 ResourceLimitError，发告警事件后透传
        try:
            await asyncio.to_thread(self.storage.save_artifact_and_version, artifact, version)
        except ResourceLimitError as e:
            self._publish_disk_alarm(str(e))
            raise
        if project_id:
            await asyncio.to_thread(self.storage.move_to_project_kb, artifact_id, project_id)
            artifact = self.storage.get_artifact(artifact_id)
            logger.info(
                "Auto-archived external artifact %s to project %s",
                artifact_id,
                project_id,
            )
        ref_text = generate_ref_text(artifact_id, name, artifact_type, 1, size, summary)
        logger.info(
            "Created external artifact: %s source=%s ws=%s size=%d",
            artifact_id,
            source_module,
            workspace_id,
            size,
        )
        return artifact, version, ref_text

    def list_by_source(
        self,
        source_module: str,
        workspace_id: str | None = None,
        workflow_run_id: str | None = None,
        page: int = 1,
        page_size: int = 200,
    ) -> list[Artifact]:
        return self.storage.list_by_source(
            source_module, workspace_id, workflow_run_id, page=page, page_size=page_size
        )

    def get_artifact(
        self, artifact_id: str, project_id: str | None = None
    ) -> Artifact | None:
        return self.storage.get_artifact(artifact_id, project_id)

    def list_artifacts(
        self,
        session_id: str,
        include_deleted: bool = False,
        project_id: str | None = None,
        metadata_filter: dict | None = None,
    ) -> list[Artifact]:
        return self.storage.list_artifacts(
            session_id, include_deleted, project_id, metadata_filter
        )

    def delete_artifact(
        self,
        artifact_id: str,
        soft_delete: bool = True,
        project_id: str | None = None,
    ) -> bool:
        return self.storage.delete_artifact(artifact_id, soft_delete, project_id)

    def get_version_content(
        self, artifact_id: str, version: int | None = None
    ) -> ArtifactVersion | None:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            return None
        ver = version if version is not None else artifact.current_version
        try:
            result = self.storage.get_version(artifact_id, ver)
        except FileNotFoundError as e:
            logger.error("Content file missing for %s v%s: %s", artifact_id, ver, e)
            return None
        # section_index 保持原始 JSON 字符串（与 list_versions 一致），不在原地改类型，
        # 避免 str/dict 漂移；调用方需 dict 时自行 json.loads (P2-8)。
        return result

    def list_versions(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]:
        return self.storage.list_versions(
            artifact_id, page=page, page_size=page_size, include_content=include_content
        )

    async def rollback_version(
        self,
        artifact_id: str,
        target_version: int,
    ) -> tuple[ArtifactVersion, str]:
        # H5: 阻塞 storage 读卸到线程池
        try:
            target = await asyncio.to_thread(
                self.storage.get_version, artifact_id, target_version
            )
        except FileNotFoundError as e:
            logger.error(
                "Content file missing for %s v%s: %s", artifact_id, target_version, e
            )
            raise ValueError(
                f"Version content not found: {artifact_id} v{target_version}"
            ) from e
        if target is None:
            raise ValueError(f"Version not found: {artifact_id} v{target_version}")
        version, ref_text = await self.create_version(
            artifact_id,
            target.content,
            f"Rollback to v{target_version}",
            snapshot_type="rollback",
            parent_version=target_version,
        )
        logger.info(
            "Rolled back %s to v%s, new v%s (rollback-tagged, parent=%s)",
            artifact_id,
            target_version,
            version.version_num,
            target_version,
        )
        return version, ref_text

    def should_create_artifact(self, content: str, content_type: str = "text") -> bool:
        return should_create_artifact(
            content,
            content_type,
            self.config.auto_create_threshold_lines,
            self.config.auto_create_threshold_chars,
        )

    _LANG_EXT: ClassVar[dict[str, str]] = {
        "python": "py",
        "javascript": "js",
        "typescript": "ts",
        "html": "html",
        "css": "css",
        "markdown": "md",
        "json": "json",
        "yaml": "yaml",
        "rust": "rs",
        "go": "go",
        "java": "java",
        "c": "c",
        "cpp": "cpp",
    }

    _EXT_LANG: ClassVar[dict[str, str]] = {v: k for k, v in _LANG_EXT.items()}

    def export_code(self, artifact_id: str, language: str = "") -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        version = self.get_version_content(artifact_id)
        if version is None:
            raise ValueError(f"No content for artifact: {artifact_id}")
        ext = self._LANG_EXT.get(language, "")
        if not ext:
            name = artifact.name.lower()
            if "." in name:
                ext = name.rsplit(".", 1)[-1]
                language = self._EXT_LANG.get(ext, language)
            else:
                ext = self._LANG_EXT.get(artifact.type, "txt")
        code = version.content
        return {
            "code": code,
            "language": language,
            "ext": ext,
            "artifact_id": artifact_id,
            "version": version.version_num,
            "name": artifact.name,
        }

    async def import_code(
        self,
        session_id: str,
        code: str,
        language: str = "",
        name: str = "",
        metadata: dict | None = None,
    ) -> tuple[Artifact, ArtifactVersion, str]:
        meta = metadata or {}
        artifact_type = "code"
        ext = self._LANG_EXT.get(language, "")
        if language in ("html",) or ext == "html":
            artifact_type = "html"
        elif language in ("javascript", "typescript") and ext in ("jsx", "tsx"):
            artifact_type = "react"
        if not name:
            name = meta.get("filename", f"imported.{ext}" if ext else "imported.txt")
        summary = meta.get("summary", _truncate_summary(code))
        return await self.create_artifact(
            session_id=session_id,
            name=name,
            artifact_type=artifact_type,
            content=code,
            summary=summary,
        )

    def register_watcher(self, artifact_id: str, watcher_id: str) -> None:
        with self._watchers_lock:
            if artifact_id not in self._watchers:
                self._watchers[artifact_id] = []
            if watcher_id not in self._watchers[artifact_id]:
                self._watchers[artifact_id].append(watcher_id)
                logger.info(
                    "Watcher registered: %s for artifact %s", watcher_id, artifact_id
                )

    def unregister_watcher(self, artifact_id: str, watcher_id: str) -> None:
        with self._watchers_lock:
            if artifact_id in self._watchers:
                self._watchers[artifact_id] = [
                    w for w in self._watchers[artifact_id] if w != watcher_id
                ]
                if not self._watchers[artifact_id]:
                    del self._watchers[artifact_id]
                logger.info(
                    "Watcher unregistered: %s for artifact %s", watcher_id, artifact_id
                )

    def get_watch_events(self, artifact_id: str, since_version: int = 0) -> list[dict]:
        versions = self.storage.list_versions(artifact_id)
        events = []
        for v in versions:
            if v.version_num > since_version:
                events.append(
                    {
                        "artifact_id": artifact_id,
                        "version": v.version_num,
                        "change_log": v.change_log,
                        "created_at": v.created_at,
                    }
                )
        logger.debug(
            "Watch events for %s since v%d: %d events",
            artifact_id,
            since_version,
            len(events),
        )
        return events

    def get_artifact_raw_content(self, artifact_id: str) -> dict | None:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            return None
        version = self.get_version_content(artifact_id)
        if version is None:
            return None
        content_type_map = {
            "html": "text/html",
            "react": "text/html",
            "markdown": "text/markdown",
            "code": "text/plain",
            "data": "application/json",
        }
        ct = content_type_map.get(artifact.type, "text/plain")
        return {
            "content": version.content,
            "content_type": ct,
            "artifact_id": artifact_id,
            "version": version.version_num,
        }

    # ── P1: lifecycle + global repo ────────────────────────────

    def rename_artifact(self, artifact_id: str, new_name: str) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.rename_artifact(artifact_id, new_name)
        logger.info("Renamed artifact %s -> %s ok=%s", artifact_id, new_name, ok)
        return ok

    def star_artifact(self, artifact_id: str, starred: bool = True) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.star_artifact(artifact_id, starred)
        logger.info("Star artifact %s starred=%s ok=%s", artifact_id, starred, ok)
        return ok

    def pin_artifact(
        self, artifact_id: str, chat_id: str | None = None, pinned: bool = True
    ) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.pin_artifact(artifact_id, chat_id, pinned)
        logger.info(
            "Pin artifact %s pinned=%s chat=%s ok=%s", artifact_id, pinned, chat_id, ok
        )
        return ok

    def duplicate_artifact(
        self, artifact_id: str, new_name: str | None = None
    ) -> Artifact | None:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        new_id = generate_artifact_id(self.config.artifact_id_prefix)
        dup = self.storage.duplicate_artifact(artifact_id, new_id, new_name)
        logger.info("Duplicated artifact %s -> %s", artifact_id, new_id)
        return dup

    def list_all_artifacts(
        self,
        filters: dict | None = None,
        sort: str = "updated_at",
        page: int = 1,
        page_size: int = 20, cursor: str | None = None,
    ) -> tuple[list[Artifact], int]:
        artifacts, total = self.storage.list_all_artifacts(
            filters, sort, page, page_size, cursor
        )
        logger.info("list_all_artifacts: %d/%d page=%s", len(artifacts), total, page)
        return artifacts, total

    # ── P1: recycle bin ────────────────────────────────────────

    def list_recycle(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[Artifact], int]:
        artifacts, total = self.storage.list_recycle(page, page_size)
        logger.info("list_recycle: %d/%d page=%s", len(artifacts), total, page)
        return artifacts, total

    def restore_artifact(self, artifact_id: str) -> bool:
        ok = self.storage.restore_artifact(artifact_id)
        logger.info("Restored artifact %s ok=%s", artifact_id, ok)
        return ok

    def purge_expired(self) -> int:
        retention_days = getattr(self.config, "recycle_retention_days", 7)
        count = self.storage.purge_expired(retention_days)
        logger.info(
            "Purged %d expired artifacts (retention=%d days)", count, retention_days
        )
        return count

    # ── P1: optimistic lock ────────────────────────────────────

    async def create_version(
        self,
        artifact_id: str,
        content: str,
        change_log: str = "",
        source: str = "manual",
        expected_content_hash: str | None = None,
        snapshot_type: str = "auto",
        parent_version: int | None = None,
    ) -> tuple[ArtifactVersion, str]:
        # H5: 阻塞 storage 读卸到线程池
        artifact = await asyncio.to_thread(self.storage.get_artifact, artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        if not change_log:
            old = await asyncio.to_thread(
                self.storage.get_version, artifact_id, artifact.current_version
            )
            change_log = _auto_changelog(old.content if old else "", content)
        now = time.time()
        size = _size_bytes(content)
        sections = extract_sections(content, artifact.type)
        # C-8: version_num 由原子存储方法在事务内分配，这里用 0 占位
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=0,
            content=content,
            size_bytes=size,
            token_count=count_tokens(content),
            section_index=json.dumps(sections) if sections else None,
            change_log=change_log,
            source=source,
            created_at=now,
            snapshot_type=snapshot_type,
            parent_version=parent_version,
        )
        # L-8: rollback 的 content_hash 与历史版本内容相同会碰撞——乐观锁调用方
        # 无法区分"已回滚到 vN"与"仍在 vN"。回滚版本 hash 计算前加标记前缀，
        # 使其与原始版本 hash 不同，消除锁歧义。
        hash_input = content
        if snapshot_type == "rollback":
            hash_input = f"rollback:{parent_version}:{content}"
        content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:16]
        summary = _truncate_summary(content) if not artifact.summary else None
        # C-8: 校验+分配+写入原子化，BEGIN IMMEDIATE 序列化并发写
        # H5: 原子写卸到线程池
        # 运维4: 写前磁盘预检可能抛 ResourceLimitError，发告警事件后透传
        try:
            new_version = await asyncio.to_thread(
                self.storage.create_version_atomic,
                artifact,
                version,
                content_hash,
                summary,
                expected_content_hash=expected_content_hash,
            )
        except ResourceLimitError as e:
            self._publish_disk_alarm(str(e))
            raise
        ref_text = generate_ref_text(
            artifact_id,
            artifact.name,
            artifact.type,
            new_version,
            size,
            artifact.summary,
        )
        logger.info(
            "Created version: %s v%s size=%d tokens=%d sections=%d hash=%s",
            artifact_id,
            new_version,
            size,
            version.token_count,
            len(sections),
            content_hash,
        )
        return version, ref_text

    # ── P1: share ──────────────────────────────────────────────

    def create_share(
        self,
        artifact_id: str,
        created_by: str | None = None,
        expires_at: str | None = None,
        max_accesses: int | None = None,
    ) -> ArtifactShare:
        # H7: 委托 ShareManager
        return self.share_mgr.create_share(
            artifact_id, created_by, expires_at, max_accesses
        )

    def get_shared_artifact(self, share_id: str) -> dict | None:
        # H7: 委托 ShareManager
        return self.share_mgr.get_shared_artifact(share_id)

    def revoke_share(self, share_id: str) -> bool:
        # H7: 委托 ShareManager
        return self.share_mgr.revoke_share(share_id)

    def get_public_share(self, share_id: str) -> dict:
        # H7: 委托 ShareManager
        return self.share_mgr.get_public_share(share_id)

    # ── P2: snapshots ──────────────────────────────────────────

    async def create_snapshot(
        self,
        artifact_id: str,
        label: str | None = None,
        author: str | None = None,
    ) -> ArtifactVersion:
        # H5: 阻塞 storage 读写卸到线程池
        artifact = await asyncio.to_thread(self.storage.get_artifact, artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        current = await asyncio.to_thread(
            self.storage.get_version, artifact_id, artifact.current_version
        )
        if current is None:
            raise ValueError(f"No current version for artifact: {artifact_id}")
        new_ver_num = self.storage.next_version_num(artifact_id)
        now = time.time()
        snapshot_content = current.content
        snapshot_size = _size_bytes(snapshot_content)
        snapshot_sections = extract_sections(snapshot_content, artifact.type)
        snapshot = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=new_ver_num,
            content=snapshot_content,
            size_bytes=snapshot_size,
            token_count=count_tokens(snapshot_content),
            section_index=json.dumps(snapshot_sections) if snapshot_sections else None,
            change_log=f"Snapshot: {label}" if label else "Snapshot",
            source="manual",
            created_at=now,
            snapshot_type="named",
            snapshot_label=label,
            author=author,
            parent_version=artifact.current_version,
        )
        await asyncio.to_thread(self.storage.save_version, snapshot)
        artifact.current_version = new_ver_num
        artifact.updated_at = now
        await asyncio.to_thread(self.storage.save_artifact, artifact)
        logger.info(
            "Created snapshot: %s v%d label=%s", artifact_id, new_ver_num, label
        )
        return snapshot

    def list_snapshots(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]:
        return self.storage.list_snapshots(
            artifact_id, page=page, page_size=page_size, include_content=include_content
        )

    # ── P2: folders ────────────────────────────────────────────

    def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
        project_id: str | None = None,
    ) -> ArtifactFolder:
        import uuid

        folder_id = f"fld_{uuid.uuid4().hex[:12]}"
        # E1: created_at 用 float epoch，与 Artifact/Version 一致
        folder = ArtifactFolder(
            folder_id=folder_id,
            name=name,
            parent_id=parent_id,
            project_id=project_id,
            created_at=time.time(),
        )
        self.storage.save_folder(folder)
        logger.info("Created folder: %s name=%s", folder_id, name)
        return folder

    def list_folders(self, project_id: str | None = None) -> list[ArtifactFolder]:
        return self.storage.list_folders(project_id)

    def rename_folder(self, folder_id: str, new_name: str) -> bool:
        ok = self.storage.rename_folder(folder_id, new_name)
        logger.info("Renamed folder %s -> %s ok=%s", folder_id, new_name, ok)
        return ok

    def delete_folder(self, folder_id: str) -> bool:
        ok = self.storage.delete_folder(folder_id)
        logger.info("Deleted folder %s ok=%s", folder_id, ok)
        return ok

    def move_to_folder(self, artifact_id: str, folder_id: str | None = None) -> bool:
        ok = self.storage.move_to_folder(artifact_id, folder_id)
        logger.info("Moved artifact %s to folder %s ok=%s", artifact_id, folder_id, ok)
        return ok

    # ── P4: tags ───────────────────────────────────────────────

    @staticmethod
    def _tag_scope(artifact: "Artifact") -> str | None:
        # L-7: tag 作用域优先 session_id，其次 project_id；都无则 None（全局）
        if artifact.session_id:
            return artifact.session_id
        if artifact.project_id:
            return artifact.project_id
        return None

    def add_tag(
        self, artifact_id: str, tag_name: str, color: str | None = None
    ) -> ArtifactTag:
        import uuid

        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        scope = self._tag_scope(artifact)
        existing = self.storage.get_tag_by_name(tag_name, scope)
        if existing:
            tag = existing
        else:
            tag_id = f"tag_{uuid.uuid4().hex[:12]}"
            tag = ArtifactTag(tag_id=tag_id, name=tag_name, color=color, scope=scope)
            self.storage.save_tag(tag)
            logger.info("Created tag: %s name=%s scope=%s", tag_id, tag_name, scope)
        self.storage.add_artifact_tag(artifact_id, tag.tag_id)
        logger.info("Added tag %s to artifact %s", tag.tag_id, artifact_id)
        return tag

    def remove_tag(self, artifact_id: str, tag_name: str) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        scope = self._tag_scope(artifact) if artifact else None
        tag = self.storage.get_tag_by_name(tag_name, scope)
        if tag is None:
            logger.warning("Tag not found: %s scope=%s", tag_name, scope)
            return False
        ok = self.storage.remove_artifact_tag(artifact_id, tag.tag_id)
        logger.info("Removed tag %s from artifact %s ok=%s", tag_name, artifact_id, ok)
        return ok

    def list_tags(self, scope: str | None = None) -> list[ArtifactTag]:
        return self.storage.list_tags(scope)

    def list_artifact_tags(self, artifact_id: str) -> list[ArtifactTag]:
        return self.storage.list_artifact_tags(artifact_id)

    # ── P4: events ─────────────────────────────────────────────

    def emit_event(
        self,
        event_type: str,
        artifact_id: str | None = None,
        session_id: str | None = None,
        payload: dict | None = None,
    ) -> ArtifactEvent:
        import time as _time
        import uuid

        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now_ts = _time.time()
        # P0-5/H6: emit_event payload 字节上限校验，超限拒绝写入防事件总线放大
        if payload is not None:
            import json as _json

            payload_size = len(_json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            limit = self.config.max_event_payload_bytes
            if limit > 0 and payload_size > limit:
                logger.warning(
                    "Event payload rejected: %d bytes > max_event_payload_bytes %d (type=%s)",
                    payload_size, limit, event_type,
                )
                raise ResourceLimitError(
                    f"Event payload too large: {payload_size} bytes exceeds "
                    f"max_event_payload_bytes {limit}"
                )
        event = ArtifactEvent(
            event_id=event_id,
            artifact_id=artifact_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            created_at=now_ts,
        )
        self.storage.save_event(event)
        logger.info(
            "Emitted event: %s type=%s artifact=%s", event_id, event_type, artifact_id
        )
        return event

    def list_events(
        self,
        artifact_id: str | None = None,
        session_id: str | None = None,
        since_ts: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ArtifactEvent], int]:
        events, total = self.storage.list_events(
            artifact_id, session_id, since_ts, page, page_size
        )
        logger.info("list_events: %d/%d page=%s", len(events), total, page)
        return events, total

    # ── P3: project KB ─────────────────────────────────────────

    def move_to_project_kb(self, artifact_id: str, project_id: str) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.move_to_project_kb(artifact_id, project_id)
        logger.info(
            "Moved artifact %s to project_kb project=%s ok=%s",
            artifact_id,
            project_id,
            ok,
        )
        return ok

    # ── AE-1: patch_artifact ───────────────────────────────────

    async def patch_artifact(
        self,
        artifact_id: str,
        operation: str,
        anchor: str = "",
        content: str = "",
        expected_version: int | None = None,
    ) -> tuple[ArtifactVersion, dict]:
        # H5: 阻塞 storage 读卸到线程池
        artifact = await asyncio.to_thread(self.storage.get_artifact, artifact_id)
        if artifact is None:
            raise NotFoundError(f"Artifact not found: {artifact_id}")
        if (
            expected_version is not None
            and artifact.current_version != expected_version
        ):
            # P0-2: 乐观锁冲突映射 ConflictError(-32002, 可重试)。
            raise ConflictError(
                f"Optimistic lock failed: expected version {expected_version}, "
                f"got {artifact.current_version}"
            )
        current = await asyncio.to_thread(
            self.storage.get_version, artifact_id, artifact.current_version
        )
        if current is None:
            raise NotFoundError(f"No current version for artifact: {artifact_id}")
        old_content = current.content
        old_tokens = current.token_count

        if operation == "replace_section":
            if not anchor:
                raise ValueError("anchor is required for replace_section")
            # H7: section patch 抽到 section_index 模块
            new_content, replaced_content = _replace_sec(
                old_content, anchor, content, artifact.type
            )
        elif operation == "append":
            new_content = old_content + content
            replaced_content = ""
        elif operation == "prepend":
            new_content = content + old_content
            replaced_content = ""
        elif operation == "delete_section":
            if not anchor:
                raise ValueError("anchor is required for delete_section")
            # H7: section patch 抽到 section_index 模块
            new_content, replaced_content = _delete_sec(
                old_content, anchor, artifact.type
            )
        else:
            raise ValueError(f"Unknown operation: {operation}")

        version, _ref_text = await self.create_version(
            artifact_id, new_content, f"patch:{operation} anchor={anchor}"
        )
        # F3: 复用 version.token_count（create_version 已计数），避免对 new_content 二次编码
        new_tokens = version.token_count
        replaced_tokens = count_tokens(replaced_content)
        tokens_added = new_tokens - old_tokens + replaced_tokens
        tokens_removed = replaced_tokens
        tokens_net = tokens_added - tokens_removed
        patch_info = {
            "artifact_id": artifact_id,
            "new_version": version.version_num,
            "tokens_added": tokens_added,
            "tokens_removed": tokens_removed,
            "tokens_net": tokens_net,
        }
        logger.info(
            "Patched artifact %s op=%s anchor=%s new_v=%d net_tokens=%d",
            artifact_id,
            operation,
            anchor,
            version.version_num,
            tokens_net,
        )
        return version, patch_info

    # ── AE-2: load_artifact ────────────────────────────────────

    def load_artifact(
        self,
        artifact_id: str,
        preview_only: bool = True,
        section: str | None = None,
    ) -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        version = self.get_version_content(artifact_id)
        if version is None:
            raise ValueError(f"No version found for artifact: {artifact_id}")

        # H7: section 构建/查找抽到 section_index 模块
        sections = _build_secs(version.content, artifact.type)

        result: dict = {
            "artifact_id": artifact_id,
            "title": artifact.name,
            "type": artifact.type,
            "version": version.version_num,
            "total_tokens": version.token_count,
            "sections": sections,
            "summary": artifact.summary or "",
        }

        if section:
            section = normalize_anchor(section)
            matches = _find_bounds(version.content, section, artifact.type)
            if not matches:
                raise ValueError(f"Section '{section}' not found in content")
            if len(matches) > 1:
                raise ValueError(
                    f"Multiple matches for section '{section}': "
                    f"found at lines {[m[0] + 1 for m in matches]}"
                )
            lines = version.content.split("\n")
            start, end, _ = matches[0]
            sec_content = "\n".join(lines[start:end])
            sec_tokens = count_tokens(sec_content)
            matched_anchor = section
            for s in sections:
                if s["anchor"].strip() == section:
                    matched_anchor = s["anchor"]
                    break
            result["section"] = {"anchor": matched_anchor, "tokens": sec_tokens}
            result["content"] = sec_content
        elif preview_only:
            result["content"] = None
        else:
            result["content"] = version.content

        logger.info(
            "Loaded artifact %s preview=%s section=%s sections=%d",
            artifact_id,
            preview_only,
            section,
            len(sections),
        )
        return result

    # ── AE-6: context_budget ───────────────────────────────────

    def context_budget(
        self,
        session_id: str | None = None,
        context_window: int | None = None,
    ) -> dict:
        # H7: token 预算逻辑抽到 token_budget 模块
        return _context_budget_fn(self.config, self.storage, session_id, context_window)

    # ── AE-7: auto_compact ─────────────────────────────────────

    async def auto_compact(self, artifact_id: str, token_budget: int) -> dict:
        from fusion_artifacts_engine.compactor import compact_and_count

        # H5: 阻塞 storage 读卸到线程池
        artifact = await asyncio.to_thread(self.storage.get_artifact, artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        version = self.get_version_content(artifact_id)
        if version is None:
            raise ValueError(f"No version found for artifact: {artifact_id}")

        original_tokens = version.token_count
        if original_tokens <= token_budget:
            logger.info(
                "Artifact %s already within budget: %d <= %d",
                artifact_id,
                original_tokens,
                token_budget,
            )
            return {
                "version": version.model_dump(),
                "original_tokens": original_tokens,
                "compacted_tokens": original_tokens,
                "savings_pct": 0.0,
                "compacted": False,
                "reason": "already_within_budget",
            }

        # F3: 大 content 压缩+计数卸线程池，避免阻塞事件循环
        compacted_content, compacted_tokens = await asyncio.to_thread(compact_and_count, version.content, artifact.type, token_budget)

        if compacted_tokens < original_tokens:
            version, _ = await self.create_version(
                artifact_id,
                compacted_content,
                change_log=f"auto_compact: {original_tokens} -> {compacted_tokens} tokens (budget={token_budget})",
                source="ai_generation",
            )
            savings = round(
                (original_tokens - compacted_tokens) / original_tokens * 100, 1
            )
            logger.info(
                "Auto-compacted %s: %d -> %d tokens (%.1f%% savings)",
                artifact_id,
                original_tokens,
                compacted_tokens,
                savings,
            )
            return {
                "version": version.model_dump(),
                "original_tokens": original_tokens,
                "compacted_tokens": compacted_tokens,
                "savings_pct": savings,
                "compacted": True,
            }

        logger.warning(
            "Auto-compact could not reduce %s below budget %d",
            artifact_id,
            token_budget,
        )
        return {
            "version": version.model_dump(),
            "original_tokens": original_tokens,
            "compacted_tokens": compacted_tokens,
            "savings_pct": 0.0,
            "compacted": False,
            "reason": "could_not_reduce",
        }

    # ── #36: version_diff ───────────────────────────────────────

    def version_diff(
        self, artifact_id: str, from_version: int, to_version: int
    ) -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        from_ver = self.storage.get_version(artifact_id, from_version)
        if from_ver is None:
            raise ValueError(
                f"Version {from_version} not found for artifact: {artifact_id}"
            )
        to_ver = self.storage.get_version(artifact_id, to_version)
        if to_ver is None:
            raise ValueError(
                f"Version {to_version} not found for artifact: {artifact_id}"
            )
        from_lines = from_ver.content.splitlines(keepends=True)
        to_lines = to_ver.content.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                from_lines,
                to_lines,
                fromfile=f"v{from_version}",
                tofile=f"v{to_version}",
            )
        )
        diff_text = "".join(diff_lines)
        lines_added = sum(1 for dl in diff_lines if dl.startswith("+") and not dl.startswith("+++"))
        lines_removed = sum(1 for dl in diff_lines if dl.startswith("-") and not dl.startswith("---"))
        logger.info(
            "Version diff %s v%d->v%d: +%d -%d lines",
            artifact_id,
            from_version,
            to_version,
            lines_added,
            lines_removed,
        )
        return {
            "diff": diff_text,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
        }

    # ── #37: render / check_safety / inject / interact / sync ───

    async def render_artifact(
        self,
        content: str,
        session_id: str,
        lang_hint: str = "",
        project_id: str | None = None,
    ) -> dict:
        # 边界说明：本方法不执行浏览器渲染，只做 (1) 阈值判定 (2) 类型检测
        # (3) 存储为 artifact (4) 返回 render_type 提示 + 原始 content。
        # 浏览器渲染由调用方 (fusion-studio) 负责；服务端渲染场景见
        # _render_share_html (公开分享只读预览，实现在 render.py)。
        from fusion_artifacts_engine.auto_identifier import (
            detect_artifact_type,
            detect_renderable_type,
            extract_name_hint,
            should_create_artifact,
        )

        if not content:
            logger.info("render_artifact: empty content, skipping")
            return {"created": False, "reason": "empty_content"}
        if not should_create_artifact(content, content_type="text"):
            logger.info("render_artifact: content below threshold, skipping")
            return {"created": False, "reason": "below_threshold"}
        try:
            name = extract_name_hint(content, lang_hint)
            artifact_type = detect_artifact_type(name, content)
            render_type = detect_renderable_type(content, name) or artifact_type
            renderable_types = {"html", "react", "markdown", "svg", "mermaid"}
            is_renderable = render_type in renderable_types
            artifact, _version, ref_text = await self.create_artifact(
                session_id=session_id,
                name=name,
                artifact_type=artifact_type,
                content=content,
                summary=_truncate_summary(content),
                change_log="Created via artifact.render",
                project_id=project_id,
            )
            logger.info(
                "render_artifact: created %s type=%s render_type=%s renderable=%s",
                artifact.id,
                artifact_type,
                render_type,
                is_renderable,
            )
            return {
                "created": True,
                "artifact": artifact.model_dump(),
                "render_type": render_type if is_renderable else None,
                "content": content,
                "ref_text": ref_text,
            }
        except Exception as e:
            logger.exception("render_artifact failed")
            return {"created": False, "reason": str(e)}

    def check_safety(
        self, messages: list[dict], output_budget: int | None = None
    ) -> dict:
        # H7: 委托 token_budget 模块
        return _check_safety_fn(self.config, messages, output_budget)

    def inject(
        self, messages: list[dict], output_budget: int | None = None
    ) -> dict:
        # H7: 委托 token_budget 模块（R7/STUB 语义不变）
        return _inject_fn(self.config, messages, output_budget)

    def interact_artifact(
        self,
        artifact_id: str,
        action: str,
        payload: dict | None = None,
        session_id: str | None = None,
    ) -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        event = self.emit_event(
            "artifact.interaction",
            artifact_id=artifact_id,
            session_id=session_id,
            payload={"action": action, "payload": payload or {}},
        )
        logger.info(
            "interact_artifact: %s action=%s event=%s", artifact_id, action, event.event_id
        )
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "action": action,
            "event_id": event.event_id,
            "dispatched": False,
            "stub": True,
            "note": "STUB: records interaction event only, no action dispatch implemented",
        }

    async def sync_artifact_file(
        self, artifact_id: str, file_path: str, direction: str
    ) -> dict:
        if direction not in ("artifact_to_code", "code_to_artifact"):
            raise ValueError(
                "direction must be 'artifact_to_code' or 'code_to_artifact'"
            )
        # H5: 阻塞 storage 读卸到线程池
        artifact = await asyncio.to_thread(self.storage.get_artifact, artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        path = Path(file_path).expanduser()
        # C-1: 路径必须落在配置的 sync_root 内，杜绝任意文件写/读 (LFI)
        sync_root = self.config.sync_root
        if sync_root is None:
            raise PermissionError(
                "sync_root not configured; refuse sync_artifact_file"
            )
        root_resolved = sync_root.resolve()
        try:
            target_resolved = path.resolve()
            target_resolved.relative_to(root_resolved)
        except ValueError as e:
            logger.warning(
                "sync path %s escapes sync_root %s", file_path, sync_root
            )
            raise PermissionError(
                f"file_path must be within sync_root: {file_path}"
            ) from e
        if direction == "artifact_to_code":
            version = self.get_version_content(artifact_id)
            if version is None:
                raise ValueError(f"No version found for artifact: {artifact_id}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(version.content, encoding="utf-8")
            logger.info(
                "sync_artifact_file: artifact %s -> %s (%d bytes)",
                artifact_id,
                file_path,
                len(version.content),
            )
        else:
            if not path.exists():
                raise ValueError(f"File not found: {file_path}")
            new_content = path.read_text(encoding="utf-8")
            await self.create_version(
                artifact_id, new_content, f"sync from {file_path}"
            )
            logger.info(
                "sync_artifact_file: %s -> artifact %s (%d bytes)",
                file_path,
                artifact_id,
                len(new_content),
            )
        return {
            "ok": True,
            "direction": direction,
            "artifact_id": artifact_id,
            "file_path": file_path,
        }

    def close(self) -> None:
        # A-4: 优雅停机——先 flush share access 缓冲防计数丢失，再通知 EventBus 关流
        # 让 SSE handler 退出，最后关 storage，避免线程悬在已关闭 storage handle 上
        try:
            self.shutdown_share_buffer()
        except Exception:
            logger.exception("share buffer flush error during close")
        try:
            self.event_bus.shutdown()
        except Exception:
            logger.exception("EventBus shutdown error during close")
        self.storage.close()
        logger.info("ArtifactEngine closed")
