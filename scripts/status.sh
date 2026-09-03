#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
DATA_DIR="$PROJECT_ROOT/.data"
PORT=${PORT:-8765}

usage() {
    cat <<'EOF'
Usage: scripts/status.sh [--port PORT]

Show PID ownership, local API health, the current temporary tunnel URL, and
the runtime log locations. This command does not start or stop anything.
EOF
}

die() {
    printf '오류: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --port)
            (($# >= 2)) || die "--port 뒤에 포트 번호가 필요합니다."
            PORT=$2
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

process_matches() {
    local kind=$1 pid=$2 cwd command_line
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    cwd=$(readlink -f -- "/proc/$pid/cwd" 2>/dev/null) || return 1
    [[ "$cwd" == "$PROJECT_ROOT" ]] || return 1
    command_line=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) || return 1
    case "$kind" in
        server) [[ "$command_line" == *"uvicorn"* && "$command_line" == *"server.app:create_app"* && "$command_line" == *"--factory"* ]] ;;
        tunnel) [[ "$command_line" == *"cloudflared"* && "$command_line" == *"tunnel"* && "$command_line" == *"--url"* ]] ;;
        *) return 1 ;;
    esac
}

pid_status() {
    local kind=$1 pid_file=$2 pid
    if [[ ! -s "$pid_file" ]]; then
        printf 'stopped'
        return
    fi
    IFS= read -r pid <"$pid_file" || true
    if process_matches "$kind" "${pid:-}"; then
        printf 'running (PID %s)' "$pid"
    elif [[ ${pid:-} =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        printf 'stale PID file (PID %s belongs to another process)' "$pid"
    else
        printf 'stopped (stale PID file)'
    fi
}

health_ok() {
    local url="http://127.0.0.1:$PORT/health" status
    if command -v curl >/dev/null 2>&1; then
        status=$(curl --silent --show-error --max-time 2 --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null) || return 1
        [[ "$status" == 200 ]]
    elif [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
        "$PROJECT_ROOT/.venv/bin/python" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    else
        return 1
    fi
}

external_health_ok() {
    local url=$1 status
    if command -v curl >/dev/null 2>&1; then
        for _ in {1..5}; do
            status=$(curl --silent --show-error --connect-timeout 3 --max-time 5 --output /dev/null --write-out '%{http_code}' "$url/health" 2>/dev/null) || status=000
            [[ "$status" == 200 ]] && return 0
            sleep 0.5
        done
        return 1
    elif [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
        "$PROJECT_ROOT/.venv/bin/python" - "$url/health" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    else
        return 1
    fi
}

server_status=$(pid_status server "$DATA_DIR/server.pid")
tunnel_status=$(pid_status tunnel "$DATA_DIR/tunnel.pid")
printf '로컬 API 서버: %s\n' "$server_status"
if health_ok; then
    printf 'API 상태: 정상 (http://127.0.0.1:%s/health)\n' "$PORT"
else
    printf 'API 상태: 응답 없음 (http://127.0.0.1:%s/health)\n' "$PORT"
fi
printf 'Cloudflare 터널: %s\n' "$tunnel_status"

public_url=
if [[ "$tunnel_status" == running* && -s "$DATA_DIR/tunnel-url.txt" ]]; then
    IFS= read -r public_url <"$DATA_DIR/tunnel-url.txt" || true
elif [[ "$tunnel_status" == running* && -r "$DATA_DIR/tunnel.log" ]]; then
    public_url=$(grep -Eo 'https://[[:alnum:]-]+\.trycloudflare\.com' "$DATA_DIR/tunnel.log" | tail -n 1 || true)
fi
if [[ -n "$public_url" ]]; then
    printf '현재 공개 주소: %s\n' "$public_url"
    if external_health_ok "$public_url"; then
        printf '외부 HTTPS 상태: 정상 (%s/health)\n' "$public_url"
    else
        printf '외부 HTTPS 상태: 응답 없음 (%s/health)\n' "$public_url"
    fi
else
    printf '현재 공개 주소: 없음\n'
    if [[ -s "$DATA_DIR/tunnel-url.txt" ]]; then
        IFS= read -r last_url <"$DATA_DIR/tunnel-url.txt" || true
        [[ -z "${last_url:-}" ]] || printf '마지막 주소(현재 비활성): %s\n' "$last_url"
    fi
fi

printf '서버 로그: %s\n' "$DATA_DIR/server.log"
printf '터널 로그: %s\n' "$DATA_DIR/tunnel.log"
