import hashlib
import json
import logging
import re
import time
from datetime import UTC
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
from fusion_artifacts_engine.section_index import extract_sections
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage
from fusion_artifacts_engine.token_counter import count_tokens
from fusion_artifacts_engine.utils import generate_artifact_id

logger = logging.getLogger(__name__)

_SUMMARY_MAX_LEN = 200


def _truncate_summary(content: str) -> str:
    return content[:_SUMMARY_MAX_LEN].replace("\n", " ").strip()


def _auto_changelog(old_content: str, new_content: str) -> str:
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    added = max(len(new_lines) - len(old_lines), 0)
    removed = max(len(old_lines) - len(new_lines), 0)
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
    return ", ".join(parts)


def _size_bytes(content: str) -> int:
    return len(content.encode("utf-8"))


class ArtifactEngine:
    def __init__(self, config: ArtifactEngineConfig | None = None):
        self.config = config or ArtifactEngineConfig()
        self.storage = SQLiteStorage(
            db_path=self.config.db_path,
            content_dir=self.config.content_dir,
            small_content_limit=self.config.small_content_limit,
        )
        self._watchers: dict[str, list[str]] = {}
        logger.info(
            "ArtifactEngine initialized: storage_root=%s", self.config.storage_root
        )

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
            metadata=metadata,
            current_version=1,
            summary=summary,
            created_at=now,
            updated_at=now,
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
        self.storage.save_artifact_and_version(artifact, version)
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
        self.storage.save_artifact_and_version(artifact, version)
        if project_id:
            self.storage.move_to_project_kb(artifact_id, project_id)
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
    ) -> list[Artifact]:
        return self.storage.list_by_source(source_module, workspace_id, workflow_run_id)

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
        if result is not None and result.section_index:
            try:
                result.section_index = json.loads(result.section_index)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Invalid section_index JSON for %s v%s", artifact_id, ver
                )
                result.section_index = None
        return result

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        return self.storage.list_versions(artifact_id)

    async def rollback_version(
        self,
        artifact_id: str,
        target_version: int,
    ) -> tuple[ArtifactVersion, str]:
        try:
            target = self.storage.get_version(artifact_id, target_version)
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
            artifact_id, target.content, f"Rollback to v{target_version}"
        )
        logger.info(
            "Rolled back %s to v%s, new v%s",
            artifact_id,
            target_version,
            version.version_num,
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
        if artifact_id not in self._watchers:
            self._watchers[artifact_id] = []
        if watcher_id not in self._watchers[artifact_id]:
            self._watchers[artifact_id].append(watcher_id)
            logger.info(
                "Watcher registered: %s for artifact %s", watcher_id, artifact_id
            )

    def unregister_watcher(self, artifact_id: str, watcher_id: str) -> None:
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
        page_size: int = 20,
    ) -> tuple[list[Artifact], int]:
        artifacts, total = self.storage.list_all_artifacts(
            filters, sort, page, page_size
        )
        logger.info("list_all_artifacts: %d/%d page=%d", len(artifacts), total, page)
        return artifacts, total

    # ── P1: recycle bin ────────────────────────────────────────

    def list_recycle(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[Artifact], int]:
        artifacts, total = self.storage.list_recycle(page, page_size)
        logger.info("list_recycle: %d/%d page=%d", len(artifacts), total, page)
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
    ) -> tuple[ArtifactVersion, str]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        if expected_content_hash is not None:
            current_hash = artifact.content_hash
            if current_hash is not None and current_hash != expected_content_hash:
                raise ValueError(
                    f"Optimistic lock failed: expected hash {expected_content_hash}, got {current_hash}"
                )
        if not change_log:
            old = self.storage.get_version(artifact_id, artifact.current_version)
            change_log = _auto_changelog(old.content if old else "", content)
        new_version = self.storage.next_version_num(artifact_id)
        now = time.time()
        size = _size_bytes(content)
        sections = extract_sections(content, artifact.type)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=new_version,
            content=content,
            size_bytes=size,
            token_count=count_tokens(content),
            section_index=json.dumps(sections) if sections else None,
            change_log=change_log,
            source=source,
            created_at=now,
        )
        self.storage.save_version(version)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        artifact.current_version = new_version
        artifact.updated_at = now
        artifact.content_hash = content_hash
        if not artifact.summary:
            artifact.summary = _truncate_summary(content)
        self.storage.save_artifact(artifact)
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
    ) -> ArtifactShare:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        existing = self.storage.get_share_by_artifact(artifact_id)
        if existing is not None:
            logger.info(
                "Returning existing share %s for artifact %s",
                existing.share_id,
                artifact_id,
            )
            return existing
        import uuid
        from datetime import datetime

        share_id = f"shr_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(UTC).isoformat()
        share = ArtifactShare(
            share_id=share_id,
            artifact_id=artifact_id,
            created_by=created_by,
            created_at=now_iso,
            expires_at=expires_at,
        )
        self.storage.save_share(share)
        with self.storage._write_lock:
            self.storage._conn.execute(
                "UPDATE artifacts SET share_id = ?, updated_at = ? WHERE id = ?",
                (share_id, time.time(), artifact_id),
            )
            self.storage._conn.commit()
        logger.info("Created share %s for artifact %s", share_id, artifact_id)
        return share

    def get_shared_artifact(self, share_id: str) -> dict | None:
        share = self.storage.get_share(share_id)
        if share is None:
            return None
        if share.revoked:
            logger.info("Share %s is revoked", share_id)
            return None
        if share.expires_at:
            from datetime import datetime

            now_iso = datetime.now(UTC).isoformat()
            if now_iso > share.expires_at:
                logger.info("Share %s expired", share_id)
                return None
        self.storage.increment_share_access(share_id)
        artifact = self.storage.get_artifact(share.artifact_id)
        if artifact is None:
            return None
        version = self.get_version_content(share.artifact_id)
        content_type_map = {
            "html": "text/html",
            "react": "text/html",
            "markdown": "text/markdown",
            "code": "text/plain",
            "data": "application/json",
        }
        ct = content_type_map.get(artifact.type, "text/plain")
        return {
            "artifact": artifact.model_dump(),
            "content": version.content if version else "",
            "content_type": ct,
        }

    def revoke_share(self, share_id: str) -> bool:
        ok = self.storage.revoke_share(share_id)
        logger.info("Revoked share %s ok=%s", share_id, ok)
        return ok

    # ── P2: snapshots ──────────────────────────────────────────

    async def create_snapshot(
        self,
        artifact_id: str,
        label: str | None = None,
        author: str | None = None,
    ) -> ArtifactVersion:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        current = self.storage.get_version(artifact_id, artifact.current_version)
        if current is None:
            raise ValueError(f"No current version for artifact: {artifact_id}")
        new_ver_num = self.storage.next_version_num(artifact_id)
        now = time.time()
        snapshot = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=new_ver_num,
            content=current.content,
            size_bytes=current.size_bytes,
            change_log=f"Snapshot: {label}" if label else "Snapshot",
            source="manual",
            created_at=now,
            snapshot_type="named",
            snapshot_label=label,
            author=author,
            parent_version=artifact.current_version,
        )
        self.storage.save_version(snapshot)
        artifact.current_version = new_ver_num
        artifact.updated_at = now
        self.storage.save_artifact(artifact)
        logger.info(
            "Created snapshot: %s v%d label=%s", artifact_id, new_ver_num, label
        )
        return snapshot

    def list_snapshots(self, artifact_id: str) -> list[ArtifactVersion]:
        return self.storage.list_snapshots(artifact_id)

    # ── P2: folders ────────────────────────────────────────────

    def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
        project_id: str | None = None,
    ) -> ArtifactFolder:
        import uuid
        from datetime import datetime

        folder_id = f"fld_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(UTC).isoformat()
        folder = ArtifactFolder(
            folder_id=folder_id,
            name=name,
            parent_id=parent_id,
            project_id=project_id,
            created_at=now_iso,
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

    def add_tag(
        self, artifact_id: str, tag_name: str, color: str | None = None
    ) -> ArtifactTag:
        import uuid

        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        existing = self.storage.get_tag_by_name(tag_name)
        if existing:
            tag = existing
        else:
            tag_id = f"tag_{uuid.uuid4().hex[:12]}"
            tag = ArtifactTag(tag_id=tag_id, name=tag_name, color=color)
            self.storage.save_tag(tag)
            logger.info("Created tag: %s name=%s", tag_id, tag_name)
        self.storage.add_artifact_tag(artifact_id, tag.tag_id)
        logger.info("Added tag %s to artifact %s", tag.tag_id, artifact_id)
        return tag

    def remove_tag(self, artifact_id: str, tag_name: str) -> bool:
        tag = self.storage.get_tag_by_name(tag_name)
        if tag is None:
            logger.warning("Tag not found: %s", tag_name)
            return False
        ok = self.storage.remove_artifact_tag(artifact_id, tag.tag_id)
        logger.info("Removed tag %s from artifact %s ok=%s", tag_name, artifact_id, ok)
        return ok

    def list_tags(self) -> list[ArtifactTag]:
        return self.storage.list_tags()

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
        import uuid
        from datetime import datetime

        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(UTC).isoformat()
        event = ArtifactEvent(
            event_id=event_id,
            artifact_id=artifact_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            created_at=now_iso,
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
        logger.info("list_events: %d/%d page=%d", len(events), total, page)
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
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        if (
            expected_version is not None
            and artifact.current_version != expected_version
        ):
            raise ValueError(
                f"Optimistic lock failed: expected version {expected_version}, "
                f"got {artifact.current_version}"
            )
        current = self.storage.get_version(artifact_id, artifact.current_version)
        if current is None:
            raise ValueError(f"No current version for artifact: {artifact_id}")
        old_content = current.content
        old_tokens = count_tokens(old_content)

        if operation == "replace_section":
            if not anchor:
                raise ValueError("anchor is required for replace_section")
            new_content, replaced_content = self._replace_section(
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
            new_content, replaced_content = self._delete_section(
                old_content, anchor, artifact.type
            )
        else:
            raise ValueError(f"Unknown operation: {operation}")

        version, _ref_text = await self.create_version(
            artifact_id, new_content, f"patch:{operation} anchor={anchor}"
        )
        replaced_tokens = count_tokens(replaced_content)
        new_tokens = count_tokens(new_content)
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

    @staticmethod
    def _find_section_bounds(
        content: str, anchor: str, artifact_type: str
    ) -> list[tuple[int, int, int]]:
        anchor = anchor.lstrip("#").strip()
        lines = content.split("\n")
        matches = []
        for i, line in enumerate(lines):
            matched = False
            if artifact_type == "markdown":
                m = re.match(r"^(#{1,6})\s+(.+)$", line)
                if m and m.group(2).strip() == anchor:
                    matched = True
                    level = len(m.group(1))
            elif artifact_type == "code":
                m = re.match(r"^(?:async\s+)?(?:def|class|func)\s+(\w+)", line)
                if m and m.group(1) == anchor:
                    matched = True
                    level = 1
            if matched:
                end = len(lines)
                if artifact_type == "markdown":
                    for j in range(i + 1, len(lines)):
                        m2 = re.match(r"^(#{1,6})\s+", lines[j])
                        if m2 and len(m2.group(1)) <= level:
                            end = j
                            break
                elif artifact_type == "code":
                    for j in range(i + 1, len(lines)):
                        m2 = re.match(
                            r"^(?:async\s+)?(?:def|class|func)\s+\w+", lines[j]
                        )
                        if m2:
                            end = j
                            break
                matches.append((i, end, level))
        return matches

    def _replace_section(
        self, content: str, anchor: str, new_content: str, artifact_type: str
    ) -> tuple[str, str]:
        matches = self._find_section_bounds(content, anchor, artifact_type)
        if len(matches) > 1:
            raise ValueError(
                f"Multiple matches for anchor '{anchor}': "
                f"found at lines {[m[0] + 1 for m in matches]}"
            )
        if not matches:
            raise ValueError(f"Anchor '{anchor}' not found in content")
        lines = content.split("\n")
        start, end, _ = matches[0]
        replaced_lines = lines[start:end]
        replaced_content = "\n".join(replaced_lines)
        new_lines = lines[:start] + new_content.split("\n") + lines[end:]
        return "\n".join(new_lines), replaced_content

    def _delete_section(
        self, content: str, anchor: str, artifact_type: str
    ) -> tuple[str, str]:
        matches = self._find_section_bounds(content, anchor, artifact_type)
        if len(matches) > 1:
            raise ValueError(
                f"Multiple matches for anchor '{anchor}': "
                f"found at lines {[m[0] + 1 for m in matches]}"
            )
        if not matches:
            raise ValueError(f"Anchor '{anchor}' not found in content")
        lines = content.split("\n")
        start, end, _ = matches[0]
        replaced_lines = lines[start:end]
        replaced_content = "\n".join(replaced_lines)
        new_lines = lines[:start] + lines[end:]
        return "\n".join(new_lines), replaced_content

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

        result: dict = {
            "artifact_id": artifact_id,
            "name": artifact.name,
            "type": artifact.type,
            "version": version.version_num,
            "token_count": version.token_count,
            "section_index": version.section_index,
            "summary": artifact.summary or "",
        }

        if section:
            section = section.lstrip("#").strip()
            matches = self._find_section_bounds(version.content, section, artifact.type)
            if not matches:
                raise ValueError(f"Section '{section}' not found in content")
            if len(matches) > 1:
                raise ValueError(
                    f"Multiple matches for section '{section}': "
                    f"found at lines {[m[0] + 1 for m in matches]}"
                )
            lines = version.content.split("\n")
            start, end, _ = matches[0]
            result["content"] = "\n".join(lines[start:end])
            result["section"] = section
        elif preview_only:
            result["content"] = None
        else:
            result["content"] = version.content

        logger.info(
            "Loaded artifact %s preview=%s section=%s",
            artifact_id,
            preview_only,
            section,
        )
        return result

    # ── AE-6: context_budget ───────────────────────────────────

    def context_budget(self, session_id: str) -> dict:
        artifacts = self.storage.list_artifacts(session_id)
        budget = self.config.context_budget_default
        artifact_tokens = []
        total_tokens = 0
        for art in artifacts:
            ver = self.get_version_content(art.id)
            tc = ver.token_count if ver else 0
            total_tokens += tc
            artifact_tokens.append({"id": art.id, "name": art.name, "token_count": tc})
        available = max(0, budget - total_tokens)
        utilization_pct = round(total_tokens / budget * 100, 1) if budget > 0 else 0.0
        logger.info(
            "Context budget session=%s used=%d/%d (%.1f%%)",
            session_id,
            total_tokens,
            budget,
            utilization_pct,
        )
        return {
            "session_id": session_id,
            "total_budget": budget,
            "used_tokens": total_tokens,
            "available_tokens": available,
            "utilization_pct": utilization_pct,
            "artifacts": artifact_tokens,
        }

    def close(self) -> None:
        self.storage.close()
        logger.info("ArtifactEngine closed")
