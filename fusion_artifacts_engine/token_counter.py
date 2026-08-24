import hashlib
import logging
import threading
from collections import OrderedDict

import tiktoken

logger = logging.getLogger(__name__)

_encoder = None

# H6: token 计数缓存。版本内容不可变（create 后不修改），同 content 重复计数命中缓存
# 省 tiktoken BPE 分词开销。LRU + 上限防内存无界增长。短文本（<256B）跳过缓存，
# 缓存自身开销可能超过分词收益。
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
    # H6: 短文本不缓存（缓存开销 > 分词收益）
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
        # L-18: CJK 回退。中文 token ≈ 字数 * 0.6~1.0，按 utf-8 字节 /3 更接近真实，
        # 远好于英文启发式 chars/4（对中文低估 4-8 倍）
        byte_len = len(text.encode("utf-8"))
        n = max(0, byte_len // 3)
        logger.warning(
            "tiktoken encode failed, falling back to utf8-bytes/3 estimate: %d", n
        )
        # 编码失败不缓存（内容可能触发重复告警，但避免缓存错误估算）
        return n
    if cacheable:
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE[key] = n
            _TOKEN_CACHE.move_to_end(key)
            if len(_TOKEN_CACHE) > _TOKEN_CACHE_MAX:
                _TOKEN_CACHE.popitem(last=False)
    return n


def clear_token_cache() -> None:
    # H6: 测试/运维可清缓存
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.clear()
