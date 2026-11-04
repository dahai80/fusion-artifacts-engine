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

# User instruction: "Every project must have a config file; config belongs in the config file, never hardcoded in code"
# Importers/callers: CLI entry point, calls load_config() from config.py, passes config to ArtifactEngine and ArtifactRPCServer
# Affected API: argparse --host/--port/--config flags; status command reads config instead of hardcoded URL
# Data schemas: ArtifactEngineConfig with server_host/server_port fields

logger = logging.getLogger(__name__)


def _set_process_title(title: str) -> None:
    # #68: on macOS homebrew Python, comm shows bare "Python" and operators cannot tell products apart.
    # Prefer fusion_core.proc (shared helper); fall back to a direct setproctitle import
    # when fusion_core is missing (isolated CI); if both are missing, skip silently (acceptance #3: fallback safe).
    try:
        from fusion_core.proc import set_process_title

        set_process_title(title)
        return
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("fusion_core set_process_title failed: %s", e)
    try:
        import setproctitle

        setproctitle.setproctitle(title)
    except ImportError:
        logger.debug("setproctitle not installed, process title stays default (pip install '.[proc]')")
    except Exception as e:  # noqa: BLE001
        logger.debug("setproctitle failed, process title stays default: %s", e)


def _run_once(config, host, port) -> int:
    # P0-8: single engine run, returns an exit code. 0=graceful stop, non-0=crash (watch loop restarts on it).
    # P2-5: configure OTel tracing from config at startup (optional; no SDK or enabled=false → no-op).
    import fusion_artifacts_engine.tracing as tracing

    tracing.configure_tracing(config.tracing_enabled, config.tracing_service_name)
    engine = ArtifactEngine(config)
    server = ArtifactRPCServer(engine, host=host, port=port)

    # #84: fusion-design [Save to Artifacts] file-IPC consumer. Runs within the engine lifecycle;
    # rebuilt around the new engine after watch restart (consumer holds an engine reference).
    ipc_consumer = None
    if getattr(config, "ipc_enabled", True):
        from fusion_artifacts_engine.ipc_consumer import DesignIPCConsumer

        try:
            ipc_consumer = DesignIPCConsumer(
                engine, config.ipc_design_path, config.ipc_poll_interval
            )
            ipc_consumer.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("design IPC consumer start failed (non-fatal): %s", e)
            ipc_consumer = None

    # v0.7.0: fusion-office file ingest (read path) + .outbox write-back dir. Also non-fatal.
    office_consumer = None
    if getattr(config, "ipc_enabled", True) and getattr(config, "ipc_office_enabled", True):
        from fusion_artifacts_engine.ipc_consumer import OfficeIPCConsumer

        try:
            office_consumer = OfficeIPCConsumer(
                engine, config.ipc_office_path, config.ipc_poll_interval
            )
            office_consumer.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("office IPC consumer start failed (non-fatal): %s", e)
            office_consumer = None

    # C-13: calling shutdown() from a signal handler on the same thread that runs serve_forever deadlocks
    # (socketserver docs forbid same-thread shutdown). Instead start_async runs the server on a background
    # thread while the main thread blocks on threading.Event; the signal handler sets the Event, the main
    # thread wakes up and stops.
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
            if office_consumer is not None:
                office_consumer.stop()
            if ipc_consumer is not None:
                ipc_consumer.stop()
            server.stop()
            engine.close()
        except Exception as e:  # noqa: BLE001
            logger.exception("error during shutdown: %s", e)
            code = 1
        try:
            tracing.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.debug("tracing shutdown: %s", e)
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
    # P0-8: automatic crash restart. If the engine exits abnormally (non-0), restart after backoff seconds;
    # SIGTERM/SIGINT graceful stop (stop_event set) returns 0 and the watch loop stops restarting.
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

        # #68: set the process title to the product name so ps/Activity Monitor no longer shows bare "Python".
        # Outside the watch loop — restarts do not change the port, no need to set it again.
        _set_process_title(f"fusion-artifacts-engine serve :{port}")

        # #75: web-shell integration. When enabled, self-registers the manifest to fusion-gateway + heartbeat.
        # Register/heartbeat threads are independent of the engine lifecycle (watch restarts do not re-register).
        gateway_client = None
        if config.gateway_enabled:
            from fusion_artifacts_engine.gateway_client import GatewayClient

            gateway_client = GatewayClient(config, host, port)
            try:
                gateway_client.start()
            except Exception as e:  # noqa: BLE001
                # Hardening surface: gateway unreachable must not block engine startup
                logger.warning("gateway client start failed (non-fatal): %s", e)
                gateway_client = None

        # P1-1/H4: rate-limit production warning. rps==0 means unlimited — unthrottled in production, explicit WARN at startup.
        # default_config.yaml already sets production defaults (rps:200/burst:400, public_rps:50/burst:100),
        # but user configs or test environments may fall back to 0; this is the safety-net warning.
        if config.rate_limit_rps == 0:
            logger.warning(
                "rate_limit.rps=0 (unlimited) — JSON-RPC/REST endpoints unthrottled; "
                "set rate_limit.rps>0 in production"
            )
        if config.public_rate_limit_rps == 0:
            logger.warning(
                "rate_limit.public_rps=0 (unlimited) — public share endpoints unthrottled; "
                "set rate_limit.public_rps>0 in production"
            )

        # P0-8: watch loop wraps a single run. With --watch, a non-0 engine exit code restarts after backoff.
        # Exit code 0 (graceful stop) breaks out of the loop. A single run includes signal registration and stop_event logic.
        try:
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
        finally:
            if gateway_client is not None:
                try:
                    gateway_client.stop()
                except Exception as e:  # noqa: BLE001
                    logger.debug("gateway client stop: %s", e)

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
        # L-20: health-check exit code — 0 healthy, 1 not running; start.sh relies on $?
        sys.exit(0 if healthy else 1)

    elif args.command == "version":
        print(f"fusion-artifacts-engine {get_package_version()}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
