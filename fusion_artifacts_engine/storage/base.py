import logging
from abc import ABC, abstractmethod
from typing import Optional
from fusion_artifacts_engine.models import Artifact, ArtifactVersion

logger = logging.getLogger(__name__)


class StorageDriver(ABC):
    @abstractmethod
    def save_artifact(self, artifact: Artifact) -> None: ...

    @abstractmethod
    def get_artifact(
        self, artifact_id: str, project_id: Optional[str] = None
    ) -> Optional[Artifact]: ...

    @abstractmethod
    def list_artifacts(
        self,
        session_id: str,
        include_deleted: bool = False,
        project_id: Optional[str] = None,
        metadata_filter: Optional[dict] = None,
    ) -> list[Artifact]: ...

    @abstractmethod
    def delete_artifact(
        self,
        artifact_id: str,
        soft_delete: bool = True,
        project_id: Optional[str] = None,
    ) -> bool: ...

    @abstractmethod
    def save_version(self, version: ArtifactVersion) -> None: ...

    @abstractmethod
    def get_version(
        self, artifact_id: str, version_num: int
    ) -> Optional[ArtifactVersion]: ...

    @abstractmethod
    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]: ...

    @abstractmethod
    def next_version_num(self, artifact_id: str) -> int: ...

    @abstractmethod
    def close(self) -> None: ...
