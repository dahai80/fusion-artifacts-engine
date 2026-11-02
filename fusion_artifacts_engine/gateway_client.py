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

# #75: web-shell 集成（Config Phase 3.3 之后的服务注册面）。
# 引擎启动时向 fusion-gateway 自注册 shell manifest（幂等 upsert），之后周期心跳。
# #82: 注册体不带 api_key —— gateway 签发 fm-<module>-<hex> service key（raw_key
# 仅返回一次），引擎持久化后用该 key 心跳与再注册。代理经 manifest.api_key 注入
# 同一 fm- key，引擎鉴权层须接受它（rpc/server.py _is_authed）。

_MODULE_TITLE = "Artifacts"
_MODULE_PATH = "/artifacts"
_MODULE_ICON = "Package"
_MODULE_CATEGORY = "platform"
_MODULE_DEVICES = ["desktop"]

# #82: 心跳命中 403/404 后再注册的最小间隔。gateway 重启会清空 manifest store，
# 再注册后签发新 key 即可恢复；但 key 被管理员 revoke 等不可自愈场景下避免每
# 15s 一次 admin-key 调用的日志/调用风暴。
_REREGISTER_COOLDOWN_SECONDS = 300

# _is_authed 每次请求都要比对 service key；按 (path, mtime) 缓存避免重复读盘。
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
        self._register_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = httpx.Client(timeout=5.0)

    def _manifest_payload(self) -> dict:
        # #82: 不带 api_key —— gateway 据此签发/保留 fm- service key（#261 K1）。
        # 带 api_key 会走 module-supplied 凭据路径并丢弃已签发 key，心跳永久 403。
        return {
            "id": self._module_id,
            "title": _MODULE_TITLE,
            "title_en": _MODULE_TITLE,
            "version": get_package_version(),
            "path": _MODULE_PATH,
            "icon": _MODULE_ICON,
            "entry": f"/shell/{self._module_id}/",
            # health 直连本进程（与 carvideo/surl 模式一致）：无鉴权 liveness，
            # gateway 探针不依赖 shell session。
            "health": f"http://{self._host}:{self._port}/healthz",
            "devices": list(_MODULE_DEVICES),
            "category": _MODULE_CATEGORY,
            # #232 动态代理：upstream 使 /shell/<id>/ 无需改 gateway config
            "upstream": f"http://{self._host}:{self._port}",
        }

    def _register_candidates(self) -> list[tuple[str, str]]:
        # fm- key 优先（#369 self-heal / 幂等再注册），admin key 仅首次引导。
        candidates: list[tuple[str, str]] = []
        if self._service_key:
            candidates.append(("service-key", self._service_key))
        admin_key = self._config.gateway_admin_key or ""
        if admin_key and admin_key != self._service_key:
            candidates.append(("admin-key", admin_key))
        return candidates

    def register(self) -> bool:
        with self._register_lock:
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
        try:
            data = json.loads(resp.text or "{}")
            raw_key = data.get("raw_key") or ""
        except (ValueError, AttributeError):
            logger.debug("gateway register response not JSON, assuming no issued key")
        if raw_key:
            self._service_key = raw_key
            try:
                save_service_key(self._key_path, raw_key)
            except OSError as e:
                logger.error("gateway service key persist failed %s: %s", self._key_path, e)
        elif not self._service_key:
            logger.warning(
                "gateway registered (%s) but returned no raw_key and no stored key — "
                "heartbeat will fail; ask an admin to rotate the service key",
                label,
            )
        else:
            logger.info("gateway registration accepted (%s, existing key retained)", label)

    def heartbeat_once(self) -> bool:
        if not self._service_key:
            # 首次引导或 key 文件丢失：注册即保活（Upsert 刷新 lastHeartbeat）
            return self.register()
        url = f"{self._base_url}/api/v1/shell/manifests/{self._module_id}/heartbeat"
        try:
            resp = self._client.post(
                url, json={}, headers={"Authorization": f"Bearer {self._service_key}"}
            )
            if resp.status_code == 200:
                return True
            # 404 manifest_unknown：gateway 重启清空 store；403 heartbeat_forbidden：
            # 该 module 无匹配 service key 记录（store 重建/key 丢失）。两者都再注册
            # 一次（gateway 会为新条目签发新 fm- key）。cooldown 防 15s 循环。
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

    def _loop(self) -> None:
        # 周期心跳。失败不退出（gateway 重启等瞬时故障自愈），只节流告警。
        fail_streak = 0
        while not self._stop.wait(self._interval):
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
            # 注册失败仍启动心跳线程——若注册是瞬时故障（gateway 未起），心跳会
            # 走 register() 引导路径重试，无需人工干预。
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
