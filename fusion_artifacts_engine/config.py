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
        with open(path) as f:
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


def _parse_bool(v: str) -> bool:
    # P1-12: env bool 解析——接受 true/false/1/0/yes/no（大小写不敏感），非法值 raise 触发 env_map 警告
    s = v.strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"invalid bool: {v}")


def _flatten_yaml_config(data: dict[str, Any]) -> dict[str, Any]:
    # A-9: yaml section.key -> model field 的映射集中在此表，新增字段只改一处。
    # 路径类值经 _expanduser_path 展开 ~；空字符串 sync_root 归一为 None。
    section_map: dict[str, dict[str, tuple[str, Any | None]]] = {
        "server": {
            "host": ("server_host", None),
            "port": ("server_port", None),
            "max_workers": ("server_max_workers", None),
        },
        "storage": {
            "root": ("storage_root", _expanduser_path),
            "db_name": ("db_name", None),
            "small_content_limit": ("small_content_limit", None),
            "sync_root": ("sync_root", _expanduser_path),
            "disk_space_warning_pct": ("disk_space_warning_pct", None),
            "wal_checkpoint_interval": ("wal_checkpoint_interval", None),
            "metadata_indexed_keys": ("metadata_indexed_keys", None),
        },
        "thresholds": {
            "auto_create_lines": ("auto_create_threshold_lines", None),
            "auto_create_chars": ("auto_create_threshold_chars", None),
        },
        "artifact": {
            "id_prefix": ("artifact_id_prefix", None),
            "max_versions_per_artifact": ("max_versions_per_artifact", None),
            "max_content_bytes": ("max_content_bytes", None),
            "max_metadata_bytes": ("max_metadata_bytes", None),
            "max_event_payload_bytes": ("max_event_payload_bytes", None),
        },
        "security": {
            "api_key": ("api_key", None),
            "allow_no_auth": ("allow_no_auth", None),
            "recycle_retention_days": ("recycle_retention_days", None),
            "share_max_ttl_days": ("share_max_ttl_days", None),
        },
        "sse": {
            "heartbeat_interval": ("sse_heartbeat_interval", None),
            "max_lifetime": ("sse_max_lifetime", None),
            "max_event_bytes": ("sse_max_event_bytes", None),
        },
        "rate_limit": {
            "rps": ("rate_limit_rps", None),
            "burst": ("rate_limit_burst", None),
            "public_rps": ("public_rate_limit_rps", None),
            "public_burst": ("public_rate_limit_burst", None),
        },
        "metrics": {
            "enabled": ("metrics_enabled", None),
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
            # 空字符串归一为 None：sync_root（禁用 sync）
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
        # P1-12/M22: 运维可调项 env 覆盖补全——容器/多节点部署不依赖改 YAML 即可调参
        "FUSION_ARTIFACTS_MAX_WORKERS": ("server_max_workers", int),
        "FUSION_ARTIFACTS_RATE_LIMIT_RPS": ("rate_limit_rps", float),
        "FUSION_ARTIFACTS_RATE_LIMIT_BURST": ("rate_limit_burst", int),
        "FUSION_ARTIFACTS_PUBLIC_RPS": ("public_rate_limit_rps", float),
        "FUSION_ARTIFACTS_PUBLIC_BURST": ("public_rate_limit_burst", int),
        "FUSION_ARTIFACTS_METRICS_ENABLED": ("metrics_enabled", _parse_bool),
        "FUSION_ARTIFACTS_DISK_WARNING_PCT": ("disk_space_warning_pct", int),
        # P1-4: WAL checkpoint 间隔 env 覆盖
        "FUSION_ARTIFACTS_WAL_CHECKPOINT_INTERVAL": ("wal_checkpoint_interval", int),
        "FUSION_ARTIFACTS_SSE_MAX_LIFETIME": ("sse_max_lifetime", int),
        "FUSION_ARTIFACTS_SSE_MAX_EVENT_BYTES": ("sse_max_event_bytes", int),
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
    # R1: HTTP 线程池上限，0=不限（回退 ThreadingMixIn 默认）。防 SSE/并发耗尽线程
    server_max_workers: int = Field(default=64)
    recycle_retention_days: int = Field(default=7)
    # L-6: share expires_at 最大 TTL（天），0=不限；过去日期一律拒绝
    share_max_ttl_days: int = Field(default=90)
    # C-4: 默认 fail-closed，无 api_key 时拒绝请求。本地单机可显式 allow_no_auth=true
    allow_no_auth: bool = Field(default=False)
    api_key: str | None = Field(default=None)
    sse_heartbeat_interval: int = Field(default=30)
    # P1-7/M7: SSE 单连接最大存活秒。0=不限。超时后服务端主动关流促客户端重连，
    # 防僵尸长连接占线程。生产建议 3600（1h），客户端应实现自动重连。
    sse_max_lifetime: int = Field(default=3600)
    # F5: SSE 单事件序列化后字节上限。超限事件被丢弃（仅记日志），防大 payload
    # 长时间阻塞单连接线程 + 客户端缓冲爆炸。0=不限（向后兼容）。
    sse_max_event_bytes: int = Field(default=262144)
    context_budget_default: int = Field(default=200000)
    # C-1: sync_artifact_file 文件读写根目录，路径必须在其下
    sync_root: Path | None = Field(default=None)
    # R9: 单 artifact 版本上限，0=不限；超限淘汰最旧非快照版本（防磁盘无限增长）
    max_versions_per_artifact: int = Field(default=100)
    # P0-5/H9: 单版本内容字节上限，0=不限；超限拒绝写入防 OOM/磁盘耗尽
    max_content_bytes: int = Field(default=0)
    # P0-5/H9: 单 artifact metadata JSON 字节上限，0=不限；超限拒绝写入
    max_metadata_bytes: int = Field(default=0)
    # P0-5/H6: emit_event payload JSON 字节上限，默认 64KB；超限拒绝写入防事件总线放大
    max_event_payload_bytes: int = Field(default=65536)
    # R9: 磁盘水位告警百分比（0-100），0=禁用监控。写前预检 + log warning。
    # 字段默认 0（禁用，单元测试不依赖宿主机磁盘状态）；生产经 default_config.yaml 设 90。
    disk_space_warning_pct: int = Field(default=0)
    # P1-4: WAL 周期 checkpoint 间隔秒。0=禁用后台 checkpoint（依赖 SQLite 默认 1000 页自动）。
    # 生产建议 300——后台 PASSIVE checkpoint 控制 -wal 文件增长，配合 backup.sh 在线备份。
    wal_checkpoint_interval: int = Field(default=300)
    # 运维1: 令牌桶限流。rps=0 表示不限流（默认）。default 桶覆盖 JSON-RPC + 鉴权 REST
    rate_limit_rps: float = Field(default=0)
    rate_limit_burst: int = Field(default=0)
    # 运维1: 公开 share 端点独立配额（无鉴权，易刷量，须独立桶）
    public_rate_limit_rps: float = Field(default=0)
    public_rate_limit_burst: int = Field(default=0)
    # 运维2: Prometheus /metrics 端点开关
    metrics_enabled: bool = Field(default=True)
    # P2-7/F6: metadata 高频过滤字段名。对每个 key 建 json_extract 表达式索引，
    # 使 metadata_filter 不再全表扫。空列表=不建索引（向后兼容）。仅含安全标识符字符。
    metadata_indexed_keys: list[str] = Field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.storage_root / self.db_name

    @property
    def content_dir(self) -> Path:
        return self.storage_root / "content"
