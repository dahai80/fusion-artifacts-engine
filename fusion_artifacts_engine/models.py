import time
import logging
from typing import Literal, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ArtifactType = Literal["code", "markdown", "html", "react", "data"]
ArtifactKind = Literal["app", "code", "document", "game", "tool", "template"]

_TYPE_TO_KIND: dict[str, str] = {
    "html": "app",
    "react": "app",
    "markdown": "document",
    "code": "code",
    "data": "tool",
}


def infer_kind(artifact_type: str) -> str:
    return _TYPE_TO_KIND.get(artifact_type, "tool")


class Artifact(BaseModel):
    id: str
    session_id: str
    name: str
    type: ArtifactType
    kind: Optional[ArtifactKind] = None
    project_id: Optional[str] = None
    metadata: Optional[dict] = None
    current_version: int = 1
    summary: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    is_deleted: bool = False


class ArtifactVersion(BaseModel):
    id: int = 0
    artifact_id: str
    version_num: int
    content: str = ""
    content_path: Optional[str] = None
    token_count: int = 0
    change_log: str = ""
    source: str = "manual"
    created_at: float = Field(default_factory=time.time)


class ArtifactRef(BaseModel):
    artifact_id: str
    name: str
    type: ArtifactType
    version: str
    token_count: int = 0
    summary: str = ""
