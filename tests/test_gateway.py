import json
import socket
import time
import urllib.error
import urllib.request

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.gateway_client import GatewayClient
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

API_KEY = "fae-gw-test-key-abcdef0123456789"


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeHttpClient:
    def __init__(self, responses):
        # responses: list of (status_code, text) returned per call in order
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        status, text = self._responses.pop(0) if self._responses else (200, "")
        return _FakeResponse(status, text)

    def close(self):
        pass


def _ephemeral_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _post_ping(port, headers):
    req = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}).encode()
    r = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=req, headers=headers
    )
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_bearer_auth_accepted(tmp_path):
    # #75: gateway /shell/ 代理只注入 Authorization: Bearer——引擎须接受
    port = _ephemeral_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        api_key=API_KEY,
    )
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    try:
        status, data = _post_ping(port, {"Authorization": f"Bearer {API_KEY}"})
        assert status == 200
        assert data["result"]["pong"] is True
    finally:
        server.stop()
        eng.close()


def test_bearer_auth_wrong_key_rejected(tmp_path):
    port = _ephemeral_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        api_key=API_KEY,
    )
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    try:
        status, data = _post_ping(port, {"Authorization": "Bearer wrong-key"})
        assert status == 401
        assert data["error"]["code"] == -32600
    finally:
        server.stop()
        eng.close()


def test_x_api_key_still_works(tmp_path):
    # 向后兼容：传统 X-API-Key 通道不受 #75 改动影响
    port = _ephemeral_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        api_key=API_KEY,
    )
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    try:
        status, data = _post_ping(port, {"X-API-Key": API_KEY})
        assert status == 200
        assert data["result"]["pong"] is True
    finally:
        server.stop()
        eng.close()


def _make_client(tmp_path, **overrides):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        api_key=API_KEY,
        gateway_enabled=True,
        gateway_module_id="fusion-artifacts",
        gateway_admin_key="gw-admin-key",
        **overrides,
    )
    return GatewayClient(config, "127.0.0.1", 11451)


def test_gateway_register_self_key_success(tmp_path):
    client = _make_client(tmp_path)
    fake = _FakeHttpClient([(200, '{"code":"ok"}')])
    client._client = fake
    assert client.register() is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/v1/shell/manifests")
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
    body = call["json"]
    assert body["id"] == "fusion-artifacts"
    assert body["entry"] == "/shell/fusion-artifacts/"
    assert body["upstream"] == "http://127.0.0.1:11451"
    assert body["health"] == "http://127.0.0.1:11451/healthz"
    assert body["api_key"] == API_KEY
    assert body["icon"] == "Package"
    assert body["devices"] == ["desktop"]


def test_gateway_register_falls_back_to_admin_key(tmp_path):
    # 首次注册：自身 key 被 401（registry 里还没有它）→ admin_key 引导
    client = _make_client(tmp_path)
    fake = _FakeHttpClient([(401, "unauthorized"), (200, '{"code":"ok"}')])
    client._client = fake
    assert client.register() is True
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert fake.calls[1]["headers"]["Authorization"] == "Bearer gw-admin-key"


def test_gateway_register_no_admin_key_fails(tmp_path):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        api_key=API_KEY,
        gateway_module_id="fusion-artifacts",
        gateway_admin_key=None,
    )
    client = GatewayClient(config, "127.0.0.1", 11451)
    fake = _FakeHttpClient([(401, "unauthorized")])
    client._client = fake
    assert client.register() is False


def test_gateway_register_skipped_without_api_key(tmp_path):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        gateway_module_id="fusion-artifacts",
        gateway_admin_key="gw-admin-key",
    )
    client = GatewayClient(config, "127.0.0.1", 11451)
    fake = _FakeHttpClient([])
    client._client = fake
    assert client.register() is False
    assert fake.calls == []


def test_gateway_heartbeat_ok(tmp_path):
    client = _make_client(tmp_path)
    fake = _FakeHttpClient([(200, '{"code":"ok"}')])
    client._client = fake
    assert client.heartbeat_once() is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/v1/shell/manifests/fusion-artifacts/heartbeat")
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"


def test_gateway_heartbeat_404_reregisters(tmp_path):
    # #369 self-heal：心跳 404 manifest_unknown → 再注册
    client = _make_client(tmp_path)
    fake = _FakeHttpClient([(404, "manifest_unknown"), (200, '{"code":"ok"}')])
    client._client = fake
    assert client.heartbeat_once() is True
    assert fake.calls[0]["url"].endswith("/heartbeat")
    assert fake.calls[1]["url"].endswith("/api/v1/shell/manifests")


def test_gateway_start_stop(tmp_path):
    client = _make_client(tmp_path, gateway_heartbeat_interval=5)
    fake = _FakeHttpClient([(200, ""), (200, "")])
    client._client = fake
    assert client.start() is True
    assert client._thread is not None
    client.stop()
    assert client._thread is None


def test_gateway_config_env_override(monkeypatch, tmp_path):
    # #75: gateway 配置节 env 覆盖
    from fusion_artifacts_engine.config import load_config

    monkeypatch.setenv("FUSION_ARTIFACTS_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("FUSION_ARTIFACTS_GATEWAY_URL", "http://127.0.0.1:19999")
    monkeypatch.setenv("FUSION_ARTIFACTS_GATEWAY_ADMIN_KEY", "env-admin-key")
    monkeypatch.setenv("FUSION_ARTIFACTS_GATEWAY_HEARTBEAT_INTERVAL", "20")
    config = load_config()
    assert config.gateway_enabled is True
    assert config.gateway_url == "http://127.0.0.1:19999"
    assert config.gateway_admin_key == "env-admin-key"
    assert config.gateway_heartbeat_interval == 20


def test_gateway_config_yaml_section(monkeypatch, tmp_path):
    import yaml as yaml_mod

    from fusion_artifacts_engine.config import load_config

    cfg_file = tmp_path / "cfg.yaml"
    with open(cfg_file, "w") as f:
        yaml_mod.dump(
            {
                "gateway": {
                    "enabled": True,
                    "url": "http://127.0.0.1:11432",
                    "module_id": "fusion-artifacts",
                    "admin_key": "yaml-admin-key",
                    "heartbeat_interval": 12,
                }
            },
            f,
        )
    config = load_config(user_config_path=cfg_file)
    assert config.gateway_enabled is True
    assert config.gateway_heartbeat_interval == 12
    assert config.gateway_admin_key == "yaml-admin-key"
