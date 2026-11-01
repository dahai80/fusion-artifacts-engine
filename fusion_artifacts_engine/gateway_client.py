import logging
import os
import threading

import httpx

from fusion_artifacts_engine.utils import get_package_version

logger = logging.getLogger(__name__)

# #75: web-shell 集成（Config Phase 3.3 之后的服务注册面）。
# 引擎启动时向 fusion-gateway 自注册 shell manifest（幂等 upsert），之后周期心跳。
# 注册与心跳共用引擎自身 api_key（#296 self-keyed 模式）：首次注册需要 gateway
# admin_key 引导，注册成功后凭自身 key 完成心跳与 #369 self-heal 再注册。

_MODULE_TITLE = "Artifacts"
_MODULE_PATH = "/artifacts"
_MODULE_ICON = "Package"
_MODULE_CATEGORY = "platform"
_MODULE_DEVICES = ["desktop"]


class GatewayClient:
    def __init__(self, config, host: str, port: int) -> None:
        self._config = config
        self._host = host
        self._port = port
        self._module_id = config.gateway_module_id
        self._base_url = config.gateway_url.rstrip("/")
        self._interval = max(5, int(config.gateway_heartbeat_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = httpx.Client(timeout=5.0)

    def _api_key(self) -> str:
        # 与 rpc/server.py _is_authed 同源：env 优先（支持运行时旋转），其次 config
        return os.environ.get("FUSION_ARTIFACTS_API_KEY", "") or (
            self._config.api_key or ""
        )

    def _manifest_payload(self) -> dict:
        api_key = self._api_key()
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
            # #232 动态代理：upstream + api_key 使 /shell/<id>/ 无需改 gateway config
            "upstream": f"http://{self._host}:{self._port}",
            "api_key": api_key,
        }

    def register(self) -> bool:
        # 幂等注册。先试自身 key（已注册过的 #369 self-heal 路径），失败再试
        # admin_key（首次引导）。两者都失败仅告警——shell 集成是增强面，不阻断启动。
        payload = self._manifest_payload()
        api_key = self._api_key()
        if not api_key:
            logger.warning(
                "gateway registration skipped: no api_key configured "
                "(set security.api_key or FUSION_ARTIFACTS_API_KEY)"
            )
            return False
        url = f"{self._base_url}/api/v1/shell/manifests"
        try:
            resp = self._client.post(
                url, json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
            if resp.status_code == 200:
                logger.info(
                    "gateway manifest registered (self-key, module=%s)", self._module_id
                )
                return True
            logger.info(
                "gateway self-key registration not accepted (status=%s), trying admin key",
                resp.status_code,
            )
        except httpx.HTTPError as e:
            logger.warning("gateway registration request failed: %s", e)
            return False
        admin_key = self._config.gateway_admin_key or ""
        if not admin_key:
            logger.warning(
                "gateway registration failed and no gateway.admin_key configured "
                "for bootstrap"
            )
            return False
        try:
            resp = self._client.post(
                url, json=payload, headers={"Authorization": f"Bearer {admin_key}"}
            )
            if resp.status_code == 200:
                logger.info(
                    "gateway manifest registered (admin-key bootstrap, module=%s)",
                    self._module_id,
                )
                return True
            logger.warning(
                "gateway registration rejected: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
        except httpx.HTTPError as e:
            logger.warning("gateway admin-key registration failed: %s", e)
        return False

    def heartbeat_once(self) -> bool:
        # 心跳鉴权用引擎自身 key（注册时 api_key 的 sha256 在 gateway registry 上）
        url = f"{self._base_url}/api/v1/shell/manifests/{self._module_id}/heartbeat"
        api_key = self._api_key()
        if not api_key:
            return False
        try:
            resp = self._client.post(
                url, json={}, headers={"Authorization": f"Bearer {api_key}"}
            )
            if resp.status_code == 200:
                return True
            # 404 manifest_unknown：注册丢失（gateway 重建 store 等），再注册
            if resp.status_code == 404:
                logger.warning(
                    "gateway heartbeat 404 (manifest gone), re-registering module=%s",
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
            # 注册失败仍启动心跳线程——若注册是瞬时故障（gateway 未起），心跳 404
            # 分支会触发再注册，无需人工干预。
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
