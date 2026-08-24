import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent
_DEFAULT_CONFIG_PATH = _PACKAGE_DIR / "default_config.yaml"


def _default_user_config_path() -> Path:
    # M-1: 不在 import 时捕获 env，每次 load_config 实时读，避免长进程轮换配置失效
    return Path(
        os.environ.get(
            "FUSION_ARTIFACTS_CONFIG",
            str(Path.home() / ".fusion" / "artifacts" / "config.yaml"),
        )
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        logger.info("Loaded config from %s", path)
        return data or {}
    except FileNotFoundError:
        logger.debug("Config file not found: %s", path)
        return {}
    except yaml.YAMLError as e:
        # C-14: YAML 解析失败不再静默回退到 fail-open 默认，直接 raise
        logger.error("Failed to parse config %s: %s", path, e)
        raise RuntimeError(f"Invalid YAML config {path}: {e}") from e


def _expanduser_path(v: str) -> Path:
    # L-23: env 与 YAML 路径统一展开 ~
    return Path(os.path.expanduser(v))


def _flatten_yaml_config(data: dict[str, Any]) -> dict[str, Any]:
    # A-9: yaml section.key -> model field 的映射集中在此表，新增字段只改一处。
    # 路径类值经 _expanduser_path 展开 ~；空字符串 sync_root 归一为 None。
    section_map: dict[str, dict[str, tuple[str, Any | None]]] = {
        "server": {
            "host": ("server_host", None),
            "port": ("server_port", None),
        },
        "storage": {
            "root": ("storage_root", _expanduser_path),
            "db_name": ("db_name", None),
            "small_content_limit": ("small_content_limit", None),
            "sync_root": ("sync_root", _expanduser_path),
        },
        "thresholds": {
            "auto_create_lines": ("auto_create_threshold_lines", None),
            "auto_create_chars": ("auto_create_threshold_chars", None),
        },
        "artifact": {
            "id_prefix": ("artifact_id_prefix", None),
        },
        "security": {
            "api_key": ("api_key", None),
            "allow_no_auth": ("allow_no_auth", None),
            "recycle_retention_days": ("recycle_retention_days", None),
            "share_max_ttl_days": ("share_max_ttl_days", None),
        },
        "sse": {
            "heartbeat_interval": ("sse_heartbeat_interval", None),
        },
    }
    flat: dict[str, Any] = {}
    for section, field_map in section_map.items():
        section_data = data.get(section, {})
        if not isinstance(section_data, dict):
            logger.warning("Config section %s not a dict, skip: %r", section, section_data)
            continue
        for yaml_key, (field_name, converter) in field_map.items():
            if yaml_key not in section_data:
                continue
            val = section_data[yaml_key]
            if field_name == "sync_root" and not val:
                flat[field_name] = None
                continue
            if converter is not None:
                val = converter(val)
            flat[field_name] = val
    return flat


def load_config(user_config_path: Path | None = None) -> "ArtifactEngineConfig":
    default_data = _flatten_yaml_config(_load_yaml(_DEFAULT_CONFIG_PATH))
    user_path = user_config_path or _default_user_config_path()
    user_data = _flatten_yaml_config(_load_yaml(user_path))
    merged = {**default_data, **user_data}

    env_overrides: dict[str, Any] = {}
    env_map = {
        "FUSION_ARTIFACTS_HOST": ("server_host", str),
        "FUSION_ARTIFACTS_PORT": ("server_port", int),
        # L-23: env STORAGE_ROOT 也展开 ~
        "FUSION_ARTIFACTS_STORAGE_ROOT": ("storage_root", _expanduser_path),
        "FUSION_ARTIFACTS_API_KEY": ("api_key", str),
    }
    for env_key, (field_name, converter) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                env_overrides[field_name] = converter(val)
                logger.debug("Config override from env %s", env_key)
            except (ValueError, TypeError) as e:
                logger.warning("Invalid env %s=%s: %s", env_key, val, e)

    merged.update(env_overrides)

    # 丢弃用户配置中模型已不存在的 stale key，避免 extra="forbid" 抛错 (P2-18)。
    valid_fields = set(ArtifactEngineConfig.model_fields.keys())
    stale_keys = [k for k in merged if k not in valid_fields]
    for stale in stale_keys:
        logger.warning("Stripping stale config key: %s", stale)
        merged.pop(stale, None)

    # L-21: 不再 pop/reassign 绕过 pydantic 校验，host/port 一并经 model 构造校验
    return ArtifactEngineConfig(**merged)


class ArtifactEngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_root: Path = Field(default=Path.home() / ".fusion" / "artifacts")
    db_name: str = Field(default="meta.db")
    auto_create_threshold_lines: int = Field(default=30)
    auto_create_threshold_chars: int = Field(default=1500)
    small_content_limit: int = Field(default=10240)
    artifact_id_prefix: str = Field(default="art_")
    server_host: str = Field(default="127.0.0.1")
    server_port: int = Field(default=11451)
    recycle_retention_days: int = Field(default=7)
    # L-6: share expires_at 最大 TTL（天），0=不限；过去日期一律拒绝
    share_max_ttl_days: int = Field(default=90)
    # C-4: 默认 fail-closed，无 api_key 时拒绝请求。本地单机可显式 allow_no_auth=true
    allow_no_auth: bool = Field(default=False)
    api_key: str | None = Field(default=None)
    sse_heartbeat_interval: int = Field(default=30)
    context_budget_default: int = Field(default=200000)
    # C-1: sync_artifact_file 文件读写根目录，路径必须在其下
    sync_root: Path | None = Field(default=None)

    @property
    def db_path(self) -> Path:
        return self.storage_root / self.db_name

    @property
    def content_dir(self) -> Path:
        return self.storage_root / "content"
