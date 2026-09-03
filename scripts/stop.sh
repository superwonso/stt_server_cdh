#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
DATA_DIR="$PROJECT_ROOT/.data"
TUNNEL_LOCK_FILE="$DATA_DIR/tunnel-start.lock"
STOP_SERVER=1
STOP_TUNNEL=1
STOP_TIMEOUT=${STOP_TIMEOUT:-20}

usage() {
    cat <<'EOF'
Usage: scripts/stop.sh [options]

Stop the tunnel first and then the API server. Each process receives SIGTERM
and is given time for a safe shutdown before SIGKILL is used as a last resort.
When the tunnel is stopped, request an OFFLINE Pages config without waiting for
the deployment; publication failure does not undo the local shutdown.

Options:
  --server-only      Stop only the API server
  --tunnel-only      Stop only the Cloudflare tunnel
  --timeout SEC      Grace period for each process (default: 20)
  -h, --help         Show this help
EOF
}

die() {
    printf '오류: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --server-only)
            STOP_SERVER=1
            STOP_TUNNEL=0
            shift
            ;;
        --tunnel-only)
            STOP_SERVER=0
            STOP_TUNNEL=1
            shift
            ;;
        --timeout)
            (($# >= 2)) || die "--timeout 뒤에 초가 필요합니다."
            STOP_TIMEOUT=$2
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

if [[ ! "$STOP_TIMEOUT" =~ ^[0-9]+$ ]] || ((STOP_TIMEOUT < 1 || STOP_TIMEOUT > 300)); then
    die "대기 시간은 1~300초여야 합니다."
fi

if ((STOP_TUNNEL == 1)); then
    mkdir -p -- "$DATA_DIR"
    chmod 0700 "$DATA_DIR"
    command -v flock >/dev/null 2>&1 || die "터널 수명주기 잠금에 필요한 flock을 찾을 수 없습니다."
    exec 8>"$TUNNEL_LOCK_FILE"
    chmod 0600 "$TUNNEL_LOCK_FILE"
    flock 8
fi

process_matches() {
    local kind=$1 pid=$2 cwd command_line
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    cwd=$(readlink -f -- "/proc/$pid/cwd" 2>/dev/null) || return 1
    [[ "$cwd" == "$PROJECT_ROOT" ]] || return 1
    command_line=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) || return 1
    case "$kind" in
        server)
            [[ "$command_line" == *"uvicorn"* && "$command_line" == *"server.app:create_app"* && "$command_line" == *"--factory"* ]]
            ;;
        tunnel)
            [[ "$command_line" == *"cloudflared"* && "$command_line" == *"tunnel"* && "$command_line" == *"--url"* ]]
            ;;
        *)
            return 1
            ;;
    esac
}

stop_one() {
    local kind=$1 label=$2 pid_file=$3 pid deadline

    if [[ ! -s "$pid_file" ]]; then
        printf '%s: 실행 기록이 없습니다.\n' "$label"
        return
    fi
    IFS= read -r pid <"$pid_file" || true
    if ! process_matches "$kind" "${pid:-}"; then
        if [[ ${pid:-} =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            printf '%s: PID 파일이 다른 프로세스(PID %s)를 가리킵니다. 안전을 위해 신호를 보내지 않습니다.\n' "$label" "$pid" >&2
        else
            printf '%s: 이미 종료됐습니다.\n' "$label"
        fi
        rm -f -- "$pid_file"
        return
    fi

    printf '%s(PID %s)에 SIGTERM을 보냅니다...\n' "$label" "$pid"
    kill -TERM "$pid"
    deadline=$((SECONDS + STOP_TIMEOUT))
    while ((SECONDS < deadline)); do
        if ! process_matches "$kind" "$pid"; then
            rm -f -- "$pid_file"
            printf '%s: 안전하게 종료됐습니다.\n' "$label"
            return
        fi
        sleep 0.25
    done

    if process_matches "$kind" "$pid"; then
        printf '%s: %s초 안에 끝나지 않아 PID %s에 SIGKILL을 보냅니다.\n' "$label" "$STOP_TIMEOUT" "$pid" >&2
        kill -KILL "$pid"
        sleep 0.25
    fi
    rm -f -- "$pid_file"
    printf '%s: 종료됐습니다.\n' "$label"
}

if ((STOP_TUNNEL == 1)); then
    stop_one tunnel "Cloudflare 터널" "$DATA_DIR/tunnel.pid"
    rm -f -- "$DATA_DIR/tunnel-url.txt"
fi
if ((STOP_SERVER == 1)); then
    stop_one server "로컬 API 서버" "$DATA_DIR/server.pid"
fi
if ((STOP_TUNNEL == 1)); then
    # Finish the desired-state update before returning so an immediate next
    # start cannot be overwritten by a late background OFFLINE request. The
    # deployment itself remains asynchronous and failure never blocks shutdown.
    if command -v timeout >/dev/null 2>&1 \
        && timeout --signal=TERM --kill-after=3 15 \
            "$SCRIPT_DIR/publish-api-url.sh" --offline --no-wait 8>&- >/dev/null 2>&1; then
        printf 'Pages에 서버 오프라인 상태를 게시하도록 요청했습니다.\n'
    else
        printf 'Pages 오프라인 상태를 게시하지 못했습니다. 서버와 터널은 정상적으로 종료됐습니다.\n' >&2
    fi
    flock -u 8
    exec 8>&-
fi
