import asyncio
import hmac
import json
import logging
import mimetypes
import os
import queue
import re
import select
import socket
import threading
import time
import typing
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.gateway_client import load_service_key
from fusion_artifacts_engine.metrics import get_metrics
from fusion_artifacts_engine.models import ArtifactKind
from fusion_artifacts_engine.rate_limiter import RateLimiter
from fusion_artifacts_engine.render import render_share_html
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

# LOW-1: format pre-validation for share_id on public endpoints. A valid share_id looks like shr_<12 hex> (see share.py).
# Invalid format returns 400 directly, saving a DB round-trip and not leaking 'whether that share exists'.
_SHARE_ID_RE = re.compile(r"^shr_[A-Za-z0-9]{12}$")


def _share_notice_page(title: str, message: str) -> str:
    # #83: expired/revoked/not-found page - static copy, no script, pure inline styles (no external resources).
    import html as _html

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Artifacts</title>"
        "<meta http-equiv='Content-Security-Policy' "
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<style>body{font-family:system-ui,sans-serif;display:flex;min-height:100vh;"
        "align-items:center;justify-content:center;margin:0;background:#fafafa;color:#18181b}"
        "main{max-width:380px;padding:28px 32px;border:1px solid #e4e4e7;border-radius:16px;"
        "background:#fff;box-shadow:0 1px 2px rgba(24,24,27,.05)}"
        "h1{font-size:17px;margin:0 0 8px}p{font-size:13px;color:#52525b;margin:0}</style>"
        f"</head><body><main><h1>{_html.escape(title)}</h1>"
        f"<p>{_html.escape(message)}</p></main></body></html>"
    )

# #76: web GUI static asset directory (vite build output, shipped as package-data).
# The gateway /shell/fusion-artifacts/ proxy strips the prefix; GET / lands here.
_WEBUI_DIST = Path(__file__).resolve().parent.parent / "webui_dist"
# CSP: frame-ancestors allows the gateway same-origin iframe (web-shell host on 11432);
# blob: is for the GUI detail-page preview iframe (blob URLs do not inherit the parent page CSP; sandbox isolated).
_WEBUI_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self' data: blob:; "
    "frame-ancestors 'self' http://127.0.0.1:11432 http://localhost:11432"
)

# P1-3/H12: generate a uuid4 request ID per request, echo it in the X-Request-ID response header + bind it into log extra,
# so ops can trace from the client back to the server log line. BaseHTTPRequestHandler creates a fresh handler instance per request,
# so _request_id is naturally per-request; no cleanup needed.
_REQUEST_ID_HEADER = "X-Request-ID"


class _RequestIdLogger(logging.LoggerAdapter):
    # P1-3: inject request_id into the extra of every log record; formatters can reference %(request_id)s.
    def process(self, msg, kwargs):
        rid = self.extra.get("request_id", "-") if self.extra else "-"
        kwargs.setdefault("extra", {})
        if "request_id" not in kwargs["extra"]:
            kwargs["extra"]["request_id"] = rid
        return msg, kwargs


_MAX_BODY_SIZE = 10 * 1024 * 1024

# R4: large request body threshold. Above it the request is treated as a 'heavy write'; use the longer timeout tier and log it so ops can locate write-lock occupancy.
_LARGE_BODY_THRESHOLD = 2 * 1024 * 1024

# Snapshot at import time for backward compat with tests/old usage; _is_authed reads env first to support runtime rotation (P1-10).
_API_KEY = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")

_API_CSP = "default-src 'none'; frame-ancestors 'none'"

# P1-7/M7: set of valid ArtifactKind values, used for SSE kind_filter validation. Invalid kinds are rejected.
_VALID_ARTIFACT_KINDS = set(typing.get_args(ArtifactKind))


def _parse_json_query(query: dict, key: str) -> dict | None:
    # REST GET has no body; metadata_filter/filters are passed via JSON-encoded query params.
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
    # R1: ThreadingMixIn defaults to one thread per connection, unbounded. SSE long connections hold a thread each,
    # so many connections exhaust threads/fds. Cap with a concurrency semaphore: requests beyond _max_workers
    # block on acquire (instead of spawning unbounded threads), protecting process memory and context switching.
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, server_address, handler_cls, max_workers: int = 64,
                 sse_max_connections: int = 16):
        self._max_workers = max(1, int(max_workers))
        self._worker_sem = threading.BoundedSemaphore(self._max_workers)
        self._rejected_count = 0
        self._rate_limiter = RateLimiter()
        # P2-1/F1/H3: dedicated SSE concurrency semaphore. After a successful SSE handshake, release _worker_sem and take this semaphore,
        # so SSE long connections no longer occupy the 64 RPC threads - 64 SSE clients no longer saturate the backend and turn new RPCs all 503.
        # 0 = no dedicated cap (falls back to old behavior: SSE keeps occupying worker slots; not recommended).
        self._sse_max_connections = max(0, int(sse_max_connections))
        self._sse_sem = (
            threading.BoundedSemaphore(self._sse_max_connections)
            if self._sse_max_connections > 0
            else None
        )
        # P2-1: track connection ids handed off to the SSE semaphore. socket objects have no __dict__ and cannot carry attributes,
        # so use a server-level set instead; process_request_thread skips the _worker_sem release when it sees this id.
        # ids are not reused within a connection lifetime; process_request_thread must clean up in finally to prevent leaks.
        self._sse_handoffs: set[int] = set()
        self._sse_handoffs_lock = threading.Lock()
        super().__init__(server_address, handler_cls)

    def process_request(self, request, client_address):
        # R1: reject the connection when no permit is available (concurrency full), rather than spawning unbounded threads.
        # Return 503 and close the connection immediately after acquire times out; callers may retry.
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
            # P2-1: the SSE handshake already handed this connection from _worker_sem to _sse_sem; never release
            # _worker_sem again here (otherwise the semaphore count overflows and raises ValueError). socket has no __dict__,
            # so check the server-level _sse_handoffs set; on a hit, skip the release and clean up the record.
            if self._sse_sem is not None:
                with self._sse_handoffs_lock:
                    if id(request) in self._sse_handoffs:
                        self._sse_handoffs.discard(id(request))
                        return
            self._worker_sem.release()


class JSONRPCHandler(BaseHTTPRequestHandler):
    def _is_authed(self) -> bool:
        # C-4: api_key prefers env (supports runtime rotation), then config.api_key;
        # with no key, allow_no_auth decides; default False (fail-closed)
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
        # #75: dual-channel auth. X-API-Key is the legacy channel (fusion-studio/fusion-code direct);
        # Authorization: Bearer is the gateway /shell/<id>/ proxy-injected channel (the proxy strips the caller
        # credentials and only injects Bearer). Both compared in constant time; either match passes.
        provided = self.headers.get("X-API-Key", "")
        if not provided:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                provided = auth_header[len("Bearer "):].strip()
        if hmac.compare_digest(provided, api_key):
            return True
        # #82: the gateway-issued fm- service key is a proxy-injected credential (gateway #294 contract:
        # modules must validate this key). Read from the persisted file (mtime-cached); skip when missing.
        service_key = load_service_key(engine.config.gateway_service_key_path)
        return bool(service_key) and hmac.compare_digest(provided, service_key)

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
        # Ops-1: token bucket exhausted. JSON-RPC uses -32003 ResourceLimitError (retryable), REST uses 429.
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
        # P1-3: generate a uuid4 request ID per request, bind it into logs + response header for ops end-to-end tracing.
        self._request_id = uuid.uuid4().hex
        self._log = _RequestIdLogger(logger, {"request_id": self._request_id})
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_post()
            return
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=True)
            return
        # Ops-1: rate limit after auth, before parsing, so request floods do not drag down parse+dispatch
        if not self.server._rate_limiter.check_default():
            self._send_rate_limited(jsonrpc=True)
            return
        # Ops-2: metering - active connections +1, request counter, latency histogram, error counters
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
                # R4: large request body heavy writes hold the write lock for a long time; log it so ops can locate site-wide write-stall windows.
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
            # H5: coroutine queueing timeout (slow ops dragging down the whole site). Return -32603 marked retryable,
            # so callers can back off and retry instead of treating it as fatal. Distinct from the R8 business error codes.
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
        # P1-3: generate a uuid4 request ID per request, bind it into logs + response header for ops end-to-end tracing.
        self._request_id = uuid.uuid4().hex
        self._log = _RequestIdLogger(logger, {"request_id": self._request_id})
        # Ops-5: /healthz (liveness) + /readyz (readiness) separation, unauthenticated (standard K8s probes)
        if self.path == "/healthz":
            self._handle_healthz()
            return
        if self.path == "/readyz":
            self._handle_readyz()
            return
        # Ops-2: /metrics endpoint - Prometheus text format, unauthenticated (standard for monitoring scrapes)
        if self.path == "/metrics":
            self._handle_metrics()
            return
        # #59: SSE streaming visual rendering - push HTML fragments progressively per pipeline step
        if self.path.startswith("/api/v1/render-visual/stream"):
            self._handle_render_visual_sse()
            return
        # SSE event streams. Three forms share _handle_sse:
        # /api/v1/events/stream[?kind=...]                 global stream
        # /api/v1/artifacts/{artifactId}/events[?kind=...] filtered by artifact (#55)
        # /api/v1/sessions/{sessionId}/events[?kind=...]   filtered by session (#55)
        # fusion-studio bridge artifactEventStream/sessionEventStream call the latter two.
        parsed_get = urlparse(self.path)
        gp = [p for p in parsed_get.path.split("/") if p]
        if (
            (len(gp) == 5 and gp[2] == "artifacts" and gp[4] == "events")
            or (len(gp) == 5 and gp[2] == "sessions" and gp[4] == "events")
            or self.path.startswith("/api/v1/events/stream")
        ):
            self._handle_sse()
            return
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_get()
            return
        # #83: public share page (unauthenticated, public rate-limit bucket). /s/<share_id> renders a read-only preview,
        # with a 'Remix to my library' entry when remix permission is granted (routes through the gateway shell login flow).
        sp = [p for p in parsed_get.path.split("/") if p]
        if len(sp) == 2 and sp[0] == "s":
            self._handle_share_page(sp[1])
            return
        # #76: web GUI. GET / returns index.html, GET /assets/* returns build output.
        # Only active when webui_dist exists (stays 404 on source installs without dist; REST/RPC unaffected).
        if self._try_serve_webui():
            return
        self._send_rest_response(404, {"error": "Not found"})

    def _try_serve_webui(self) -> bool:
        # #76: static serving is GET-only; skipped entirely when webui_dist is missing (dev/minimal installs).
        if not _WEBUI_DIST.is_dir():
            return False
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._serve_webui_file(_WEBUI_DIST / "index.html", no_store=True)
        if path.startswith("/assets/") or path in ("/favicon.svg", "/favicon.ico"):
            rel = path.lstrip("/")
            target = (_WEBUI_DIST / rel).resolve()
            # Path traversal guard: the resolved path must remain inside the dist directory
            if not str(target).startswith(str(_WEBUI_DIST.resolve())):
                self._log.warning("webui path traversal rejected: %s", path)
                self._send_rest_response(404, {"error": "Not found"})
                return True
            return self._serve_webui_file(target, no_store=False)
        return False

    def _serve_webui_file(self, target: Path, no_store: bool) -> bool:
        if not target.is_file():
            return False
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        try:
            data = target.read_bytes()
        except OSError as e:
            self._log.warning("webui read failed %s: %s", target, e)
            return False
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # index.html is never cached (the entry page pins old bundle hashes); assets have hashed filenames and can be cached long-term
        if no_store:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", _WEBUI_CSP)
        else:
            self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.end_headers()
        self.wfile.write(data)
        return True

    def _handle_healthz(self) -> None:
        # Ops-5: liveness - 200 as long as the process responds. No dependency checks, avoiding dependency flapping causing restart loops.
        self._send_rest_response(200, {"status": "ok", "check": "liveness"})

    def _handle_readyz(self) -> None:
        # Ops-5: readiness - 200 only when storage is usable + EventBus not closed, else 503.
        # K8s routes no traffic while dependencies are unready, but liveness stays 200 so no restart.
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
        # LOW-4: when metrics_token is configured, /metrics requires a matching X-Metrics-Token header (constant-time compare).
        # Without it, rely on the 127.0.0.1 binding; exposing 0.0.0.0 must set a value to prevent ops metric leaks.
        expected_token = getattr(engine.config, "metrics_token", None)
        if expected_token:
            supplied = self.headers.get("X-Metrics-Token", "")
            if not hmac.compare_digest(supplied, expected_token):
                self._send_rest_response(401, {"error": "Unauthorized metrics access"})
                return
        body = get_metrics().expose().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        # P1-3: echo the request ID
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_share_page(self, share_id: str) -> None:
        # #83: public share page. The render pipeline reuses share.get_public_share (permission/expiry/
        # access-count checks) and render.render_share_html (sanitization + nonce CSP).
        engine = self.server._rpc_handler.engine
        if not self.server._rate_limiter.check_public():
            self._send_share_html(429, _share_notice_page("Too many requests", "Please retry shortly."))
            return
        if not _SHARE_ID_RE.fullmatch(share_id):
            self._send_share_html(404, _share_notice_page("Not found", "This share link is not valid."))
            return
        result = engine.get_public_share(share_id)
        status = result.get("status")
        if status == "gone":
            reason = result.get("reason", "unavailable")
            logger.info("share page %s gone: %s", share_id, reason)
            self._send_share_html(
                410, _share_notice_page("Link expired", f"This share link is {reason}.")
            )
            return
        if status != "ok":
            self._send_share_html(404, _share_notice_page("Not found", "This share link does not exist."))
            return
        artifact_id = result["artifact"]["id"]
        artifact = engine.get_artifact(artifact_id)
        version = engine.get_version_content(artifact_id)
        cfg = engine.config
        remix_url = ""
        if cfg.gateway_enabled:
            remix_url = (
                f"{cfg.gateway_url.rstrip('/')}/shell/{cfg.gateway_module_id}/"
                f"#/s/{share_id}?remix=1"
            )
        html = render_share_html(
            artifact,
            version.content if version else "",
            remix_url=remix_url or None,
            permission=result["share"]["permission"],
        )
        logger.info("share page %s served artifact %s", share_id, artifact_id)
        self._send_share_html(200, html)

    def _send_share_html(self, status: int, html_body: str) -> None:
        body = html_body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.end_headers()
        self.wfile.write(body)

    def _handle_rest_v1_get(self) -> None:
        engine = self.server._rpc_handler.engine
        parsed = urlparse(self.path)
        path_parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

        # Public share endpoint — no auth required (#38, #26-B)
        if len(path_parts) == 4 and path_parts[2] == "share":
            # Ops-1: dedicated token bucket for public endpoints, so floods do not drag down the authed backend
            if not self.server._rate_limiter.check_public():
                self._send_rest_response(
                    429,
                    {"error": "Rate limit exceeded", "code": -32003, "retryable": True},
                )
                return
            share_id = path_parts[3]
            # LOW-1: share_id format pre-validation. Invalid format returns 400, saving a DB round-trip and not leaking existence.
            if not _SHARE_ID_RE.fullmatch(share_id):
                self._send_rest_response(
                    400, {"error": "Invalid share_id format", "code": -32602}
                )
                return
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

        # #83: public page base URL used for GUI share-link construction (authed; returns the config value only)
        if len(path_parts) == 3 and path_parts[2] == "share-base":
            self._send_rest_response(200, {"base_url": engine.config.share_base_url})
            return

        # #100-3: REST event reads - on par with JSON-RPC artifact.list_events.
        # Note /api/v1/artifacts/{id}/events is already an SSE stream (#55), so the REST read
        # endpoint uses the collection path /api/v1/events?artifact_id=, avoiding conflict with the stream route.
        if len(path_parts) == 3 and path_parts[2] == "events":
            artifact_id = query.get("artifact_id", [None])[0]
            session_id = query.get("session_id", [None])[0]
            try:
                page = int(query.get("page", ["1"])[0])
                page_size = int(query.get("page_size", ["50"])[0])
            except ValueError:
                self._send_rest_response(400, {"error": "Invalid page or page_size"})
                return
            events, total = engine.list_events(
                artifact_id=artifact_id,
                session_id=session_id,
                page=page,
                page_size=page_size,
            )
            self._send_rest_response(
                200,
                {"events": [e.model_dump() for e in events], "total": total},
            )
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

        # REST create/update/delete reuse RPC handler.dispatch, inheriting SSE publishing and param validation,
        # keeping REST and RPC capability parity (P1-3 / P1-4).
        # #100-3: POST /api/v1/artifacts (collection path) = create alias - acceptance found only the
        # /create alias was recognized, so POST to the collection 404ed; added standard REST semantics.
        if len(path_parts) == 3 and path_parts[2] == "artifacts":
            try:
                result = self._run_async(rpc_handler.dispatch("artifact.create", data))
            except Exception as e:
                self._rest_error_response(e)
                return
            self._send_rest_response(201, result)
            return
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

        # #59: POST /api/v1/render-visual - DSL -> HTML (3-level fallback rendering)
        if len(path_parts) == 3 and path_parts[2] == "render-visual":
            try:
                result = self._run_async(
                    rpc_handler.dispatch("render.visual", data)
                )
            except Exception as e:
                self._rest_error_response(e)
                return
            self._send_rest_response(200, result)
            return

        self._send_rest_response(404, {"error": "Not found"})

    def _handle_sse(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        kind_filter = query.get("kind", [None])[0]
        # #55: scope filter carried in the path.
        #   /api/v1/artifacts/{id}/events → artifact_id_filter
        #   /api/v1/sessions/{id}/events  → session_id_filter
        # Events whose payload artifact_id/session_id does not match are dropped (same mechanism as kind_filter).
        sse_path_parts = [p for p in parsed.path.split("/") if p]
        artifact_id_filter = None
        session_id_filter = None
        if len(sse_path_parts) == 5 and sse_path_parts[2] == "artifacts" and sse_path_parts[4] == "events":
            artifact_id_filter = sse_path_parts[3]
        elif len(sse_path_parts) == 5 and sse_path_parts[2] == "sessions" and sse_path_parts[4] == "events":
            session_id_filter = sse_path_parts[3]
        # P1-7/M7: kind_filter validation - when not None it must be a valid ArtifactKind, otherwise the connection is rejected.
        if kind_filter is not None and kind_filter not in _VALID_ARTIFACT_KINDS:
            self._log.warning(
                "SSE rejected invalid kind_filter=%s (allowed: %s)",
                kind_filter, sorted(_VALID_ARTIFACT_KINDS),
            )
            self._send_rest_response(400, {"error": f"Invalid kind: {kind_filter}"})
            return
        # P2-1/F1/H3: dedicated SSE concurrency semaphore. Take a dedicated slot before the handshake; 503 if unavailable,
        # never letting SSE clients eat the whole RPC worker pool. On success release _worker_sem to return the RPC slot,
        # and set the handoff flag - process_request_thread sees the flag and does not release _worker_sem again.
        # sse_max_connections==0 (_sse_sem None) = fall back to old behavior, SSE keeps occupying worker slots.
        sse_sem = self.server._sse_sem
        sse_handoff = False
        if sse_sem is not None:
            if not sse_sem.acquire(timeout=0.01):
                self._log.warning(
                    "SSE rejected: SSE connection cap reached (max=%d)",
                    self.server._sse_max_connections,
                )
                self._send_rest_response(503, {"error": "SSE connection limit reached"})
                return
            sse_handoff = True
            self.server._worker_sem.release()
            # socket has no __dict__; use a server-level set to record handed-off connection ids (see process_request_thread)
            with self.server._sse_handoffs_lock:
                self.server._sse_handoffs.add(id(self.request))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        # P1-3: echo the request ID, SSE clients can use it to look up server logs
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.end_headers()

        watcher_id = f"sse_{id(self)}"
        engine = self.server._rpc_handler.engine
        # A-2: use the EventBus of the engine instance, avoiding cross-engine streaming
        sub = engine.event_bus.subscribe()
        heartbeat_interval = engine.config.sse_heartbeat_interval
        # P1-7/M7: max lifetime per connection. 0 = unlimited; close the stream proactively on timeout to prompt client reconnect, preventing zombie long connections from holding threads.
        max_lifetime = max(0, getattr(engine.config, "sse_max_lifetime", 0))

        self._log.info(
            "SSE connected: watcher=%s kind_filter=%s artifact_id=%s session_id=%s max_lifetime=%s",
            watcher_id, kind_filter, artifact_id_filter, session_id_filter,
            max_lifetime or "unlimited",
        )
        deadline = time.monotonic() + max_lifetime if max_lifetime > 0 else None
        try:
            while True:
                # P1-7: close the stream proactively when the lifetime cap is reached
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
                # P2-1: client disconnect detection. While blocked on sub.get(heartbeat), after the client closes the stream
                # the server waits up to one heartbeat_interval (default 30s) before noticing on the next failed write,
                # during which the _sse_sem slot is not returned - high-frequency connect/disconnect clients can exhaust the 16 slots.
                # So each loop uses select for non-blocking liveness probing: socket readable with no data = peer closed (EOF), break immediately.
                # read fd timeout capped at 1s, keeping the probe frequency at least once per second; slot return delay <= 1s instead of 30s.
                try:
                    rready, _, _ = select.select(
                        [self.request], [], [], min(1.0, heartbeat_interval)
                    )
                    if rready:
                        # readable but recv 0 bytes = EOF; >0 bytes = the client sent garbage data, also treated as a disconnect
                        try:
                            if not self.request.recv(1, socket.MSG_PEEK):
                                break
                        except OSError:
                            break
                except (OSError, ValueError):
                    break
                try:
                    # P1-7: when fetching events, never exceed the remaining time to the deadline, avoiding blocking a full heartbeat interval past it
                    wait = heartbeat_interval
                    if deadline is not None:
                        wait = max(0.1, min(wait, deadline - time.monotonic()))
                    event = sub.get(timeout=wait)
                    if event is None:
                        break
                    etype = event.get("event_type")
                    # A-3/A-4: __dropped__ evicted on a full queue, __closed__ on shutdown - close the stream to prompt reconnect
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
                        # E10: log when kind is missing or filtered out, avoiding silent event drops that are hard to debug.
                        # kind None usually means publish omitted kind and the artifact was deleted (the backfill lookup fails).
                        if event.get("kind") is None:
                            self._log.warning(
                                "SSE event %s has no kind, dropped by filter=%s (watcher=%s)",
                                etype, kind_filter, watcher_id,
                            )
                        continue
                    # #55: artifact_id / session_id scope filtering.
                    # Events missing the field or mismatching are dropped. folder/system events carry neither field,
                    # so scope streams naturally never receive them - matching the fusion-studio per-artifact/per-session subscription semantics.
                    if artifact_id_filter and event.get("artifact_id") != artifact_id_filter:
                        continue
                    if session_id_filter and event.get("session_id") != session_id_filter:
                        continue
                    data = json.dumps(event)
                    # F5: per-event size cap. Oversized events are dropped (log only), preventing large payloads from blocking the connection thread
                    # + exploding client buffers. 0 = unlimited (backward compatible).
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
            # P2-1: return the dedicated SSE semaphore slot (only when the handshake performed the handoff)
            if sse_handoff and sse_sem is not None:
                try:
                    sse_sem.release()
                except ValueError:
                    # Defensive: duplicate release under an extreme race (should not happen); ignore rather than crash
                    self._log.warning("SSE sse_sem double-release guarded, watcher=%s", watcher_id)
            self._log.info("SSE disconnected: watcher=%s", watcher_id)

    def _handle_render_visual_sse(self) -> None:
        # #59: SSE streaming visual rendering. Accepts ?dsl=<urlencoded_json> or ?raw_text=<urlencoded>,
        # parses the DSL then renders HTML segment by segment at each pipeline step's progress_range midpoint and pushes it.
        # Rate limited - the stream is closed proactively after all steps are pushed (not a long-lived event bus).
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        from urllib.parse import unquote

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        engine = self.server._rpc_handler.engine
        raw_text = None
        dsl_dict = None
        if "raw_text" in query:
            raw_text = unquote(query["raw_text"][0])
        elif "dsl" in query:
            try:
                dsl_dict = json.loads(unquote(query["dsl"][0]))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_rest_response(400, {"error": f"Invalid dsl param: {e}"})
                return
        else:
            self._send_rest_response(400, {"error": "Missing raw_text or dsl param"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Rate limited - close the stream proactively after all steps are pushed; not a long-lived event bus, so close rather than keep-alive
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.end_headers()
        self.close_connection = True
        try:
            from fusion_artifacts_engine.dsl_parser import (
                get_pipeline_progress,
                parse_model_output,
            )

            if dsl_dict is not None:
                progress_ranges = get_pipeline_progress(dsl_dict)
                visual_type = dsl_dict.get("meta", {}).get("visual_type", "flow_card")
            else:
                parsed_dsl = parse_model_output(raw_text)
                dsl_dict = parsed_dsl.dsl
                progress_ranges = get_pipeline_progress(dsl_dict)
                visual_type = parsed_dsl.visual_type
            if not progress_ranges:
                progress_ranges = [(0.0, 1.0)]
            for i, (lo, hi) in enumerate(progress_ranges):
                mid = (lo + hi) / 2.0
                html_out = engine.visual_dispatcher.dispatch(dsl_dict, mid)
                evt_data = json.dumps(
                    {"step": i, "progress": round(mid, 3), "html": html_out},
                    ensure_ascii=False,
                )
                self.wfile.write(f"event: render_step\ndata: {evt_data}\n\n".encode())
                self.wfile.flush()
            final_html = engine.visual_dispatcher.dispatch(dsl_dict, 1.0)
            final_data = json.dumps(
                {"step": "final", "progress": 1.0, "html": final_html, "visual_type": visual_type},
                ensure_ascii=False,
            )
            self.wfile.write(f"event: render_final\ndata: {final_data}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._log.info("render-visual SSE client disconnected")
        except Exception as e:
            self._log.warning("render-visual SSE error: %s", e)
            try:
                err_data = json.dumps({"error": str(e)})
                self.wfile.write(f"event: render_error\ndata: {err_data}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_rest_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Security-Policy", _API_CSP)
        # P1-3: echo the request ID, clients can use it to look up server logs
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header(_REQUEST_ID_HEADER, rid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rest_error_response(self, exc: Exception) -> None:
        # R8: REST reuses RPC dispatch, mapping exception types to HTTP status and retryable flags.
        # NotFoundError→404, ConflictError→409(retryable), ResourceLimitError→503(retryable),
        # BusinessRuleError->422, other RpcError->inferred from code, ValueError->400 (validation), fallback->500.
        if isinstance(exc, NotFoundError):
            self._send_rest_response(404, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, ConflictError):
            self._send_rest_response(409, {"error": exc.message, "code": exc.code, "retryable": True})
        elif isinstance(exc, ResourceLimitError):
            self._send_rest_response(503, {"error": exc.message, "code": exc.code, "retryable": True})
        elif isinstance(exc, BusinessRuleError):
            self._send_rest_response(422, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, PermissionError):
            # P2-3/MEDIUM-4: IDOR unauthorized access - 403 Forbidden
            self._send_rest_response(403, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, NotImplementedError):
            # Ops-6: placeholder method retired - 501 Not Implemented
            self._send_rest_response(501, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, RpcError):
            # Ungrouped RpcError: negative 32000-range inferred as business, otherwise treated as a server error
            status = 400 if exc.code == -32602 else 500
            self._send_rest_response(status, {"error": exc.message, "code": exc.code})
        elif isinstance(exc, ValueError):
            self._send_rest_response(400, {"error": str(exc)})
        else:
            logger.exception("REST dispatch error")
            self._send_rest_response(500, {"error": "Internal error"})

    # H5: per-operation timeout tiers, replacing a single 30s hard timeout. Fast ops are not bound to long timeouts (pointless),
    # heavy ops (render/auto_compact/large write) get enough time to avoid avalanche false kills.
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
        # H5: a method hit uses its tier; R4: large writes near 10MB auto-promote to the heavy-op timeout,
        # avoiding large artifact hash+file write+DB INSERT being falsely killed by the default 30s.
        t = self._TIMEOUT_TIERS.get(method, self._TIMEOUT_DEFAULT)
        if body_size >= _LARGE_BODY_THRESHOLD and t < 120:
            return 120
        return t

    def _run_async(self, coro, timeout: int | None = None):
        loop = self.server._async_loop
        # P1-9/M17: capture the future and cancel() explicitly after timeout, otherwise the submitted task keeps running
        # orphaned in the event loop (holding threads/memory/locks); the timeout only abandons waiting, not execution.
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
            # P2-5/H12(trace): the rpc.server.duration span is emitted inside RPCHandler.dispatch,
            # covering all call paths (server / REST / direct). No duplicate span wrapping here.
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
        # P1-3: echo the request ID, clients can use it to look up server logs
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
        sse_max = getattr(self.engine.config, "sse_max_connections", 16)
        self._server = _ThreadingHTTPServer(
            (self.host, self.port), JSONRPCHandler,
            max_workers=max_workers, sse_max_connections=sse_max,
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
        sse_max = getattr(self.engine.config, "sse_max_connections", 16)
        self._server = _ThreadingHTTPServer(
            (self.host, self.port), JSONRPCHandler,
            max_workers=max_workers, sse_max_connections=sse_max,
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
            # P0-7: shutdown() stops the accept loop, server_close() releases the listening socket.
            # Skipping server_close leaks the listen socket and port (only reclaimed after TIME_WAIT).
            self._server.shutdown()
            self._server.server_close()
        if self._async_loop and self._async_loop.is_running():
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        logger.info("ArtifactRPCServer stopped")
