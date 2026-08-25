import logging
import threading
import time

logger = logging.getLogger(__name__)


class TokenBucket:
    # 令牌桶：恒定速率 rps 补令牌，桶容量 burst 封顶突发。
    # acquire() 拿令牌；空桶返回 False（调用方回 429），不阻塞，不排队。

    def __init__(self, rps: float, burst: int):
        self.rps = max(0.0, float(rps))
        self.burst = max(1, int(burst))
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._rejected = 0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.burst, self._tokens + elapsed * self.rps)

    def acquire(self) -> bool:
        if self.rps <= 0:
            return True
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            self._rejected += 1
            return False

    def snapshot(self) -> dict:
        with self._lock:
            self._refill()
            return {
                "rps": self.rps,
                "burst": self.burst,
                "tokens": round(self._tokens, 2),
                "rejected": self._rejected,
            }


class RateLimiter:
    # 两桶：default 覆盖 JSON-RPC + authed REST；public 覆盖无鉴权公开 share 端点。
    # public 配额独立——公开端点易被刷量，不能拖垮鉴权后端。
    # rps=0 表示不限流（默认）。

    def __init__(self, rps: float = 0, burst: int = 0,
                 public_rps: float = 0, public_burst: int = 0):
        self.default = TokenBucket(rps, burst if burst else max(1, int(rps)))
        self.public = TokenBucket(
            public_rps, public_burst if public_burst else max(1, int(public_rps))
        )
        self.enabled = rps > 0
        self.public_enabled = public_rps > 0

    def check_default(self) -> bool:
        if not self.enabled:
            return True
        ok = self.default.acquire()
        if not ok:
            logger.warning("Rate limit hit (default bucket)")
        return ok

    def check_public(self) -> bool:
        if not self.public_enabled:
            return True
        ok = self.public.acquire()
        if not ok:
            logger.warning("Rate limit hit (public bucket)")
        return ok

    def metrics(self) -> dict:
        return {
            "default": self.default.snapshot(),
            "public": self.public.snapshot(),
        }
