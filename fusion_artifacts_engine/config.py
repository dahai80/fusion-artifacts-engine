import os
import logging
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field
import yaml

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent
_DEFAULT_CONFIG_PATH = _PACKAGE_DIR / "default_config.yaml"
_USER_CONFIG_PATH = Path(
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
    except Exception as e:
        logger.warning("Failed to load config %s: %s", path, e)
        return {}


def _flatten_yaml_config(data: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    server = data.get("server", {})
    if "host" in server:
        flat["server_host"] = server["host"]
    if "port" in server:
        flat["server_port"] = server["port"]

    storage = data.get("storage", {})
    if "root" in storage:
        root = storage["root"]
        root = os.path.expanduser(root)
        flat["storage_root"] = Path(root)
    if "db_name" in storage:
        flat["db_name"] = storage["db_name"]
    if "small_content_limit" in storage:
        flat["small_content_limit"] = storage["small_content_limit"]

    thresholds = data.get("thresholds", {})
    if "auto_create_lines" in thresholds:
        flat["auto_create_threshold_lines"] = thresholds["auto_create_lines"]
    if "auto_create_chars" in thresholds:
        flat["auto_create_threshold_chars"] = thresholds["auto_create_chars"]

    artifact = data.get("artifact", {})
    if "id_prefix" in artifact:
        flat["artifact_id_prefix"] = artifact["id_prefix"]

    security = data.get("security", {})
    if "allow_no_auth" in security:
        flat["allow_no_auth"] = security["allow_no_auth"]
    if "recycle_retention_days" in security:
        flat["recycle_retention_days"] = security["recycle_retention_days"]

    sse = data.get("sse", {})
    if "heartbeat_interval" in sse:
        flat["sse_heartbeat_interval"] = sse["heartbeat_interval"]

    return flat


def load_config(user_config_path: Optional[Path] = None) -> "ArtifactEngineConfig":
    default_data = _flatten_yaml_config(_load_yaml(_DEFAULT_CONFIG_PATH))
    user_path = user_config_path or _USER_CONFIG_PATH
    user_data = _flatten_yaml_config(_load_yaml(user_path))
    merged = {**default_data, **user_data}

    env_overrides: dict[str, Any] = {}
    env_map = {
        "FUSION_ARTIFACTS_HOST": ("server_host", str),
        "FUSION_ARTIFACTS_PORT": ("server_port", int),
        "FUSION_ARTIFACTS_STORAGE_ROOT": ("storage_root", lambda v: Path(v)),
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

    server_host = merged.pop("server_host", None)
    server_port = merged.pop("server_port", None)

    # Remove stale keys from user config that no longer exist in model
    for stale in ("mlx_url", "safe_context_threshold", "output_reserve_tokens"):
        merged.pop(stale, None)

    config = ArtifactEngineConfig(**merged)

    if server_host is not None:
        config.server_host = server_host
    if server_port is not None:
        config.server_port = server_port

    return config


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
    allow_no_auth: bool = Field(default=True)
    sse_heartbeat_interval: int = Field(default=30)

    @property
    def db_path(self) -> Path:
        return self.storage_root / self.db_name

    @property
    def content_dir(self) -> Path:
        return self.storage_root / "content"
