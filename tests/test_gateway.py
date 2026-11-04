import json
import socket
import time
import urllib.error
import urllib.request

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.gateway_client import (
    GatewayClient,
    load_key_id,
    load_service_key,
    save_key_id,
)
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

API_KEY = "fae-gw-test-key-abcdef0123456789"
SERVICE_KEY = "fm-fusion-artifacts-0123456789abcdef0123456789abcdef"


class _FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeHttpClient:
    def __init__(self, responses):
        # responses: list of (status_code, text) or (status_code, text, headers)
        # returned per call in order
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self._responses.pop(0) if self._responses else (200, "")
        status, text = item[0], item[1]
        resp_headers = item[2] if len(item) > 2 else {}
        return _FakeResponse(status, text, resp_headers)

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
    r = urllib.request.Request(f"http://127.0.0.1:{port}/", data=req, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_bearer_auth_accepted(tmp_path):
    # #75: the gateway /shell/ proxy injects only Authorization: Bearer — the engine must accept it
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


def test_gateway_issued_service_key_accepted(tmp_path):
    # #82: the gateway-issued fm- key is the proxy-injected credential; the engine auth layer must accept it
    port = _ephemeral_port()
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=port,
        api_key=API_KEY,
    )
    key_path = config.gateway_service_key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(SERVICE_KEY, encoding="utf-8")
    eng = ArtifactEngine(config)
    server = ArtifactRPCServer(eng, port=port)
    server.start_async()
    time.sleep(0.5)
    try:
        status, data = _post_ping(port, {"Authorization": f"Bearer {SERVICE_KEY}"})
        assert status == 200
        assert data["result"]["pong"] is True
        status, _ = _post_ping(port, {"Authorization": "Bearer fm-other-module-ffff"})
        assert status == 401
    finally:
        server.stop()
        eng.close()


def test_x_api_key_still_works(tmp_path):
    # Back-compat: the legacy X-API-Key channel is unaffected by #75
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


def _write_service_key(client, raw_key=SERVICE_KEY):
    client._key_path.parent.mkdir(parents=True, exist_ok=True)
    client._key_path.write_text(raw_key, encoding="utf-8")
    client._service_key = raw_key


def test_gateway_register_admin_bootstrap_issues_key(tmp_path):
    # #82: body without api_key — gateway issues an fm- key (raw_key returned once)
    client = _make_client(tmp_path)
    fake = _FakeHttpClient(
        [(200, json.dumps({"code": "ok", "key_id": "abc", "raw_key": SERVICE_KEY}))]
    )
    client._client = fake
    assert client.register() is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/v1/shell/manifests")
    assert call["headers"]["Authorization"] == "Bearer gw-admin-key"
    body = call["json"]
    assert body["id"] == "fusion-artifacts"
    assert body["entry"] == "/shell/fusion-artifacts/"
    assert body["upstream"] == "http://127.0.0.1:11451"
    assert body["health"] == "http://127.0.0.1:11451/healthz"
    assert "api_key" not in body
    assert body["icon"] == "Package"
    assert body["devices"] == ["desktop"]
    assert client._service_key == SERVICE_KEY
    assert load_service_key(client._key_path) == SERVICE_KEY


def test_gateway_register_prefers_stored_service_key(tmp_path):
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient([(200, json.dumps({"code": "ok"}))])
    client._client = fake
    assert client.register() is True
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {SERVICE_KEY}"


def test_gateway_register_falls_back_to_admin_key(tmp_path):
    # fm- key present but dead on the gateway side (restart) — fall back to admin key bootstrap
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient(
        [
            (403, "forbidden"),
            (200, json.dumps({"code": "ok", "raw_key": SERVICE_KEY + "b"})),
        ]
    )
    client._client = fake
    assert client.register() is True
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {SERVICE_KEY}"
    assert fake.calls[1]["headers"]["Authorization"] == "Bearer gw-admin-key"
    assert client._service_key == SERVICE_KEY + "b"


def test_gateway_register_no_credentials_fails(tmp_path):
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
    assert fake.calls == []


def test_gateway_register_no_raw_key_retains_persisted_key(tmp_path):
    # v0.7.3: a gateway upsert re-register issues no raw_key (record retained) —
    # with an empty in-memory key the engine must re-read the key file and reuse
    # it, not misreport "no stored key"
    client = _make_client(tmp_path)
    client._key_path.parent.mkdir(parents=True, exist_ok=True)
    client._key_path.write_text(SERVICE_KEY, encoding="utf-8")
    client._service_key = ""
    fake = _FakeHttpClient([(200, json.dumps({"code": "ok"}))])
    client._client = fake
    assert client.register() is True
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer gw-admin-key"
    assert client._service_key == SERVICE_KEY


def test_gateway_heartbeat_empty_key_cooldown(tmp_path):
    # v0.7.3: a keyless heartbeat's registration path is cooldown-gated too —
    # prevents a 15s register+warn loop
    client = _make_client(tmp_path)
    client._service_key = ""
    client._last_reregister = time.time()
    fake = _FakeHttpClient([])
    client._client = fake
    assert client.heartbeat_once() is False
    assert fake.calls == []


def test_gateway_heartbeat_uses_service_key(tmp_path):
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient([(200, json.dumps({"code": "ok"}))])
    client._client = fake
    assert client.heartbeat_once() is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/v1/shell/manifests/fusion-artifacts/heartbeat")
    assert call["headers"]["Authorization"] == f"Bearer {SERVICE_KEY}"


def test_gateway_heartbeat_without_key_registers(tmp_path):
    client = _make_client(tmp_path)
    fake = _FakeHttpClient([(200, json.dumps({"code": "ok", "raw_key": SERVICE_KEY}))])
    client._client = fake
    assert client.heartbeat_once() is True
    assert fake.calls[0]["url"].endswith("/api/v1/shell/manifests")
    assert client._service_key == SERVICE_KEY


def test_gateway_heartbeat_404_reregisters(tmp_path):
    # #369 self-heal: heartbeat 404 manifest_unknown — re-register
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient([(404, "manifest_unknown"), (200, json.dumps({"code": "ok"}))])
    client._client = fake
    assert client.heartbeat_once() is True
    assert fake.calls[0]["url"].endswith("/heartbeat")
    assert fake.calls[1]["url"].endswith("/api/v1/shell/manifests")


def test_gateway_heartbeat_403_reregisters(tmp_path):
    # #75 regression seen live: after a gateway restart wipes the manifest store,
    # heartbeat 403 heartbeat_forbidden (no service-key record for the module)
    # must also trigger re-registration, or the menu disappears until restart
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient(
        [(403, '{"code":"heartbeat_forbidden"}'), (200, json.dumps({"code": "ok"}))]
    )
    client._client = fake
    assert client.heartbeat_once() is True
    assert fake.calls[0]["url"].endswith("/heartbeat")
    assert fake.calls[1]["url"].endswith("/api/v1/shell/manifests")


def test_gateway_heartbeat_403_cooldown_no_loop(tmp_path):
    # #82 acceptance: consecutive 403s trigger exactly one re-register; during
    # cooldown heartbeat failures must not hit the registration endpoint
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient(
        [
            (403, '{"code":"heartbeat_forbidden"}'),
            (200, json.dumps({"code": "ok"})),
            (403, '{"code":"heartbeat_forbidden"}'),
        ]
    )
    client._client = fake
    assert client.heartbeat_once() is True
    assert client.heartbeat_once() is False
    assert len(fake.calls) == 3
    assert fake.calls[-1]["url"].endswith("/heartbeat")


def test_gateway_probe_detects_keyid_divergence_and_heals(tmp_path):
    # #104: engine holds a stale key (heartbeat plane keeps returning 200 inside
    # the grace window, masking the drift) — probe compares the upsert response
    # key_id with the local baseline; on mismatch heal via admin re-register
    client = _make_client(tmp_path)
    _write_service_key(client)
    save_key_id(client._key_path, "kid-old")
    fake = _FakeHttpClient(
        [
            (200, json.dumps({"code": "ok", "key_id": "kid-new", "raw_key": ""})),
            (
                200,
                json.dumps({"code": "ok", "key_id": "kid-rot", "raw_key": SERVICE_KEY + "new"}),
            ),
        ]
    )
    client._client = fake
    assert client.probe_key_state() is True
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {SERVICE_KEY}"
    assert fake.calls[1]["headers"]["Authorization"] == "Bearer gw-admin-key"
    assert client._service_key == SERVICE_KEY + "new"
    assert load_service_key(client._key_path) == SERVICE_KEY + "new"
    assert load_key_id(client._key_path) == "kid-rot"


def test_gateway_probe_no_action_when_keyid_matches(tmp_path):
    # No drift: the probe does one idempotent re-register, no rotation, no warn
    client = _make_client(tmp_path)
    _write_service_key(client)
    save_key_id(client._key_path, "kid-1")
    fake = _FakeHttpClient([(200, json.dumps({"code": "ok", "key_id": "kid-1"}))])
    client._client = fake
    assert client.probe_key_state() is True
    assert len(fake.calls) == 1
    assert client._service_key == SERVICE_KEY


def test_gateway_probe_seeds_keyid_baseline(tmp_path):
    # First probe after upgrade: no local key_id baseline — seed it, rotate nothing
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient([(200, json.dumps({"code": "ok", "key_id": "kid-new"}))])
    client._client = fake
    assert client.probe_key_state() is True
    assert len(fake.calls) == 1
    assert load_key_id(client._key_path) == "kid-new"
    assert client._service_key == SERVICE_KEY


def test_gateway_probe_cooldown_skips(tmp_path):
    # The probe has its own cooldown window and does not upsert on heartbeat cadence
    client = _make_client(tmp_path)
    _write_service_key(client)
    client._last_probe = time.time()
    fake = _FakeHttpClient([])
    client._client = fake
    assert client.probe_key_state() is True
    assert fake.calls == []


def test_gateway_probe_skips_when_no_service_key(tmp_path):
    # Probe stays out when no key exists — heartbeat_once's bootstrap registration handles issuance
    client = _make_client(tmp_path)
    client._service_key = ""
    fake = _FakeHttpClient([])
    client._client = fake
    assert client.probe_key_state() is True
    assert fake.calls == []


def test_gateway_register_issuance_persists_key_id(tmp_path):
    # #104: a raw_key issuance persists the key_id baseline alongside
    client = _make_client(tmp_path)
    fake = _FakeHttpClient(
        [(200, json.dumps({"code": "ok", "key_id": "kid-abc", "raw_key": SERVICE_KEY}))]
    )
    client._client = fake
    assert client.register() is True
    assert load_key_id(client._key_path) == "kid-abc"


def test_gateway_heartbeat_stale_signal_heals(tmp_path):
    # #110: gateway #485 heartbeat 200 + X-Fusion-Key-State: stale = matched the
    # rotated-out key inside the grace window — re-register via admin key
    # immediately for the new plaintext, no waiting for grace to expire
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient(
        [
            (
                200,
                json.dumps({"code": "ok"}),
                {"X-Fusion-Key-State": "stale"},
            ),
            (
                200,
                json.dumps({"code": "ok", "key_id": "kid-new", "raw_key": SERVICE_KEY + "b"}),
            ),
        ]
    )
    client._client = fake
    assert client.heartbeat_once() is True
    assert fake.calls[0]["url"].endswith("/heartbeat")
    assert fake.calls[1]["url"].endswith("/api/v1/shell/manifests")
    assert fake.calls[1]["headers"]["Authorization"] == "Bearer gw-admin-key"
    assert client._service_key == SERVICE_KEY + "b"


def test_gateway_heartbeat_stale_signal_cooldown(tmp_path):
    # #110: the stale signal is cooldown-gated by the re-register window — rotation flapping cannot storm
    client = _make_client(tmp_path)
    _write_service_key(client)
    client._last_reregister = time.time()
    fake = _FakeHttpClient([(200, json.dumps({"code": "ok"}), {"X-Fusion-Key-State": "stale"})])
    client._client = fake
    assert client.heartbeat_once() is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith("/heartbeat")


def test_gateway_heartbeat_current_state_no_reregister(tmp_path):
    # #110 regression: no signal header (older gateway) or current state — no re-registration
    client = _make_client(tmp_path)
    _write_service_key(client)
    fake = _FakeHttpClient(
        [
            (200, json.dumps({"code": "ok"}), {"X-Fusion-Key-State": "current"}),
            (200, json.dumps({"code": "ok"})),
        ]
    )
    client._client = fake
    assert client.heartbeat_once() is True
    assert client.heartbeat_once() is True
    assert len(fake.calls) == 2
    assert all(c["url"].endswith("/heartbeat") for c in fake.calls)


def test_gateway_start_stop(tmp_path):
    client = _make_client(tmp_path, gateway_heartbeat_interval=5)
    fake = _FakeHttpClient([(200, ""), (200, "")])
    client._client = fake
    assert client.start() is True
    assert client._thread is not None
    client.stop()
    assert client._thread is None


def test_gateway_config_env_override(monkeypatch, tmp_path):
    # #75: gateway config section env overrides
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
