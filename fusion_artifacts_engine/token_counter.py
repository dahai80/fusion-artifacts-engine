import hashlib
import logging
import threading
from collections import OrderedDict

import tiktoken

logger = logging.getLogger(__name__)

_encoder = None

# H6: token count cache. Version content is immutable (never modified after create), so repeated counts of the
# same content hit the cache and skip the tiktoken BPE tokenization cost. LRU + cap prevents unbounded memory
# growth. Short texts (<256B) skip the cache — cache overhead could exceed the tokenization savings.
_TOKEN_CACHE: "OrderedDict[str, int]" = OrderedDict()
_TOKEN_CACHE_LOCK = threading.Lock()
_TOKEN_CACHE_MAX = 2048
_TOKEN_CACHE_MIN_BYTES = 256


def _get_encoder():
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _cache_key(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def count_tokens(text: str) -> int:
    if not text:
        return 0
    # H6: short texts are not cached (cache overhead > tokenization savings)
    cacheable = len(text) >= _TOKEN_CACHE_MIN_BYTES
    if cacheable:
        key = _cache_key(text)
        with _TOKEN_CACHE_LOCK:
            cached = _TOKEN_CACHE.get(key)
            if cached is not None:
                _TOKEN_CACHE.move_to_end(key)
                return cached
    try:
        n = len(_get_encoder().encode(text))
    except Exception:  # noqa: BLE001
        # L-18: CJK fallback. Chinese tokens ≈ char count * 0.6~1.0; utf-8 bytes /3 is closer to reality
        # and far better than the English heuristic chars/4 (which underestimates Chinese by 4-8x)
        byte_len = len(text.encode("utf-8"))
        n = max(0, byte_len // 3)
        logger.warning(
            "tiktoken encode failed, falling back to utf8-bytes/3 estimate: %d", n
        )
        # Do not cache on encode failure (content may trigger repeated warnings, but avoids caching a wrong estimate)
        return n
    if cacheable:
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE[key] = n
            _TOKEN_CACHE.move_to_end(key)
            if len(_TOKEN_CACHE) > _TOKEN_CACHE_MAX:
                _TOKEN_CACHE.popitem(last=False)
    return n


def estimate_tokens(text: str) -> int:
    # F3: cheap estimate, only for compactor to skip full tiktoken encoding of content that is clearly over budget.
    # No upper/lower bound guaranteed — the final within-budget decision is validated precisely by count_tokens.
    if not text:
        return 0
    char_len = len(text)
    byte_len = len(text.encode("utf-8"))
    if byte_len > char_len * 1.5:
        return max(1, byte_len // 3)
    return max(1, char_len // 4)


def clear_token_cache() -> None:
    # H6: cache clear for tests/ops
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.clear()
