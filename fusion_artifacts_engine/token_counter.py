import logging

import tiktoken

logger = logging.getLogger(__name__)

_encoder = None


def _get_encoder():
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        return len(_get_encoder().encode(text))
    except Exception:  # noqa: BLE001
        logger.warning("tiktoken encode failed, falling back to char/4 estimate")
        return max(0, len(text) // 4)
