import json
import logging
import os
import re
import secrets
import string
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

# Safe charset for path segments: artifact_id / share_id / folder_id and other identifiers that get joined into filesystem paths (C-6, C-10)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
# id_prefix allowed characters: alphanumerics, underscore, hyphen; blocks / .. NUL etc. (C-6)
_SAFE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Ops-3: log rotation params. 10MB per file, 5 rotating backups retained.
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
    # Ops-3: structured JSON logs for ELK/Loki ingestion and search.
    # One JSON object per line: ts / level / logger / msg / optional fields.

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


class LogSanitizerFilter(logging.Filter):
    # LOW-3/CWE-117: log injection protection. User input may contain \n / \r; unfiltered it can forge
    # new lines in console/text logs (e.g. artifact name "ok\n[ERROR] fake"). This filter replaces CR/LF
    # in msg and args with literal \\r \\n so injected text stays on the same log line. The JSON file
    # formatter already escapes \n into literal two-char sequences; this filter mainly covers the console
    # text formatter. The two coexist without side effects.

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = record.msg.replace("\r", "\\r").replace("\n", "\\n")
        if record.args:
            record.args = tuple(
                a.replace("\r", "\\r").replace("\n", "\\n")
                if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


def _resolve_log_dir() -> Path | None:
    # Ops-3: log dir resolution order — env var first, then storage_root/logs, finally ~/.fusion/artifacts/logs.
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
    # Ops-3: human-readable console format + rotating structured JSON file. Both coexist:
    # console for local troubleshooting, file for log-system ingestion and long-term retention.
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    # LOW-3/CWE-117: global log-injection filter, strips CR/LF from user input to prevent forged log lines
    root.addFilter(LogSanitizerFilter())

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
        # Unwritable log dir is not fatal: degrade to console-only, fail loudly
        logger.warning("Failed to init rotating file log at %s: %s", log_dir, e)


def get_package_version() -> str:
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("fusion-artifacts-engine")
    except Exception:  # noqa: BLE001
        return "0.1.0"
