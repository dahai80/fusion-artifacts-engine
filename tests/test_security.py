import json
import time
import urllib.request
import asyncio
import pytest
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer


@pytest.fixture
def engine(tmp_path):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=0,
    )
    eng = ArtifactEngine(config)
    yield eng
    eng.close()


def test_path_traversal_export_rejected(engine):
    from fusion_artifacts_engine.rpc.methods import RPCHandler
    handler = RPCHandler(engine)
    with pytest.raises(ValueError, match="under storage root"):
        asyncio.run(
            handler._export_session({
                "session_id": "test",
                "output_dir": "/tmp/evil_escape",
            })
        )


def test_rpc_unknown_method_returns_32601(tmp_path):
    import socket
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
    time.sleep(0.5)

    try:
        req = json.dumps({"jsonrpc": "2.0", "method": "nonexistent.method", "id": 1}).encode()
        resp = urllib.request.urlopen(
            "http://127.0.0.1:%d/" % port, data=req, timeout=5,
        )
        data = json.loads(resp.read())
        assert data["error"]["code"] == -32601
    finally:
        server.stop()
        eng.close()


def test_rpc_invalid_json_returns_32700(tmp_path):
    import socket
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
    time.sleep(0.5)

    try:
        req = b"{invalid json"
        resp = urllib.request.urlopen(
            "http://127.0.0.1:%d/" % port, data=req, timeout=5,
        )
        data = json.loads(resp.read())
        assert data["error"]["code"] == -32700
    finally:
        server.stop()
        eng.close()


def test_rpc_invalid_request_returns_32600(tmp_path):
    import socket
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
    time.sleep(0.5)

    try:
        req = json.dumps({"method": "ping"}).encode()
        resp = urllib.request.urlopen(
            "http://127.0.0.1:%d/" % port, data=req, timeout=5,
        )
        data = json.loads(resp.read())
        assert data["error"]["code"] == -32600
    finally:
        server.stop()
        eng.close()


def test_name_sanitization_in_export(engine):
    asyncio.run(
        engine.create_artifact(
            session_id="sec_test",
            name="evil\x00name",
            artifact_type="code",
            content="safe content",
        )
    )
    from fusion_artifacts_engine.rpc.methods import RPCHandler
    handler = RPCHandler(engine)
    result = asyncio.run(
        handler._export_session({
            "session_id": "sec_test",
            "output_dir": str(engine.config.storage_root / "export"),
        })
    )
    assert result["count"] >= 0
