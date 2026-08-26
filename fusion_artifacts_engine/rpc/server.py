import asyncio
import hmac
import json
import logging
import os
import queue
import threading
import time
import typing
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.metrics import get_metrics
from fusion_artifacts_engine.models import ArtifactKind
from fusion_artifacts_engine.rate_limiter import RateLimiter
from fusion_artifacts_engine.rpc.errors import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    NotImplementedError,
    PermissionError,
    ResourceLimitError,
    RpcError,
)
from fusion_artifacts_engine.rpc.methods import RPCHandler

logger = logging.getLogger(__name__)

# P1-3/H12: 每请求生成 uuid4 request ID，回写 X-Request-ID 响应头 + 绑入日志 extra，
# 让运维从客户端回溯到服务端日志行。BaseHTTPRequestHandler 每请求新建 handler 实例，
# 故 _request_id 天然 per-request，无需清理。
_REQUEST_ID_HEADER = "X-Request-ID"


class _RequestIdLogger(logging.LoggerAdapter):
    # P1-3: 注入 request_id 到每条日志的 extra，格式器可引用 %(request_id)s。
    def process(self, msg, kwargs):
        rid = self.extra.get("request_id", "-") if self.extra else "-"
        kwargs.setdefault("extra", {})
        if "request_id" not in kwargs["extra"]:
            kwargs["extra"]["request_id"] = rid
        return msg, kwargs


_MAX_BODY_SIZE = 10 * 1024 * 1024

# R4: 大请求体阈值。超过此值判定为「重写入」，用更长超时档并记日志便于运维定位写锁占用。
_LARGE_BODY_THRESHOLD = 2 * 1024 * 1024

# 导入时快照，向后兼容测试/旧用法；_is_authed 优先读 env 以支持运行时旋转 (P1-10)。
_API_KEY = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")

_API_CSP = "default-src 'none'; frame-ancestors 'none'"

# P1-7/M7: 合法 ArtifactKind 值集合，SSE kind_filter 校验用。非法 kind 拒绝连接。
_VALID_ARTIFACT_KINDS = set(typing.get_args(ArtifactKind))


def _parse_json_query(query: dict, key: str) -> dict | None:
    # REST GET 无 body，metadata_filter/filters 通过 JSON 编码的查询参数传递。
    raw = query.get(key, [None])[0]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Invalid JSON query param %s: %s", key, e)
        return None


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    # R1: ThreadingMixIn 默认每连接一线程、无上限。SSE 长连接独占线程，
    # 大量连接即线程/fd 耗尽。用并发信号量封顶：超过 _max_workers 的请求
    # 阻塞在 acquire（而非无界开线程），保护进程内存与上下文切换。
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, server_address, handler_cls, max_workers: int = 64):
        self._max_workers = max(1, int(max_workers))
        self._worker_sem = threading.BoundedSemaphore(self._max_workers)
        self._rejected_count = 0
        self._rate_limiter = RateLimiter()
        super().__init__(server_address, handler_cls)

    def process_request(self, request, client_address):
        # R1: 拿不到许可（并发已满）即拒绝连接，而非无界开线程。
        # acquire 超时后立即回 503 关连接，调用方可重试。
        if not self._worker_sem.acquire(timeout=0.01):
            self._rejected_count += 1
            logger.warning(
                "RPC server at worker cap %d, rejecting connection (rejected=%d)",
                self._max_workers,
                self._rejected_count,
            )
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n")
                request.sendall(b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            try:
                request.shutdown()
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_sem.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_sem.release()


class JSONRPCHandler(BaseHTTPRequestHandler):
    def _is_authed(self) -> bool:
        # C-4: api_key 优先 env（支持运行时旋转），其次 config.api_key；
        # 无 key 时按 allow_no_auth 决定，默认 False（fail-closed）
        engine = self.server._rpc_handler.engine
        api_key = os.environ.get("FUSION_ARTIFACTS_API_KEY", "") or _API_KEY or getattr(
            engine.config, "api_key", None
        ) or ""
        if not api_key:
            allow_no_auth = getattr(engine.config, "allow_no_auth", False)
            if allow_no_auth:
                return True
            logger.warning(
                "Auth rejected: no API_KEY configured and allow_no_auth=False"
            )
            return False
        provided = self.headers.get("X-API-Key", "")
        return hmac.compare_digest(provided, api_key)

    def _send_auth_denied(self, jsonrpc: bool = True) -> None:
        if jsonrpc:
            self._send_response(
                401,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Unauthorized"},
                },
            )
        else:
            self._send_rest_response(401, {"error": "Unauthorized"})

    def _send_rate_limited(self, jsonrpc: bool = True) -> None:
        # 运维1: 令牌桶耗尽。JSON-RPC 用 -32003 ResourceLimitError（可重试），REST 用 429。
        if jsonrpc:
            self._send_response(
                429,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32003,
                        "message": "Rate limit exceeded, retryable",
                        "retryable": True,
                    },
                },
            )
        else:
            self._send_rest_response(
                429,
                {
                    "error": "Rate limit exceeded",
                    "code": -32003,
                    "retryable": True,
                },
            )

    def do_POST(self):
        # P1-3: 每请求生成 uuid4 request ID，绑入日志 + 响应头，便于运维端到端追踪。
        self._request_id = uuid.uuid4().hex
        self._log = _RequestIdLogger(logger, {"request_id": self._request_id})
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_post()
            return
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=True)
            return
        # 运维1: 鉴权后、解析前限流，避免被刷量请求拖垮解析+dispatch
        if not self.server._rate_limiter.check_default():
            self._send_rate_limited(jsonrpc=True)
            return
        # 运维2: 计量——活跃连接 +1，请求计数，延迟直方图，错误计数
        metrics = get_metrics()
        metrics.inc_gauge("rpc_active_conns")
        metrics.inc_counter("rpc_requests_total", labels={"path": "jsonrpc"})
        t0 = time.monotonic()
        method_name = ""
        errored = False
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > _MAX_BODY_SIZE:
                self._send_response(
                    413,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32603, "message": "Request body too large"},
                    },
                )
                return
            body = self.rfile.read(length)
            try:
                request = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_response(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {e}"},
                    },
                )
                return
            if not isinstance(request, dict) or "jsonrpc" not in request:
                self._send_response(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid Request"},
                    },
                )
                return
            self._log.debug("RPC request: %s", request.get("method"))
            method_name = request.get("method", "")
            timeout = self._timeout_for(method_name, length)
            if length >= _LARGE_BODY_THRESHOLD:
                # R4: 大请求体重写入占写锁时间长，记日志便于运维定位全站写停摆窗口。
                self._log.warning(
                    "Large RPC body %d bytes for %s, timeout tier=%ds",
                    length,
                    method_name,
                    timeout,
                )
            response = self._run_async(self._handle(request), timeout=timeout)
            if isinstance(response, dict) and "error" in response:
                errored = True
            self._send_response(200, response)
        except TimeoutError:
            # H5: 协程排队超时（慢操作拖累全站）。返回 -32603 并标记 retryable，
            # 调用方可退避重试而非当致命错误。区分 R8 的业务错误码。
            errored = True
            self._log.warning("RPC timeout for %s (tier=%ds)", method_name, timeout)
            self._send_response(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Request timeout, retryable",
                        "retryable": True,
                    },
                },
            )
        except Exception:
            errored = True
            self._log.exception("RPC error")
            self._send_response(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "Internal error"},
                },
            )
        finally:
            metrics.observe("rpc_request_latency_seconds", time.monotonic() - t0)
            if errored:
                metrics.inc_counter("rpc_error_total", labels={"path": "jsonrpc"})
            metrics.dec_gauge("rpc_active_conns")

    def do_GET(self):
        # P1-3: 每请求生成 uuid4 request ID，绑入日志 + 响应头，便于运维端到端追踪。
        self._request_id = uuid.uuid4().hex
        self._log = _RequestIdLogger(logger, {"request_id": self._request_id})
        # 运维5: /healthz（liveness）+ /readyz（readiness）分离，无鉴权（K8s probe 标配）
        if self.path == "/healthz":
            self._handle_healthz()
            return
        if self.path == "/readyz":
            self._handle_readyz()
            return
        # 运维2: /metrics 端点——Prometheus 文本格式，无鉴权（监控 scrape 标配）
        if self.path == "/metrics":
            self._handle_metrics()
            return
        if self.path.startswith("/api/v1/events/stream"):
            self._handle_sse()
            return
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_get()
            return
        self._send_rest_response(404, {"error": "Not found"})

    def _handle_healthz(self) -> None:
        # 运维5: liveness——进程能响应即 200。不查依赖，避免依赖抖动导致重启循环。
        self._send_rest_response(200, {"status": "ok", "check": "liveness"})

    def _handle_readyz(self) -> None:
        # 运维5: readiness——存储可用 + EventBus 未关闭才 200，否则 503。
        # 依赖未就绪时 K8s 不导流量，但 liveness 仍 200 不重启。
        engine = self.server._rpc_handler.engine
        checks = {"storage": False, "event_bus": False}
        ok = True
        try:
            storage_ok = engine.storage.health_check()
            checks["storage"] = storage_ok
            if not storage_ok:
                ok = False
        except Exception:  # noqa: BLE001
            logger.exception("readyz storage check failed")
            ok = False
        try:
            checks["event_bus"] = not engine.event_bus.is_closed()
            if not checks["event_bus"]:
                ok = False
        except Exception:  # noqa: BLE001
            logger.exception("readyz event_bus check failed")
            ok = False
        if ok:
            self._send_rest_response(200, {"status": "ready", "checks": checks})
        else:
            self._send_rest_response(503, {"status": "not_ready", "checks": checks})

    def _handle_metrics(self) -> None:
        engine = self.server._rpc_handler.engine
        if not getattr(engine.config, "metrics_enabled", True):
            self._send_rest_response(404, {"error": "metrics disabled"})
            return
        body = get_metrics().expose().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        # P1-3: 回写 request ID
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_rest_v1_get(self) -> None:
        engine = self.server._rpc_handler.engine
        parsed = urlparse(self.path)
        path_parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

        # Public share endpoint — no auth required (#38, #26-B)
        if len(path_parts) == 4 and path_parts[2] == "share":
            # 运维1: 公开端点独立令牌桶，防刷量拖垮鉴权后端
            if not self.server._rate_limiter.check_public():
                self._send_rest_response(
                    429,
                    {"error": "Rate limit exceeded", "code": -32003, "retryable": True},
                )
                return
            share_id = path_parts[3]
            result = engine.get_public_share(share_id)
            status = result.get("status")
            if status == "ok":
                self._send_rest_response(200, result)
            elif status == "gone":
                self._send_rest_response(410, {"error": "Gone", "reason": result.get("reason")})
            else:
                self._send_rest_response(404, {"error": "Share not found"})
            return

        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return

        if len(path_parts) >= 4 and path_parts[2] == "artifacts":
            if len(path_parts) == 4:
                artifact_id = path_parts[3]
                artifact = engine.get_artifact(artifact_id)
                if artifact is None:
                    self._send_rest_response(404, {"error": "Artifact not found"})
                    return
                self._send_rest_response(200, {"artifact": artifact.model_dump()})
                return
            if len(path_parts) == 5 and path_parts[4] == "versions":
                artifact_id = path_parts[3]
                versions = engine.list_versions(artifact_id)
                self._send_rest_response(
                    200,
                    {
                        "versions": [v.model_dump() for v in versions],
                    },
                )
                return
            if len(path_parts) == 6 and path_parts[4] == "versions":
                artifact_id = path_parts[3]
                try:
                    ver_num = int(path_parts[5])
                except ValueError:
                    self._send_rest_response(400, {"error": "Invalid version number"})
                    return
                version = engine.get_version_content(artifact_id, ver_num)
                if version is None:
                    self._send_rest_response(404, {"error": "Version not found"})
                    return
                self._send_rest_response(200, {"version": version.model_dump()})
                return
            self._send_rest_response(404, {"error": "Not found"})
            return

        if len(path_parts) >= 3 and path_parts[2] == "artifacts":
            session_id = query.get("session_id", [""])[0]
            include_deleted = (
                query.get("include_deleted", ["false"])[0].lower() == "true"
            )
            project_id = query.get("project_id", [None])[0]
            metadata_filter = _parse_json_query(query, "metadata_filter")
            filters = _parse_json_query(query, "filters")
            if session_id:
                artifacts = engine.list_artifacts(
                    session_id,
                    include_deleted,
                    project_id,
                    metadata_filter=metadata_filter,
                )
                self._send_rest_response(
                    200,
                    {
                        "artifacts": [a.model_dump() for a in artifacts],
                        "total": len(artifacts),
                    },
                )
            else:
                try:
                    page = int(query.get("page", ["1"])[0])
                    page_size = int(query.get("page_size", ["20"])[0])
                except ValueError:
                    self._send_rest_response(400, {"error": "Invalid page or page_size"})
                    return
                sort = query.get("sort", ["updated_at"])[0]
                artifacts, total = engine.list_all_artifacts(
                    filters=filters,
                    page=page,
                    page_size=page_size,
                    sort=sort,
                )
                self._send_rest_response(
                    200,
                    {
                        "artifacts": [a.model_dump() for a in artifacts],
                        "total": total,
                    },
                )
            return

        self._send_rest_response(404, {"error": "Not found"})

    def _handle_rest_v1_post(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        rpc_handler = self.server._rpc_handler
        parsed = urlparse(self.path)
        path_parts = [p for p in parsed.path.split("/") if p]

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > _MAX_BODY_SIZE:
                self._send_rest_response(413, {"error": "Request body too large"})
                return
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_rest_response(400, {"error": f"Invalid JSON: {e}"})
            return

        # REST create/update/delete 复用 RPC handler.dispatch，继承 SSE 发布与参数校验，
        # 保持 REST 与 RPC 能力对等 (P1-3 / P1-4)。
        if len(path_parts) >= 4 and path_parts[2] == "artifacts":
            if len(path_parts) == 4 and path_parts[3] == "create":
                try:
                    result = self._run_async(
                        rpc_handler.dispatch("artifact.create", data)
                    )
                except Exception as e:
                    self._rest_error_response(e)
                    return
                self._send_rest_response(201, result)
                return
            artifact_id = path_parts[3]
            if len(path_parts) == 4:
                action = data.get("action", "")
                if action == "delete":
                    try:
                        result = self._run_async(
                            rpc_handler.dispatch(
                                "artifact.delete",
                                {"artifact_id": artifact_id, **data},
                            )
                        )
                    except Exception as e:
                        self._rest_error_response(e)
                        return
                    self._send_rest_response(200, result)
                else:
                    try:
                        result = self._run_async(
                            rpc_handler.dispatch(
                                "artifact.update",
                                {"artifact_id": artifact_id, **data},
                            )
                        )
                    except Exception as e:
                        self._rest_error_response(e)
                        return
                    self._send_rest_response(200, result)
                return
            self._send_rest_response(404, {"error": "Not found"})
            return

        self._send_rest_response(404, {"error": "Not found"})

    def _handle_sse(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        kind_filter = query.get("kind", [None])[0]
        # P1-7/M7: kind_filter 校验——非 None 时必须为合法 ArtifactKind，否则拒绝连接。
        if kind_filter is not None and kind_filter not in _VALID_ARTIFACT_KINDS:
            self._log.warning(
                "SSE rejected invalid kind_filter=%s (allowed: %s)",
                kind_filter, sorted(_VALID_ARTIFACT_KINDS),
            )
            self._send_rest_response(400, {"error": f"Invalid kind: {kind_filter}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        # P1-3: 回写 request ID，SSE 客户端可凭此查服务端日志
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.end_headers()

        watcher_id = f"sse_{id(self)}"
        engine = self.server._rpc_handler.engine
        # A-2: 用 engine 实例的 EventBus，避免跨 engine 串流
        sub = engine.event_bus.subscribe()
        heartbeat_interval = engine.config.sse_heartbeat_interval
        # P1-7/M7: 单连接最大存活。0=不限；超时主动关流促客户端重连，防僵尸长连接占线程。
        max_lifetime = max(0, getattr(engine.config, "sse_max_lifetime", 0))

        self._log.info(
            "SSE connected: watcher=%s kind_filter=%s max_lifetime=%s",
            watcher_id, kind_filter, max_lifetime or "unlimited",
        )
        deadline = time.monotonic() + max_lifetime if max_lifetime > 0 else None
        try:
            while True:
                # P1-7: 到达存活上限主动关流
                if deadline is not None and time.monotonic() >= deadline:
                    self._log.info(
                        "SSE max_lifetime reached, closing watcher=%s", watcher_id
                    )
                    try:
                        self.wfile.write(b"event: __max_lifetime__\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    break
                try:
                    # P1-7: 取事件时不超过到 deadline 的剩余时间，避免超期仍阻塞整段心跳间隔
                    wait = heartbeat_interval
                    if deadline is not None:
                        wait = max(0.1, min(wait, deadline - time.monotonic()))
                    event = sub.get(timeout=wait)
                    if event is None:
                        break
                    etype = event.get("event_type")
                    # A-3/A-4: __dropped__ 队列满被驱逐，__closed__ 停机——关流促重连
                    if etype in ("__dropped__", "__closed__"):
                        logger.info(
                            "SSE stream %s, closing watcher=%s", etype, watcher_id
                        )
                        try:
                            data = json.dumps({"type": etype})
                            self.wfile.write(
                                f"event: {etype}\ndata: {data}\n\n".encode()
                            )
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        break
                    if kind_filter and event.get("kind") != kind_filter:
                        # E10: kind 缺失或被过滤时记日志，避免静默丢事件难排查。
                        # kind None 通常因 publish 时未传 kind 且 artifact 已删（回查取不到）。
                        if event.get("kind") is None:
                            self._log.warning(
                                "SSE event %s has no kind, dropped by filter=%s (watcher=%s)",
                                etype, kind_filter, watcher_id,
                            )
                        continue
                    data = json.dumps(event)
                    # F5: 单事件体积上限。超限丢弃（仅记日志），防大 payload 阻塞连接线程
                    # + 客户端缓冲爆炸。0=不限（向后兼容）。
                    max_event_bytes = max(
                        0, getattr(engine.config, "sse_max_event_bytes", 0)
                    )
                    if max_event_bytes > 0 and len(data) > max_event_bytes:
                        self._log.warning(
                            "SSE event %s dropped: %d bytes > cap %d (watcher=%s)",
                            etype, len(data), max_event_bytes, watcher_id,
                        )
                        continue
                    self.wfile.write(f"event: artifact\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                except (BrokenPipeError, ConnectionResetError):
                    break
                except Exception:
                    self._log.exception("SSE loop error")
                    break
        finally:
            engine.event_bus.unsubscribe(sub)
            self._log.info("SSE disconnected: watcher=%s", watcher_id)

    def _send_rest_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Security-Policy", _API_CSP)
        # P1-3: 回写 request ID，客户端可凭此查服务端日志
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rest_error_response(self, exc: Exception) -> None:
        # R8: REST 复用 RPC dispatch，按异常类型映射 HTTP 状态与可重试标记。
        # NotFoundError→404, ConflictError→409(retryable), ResourceLimitError→503(retryable),
        # BusinessRuleError→422, RpcError 其他→按 code 推断, ValueError→400(校验), 兜底→500。
        if isinstance(exc, NotFoundError):
            self._send_rest_response(404, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, ConflictError):
            self._send_rest_response(409, {"error": exc.message, "code": exc.code, "retryable": True})
        elif isinstance(exc, ResourceLimitError):
            self._send_rest_response(503, {"error": exc.message, "code": exc.code, "retryable": True})
        elif isinstance(exc, BusinessRuleError):
            self._send_rest_response(422, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, PermissionError):
            # P2-3/MEDIUM-4: IDOR 越权——403 Forbidden
            self._send_rest_response(403, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, NotImplementedError):
            # 运维6: 占位方法下线——501 Not Implemented
            self._send_rest_response(501, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, RpcError):
            # 未细分的 RpcError：负 32000 段按业务推断，否则当服务端错误
            status = 400 if exc.code == -32602 else 500
            self._send_rest_response(status, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, ValueError):
            self._send_rest_response(400, {"error": str(exc)})
        else:
            logger.exception("REST dispatch error")
            self._send_rest_response(500, {"error": "Internal error"})

    # H5: 按操作分级超时，取代单 30s 硬超时。快操作不绑长超时（无意义），
    # 重操作（render/auto_compact/large write）给够时间避免雪崩误杀。
    _TIMEOUT_TIERS = {
        "ping": 5,
        "context.budget": 10,
        "artifact.get": 10,
        "artifact.list": 15,
        "artifact.versions": 15,
        "artifact.version_content": 15,
        "artifact.render": 120,
        "artifact.auto_compact": 120,
        "artifact.check_safety": 60,
    }
    _TIMEOUT_DEFAULT = 30

    def _timeout_for(self, method: str, body_size: int = 0) -> int:
        # H5: 方法命中 tier 用 tier；R4: 接近 10MB 的大写自动升档到重操作超时，
        # 避免大 artifact 的 hash+文件写+DB INSERT 在默认 30s 内被误杀。
        t = self._TIMEOUT_TIERS.get(method, self._TIMEOUT_DEFAULT)
        if body_size >= _LARGE_BODY_THRESHOLD and t < 120:
            return 120
        return t

    def _run_async(self, coro, timeout: int | None = None):
        loop = self.server._async_loop
        # P1-9/M17: 捕获 future，超时后显式 cancel()，否则已提交任务在 event loop 里
        # 孤儿般继续跑（占线程/内存/锁），超时只是放弃等待不放弃执行。
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout or self._TIMEOUT_DEFAULT)
        except TimeoutError:
            cancelled = future.cancel()
            logger.warning(
                "async dispatch timed out (timeout=%ss), future cancel=%s",
                timeout or self._TIMEOUT_DEFAULT, cancelled,
            )
            raise

    async def _handle(self, request: dict) -> dict:
        method = request.get("method", "")
        params = request.get("params") or {}
        req_id = request.get("id")
        try:
            rpc_handler = self.server._rpc_handler
            result = await rpc_handler.dispatch(method, params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except RpcError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": e.code, "message": e.message},
            }
        except ValueError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(e)},
            }
        except KeyError as e:
            logger.warning("Missing required parameter for %s: %s", method, e)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": f"Missing required parameter: {e}",
                },
            }
        except TypeError as e:
            logger.warning("Invalid parameter type for %s: %s", method, e)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Invalid parameter type: {e}"},
            }
        except Exception:
            logger.exception("Dispatch error for %s", method)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Internal error"},
            }

    def _send_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Security-Policy", _API_CSP)
        # P1-3: 回写 request ID，客户端可凭此查服务端日志
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug("HTTP: %s", format % args)


def _async_loop_thread(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


class ArtifactRPCServer:
    def __init__(
        self,
        engine: ArtifactEngine,
        host: str | None = None,
        port: int | None = None,
    ):
        self.engine = engine
        self.host = host or getattr(engine.config, "server_host", "127.0.0.1")
        self.port = port or getattr(engine.config, "server_port", 11451)
        self.rpc_handler = RPCHandler(engine)
        self._server = None
        self._async_loop = None
        self._loop_thread = None

    def _start_loop(self) -> None:
        self._async_loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=_async_loop_thread, args=(self._async_loop,), daemon=True
        )
        self._loop_thread.start()
        logger.info("Persistent async event loop started")

    def _build_rate_limiter(self) -> RateLimiter:
        cfg = self.engine.config
        return RateLimiter(
            rps=getattr(cfg, "rate_limit_rps", 0),
            burst=getattr(cfg, "rate_limit_burst", 0),
            public_rps=getattr(cfg, "public_rate_limit_rps", 0),
            public_burst=getattr(cfg, "public_rate_limit_burst", 0),
        )

    def start(self) -> None:
        self._start_loop()
        max_workers = getattr(self.engine.config, "server_max_workers", 64)
        self._server = _ThreadingHTTPServer(
            (self.host, self.port), JSONRPCHandler, max_workers=max_workers
        )
        self._server._rpc_handler = self.rpc_handler
        self._server._async_loop = self._async_loop
        self._server._rate_limiter = self._build_rate_limiter()
        logger.info(
            "ArtifactRPCServer starting on %s:%d (max_workers=%d)",
            self.host, self.port, max_workers,
        )
        self._server.serve_forever()

    def start_async(self) -> None:
        self._start_loop()
        max_workers = getattr(self.engine.config, "server_max_workers", 64)
        self._server = _ThreadingHTTPServer(
            (self.host, self.port), JSONRPCHandler, max_workers=max_workers
        )
        self._server._rpc_handler = self.rpc_handler
        self._server._async_loop = self._async_loop
        self._server._rate_limiter = self._build_rate_limiter()
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        logger.info(
            "ArtifactRPCServer started on %s:%d (async, max_workers=%d)",
            self.host, self.port, max_workers,
        )

    def stop(self) -> None:
        if self._server:
            # P0-7: shutdown() 停止 accept 循环，server_close() 释放监听 socket。
            # 不调 server_close 会泄漏 listen socket 与端口（TIME_WAIT 后才回收）。
            self._server.shutdown()
            self._server.server_close()
        if self._async_loop and self._async_loop.is_running():
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        logger.info("ArtifactRPCServer stopped")
