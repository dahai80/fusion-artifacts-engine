import os
import json
import hmac
import queue
import logging
import asyncio
import threading
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
from fusion_artifacts_engine.rpc.methods import RPCHandler
from fusion_artifacts_engine.rpc.errors import RpcError
from fusion_artifacts_engine.rpc.event_bus import event_bus
from fusion_artifacts_engine.engine import ArtifactEngine

logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 10 * 1024 * 1024

_API_KEY = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")

_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class JSONRPCHandler(BaseHTTPRequestHandler):
    def _is_authed(self) -> bool:
        if not _API_KEY:
            allow_no_auth = getattr(
                self.server._rpc_handler.engine.config,
                "allow_no_auth",
                True,
            )
            if allow_no_auth:
                return True
            logger.warning(
                "Auth rejected: no API_KEY configured and allow_no_auth=False"
            )
            return False
        api_key = self.headers.get("X-API-Key", "")
        return hmac.compare_digest(api_key, _API_KEY)

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

    def do_POST(self):
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_post()
            return
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=True)
            return
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
            logger.debug("RPC request: %s", request.get("method"))
            response = self._run_async(self._handle(request))
            self._send_response(200, response)
        except Exception as e:
            logger.error("RPC error: %s", e, exc_info=True)
            self._send_response(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "Internal error"},
                },
            )

    def do_GET(self):
        if self.path.startswith("/api/v1/events/stream"):
            self._handle_sse()
            return
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_get()
            return
        self._send_rest_response(404, {"error": "Not found"})

    def _handle_rest_v1_get(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        engine = self.server._rpc_handler.engine
        parsed = urlparse(self.path)
        path_parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

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
            if session_id:
                artifacts = engine.list_artifacts(
                    session_id, include_deleted, project_id
                )
                self._send_rest_response(
                    200,
                    {
                        "artifacts": [a.model_dump() for a in artifacts],
                        "total": len(artifacts),
                    },
                )
            else:
                page = int(query.get("page", ["1"])[0])
                page_size = int(query.get("page_size", ["20"])[0])
                sort = query.get("sort", ["updated_at"])[0]
                artifacts, total = engine.list_all_artifacts(
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
        engine = self.server._rpc_handler.engine
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

        if len(path_parts) >= 4 and path_parts[2] == "artifacts":
            if len(path_parts) == 4:
                result = self._run_async(
                    engine.create_artifact(
                        session_id=data.get("session_id", ""),
                        name=data.get("name", ""),
                        artifact_type=data.get("type", "code"),
                        content=data.get("content", ""),
                        summary=data.get("summary", ""),
                        kind=data.get("kind"),
                        project_id=data.get("project_id"),
                        metadata=data.get("metadata"),
                    )
                )
                artifact, version, ref_text = result
                self._send_rest_response(
                    201,
                    {
                        "artifact": artifact.model_dump(),
                        "version": version.model_dump(),
                        "ref_text": ref_text,
                    },
                )
                return
            artifact_id = path_parts[3]
            if len(path_parts) == 4:
                action = data.get("action", "")
                if action == "delete":
                    ok = engine.delete_artifact(
                        artifact_id, soft_delete=data.get("soft_delete", True)
                    )
                    self._send_rest_response(200, {"ok": ok})
                else:
                    result = self._run_async(
                        engine.create_version(
                            artifact_id,
                            data.get("content", ""),
                            change_log=data.get("change_log", ""),
                            expected_content_hash=data.get("expected_content_hash"),
                        )
                    )
                    version, ref_text = result
                    self._send_rest_response(
                        200,
                        {
                            "version": version.model_dump(),
                            "ref_text": ref_text,
                        },
                    )
                return
            self._send_rest_response(404, {"error": "Not found"})
            return

        self._send_rest_response(404, {"error": "Not found"})

    def _handle_sse(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        watcher_id = f"sse_{id(self)}"
        sub = queue.Queue()
        event_bus.subscribe(sub)

        engine = self.server._rpc_handler.engine
        heartbeat_interval = engine.config.sse_heartbeat_interval

        logger.info("SSE connected: watcher=%s", watcher_id)
        try:
            while True:
                try:
                    event = sub.get(timeout=heartbeat_interval)
                    if event is None:
                        break
                    data = json.dumps(event)
                    self.wfile.write(
                        f"event: artifact\ndata: {data}\n\n".encode("utf-8")
                    )
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
                except Exception as e:
                    logger.error("SSE loop error: %s", e)
                    break
        finally:
            event_bus.unsubscribe(sub)
            logger.info("SSE disconnected: watcher=%s", watcher_id)

    def _send_rest_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run_async(self, coro):
        loop = self.server._async_loop
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)

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
        except Exception as e:
            logger.error("Dispatch error for %s: %s", method, e, exc_info=True)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Internal error"},
            }

    def _send_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
        host: Optional[str] = None,
        port: Optional[int] = None,
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

    def start(self) -> None:
        self._start_loop()
        self._server = _ThreadingHTTPServer((self.host, self.port), JSONRPCHandler)
        self._server._rpc_handler = self.rpc_handler
        self._server._async_loop = self._async_loop
        logger.info("ArtifactRPCServer starting on %s:%d", self.host, self.port)
        self._server.serve_forever()

    def start_async(self) -> None:
        self._start_loop()
        self._server = _ThreadingHTTPServer((self.host, self.port), JSONRPCHandler)
        self._server._rpc_handler = self.rpc_handler
        self._server._async_loop = self._async_loop
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        logger.info("ArtifactRPCServer started on %s:%d (async)", self.host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
        if self._async_loop and self._async_loop.is_running():
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        logger.info("ArtifactRPCServer stopped")
