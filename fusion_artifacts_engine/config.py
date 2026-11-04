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
    # M-1: do not capture env at import time; read live on each load_config so long-running
    # processes pick up config rotation
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
        # C-14: YAML parse failure no longer silently falls back to fail-open defaults; raise directly
        logger.error("Failed to parse config %s: %s", path, e)
        raise RuntimeError(f"Invalid YAML config {path}: {e}") from e


def _expanduser_path(v: str) -> Path:
    # L-23: expand ~ uniformly for env and YAML paths
    return Path(os.path.expanduser(v))


def _parse_bool(v: str) -> bool:
    # P1-12: env bool parsing - accepts true/false/1/0/yes/no (case-insensitive); invalid values
    # raise to trigger the env_map warning
    s = v.strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"invalid bool: {v}")


def _resolve_contract_env() -> dict[str, Any]:
    # #69 (config Phase 2.2): contract vars FUSION_ARTIFACTS_PORT / FUSION_ARTIFACTS_DATA_DIR
    # are resolved via fusion_core.config as the single point (config-conventions §1/§2).
    # fusion_core is a monorepo sibling package and may be missing in isolated CI - fall back to a
    # semantically equivalent in-repo resolution (same pattern as dsl_parser's
    # _fallback_validate_dsl); both paths behave identically.
    overrides: dict[str, Any] = {}
    raw_port = os.environ.get("FUSION_ARTIFACTS_PORT", "")
    if raw_port:
        try:
            from fusion_core.config import service_port
        except ImportError:
            try:
                overrides["server_port"] = int(raw_port)
            except ValueError:
                logger.warning("Invalid env FUSION_ARTIFACTS_PORT=%s: not an integer", raw_port)
        else:
            try:
                overrides["server_port"] = service_port("artifacts", default=11451)
            except ValueError as exc:
                logger.warning("Invalid FUSION_ARTIFACTS_PORT: %s", exc)
    raw_dir = os.environ.get("FUSION_ARTIFACTS_DATA_DIR", "")
    if raw_dir:
        try:
            from fusion_core.config import service_data_dir
        except ImportError:
            overrides["storage_root"] = _expanduser_path(raw_dir)
        else:
            # fusion_core does not expand ~ (upstream gap); expand in-repo to stay consistent
            # with STORAGE_ROOT
            overrides["storage_root"] = _expanduser_path(str(service_data_dir("artifacts", default=raw_dir)))
    return overrides


def _flatten_yaml_config(data: dict[str, Any]) -> dict[str, Any]:
    # A-9: the yaml section.key -> model field mapping is centralized in this one table;
    # new fields only touch this. Path values are ~-expanded via _expanduser_path;
    # empty sync_root normalizes to None.
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
            "max_connections": ("sse_max_connections", None),
        },
        "rate_limit": {
            "rps": ("rate_limit_rps", None),
            "burst": ("rate_limit_burst", None),
            "public_rps": ("public_rate_limit_rps", None),
            "public_burst": ("public_rate_limit_burst", None),
        },
        "metrics": {
            "enabled": ("metrics_enabled", None),
            "token": ("metrics_token", None),
        },
        "tracing": {
            "enabled": ("tracing_enabled", None),
            "service_name": ("tracing_service_name", None),
        },
        "gateway": {
            "enabled": ("gateway_enabled", None),
            "url": ("gateway_url", None),
            "module_id": ("gateway_module_id", None),
            "admin_key": ("gateway_admin_key", None),
            "key_file": ("gateway_key_file", _expanduser_path),
            "heartbeat_interval": ("gateway_heartbeat_interval", None),
        },
        "share": {
            "public_base_url": ("share_public_base_url", None),
        },
        "ai": {
            "base_url": ("ai_base_url", None),
            "model": ("ai_model", None),
            "timeout": ("ai_timeout", None),
            "api_key": ("ai_api_key", None),
        },
        "ipc": {
            "enabled": ("ipc_enabled", None),
            "design_dir": ("ipc_design_dir", _expanduser_path),
            "poll_interval": ("ipc_poll_interval", None),
            "office_enabled": ("ipc_office_enabled", None),
            "office_dir": ("ipc_office_dir", _expanduser_path),
        },
        "long_context": {
            "summary_threshold": ("long_context_summary_threshold", None),
            "summary_model": ("long_context_summary_model", None),
            "mlx_url": ("long_context_mlx_url", None),
            "mlx_api_key": ("long_context_mlx_api_key", None),
            "rag_enabled": ("long_context_rag_enabled", None),
            "rag_url": ("long_context_rag_url", None),
            "rag_api_key": ("long_context_rag_api_key", None),
            "rag_kb_id": ("long_context_rag_kb_id", None),
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
            # Empty string normalizes to None: path defaults are derived in code (sync_root
            # disabled / gateway_key_file uses storage_root default / ipc_design_dir uses the
            # convention dir)
            if (
                field_name in ("sync_root", "gateway_key_file", "ipc_design_dir", "ipc_office_dir")
                and not val
            ):
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
        # FUSION_ARTIFACTS_PORT / FUSION_ARTIFACTS_DATA_DIR contract vars are resolved
        # via fusion_core in _resolve_contract_env (#69), no longer through env_map.
        # L-23: env STORAGE_ROOT is also ~-expanded. Legacy alias after #69; prefer DATA_DIR.
        "FUSION_ARTIFACTS_STORAGE_ROOT": ("storage_root", _expanduser_path),
        "FUSION_ARTIFACTS_API_KEY": ("api_key", str),
        # P1-12/M22: ops-tunable env override completion - container/multi-node deployments
        # can tune parameters without editing YAML
        "FUSION_ARTIFACTS_MAX_WORKERS": ("server_max_workers", int),
        "FUSION_ARTIFACTS_RATE_LIMIT_RPS": ("rate_limit_rps", float),
        "FUSION_ARTIFACTS_RATE_LIMIT_BURST": ("rate_limit_burst", int),
        "FUSION_ARTIFACTS_PUBLIC_RPS": ("public_rate_limit_rps", float),
        "FUSION_ARTIFACTS_PUBLIC_BURST": ("public_rate_limit_burst", int),
        "FUSION_ARTIFACTS_METRICS_ENABLED": ("metrics_enabled", _parse_bool),
        "FUSION_ARTIFACTS_METRICS_TOKEN": ("metrics_token", str),
        "FUSION_ARTIFACTS_DISK_WARNING_PCT": ("disk_space_warning_pct", int),
        # P1-4: WAL checkpoint interval env override
        "FUSION_ARTIFACTS_WAL_CHECKPOINT_INTERVAL": ("wal_checkpoint_interval", int),
        "FUSION_ARTIFACTS_SSE_MAX_LIFETIME": ("sse_max_lifetime", int),
        "FUSION_ARTIFACTS_SSE_MAX_EVENT_BYTES": ("sse_max_event_bytes", int),
        "FUSION_ARTIFACTS_SSE_MAX_CONNECTIONS": ("sse_max_connections", int),
        # P2-5: OTel tracing env override
        "FUSION_ARTIFACTS_TRACING_ENABLED": ("tracing_enabled", _parse_bool),
        "FUSION_ARTIFACTS_TRACING_SERVICE_NAME": ("tracing_service_name", str),
        # #75: web-shell integration env override
        "FUSION_ARTIFACTS_GATEWAY_ENABLED": ("gateway_enabled", _parse_bool),
        "FUSION_ARTIFACTS_GATEWAY_URL": ("gateway_url", str),
        "FUSION_ARTIFACTS_GATEWAY_MODULE_ID": ("gateway_module_id", str),
        "FUSION_ARTIFACTS_GATEWAY_ADMIN_KEY": ("gateway_admin_key", str),
        "FUSION_ARTIFACTS_GATEWAY_KEY_FILE": ("gateway_key_file", _expanduser_path),
        "FUSION_ARTIFACTS_GATEWAY_HEARTBEAT_INTERVAL": (
            "gateway_heartbeat_interval",
            int,
        ),
        # #83: public share page base URL (empty = derived from server host/port)
        "FUSION_ARTIFACTS_SHARE_PUBLIC_BASE_URL": ("share_public_base_url", str),
        # #84: fusion-design save_artifact file-IPC consumption
        "FUSION_ARTIFACTS_IPC_ENABLED": ("ipc_enabled", _parse_bool),
        "FUSION_ARTIFACTS_IPC_DESIGN_DIR": ("ipc_design_dir", _expanduser_path),
        "FUSION_ARTIFACTS_IPC_POLL_INTERVAL": ("ipc_poll_interval", float),
        # v0.7.0: fusion-office file ingest (docx/xlsx/pptx -> sectioned markdown)
        "FUSION_ARTIFACTS_IPC_OFFICE_ENABLED": ("ipc_office_enabled", _parse_bool),
        "FUSION_ARTIFACTS_IPC_OFFICE_DIR": ("ipc_office_dir", _expanduser_path),
        # v0.7.1 #91: rolling summary + RAG archival
        "FUSION_ARTIFACTS_LC_SUMMARY_THRESHOLD": ("long_context_summary_threshold", int),
        "FUSION_ARTIFACTS_LC_RAG_ENABLED": ("long_context_rag_enabled", _parse_bool),
        "FUSION_ARTIFACTS_LC_MLX_URL": ("long_context_mlx_url", None),
        "FUSION_ARTIFACTS_LC_RAG_URL": ("long_context_rag_url", None),
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

    # #69: contract vars (PORT/DATA_DIR) applied afterwards - higher priority than legacy
    # STORAGE_ROOT; STORAGE_ROOT kept during migration (config-conventions §6); setting it
    # warns and points to DATA_DIR.
    merged.update(_resolve_contract_env())
    if os.environ.get("FUSION_ARTIFACTS_STORAGE_ROOT", ""):
        logger.warning(
            "FUSION_ARTIFACTS_STORAGE_ROOT is deprecated, set FUSION_ARTIFACTS_DATA_DIR instead "
            "(config-conventions §1, issue #69)"
        )

    # Strip stale keys no longer present on the model from user config, avoiding
    # extra="forbid" errors (P2-18).
    valid_fields = set(ArtifactEngineConfig.model_fields.keys())
    stale_keys = [k for k in merged if k not in valid_fields]
    for stale in stale_keys:
        logger.warning("Stripping stale config key: %s", stale)
        merged.pop(stale, None)

    # L-21: no longer pop/reassign to bypass pydantic validation; host/port are validated
    # via model construction too
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
    # R1: HTTP thread-pool cap, 0 = unlimited (falls back to ThreadingMixIn default).
    # Prevents SSE/concurrency from exhausting threads
    server_max_workers: int = Field(default=64)
    recycle_retention_days: int = Field(default=7)
    # L-6: max share expires_at TTL (days), 0 = unlimited; past dates are always rejected
    share_max_ttl_days: int = Field(default=90)
    # C-4: fail-closed by default; requests without api_key are rejected. Local single-user
    # setups may set allow_no_auth=true explicitly
    allow_no_auth: bool = Field(default=False)
    api_key: str | None = Field(default=None)
    sse_heartbeat_interval: int = Field(default=30)
    # P1-7/M7: max SSE single-connection lifetime in seconds. 0 = unlimited. After timeout
    # the server closes the stream proactively to prompt client reconnect, preventing zombie
    # long-lived connections from hogging threads. Production recommends 3600 (1h);
    # clients should implement auto-reconnect.
    sse_max_lifetime: int = Field(default=3600)
    # F5: max serialized byte size per SSE event. Oversized events are dropped (log only),
    # preventing large payloads from blocking a connection thread long-term plus client
    # buffer blowup. 0 = unlimited (backward compatible).
    sse_max_event_bytes: int = Field(default=262144)
    # P2-1/F1/H3: dedicated SSE concurrency cap. After the SSE handshake the RPC worker
    # semaphore is released and this dedicated semaphore held, so long-lived connections no
    # longer monopolize the 64 RPC threads - 64 SSE clients no longer saturate the backend
    # causing new RPCs to all 503. Default 16: enough for local single-machine
    # (fusion-studio typically 1-2 SSE); 0 = no dedicated cap (old behavior, not recommended).
    sse_max_connections: int = Field(default=16)
    context_budget_default: int = Field(default=200000)
    # C-1: root dir for sync_artifact_file reads/writes; paths must stay under it
    sync_root: Path | None = Field(default=None)
    # R9: per-artifact version cap, 0 = unlimited; over the cap the oldest non-snapshot
    # version is evicted (prevents unbounded disk growth)
    max_versions_per_artifact: int = Field(default=100)
    # P0-5/H9: per-version content byte cap, 0 = unlimited; over the cap writes are rejected
    # to prevent OOM/disk exhaustion
    max_content_bytes: int = Field(default=0)
    # P0-5/H9: per-artifact metadata JSON byte cap, 0 = unlimited; over the cap writes are rejected
    max_metadata_bytes: int = Field(default=0)
    # P0-5/H6: emit_event payload JSON byte cap, default 64KB; over the cap writes are
    # rejected to prevent event-bus amplification
    max_event_payload_bytes: int = Field(default=65536)
    # R9: disk usage warning percentage (0-100), 0 = disables monitoring. Pre-write check
    # plus log warning. Field default 0 (disabled, so unit tests do not depend on host disk
    # state); production sets 90 via default_config.yaml.
    disk_space_warning_pct: int = Field(default=0)
    # P1-4: WAL periodic checkpoint interval in seconds. 0 = disables background checkpoint
    # (relies on SQLite default 1000-page auto checkpoint). Production recommends 300 -
    # background PASSIVE checkpoint controls -wal file growth, paired with backup.sh online backup.
    wal_checkpoint_interval: int = Field(default=300)
    # Ops-1: token-bucket rate limiting. rps=0 means unlimited (default). The default bucket
    # covers JSON-RPC + authenticated REST
    rate_limit_rps: float = Field(default=0)
    rate_limit_burst: int = Field(default=0)
    # Ops-1: isolated quota for public share endpoints (unauthenticated, easily abused,
    # needs its own bucket)
    public_rate_limit_rps: float = Field(default=0)
    public_rate_limit_burst: int = Field(default=0)
    # Ops-2: Prometheus /metrics endpoint switch
    metrics_enabled: bool = Field(default=True)
    # LOW-4: optional token auth for /metrics. When set, /metrics requires a matching
    # X-Metrics-Token header (constant-time compare). Empty = no auth (relies on the
    # 127.0.0.1 binding for local isolation). Must be set when exposing 0.0.0.0, otherwise
    # ops metrics leak.
    metrics_token: str | None = Field(default=None)
    # P2-5/H12(trace)/M20: OTel distributed tracing switch. Default false - local-first
    # single-tenant needs no tracing, and opentelemetry-sdk is an optional dependency
    # ([otel] extra). With enabled=true and the SDK missing it is a no-op + WARN.
    tracing_enabled: bool = Field(default=False)
    tracing_service_name: str = Field(default="fusion-artifacts-engine")
    # P2-7/F6: high-frequency metadata filter field names. Builds a json_extract expression
    # index per key so metadata_filter no longer does full table scans. Empty list = no
    # indexes (backward compatible). Only safe identifier characters allowed.
    metadata_indexed_keys: list[str] = Field(default_factory=list)
    # #75: web-shell integration - self-register the shell manifest to fusion-gateway plus
    # periodic heartbeat. enabled=false (default) zero overhead; enabled=true and api_key
    # configured starts the registration/heartbeat thread.
    gateway_enabled: bool = Field(default=False)
    gateway_url: str = Field(default="http://127.0.0.1:11432")
    gateway_module_id: str = Field(default="fusion-artifacts")
    # For first-registration bootstrap only (gateway web_shell.admin_key); after successful
    # registration the engine's own api_key handles heartbeat and #369 self-heal
    # re-registration, and admin_key can be removed from config.
    gateway_admin_key: str | None = Field(default=None)
    # #82: persistence file for the gateway-issued fm- service key (empty =
    # storage_root/gateway_service_key)
    gateway_key_file: str | None = Field(default=None)
    gateway_heartbeat_interval: int = Field(default=15)
    # #83: public share page base URL (empty = derived from server host/port)
    share_public_base_url: str = Field(default="")
    # #84: fusion-design save_artifact file-IPC consumption (dir default =
    # ~/.fusion-design/fusion-artifacts-engine)
    ipc_enabled: bool = Field(default=True)
    ipc_design_dir: str | None = Field(default=None)
    ipc_poll_interval: float = Field(default=3.0)
    # v0.7.0: fusion-office file ingest (dir default = ~/.fusion-office/fusion-artifacts-engine)
    ipc_office_enabled: bool = Field(default=True)
    ipc_office_dir: str | None = Field(default=None)
    # v0.7.1 #91: layered context compaction - rolling summary (fusion-mlx) plus
    # old-version archival (fusion-rag)
    long_context_summary_threshold: int = Field(default=5)
    long_context_summary_model: str = Field(default="")
    long_context_mlx_url: str = Field(default="http://127.0.0.1:11434")
    long_context_mlx_api_key: str = Field(default="")
    long_context_rag_enabled: bool = Field(default=True)
    # The /kb prefix is the fusion-rag application-layer route (openapi: /kb/bases/...)
    long_context_rag_url: str = Field(default="http://127.0.0.1:11436/kb")
    long_context_rag_api_key: str = Field(default="")
    long_context_rag_kb_id: str = Field(default="fusion-artifacts")
    # v0.7.5 #97: AI generate/iterate entry - webui calls the fusion-mlx OpenAI-compatible
    # API via the ai.generate RPC. Empty model = auto-pick the first available model
    # from /v1/models.
    ai_base_url: str = Field(default="http://127.0.0.1:11434")
    ai_model: str = Field(default="")
    ai_timeout: float = Field(default=120.0)
    ai_api_key: str = Field(default="")

    @property
    def gateway_service_key_path(self) -> Path:
        if self.gateway_key_file:
            return Path(self.gateway_key_file).expanduser()
        return self.storage_root / "gateway_service_key"

    @property
    def share_base_url(self) -> str:
        if self.share_public_base_url:
            return self.share_public_base_url.rstrip("/")
        return f"http://{self.server_host}:{self.server_port}"

    @property
    def ipc_design_path(self) -> Path:
        if self.ipc_design_dir:
            return Path(self.ipc_design_dir).expanduser()
        base = os.environ.get("FUSION_DESIGN_DATA_DIR", "")
        root = Path(base).expanduser() if base else Path.home() / ".fusion-design"
        return root / "fusion-artifacts-engine"

    @property
    def ipc_office_path(self) -> Path:
        if self.ipc_office_dir:
            return Path(self.ipc_office_dir).expanduser()
        base = os.environ.get("FUSION_OFFICE_DATA_DIR", "")
        root = Path(base).expanduser() if base else Path.home() / ".fusion-office"
        return root / "fusion-artifacts-engine"

    @property
    def ipc_office_outbox_path(self) -> Path:
        return self.ipc_office_path / ".outbox"

    @property
    def db_path(self) -> Path:
        return self.storage_root / self.db_name

    @property
    def content_dir(self) -> Path:
        return self.storage_root / "content"
