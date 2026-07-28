import time
import hashlib
import logging
from typing import Optional
from pathlib import Path
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.models import Artifact, ArtifactVersion, ArtifactRef, infer_kind
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage
from fusion_artifacts_engine.token_counter import TokenCounter
from fusion_artifacts_engine.ref_parser import generate_ref_text
from fusion_artifacts_engine.injection import inject_artifacts_to_messages
from fusion_artifacts_engine.auto_identifier import should_create_artifact, detect_artifact_type
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


class ArtifactEngine:

    def __init__(self, config: Optional[ArtifactEngineConfig] = None):
        self.config = config or ArtifactEngineConfig()
        self.storage = SQLiteStorage(
            db_path=self.config.db_path,
            content_dir=self.config.content_dir,
            small_content_limit=self.config.small_content_limit,
        )
        self.token_counter = TokenCounter(mlx_url=self.config.mlx_url)
        self._watchers: dict[str, list[str]] = {}
        self._sync_registry: dict[str, dict] = {}
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
            current_version=1,
            summary=summary,
            created_at=now,
            updated_at=now,
        )
        token_count = self.token_counter.count_sync(content)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=1,
            content=content,
            token_count=token_count,
            change_log=change_log,
            created_at=now,
        )
        self.storage.save_artifact_and_version(artifact, version)
        ref_text = generate_ref_text(artifact_id, name, artifact_type, 1, token_count, summary)
        logger.info("Created artifact: %s name=%s tokens=%s", artifact_id, name, token_count)
        return artifact, version, ref_text

    def get_artifact(self, artifact_id: str, project_id: Optional[str] = None) -> Optional[Artifact]:
        return self.storage.get_artifact(artifact_id, project_id)

    def list_artifacts(self, session_id: str, include_deleted: bool = False, project_id: Optional[str] = None) -> list[Artifact]:
        return self.storage.list_artifacts(session_id, include_deleted, project_id)

    def delete_artifact(self, artifact_id: str, soft_delete: bool = True, project_id: Optional[str] = None) -> bool:
        return self.storage.delete_artifact(artifact_id, soft_delete, project_id)

    async def create_version(
        self,
        artifact_id: str,
        content: str,
        change_log: str = "",
        source: str = "manual",
    ) -> tuple[ArtifactVersion, str]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        if not change_log:
            old = self.storage.get_version(artifact_id, artifact.current_version)
            change_log = _auto_changelog(old.content if old else "", content)
        new_version = self.storage.next_version_num(artifact_id)
        now = time.time()
        token_count = self.token_counter.count_sync(content)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=new_version,
            content=content,
            token_count=token_count,
            change_log=change_log,
            source=source,
            created_at=now,
        )
        self.storage.save_version(version)
        artifact.current_version = new_version
        artifact.updated_at = now
        if not artifact.summary:
            artifact.summary = _truncate_summary(content)
        self.storage.save_artifact(artifact)
        ref_text = generate_ref_text(artifact_id, artifact.name, artifact.type, new_version, token_count, artifact.summary)
        logger.info("Created version: %s v%s tokens=%s", artifact_id, new_version, token_count)
        return version, ref_text

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

    async def inject(
        self,
        messages: list[dict],
        max_context: Optional[int] = None,
    ) -> tuple[list[dict], int, bool]:
        mc = max_context or self.config.safe_context_threshold

        async def get_content(artifact_id: str, version: str) -> Optional[str]:
            ver_num = None
            if version != "latest":
                try:
                    ver_num = int(version)
                except ValueError:
                    pass
            result = self.get_version_content(artifact_id, ver_num)
            return result.content if result else None

        return await inject_artifacts_to_messages(
            messages, get_content, self.token_counter, mc, self.config.output_reserve_tokens
        )

    async def check_safety(
        self,
        messages: list[dict],
        max_context: Optional[int] = None,
    ) -> tuple[bool, int, int]:
        mc = max_context or self.config.safe_context_threshold
        return await self.token_counter.check_safety(
            messages, mc, self.config.output_reserve_tokens
        )

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

    async def sync_artifact_file(self, artifact_id: str, code_path: str, direction: str = "artifact_to_code") -> dict:
        path = Path(code_path).resolve()
        storage_root = Path(self.config.storage_root).resolve()
        try:
            path.relative_to(storage_root)
        except ValueError:
            raise ValueError(f"code_path must be under storage root {storage_root}")
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        version = self.get_version_content(artifact_id)
        if version is None:
            raise ValueError(f"No content for artifact: {artifact_id}")
        content_hash = hashlib.sha256(version.content.encode("utf-8")).hexdigest()[:16]
        if direction == "artifact_to_code":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(version.content, encoding="utf-8")
            self._sync_registry[artifact_id] = {
                "path": str(path),
                "hash": content_hash,
                "version": version.version_num,
            }
            logger.info("Synced artifact %s v%d -> %s", artifact_id, version.version_num, path)
            return {"direction": "artifact_to_code", "path": str(path), "version": version.version_num}
        elif direction == "code_to_artifact":
            if not path.exists():
                raise ValueError(f"Code file not found: {path}")
            code_content = path.read_text(encoding="utf-8")
            file_hash = hashlib.sha256(code_content.encode("utf-8")).hexdigest()[:16]
            reg = self._sync_registry.get(artifact_id)
            if reg and reg.get("hash") == file_hash:
                return {"direction": "code_to_artifact", "status": "no_change", "version": version.version_num}
            old_hash = reg.get("hash", "") if reg else ""
            new_version, ref = await self.create_version(
                artifact_id, code_content, f"Synced from {path}"
            )
            new_content_hash = hashlib.sha256(code_content.encode("utf-8")).hexdigest()[:16]
            self._sync_registry[artifact_id] = {
                "path": str(path),
                "hash": new_content_hash,
                "version": new_version.version_num,
            }
            logger.info("Synced %s -> artifact %s v%d", path, artifact_id, new_version.version_num)
            return {"direction": "code_to_artifact", "path": str(path), "version": new_version.version_num, "ref_text": ref}
        else:
            raise ValueError(f"Invalid direction: {direction}, must be 'artifact_to_code' or 'code_to_artifact'")

    def close(self) -> None:
        self.storage.close()
        logger.info("ArtifactEngine closed")
