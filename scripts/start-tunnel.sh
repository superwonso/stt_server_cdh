#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
DATA_DIR="$PROJECT_ROOT/.data"
PID_FILE="$DATA_DIR/tunnel.pid"
LOG_FILE="$DATA_DIR/tunnel.log"
PREVIOUS_LOG_FILE="$DATA_DIR/tunnel.previous.log"
LOCK_FILE="$DATA_DIR/tunnel-start.lock"
URL_FILE="$DATA_DIR/tunnel-url.txt"
PUBLISH_SCRIPT="$SCRIPT_DIR/publish-api-url.sh"
PORT=${PORT:-8765}
TUNNEL_TIMEOUT=${TUNNEL_TIMEOUT:-60}
CLOUDFLARED=${CLOUDFLARED_BIN:-}
TUNNEL_PID=
PID_TEMPORARY=
START_CLEANUP_ARMED=0

usage() {
    cat <<'EOF'
Usage: scripts/start-tunnel.sh [options]

Start a free temporary Cloudflare HTTPS tunnel in the background. The public
URL changes whenever a new quick tunnel is created. After health verification,
publish that URL through GitHub Actions and wait for the Pages runtime config.

Options:
  --port PORT             Local API port (default: 8765, or PORT)
  --cloudflared PATH      Explicit cloudflared executable
  --timeout SEC           Wait for public URL (default: 60)
  -h, --help              Show this help

Lookup order: --cloudflared, CLOUDFLARED_BIN, .tools/cloudflared, PATH.
GitHub publication uses GH_BIN, PATH gh, or .tools/gh-*/bin/gh.
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
        --cloudflared)
            (($# >= 2)) || die "--cloudflared 뒤에 실행 파일 경로가 필요합니다."
            CLOUDFLARED=$2
            shift 2
            ;;
        --timeout)
            (($# >= 2)) || die "--timeout 뒤에 초가 필요합니다."
            TUNNEL_TIMEOUT=$2
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
if [[ ! "$TUNNEL_TIMEOUT" =~ ^[0-9]+$ ]] || ((TUNNEL_TIMEOUT < 1 || TUNNEL_TIMEOUT > 300)); then
    die "대기 시간은 1~300초여야 합니다."
fi

if [[ -n "$CLOUDFLARED" ]]; then
    [[ -x "$CLOUDFLARED" ]] || die "cloudflared를 실행할 수 없습니다: $CLOUDFLARED"
    CLOUDFLARED=$(readlink -f -- "$CLOUDFLARED")
elif [[ -x "$PROJECT_ROOT/.tools/cloudflared" ]]; then
    CLOUDFLARED="$PROJECT_ROOT/.tools/cloudflared"
elif command -v cloudflared >/dev/null 2>&1; then
    CLOUDFLARED=$(command -v cloudflared)
else
    die "cloudflared를 찾을 수 없습니다. scripts/setup.sh를 실행하거나 --cloudflared PATH를 지정하세요."
fi
CLOUDFLARED=$(readlink -f -- "$CLOUDFLARED")
"$CLOUDFLARED" --version >/dev/null 2>&1 || die "cloudflared 실행 파일을 확인하지 못했습니다: $CLOUDFLARED"

mkdir -p -- "$DATA_DIR"
chmod 0700 "$DATA_DIR"

process_is_tunnel() {
    local pid=$1 cwd argument first= saw_tunnel=0 saw_url=0
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    cwd=$(readlink -f -- "/proc/$pid/cwd" 2>/dev/null) || return 1
    [[ "$cwd" == "$PROJECT_ROOT" ]] || return 1
    while IFS= read -r -d '' argument; do
        if [[ -z "$first" ]]; then
            first=$argument
        elif [[ "$argument" == "tunnel" ]]; then
            saw_tunnel=1
        elif [[ "$argument" == "--url" || "$argument" == --url=* ]]; then
            saw_url=1
        fi
    done <"/proc/$pid/cmdline"
    [[ "$first" == "$CLOUDFLARED" ]] && ((saw_tunnel == 1 && saw_url == 1))
}

tunnel_uses_requested_port() {
    local pid=$1 argument expect_url=0 target="http://127.0.0.1:$PORT"
    while IFS= read -r -d '' argument; do
        if ((expect_url == 1)); then
            [[ "$argument" == "$target" ]]
            return
        fi
        case "$argument" in
            --url) expect_url=1 ;;
            --url=*)
                [[ "${argument#--url=}" == "$target" ]]
                return
                ;;
        esac
    done <"/proc/$pid/cmdline"
    return 1
}

local_health_ok() {
    local url="http://127.0.0.1:$PORT/health" status
    if command -v curl >/dev/null 2>&1; then
        status=$(curl --silent --show-error --max-time 2 --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null) || return 1
        [[ "$status" == "200" ]]
    else
        "$PROJECT_ROOT/.venv/bin/python" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    fi
}

external_health_ok() {
    local public_url=$1 url="${1%/}/health" status
    [[ "$public_url" =~ ^https://[[:alnum:]-]+\.trycloudflare\.com$ ]] || return 1
    if command -v curl >/dev/null 2>&1; then
        status=$(curl --silent --show-error --connect-timeout 2 --max-time 5 --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null) || return 1
        [[ "$status" == "200" ]]
    else
        "$PROJECT_ROOT/.venv/bin/python" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    fi
}

find_public_url() {
    local candidate=
    if [[ -r "$LOG_FILE" ]]; then
        candidate=$(grep -Eo 'https://[[:alnum:]-]+\.trycloudflare\.com' "$LOG_FILE" | tail -n 1 || true)
    fi
    if [[ "$candidate" =~ ^https://[[:alnum:]-]+\.trycloudflare\.com$ ]]; then
        printf '%s\n' "$candidate"
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

publish_public_url() {
    local public_url=$1
    if ! printf '%s\n' "$public_url" | "$PUBLISH_SCRIPT" --stdin --no-wait; then
        printf '터널은 계속 실행 중이지만 Pages 자동 연결 설정을 게시하지 못했습니다.\n' >&2
        printf 'GitHub 로그인을 확인한 뒤 이 명령을 다시 실행하세요. 필요하면 위 공개 주소를 화면의 내 서버에 직접 입력할 수 있습니다.\n' >&2
        return 1
    fi
    # GitHub now knows the desired online state. Release the lifecycle lock
    # before CDN polling so stop.sh can serialize a newer OFFLINE state.
    flock -u 9
    exec 9>&-
    if printf '%s\n' "$public_url" | "$PUBLISH_SCRIPT" --wait-only --stdin; then
        return
    fi
    printf 'Pages 자동 연결 설정의 배포를 확인하지 못했습니다.\n' >&2
    printf 'scripts/status.sh와 Actions 상태를 확인하세요. 터널이 실행 중이면 필요할 때 위 공개 주소를 화면의 내 서버에 직접 입력할 수 있습니다.\n' >&2
    return 1
}

command -v flock >/dev/null 2>&1 || die "시작 동시 실행을 막는 flock 명령을 찾을 수 없습니다. util-linux를 확인하세요."
command -v setsid >/dev/null 2>&1 || die "터미널 종료 뒤에도 터널을 유지하는 setsid 명령을 찾을 수 없습니다. util-linux를 확인하세요."
exec 9>"$LOCK_FILE"
chmod 0600 "$LOCK_FILE"
flock -n 9 || die "다른 터널 시작 작업이 진행 중입니다. 잠시 후 다시 실행하세요."

terminate_owned_tunnel() {
    local pid=$1 deadline
    if process_is_tunnel "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
        deadline=$((SECONDS + 20))
        while ((SECONDS < deadline)) && process_is_tunnel "$pid"; do
            sleep 0.25
        done
        if process_is_tunnel "$pid"; then
            kill -KILL "$pid" 2>/dev/null || true
            deadline=$((SECONDS + 5))
            while ((SECONDS < deadline)) && process_is_tunnel "$pid"; do
                sleep 0.1
            done
        fi
    fi
    process_is_tunnel "$pid" && return 1
    remove_our_pid_file "$pid"
}

if [[ -s "$PID_FILE" ]]; then
    IFS= read -r previous_pid <"$PID_FILE" || true
    if process_is_tunnel "${previous_pid:-}"; then
        public_url=$(find_public_url)
        if tunnel_uses_requested_port "$previous_pid" && [[ -n "$public_url" ]] && external_health_ok "$public_url"; then
            printf '%s\n' "$public_url" >"$URL_FILE"
            chmod 0600 "$URL_FILE"
            printf 'Cloudflare 임시 HTTPS 터널이 이미 준비되어 있습니다 (PID %s).\n' "$previous_pid"
            printf '공개 주소: %s\n' "$public_url"
            publish_public_url "$public_url"
            exit 0
        fi
        printf '기존 터널(PID %s)의 대상 포트, 공개 주소 또는 외부 health를 확인하지 못해 안전하게 다시 시작합니다.\n' "$previous_pid" >&2
        terminate_owned_tunnel "$previous_pid" || die "기존 터널(PID $previous_pid)을 종료하지 못했습니다. scripts/stop.sh --tunnel-only를 실행한 뒤 다시 시도하세요."
        rm -f -- "$URL_FILE"
        previous_pid=
    fi
    if [[ ${previous_pid:-} =~ ^[0-9]+$ ]] && kill -0 "$previous_pid" 2>/dev/null; then
        printf '경고: 오래된 PID 파일이 다른 프로세스(PID %s)를 가리켜 삭제합니다. 해당 프로세스에는 신호를 보내지 않습니다.\n' "$previous_pid" >&2
    fi
    rm -f -- "$PID_FILE"
fi

rm -f -- "$URL_FILE"
local_health_ok || die "로컬 서버 http://127.0.0.1:$PORT/health 에 연결할 수 없습니다. scripts/start-server.sh를 먼저 실행하세요."

terminate_started_tunnel() {
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
    rm -f -- "$URL_FILE"
}

cleanup_failed_start() {
    local status=$?
    trap - EXIT INT TERM HUP
    if ((START_CLEANUP_ARMED == 1)) && [[ -n "$TUNNEL_PID" ]]; then
        terminate_started_tunnel "$TUNNEL_PID"
    fi
    if [[ -n "$PID_TEMPORARY" ]]; then
        rm -f -- "$PID_TEMPORARY"
    fi
    exit "$status"
}

cleanup_interrupted_start() {
    local signal=$1
    trap - EXIT INT TERM HUP
    if [[ -n "$TUNNEL_PID" ]]; then
        terminate_started_tunnel "$TUNNEL_PID"
    fi
    if [[ -n "$PID_TEMPORARY" ]]; then
        rm -f -- "$PID_TEMPORARY"
    fi
    printf '터널 시작이 %s 신호로 취소됐습니다.\n' "$signal" >&2
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
printf '\n[%s] tunnel start requested (target=http://127.0.0.1:%s)\n' "$(date --iso-8601=seconds)" "$PORT" >>"$LOG_FILE"
nohup setsid "$CLOUDFLARED" tunnel \
    --url "http://127.0.0.1:$PORT" \
    --no-autoupdate \
    >>"$LOG_FILE" 2>&1 </dev/null 9>&- &
TUNNEL_PID=$!
START_CLEANUP_ARMED=1

PID_TEMPORARY="$PID_FILE.$$"
printf '%s\n' "$TUNNEL_PID" >"$PID_TEMPORARY"
chmod 0600 "$PID_TEMPORARY"
mv -f -- "$PID_TEMPORARY" "$PID_FILE"
PID_TEMPORARY=

deadline=$((SECONDS + TUNNEL_TIMEOUT))
public_url=
while ((SECONDS < deadline)); do
    if ! pid_is_live "$TUNNEL_PID"; then
        wait "$TUNNEL_PID" 2>/dev/null || true
        remove_our_pid_file "$TUNNEL_PID"
        rm -f -- "$URL_FILE"
        START_CLEANUP_ARMED=0
        trap - EXIT INT TERM HUP
        printf '터널이 공개 주소를 만들기 전에 종료됐습니다. 최근 로그:\n' >&2
        tail -n 30 "$LOG_FILE" >&2 || true
        exit 1
    fi
    public_url=$(find_public_url)
    if [[ -n "$public_url" ]] && external_health_ok "$public_url"; then
        printf '%s\n' "$public_url" >"$URL_FILE"
        chmod 0600 "$URL_FILE"
        START_CLEANUP_ARMED=0
        trap - EXIT INT TERM HUP
        printf 'Cloudflare 임시 HTTPS 터널 시작 완료 (PID %s).\n' "$TUNNEL_PID"
        printf '공개 주소: %s\n' "$public_url"
        printf '이 주소는 터널을 다시 시작하면 바뀝니다. 로그: %s\n' "$LOG_FILE"
        publish_public_url "$public_url"
        exit 0
    fi
    sleep 1
done

printf '터널이 %s초 안에 외부 health 확인을 마치지 못했습니다. 이번 시작에서 만든 프로세스를 종료합니다.\n' "$TUNNEL_TIMEOUT" >&2
terminate_started_tunnel "$TUNNEL_PID"
START_CLEANUP_ARMED=0
trap - EXIT INT TERM HUP
printf '최근 로그:\n' >&2
tail -n 30 "$LOG_FILE" >&2 || true
exit 1
