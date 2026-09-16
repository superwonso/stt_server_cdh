# Windows 네이티브 실행 안내

Windows 11에서 API와 Qwen 모델을 별도 프로세스로 실행합니다. 기본 전사는 **Transformers/SDPA + Qwen ForcedAligner**입니다. 이 문서는 설치·실행 절차와 검증 범위를 설명하며, 특정 PC의 현재 실행 상태나 공개 연결 게시 상태를 나타내지 않습니다.

## 환경 준비

Windows Python 3.12, NVIDIA 드라이버, 검증된 공식 uv Windows 실행 파일을 준비합니다. Git은 소스 관리에, Node.js는 JavaScript 회귀 테스트에 필요합니다. 기존 Linux/ROCm `.venv`를 Windows에서 재사용하지 않습니다.

일반 PowerShell에서 저장소로 이동합니다. 아래 경로는 실제 소스 위치로 바꿉니다.

```powershell
Set-Location 'C:\path\to\source'

# 설치 스크립트의 기본 도구 위치
# ..\tools\python312\python.exe
# ..\tools\uv\uv.exe
.\scripts\setup-windows.ps1

# 다른 곳에 설치된 Python 3.12를 지정하는 경우
.\scripts\setup-windows.ps1 -Python 'C:\path\to\python312\python.exe'
```

설치 스크립트는 `.venv-win`을 만들고 프로젝트 의존성, 공식 CUDA 12.6 PyTorch wheel, 고정 모델을 준비합니다. CUDA 사용 가능 여부와 실제 텐서 연산도 검사합니다. 기존 `.venv-win`이 호환되지 않으면 보존하고 중단합니다.

검증 기준 환경은 Python 3.12.14, PyTorch 2.11.0+cu126, torchvision 0.26.0+cu126, torchaudio 2.11.0+cu126, Transformers 4.57.6, qwen-asr 0.0.6, RTX 4090입니다. 기본 Transformers 경로는 별도 시스템 CUDA Toolkit 없이 동작했습니다. 다른 장치에서는 NVIDIA 드라이버와 해당 wheel의 호환성을 확인해야 합니다.

`-SkipModels -SkipTestInit`은 모델 다운로드와 시험 DB 초기화를 생략합니다. 의존성 설치와 CUDA 검사는 계속 수행합니다. 시험 프로필만 따로 초기화하려면 다음 명령을 사용합니다.

```powershell
.\.venv-win\Scripts\python.exe -m server.windows_local init
```

일반 설치·실행에 관리자 권한은 필요하지 않습니다. 드라이버 설치 등 시스템 변경이나 재부팅이 필요한 작업은 별도로 승인받아 진행합니다. PowerShell 실행 정책 때문에 스크립트가 차단되면 해당 프로세스에만 정책을 지정할 수 있습니다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\start-windows.ps1 -WaitReady
```

## 독립 로컬 시험: 켜기·상태·끄기

```powershell
.\scripts\start-windows.ps1 -WaitReady
.\scripts\status-windows.ps1
.\scripts\stop-windows.ps1
```

시작 후 `http://127.0.0.1:18766`을 엽니다. 모델은 `127.0.0.1:18765`에만 바인딩됩니다. 모델 포트를 브라우저나 외부에 연결하지 않습니다.

- 시험 DB: `.windows-local\data\classroom.sqlite3`
- 무작위 시험 계정 2개: `.windows-local\test-accounts.json`
- 계정·로그·DB·녹음 파일: 해당 Windows 사용자만 접근하도록 ACL 보호

시험 프로필은 운영 `.env`를 읽지 않습니다. Drive·CLOVA·LLM, 백업, 공개 주소 갱신과 터널을 사용하지 않으며 API의 외부 네트워크와 자식 명령 실행도 차단합니다. 프로필·DB가 기대한 시험 자료와 다르면 덮어쓰지 않고 거부합니다.

모델 중단 시 API 유지 여부를 재검증할 수 있습니다.

```powershell
.\scripts\stop-windows.ps1 -ModelOnly
.\scripts\start-windows.ps1 -ApiOnly
.\.venv-win\Scripts\python.exe scripts\verify_windows_local.py --expect-model-offline
.\scripts\start-windows.ps1 -ModelOnly -WaitReady
```

## 승인된 운영 설정으로 실행

운영 자료 사용이 명시적으로 승인되고 다음 준비가 끝난 뒤 운영 명령을 사용합니다.

- 기존 계정·비밀번호·관리자 설정을 유지한 단일 운영 DB 복원 및 무결성 확인
- `..\production\data\classroom.sqlite3`와 비공개 `..\production\config\service.env` 준비
- Windows 사용자 ACL, 절대 경로, 모델 설정, 허용 웹 Origin 검증
- 기존 Drive identity·OAuth client·DB binding 보존 및 인증·백업 정책 확인
- 노트북과 데스크톱이 동일 DB 복사본을 동시에 서비스하지 않도록 역할 확인

운영 실행기는 지정된 `service.env`와 기존 운영 DB를 검사합니다. 운영 `.env`나 Drive 인증을 복사했다는 이유만으로 바로 시작하지 않습니다. 실행 중 다른 기기에서 같은 SQLite 복사본을 운영하거나 Google Drive로 DB를 실시간 동기화하지 않습니다.

```powershell
# 운영 API와 모델
.\scripts\start-service-windows.ps1 -WaitReady
.\scripts\status-service-windows.ps1
.\scripts\stop-service-windows.ps1
```

운영 API는 `127.0.0.1:8765`, 모델은 `127.0.0.1:18775`를 사용합니다. 모델 요청·응답은 비공개 인증정보와 HMAC으로 검증하며 무인증·재전송·잘못된 Host·브라우저 Origin 요청을 거부합니다.

공개 연결이 승인된 경우에는 공식 출처에서 검증한 cloudflared를 `..\tools\cloudflared\cloudflared.exe`에 준비하고 터널을 별도로 관리합니다.

```powershell
# API 준비 후 터널 시작
.\scripts\start-tunnel-windows.ps1
.\scripts\status-tunnel-windows.ps1 -CheckHealth

# 종료할 때는 공개 연결을 먼저 종료
.\scripts\stop-tunnel-windows.ps1
.\scripts\stop-service-windows.ps1
```

터널이 실행됐다는 사실만으로 기존 웹사이트의 API 연결 설정이 게시·갱신됐다고 판단하지 않습니다. GitHub 인증, Pages 게시, 공개 주소 전환과 갱신은 별도 운영 절차에서 승인 범위와 완료 여부를 확인합니다. `start-tunnel.sh`를 Windows 시험용으로 실행하지 않습니다.

### 터널 시작이나 게시가 실패하는 경우

시작 명령이 실패하면 다음 게시 명령으로 넘어가지 않습니다. API와 모델이 `ready`여도 터널의 공개 상태 확인은 별도입니다. 터널 시작에 실패하면 현재 소유한 정상 터널 후보가 없으므로 게시 명령의 `candidate must match` 검사가 거부할 수 있습니다.

터널 오류에는 `startup_timeout; public_dns_lookup_failed`처럼 고정된 원인 코드가 표시됩니다. `public_tls_failed`는 HTTPS 인증서 확인 실패, `public_http_status_502`는 공개 연결의 502 응답, `startup_no_url`은 제한 시간 내 주소 미발견, `process_exited`는 터널 프로세스 종료를 뜻합니다. 비공개 로그와 자격 증명은 공개 이슈나 채팅에 붙이지 않습니다.

Windows에서 새 터널을 시작할 때 실제 DNS 조회 오류가 확인되면 공식 `ipconfig /flushdns`로 일시적인 시스템 DNS 캐시를 첫 DNS 오류 후 10·30·60초에 해당 시작 시도당 최대 세 번 비우고 다시 확인합니다. 이 작업은 모든 이름의 캐시를 비우지만 DNS 서버 설정이나 이미 연결된 세션은 변경하지 않습니다. 실행 중인 터널의 상태 확인·게시·자동 갱신에서는 캐시를 비우지 않습니다. 관리자 권한으로 자동 승격하지 않습니다. 캐시 초기화 성공만으로 준비 완료로 판단하지 않으며, 원래 HTTPS 인증서·정확한 API 상태 확인이 성공해야 정상 시작으로 취급합니다. 제한 시간 내 준비되지 않으면 이번에 시작한 터널만 종료하고 게시를 진행하지 않습니다.

## 고정 모델과 승인 음성 검증

| 모델 | Revision |
|---|---|
| Qwen3-ASR-1.7B | `7278e1e70fe206f11671096ffdd38061171dd6e5` |
| Qwen3-ForcedAligner-0.6B | `c7cbfc2048c462b0d63a45797104fc9db3ad62b7` |

음성 검증 도구의 기본 샘플 위치는 저장소의 `.samples`입니다. 샘플 파일은 저장소나 설치 스크립트가 자동으로 제공하지 않습니다. 도구는 기존에 승인된 공개·합성 샘플 4개의 해시를 검사하며 임의 녹음으로 대체하지 않습니다.

샘플 루트 안에는 다음 구조가 필요합니다. manifest의 `path`는 해당 하위 폴더에 있는 승인 샘플의 절대 경로여야 합니다.

```text
.samples/
  vllm-audit/samples/manifest.json
  synthetic-samples/manifest.json
```

다른 승인 샘플 보관 위치를 사용하려면 `--samples-root`를 지정합니다.

```powershell
# 형식과 고정 해시만 검사
.\.venv-win\Scripts\python.exe scripts\verify_windows_audio.py --samples-root 'C:\path\to\approved-samples'

# 로컬 시험 API·모델이 준비된 뒤 실제 Qwen 전사 검증
.\.venv-win\Scripts\python.exe scripts\verify_windows_audio.py --run --samples-root 'C:\path\to\approved-samples'
```

`--run`은 승인 샘플 4개를 파일 업로드와 청크 전사로 처리해 새 시험 수업 8개를 생성합니다. 계정·전사문은 출력하지 않고 집계 결과만 출력합니다. 운영 녹음이나 유료 API를 검증하는 명령이 아닙니다.

## 회귀 테스트와 검증 범위

```powershell
.\scripts\test-windows.ps1
.\.venv-win\Scripts\python.exe scripts\check_windows_access.py
```

검증 집계: **Python 1,178개 중 1,090개 통과·88개 제외, JavaScript 611개 통과**. 제외 항목에는 Linux 전용 경로와 파일 symlink 생성 권한이 필요한 시험이 포함됩니다. Windows ACL·junction·잠금 검사를 생략해 통과시킨 결과가 아닙니다.

- 승인 한국어·영어 음성 4개, 파일 업로드·청크 처리로 만든 시험 수업 8개 검증
- 로그인·사용자별 접근 권한·수업 저장·재시도·재개·응답 유실 후 중복 방지·마지막 음성 처리 검증
- WAV PCM 일치, Range 다운로드, 로그아웃 후 다운로드 권한 폐기 검증
- 모델만 종료해도 API 로그인·수업 조회 유지, API·모델 정상 재시작·종료 검증
- 합성 DB의 age v1.3.2 암호화·복구·변조 거부: 28개 통과, symlink 권한 필요 1개 제외
- 설치 의존성 `pip check` 통과

`test-windows.ps1`은 stdout과 stderr를 `..\work\regression`에 따로 저장하고 실패를 그대로 보고합니다. 설치된 `..\tools\age\age\age.exe`가 있으면 시험 전용 `STT_TEST_AGE_BINARY`를 설정하고 끝난 뒤 복구합니다. `-Targeted`는 일부 시험만, `-InProcessJavaScript`는 Node 자식 프로세스 없이 진단할 때 사용합니다.

실제 유료 CLOVA·LLM 호출, 운영 Drive 읽기·쓰기, 실제 외부 백업 위치는 별도 승인과 검증이 필요합니다. 짧은 모델·음성 검증을 장시간 수업 안정성 검증으로 해석하지 않습니다.

## LLM 결과 표시 정책

통신으로 받은 읽을 수 있는 결과는 형식·품질·출처 검증이 불완전해도 제공하고, 본문과 지원하는 다운로드의 맨 아래에 다음 한 문장을 표시하도록 변경했습니다.

> 일부 내용을 확인하지 못했습니다. 원문과 함께 확인해 주세요.

출처를 확인하지 못한 결과에는 시간·출처 링크를 만들지 않습니다. 숨은 reasoning, 오류 응답 본문, 도구 인수는 결과로 제공하지 않으며 HTML·링크는 안전한 텍스트로 취급합니다. 소유권·원문 revision·취소·저장 무결성 검사는 유지합니다. 이 정책 변경은 배포 대상으로 승인됐으며, 이 문서 자체가 배포 완료를 의미하지는 않습니다.

## 파일 보호·백업·절전

Windows에서는 사용자 ACL과 `LockFileEx`를 사용합니다. 비공개 파일은 생성 시점부터 보호하며 안전하지 않은 ACL, junction·재분석 지점·하드링크는 거부합니다. WAV 다운로드는 검사한 파일 핸들을 끝까지 유지하고, 취소 시 읽기가 끝나기 전에 핸들을 재사용하지 않습니다. 열린 파일 교체가 거부되면 기존 파일을 보존하고 실패를 알립니다.

Drive 인증·보관·백업에도 Windows ACL·잠금을 적용했습니다. 운영 백업은 명시적으로 구성·검증해야 하며 합성 암호화 왕복 통과가 실제 운영 백업 경로 연결을 보증하지는 않습니다.

- 절전 중에는 API·전사·공개 연결이 중단됩니다. 복귀 후 해당 환경의 상태 명령으로 확인합니다.
- 재부팅·로그아웃하면 프로세스가 종료됩니다. 이 실행 절차는 자동 시작 서비스나 예약 작업을 설치하지 않습니다. 로그인 후 다시 시작합니다.
- 종료는 이 프로젝트의 실행 기록·실행 파일·생성 시각을 확인한 프로세스 핸들에 한정합니다. 다른 Python 프로세스를 일괄 종료하지 않습니다.
- 남은 PID가 없으면 검증 후 기록을 정리합니다. PID가 다른 실제 프로세스에 재사용됐다면 프로세스와 기록을 보존하고 거부합니다.
- 장시간 연속 수업, 강제 충돌·전원 차단 복구, 절전 복귀는 별도 안정성 검증이 필요합니다.

## Linux/ROCm과 vLLM

기존 Linux/ROCm의 `setup.sh`, Unix 소켓, Bash 실행 경로를 유지합니다. Windows는 별도 CUDA 가상환경과 PowerShell 실행 경로를 사용하며 WSL2·Linux Docker·가상머신을 설치하지 않습니다. 기존 노트북은 보조 기기로 계속 사용할 수 있으며 Linux/ROCm 실기기 회귀는 해당 기기에서 확인합니다.

Windows vLLM 비교에는 공식 upstream 배포와 구분되는 **SystemPanic 커뮤니티 포크**를 별도 환경에서 사용했습니다. 같은 모델 revision과 공개 음성 6개를 각 3회 처리한 18/18회가 성공했으며 Transformers ASR 단독 출력과 모두 같았습니다.

| ASR 단독 비교 | Transformers / SDPA | Windows native vLLM |
|---|---:|---:|
| 예열 후 RTF | 0.148442 | 0.084312 |
| 최대 Torch reserved | 4.416 GiB | 11.848 GiB |

RTF는 처리 시간/음성 시간입니다. 이 표본에서 vLLM은 1.761배 빨랐지만 `gpu_memory_utilization=0.5`로 KV cache를 미리 할당했으므로 VRAM 수치를 최소 필요량 비교로 해석하지 않습니다. ForcedAligner가 포함된 기본 서버와 ASR 단독 수치를 직접 비교하지 않습니다.

고유 realtime은 14구간·276개 델타·마지막 25ms 입력 보존을 확인했습니다. 구간 사이 문맥은 사용하지 않았고 영어 CER은 0%에서 2.373%로 증가했습니다. 실제 WebSocket 연결·재연결 수명주기와 vLLM aligner는 미검증입니다. **기본은 Transformers/SDPA + ForcedAligner이며 vLLM은 별도 실험 대상으로 유지합니다.**
