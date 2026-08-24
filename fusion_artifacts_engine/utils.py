import logging
import re
import secrets
import string

logger = logging.getLogger(__name__)

# 路径段安全字符集：artifact_id / share_id / folder_id 等会拼进文件系统路径的标识符 (C-6, C-10)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
# id_prefix 允许字符：字母数字下划线连字符，禁止 / .. 空字节等 (C-6)
_SAFE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_package_version() -> str:
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("fusion-artifacts-engine")
    except Exception:  # noqa: BLE001
        return "0.1.0"
