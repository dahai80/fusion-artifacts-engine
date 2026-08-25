from fusion_artifacts_engine.metrics import MetricsRegistry, get_metrics, reset_metrics


def test_counter_increment():
    reg = MetricsRegistry()
    reg.inc_counter("rpc_requests_total")
    reg.inc_counter("rpc_requests_total", amount=2)
    text = reg.expose()
    assert "rpc_requests_total 3" in text


def test_counter_with_labels():
    reg = MetricsRegistry()
    reg.inc_counter("rpc_requests_total", labels={"path": "jsonrpc"})
    reg.inc_counter("rpc_requests_total", labels={"path": "jsonrpc"})
    reg.inc_counter("rpc_requests_total", labels={"path": "rest"})
    text = reg.expose()
    assert 'rpc_requests_total{path="jsonrpc"} 2' in text
    assert 'rpc_requests_total{path="rest"} 1' in text


def test_gauge_set_inc_dec():
    reg = MetricsRegistry()
    reg.set_gauge("rpc_active_conns", 5)
    reg.inc_gauge("rpc_active_conns")
    reg.dec_gauge("rpc_active_conns", 2)
    text = reg.expose()
    assert "rpc_active_conns 4" in text


def test_histogram_observe():
    reg = MetricsRegistry()
    reg.observe("rpc_request_latency_seconds", 0.05)
    reg.observe("rpc_request_latency_seconds", 1.5)
    text = reg.expose()
    assert 'rpc_request_latency_seconds_bucket{le="0.05"}' in text
    assert "rpc_request_latency_seconds_count 2" in text
    assert "rpc_request_latency_seconds_sum 1.55" in text
    assert 'rpc_request_latency_seconds_bucket{le="+Inf"} 2' in text


def test_expose_format_types():
    reg = MetricsRegistry()
    reg.inc_counter("rpc_requests_total")
    text = reg.expose()
    assert "# TYPE rpc_requests_total counter" in text
    assert "# TYPE rpc_active_conns gauge" in text
    assert "# TYPE rpc_request_latency_seconds histogram" in text


def test_get_metrics_singleton():
    reset_metrics()
    m1 = get_metrics()
    m2 = get_metrics()
    assert m1 is m2


def test_metrics_endpoint_serves_text(tmp_path):
    import socket

    import httpx

    from fusion_artifacts_engine.config import ArtifactEngineConfig
    from fusion_artifacts_engine.engine import ArtifactEngine
    from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

    reset_metrics()
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
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    import time
    time.sleep(0.5)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
        assert "rpc_requests_total" in r.text
        assert "# TYPE rpc_request_latency_seconds histogram" in r.text
    finally:
        server.stop()
        eng.close()
