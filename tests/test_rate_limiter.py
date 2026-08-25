import time

from fusion_artifacts_engine.rate_limiter import RateLimiter, TokenBucket


def test_token_bucket_allows_burst():
    # burst=5 桶初始满，连续 5 次 acquire 应全过
    bucket = TokenBucket(rps=1.0, burst=5)
    results = [bucket.acquire() for _ in range(5)]
    assert all(results), f"burst should allow 5 immediate: {results}"
    # 第 6 次应被拒
    assert bucket.acquire() is False


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(rps=100.0, burst=2)
    bucket.acquire()
    bucket.acquire()
    assert bucket.acquire() is False
    # 100 rps = 10ms 补一个令牌，等 30ms 应至少补一个
    time.sleep(0.03)
    assert bucket.acquire() is True


def test_token_bucket_rps_zero_unlimited():
    # rps=0 表示不限流（默认，向后兼容）
    bucket = TokenBucket(rps=0, burst=1)
    for _ in range(100):
        assert bucket.acquire() is True


def test_rate_limiter_disabled_by_default():
    # 默认 rps=0 不限流
    rl = RateLimiter()
    assert rl.check_default() is True
    assert rl.check_public() is True
    assert not rl.enabled
    assert not rl.public_enabled


def test_rate_limiter_default_bucket_enforced():
    rl = RateLimiter(rps=1.0, burst=3)
    assert rl.enabled
    for _ in range(3):
        assert rl.check_default() is True
    assert rl.check_default() is False


def test_rate_limiter_public_bucket_independent():
    # public 桶与 default 桶独立：public 耗尽不影响 default
    rl = RateLimiter(rps=10.0, burst=2, public_rps=10.0, public_burst=2)
    assert rl.check_public() is True
    assert rl.check_public() is True
    assert rl.check_public() is False
    # default 桶未动
    assert rl.check_default() is True


def test_rate_limiter_snapshot_has_counts():
    rl = RateLimiter(rps=1.0, burst=2, public_rps=1.0, public_burst=2)
    rl.check_default()
    rl.check_default()
    rl.check_default()  # 拒一次
    snap = rl.metrics()
    assert snap["default"]["rejected"] >= 1
    assert snap["default"]["burst"] == 2


def test_server_attaches_rate_limiter(tmp_path):
    import socket

    from fusion_artifacts_engine.config import ArtifactEngineConfig
    from fusion_artifacts_engine.engine import ArtifactEngine
    from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        allow_no_auth=True,
    )
    config.rate_limit_rps = 5
    config.rate_limit_burst = 5
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    try:
        rl = server._server._rate_limiter
        assert rl.enabled
        assert rl.default.rps == 5.0
        assert rl.default.burst == 5
    finally:
        server.stop()
        eng.close()
