import json
import logging
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from fusion_artifacts_engine.rpc.methods import RPCHandler
from fusion_artifacts_engine.engine import ArtifactEngine

logger = logging.getLogger(__name__)


class JSONRPCHandler(BaseHTTPRequestHandler):

    rpc_handler: RPCHandler

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            request = json.loads(body.decode("utf-8"))
            logger.debug(f"RPC request: {request.get('method')}")
            loop = asyncio.new_event_loop()
            try:
                response = loop.run_until_complete(self._handle(request))
            finally:
                loop.close()
            self._send_response(200, response)
        except Exception as e:
            logger.error(f"RPC error: {e}")
            self._send_response(200, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(e)},
            })

    async def _handle(self, request: dict) -> dict:
        method = request.get("method", "")
        params = request.get("params") or {}
        req_id = request.get("id")
        try:
            result = await self.rpc_handler.dispatch(method, params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        except Exception as e:
            logger.error(f"Dispatch error for {method}: {e}")
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}

    def _send_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")


class ArtifactRPCServer:

    # User instruction: "所有的项目要有一个配置文件，配置类的卸载配置文件里面，不能写死在代码里面"
    # Importers/callers: __main__.py passes host/port from config; server reads from engine.config if not provided
    # Affected API: host/port default changed from hardcoded to reading from engine.config

    def __init__(self, engine: ArtifactEngine, host: str = None, port: int = None):
        self.engine = engine
        self.host = host or getattr(engine.config, "server_host", "127.0.0.1")
        self.port = port or getattr(engine.config, "server_port", 8892)
        self.rpc_handler = RPCHandler(engine)
        self._server = None

    def start(self) -> None:
        JSONRPCHandler.rpc_handler = self.rpc_handler
        self._server = HTTPServer((self.host, self.port), JSONRPCHandler)
        logger.info(f"ArtifactRPCServer starting on {self.host}:{self.port}")
        self._server.serve_forever()

    def start_async(self) -> None:
        import threading
        JSONRPCHandler.rpc_handler = self.rpc_handler
        self._server = HTTPServer((self.host, self.port), JSONRPCHandler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"ArtifactRPCServer started on {self.host}:{self.port} (async)")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            logger.info("ArtifactRPCServer stopped")
