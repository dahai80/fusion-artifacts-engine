import json
import logging
import os
import stat
import threading
import time
from pathlib import Path

import httpx

from fusion_artifacts_engine.utils import get_package_version

logger = logging.getLogger(__name__)

# #75: web-shell integration (service registration plane added after Config
# Phase 3.3). On startup the engine self-registers its shell manifest with
# fusion-gateway (idempotent upsert), then heartbeats periodically.
# #82: the registration body omits api_key — the gateway issues an
# fm-<module>-<hex> service key (raw_key returned exactly once), which the
# engine persists and uses for heartbeats and re-registration. The proxy
# injects the same fm- key via manifest.api_key, so the engine auth layer
# must accept it (rpc/server.py _is_authed).

_MODULE_TITLE = "Artifacts"
_MODULE_PATH = "/artifacts"
_MODULE_ICON = "Package"
_MODULE_CATEGORY = "platform"
_MODULE_DEVICES = ["desktop"]

# #82: minimum interval between re-registrations after a 403/404 heartbeat.
# A gateway restart wipes the manifest store — one re-registration re-issues a
# key and recovers; but for non-self-healable states (admin-revoked key) the
# cooldown prevents a 15s log/call storm of admin-key attempts.
_REREGISTER_COOLDOWN_SECONDS = 300

# #104: key-drift probe interval. Both the gateway heartbeat plane and the
# re-registration plane accept the "previous key" inside KeyRotationGrace
# (default 24h) — an engine holding a stale key keeps getting 200 heartbeats
# while the data plane (proxy injects the current key) is already rejected;
# symptom is shell-proxy -32600 for up to a full grace window. The probe goes
# through the upsert self-heal plane: the response key_id is always the
# gateway's current key id (same on the retained branch), so comparing it
# with the locally persisted key_id detects drift inside the grace window;
# the follow-up admin re-register triggers a #460 rotation for new plaintext.
_KEY_PROBE_INTERVAL_SECONDS = 600

# _is_authed compares the service key on every request; cache by (path, mtime)
# to avoid repeated disk reads.
_key_file_cache: dict[str, tuple[float, str]] = {}
_key_file_cache_lock = threading.Lock()


def load_service_key(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    key = str(path)
    with _key_file_cache_lock:
        cached = _key_file_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning("gateway service key read failed %s: %s", path, e)
        return ""
    with _key_file_cache_lock:
        _key_file_cache[key] = (mtime, content)
    return content


def save_service_key(path: Path, raw_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(raw_key.strip(), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
    with _key_file_cache_lock:
        _key_file_cache.pop(str(path), None)
    logger.info("gateway service key persisted to %s", path)


def key_id_path(key_path: Path) -> Path:
    # #104: key_id sidecar (<key_file>.keyid). The key file itself stays plain
    # text (rpc/server.py _is_authed reads it directly); the key_id lives in a
    # sidecar so the two never interfere.
    return key_path.with_name(key_path.name + ".keyid")


def load_key_id(key_path: Path) -> str:
    try:
        return key_id_path(key_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_key_id(key_path: Path, key_id: str) -> None:
    p = key_id_path(key_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(key_id.strip(), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(p)
    logger.info("gateway service key_id persisted to %s", p)


class GatewayClient:
    def __init__(self, config, host: str, port: int) -> None:
        self._config = config
        self._host = host
        self._port = port
        self._module_id = config.gateway_module_id
        self._base_url = config.gateway_url.rstrip("/")
        self._interval = max(5, int(config.gateway_heartbeat_interval))
        self._key_path: Path = config.gateway_service_key_path
        self._service_key = load_service_key(self._key_path)
        self._last_reregister = 0.0
        self._last_probe = 0.0
        self._divergent_keyid = False
        self._register_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = httpx.Client(timeout=5.0)

    def _manifest_payload(self) -> dict:
        # #82: omit api_key — the gateway issues/retains the fm- service key
        # based on this (#261 K1). Sending api_key takes the module-supplied
        # credential path and drops the issued key; heartbeats then 403 forever.
        return {
            "id": self._module_id,
            "title": _MODULE_TITLE,
            "title_en": _MODULE_TITLE,
            "version": get_package_version(),
            "path": _MODULE_PATH,
            "icon": _MODULE_ICON,
            "entry": f"/shell/{self._module_id}/",
            # health hits this process directly (same pattern as carvideo/surl):
            # unauthenticated liveness, gateway probes do not need a shell session.
            "health": f"http://{self._host}:{self._port}/healthz",
            "devices": list(_MODULE_DEVICES),
            "category": _MODULE_CATEGORY,
            # #232 dynamic proxy: upstream makes /shell/<id>/ work with no gateway config change
            "upstream": f"http://{self._host}:{self._port}",
        }

    def _register_candidates(self) -> list[tuple[str, str]]:
        # fm- key first (#369 self-heal / idempotent re-register); admin key is
        # for first bootstrap only.
        candidates: list[tuple[str, str]] = []
        if self._service_key:
            candidates.append(("service-key", self._service_key))
        admin_key = self._config.gateway_admin_key or ""
        if admin_key and admin_key != self._service_key:
            candidates.append(("admin-key", admin_key))
        return candidates

    def register(self, force_admin: bool = False) -> bool:
        with self._register_lock:
            if force_admin:
                # #104: drift self-heal only — skip the service-key candidate and
                # re-register with the admin key directly (#460: an admin-plane
                # re-register rotates the key and returns the new plaintext).
                admin_key = self._config.gateway_admin_key or ""
                candidates = [("admin-key", admin_key)] if admin_key else []
            else:
                candidates = self._register_candidates()
            if not candidates:
                logger.warning(
                    "gateway registration skipped: no service key (key_file=%s) and no "
                    "gateway.admin_key configured",
                    self._key_path,
                )
                return False
            url = f"{self._base_url}/api/v1/shell/manifests"
            payload = self._manifest_payload()
            for label, credential in candidates:
                try:
                    resp = self._client.post(
                        url, json=payload, headers={"Authorization": f"Bearer {credential}"}
                    )
                except httpx.HTTPError as e:
                    logger.warning("gateway registration request failed (%s): %s", label, e)
                    continue
                if resp.status_code == 200:
                    self._accept_register_response(resp, label)
                    return True
                logger.info(
                    "gateway registration not accepted (%s, status=%s), trying next credential",
                    label,
                    resp.status_code,
                )
            logger.warning("gateway registration rejected by all credentials")
            return False

    def _accept_register_response(self, resp, label: str) -> None:
        raw_key = ""
        key_id = ""
        try:
            data = json.loads(resp.text or "{}")
            raw_key = data.get("raw_key") or ""
            key_id = data.get("key_id") or ""
        except (ValueError, AttributeError):
            logger.debug("gateway register response not JSON, assuming no issued key")
        if raw_key:
            self._service_key = raw_key
            try:
                save_service_key(self._key_path, raw_key)
            except OSError as e:
                logger.error("gateway service key persist failed %s: %s", self._key_path, e)
            if key_id:
                try:
                    save_key_id(self._key_path, key_id)
                except OSError as e:
                    logger.error("gateway key_id persist failed: %s", e)
        elif label == "service-key" and key_id:
            # #104: on the retained branch the key_id is the gateway's CURRENT
            # key id. Compare with the locally persisted value: a mismatch means
            # the engine holds a stale key (the grace-window "previous key",
            # whose drift the heartbeat plane masks with 200s). probe_key_state
            # then heals via an admin re-register rotation; with no local record
            # the first probe just seeds the baseline.
            stored = load_key_id(self._key_path)
            if not stored:
                try:
                    save_key_id(self._key_path, key_id)
                except OSError as e:
                    logger.error("gateway key_id persist failed: %s", e)
                logger.info("gateway key_id baseline recorded (%s)", key_id)
            elif stored != key_id:
                self._divergent_keyid = True
                logger.warning(
                    "gateway service key divergence detected: engine holds "
                    "key_id=%s, gateway current=%s (stale key masked by "
                    "heartbeat grace window)",
                    stored,
                    key_id,
                )
        elif not self._service_key:
            # A gateway upsert re-register may not issue a new key (record
            # retained) — re-read the key file first (an earlier instance may
            # have persisted a valid key) and reuse it silently when present.
            stored = load_service_key(self._key_path)
            if stored:
                self._service_key = stored
                logger.info(
                    "gateway registered (%s) without raw_key; retained persisted key from %s",
                    label,
                    self._key_path,
                )
            else:
                logger.warning(
                    "gateway registered (%s) but returned no raw_key and no stored key — "
                    "heartbeat will fail; ask an admin to rotate the service key",
                    label,
                )
        else:
            logger.info("gateway registration accepted (%s, existing key retained)", label)

    def heartbeat_once(self) -> bool:
        if not self._service_key:
            # First bootstrap or lost key file: registering doubles as keepalive
            # (upsert refreshes lastHeartbeat). A keyless registration that gets
            # no raw_key would spin in place — apply the same cooldown window to
            # avoid a register+warn loop every interval (seen live in v0.7.3).
            now = time.time()
            if now - self._last_reregister < _REREGISTER_COOLDOWN_SECONDS:
                return False
            self._last_reregister = now
            return self.register()
        url = f"{self._base_url}/api/v1/shell/manifests/{self._module_id}/heartbeat"
        try:
            resp = self._client.post(
                url, json={}, headers={"Authorization": f"Bearer {self._service_key}"}
            )
            if resp.status_code == 200:
                # #110: gateway #485 exposes key drift on the 200 heartbeat
                # itself — X-Fusion-Key-State: stale means the heartbeat matched
                # the rotated-out key inside the grace window while the data
                # plane (proxy injects the current key) is already rejected.
                # Older gateways send no header and keep today's behavior (the
                # 403 path recovers after grace). On the signal, re-register via
                # the admin key immediately (#460 returns fresh plaintext),
                # cooldown-gated against rotation flapping. The heartbeat itself
                # succeeded, so return True and keep the failure counter clean.
                if resp.headers.get("X-Fusion-Key-State") == "stale":
                    now = time.time()
                    if now - self._last_reregister >= _REREGISTER_COOLDOWN_SECONDS:
                        self._last_reregister = now
                        logger.warning(
                            "gateway heartbeat 200 but key state=stale (rotated out, "
                            "grace window), re-registering via admin key module=%s",
                            self._module_id,
                        )
                        self.register(force_admin=True)
                return True
            # 404 manifest_unknown: gateway restart wiped the store;
            # 403 heartbeat_forbidden: no matching service-key record for this
            # module (store rebuilt / key lost). Either way re-register once —
            # the gateway issues a fresh fm- key. Cooldown prevents a 15s loop.
            if resp.status_code in (403, 404):
                now = time.time()
                if now - self._last_reregister < _REREGISTER_COOLDOWN_SECONDS:
                    logger.warning(
                        "gateway heartbeat %d; re-register skipped (cooldown %.0fs left)",
                        resp.status_code,
                        _REREGISTER_COOLDOWN_SECONDS - (now - self._last_reregister),
                    )
                    return False
                self._last_reregister = now
                logger.warning(
                    "gateway heartbeat %d (manifest gone), re-registering module=%s",
                    resp.status_code,
                    self._module_id,
                )
                return self.register()
            logger.warning(
                "gateway heartbeat failed: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
        except httpx.HTTPError as e:
            logger.warning("gateway heartbeat request failed: %s", e)
        return False

    def probe_key_state(self) -> bool:
        # #104: periodic key-drift probe. A 200 heartbeat does not prove the
        # key is current (the grace window lets the previous key pass the
        # heartbeat plane), so on its own interval run one service-key
        # re-register and compare the response key_id; on drift, heal via an
        # admin re-register rotation (#460 returns new plaintext).
        now = time.time()
        if now - self._last_probe < _KEY_PROBE_INTERVAL_SECONDS:
            return True
        self._last_probe = now
        if not self._service_key:
            # No key yet — the heartbeat_once bootstrap registration path
            # handles issuance; nothing to probe.
            return True
        self._divergent_keyid = False
        if not self.register():
            return False
        if self._divergent_keyid:
            logger.warning(
                "self-healing stale gateway service key via admin re-register (module=%s)",
                self._module_id,
            )
            return self.register(force_admin=True)
        return True

    def _loop(self) -> None:
        # Periodic heartbeat. Failures do not exit the loop (transient gateway
        # restarts self-heal); warnings are throttled instead.
        fail_streak = 0
        while not self._stop.wait(self._interval):
            self.probe_key_state()
            ok = self.heartbeat_once()
            if ok:
                fail_streak = 0
                continue
            fail_streak += 1
            if fail_streak == 1 or fail_streak % 10 == 0:
                logger.warning(
                    "gateway heartbeat failing (streak=%d, module=%s)",
                    fail_streak,
                    self._module_id,
                )

    def start(self) -> bool:
        if not self.register():
            # Start the heartbeat thread even on registration failure — if the
            # failure was transient (gateway down), the heartbeat loop retries
            # via the register() bootstrap path with no manual intervention.
            logger.info("gateway registration incomplete, heartbeat loop will retry")
        self._thread = threading.Thread(
            target=self._loop, name="fae-gateway-heartbeat", daemon=True
        )
        self._thread.start()
        logger.info(
            "gateway client started (url=%s module=%s interval=%ds)",
            self._base_url,
            self._module_id,
            self._interval,
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 2)
            self._thread = None
        try:
            self._client.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("gateway client close: %s", e)
        logger.info("gateway client stopped")
