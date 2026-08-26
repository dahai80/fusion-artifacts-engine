import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from fusion_artifacts_engine.config import load_config
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer
from fusion_artifacts_engine.utils import get_package_version, setup_logging

# User instruction: "所有的项目要有一个配置文件，配置类的卸载配置文件里面，不能写死在代码里面"
# Importers/callers: CLI entry point, calls load_config() from config.py, passes config to ArtifactEngine and ArtifactRPCServer
# Affected API: argparse --host/--port/--config flags; status command reads config instead of hardcoded URL
# Data schemas: ArtifactEngineConfig with server_host/server_port fields

logger = logging.getLogger(__name__)


def _run_once(config, host, port) -> int:
    # P0-8: 单次 engine 运行，返回退出码。0=优雅退出，非 0=崩溃（watch loop 据此重启）。
    engine = ArtifactEngine(config)
    server = ArtifactRPCServer(engine, host=host, port=port)

    # C-13: 主线程跑 serve_forever 时信号处理器同线程调 shutdown() 会死锁
    # (socketserver 文档禁止同线程 shutdown)。改用 start_async 起后台线程，
    # 主线程用 threading.Event 阻塞；信号处理器 set Event，主线程醒来后 stop。
    stop_event = threading.Event()
    graceful = {"yes": False}

    def shutdown(sig, frame):
        logger.info("Received signal %s, shutting down", sig)
        graceful["yes"] = True
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    else:
        logger.warning(
            "Not main thread, skip signal handlers (SIGINT/SIGTERM won't stop daemon)"
        )

    logger.info("fusion-artifacts-engine starting...")
    server.start_async()
    code = 0
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        graceful["yes"] = True
    except Exception as e:  # noqa: BLE001
        logger.exception("engine crashed: %s", e)
        code = 1
    finally:
        try:
            server.stop()
            engine.close()
        except Exception as e:  # noqa: BLE001
            logger.exception("error during shutdown: %s", e)
            code = 1
        logger.info("fusion-artifacts-engine stopped")
    if not graceful["yes"]:
        code = 1
    return code


def main():
    parser = argparse.ArgumentParser(prog="fusion-artifacts-engine")
    sub = parser.add_subparsers(dest="command")

    start_parser = sub.add_parser("start", help="Start the artifact engine daemon")
    start_parser.add_argument("--host", default=None)
    start_parser.add_argument("--port", type=int, default=None)
    start_parser.add_argument("--storage-root", default=None)
    start_parser.add_argument("--config", default=None, help="Path to config YAML file")
    # P0-8: 崩溃自动重启。engine 异常退出（非 0）后 backoff 秒重启；
    # SIGTERM/SIGINT 优雅退出（stop_event 被 set）返回 0，watch loop 不再重启。
    start_parser.add_argument(
        "--watch", action="store_true",
        help="Restart daemon on crash (backoff between restarts)",
    )
    start_parser.add_argument(
        "--restart-backoff", type=float, default=3.0,
        help="Seconds to wait between crash restarts (default 3.0)",
    )

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

        # P0-8: watch loop 包裹单次 run。--watch 时 engine 退出码非 0 则 backoff 后重启。
        # 退出码 0（优雅 stop）跳出循环。单次 run 内含信号注册与 stop_event 逻辑。
        while True:
            code = _run_once(config, host, port)
            if not getattr(args, "watch", False):
                sys.exit(code)
            if code == 0:
                logger.info("watch: engine exited 0 (graceful), watcher exiting")
                sys.exit(0)
            backoff = getattr(args, "restart_backoff", 3.0)
            logger.warning("watch: engine crashed (code=%s), restart in %ss", code, backoff)
            time.sleep(backoff)

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
        healthy = False
        try:
            resp = httpx.post(
                f"http://{host}:{port}",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers=headers,
                timeout=3.0,
            )
            data = resp.json()
            if "result" in data and data["result"].get("pong"):
                healthy = True
                print(f"Running: version={data['result'].get('version', 'unknown')}")
            else:
                print("Not running or error")
        except Exception as e:  # noqa: BLE001
            print(f"Not running: {e}")
        # L-20: 健康检查退出码——0 健康，1 未运行；start.sh 依赖 $? 判定
        sys.exit(0 if healthy else 1)

    elif args.command == "version":
        print(f"fusion-artifacts-engine {get_package_version()}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
