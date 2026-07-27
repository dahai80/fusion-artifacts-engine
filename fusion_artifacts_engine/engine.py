import time
import logging
from typing import Optional
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


class ArtifactEngine:

    def __init__(self, config: Optional[ArtifactEngineConfig] = None):
        self.config = config or ArtifactEngineConfig()
        self.storage = SQLiteStorage(
            db_path=self.config.db_path,
            content_dir=self.config.content_dir,
            small_content_limit=self.config.small_content_limit,
        )
        self.token_counter = TokenCounter(mlx_url=self.config.mlx_url)
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

    def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        return self.storage.get_artifact(artifact_id)

    def list_artifacts(self, session_id: str, include_deleted: bool = False) -> list[Artifact]:
        return self.storage.list_artifacts(session_id, include_deleted)

    def delete_artifact(self, artifact_id: str, soft_delete: bool = True) -> bool:
        return self.storage.delete_artifact(artifact_id, soft_delete)

    async def create_version(
        self,
        artifact_id: str,
        content: str,
        change_log: str = "",
    ) -> tuple[ArtifactVersion, str]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        new_version = self.storage.next_version_num(artifact_id)
        now = time.time()
        token_count = self.token_counter.count_sync(content)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=new_version,
            content=content,
            token_count=token_count,
            change_log=change_log,
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

    def close(self) -> None:
        self.storage.close()
        logger.info("ArtifactEngine closed")
