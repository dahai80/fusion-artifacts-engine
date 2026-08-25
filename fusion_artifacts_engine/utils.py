import json
import logging
import os
import re
import secrets
import string
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

# 路径段安全字符集：artifact_id / share_id / folder_id 等会拼进文件系统路径的标识符 (C-6, C-10)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
# id_prefix 允许字符：字母数字下划线连字符，禁止 / .. 空字节等 (C-6)
_SAFE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 运维3: 日志轮转参数。10MB 单文件，保留 5 份轮转备份。
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_LOG_FILE_NAME = "artifacts-engine.log"


def validate_id_prefix(prefix: str) -> str:
    if not prefix or not _SAFE_PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            f"Invalid artifact_id_prefix: {prefix!r} (allowed [A-Za-z0-9_-]+)"
        )
    return prefix


def assert_safe_path_id(id_value: str, field_name: str = "id") -> str:
    if not id_value or not _SAFE_ID_RE.fullmatch(id_value):
        raise ValueError(
            f"Unsafe {field_name} for filesystem path: {id_value!r}"
        )
    return id_value


def generate_artifact_id(prefix: str = "art_") -> str:
    validate_id_prefix(prefix)
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(8))
    artifact_id = f"{prefix}{suffix}"
    logger.debug("Generated artifact ID: %s", artifact_id)
    return artifact_id


class JsonFormatter(logging.Formatter):
    # 运维3: 结构化 JSON 日志，便于 ELK/Loki 采集与检索。
    # 每行一个 JSON 对象：ts / level / logger / msg / 可选字段。

    _STD_ATTRS = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in self._STD_ATTRS and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _resolve_log_dir() -> Path | None:
    # 运维3: 日志目录优先 env，其次 storage_root/logs，最后 ~/.fusion/artifacts/logs。
    env_dir = os.environ.get("FUSION_ARTIFACTS_LOG_DIR")
    if env_dir:
        return Path(os.path.expanduser(env_dir))
    try:
        from fusion_artifacts_engine.config import load_config
        cfg = load_config()
        return Path(cfg.storage_root) / "logs"
    except Exception:  # noqa: BLE001
        return Path.home() / ".fusion" / "artifacts" / "logs"


def setup_logging(level: int = logging.INFO) -> None:
    # 运维3: 控制台人读格式 + 文件 JSON 结构化轮转。两者并存：
    # 控制台便于本地排查，文件供日志系统采集与长期留存。
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(console)

    log_dir = _resolve_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / _LOG_FILE_NAME,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
        logger.info("Rotating JSON log initialized at %s", log_dir / _LOG_FILE_NAME)
    except OSError as e:
        # 日志目录不可写不致命：退化为仅控制台，大声告警
        logger.warning("Failed to init rotating file log at %s: %s", log_dir, e)


def get_package_version() -> str:
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("fusion-artifacts-engine")
    except Exception:  # noqa: BLE001
        return "0.1.0"
