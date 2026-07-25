import os
import logging
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field
import yaml

# User instruction: "所有的项目要有一个配置文件，配置类的卸载配置文件里面，不能写死在代码里面"
# Importers/callers: __main__.py calls load_config(), engine.py receives ArtifactEngineConfig, server.py reads config.server_host/server_port
# Affected API: ArtifactEngineConfig gains server_host/server_port fields; new load_config() function; DEFAULT_* constants removed
# Data schemas: YAML config with server/storage/mlx/thresholds/artifact sections; ArtifactEngineConfig Pydantic model

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent
_DEFAULT_CONFIG_PATH = _PACKAGE_DIR / "default_config.yaml"
_USER_CONFIG_PATH = Path(os.environ.get(
    "FUSION_ARTIFACTS_CONFIG",
    str(Path.home() / ".fusion" / "artifacts" / "config.yaml"),
))


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

    mlx = data.get("mlx", {})
    if "url" in mlx:
        flat["mlx_url"] = mlx["url"]

    thresholds = data.get("thresholds", {})
    if "safe_context" in thresholds:
        flat["safe_context_threshold"] = thresholds["safe_context"]
    if "output_reserve" in thresholds:
        flat["output_reserve_tokens"] = thresholds["output_reserve"]
    if "auto_create_lines" in thresholds:
        flat["auto_create_threshold_lines"] = thresholds["auto_create_lines"]
    if "auto_create_chars" in thresholds:
        flat["auto_create_threshold_chars"] = thresholds["auto_create_chars"]

    artifact = data.get("artifact", {})
    if "id_prefix" in artifact:
        flat["artifact_id_prefix"] = artifact["id_prefix"]

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
        "FUSION_ARTIFACTS_MLX_URL": ("mlx_url", str),
        "FUSION_ARTIFACTS_SAFE_CONTEXT": ("safe_context_threshold", int),
        "FUSION_ARTIFACTS_OUTPUT_RESERVE": ("output_reserve_tokens", int),
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
    mlx_url: str = Field(default="http://localhost:8890")
    safe_context_threshold: int = Field(default=180_000)
    output_reserve_tokens: int = Field(default=8192)
    auto_create_threshold_lines: int = Field(default=30)
    auto_create_threshold_chars: int = Field(default=1500)
    small_content_limit: int = Field(default=10240)
    artifact_id_prefix: str = Field(default="art_")
    server_host: str = Field(default="127.0.0.1")
    server_port: int = Field(default=8892)

    @property
    def db_path(self) -> Path:
        return self.storage_root / self.db_name

    @property
    def content_dir(self) -> Path:
        return self.storage_root / "content"
