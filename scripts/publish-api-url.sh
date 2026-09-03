#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
DATA_DIR="$PROJECT_ROOT/.data"
LOCK_FILE=${PAGES_PUBLISH_LOCK:-"$DATA_DIR/pages-publish.lock"}
DESIRED_FILE=${PAGES_DESIRED_FILE:-"$DATA_DIR/pages-desired-config.json"}
REPOSITORY="superwonso/stt_server_cdh"
WORKFLOW="pages.yml"
VARIABLE="CLASSROOM_API_CONFIG"
CONFIG_URL=${PAGES_CONFIG_URL:-https://superwonso.github.io/stt_server_cdh/config.json}
PUBLISH_TIMEOUT=${PAGES_PUBLISH_TIMEOUT:-180}
GITHUB_TIMEOUT=${PAGES_GITHUB_TIMEOUT:-30}
WAIT_FOR_PAGES=1
WAIT_ONLY=0
VALUE=

usage() {
    cat <<'EOF'
Usage: scripts/publish-api-url.sh URL [--no-wait]
       scripts/publish-api-url.sh --stdin [--no-wait]
       scripts/publish-api-url.sh --wait-only URL
       scripts/publish-api-url.sh --offline [--no-wait]

Publish the current Quick Tunnel origin to the GitHub Pages runtime artifact.
Only the public tunnel origin and timestamps are published. Account IDs and
credentials are never read by this script.

Options:
  --offline       Publish an explicit server-offline state
  --stdin         Read the server URL from standard input
  --no-wait       Dispatch the Pages deployment without waiting for the CDN
  --wait-only     Wait for the last locally requested state without mutating GitHub
  -h, --help      Show this help
EOF
}

die() {
    printf '오류: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --offline)
            [[ -z "$VALUE" ]] || die "서버 주소와 --offline을 함께 사용할 수 없습니다."
            VALUE=OFFLINE
            shift
            ;;
        --stdin)
            [[ -z "$VALUE" ]] || die "서버 주소와 --stdin을 함께 사용할 수 없습니다."
            IFS= read -r VALUE || die "표준 입력에서 서버 주소를 읽지 못했습니다."
            shift
            ;;
        --no-wait)
            WAIT_FOR_PAGES=0
            shift
            ;;
        --wait-only)
            WAIT_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*)
            die "알 수 없는 옵션입니다: $1"
            ;;
        *)
            [[ -z "$VALUE" ]] || die "서버 주소는 하나만 지정하세요."
            VALUE=$1
            shift
            ;;
    esac
done

[[ -n "$VALUE" ]] || die "게시할 서버 주소 또는 --offline이 필요합니다."
if ((WAIT_ONLY == 1 && WAIT_FOR_PAGES == 0)); then
    die "--wait-only와 --no-wait을 함께 사용할 수 없습니다."
fi
if [[ ! "$PUBLISH_TIMEOUT" =~ ^[0-9]+$ ]] || ((PUBLISH_TIMEOUT < 1 || PUBLISH_TIMEOUT > 600)); then
    die "PAGES_PUBLISH_TIMEOUT은 1~600초여야 합니다."
fi
if [[ ! "$GITHUB_TIMEOUT" =~ ^[0-9]+$ ]] || ((GITHUB_TIMEOUT < 1 || GITHUB_TIMEOUT > 120)); then
    die "PAGES_GITHUB_TIMEOUT은 1~120초여야 합니다."
fi

PYTHON=${PYTHON_BIN:-}
if [[ -z "$PYTHON" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=$(command -v python3)
    elif [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
        PYTHON="$PROJECT_ROOT/.venv/bin/python"
    else
        die "런타임 설정을 검증할 Python 3를 찾을 수 없습니다."
    fi
fi
[[ -x "$PYTHON" ]] || die "Python 3를 실행할 수 없습니다."
if ! DESIRED_CONFIG=$(printf '%s' "$VALUE" \
    | "$PYTHON" "$SCRIPT_DIR/runtime_config.py" desired 2>/dev/null); then
    die "게시할 서버 주소 형식이 올바르지 않습니다."
fi

wait_for_pages() {
    command -v curl >/dev/null 2>&1 || die "Pages 배포 확인에 필요한 curl을 찾을 수 없습니다."
    local current_desired deadline=$((SECONDS + PUBLISH_TIMEOUT)) next_update=$((SECONDS + 15)) separator
    while ((SECONDS < deadline)); do
        [[ -r "$DESIRED_FILE" ]] || die "Pages 게시 상태가 더 새로운 요청으로 바뀌었습니다."
        current_desired=$(<"$DESIRED_FILE")
        [[ "$current_desired" == "$DESIRED_CONFIG" ]] \
            || die "Pages 게시 상태가 더 새로운 요청으로 바뀌었습니다."
        separator='?'
        [[ "$CONFIG_URL" == *\?* ]] && separator='&'
        if curl --silent --show-error --fail --location \
            --connect-timeout 3 --max-time 8 --max-filesize 4096 \
            "${CONFIG_URL}${separator}check=${RANDOM}-${SECONDS}-$$" 2>/dev/null \
            | CLASSROOM_API_EXPECTED="$DESIRED_CONFIG" \
                "$PYTHON" "$SCRIPT_DIR/runtime_config.py" matches >/dev/null 2>&1; then
            printf 'GitHub Pages 자동 서버 연결 설정이 준비됐습니다.\n'
            return
        fi
        if ((SECONDS >= next_update)); then
            printf 'GitHub Pages 배포를 기다리는 중입니다...\n'
            next_update=$((SECONDS + 15))
        fi
        sleep 2
    done
    die "GitHub Pages가 ${PUBLISH_TIMEOUT}초 안에 새 서버 설정을 배포하지 못했습니다. Actions 상태를 확인하세요."
}

if ((WAIT_ONLY == 1)); then
    [[ -r "$DESIRED_FILE" ]] || die "기다릴 로컬 Pages 게시 상태가 없습니다. 먼저 게시를 요청하세요."
    DESIRED_CONFIG=$(<"$DESIRED_FILE")
    if ! printf '%s' "$DESIRED_CONFIG" \
        | CLASSROOM_API_EXPECTED_VALUE="$VALUE" \
            "$PYTHON" "$SCRIPT_DIR/runtime_config.py" matches-value >/dev/null 2>&1; then
        die "기다리던 서버 상태가 더 새로운 게시 요청으로 바뀌었습니다."
    fi
    wait_for_pages
    exit 0
fi

GH=${GH_BIN:-}
if [[ -z "$GH" ]] && command -v gh >/dev/null 2>&1; then
    GH=$(command -v gh)
fi
if [[ -z "$GH" ]]; then
    for candidate in "$PROJECT_ROOT"/.tools/gh-*/bin/gh; do
        [[ -x "$candidate" ]] && GH=$candidate
    done
fi
[[ -n "$GH" && -x "$GH" ]] \
    || die "GitHub CLI를 찾을 수 없습니다. GitHub 게시 설정을 먼저 완료하세요."

mkdir -p -- "$DATA_DIR"
chmod 0700 "$DATA_DIR"
mkdir -p -- "$(dirname -- "$LOCK_FILE")"
mkdir -p -- "$(dirname -- "$DESIRED_FILE")"
command -v flock >/dev/null 2>&1 || die "게시 작업 잠금에 필요한 flock을 찾을 수 없습니다."
command -v timeout >/dev/null 2>&1 || die "GitHub 요청 시간 제한에 필요한 timeout을 찾을 수 없습니다."
exec 9>"$LOCK_FILE"
chmod 0600 "$LOCK_FILE"
flock 9

# Suppress CLI diagnostics so an unusual credential helper can never copy a
# credential into the classroom operator's terminal or runtime log.
if ! printf '%s' "$DESIRED_CONFIG" \
    | timeout --signal=TERM --kill-after=5 "$GITHUB_TIMEOUT" \
        "$GH" variable set "$VARIABLE" --repo "$REPOSITORY" >/dev/null 2>&1; then
    die "GitHub Actions 설정을 갱신하지 못했습니다. 이 컴퓨터의 GitHub 로그인을 확인하세요."
fi
if ! timeout --signal=TERM --kill-after=5 "$GITHUB_TIMEOUT" \
    "$GH" workflow run "$WORKFLOW" --repo "$REPOSITORY" --ref main >/dev/null 2>&1; then
    die "GitHub Pages 자동 배포를 요청하지 못했습니다. GitHub 로그인과 Actions 설정을 확인하세요."
fi

DESIRED_TEMPORARY="$DESIRED_FILE.$$"
printf '%s\n' "$DESIRED_CONFIG" >"$DESIRED_TEMPORARY"
chmod 0600 "$DESIRED_TEMPORARY"
mv -f -- "$DESIRED_TEMPORARY" "$DESIRED_FILE"

# The lock protects only the desired-state update. CDN propagation can take
# minutes; holding it while polling would prevent a concurrent stop from
# publishing OFFLINE and could leave a dead online address as the last deploy.
flock -u 9
exec 9>&-

if ((WAIT_FOR_PAGES == 0)); then
    printf 'GitHub Pages 런타임 설정 게시를 요청했습니다.\n'
    exit 0
fi
wait_for_pages
