import sys
import signal
import logging
import argparse
from pathlib import Path
from fusion_artifacts_engine.config import load_config
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer
from fusion_artifacts_engine.utils import setup_logging, get_package_version

# User instruction: "所有的项目要有一个配置文件，配置类的卸载配置文件里面，不能写死在代码里面"
# Importers/callers: CLI entry point, calls load_config() from config.py, passes config to ArtifactEngine and ArtifactRPCServer
# Affected API: argparse --host/--port/--config flags; status command reads config instead of hardcoded URL
# Data schemas: ArtifactEngineConfig with server_host/server_port fields

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(prog="fusion-artifacts-engine")
    sub = parser.add_subparsers(dest="command")

    start_parser = sub.add_parser("start", help="Start the artifact engine daemon")
    start_parser.add_argument("--host", default=None)
    start_parser.add_argument("--port", type=int, default=None)
    start_parser.add_argument("--storage-root", default=None)
    start_parser.add_argument("--config", default=None, help="Path to config YAML file")

    sub.add_parser("status", help="Check if daemon is running")
    sub.add_parser("version", help="Print version")

    args = parser.parse_args()

    if args.command == "start":
        setup_logging(logging.INFO)
        user_config = Path(args.config) if args.config else None
        config = load_config(user_config_path=user_config)

        if args.storage_root:
            config.storage_root = Path(args.storage_root)
        host = args.host or config.server_host
        port = args.port or config.server_port

        engine = ArtifactEngine(config)
        server = ArtifactRPCServer(engine, host=host, port=port)

        def shutdown(sig, frame):
            logger.info("Received signal %s, shutting down", sig)
            server.stop()
            engine.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        logger.info("fusion-artifacts-engine starting...")
        try:
            server.start()
        except KeyboardInterrupt:
            server.stop()
            engine.close()

    elif args.command == "status":
        import os
        import httpx

        config = load_config()
        host = config.server_host
        port = config.server_port
        headers = {}
        api_key = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")
        if api_key:
            headers["X-API-Key"] = api_key
        try:
            resp = httpx.post(
                f"http://{host}:{port}",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers=headers,
                timeout=3.0,
            )
            data = resp.json()
            if "result" in data and data["result"].get("pong"):
                print(f"Running: version={data['result'].get('version', 'unknown')}")
            else:
                print("Not running or error")
        except Exception as e:
            print(f"Not running: {e}")

    elif args.command == "version":
        print(f"fusion-artifacts-engine {get_package_version()}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
