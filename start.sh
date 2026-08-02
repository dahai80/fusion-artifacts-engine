#!/bin/bash
# fusion-artifacts-engine lifecycle manager (start|stop|restart|status)
# JSON-RPC artifact engine on 127.0.0.1:11451 (ping method).
# Callers: fusion-studio UpstreamServiceManager (auto-start on launch + manual start).
# Affected API: start.sh start|stop|restart|status; status exits 0 if running, 1 if not.
# Data schemas: PID file .fusion-artifacts-engine.pid; logs/stdout.log + logs/stderr.log.
# User instruction: "在所有依赖的上游模块根目录创建start.sh，在fusion-studio启动时需要检测上游服务是否启动，如果没有启动，尝试调用start.sh启动上游服务，如果启动不成功，fusion-studio要展示服务不存在，或者服务启动失败等等"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="${SCRIPT_DIR}/.venv"
HOST="${FUSION_ARTIFACTS_HOST:-127.0.0.1}"
PORT="${FUSION_ARTIFACTS_PORT:-11451}"
PID_FILE="${SCRIPT_DIR}/.fusion-artifacts-engine.pid"
LOG_DIR="${SCRIPT_DIR}/logs"
STDOUT_LOG="${LOG_DIR}/stdout.log"
STDERR_LOG="${LOG_DIR}/stderr.log"
HEALTH_WAIT=60

log_info()  { printf "\033[0;32m[INFO]\033[0m  %s\n" "$*"; }
log_warn()  { printf "\033[0;33m[WARN]\033[0m  %s\n" "$*"; }
log_error() { printf "\033[0;31m[ERROR]\033[0m %s\n" "$*"; }

ensure_venv() {
    if [[ -f "${VENV}/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source "${VENV}/bin/activate"
    else
        log_warn "no .venv found at ${VENV}, using system python3"
    fi
}

get_pid() {
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || echo ""
}

is_running() {
    local pid
    pid=$(get_pid)
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# Health = process alive AND JSON-RPC ping returns pong (stdlib urllib, no deps).
is_healthy() {
    is_running || return 1
    FAE_HOST="$HOST" FAE_PORT="$PORT" python3 - <<'PY' 2>/dev/null
import os, sys, json, urllib.request
host = os.environ.get("FAE_HOST", "127.0.0.1")
port = os.environ.get("FAE_PORT", "11451")
body = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}).encode()
req = urllib.request.Request(f"http://{host}:{port}", data=body,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=2.0) as r:
        data = json.loads(r.read().decode())
    sys.exit(0 if (data.get("result") or {}).get("pong") else 1)
except Exception:
    sys.exit(1)
PY
}

start() {
    if is_running; then
        log_info "artifacts-engine already running (PID $(get_pid))"
        exit 0
    fi
    mkdir -p "$LOG_DIR"
    ensure_venv

    log_info "starting artifacts-engine on ${HOST}:${PORT}..."
    nohup fusion-artifacts-engine start --host "$HOST" --port "$PORT" \
        >> "$STDOUT_LOG" 2>> "$STDERR_LOG" &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    log_info "launched (PID ${pid}), waiting for health..."

    local i
    for i in $(seq 1 "$HEALTH_WAIT"); do
        if is_healthy; then
            log_info "artifacts-engine running (PID ${pid}) at ${HOST}:${PORT}"
            exit 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            log_error "process exited prematurely. recent stderr:"
            tail -n 20 "$STDERR_LOG" 2>/dev/null || true
            rm -f "$PID_FILE"
            exit 1
        fi
        sleep 1
    done

    log_error "timeout after ${HEALTH_WAIT}s. recent stderr:"
    tail -n 20 "$STDERR_LOG" 2>/dev/null || true
    exit 1
}

stop() {
    local pid
    pid=$(get_pid)
    if [[ -z "$pid" ]]; then
        log_info "artifacts-engine not running"
        return 0
    fi
    log_info "stopping artifacts-engine (PID ${pid})..."
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    log_info "stopped"
}

status() {
    if is_healthy; then
        echo "running (PID $(get_pid)) at ${HOST}:${PORT}"
        exit 0
    fi
    echo "not running"
    exit 1
}

restart() {
    stop || true
    start
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) restart ;;
    status)  status ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 2
        ;;
esac
