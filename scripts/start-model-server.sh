#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { printf '먼저 scripts/setup.sh를 실행하세요.\n' >&2; exit 1; }
cd -- "$PROJECT_ROOT"
# The controller selects model-only env keys; never source the private .env.
exec "$PYTHON" -m server.model_process start --socket "${LOCAL_MODEL_SOCKET:-$PROJECT_ROOT/.data/model-server/model.sock}" "$@"
