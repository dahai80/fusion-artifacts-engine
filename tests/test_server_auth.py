import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT_AUTH = 19900


@pytest.fixture(scope="module")
def auth_server():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=False)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=PORT_AUTH)
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", "test-secret-key"):
        server.start_async()
        time.sleep(0.5)
        yield server, engine
        server.stop()
    engine.close()
    shutil.rmtree(tmp)


PORT_NO_AUTH_REJECT = 19901


@pytest.fixture(scope="module")
def no_auth_reject_server():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=False)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=PORT_NO_AUTH_REJECT)
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", ""):
        server.start_async()
        time.sleep(0.5)
        yield server, engine
        server.stop()
    engine.close()
    shutil.rmtree(tmp)


def test_rpc_auth_success(auth_server):
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", "test-secret-key"):
        resp = httpx.post(
            f"http://127.0.0.1:{PORT_AUTH}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-API-Key": "test-secret-key"},
            timeout=5.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data


def test_rpc_auth_denied(auth_server):
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", "test-secret-key"):
        resp = httpx.post(
            f"http://127.0.0.1:{PORT_AUTH}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-API-Key": "wrong-key"},
            timeout=5.0,
        )
        assert resp.status_code == 401


def test_rpc_auth_no_key(auth_server):
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", "test-secret-key"):
        resp = httpx.post(
            f"http://127.0.0.1:{PORT_AUTH}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            timeout=5.0,
        )
        assert resp.status_code == 401


def test_rest_auth_denied(auth_server):
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", "test-secret-key"):
        resp = httpx.get(
            f"http://127.0.0.1:{PORT_AUTH}/api/v1/artifacts",
            headers={"X-API-Key": "wrong-key"},
            timeout=5.0,
        )
        assert resp.status_code == 401


def test_rest_post_auth_denied(auth_server):
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", "test-secret-key"):
        resp = httpx.post(
            f"http://127.0.0.1:{PORT_AUTH}/api/v1/artifacts/create",
            json={"session_id": "s1", "name": "t.py", "type": "code", "content": "x"},
            headers={"X-API-Key": "wrong-key"},
            timeout=5.0,
        )
        assert resp.status_code == 401


def test_no_api_key_no_auth_reject(no_auth_reject_server):
    with patch("fusion_artifacts_engine.rpc.server._API_KEY", ""):
        resp = httpx.post(
            f"http://127.0.0.1:{PORT_NO_AUTH_REJECT}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            timeout=5.0,
        )
        assert resp.status_code == 401
