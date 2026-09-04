#!/usr/bin/env bash

set -Eeuo pipefail
umask 077
export PYTHONNOUSERSITE=1

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
VENV_DIR="$PROJECT_ROOT/.venv"
REQUIREMENTS_FILE="$PROJECT_ROOT/server/requirements.txt"
DEFAULT_ROCM_PYTHON="${HOME}/miniconda3/envs/torch-rocm/bin/python"
ROCM_PYTHON=${ROCM_PYTHON:-$DEFAULT_ROCM_PYTHON}
INSTALL_CLOUDFLARED=1
INSTALL_MODEL=1
RECREATE_VENV=0
MODEL_REPOSITORY="Qwen/Qwen3-ASR-1.7B"
MODEL_REVISION="7278e1e70fe206f11671096ffdd38061171dd6e5"
MODEL_DIRECTORY="$PROJECT_ROOT/.models/Qwen3-ASR-1.7B"
ALIGNER_REPOSITORY="Qwen/Qwen3-ForcedAligner-0.6B"
ALIGNER_REVISION="c7cbfc2048c462b0d63a45797104fc9db3ad62b7"
ALIGNER_DIRECTORY="$PROJECT_ROOT/.models/Qwen3-ForcedAligner-0.6B"
CLOUDFLARED_VERSION="2026.8.3"

usage() {
    cat <<'EOF'
Usage: scripts/setup.sh [options]

Create .venv with the existing ROCm Python environment exposed through
system-site-packages, install server/requirements.txt, and install cloudflared
locally when it is not already available.

Options:
  --python PATH          ROCm environment's Python executable
  --recreate-venv        Replace an incompatible existing .venv
  --skip-cloudflared     Do not find or download cloudflared
  --skip-model           Do not download/verify Qwen3-ASR
  -h, --help             Show this help

ROCM_PYTHON can also provide the Python path.
EOF
}

die() {
    printf '오류: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --python)
            (($# >= 2)) || die "--python 뒤에 경로가 필요합니다."
            ROCM_PYTHON=$2
            shift 2
            ;;
        --recreate-venv)
            RECREATE_VENV=1
            shift
            ;;
        --skip-cloudflared)
            INSTALL_CLOUDFLARED=0
            shift
            ;;
        --skip-model)
            INSTALL_MODEL=0
            shift
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

[[ -f "$REQUIREMENTS_FILE" ]] || die "요구사항 파일이 없습니다: $REQUIREMENTS_FILE"
[[ -x "$ROCM_PYTHON" ]] || die "ROCm Python을 실행할 수 없습니다: $ROCM_PYTHON"

ROCM_PYTHON=$(readlink -f -- "$ROCM_PYTHON")
"$ROCM_PYTHON" - <<'PY'
import sys

if not ((3, 10) <= sys.version_info < (3, 14)):
    raise SystemExit("Python 3.10~3.13이 필요합니다.")
try:
    import torch
except ImportError as error:
    raise SystemExit("선택한 Python 환경에서 PyTorch를 불러올 수 없습니다.") from error
if not torch.version.hip:
    raise SystemExit("선택한 Python의 PyTorch는 ROCm 빌드가 아닙니다.")
print(f"ROCm Python: {sys.executable}")
print(f"PyTorch: {torch.__version__} (ROCm {torch.version.hip})")
if not torch.cuda.is_available():
    print("경고: 현재 셸에서는 PyTorch가 GPU를 사용할 수 없습니다. WSL의 /dev/dxg와 ROCm 권한을 확인하세요.")
PY

venv_is_compatible() {
    [[ -x "$VENV_DIR/bin/python" && -f "$VENV_DIR/pyvenv.cfg" ]] || return 1
    grep -Eiq '^include-system-site-packages[[:space:]]*=[[:space:]]*true[[:space:]]*$' "$VENV_DIR/pyvenv.cfg" || return 1
    "$VENV_DIR/bin/python" - "$ROCM_PYTHON" <<'PY'
import pathlib
import sys
import sysconfig

requested = pathlib.Path(sys.argv[1]).resolve()
base = pathlib.Path(sys._base_executable).resolve()
requested_prefix = pathlib.Path(requested).parent.parent
base_prefix = pathlib.Path(base).parent.parent
if requested_prefix != base_prefix:
    raise SystemExit(1)
try:
    import torch
except ImportError:
    raise SystemExit(1)
if not torch.version.hip:
    raise SystemExit(1)
purelib = pathlib.Path(sysconfig.get_path("purelib")).resolve()
torch_path = pathlib.Path(torch.__file__).resolve()
if purelib in torch_path.parents or requested_prefix not in torch_path.parents:
    raise SystemExit(1)
PY
}

if [[ -e "$VENV_DIR" ]] && ! venv_is_compatible; then
    if ((RECREATE_VENV == 0)); then
        die ".venv가 선택한 ROCm 환경을 system-site-packages로 재사용하지 않습니다. 내용을 확인한 뒤 --recreate-venv로 다시 실행하세요."
    fi
    mkdir -p -- "$PROJECT_ROOT/.data"
    chmod 0700 "$PROJECT_ROOT/.data"
    backup="$PROJECT_ROOT/.data/venv-backup-$(date +%Y%m%d-%H%M%S)-$$"
    mv -- "$VENV_DIR" "$backup"
    printf '기존 .venv를 보존했습니다: %s\n' "$backup"
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    printf '.venv를 생성합니다 (ROCm 패키지 재사용)...\n'
    "$ROCM_PYTHON" -m venv --system-site-packages "$VENV_DIR"
fi

if ! venv_is_compatible; then
    die ".venv에서 ROCm PyTorch 재사용을 확인하지 못했습니다."
fi

printf '서버 Python 패키지를 설치합니다...\n'
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS_FILE"

"$VENV_DIR/bin/python" - <<'PY'
import torch

print(f".venv가 재사용하는 PyTorch: {torch.__version__} (ROCm {torch.version.hip})")
print(f"PyTorch 위치: {torch.__file__}")
PY

install_model() {
    mkdir -p -- "$PROJECT_ROOT/.models"
    chmod 0700 "$PROJECT_ROOT/.models"
    [[ -x "$VENV_DIR/bin/hf" ]] || die "qwen-asr 설치 후에도 Hugging Face CLI를 찾지 못했습니다."
    download_snapshot() {
        local repository=$1 revision=$2 directory=$3 attempt
        for attempt in 1 2 3; do
            if env HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=600 \
                "$VENV_DIR/bin/hf" download "$repository" --revision "$revision" --local-dir "$directory"; then
                return
            fi
            printf '다운로드 연결이 끊겼습니다 (%s/3). 기존 파일에서 다시 시도합니다.\n' "$attempt" >&2
            sleep 2
        done
        die "모델 다운로드를 세 번 완료하지 못했습니다: $repository"
    }

    printf 'Qwen3-ASR 1.7B 모델을 확인합니다 (약 4.4 GiB)...\n'
    download_snapshot "$MODEL_REPOSITORY" "$MODEL_REVISION" "$MODEL_DIRECTORY"
    printf 'Qwen3 한국어 경계 정렬기를 확인합니다 (약 1.7 GiB)...\n'
    download_snapshot "$ALIGNER_REPOSITORY" "$ALIGNER_REVISION" "$ALIGNER_DIRECTORY"
    "$VENV_DIR/bin/python" - "$MODEL_DIRECTORY" <<'PY'
import json
import pathlib
import sys

directory = pathlib.Path(sys.argv[1])
index_path = directory / "model.safetensors.index.json"
if not index_path.is_file():
    raise SystemExit("모델 인덱스 파일이 없습니다.")
index = json.loads(index_path.read_text(encoding="utf-8"))
missing = sorted({name for name in index["weight_map"].values() if not (directory / name).is_file()})
if missing:
    raise SystemExit("모델 shard가 없습니다: " + ", ".join(missing))
print(f"로컬 모델 확인 완료: {directory}")
PY
    [[ -s "$ALIGNER_DIRECTORY/model.safetensors" ]] \
        || die "정렬기 가중치가 없습니다: $ALIGNER_DIRECTORY/model.safetensors"
    printf '로컬 정렬기 확인 완료: %s\n' "$ALIGNER_DIRECTORY"
}

if ((INSTALL_MODEL == 1)); then
    install_model
fi

install_cloudflared() {
    local local_binary="$PROJECT_ROOT/.tools/cloudflared"
    local arch checksum download_url temporary actual_checksum

    # Immutable release and checksums from the official Cloudflare release:
    # https://github.com/cloudflare/cloudflared/releases/tag/2026.8.3
    case "$(uname -m)" in
        x86_64|amd64)
            arch=amd64
            checksum=f29324fe934d1e100617484c78deef803c4dc2cd351d645bbde42e96b4fccc5e
            ;;
        aarch64|arm64)
            arch=arm64
            checksum=4bcfd35521a7cbc545ebfd5d57334a71ee180e2a64874981f374c81472118391
            ;;
        armv7l|armhf)
            arch=arm
            checksum=7a7cac4ad4561ff55797eaf27aae1a0be37498c85502715bc87e3bad919d928c
            ;;
        *) die "지원하지 않는 CPU 아키텍처입니다: $(uname -m). cloudflared를 직접 .tools/cloudflared에 설치하세요." ;;
    esac

    if [[ -x "$local_binary" ]] \
        && "$local_binary" --version 2>/dev/null | grep -Fq "version $CLOUDFLARED_VERSION " \
        && [[ "$(sha256sum "$local_binary" | cut -d' ' -f1)" == "$checksum" ]]; then
        printf '로컬 cloudflared 사용: %s\n' "$local_binary"
        return
    fi
    if command -v cloudflared >/dev/null 2>&1; then
        printf '시스템 cloudflared 사용 가능: %s\n' "$(command -v cloudflared)"
        return
    fi

    download_url="https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-$arch"
    mkdir -p -- "$PROJECT_ROOT/.tools"
    temporary=$(mktemp "${TMPDIR:-/tmp}/classroom-cloudflared.XXXXXX")
    trap 'rm -f -- "$temporary"' EXIT

    printf 'cloudflared를 .tools에 내려받습니다...\n'
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --show-error --silent "$download_url" --output "$temporary"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet --output-document="$temporary" "$download_url"
    else
        die "cloudflared 다운로드에 curl 또는 wget이 필요합니다."
    fi
    actual_checksum=$(sha256sum "$temporary" | cut -d' ' -f1)
    [[ "$actual_checksum" == "$checksum" ]] \
        || die "cloudflared SHA-256이 공식 $CLOUDFLARED_VERSION 체크섬과 다릅니다."
    chmod 0755 "$temporary"
    "$temporary" --version >/dev/null
    install -m 0755 "$temporary" "$local_binary"
    rm -f -- "$temporary"
    trap - EXIT
    printf '로컬 cloudflared 설치 완료: %s\n' "$local_binary"
}

if ((INSTALL_CLOUDFLARED == 1)); then
    install_cloudflared
fi

mkdir -p -- "$PROJECT_ROOT/.data" "$PROJECT_ROOT/.models"
chmod 0700 "$PROJECT_ROOT/.data" "$PROJECT_ROOT/.models"

printf '\n설정이 끝났습니다. 기본 모델은 Qwen3-ASR 1.7B입니다.\n'
printf '다음 단계: server/.env를 준비한 뒤 scripts/start-server.sh를 실행하세요.\n'
