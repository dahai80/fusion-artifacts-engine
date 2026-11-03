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

# #104: key 漂移探测间隔。gateway 心跳面与再注册面都会在 KeyRotationGrace
# （默认 24h）内接受"上一把 key"——引擎持陈旧 key 时心跳仍 200，数据面
# （代理注入当前 key）却已被引擎拒之门外，症状是 shell 代理 -32600 且最长
# 持续一个 grace 窗口。探测走 upsert 自愈面：响应体 key_id 始终是 gateway
# 当前 key 的 id（保留分支也一样），与本地持久化的 key_id 比对即可在 grace
# 窗口内发现漂移，再用 admin 再注册触发 #460 轮换拿到新明文。
_KEY_PROBE_INTERVAL_SECONDS = 600

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


def key_id_path(key_path: Path) -> Path:
    # #104: key_id sidecar（<key文件名>.keyid）。key 文件本体是纯文本 key
    # （rpc/server.py _is_authed 直接读），key_id 存旁路文件互不干扰。
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

    def register(self, force_admin: bool = False) -> bool:
        with self._register_lock:
            if force_admin:
                # #104: 漂移自愈专用——跳过 service-key 候选，直接用 admin key
                # 再注册（#460：admin 面再注册会轮换 key 并返回新明文）。
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
            # #104: upsert 保留分支的 key_id 是 gateway 当前 key 的 id。与本地
            # 持久化值比对：不一致 = 引擎持陈旧 key（grace 窗口内的"上一把"，
            # 心跳面照常 200 掩盖了漂移）。置位后由 probe_key_state 走 admin
            # 再注册轮换自愈；本地无记录则首次落盘建立基线。
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
            # 网关 upsert 再注册可能不签发新 key（保留已存记录）——先回读 key 文件
            # 自检（先前实例可能已持久化有效 key），有效则直接复用，不告警
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
            # 首次引导或 key 文件丢失：注册即保活（Upsert 刷新 lastHeartbeat）。
            # 无 key 注册若拿不到 raw_key 会原地空转——套用同一冷却窗口，
            # 防止每 interval 刷一轮注册+告警（v0.7.3 线上实测 15s 循环）。
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
                # #110: gateway #485 在心跳 200 响应里暴露 key 漂移信号——
                # X-Fusion-Key-State: stale = 命中的是 grace 窗口内被轮换的
                # 旧 key，数据面（代理注入当前 key）已被拒。旧网关无此头，
                # 行为不变（403 后走既有恢复路径）。收到信号立即 admin 再
                # 注册轮换（#460 返回新明文），冷却窗口防轮换抖动引发风暴；
                # 心跳本身成功，返回 True 不进失败计数。
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

    def probe_key_state(self) -> bool:
        # #104: 周期 key 漂移探测。心跳 200 不代表 key 未漂移（grace 窗口内
        # 上一把 key 照常通过心跳面），故按独立间隔走一次 service-key 再注册，
        # 比对响应 key_id；发现漂移则 admin 再注册轮换（#460 返回新明文）。
        now = time.time()
        if now - self._last_probe < _KEY_PROBE_INTERVAL_SECONDS:
            return True
        self._last_probe = now
        if not self._service_key:
            # 无 key 时 heartbeat_once 的引导注册路径负责签发，无需探测。
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
        # 周期心跳。失败不退出（gateway 重启等瞬时故障自愈），只节流告警。
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
