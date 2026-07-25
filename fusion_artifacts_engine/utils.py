import logging
import secrets
import string


def generate_artifact_id(prefix: str = "art_") -> str:
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(8))
    artifact_id = f"{prefix}{suffix}"
    logging.getLogger(__name__).debug(f"Generated artifact ID: {artifact_id}")
    return artifact_id


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
