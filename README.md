# 여백 · 수업 받아쓰기

노트북·태블릿의 마이크, 데스크톱 브라우저가 공유한 탭/화면 소리, 기존 녹음 파일을 이 PC로 보내고 로컬 `Qwen3-ASR-1.7B`와 `Qwen3-ForcedAligner-0.6B`로 한국어 수업 기록을 만드는 개인용 웹 앱입니다. 화면만 GitHub Pages에 공개되고, 로그인·음성 인식·수업 기록은 WSL이 실행되는 이 컴퓨터에서 처리됩니다.

사용할 주소와 현재 준비 상태는 다음과 같습니다.

- 저장소: <https://github.com/superwonso/stt_server_cdh>
- 웹 화면: <https://superwonso.github.io/stt_server_cdh/>
- API: 수업 때마다 새로 만들어지는 `https://….trycloudflare.com`
- 계정: 관리자에게 요청
- **확인 완료:** GitHub Pages 배포, Quick Tunnel 외부 `/health`, Pages CORS, 허용하지 않은 웹 출처 차단
- **사용자 작업 필요:** 두 사용자의 일회용 링크 비밀번호 설정, 실제 학교 Wi-Fi·태블릿·수업 음성 시험

현재 방식은 단어마다 바로 바뀌는 자막이 아닙니다. 실시간 입력과 파일 변환 모두 새 음성을 최소 약 8초 모으고 말이 잠시 멈춘 지점에서 자르되, 최대 15초 WAV와 이전 3초 겹침을 사용합니다. 서버는 Qwen 강제 정렬의 단어 시각과 0.6초 안정 구간으로 각 겹침의 저장 범위를 나눕니다. FLEURS 시험에서는 정확히 반복된 8-gram이 0건이었지만, 서로 독립적으로 인식한 청크의 정렬 시각은 흔들릴 수 있으므로 실제 수업에서 모든 말을 수학적으로 정확히 한 번씩 저장한다고 보장하지는 않습니다.

## 구조와 개인정보

```text
노트북/태블릿 브라우저
  ├─ 웹 화면 ───────────────────────────── GitHub Pages
  ├─ 마이크/공유 탭 소리의 겹친 WAV ─┐
  └─ 녹음 파일의 재개 가능 조각 ─────┴─ Cloudflare Quick Tunnel ─ 127.0.0.1 FastAPI
                                                                    ├─ ROCm Qwen3-ASR
                                                                    ├─ .data/classroom.sqlite3
                                                                    └─ .data/imports/* (변환 중만)
```

- 브라우저에서 받은 음성은 Cloudflare를 경유해 이 PC로 전송됩니다. Cloudflare가 HTTPS를 종단하는 구조이므로 Cloudflare를 통하지 않는 종단간 암호화라고 표현하면 안 됩니다.
- 실시간 마이크/공유 소리 WAV는 서버 메모리에서 인식한 뒤 버리고 디스크에 쓰지 않습니다. 녹음 파일 가져오기는 전송 재개와 서버 디코딩을 위해 계정별 `0700` 폴더에 `0600` 원본을 임시 저장합니다. 완료·취소·실패 때 삭제를 확인하고, 실패하면 상태에 그대로 표시한 채 실행 중인 서버가 재시도합니다. 7일간 끝내지 않은 업로드도 자동 폐기합니다.
- 비밀번호는 Argon2 해시로, 로그인 세션과 초대 코드는 해시로 저장합니다. 로그인 토큰은 브라우저 메모리에만 두므로 탭을 닫으면 사라집니다.
- 두 로그인 ID는 Git에서 제외된 `server/.env`와 로컬 DB에만 둡니다. 공개 예시의 `ACCOUNT_USERNAMES`는 의도적으로 비어 있으며, 설정이 없거나 기존 DB의 계정과 다르면 서버는 계정을 임의로 추가하지 않고 시작을 중단합니다.
- 모든 수업 조회·업로드에 서버 측 소유권 검사를 적용합니다.
- 웹 코드에는 계정 목록, 비밀번호, 초대 코드가 없습니다. 브라우저 `localStorage`에는 현재 API 주소만 저장됩니다.
- GitHub Pages의 같은 계정 아래 프로젝트들은 `https://superwonso.github.io`라는 웹 출처를 공유합니다. 이 앱을 쓰는 동안 `superwonso` 계정의 다른 Pages 저장소도 신뢰할 수 있어야 합니다. 강한 출처 격리가 필요하면 향후 전용 Pages 계정이나 전용 도메인으로 옮겨야 합니다.
- `.data/`, `server/.env`, `.models/`, `.samples/`, 지원하는 음성·영상 컨테이너 확장자, 로그는 `.gitignore`에 포함됩니다. 그래도 푸시 전 `git status`에서 개인 파일이 없는지 반드시 확인하세요. `git add -f`로 강제로 추가하지 마세요.

## 이 PC에서 처음 한 번 설치

확인된 대상 환경은 Ubuntu 24.04 WSL2, Radeon 8060S(gfx1151), ROCm 7.2.4와 `/home/wonso/miniconda3/envs/torch-rocm`입니다. 다른 PC에서는 Python 경로와 ROCm 호환성을 별도로 확인해야 합니다.

```bash
cd /home/wonso/stt_server
chmod +x scripts/*.sh
./scripts/setup.sh
install -m 600 server/env.example server/.env
```

`server/.env`를 이 컴퓨터에서만 열어 `ACCOUNT_USERNAMES`에 쉼표로 구분한 두 로그인 ID를 입력하세요. ID는 소문자 영문·숫자·점·밑줄·하이픈으로 된 1~32자여야 하며 서로 달라야 합니다. 실제 값이 든 파일은 커밋하거나 다른 곳에 공유하지 마세요.

`setup.sh`는 기존 ROCm Python을 재사용하는 `.venv`를 만들고 서버 패키지, 약 6.1 GiB의 Qwen3-ASR·강제 정렬기, 로컬 `cloudflared` 실행 파일을 준비합니다. 검증한 모델 revision과 cloudflared 2026.8.3의 공식 SHA-256을 고정하며, 시스템 ROCm 환경을 덮어쓰지 않습니다. ROCm Python 위치가 바뀌었다면 다음처럼 지정합니다.

```bash
./scripts/setup.sh --python /실제/rocm/python/경로
```

이미 잘못 만든 `.venv`를 교체해야 할 때만 `--recreate-venv`를 사용합니다. 기존 환경은 `.data/venv-backup-*`으로 옮겨 보존됩니다.

GPU가 보이는지 확인하려면 다음을 실행합니다.

```bash
./.venv/bin/python -c "import torch; print(torch.__version__, torch.version.hip, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
```

마지막 값이 `False`이면 서버를 시작하지 말고 WSL의 `/dev/dxg`, ROCm 설치, 사용 중인 Python 경로부터 확인하세요.

## GitHub Pages 배포

저장소의 `main` 브랜치에 푸시하면 `.github/workflows/pages.yml`이 `web/` 폴더만 Pages 아티팩트로 배포합니다. GitHub 저장소의 **Settings → Pages → Build and deployment → Source**에서 **GitHub Actions**를 선택하고, Actions의 `Deploy classroom to GitHub Pages` 작업이 성공했는지 확인합니다.

배포 후 <https://superwonso.github.io/stt_server_cdh/>를 열어 화면이 나오는지 확인하세요. `web/config.json`의 `apiUrl`은 비워 두는 것이 정상입니다. Quick Tunnel 주소는 매번 바뀌므로 페이지의 **내 서버** 버튼에서 현재 주소를 입력합니다.

GitHub에 올리기 전에는 최소한 다음을 확인합니다.

```bash
git status --short
git check-ignore server/.env .data/classroom.sqlite3 .data/invitations.txt .models/Qwen3-ASR-1.7B
```

두 번째 명령에 네 경로가 모두 표시되어야 합니다. 저장소 인증용 토큰도 파일이나 셸 스크립트에 적지 마세요.

## 두 계정의 첫 비밀번호 설정

초대 링크는 Quick Tunnel 주소가 만들어진 뒤 생성합니다. 먼저 서버와 터널을 실행합니다.

```bash
cd /home/wonso/stt_server
./scripts/start-server.sh
./scripts/start-tunnel.sh
./scripts/status.sh
```

`status.sh`가 보여 준 `https://….trycloudflare.com` 주소를 아래의 `--api-url` 값에 그대로 넣습니다.

```bash
./.venv/bin/python -m server.manage init \
  --site-url https://superwonso.github.io/stt_server_cdh/ \
  --api-url https://여기에-현재주소.trycloudflare.com
```

이 명령은 다음 작업만 합니다.

- 아직 비밀번호가 없는 계정마다 7일 유효한 일회용 초대 코드를 새로 만듭니다.
- 원문 링크와 현재 서버 주소는 Git에서 제외된 `.data/invitations.txt`에만 기록합니다.
- **초대 링크에는 서버 주소를 넣지 않습니다.** 위조된 링크가 초대 코드와 새 비밀번호를 공격자 서버로 보내지 못하게 하기 위한 조치입니다.
- Pages 출처를 `server/.env`에 반영합니다. 자주 바뀌는 API 주소는 초대 링크나 서버 설정에 결합하지 않습니다.
- 이미 활성화된 계정의 비밀번호는 바꾸지 않습니다.

링크나 코드가 출력되지 않는 안전한 상태 확인은 `./.venv/bin/python -m server.manage status`로 할 수 있습니다.

출처 설정을 적용하려면 API 서버만 다시 시작합니다. 터널은 그대로 유지합니다.

```bash
./scripts/stop.sh --server-only
./scripts/start-server.sh
```

이 PC에서 `.data/invitations.txt`를 열고 각 사용자에게 다음 두 항목을 서로 구분해 직접 전달합니다.

1. 현재 `https://….trycloudflare.com` 서버 주소
2. 본인에게만 해당하는 Pages 초대 링크

링크를 받은 사람은 **먼저 일반 Pages 화면을 열고 `내 서버`에 전달받은 서버 주소를 입력해 연결을 확인한 다음**, 본인 초대 링크를 엽니다. 그 뒤 4자 이상의 새 비밀번호를 두 번 입력합니다. 4자도 허용하지만 추측하기 쉬우므로 가능하면 더 길게 정하고, 4자리 숫자·흔한 단어는 피하세요. 브라우저는 초대 링크의 `username`과 `setup_code`만 복원하며, 링크에 `api=`가 위조돼 있어도 무시합니다. 성공한 링크는 다시 사용할 수 없습니다. 두 사람의 설정이 끝나면 초대 파일은 이 PC 밖으로 백업하지 말고 보관하거나 직접 삭제하세요.

한 사람만 설정하기 전에 터널 주소가 바뀌었다면 **초대 링크는 그대로 두고 새 서버 주소만** 별도로 알려 주세요. 초대 링크는 터널 주소를 포함하지 않으므로 `init`을 다시 실행할 필요가 없습니다. `init`을 다시 실행하면 아직 활성화되지 않은 계정의 초대 코드가 새것으로 교체되어 이전 링크가 무효가 됩니다.

## 수업 전 켜기

수업 시작 10분 전 WSL 터미널에서 다음 세 줄을 실행하는 것을 권장합니다.

```bash
cd /home/wonso/stt_server
./scripts/start-server.sh
./scripts/start-tunnel.sh
./scripts/status.sh
```

`start-server.sh`는 모델을 미리 불러오고 첫 추론 경로를 준비합니다. 첫 준비는 몇 분 걸릴 수 있으며 최대 10분을 기다립니다. 터널은 로컬 서버가 정상일 때만 시작합니다.

그다음 사용자 기기에서 다음 순서로 확인합니다.

1. <https://superwonso.github.io/stt_server_cdh/>를 엽니다.
2. 상단 **내 서버**를 눌러 `status.sh`의 새 HTTPS 주소를 붙여 넣습니다.
3. 본인 아이디와 비밀번호로 로그인합니다.
4. 새 수업 이름과 언어를 고릅니다.
5. 아래의 마이크·컴퓨터 소리·녹음 파일 중 원하는 입력을 사용합니다.

### 마이크로 실시간 받아쓰기

**실시간 입력 → 기기 마이크**를 고르고 **받아쓰기 시작**을 누른 뒤 권한을 허용합니다. 수업 중에는 이 탭과 화면을 가능한 한 유지하세요.

### 유튜브·온라인 강의 등 컴퓨터 소리 받아쓰기

데스크톱 Chrome 또는 Edge에서 **실시간 입력 → 컴퓨터·브라우저 탭 소리**를 고르고 **화면 소리 받아쓰기**를 누릅니다. 공유 창에서 재생할 브라우저 탭을 선택하고 **탭 오디오 공유**를 켜는 방식이 가장 예측 가능합니다. 전체 화면을 고를 때 **시스템 오디오 공유**가 보이면 켤 수 있습니다.

- 브라우저의 화면 공유 API는 사용자가 매번 화면/탭을 직접 골라야 하고, 오디오 제공 여부도 브라우저와 선택한 표면에 따라 달라집니다. 오디오 없는 표면을 고르면 앱이 중단하고 다시 안내합니다. [MDN `getDisplayMedia`](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia), [W3C Screen Capture](https://www.w3.org/TR/screen-capture/)
- API 규칙상 영상 선택도 필요하지만 앱은 반환된 **오디오 트랙만** AudioWorklet에 연결합니다. 영상 프레임은 인코딩·네트워크 전송·저장하지 않습니다.
- 공유 범위에 포함된 알림과 다른 앱의 사적인 소리도 들어갈 수 있습니다. 알림을 끄고 공유 범위를 확인하세요. DRM 보호 영상, 일부 강의 플랫폼, Safari/Firefox, 태블릿에서는 오디오 트랙을 주지 않거나 캡처를 막을 수 있습니다.
- 일시적인 `mute`는 5초간 복구를 기다리고, 공유 중지나 장시간 중단은 마지막 받은 오디오를 정리한 뒤 멈춥니다.

### 녹음 파일 올려 변환

**녹음 파일 가져오기**에서 파일을 고르고 수업 이름·언어를 확인한 뒤 **파일 올려 변환**을 누릅니다. WAV, MP3, M4A/AAC, FLAC, OGG/Opus, WebM과 오디오가 든 MP4·MKV·MOV를 표시하지만, 실제 디코딩 가능 여부는 파일 내부 코덱과 [PyAV/FFmpeg](https://pyav.basswood.io/docs/stable/api/audio.html) 지원에 달려 있습니다. 파일은 최대 1 GiB, 디코딩된 음성은 최대 4시간이며 계정마다 동시에 한 작업만 진행합니다.

- 브라우저는 모든 파일 바이트를 480 KiB 이하로 읽어 SHA-256 조각 목록에 묶습니다. 전체 파일을 한꺼번에 메모리에 올리지 않으며, 업로드 조각도 하나씩 보냅니다.
- **업로드 중에는 탭을 닫지 마세요.** 연결이 끊기면 처음 고른 것과 정확히 같은 파일을 다시 골라 받은 위치부터 이어갈 수 있습니다. 서버가 대기/변환 상태가 된 뒤에는 탭을 닫아도 이 PC에서 계속 처리하고, 다음 로그인 때 상태를 복구합니다.
- 파일 원본은 Cloudflare를 경유하며 이 PC의 `.data/imports/`에만 임시 저장됩니다. 완료·취소·실패 화면의 “원본 임시 파일 삭제됨”을 확인하세요. “삭제 재시도 중”이면 서버를 켜 둔 채 잠시 뒤 다시 로그인해 상태를 확인합니다.
- 취소는 현재 최대 15초짜리 모델 추론을 끝낸 뒤 적용될 수 있습니다. 그 사이 먼저 완료되면 앱은 완료로 표시하며 결과를 삭제하지 않습니다.

임시 네트워크 오류와 서버 혼잡은 같은 음성 조각 ID로 순서대로 최대 8회 자동 재시도합니다. 계속 실패하면 자동 전송과 녹음을 멈추고 WAV를 브라우저 메모리에 보존합니다. **다시 전송**을 누르거나, **실패 WAV 내려받기**를 누른 뒤 파일이 실제로 기기에 저장됐는지 확인하고 **파일 저장 확인 · 건너뛰기**를 눌러 다음 조각을 처리할 수 있습니다. 건너뛴 구간은 서버 기록에서 빠지므로 내려받은 WAV를 별도로 보관해야 합니다. 처리 대기 음성이 8개에 이르면 메모리 폭증을 막기 위해 녹음을 안전하게 멈춥니다. 대기 중 음성은 새로고침이나 탭 종료 후에는 복구할 수 없습니다.

실시간 수업을 끝낼 때 **받아쓰기 중지**를 누르고, 처리 대기 표시가 사라지고 **서버 컴퓨터에 저장됨**이 나온 뒤 필요하면 **텍스트 저장**을 누릅니다. 입력 종료 확인이 실패하면 **마지막 오디오 일부 누락 가능 · 받은 내용만 저장됨** 경고가 다음 녹음 전까지 남습니다. 이 경우 내려받은 텍스트의 끝부분을 직접 확인하세요.

## 종료

실시간 수업의 전송과 파일 업로드가 끝난 것을 웹 화면에서 확인한 뒤 실행합니다. 파일이 이미 **서버 변환 대기/변환 중**이라면 정상 종료 시 완료한 청크 ID를 보존하고 작업을 대기로 돌리므로 다음 서버 시작 때 이어서 처리합니다. 그래도 종료 직전에는 가능하면 변환 완료와 원본 삭제 표시까지 기다리는 편이 안전합니다.

```bash
cd /home/wonso/stt_server
./scripts/stop.sh
./scripts/status.sh
```

종료 스크립트는 먼저 외부 터널을 닫고 API 서버를 정상 종료합니다. 종료 후 기존 텍스트 기록과 아직 끝나지 않은 파일 작업 상태는 `.data/`에 남습니다.

## 백업

SQLite WAL에 남아 있는 최신 기록까지 일관되게 포함하려면 파일을 직접 복사하지 말고 다음 명령을 사용합니다. 서버 실행 중에도 SQLite의 온라인 백업 기능으로 스냅샷을 만들 수 있으며, 기존 대상 파일은 덮어쓰지 않습니다.

```bash
./scripts/backup.sh /암호화된/개인저장소/classroom-$(date +%Y%m%d).sqlite3
```

백업에는 두 계정의 비밀번호 해시와 개인 수업 텍스트가 함께 들어 있습니다. GitHub, 공유 Drive, 메신저에 원본 그대로 올리지 마세요.

## 문제 해결

### 서버가 준비되지 않음

```bash
./scripts/status.sh
tail -n 80 .data/server.log
```

`ROCm GPU is not visible`이면 ROCm Python과 `/dev/dxg`를 확인합니다. `ASR model is incomplete`이면 `./scripts/setup.sh`를 다시 실행합니다. 프로세스가 살아 있지만 준비가 늦다면 모델 다운로드 또는 warmup이 끝날 때까지 로그를 확인합니다.

### 외부에서 연결되지 않음

```bash
tail -n 80 .data/tunnel.log
./scripts/start-tunnel.sh
```

Quick Tunnel을 다시 시작하면 주소가 바뀝니다. 이전 주소가 기기에 저장되어 있으면 Pages 화면의 **내 서버**에서 새 주소로 교체해야 합니다. Cloudflare의 [Quick Tunnel 공식 문서](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)도 이를 테스트·개발용 기능으로 설명하므로, 중요한 수업 전에는 반드시 학교 네트워크의 실제 기기에서 `/health` 연결과 짧은 받아쓰기를 확인하세요.

전송 대기 음성이 남은 상태에서 주소가 바뀌어도, 현재 전송 시도가 끝난 뒤 **내 서버**에서 새 주소를 확인할 수 있습니다. 앱은 대기 WAV와 조각 ID, 기존 사용자를 메모리에 유지합니다. 반드시 같은 계정으로 다시 로그인하면 새 터널을 통해 이어서 전송합니다. 다른 계정은 남은 큐를 가져갈 수 없습니다.

### “허용되지 않은 사이트”가 표시됨

`server/.env`의 `SITE_ORIGINS`가 다음과 같은 정확한 Pages 출처인지 확인합니다.

```dotenv
SITE_ORIGINS=https://superwonso.github.io
```

수정 후 `./scripts/stop.sh --server-only`와 `./scripts/start-server.sh`로 API를 다시 시작합니다. 저장소 경로 `/stt_server_cdh/`는 URL 출처에 포함하지 않습니다.

### 로그인이 되지 않음

- 초대 링크는 7일, 1회만 유효합니다.
- 새 비밀번호는 4~128자입니다. 가능하면 더 길게 정하고 4자리 숫자·흔한 단어는 피하세요.
- 반복 실패에는 IP·IP+계정 기준 5분 제한과, 여러 주소를 합산한 계정별 30분 창(최대 50회)이 적용됩니다.
- Quick Tunnel 주소가 달라졌다면 먼저 **내 서버**의 주소를 바꾸세요.

### 받아쓰기 지연 또는 전송 대기

이 앱은 단일 GPU 추론을 직렬화합니다. 두 사람이 동시에 말하면 한 요청이 대기할 수 있습니다. `429` 또는 일시적인 `503`은 동일 UUID로 최대 8회 자동 재시도하지만, 형식·권한 오류는 즉시 안내를 표시하고 녹음을 멈춥니다. 8회 뒤에도 실패하면 **다시 전송**을 선택하거나, **실패 WAV 내려받기** 후 파일 저장을 직접 확인하고 해당 조각을 건너뛰세요. 처리할 동안 탭을 유지해야 합니다.

### 파일 업로드가 멈추거나 변환되지 않음

- `업로드를 계속하려면`이 보이면 이름만 같은 파일이 아니라 처음의 **동일한 파일 내용**을 다시 고릅니다. 전체 바이트 지문이 다르면 전송 전에 거절합니다.
- 대기/변환 상태는 탭을 닫아도 서버에서 계속됩니다. 다시 로그인하면 자동 복구합니다.
- `지원되는 오디오가 있는 파일` 오류는 확장자와 실제 컨테이너/코덱이 다르거나, 외부 파일을 참조하는 재생목록형 미디어이거나, 오디오 스트림이 없을 때도 표시됩니다.
- `원본 임시 파일 삭제 재시도 중`이면 서버의 파일 권한·디스크 상태를 확인하고 서버를 켜 둡니다. 목록 조회와 백그라운드 정리기가 삭제를 다시 시도합니다.

## 테스트와 로컬 벤치마크

서버/API 테스트:

```bash
./.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

웹 오디오·파일 재개·경쟁 상태 테스트(Node.js 20 이상 필요):

```bash
node --test tests/*.test.mjs
```

Git에서 제외한 FLEURS 한국어 샘플이 있을 때 모델과 실제 3초 겹침·정렬 경로를 각각 재현할 수 있습니다.

```bash
./.venv/bin/python scripts/benchmark_qwen.py --limit 10
./.venv/bin/python scripts/validate_chunk_pipeline.py --limit 10 --warmup
```

같은 FLEURS 한국어 낭독 10개의 현재 실측 요약입니다. CER 정규화와 실행 경로가 다르므로 이 표를 실제 교실 품질 보장이나 모델의 보편적 순위로 읽으면 안 됩니다.

| 실행 경로 | 이 PC 실측 |
| --- | --- |
| Qwen, 파일 전체 | CER 2.24%, RTF 0.592 |
| Qwen, 실제 3초 겹침·pause-aware 경로 | CER 4.03%, RTF 0.466, 마지막 기준 25자 완전 일치, 탐지된 텍스트 중복 0 |
| Qwen, 새 파일 디코더→동일 청크 경로 | WAV 10개/111.12초/19청크, CER 4.03%, 첫 모델 로드 포함 RTF 0.685, allocated 증가 0 MiB |
| Qwen, 동일 경로 60분 35초 연속 시험 | CER 3.51%, RTF 0.442, 마지막 기준 25자 완전 일치, 경계 텍스트 중복 0, GPU allocated 처음→끝 증가 0 MiB |
| Voxtral Realtime, 2400ms | CER 4.70%, WER 21.53%, RTF 1.584로 실시간 입력보다 느림 |
| Whisper turbo, 파일 전체 | CER 4.03%, RTF 0.193 |
| Whisper turbo, 동일 3초 겹침 분할 | CER 23.71%, RTF 0.259, 한 발화 전체 누락 |
| VibeVoice, 짧은 3개 | 간이 CER 25.2%, RTF 1.016/0.753/0.724 |

60분 시험은 같은 FLEURS 낭독 샘플을 순서를 바꾸며 반복한 합성 타임라인입니다. 416청크의 PCM coverage error와 분할 gap/overlap은 0 sample이었고, GPU reserved 처음→끝 증가는 6 MiB, 프로세스 RSS 증가는 12.65 MiB였습니다. 파일 디코더 시험도 깨끗한 낭독 WAV이며 한 샘플에는 날짜 표현의 잘못된 삽입이 있었습니다. 별도의 격리 DB에서 실제 한국어 WAV 한 개를 인증 API의 생성·분할 업로드·Qwen 변환·기록 조회·원본 삭제까지 통과시켰습니다. 합성 M4A 디코딩은 확인했지만 MP3/FLAC/OGG/Opus/영상 컨테이너와 실제 교실 파일의 모델 품질을 모두 시험한 것은 아닙니다. 실제 교실의 거리·잔향·소음·여러 화자·전문용어를 재현한 시험도 아닙니다. 현재 Quick Tunnel의 새 파일 `PUT` CORS는 확인했지만, 두 계정 활성화·외부 파일 업로드, 학교 Wi-Fi와 실제 시스템 오디오 공유는 사용자 검증이 필요합니다.

현재 모델 선택 근거와 수치의 자세한 조건은 [HANDOFF.md](./HANDOFF.md)에 기록되어 있습니다. 공식 자료는 [Qwen3-ASR 모델 카드](https://huggingface.co/Qwen/Qwen3-ASR-1.7B), [Qwen 공식 저장소](https://github.com/QwenLM/Qwen3-ASR), [VibeVoice 모델 카드](https://huggingface.co/microsoft/VibeVoice-ASR-Streaming-7B), [Voxtral Realtime 모델 카드](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602), [OpenAI Whisper 저장소](https://github.com/openai/whisper)를 기준으로 했습니다.
