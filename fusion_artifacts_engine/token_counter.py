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
        # L-18: CJK 回退。中文 token ≈ 字数 * 0.6~1.0，按 utf-8 字节 /3 更接近真实，
        # 远好于英文启发式 chars/4（对中文低估 4-8 倍）
        byte_len = len(text.encode("utf-8"))
        estimate = max(0, byte_len // 3)
        logger.warning(
            "tiktoken encode failed, falling back to utf8-bytes/3 estimate: %d",
            estimate,
        )
        return estimate
