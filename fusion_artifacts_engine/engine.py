import time
import hashlib
import logging
from typing import Optional
from pathlib import Path
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.models import (
    Artifact, ArtifactVersion, ArtifactShare, ArtifactFolder,
    ArtifactTag, ArtifactEvent, infer_kind,
)
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage
from fusion_artifacts_engine.ref_parser import generate_ref_text
from fusion_artifacts_engine.auto_identifier import should_create_artifact
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

    def __init__(self, config: Optional[ArtifactEngineConfig] = None):
        self.config = config or ArtifactEngineConfig()
        self.storage = SQLiteStorage(
            db_path=self.config.db_path,
            content_dir=self.config.content_dir,
            small_content_limit=self.config.small_content_limit,
        )
        self._watchers: dict[str, list[str]] = {}
        logger.info("ArtifactEngine initialized: storage_root=%s", self.config.storage_root)

    async def create_artifact(
        self,
        session_id: str,
        name: str,
        artifact_type: str,
        content: str,
        summary: str = "",
        change_log: str = "Initial version",
        kind: Optional[str] = None,
        project_id: Optional[str] = None,
        metadata: Optional[dict] = None,
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
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=1,
            content=content,
            size_bytes=size,
            change_log=change_log,
            created_at=now,
        )
        self.storage.save_artifact_and_version(artifact, version)
        ref_text = generate_ref_text(artifact_id, name, artifact_type, 1, size, summary)
        logger.info("Created artifact: %s name=%s size=%d", artifact_id, name, size)
        return artifact, version, ref_text

    async def create_external_artifact(
        self,
        source_module: str,
        workspace_id: str,
        name: str,
        artifact_type: str,
        content: str,
        workflow_run_id: Optional[str] = None,
        summary: str = "",
        kind: Optional[str] = None,
        project_id: Optional[str] = None,
        metadata: Optional[dict] = None,
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
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=1,
            content=content,
            size_bytes=size,
            change_log="Created by external module",
            created_at=now,
        )
        self.storage.save_artifact_and_version(artifact, version)
        if project_id:
            self.storage.move_to_project_kb(artifact_id, project_id)
            logger.info("Auto-archived external artifact %s to project %s", artifact_id, project_id)
        ref_text = generate_ref_text(artifact_id, name, artifact_type, 1, size, summary)
        logger.info("Created external artifact: %s source=%s ws=%s size=%d", artifact_id, source_module, workspace_id, size)
        return artifact, version, ref_text

    def list_by_source(
        self,
        source_module: str,
        workspace_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
    ) -> list[Artifact]:
        return self.storage.list_by_source(source_module, workspace_id, workflow_run_id)

    def get_artifact(self, artifact_id: str, project_id: Optional[str] = None) -> Optional[Artifact]:
        return self.storage.get_artifact(artifact_id, project_id)

    def list_artifacts(self, session_id: str, include_deleted: bool = False, project_id: Optional[str] = None, metadata_filter: Optional[dict] = None) -> list[Artifact]:
        return self.storage.list_artifacts(session_id, include_deleted, project_id, metadata_filter)

    def delete_artifact(self, artifact_id: str, soft_delete: bool = True, project_id: Optional[str] = None) -> bool:
        return self.storage.delete_artifact(artifact_id, soft_delete, project_id)

    def get_version_content(self, artifact_id: str, version: Optional[int] = None) -> Optional[ArtifactVersion]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            return None
        ver = version if version is not None else artifact.current_version
        try:
            return self.storage.get_version(artifact_id, ver)
        except FileNotFoundError as e:
            logger.error("Content file missing for %s v%s: %s", artifact_id, ver, e)
            return None

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
            logger.error("Content file missing for %s v%s: %s", artifact_id, target_version, e)
            raise ValueError(f"Version content not found: {artifact_id} v{target_version}") from e
        if target is None:
            raise ValueError(f"Version not found: {artifact_id} v{target_version}")
        version, ref_text = await self.create_version(
            artifact_id, target.content, f"Rollback to v{target_version}"
        )
        logger.info("Rolled back %s to v%s, new v%s", artifact_id, target_version, version.version_num)
        return version, ref_text

    def should_create_artifact(self, content: str, content_type: str = "text") -> bool:
        return should_create_artifact(
            content, content_type,
            self.config.auto_create_threshold_lines,
            self.config.auto_create_threshold_chars,
        )

    _LANG_EXT = {
        "python": "py", "javascript": "js", "typescript": "ts",
        "html": "html", "css": "css", "markdown": "md",
        "json": "json", "yaml": "yaml", "rust": "rs",
        "go": "go", "java": "java", "c": "c", "cpp": "cpp",
    }

    _EXT_LANG = {v: k for k, v in _LANG_EXT.items()}

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
        metadata: Optional[dict] = None,
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
            logger.info("Watcher registered: %s for artifact %s", watcher_id, artifact_id)

    def unregister_watcher(self, artifact_id: str, watcher_id: str) -> None:
        if artifact_id in self._watchers:
            self._watchers[artifact_id] = [
                w for w in self._watchers[artifact_id] if w != watcher_id
            ]
            if not self._watchers[artifact_id]:
                del self._watchers[artifact_id]
            logger.info("Watcher unregistered: %s for artifact %s", watcher_id, artifact_id)

    def get_watch_events(self, artifact_id: str, since_version: int = 0) -> list[dict]:
        versions = self.storage.list_versions(artifact_id)
        events = []
        for v in versions:
            if v.version_num > since_version:
                events.append({
                    "artifact_id": artifact_id,
                    "version": v.version_num,
                    "change_log": v.change_log,
                    "created_at": v.created_at,
                })
        logger.debug("Watch events for %s since v%d: %d events", artifact_id, since_version, len(events))
        return events

    def get_artifact_raw_content(self, artifact_id: str) -> Optional[dict]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            return None
        version = self.get_version_content(artifact_id)
        if version is None:
            return None
        content_type_map = {"html": "text/html", "react": "text/html", "markdown": "text/markdown", "code": "text/plain", "data": "application/json"}
        ct = content_type_map.get(artifact.type, "text/plain")
        return {"content": version.content, "content_type": ct, "artifact_id": artifact_id, "version": version.version_num}

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

    def pin_artifact(self, artifact_id: str, chat_id: Optional[str] = None, pinned: bool = True) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.pin_artifact(artifact_id, chat_id, pinned)
        logger.info("Pin artifact %s pinned=%s chat=%s ok=%s", artifact_id, pinned, chat_id, ok)
        return ok

    def duplicate_artifact(self, artifact_id: str, new_name: Optional[str] = None) -> Optional[Artifact]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        new_id = generate_artifact_id(self.config.artifact_id_prefix)
        dup = self.storage.duplicate_artifact(artifact_id, new_id, new_name)
        logger.info("Duplicated artifact %s -> %s", artifact_id, new_id)
        return dup

    def list_all_artifacts(
        self,
        filters: Optional[dict] = None,
        sort: str = "updated_at",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Artifact], int]:
        artifacts, total = self.storage.list_all_artifacts(filters, sort, page, page_size)
        logger.info("list_all_artifacts: %d/%d page=%d", len(artifacts), total, page)
        return artifacts, total

    # ── P1: recycle bin ────────────────────────────────────────

    def list_recycle(self, page: int = 1, page_size: int = 20) -> tuple[list[Artifact], int]:
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
        logger.info("Purged %d expired artifacts (retention=%d days)", count, retention_days)
        return count

    # ── P1: optimistic lock ────────────────────────────────────

    async def create_version(
        self,
        artifact_id: str,
        content: str,
        change_log: str = "",
        source: str = "manual",
        expected_content_hash: Optional[str] = None,
    ) -> tuple[ArtifactVersion, str]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        if expected_content_hash is not None:
            current_hash = artifact.content_hash
            if current_hash is not None and current_hash != expected_content_hash:
                raise ValueError(f"Optimistic lock failed: expected hash {expected_content_hash}, got {current_hash}")
        if not change_log:
            old = self.storage.get_version(artifact_id, artifact.current_version)
            change_log = _auto_changelog(old.content if old else "", content)
        new_version = self.storage.next_version_num(artifact_id)
        now = time.time()
        size = _size_bytes(content)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=new_version,
            content=content,
            size_bytes=size,
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
        ref_text = generate_ref_text(artifact_id, artifact.name, artifact.type, new_version, size, artifact.summary)
        logger.info("Created version: %s v%s size=%d hash=%s", artifact_id, new_version, size, content_hash)
        return version, ref_text

    # ── P1: share ──────────────────────────────────────────────

    def create_share(
        self,
        artifact_id: str,
        created_by: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> ArtifactShare:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        existing = self.storage.get_share_by_artifact(artifact_id)
        if existing is not None:
            logger.info("Returning existing share %s for artifact %s", existing.share_id, artifact_id)
            return existing
        import uuid
        from datetime import datetime, timezone
        share_id = f"shr_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
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

    def get_shared_artifact(self, share_id: str) -> Optional[dict]:
        share = self.storage.get_share(share_id)
        if share is None:
            return None
        if share.revoked:
            logger.info("Share %s is revoked", share_id)
            return None
        if share.expires_at:
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            if now_iso > share.expires_at:
                logger.info("Share %s expired", share_id)
                return None
        self.storage.increment_share_access(share_id)
        artifact = self.storage.get_artifact(share.artifact_id)
        if artifact is None:
            return None
        version = self.get_version_content(share.artifact_id)
        content_type_map = {"html": "text/html", "react": "text/html", "markdown": "text/markdown", "code": "text/plain", "data": "application/json"}
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
        label: Optional[str] = None,
        author: Optional[str] = None,
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
        logger.info("Created snapshot: %s v%d label=%s", artifact_id, new_ver_num, label)
        return snapshot

    def list_snapshots(self, artifact_id: str) -> list[ArtifactVersion]:
        return self.storage.list_snapshots(artifact_id)

    # ── P2: folders ────────────────────────────────────────────

    def create_folder(self, name: str, parent_id: Optional[str] = None, project_id: Optional[str] = None) -> ArtifactFolder:
        import uuid
        from datetime import datetime, timezone
        folder_id = f"fld_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
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

    def list_folders(self, project_id: Optional[str] = None) -> list[ArtifactFolder]:
        return self.storage.list_folders(project_id)

    def rename_folder(self, folder_id: str, new_name: str) -> bool:
        ok = self.storage.rename_folder(folder_id, new_name)
        logger.info("Renamed folder %s -> %s ok=%s", folder_id, new_name, ok)
        return ok

    def delete_folder(self, folder_id: str) -> bool:
        ok = self.storage.delete_folder(folder_id)
        logger.info("Deleted folder %s ok=%s", folder_id, ok)
        return ok

    def move_to_folder(self, artifact_id: str, folder_id: Optional[str] = None) -> bool:
        ok = self.storage.move_to_folder(artifact_id, folder_id)
        logger.info("Moved artifact %s to folder %s ok=%s", artifact_id, folder_id, ok)
        return ok

    # ── P4: tags ───────────────────────────────────────────────

    def add_tag(self, artifact_id: str, tag_name: str, color: Optional[str] = None) -> ArtifactTag:
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
        artifact_id: Optional[str] = None,
        session_id: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> ArtifactEvent:
        import uuid
        from datetime import datetime, timezone
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        event = ArtifactEvent(
            event_id=event_id,
            artifact_id=artifact_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            created_at=now_iso,
        )
        self.storage.save_event(event)
        logger.info("Emitted event: %s type=%s artifact=%s", event_id, event_type, artifact_id)
        return event

    def list_events(
        self,
        artifact_id: Optional[str] = None,
        session_id: Optional[str] = None,
        since_ts: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ArtifactEvent], int]:
        events, total = self.storage.list_events(artifact_id, session_id, since_ts, page, page_size)
        logger.info("list_events: %d/%d page=%d", len(events), total, page)
        return events, total

    # ── P3: project KB ─────────────────────────────────────────

    def move_to_project_kb(self, artifact_id: str, project_id: str) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.move_to_project_kb(artifact_id, project_id)
        logger.info("Moved artifact %s to project_kb project=%s ok=%s", artifact_id, project_id, ok)
        return ok

    def close(self) -> None:
        self.storage.close()
        logger.info("ArtifactEngine closed")
