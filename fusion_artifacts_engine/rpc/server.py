import os
import json
import hmac
import queue
import logging
import asyncio
import threading
import time as _t
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
                "allow_no_auth", False,
            )
            if allow_no_auth:
                return True
            logger.warning("Auth rejected: no API_KEY configured and allow_no_auth=False")
            return False
        api_key = self.headers.get("X-API-Key", "")
        return hmac.compare_digest(api_key, _API_KEY)

    def _send_auth_denied(self, jsonrpc: bool = True) -> None:
        if jsonrpc:
            self._send_response(401, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Unauthorized"},
            })
        else:
            self._send_rest_response(401, {"error": "Unauthorized"})

    def do_POST(self):
        if self.path == "/api/token-count":
            self._handle_token_count()
            return
        if self.path == "/api/artifact/render":
            self._handle_render()
            return
        if self.path == "/api/artifact/interact":
            self._handle_interact()
            return
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_post()
            return
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=True)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > _MAX_BODY_SIZE:
                self._send_response(413, {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "Request body too large"},
                })
                return
            body = self.rfile.read(length)
            try:
                request = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_response(200, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                })
                return
            if not isinstance(request, dict) or "jsonrpc" not in request:
                self._send_response(200, {
                    "jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                })
                return
            logger.debug("RPC request: %s", request.get("method"))
            response = self._run_async(self._handle(request))
            self._send_response(200, response)
        except Exception as e:
            logger.error("RPC error: %s", e, exc_info=True)
            self._send_response(200, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": "Internal error"},
            })

    def do_GET(self):
        if self.path.startswith("/api/v1/events/stream"):
            self._handle_sse()
            return
        if self.path.startswith("/api/v1/"):
            self._handle_rest_v1_get()
            return
        if self.path.startswith("/api/artifact/content/"):
            self._handle_artifact_content()
            return
        if self.path.startswith("/api/share/"):
            self._handle_shared_content()
            return
        self._send_rest_response(404, {"error": "Not found"})

    def _handle_token_count(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > _MAX_BODY_SIZE:
                self._send_rest_response(413, {"error": "Request body too large"})
                return
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_rest_response(400, {"error": f"Parse error: {e}"})
                return
            text = data.get("text", "")
            model = data.get("model")
            engine = self.server._rpc_handler.engine
            token_count = self._run_async(engine.token_counter.count(text, model))
            self._send_rest_response(200, {"token_count": token_count})
        except Exception as e:
            logger.error("token-count error: %s", e, exc_info=True)
            self._send_rest_response(500, {"error": "Internal error"})

    def _handle_artifact_content(self) -> None:
        if not self._is_authed():
            self._send_rest_response(401, {"error": "Unauthorized"})
            return
        artifact_id = self.path.split("/")[-1]
        engine = self.server._rpc_handler.engine
        result = engine.get_artifact_raw_content(artifact_id)
        if result is None:
            self._send_rest_response(404, {"error": "Artifact not found"})
            return
        body = result["content"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", result["content_type"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", _CSP_HEADER)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _handle_shared_content(self) -> None:
        share_id = self.path.split("/")[-1]
        engine = self.server._rpc_handler.engine
        result = engine.get_shared_artifact(share_id)
        if result is None:
            self._send_rest_response(404, {"error": "Shared artifact not found or expired"})
            return
        content = result.get("content", "")
        content_type = result.get("content_type", "text/html")
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", _CSP_HEADER)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _handle_render(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > _MAX_BODY_SIZE:
                self._send_rest_response(413, {"error": "Request body too large"})
                return
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            engine = self.server._rpc_handler.engine
            result = self._run_async(engine.render_artifact(
                session_id=data.get("session_id", ""),
                content=data.get("content", ""),
                artifact_type=data.get("type", "auto"),
                viewport=data.get("viewport"),
                project_id=data.get("project_id"),
            ))
            self._send_rest_response(200, result)
        except Exception as e:
            logger.error("render error: %s", e, exc_info=True)
            self._send_rest_response(500, {"error": "Internal error"})

    def _handle_interact(self) -> None:
        if not self._is_authed():
            self._send_auth_denied(jsonrpc=False)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > _MAX_BODY_SIZE:
                self._send_rest_response(413, {"error": "Request body too large"})
                return
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            engine = self.server._rpc_handler.engine
            result = self._run_async(engine.interact_artifact(
                artifact_id=data["artifact_id"],
                action=data.get("action", "state_change"),
                payload=data.get("payload", {}),
                session_id=data.get("session_id", ""),
            ))
            self._send_rest_response(200, result)
        except ValueError as e:
            self._send_rest_response(400, {"error": str(e)})
        except Exception as e:
            logger.error("interact error: %s", e, exc_info=True)
            self._send_rest_response(500, {"error": "Internal error"})

    # ── P3: REST /api/v1 ───────────────────────────────────────

    def _handle_rest_v1_get(self) -> None:
        if not self._is_authed():
            self._send_rest_response(401, {"error": "Unauthorized"})
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path[len("/api/v1/"):]
        parts = path.strip("/").split("/")
        engine = self.server._rpc_handler.engine
        try:
            if not parts or parts[0] == "":
                self._send_rest_response(200, {"api": "v1", "status": "ok"})
            elif parts[0] == "artifacts" and len(parts) >= 2:
                artifact_id = parts[1]
                if len(parts) == 2:
                    artifact = engine.get_artifact(artifact_id)
                    if artifact is None:
                        self._send_rest_response(404, {"error": "Artifact not found"})
                        return
                    self._send_rest_response(200, artifact.model_dump())
                elif len(parts) == 3 and parts[2] == "versions":
                    versions = engine.list_versions(artifact_id)
                    self._send_rest_response(200, {"versions": [v.model_dump() for v in versions]})
                elif len(parts) == 3 and parts[2] == "snapshots":
                    snapshots = engine.list_snapshots(artifact_id)
                    self._send_rest_response(200, {"snapshots": [s.model_dump() for s in snapshots]})
                elif len(parts) == 3 and parts[2] == "tags":
                    tags = engine.list_artifact_tags(artifact_id)
                    self._send_rest_response(200, {"tags": [t.model_dump() for t in tags]})
                else:
                    self._send_rest_response(404, {"error": "Not found"})
            elif parts[0] == "artifacts":
                filters = {}
                created_by = qs.get("created_by", [None])[0]
                since = qs.get("since", [None])[0]
                until = qs.get("until", [None])[0]
                kind = qs.get("kind", [None])[0]
                type_ = qs.get("type", [None])[0]
                if created_by:
                    filters["owner_user_id"] = created_by
                if since:
                    try:
                        filters["since"] = float(since)
                    except ValueError:
                        pass
                if until:
                    try:
                        filters["until"] = float(until)
                    except ValueError:
                        pass
                if kind:
                    filters["kind"] = kind
                if type_:
                    filters["type"] = type_
                sort = qs.get("sort", ["updated_at"])[0]
                page = int(qs.get("page", ["1"])[0])
                page_size = int(qs.get("page_size", ["20"])[0])
                artifacts, total = engine.list_all_artifacts(
                    filters=filters or None, sort=sort, page=page, page_size=page_size,
                )
                self._send_rest_response(200, {"artifacts": [a.model_dump() for a in artifacts], "total": total})
            elif parts[0] == "external":
                source_module = qs.get("source_module", [None])[0]
                if not source_module:
                    self._send_rest_response(400, {"error": "source_module query param required"})
                    return
                workspace_id = qs.get("workspace_id", [None])[0]
                workflow_run_id = qs.get("workflow_run_id", [None])[0]
                artifacts = engine.list_by_source(source_module, workspace_id=workspace_id, workflow_run_id=workflow_run_id)
                self._send_rest_response(200, {"artifacts": [a.model_dump() for a in artifacts]})
            elif parts[0] == "folders":
                folders = engine.list_folders()
                self._send_rest_response(200, {"folders": [f.model_dump() for f in folders]})
            elif parts[0] == "tags":
                tags = engine.list_tags()
                self._send_rest_response(200, {"tags": [t.model_dump() for t in tags]})
            elif parts[0] == "events":
                events, total = engine.list_events()
                self._send_rest_response(200, {"events": [e.model_dump() for e in events], "total": total})
            elif parts[0] == "recycle":
                arts, total = engine.list_recycle()
                self._send_rest_response(200, {"artifacts": [a.model_dump() for a in arts], "total": total})
            else:
                self._send_rest_response(404, {"error": "Not found"})
        except Exception as e:
            logger.error("REST v1 GET error: %s", e, exc_info=True)
            self._send_rest_response(500, {"error": "Internal error"})

    def _handle_rest_v1_post(self) -> None:
        if not self._is_authed():
            self._send_rest_response(401, {"error": "Unauthorized"})
            return
        path = self.path[len("/api/v1/"):]
        parts = path.strip("/").split("/")
        engine = self.server._rpc_handler.engine
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > _MAX_BODY_SIZE:
                self._send_rest_response(413, {"error": "Request body too large"})
                return
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_rest_response(400, {"error": f"Parse error: {e}"})
            return
        try:
            if parts[0] == "artifacts" and len(parts) >= 2:
                artifact_id = parts[1]
                if len(parts) == 2:
                    action = data.get("action", "")
                    if action == "rename":
                        ok = engine.rename_artifact(artifact_id, data["new_name"])
                        self._send_rest_response(200, {"ok": ok})
                    elif action == "star":
                        ok = engine.star_artifact(artifact_id, data.get("starred", True))
                        self._send_rest_response(200, {"ok": ok})
                    elif action == "pin":
                        ok = engine.pin_artifact(artifact_id, data.get("chat_id"), data.get("pinned", True))
                        self._send_rest_response(200, {"ok": ok})
                    elif action == "duplicate":
                        dup = engine.duplicate_artifact(artifact_id, data.get("new_name"))
                        self._send_rest_response(200, {"artifact": dup.model_dump()} if dup else {"error": "Duplicate failed"})
                    elif action == "restore":
                        ok = engine.restore_artifact(artifact_id)
                        self._send_rest_response(200, {"ok": ok})
                    elif action == "move_to_kb":
                        ok = engine.move_to_project_kb(artifact_id, data["project_id"])
                        self._send_rest_response(200, {"ok": ok})
                    elif action == "move_to_folder":
                        ok = engine.move_to_folder(artifact_id, data.get("folder_id"))
                        self._send_rest_response(200, {"ok": ok})
                    else:
                        self._send_rest_response(400, {"error": f"Unknown action: {action}"})
                elif len(parts) == 3 and parts[2] == "snapshot":
                    snapshot = self._run_async(engine.create_snapshot(
                        artifact_id, label=data.get("label"), author=data.get("author"),
                    ))
                    self._send_rest_response(200, {"version": snapshot.model_dump()})
                elif len(parts) == 3 and parts[2] == "share":
                    share = engine.create_share(
                        artifact_id, created_by=data.get("created_by"), expires_at=data.get("expires_at"),
                    )
                    self._send_rest_response(200, {"share": share.model_dump()})
                elif len(parts) == 3 and parts[2] == "tags":
                    tag = engine.add_tag(artifact_id, data["tag_name"], color=data.get("color"))
                    self._send_rest_response(200, {"tag": tag.model_dump()})
                else:
                    self._send_rest_response(404, {"error": "Not found"})
            elif parts[0] == "folders":
                folder = engine.create_folder(data["name"], parent_id=data.get("parent_id"), project_id=data.get("project_id"))
                self._send_rest_response(200, {"folder": folder.model_dump()})
            elif parts[0] == "events":
                event = engine.emit_event(
                    data["event_type"], artifact_id=data.get("artifact_id"),
                    session_id=data.get("session_id"), payload=data.get("payload"),
                )
                event_bus.publish(event.event_type, {"artifact_id": event.artifact_id, "session_id": event.session_id, "payload": event.payload})
                self._send_rest_response(200, {"event": event.model_dump()})
            elif parts[0] == "purge":
                count = engine.purge_expired()
                self._send_rest_response(200, {"purged": count})
            elif parts[0] == "external" and len(parts) >= 2 and parts[1] == "create":
                artifact, version, ref_text = self._run_async(engine.create_external_artifact(
                    source_module=data["source_module"],
                    workspace_id=data["workspace_id"],
                    name=data["name"],
                    artifact_type=data.get("type", "code"),
                    content=data["content"],
                    workflow_run_id=data.get("workflow_run_id"),
                    summary=data.get("summary", ""),
                    kind=data.get("kind"),
                    project_id=data.get("project_id"),
                    metadata=data.get("metadata"),
                ))
                event_bus.publish("artifact.created", {"artifact_id": artifact.id, "source_module": artifact.source_module})
                self._send_rest_response(200, {"artifact": artifact.model_dump(), "version": version.model_dump(), "ref_text": ref_text})
            else:
                self._send_rest_response(404, {"error": "Not found"})
        except ValueError as e:
            self._send_rest_response(400, {"error": str(e)})
        except Exception as e:
            logger.error("REST v1 POST error: %s", e, exc_info=True)
            self._send_rest_response(500, {"error": "Internal error"})

    # ── P4: SSE (push-based via EventBus) ─────────────────────

    def _handle_sse(self) -> None:
        if not self._is_authed():
            self._send_rest_response(401, {"error": "Unauthorized"})
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        kind_filter = qs.get("kind", [None])[0]
        engine = self.server._rpc_handler.engine
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sub = event_bus.subscribe()
        watcher_id = f"sse_{id(self)}_{threading.get_ident()}"
        logger.info("SSE connected: watcher=%s kind_filter=%s", watcher_id, kind_filter)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            heartbeat_interval = getattr(engine.config, "sse_heartbeat_interval", 30)
            while True:
                try:
                    try:
                        event = sub.get(timeout=heartbeat_interval)
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        continue
                    if kind_filter:
                        event_kind = event.get("payload", {}).get("kind") if isinstance(event.get("payload"), dict) else None
                        if event_kind and event_kind != kind_filter:
                            continue
                    data = json.dumps(event)
                    self.wfile.write(f"event: artifact\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
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
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": e.code, "message": e.message}}
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": str(e)}}
        except Exception as e:
            logger.error("Dispatch error for %s: %s", method, e, exc_info=True)
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "Internal error"}}

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

    def __init__(self, engine: ArtifactEngine, host: Optional[str] = None, port: Optional[int] = None):
        self.engine = engine
        self.host = host or getattr(engine.config, "server_host", "127.0.0.1")
        self.port = port or getattr(engine.config, "server_port", 8892)
        self.rpc_handler = RPCHandler(engine)
        self._server = None
        self._async_loop = None
        self._loop_thread = None

    def _start_loop(self) -> None:
        self._async_loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=_async_loop_thread, args=(self._async_loop,), daemon=True)
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
