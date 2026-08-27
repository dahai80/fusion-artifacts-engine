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

# Monorepo 约定：27 子项目共享 repo 根 .venv。优先用根 venv，回退项目内 .venv。
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_VENV="${REPO_ROOT}/.venv"
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
    # 优先 repo 根 venv（monorepo 共享，metadata 最新）；回退项目内 .venv；再回退 system。
    if [[ -f "${ROOT_VENV}/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source "${ROOT_VENV}/bin/activate"
    elif [[ -f "${VENV}/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source "${VENV}/bin/activate"
        log_warn "using project-local .venv (stale?); prefer repo-root .venv at ${ROOT_VENV}"
    else
        log_warn "no .venv found at ${ROOT_VENV} or ${VENV}, using system python3"
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
# 健康探测带 X-API-Key：server 在 allow_no_auth=false 时对所有 JSON-RPC（含 ping）
# 强制鉴权，无 key 的 ping 被 401 拒 → 误报 not running。key 来源与 server._is_authed
# 一致：env FUSION_ARTIFACTS_API_KEY → config api_key。
is_healthy() {
    is_running || return 1
    FAE_HOST="$HOST" FAE_PORT="$PORT" python3 - <<'PY' 2>/dev/null
import os, sys, json, urllib.request
host = os.environ.get("FAE_HOST", "127.0.0.1")
port = os.environ.get("FAE_PORT", "11451")
api_key = os.environ.get("FUSION_ARTIFACTS_API_KEY", "")
if not api_key:
    # 回退读 config（~/.fusion/artifacts/config.yaml 或 env 指向的配置文件）
    cfg_path = os.environ.get(
        "FUSION_ARTIFACTS_CONFIG",
        os.path.expanduser("~/.fusion/artifacts/config.yaml"),
    )
    try:
        import yaml
        with open(cfg_path) as fh:
            cfg = yaml.safe_load(fh) or {}
        api_key = (cfg.get("security") or {}).get("api_key", "") or ""
    except Exception:
        api_key = ""
headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key
body = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}).encode()
req = urllib.request.Request(f"http://{host}:{port}", data=body, headers=headers)
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

    # P0-8: 崩溃自动重启。FUSION_ARTIFACTS_AUTO_RESTART=1（默认）时传 --watch 给 CLI，
    # CLI 内置 watch loop：engine 异常退出后在 backoff 秒后重启，SIGTERM 优雅退出不重启。
    local auto_restart="${FUSION_ARTIFACTS_AUTO_RESTART:-1}"
    local watch_flag=""
    if [[ "$auto_restart" == "1" ]]; then
        watch_flag="--watch"
        log_info "starting artifacts-engine on ${HOST}:${PORT} (auto-restart enabled)..."
    else
        log_info "starting artifacts-engine on ${HOST}:${PORT}..."
    fi

    # shellcheck disable=SC2086
    nohup fusion-artifacts-engine start --host "$HOST" --port "$PORT" $watch_flag \
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
    # P0-8: --watch 模式下 PID 文件记的是 watcher 进程；SIGTERM 让 watcher 优雅退出
    # （watcher 转发信号给 engine 子进程，engine 退出码 0 后 watcher 也不再重启）。
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
