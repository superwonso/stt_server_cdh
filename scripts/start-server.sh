#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
ENV_FILE="$PROJECT_ROOT/server/.env"
DATA_DIR="$PROJECT_ROOT/.data"
PID_FILE="$DATA_DIR/server.pid"
LOG_FILE="$DATA_DIR/server.log"
PREVIOUS_LOG_FILE="$DATA_DIR/server.previous.log"
LOCK_FILE="$DATA_DIR/server-start.lock"
PORT=${PORT:-8765}
WARMUP=${MODEL_WARMUP:-1}
STARTUP_TIMEOUT=${STARTUP_TIMEOUT:-60}
SERVER_PID=
PID_TEMPORARY=
START_CLEANUP_ARMED=0

usage() {
    cat <<'EOF'
Usage: scripts/start-server.sh [options]

Start one local API worker in the background. The API always binds to
127.0.0.1; remote access must go through the HTTPS tunnel.

Options:
  --port PORT       Local port (default: 8765, or PORT)
  --warmup          Ask the configured model backend to load during startup
  --no-warmup       Skip model warmup (not recommended before class)
  --timeout SEC     Startup wait time (default: 60; warmup default: 600)
  -h, --help        Show this help

Warmup is enabled by default so the first classroom chunk does not pay model
load/kernel compilation time. The switch never changes the configured model.
EOF
}

die() {
    printf '오류: %s\n' "$*" >&2
    exit 1
}

timeout_was_set=0
while (($#)); do
    case "$1" in
        --port)
            (($# >= 2)) || die "--port 뒤에 포트 번호가 필요합니다."
            PORT=$2
            shift 2
            ;;
        --warmup)
            WARMUP=1
            shift
            ;;
        --no-warmup)
            WARMUP=0
            shift
            ;;
        --timeout)
            (($# >= 2)) || die "--timeout 뒤에 초가 필요합니다."
            STARTUP_TIMEOUT=$2
            timeout_was_set=1
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "알 수 없는 옵션입니다: $1"
            ;;
    esac
done

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1024 || PORT > 65535)); then
    die "포트는 1024~65535여야 합니다."
fi
if [[ ! "$STARTUP_TIMEOUT" =~ ^[0-9]+$ ]] || ((STARTUP_TIMEOUT < 1 || STARTUP_TIMEOUT > 3600)); then
    die "대기 시간은 1~3600초여야 합니다."
fi
case "${WARMUP,,}" in
    1|true|yes|on) WARMUP=1 ;;
    0|false|no|off) WARMUP=0 ;;
    *) die "MODEL_WARMUP은 0/1, false/true, no/yes 중 하나여야 합니다." ;;
esac
if ((WARMUP == 1 && timeout_was_set == 0)) && [[ ${STARTUP_TIMEOUT:-60} == 60 ]]; then
    STARTUP_TIMEOUT=600
fi

[[ -x "$PYTHON" ]] || die "scripts/setup.sh를 먼저 실행하세요."
[[ -f "$ENV_FILE" ]] || die "server/.env가 없습니다. 먼저 계정 및 공개 URL 설정을 초기화하세요."

mkdir -p -- "$DATA_DIR"
chmod 0700 "$DATA_DIR"

process_is_server() {
    local pid=$1 cwd argument previous= saw_module=0 saw_application=0 saw_factory=0
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    cwd=$(readlink -f -- "/proc/$pid/cwd" 2>/dev/null) || return 1
    [[ "$cwd" == "$PROJECT_ROOT" ]] || return 1
    while IFS= read -r -d '' argument; do
        if [[ "$previous" == "-m" && "$argument" == "uvicorn" ]]; then
            saw_module=1
        elif [[ "$argument" == "server.app:create_app" ]]; then
            saw_application=1
        elif [[ "$argument" == "--factory" ]]; then
            saw_factory=1
        fi
        previous=$argument
    done <"/proc/$pid/cmdline"
    ((saw_module == 1 && saw_application == 1 && saw_factory == 1))
}

server_uses_requested_port() {
    local pid=$1 argument expect_port=0
    while IFS= read -r -d '' argument; do
        if ((expect_port == 1)); then
            [[ "$argument" == "$PORT" ]]
            return
        fi
        case "$argument" in
            --port) expect_port=1 ;;
            --port=*)
                [[ "${argument#--port=}" == "$PORT" ]]
                return
                ;;
        esac
    done <"/proc/$pid/cmdline"
    return 1
}

health_ok() {
    local url="http://127.0.0.1:$PORT/health" status
    if command -v curl >/dev/null 2>&1; then
        status=$(curl --silent --show-error --max-time 2 --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null) || return 1
        [[ "$status" == "200" ]]
    else
        "$PYTHON" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    fi
}

remove_our_pid_file() {
    local pid=$1 recorded=
    if [[ -s "$PID_FILE" ]]; then
        IFS= read -r recorded <"$PID_FILE" || true
        if [[ "$recorded" == "$pid" ]]; then
            rm -f -- "$PID_FILE"
        fi
    fi
}

pid_is_live() {
    local pid=$1 state
    kill -0 "$pid" 2>/dev/null || return 1
    state=$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null) || return 1
    [[ "$state" != "Z" && "$state" != "X" ]]
}

command -v flock >/dev/null 2>&1 || die "시작 동시 실행을 막는 flock 명령을 찾을 수 없습니다. util-linux를 확인하세요."
command -v setsid >/dev/null 2>&1 || die "터미널 종료 뒤에도 서버를 유지하는 setsid 명령을 찾을 수 없습니다. util-linux를 확인하세요."
exec 9>"$LOCK_FILE"
chmod 0600 "$LOCK_FILE"
flock -n 9 || die "다른 서버 시작 작업이 진행 중입니다. 잠시 후 다시 실행하세요."

if [[ -s "$PID_FILE" ]]; then
    IFS= read -r previous_pid <"$PID_FILE" || true
    if process_is_server "${previous_pid:-}"; then
        if ! server_uses_requested_port "$previous_pid"; then
            die "실행 중인 서버(PID $previous_pid)는 요청한 포트 $PORT를 사용하지 않습니다. scripts/stop.sh --server-only로 먼저 종료하세요."
        fi
        if health_ok; then
            printf '서버가 이미 준비되어 있습니다 (PID %s): http://127.0.0.1:%s\n' "$previous_pid" "$PORT"
            exit 0
        fi
        printf '서버(PID %s)가 아직 준비 중입니다. 최대 %s초 동안 기다립니다...\n' "$previous_pid" "$STARTUP_TIMEOUT"
        existing_deadline=$((SECONDS + STARTUP_TIMEOUT))
        while ((SECONDS < existing_deadline)); do
            if ! process_is_server "$previous_pid"; then
                remove_our_pid_file "$previous_pid"
                previous_pid=
                break
            fi
            if health_ok; then
                printf '서버 준비 완료 (PID %s): http://127.0.0.1:%s\n' "$previous_pid" "$PORT"
                exit 0
            fi
            sleep 1
        done
        if [[ -n "${previous_pid:-}" ]] && process_is_server "$previous_pid"; then
            printf '기존 서버(PID %s)가 %s초 안에 준비되지 않았습니다. 프로세스는 그대로 두었습니다.\n' "$previous_pid" "$STARTUP_TIMEOUT" >&2
            printf 'scripts/status.sh와 %s를 확인하거나 scripts/stop.sh --server-only로 종료하세요.\n' "$LOG_FILE" >&2
            exit 1
        fi
    fi
    if [[ ${previous_pid:-} =~ ^[0-9]+$ ]] && kill -0 "$previous_pid" 2>/dev/null; then
        printf '경고: 오래된 PID 파일이 다른 프로세스(PID %s)를 가리켜 삭제합니다. 해당 프로세스에는 신호를 보내지 않습니다.\n' "$previous_pid" >&2
    fi
    rm -f -- "$PID_FILE"
fi

port_is_free() {
    "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", int(sys.argv[1])))
PY
}

port_available=0
for _ in {1..40}; do
    if port_is_free; then
        port_available=1
        break
    fi
    # A just-stopped uvicorn can need a brief moment to release its listener.
    sleep 0.25
done
((port_available == 1)) || die "127.0.0.1:$PORT 포트를 이미 다른 프로세스가 사용 중입니다. PID 파일 없이 남은 서버가 없는지 확인하세요."

terminate_started_server() {
    local pid=$1 deadline
    if pid_is_live "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
        deadline=$((SECONDS + 20))
        while ((SECONDS < deadline)) && pid_is_live "$pid"; do
            sleep 0.25
        done
        if pid_is_live "$pid"; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    fi
    wait "$pid" 2>/dev/null || true
    remove_our_pid_file "$pid"
}

cleanup_failed_start() {
    local status=$?
    trap - EXIT INT TERM HUP
    if ((START_CLEANUP_ARMED == 1)) && [[ -n "$SERVER_PID" ]]; then
        terminate_started_server "$SERVER_PID"
    fi
    if [[ -n "$PID_TEMPORARY" ]]; then
        rm -f -- "$PID_TEMPORARY"
    fi
    exit "$status"
}

cleanup_interrupted_start() {
    local signal=$1
    trap - EXIT INT TERM HUP
    if [[ -n "$SERVER_PID" ]]; then
        terminate_started_server "$SERVER_PID"
    fi
    if [[ -n "$PID_TEMPORARY" ]]; then
        rm -f -- "$PID_TEMPORARY"
    fi
    printf '서버 시작이 %s 신호로 취소됐습니다.\n' "$signal" >&2
    exit 130
}
trap 'cleanup_interrupted_start INT' INT
trap 'cleanup_interrupted_start TERM' TERM
trap 'cleanup_interrupted_start HUP' HUP
trap cleanup_failed_start EXIT

cd -- "$PROJECT_ROOT"
if [[ -s "$LOG_FILE" ]]; then
    mv -f -- "$LOG_FILE" "$PREVIOUS_LOG_FILE"
    chmod 0600 "$PREVIOUS_LOG_FILE"
else
    rm -f -- "$LOG_FILE"
fi
: >"$LOG_FILE"
chmod 0600 "$LOG_FILE"
printf '\n[%s] server start requested (port=%s, warmup=%s)\n' "$(date --iso-8601=seconds)" "$PORT" "$WARMUP" >>"$LOG_FILE"
nohup setsid env PYTHONNOUSERSITE=1 MODEL_WARMUP="$WARMUP" "$PYTHON" -m uvicorn server.app:create_app --factory \
    --host 127.0.0.1 \
    --port "$PORT" \
    --workers 1 \
    --env-file "$ENV_FILE" \
    >>"$LOG_FILE" 2>&1 </dev/null 9>&- &
SERVER_PID=$!
START_CLEANUP_ARMED=1

PID_TEMPORARY="$PID_FILE.$$"
printf '%s\n' "$SERVER_PID" >"$PID_TEMPORARY"
chmod 0600 "$PID_TEMPORARY"
mv -f -- "$PID_TEMPORARY" "$PID_FILE"
PID_TEMPORARY=

deadline=$((SECONDS + STARTUP_TIMEOUT))
while ((SECONDS < deadline)); do
    if ! pid_is_live "$SERVER_PID"; then
        wait "$SERVER_PID" 2>/dev/null || true
        remove_our_pid_file "$SERVER_PID"
        START_CLEANUP_ARMED=0
        trap - EXIT INT TERM HUP
        printf '서버가 준비되기 전에 종료됐습니다. 최근 로그:\n' >&2
        tail -n 30 "$LOG_FILE" >&2 || true
        exit 1
    fi
    if health_ok; then
        START_CLEANUP_ARMED=0
        trap - EXIT INT TERM HUP
        printf '서버 시작 완료 (PID %s): http://127.0.0.1:%s\n' "$SERVER_PID" "$PORT"
        printf '로그: %s\n' "$LOG_FILE"
        if ((WARMUP == 1)); then
            printf '모델 warmup 요청이 적용됐습니다 (MODEL_WARMUP=1).\n'
        fi
        exit 0
    fi
    sleep 1
done

printf '서버 프로세스(PID %s)는 실행 중이지만 %s초 안에 준비되지 않았습니다.\n' "$SERVER_PID" "$STARTUP_TIMEOUT" >&2
printf '이번 시작에서 만든 서버를 안전하게 종료합니다. 최근 로그:\n' >&2
terminate_started_server "$SERVER_PID"
START_CLEANUP_ARMED=0
trap - EXIT INT TERM HUP
tail -n 30 "$LOG_FILE" >&2 || true
exit 1
