import logging
import time
from typing import Literal

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
    kind: ArtifactKind | None = None
    project_id: str | None = None
    metadata: dict | None = None
    current_version: int = 1
    summary: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    is_deleted: bool = False
    owner_user_id: str | None = None
    ownership_type: str = "free"
    is_starred: bool = False
    is_pinned: bool = False
    pinned_chat_id: str | None = None
    share_id: str | None = None
    in_project_kb: bool = False
    folder_id: str | None = None
    deleted_at: str | None = None
    content_hash: str | None = None
    active_in_session: str | None = None
    source_module: str | None = None
    workspace_id: str | None = None
    workflow_run_id: str | None = None


class ArtifactVersion(BaseModel):
    id: int = 0
    artifact_id: str
    version_num: int
    content: str = ""
    content_path: str | None = None
    size_bytes: int = 0
    token_count: int = 0
    section_index: str | None = None
    change_log: str = ""
    source: str = "manual"
    created_at: float = Field(default_factory=time.time)
    snapshot_type: str = "auto"
    snapshot_label: str | None = None
    author: str | None = None
    parent_version: int | None = None


class ArtifactRef(BaseModel):
    artifact_id: str
    name: str
    type: ArtifactType
    version: str
    size_bytes: int = 0
    summary: str = ""


class ArtifactShare(BaseModel):
    share_id: str
    artifact_id: str
    created_by: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    revoked: bool = False
    access_count: int = 0
    last_access_at: str | None = None


class ArtifactFolder(BaseModel):
    folder_id: str
    name: str
    parent_id: str | None = None
    project_id: str | None = None
    created_at: str | None = None


class ArtifactTag(BaseModel):
    tag_id: str
    name: str
    color: str | None = None


class ArtifactEvent(BaseModel):
    event_id: str
    artifact_id: str | None = None
    session_id: str | None = None
    event_type: str
    payload: dict | None = None
    created_at: str | None = None
