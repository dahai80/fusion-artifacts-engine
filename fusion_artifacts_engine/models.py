import time
import logging
from typing import Literal, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ArtifactType = Literal["code", "markdown", "html", "react", "data"]
ArtifactKind = Literal["app", "code", "document", "game", "tool", "template"]
OwnershipType = Literal["free", "project", "cowork"]
SnapshotType = Literal["auto", "manual", "named"]

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
    owner_user_id: Optional[str] = None
    ownership_type: str = "free"
    is_starred: bool = False
    is_pinned: bool = False
    pinned_chat_id: Optional[str] = None
    share_id: Optional[str] = None
    in_project_kb: bool = False
    folder_id: Optional[str] = None
    deleted_at: Optional[str] = None
    content_hash: Optional[str] = None
    active_in_session: Optional[str] = None
    source_module: Optional[str] = None
    workspace_id: Optional[str] = None
    workflow_run_id: Optional[str] = None


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
    snapshot_type: str = "auto"
    snapshot_label: Optional[str] = None
    author: Optional[str] = None
    parent_version: Optional[int] = None


class ArtifactRef(BaseModel):
    artifact_id: str
    name: str
    type: ArtifactType
    version: str
    token_count: int = 0
    summary: str = ""


class ArtifactShare(BaseModel):
    share_id: str
    artifact_id: str
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    revoked: bool = False
    access_count: int = 0
    last_access_at: Optional[str] = None


class ArtifactFolder(BaseModel):
    folder_id: str
    name: str
    parent_id: Optional[str] = None
    project_id: Optional[str] = None
    created_at: Optional[str] = None


class ArtifactTag(BaseModel):
    tag_id: str
    name: str
    color: Optional[str] = None


class ArtifactEvent(BaseModel):
    event_id: str
    artifact_id: Optional[str] = None
    session_id: Optional[str] = None
    event_type: str
    payload: Optional[dict] = None
    created_at: Optional[str] = None
