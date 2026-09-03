#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
SOURCE="$PROJECT_ROOT/.data/classroom.sqlite3"

usage() {
    cat <<'EOF'
Usage: scripts/backup.sh DESTINATION.sqlite3

Create a consistent SQLite snapshot, including committed data that may still
be in the WAL. The destination must not already exist. Keep it in an encrypted
private location because it contains password hashes and transcripts.
EOF
}

die() {
    printf '오류: %s\n' "$*" >&2
    exit 1
}

if (($# == 1)) && [[ "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 0
fi
(($# == 1)) || {
    usage >&2
    exit 2
}

[[ -x "$PYTHON" ]] || die "scripts/setup.sh를 먼저 실행하세요."
[[ -f "$SOURCE" ]] || die "백업할 데이터베이스가 없습니다: $SOURCE"

"$PYTHON" - "$SOURCE" "$1" <<'PY'
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

source = Path(sys.argv[1]).resolve(strict=True)
destination = Path(sys.argv[2]).expanduser().resolve(strict=False)

if destination == source:
    raise SystemExit("오류: 원본 데이터베이스를 백업 대상으로 지정할 수 없습니다.")
if destination.exists():
    raise SystemExit(f"오류: 기존 파일을 덮어쓰지 않습니다: {destination}")

destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
)
os.close(descriptor)
temporary = Path(temporary_name)
os.chmod(temporary, 0o600)

source_connection = None
destination_connection = None
try:
    source_uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=10)
    destination_connection = sqlite3.connect(temporary, timeout=10)
    source_connection.backup(destination_connection)
    result = destination_connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {result!r}")
    destination_connection.close()
    destination_connection = None
    source_connection.close()
    source_connection = None
    os.replace(temporary, destination)
    os.chmod(destination, 0o600)
except BaseException:
    if destination_connection is not None:
        destination_connection.close()
    if source_connection is not None:
        source_connection.close()
    temporary.unlink(missing_ok=True)
    raise

print(f"백업 완료: {destination}")
PY
