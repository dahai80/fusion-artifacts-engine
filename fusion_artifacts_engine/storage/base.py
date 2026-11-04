import logging
from abc import ABC, abstractmethod

from fusion_artifacts_engine.models import (
    Artifact,
    ArtifactEvent,
    ArtifactFolder,
    ArtifactShare,
    ArtifactTag,
    ArtifactVersion,
)

logger = logging.getLogger(__name__)

# H8: StorageDriver is the real contract for multi-node storage replacement (LSP).
# The original ABC had only 8 abstract methods while SQLiteStorage actually implements 40+ that the engine
# calls directly — abstraction and implementation had drifted apart, so when swapping storage (Postgres /
# object store / multi-node replication) half the methods the engine calls were undeclared and callers got
# no LSP guarantee. Completed to the full method set the engine actually depends on.
# Future storage swap: implement every abstract method of this ABC and the engine needs no changes.
# Note: signatures are aligned with SQLiteStorage so the existing implementation passes with zero changes.


class StorageDriver(ABC):
    # ── artifact ───────────────────────────────────────────────
    @abstractmethod
    def save_artifact(self, artifact: Artifact) -> None: ...

    @abstractmethod
    def save_artifact_and_version(
        self, artifact: Artifact, version: ArtifactVersion
    ) -> None: ...

    @abstractmethod
    def get_artifact(
        self, artifact_id: str, project_id: str | None = None
    ) -> Artifact | None: ...

    @abstractmethod
    def list_artifacts(
        self,
        session_id: str,
        include_deleted: bool = False,
        project_id: str | None = None,
        metadata_filter: dict | None = None,
    ) -> list[Artifact]: ...

    @abstractmethod
    def list_all_artifacts(
        self,
        filters: dict | None = None,
        sort: str = "updated_at",
        page: int = 1,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Artifact], int]: ...

    @abstractmethod
    def list_recycle(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[Artifact], int]: ...

    @abstractmethod
    def delete_artifact(
        self,
        artifact_id: str,
        soft_delete: bool = True,
        project_id: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def restore_artifact(self, artifact_id: str) -> bool: ...

    @abstractmethod
    def purge_expired(self, retention_days: int = 7) -> int: ...

    @abstractmethod
    def rename_artifact(self, artifact_id: str, new_name: str) -> bool: ...

    @abstractmethod
    def star_artifact(self, artifact_id: str, starred: bool) -> bool: ...

    @abstractmethod
    def pin_artifact(
        self, artifact_id: str, chat_id: str | None, pinned: bool
    ) -> bool: ...

    @abstractmethod
    def duplicate_artifact(
        self,
        artifact_id: str,
        new_id: str,
        new_name: str | None = None,
        parent_artifact_id: str | None = None,
    ) -> Artifact | None: ...

    @abstractmethod
    def move_to_folder(
        self, artifact_id: str, folder_id: str | None
    ) -> bool: ...

    @abstractmethod
    def move_to_project_kb(
        self, artifact_id: str, project_id: str
    ) -> bool: ...

    @abstractmethod
    def update_content_hash(
        self, artifact_id: str, content_hash: str
    ) -> None: ...

    # ── version ────────────────────────────────────────────────
    @abstractmethod
    def save_version(self, version: ArtifactVersion) -> None: ...

    @abstractmethod
    def create_version_atomic(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        content_hash: str,
        summary: str | None,
        expected_content_hash: str | None = None,
    ) -> int: ...

    @abstractmethod
    def get_version(
        self, artifact_id: str, version_num: int
    ) -> ArtifactVersion | None: ...

    @abstractmethod
    def list_versions(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]: ...

    @abstractmethod
    def list_snapshots(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]: ...

    @abstractmethod
    def next_version_num(self, artifact_id: str) -> int: ...

    @abstractmethod
    def sum_token_counts(
        self,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[int, list[dict]]: ...

    # ── share ──────────────────────────────────────────────────
    @abstractmethod
    def save_share(self, share: ArtifactShare) -> None: ...

    @abstractmethod
    def get_share(self, share_id: str) -> ArtifactShare | None: ...

    @abstractmethod
    def get_share_by_artifact(
        self, artifact_id: str
    ) -> ArtifactShare | None: ...

    @abstractmethod
    def revoke_share(self, share_id: str) -> bool: ...

    def update_share_permission(self, share_id: str, permission: str) -> bool:
        # #77: optional capability — unsupported by default (returns False); the SQLite implementation supports it
        return False

    @abstractmethod
    def increment_share_access(
        self, share_id: str, delta: int = 1
    ) -> None: ...

    @abstractmethod
    def set_artifact_share_id(
        self, artifact_id: str, share_id: str
    ) -> None: ...

    # ── folder ─────────────────────────────────────────────────
    @abstractmethod
    def save_folder(self, folder: ArtifactFolder) -> None: ...

    @abstractmethod
    def get_folder(self, folder_id: str) -> ArtifactFolder | None: ...

    @abstractmethod
    def list_folders(
        self, project_id: str | None = None
    ) -> list[ArtifactFolder]: ...

    @abstractmethod
    def rename_folder(self, folder_id: str, new_name: str) -> bool: ...

    @abstractmethod
    def delete_folder(self, folder_id: str) -> bool: ...

    # ── tag ────────────────────────────────────────────────────
    @abstractmethod
    def save_tag(self, tag: ArtifactTag) -> None: ...

    @abstractmethod
    def get_tag(self, tag_id: str) -> ArtifactTag | None: ...

    @abstractmethod
    def get_tag_by_name(
        self, name: str, scope: str | None = None
    ) -> ArtifactTag | None: ...

    @abstractmethod
    def list_tags(self, scope: str | None = None) -> list[ArtifactTag]: ...

    @abstractmethod
    def list_artifact_tags(
        self, artifact_id: str
    ) -> list[ArtifactTag]: ...

    @abstractmethod
    def add_artifact_tag(self, artifact_id: str, tag_id: str) -> bool: ...

    @abstractmethod
    def remove_artifact_tag(self, artifact_id: str, tag_id: str) -> bool: ...

    # ── event ──────────────────────────────────────────────────
    @abstractmethod
    def save_event(self, event: ArtifactEvent) -> None: ...

    @abstractmethod
    def list_events(
        self,
        artifact_id: str | None = None,
        session_id: str | None = None,
        since_ts: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ArtifactEvent], int]: ...

    # ── external source ────────────────────────────────────────
    @abstractmethod
    def list_by_source(
        self,
        source_module: str,
        workspace_id: str | None = None,
        workflow_run_id: str | None = None,
        page: int = 1,
        page_size: int = 200,
    ) -> list[Artifact]: ...

    # ── maintenance ────────────────────────────────────────────
    @abstractmethod
    def update_artifact_summary(
        self, artifact_id: str, summary: str, metadata_patch: dict | None = None
    ) -> bool: ...

    @abstractmethod
    def gc_orphan_files(self) -> int: ...

    @abstractmethod
    def health_check(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...
