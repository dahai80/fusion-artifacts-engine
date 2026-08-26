import logging
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

ArtifactType = Literal["code", "markdown", "html", "react", "data"]
ArtifactKind = Literal["app", "code", "document", "game", "tool", "template"]
OwnershipType = Literal["free", "project", "cowork"]
SnapshotType = Literal["auto", "manual", "named", "rollback"]

_TYPE_TO_KIND: dict[str, str] = {
    "html": "app",
    "react": "app",
    "markdown": "document",
    "code": "code",
    "data": "tool",
}

# P0-5/H6: metadata 结构上限——顶层 key 数与单 key 名长度，防 DoS 式巨型/深嵌套 metadata
_MAX_METADATA_KEYS = 64
_MAX_METADATA_KEY_LEN = 128


def infer_kind(artifact_type: str) -> ArtifactKind:
    return _TYPE_TO_KIND.get(artifact_type, "tool")


def _validate_metadata(metadata: dict | None) -> dict | None:
    # P0-5/H6: metadata 结构校验——非 dict 拒绝、顶层 key 数与 key 名长度上限。
    # 字节上限由 storage 层 _enforce_metadata_limit 兜底；此处早拦截畸形结构。
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
    # E1: created_at/last_access_at 统一 float epoch，与 Artifact/Version 一致，
    # 避免按时间排序/范围过滤时 float<str TypeError。expires_at 保留 ISO str
    # （经 _parse_expires_at/_is_expired 解析，不参与排序比较）。
    created_at: float | None = None
    expires_at: str | None = None
    revoked: bool = False
    access_count: int = 0
    # E2: README 宣传的访问上限字段，存储 max_accesses，None=不限。
    # get_shared/get_public_share 在 increment 前校验 access_count>=max_accesses。
    max_accesses: int | None = None
    last_access_at: float | None = None


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
    # L-7: tag 作用域——按 session_id 或 project_id 隔离，避免跨用户同名 tag 泄露
    scope: str | None = None


class ArtifactEvent(BaseModel):
    event_id: str
    artifact_id: str | None = None
    session_id: str | None = None
    event_type: str
    payload: dict | None = None
    # E1: 统一 float epoch，与 Artifact/Version/Share/Folder 一致
    created_at: float | None = None
