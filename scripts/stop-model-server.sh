#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { printf '모델 관리 Python 환경을 찾지 못했습니다.\n' >&2; exit 1; }
cd -- "$PROJECT_ROOT"
# Independent process ownership checks; no API/tunnel process is signalled.
exec "$PYTHON" -m server.model_process stop --socket "${LOCAL_MODEL_SOCKET:-$PROJECT_ROOT/.data/model-server/model.sock}" --timeout "${STOP_TIMEOUT:-20}" "$@"
