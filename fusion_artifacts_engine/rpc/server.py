import os
import json
import hmac
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Optional
from fusion_artifacts_engine.rpc.methods import RPCHandler
from fusion_artifacts_engine.engine import ArtifactEngine

logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 10 * 1024 * 1024

_API_KEY = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


from fusion_artifacts_engine.rpc.errors import RpcError


class JSONRPCHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        if _API_KEY:
            api_key = self.headers.get("X-API-Key", "")
            if not hmac.compare_digest(api_key, _API_KEY):
                self._send_response(401, {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Unauthorized"},
                })
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
