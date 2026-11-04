import logging
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

ArtifactType = Literal["code", "markdown", "html", "react", "svg", "mermaid", "data"]
ArtifactKind = Literal["app", "code", "document", "game", "tool", "template"]
OwnershipType = Literal["free", "project", "cowork"]
SnapshotType = Literal["auto", "manual", "named", "rollback"]

_TYPE_TO_KIND: dict[str, str] = {
    "html": "app",
    "react": "app",
    "svg": "app",
    "mermaid": "document",
    "markdown": "document",
    "code": "code",
    "data": "tool",
}

# P0-5/H6: metadata structure caps — top-level key count and per-key name length, against DoS-style huge/deeply-nested metadata
_MAX_METADATA_KEYS = 64
_MAX_METADATA_KEY_LEN = 128


def infer_kind(artifact_type: str) -> ArtifactKind:
    return _TYPE_TO_KIND.get(artifact_type, "tool")


def _validate_metadata(metadata: dict | None) -> dict | None:
    # P0-5/H6: metadata structure validation — reject non-dict, cap top-level key count and key name length.
    # The byte cap is enforced by the storage layer's _enforce_metadata_limit; this catches malformed structures early.
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict")
    if len(metadata) > _MAX_METADATA_KEYS:
        logger.warning("Metadata rejected: %d keys > %d", len(metadata), _MAX_METADATA_KEYS)
        raise ValueError(f"metadata has too many keys: {len(metadata)} > {_MAX_METADATA_KEYS}")
    for key in metadata:
        if not isinstance(key, str):
            raise ValueError(f"metadata key must be str, got {type(key).__name__}")
        if len(key) > _MAX_METADATA_KEY_LEN:
            raise ValueError(
                f"metadata key too long: {len(key)} > {_MAX_METADATA_KEY_LEN}"
            )
    return metadata


class Artifact(BaseModel):
    id: str
    session_id: str
    name: str
    type: ArtifactType
    kind: ArtifactKind | None = None
    project_id: str | None = None
    metadata: dict | None = None

    @field_validator("metadata")
    @classmethod
    def _metadata_validator(cls, v):
        return _validate_metadata(v)
    current_version: int = 1
    summary: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    is_deleted: bool = False
    owner_user_id: str | None = None
    ownership_type: OwnershipType = "free"
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
    # #77: remix lineage — the artifact this one was remixed from (None = original)
    parent_artifact_id: str | None = None


class ArtifactVersion(BaseModel):
    # #100-4: the global auto-increment rowid must not leak (it exposes global artifact volume and shows
    # "another session's v1 already at id=49" in the UI).
    # Field retained for storage-layer internal use; the API identifies versions by the (artifact_id, version_num) pair.
    id: int = Field(default=0, exclude=True)
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
    snapshot_type: SnapshotType = "auto"
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
    # E1: created_at/last_access_at are unified float epochs, consistent with Artifact/Version,
    # avoiding float<str TypeError in time sorting/range filtering. expires_at stays an ISO str
    # (parsed via _parse_expires_at/_is_expired, never used in sort comparisons).
    created_at: float | None = None
    expires_at: str | None = None
    revoked: bool = False
    access_count: int = 0
    # E2: the access cap field advertised in the README, stored as max_accesses, None=unlimited.
    # get_shared/get_public_share check access_count>=max_accesses before incrementing.
    max_accesses: int | None = None
    last_access_at: float | None = None
    # #77: share permission level — "view" read-only (default) / "remix" viewer may remix.
    permission: str = "view"

    @field_validator("permission")
    @classmethod
    def _permission_validator(cls, v):
        if v not in ("view", "remix"):
            raise ValueError(f"share permission must be 'view' or 'remix', got: {v!r}")
        return v


class ArtifactFolder(BaseModel):
    folder_id: str
    name: str
    parent_id: str | None = None
    project_id: str | None = None
    created_at: float | None = None


class ArtifactTag(BaseModel):
    tag_id: str
    name: str
    color: str | None = None
    # L-7: tag scoping — isolated by session_id or project_id, preventing cross-user leaks of same-named tags
    scope: str | None = None


class ArtifactEvent(BaseModel):
    event_id: str
    artifact_id: str | None = None
    session_id: str | None = None
    event_type: str
    payload: dict | None = None
    # E1: unified float epoch, consistent with Artifact/Version/Share/Folder
    created_at: float | None = None
