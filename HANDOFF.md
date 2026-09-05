# 개발 인수인계

기준일: 2026-09-05 (Asia/Seoul)

실시간 입력의 영속 대기열·무기한 재전송·입력 재연결·탭 간 잠금과 CLOVA 선택 변경은 2026-09-04 전체 자동 회귀를 통과했다. 2026-09-05에는 CLOVA 실계정 Basic 스트림으로 짧은 비개인 한국어 음성과 강제 회전 경계를 확인하고 최신 보완을 `main`에 배포했다. 배포 커밋과 API/Pages/Quick Tunnel 외부 확인 결과는 아래 `저장소·배포 체크포인트`에 이어서 기록한다. 실제 브라우저·학교 Wi-Fi·교실 음성 및 장시간 CLOVA 검증은 `아직 확인하지 못함`과 구분한다.

## 현재 결론

현재 로컬 엔진은 `Qwen/Qwen3-ASR-1.7B`의 Transformers 경로(`qwen-asr==0.0.6`, BF16, SDPA)와 `Qwen3-ForcedAligner-0.6B`다. 브라우저 마이크와 `getDisplayMedia`의 오디오 트랙은 16 kHz 모노 PCM을 모아 새 음성이 8초 이상이고 최근 0.24초가 조용하면 자르며, 조용한 지점이 없으면 전체 WAV 15초에서 자른다. 첫 조각 뒤에는 이전 3초를 겹쳐 보낸다. 업로드 파일도 PyAV로 스트리밍 디코딩한 뒤 같은 분할 규칙을 쓴다. 서버는 0.6초 stability guard와 단어 정렬 시각으로 각 조각에서 확정할 범위를 정한다. Qwen의 vLLM 네이티브 스트리밍은 현재 코드에서 사용하지 않는다.

마이크 실시간 수업에는 `qwen`(로컬)과 NAVER Cloud `clova` 선택을 추가했다. 인증된 `/status`에서 CLOVA 설정을 확인하면 새 마이크 수업은 CLOVA를 우선 선택하고, 미설정·상태 확인 실패 때는 Qwen을 선택한다. 사용자가 이번 계정 세션에서 Qwen 또는 CLOVA를 직접 고르면 새 수업·기록 화면 왕복과 상태 polling에서도 그 선호를 유지하되, CLOVA를 일시 사용할 수 없을 때의 새 수업만 Qwen으로 표시하고 회복 시 선호를 복원한다. 진행 중 수업은 절대 자동 전환하지 않는다. provider는 lecture에 불변 저장하고 청크 요청에서 바꿀 수 없다. 공유 화면/탭 소리와 파일 import는 계속 Qwen으로 강제한다. CLOVA는 공식 bidirectional gRPC 한 스트림을 수업별로 유지하며 healthy 연결에서는 브라우저 overlap을 잘라 새 PCM만 전송하되, 수업별 확정 시각 frontier를 유지해 다음 ACK에 뒤늦게 붙은 경계 텍스트도 회수한다. 5분 연결 제한 전 회전·idle/단절 후 새 스트림에서는 현재 청크의 최대 3초 overlap을 문맥으로 다시 넣고 timestamp와 정규화된 직전 텍스트를 함께 대조한다. 이미 확정된 replay라는 근거가 강한 부분만 제거하고 frontier 주변 ±0.15초의 모호한 새 반복 발화는 보존한다. 화면 갱신은 기존 8~15초 브라우저 청크 주기를 그대로 사용하므로 단어 단위 초저지연 UI로 바뀐 것은 아니다.

CLOVA 선택 시 경로는 `브라우저 → Cloudflare → 이 PC → clovaspeech-gw.ncloud.com:50051`이며 음성이 사이트 운영자가 관리하는 NAVER Cloud 계정의 CLOVA Speech 도메인으로 간다. 공식 문서상 스트리밍 결과는 운영자가 연결한 Object Storage에 자동 저장된다. 로컬 수업 삭제는 그 클라우드 사본을 삭제하지 않는다. Basic Stream은 15초당 5원(VAT 별도)이고 Free 도메인은 실시간 gRPC를 지원하지 않는다. 따라서 CLOVA를 품질 우위로 단정하거나 자동 fallback으로 쓰지 않고 같은 실제 한국어 수업 음성으로 Qwen과 비교해야 한다.

정확도 보조 기능으로 완료된 원문을 충북대 NOVA Gateway의 `solar-pro4`에 보내는 명시적 opt-in 후보정을 추가했다. Qwen 모델은 바꾸지 않았고 음성은 후보정 제공자에게 보내지 않는다. 원문과 후보정본은 분리 저장하며 화면은 원문을 기본으로 연다. 문장 ID·순서·개수, 기존 숫자, 개인정보 임시 표식과 응답 크기가 맞지 않으면 후보정본을 저장하지 않고, 새 숫자 표기가 생기면 원문 비교 경고를 붙인다. 따라서 이 기능은 원문을 대체하는 정답 생성기가 아니라 검토 가능한 별도 초안이다.

실시간 마이크와 공유 소리는 사용자 일시정지를 지원한다. pause acknowledgement 경계까지 새 PCM을 비최종 청크로 flush하고, 정지 중 render frame은 버린다. 수업 ID·누적 음성 시간축·직전 3초 overlap은 유지하며 재개하고, 실제 종료에서만 overlap-only guard를 포함한 `final` 청크로 수업을 확정한다. 진행 수업의 후보정 버튼은 녹음을 멈추지 않고 의도만 예약하며, 사용자가 종료한 뒤 final 저장이 성공해야 후보정 요청을 시작한다.

CLOVA를 마이크 화면의 기본 선택으로 둔 것은 실제 같은 수업 음성에서 Qwen보다 항상 정확하다고 검증한 결론이 아니라 사용자의 운영 선택이다. Qwen은 대상 Radeon/ROCm에서 실제 한국어 음성을 실시간보다 빠르게 처리했고, 현재 3초 겹침 경로가 짧은 낭독 시험에서 CER 4.03%, 마지막 기준 25자 완전 일치, 탐지된 텍스트 중복 0건을 냈으므로 CLOVA 미설정·상태 확인 실패의 로컬 대안과 화면 소리·파일 변환 엔진으로 유지한다. 서로 독립적으로 인식한 Qwen 청크의 정렬 시각은 흔들릴 수 있으므로 실제 수업에서 exactly-once를 수학적으로 보장하지 않는다.

보존 WAV를 운영자의 개인 Gmail My Drive로 옮기는 선택형 archive를 추가했다. 완료된 WAV를 로컬 staging에 두고 재개 가능 업로드한 뒤 크기·MD5·SHA-256을 검증해야만 로컬 사본을 삭제한다. 녹음은 `STT 수업 녹음/<runtime account ID>/<lecture UUID>.wav`로 분리한다. 실제 계정 ID 목록은 GitHub/Pages에 게시하지 않고 Drive appProperties·archive 메타데이터/다운로드 URL·집계 CLI에도 넣지 않으며, 비공개 Drive 폴더명과 인증된 본인/관리자 화면·개인 초대 링크·해당 기기의 로컬 대기열에서만 운영상 사용한다. 인증·업로드·검증·폴더 이동 오류에서는 로컬 WAV를 보존하고 상태를 재시도 또는 수동 확인으로 남긴다. 이 기능은 녹음만 옮기며 DB·계정·텍스트는 이 PC에 남는다.

실제 개인 Gmail OAuth, 첫 1건 keep-local 업로드와 사용자 수동 재생, 사용자 폴더 3개로의 전체 10건 이전·전수 재검증·로컬 정리를 완료했다. Drive 구현 `aaffb4e`를 main에 푸시했고 Pages 실행 `33950879705`가 성공했다. API도 Qwen warmup으로 재시작해 로컬/외부 health 200과 공개 자산 9개의 byte-for-byte 일치를 확인했다. 이후 종료 직후 TIME_WAIT를 잘못 점유로 판단하던 시작 스크립트도 보완했다. 최신 검증 결과와 남은 범위는 아래에서 구분한다.

VibeVoice-ASR-Streaming-7B는 이 PC에서도 BF16/SDPA로 실제 로드되고 공식 live-state API로 7분 52초를 완주했다. 2.9초 청크와 0.5초 lookahead, 이전 음성·텍스트 문맥, 화자 라벨을 한 세션에서 유지하는 점은 회의·수업에 매력적이다. 그러나 공개 체크포인트의 목표 세션이 최대 8분이고, 실측에서도 KV가 1,702→6,574토큰, GPU reserved가 16.59→22.19 GiB로 증가했다. 최근 10청크 평균 연산도 2분 2.15초에서 종료 시 3.25초로 늘어 2.933초 입력 간격을 넘었다. 따라서 45~90분 수업 기본값으로 두지 않았다. 8분마다 상태를 끊으면 실행은 가능할 수 있지만 화자 일관성과 장기 문맥이라는 장점도 함께 끊긴다.

## 대상 하드웨어

직접 확인한 환경:

| 항목 | 확인값 | 해석 |
| --- | --- | --- |
| 호스트 OS | Windows 11 Home, build 26200 | WSL2 호스트 |
| WSL | Ubuntu 24.04.4 LTS, kernel `6.18.33.2-microsoft-standard-WSL2` | 서버 실행 환경 |
| CPU | AMD Ryzen AI MAX+ 395, WSL에 16 CPU | 단일 추론 worker에 충분 |
| 시스템 RAM | 물리 약 64 GB, WSL 할당 47 GiB, swap 35 GiB | GPU와 공유되는 메모리 여유를 함께 봐야 함 |
| GPU | Radeon 8060S, gfx1151, 40 CU | NVIDIA CUDA 전용 경로를 그대로 쓸 수 없음 |
| GPU 메모리 | ROCm/PyTorch가 약 47.48 GiB를 주소 가능 | 별도 전용 VRAM이 아니라 통합/공유 메모리이므로 모델 이름만 보고 여유 있다고 단정하면 안 됨 |
| ROCm | 7.2.4 | `/dev/dxg`를 통한 WSL GPU |
| PyTorch | 2.10.0 ROCm 7.2.4 빌드 | `$HOME/miniconda3/envs/torch-rocm/bin/python` |

샌드박스 내부에서는 GPU 장치가 가려질 수 있었기 때문에 실제 GPU 확인과 벤치마크는 WSL 호스트 권한이 보이는 셸에서 수행했다.

## 모델 비교

서로 다른 데이터셋·오디오 조건·오류 지표를 모델 간 절대 순위처럼 비교하면 안 된다. 아래 공개 수치는 각 모델의 적합성과 위험을 거르는 자료이고, 최종 선택은 같은 한국어 수업 샘플을 같은 PC에서 돌린 결과로 다시 판단해야 한다.

| 모델 | 공식 자료에서 확인한 내용 | 이 PC와 긴 수업에 대한 판단 | 현재 상태 |
| --- | --- | --- | --- |
| `microsoft/VibeVoice-ASR-Streaming-7B` | 이름은 7B지만 [모델 카드](https://huggingface.co/microsoft/VibeVoice-ASR-Streaming-7B)는 9B BF16로 표시한다. [기술 보고서](https://arxiv.org/html/2609.02812v1)는 2.9초 청크 + 0.5초 lookahead, 예상 화자 귀속 지연 2.00초, 한국어 MLC CER 9.09/cpCER 23.22를 보고한다. 이전 음성·텍스트를 유지하지만 비용이 선형 증가하고 공개 체크포인트는 최대 480초 녹음을 대상으로 한다. [공식 실행 문서](https://github.com/microsoft/VibeVoice/blob/main/docs/vibevoice-asr-streaming.md)는 NVIDIA PyTorch 컨테이너를 검증 환경으로 제시한다. | 이 PC의 공식 `init_streaming_state`→`streaming_generate_step` 경로에서 472.08초를 완주했으나 최근 평균 청크 연산이 2.15→3.25초로 증가해 종료 시 입력 간격을 초과했다. allocated는 16.32→16.58 GiB, reserved는 16.59→22.19 GiB, KV는 1,702→6,574토큰이었다. | **ROCm 실측 완료, 기본값 아님.** 짧은 FLEURS 3개 간이 aggregate CER 25.2%; 별도의 14회 반복 7:52 실험은 custom content CER 15.75%. 두 값은 음성 구성과 정규화가 달라 서로 직접 비교할 수 없다. |
| `mistralai/Voxtral-Mini-4B-Realtime-2602` | [공식 모델 카드](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602)는 4B BF16, 16GB 이상 GPU, sliding-window attention, 기본 131072 context(3시간 이상)를 설명한다. Korean FLEURS 공식값은 WER로 480ms 지연 15.74, 2400ms 지연 14.30이다. [기술 보고서](https://arxiv.org/html/2602.11298v3)도 긴 연속 스트림을 주목적으로 설명한다. | 공식 2400ms stream을 호환성 shim이 있는 격리 환경에서 실행했다. 111.12초 음성 추론이 176.02초(RTF 1.584)여서 현재 경로는 입력을 따라가지 못한다. 종료 시 3.28초 right padding이 필수였다. | **ROCm 실측 완료, 기본값 아님.** CER 4.70%, WER 21.53%; GPU allocated 약 8.94GB, peak 약 9.18GB. |
| `Qwen/Qwen3-ASR-1.7B` | [공식 모델 카드](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)와 [저장소](https://github.com/QwenLM/Qwen3-ASR)는 한국어를 포함한 30개 언어, 오프라인·스트리밍 모드를 명시한다. 패키지의 스트리밍은 현재 vLLM backend에서만 제공되고, Transformers 경로는 확정 결과와 forced alignment를 제공한다. | 모델이 작고 현재 ROCm PyTorch/SDPA에서 실제 작동했다. HTTP 청크는 네이티브 스트리밍보다 느리게 확정되지만, 연결 재시도와 DB idempotency가 단순하다. 3초 겹침이 경계 손실을 완화하나 독립 ASR/정렬의 jitter는 남는다. | **현재 로컬 엔진·fallback.** 전체 파일 CER 2.24%/RTF 0.592, 실제 짧은 청크 경로 CER 4.03%/RTF 0.466. 60분 35초 연속 청크 경로도 CER 3.51%/RTF 0.442로 완주했다. |
| Whisper `turbo` | [OpenAI 모델 카드](https://github.com/openai/whisper/blob/main/model-card.md)는 798M multilingual 모델이며 large-v3의 디코더를 줄여 추론 속도를 높였다고 설명한다. Whisper 자체는 실시간 state를 유지하는 네이티브 스트리밍 모델이 아니다. 기존 코드는 faster-whisper/CTranslate2였다. | [CTranslate2 GPU 지원](https://opennmt.net/CTranslate2/hardware_support.html)은 NVIDIA CUDA 중심이라 기존 faster-whisper GPU 설정을 AMD ROCm에서 그대로 쓸 수 없다. 대신 OpenAI PyTorch 구현은 이 PC ROCm에서 빠르게 실행됐다. 파일 전체 품질은 좋았지만 분할 경로에서 한 발화가 통째로 빠졌다. | **ROCm 기준선 실측 완료, 기본값 아님.** 전체 파일 CER 4.03%/RTF 0.193; 동일 3초 겹침+단어 시각 분할 CER 23.71%/RTF 0.259. |

### 왜 현재는 네이티브 스트리밍이 아닌가

- 사용 요구는 최소 지연보다 정확도와 장시간 안정성이 우선이다.
- 8초 이후 pause-aware HTTP 요청은 Cloudflare 연결이 끊겨도 같은 UUID와 WAV로 재시도할 수 있다.
- 서버의 `(lecture_id, chunk_id)` 기본키와 payload hash가 같은 요청의 이중 저장을 막는다.
- 각 응답은 확정 결과뿐이라 interim 텍스트를 append해 생기는 중복 문제가 없다.
- 3초 겹침과 정렬은 경계 문제를 줄이지만 이전 음성·텍스트 state를 모델 내부에 유지하지 않는다. 실제 교실 음성에서 누락·의미적 중복이 전혀 없다는 보장은 아직 없다.

향후 Qwen 네이티브 스트리밍을 붙일 경우 `state.text`는 누적·수정 가능한 전체 가설로 취급하고, 화면의 interim 영역을 교체해야 한다. 확정된 prefix만 DB에 append하고 종료 시 `finish` flush를 반드시 호출해야 한다. 현재 API처럼 모든 응답을 새 확정 문장으로 append하면 중복 기록이 생긴다.

## NOVA AI 후보정

사용자가 제공한 충북대 계정 키로 [NOVA API Gateway](https://docs.mindlogic.ai/docs/cbnu-ac/api-gateway/getting-started/overview)를 검토하고 실제 인증을 확인했다. OpenAI 호환 base URL은 `https://factchat-cloud.mindlogic.ai/v1/gateway`, 사용 endpoint는 `POST /chat/completions/`다. `GET /models/?type=llm`은 2026-09-04에 200과 34개 모델을 반환했고 `solar-pro4`가 있었으며, `GET /credits/`도 200이었다. 키와 credits 응답 본문은 로그·문서에 남기지 않았다.

현재 기본 후보정 모델은 `solar-pro4`다. 한국어를 공식 지원하고 NOVA 표의 1K input/output당 0.3/1.2 credits로 당시 후보군 중 비용이 낮으며, 수업 원문을 보수적으로 교정하는 용도에 먼저 시험하기 적절하다고 판단했다. 이는 공개 모델명이나 context 크기만으로 품질을 보장한 선택이 아니다. [NOVA 모델 목록](https://docs.mindlogic.ai/docs/cbnu-ac/api-gateway/getting-started/models), [모델별 크레딧](https://docs.mindlogic.ai/docs/cbnu-ac/factchat/product/model-credits), [Solar Pro 4](https://www.upstage.ai/blog/en/solar-pro-4)

구현 계약:

- `server/.env`의 `MINDLOGIC_API_KEY`만 서버에서 읽는다. `Settings` repr과 `/status`에는 키가 없고, base URL은 공식 HTTPS host/path/443로 고정한다. redirect와 환경 proxy 신뢰도 끈다.
- `POST /lectures/{id}/correction`은 로그인·소유권·`recording_finalized`를 두 번 확인한 뒤 현재 원문의 SHA-256 revision과 모델을 기준으로 idempotent job을 만든다. `GET`도 같은 소유자만 상태/결과를 읽는다.
- 같은 수업의 원문은 overlap 경계와 final tail 때문에 종료 전까지 segment 집합과 revision이 달라질 수 있다. 중간 가설을 즉시 보내 stale 후보정이나 마지막 문장 누락을 만들지 않도록 final 원문만 허용한다. 웹의 진행 수업 예약도 이 서버 gate를 우회하지 않는다.
- 작업 상태는 `queued/processing/completed/failed`이며 별도 `transcript_corrections` 행에 저장한다. 단일 background worker와 attempt/revision/status CAS가 늦게 끝난 결과를 버리고, 재시작 때 `processing`을 안전하게 다시 `queued`로 돌린다.
- 후보정 worker는 로컬 `inference_lock`, 오디오 capacity, recording/import filesystem lock을 잡지 않고 외부 호출 중 SQLite transaction도 유지하지 않는다. 따라서 완료된 과거 수업 후보정은 다른 수업의 실시간 전사·일시정지와 병행하며, 짧은 SQLite write 구간만 직렬화된다.
- 기본 6,000자 target 묶음에 앞뒤 2개 segment를 `target=false` 문맥으로 보낸다. 응답은 strict JSON Schema로 같은 target ID를 한 번씩 같은 순서로 돌려받아 문맥 겹침이 출력 중복으로 들어오지 않게 한다.
- payload는 전사 텍스트, `language`, 무작위 내부 segment UUID뿐이다. 오디오, 수업 제목, 로그인 ID는 보내지 않는다. 이메일·전화·주민/카드번호·긴 숫자는 형식 기반으로 가리고, 모든 아라비아 숫자도 보호 표식으로 바꾼다. 응답에서 placeholder의 순서·개수를 검사해 한 번만 복원하지만 의미적 위치까지 증명하지는 못한다. 이름·주소·드문 형식은 가려지지 않을 수 있으므로 익명화라고 표현하지 않는다.
- 기존 숫자가 있는 문장은 결과 숫자열 전체가 정확히 같아야 하므로 삭제·치환·추가를 fail-closed한다. 숫자가 없던 문장에 새로운 숫자 표기만 생긴 경우에는 결과와 함께 로컬 경고를 넣는다. 모델에게 한글 수사를 숫자로 바꾸지 말라고 지시해도 실제 Solar가 `제 이법칙`을 `제2법칙`으로 고쳤기 때문에 이 별도 정책이 필요했다.
- provider 오류 본문과 exception 문자열은 로그나 DB에 반사하지 않는다. 401/403, 402, 429, 5xx/timeout은 정제된 코드로 바꾸고 transient 오류만 제한 재시도한다. 요청 1 MiB, 응답 2 MiB, 원문 250,000자, segment/개수/누적 출력에 별도 상한이 있다.
- 새 외부 작업은 계정당 10건/시간, 전체 16건/시간으로 제한한다. 긴 수업 한 건은 여러 provider call을 사용하며 외부 API가 idempotency key를 지원한다는 확인이 없어서, 응답 중 강제 종료 뒤 같은 묶음이 다시 전송·과금될 가능성은 남는다.
- 후보정 처리 중 수업 삭제는 `409`다. 대기 중 작업 삭제와 lecture의 durable deleting 전환은 한 SQLite `BEGIN IMMEDIATE` 안에서 처리해 worker claim과 경쟁하지 않는다.

실제 비개인 한국어 3문장으로 NOVA strict JSON Schema 응답을 확인했다. Solar는 문장 ID·기존 숫자 `15`·가린 테스트 이메일을 보존하고 띄어쓰기를 고쳤으며, `제 이법칙`을 `제2법칙`으로 바꿨다. 최종 정책은 이를 저장하되 `AI가 원문에 없던 숫자 표기를 추가했습니다. 원문과 비교하세요.`를 함께 반환했다. 이는 연결·형식·보호 로직 검증이지 실제 수업 품질 벤치마크가 아니다.

## Qwen 실측

`scripts/benchmark_qwen.py --limit 10`으로 공개 FLEURS `ko-KR`의 실제 한국어 읽기 음성 10개를 순차 실행했다. 각 파일 전체를 독립적으로 인식한 결과다.

| 항목 | 결과 |
| --- | ---: |
| 오디오 합계 | 111.12초 |
| 총 RTF | 0.592 |
| 정규화 문자 오류율(CER) | 2.24% |
| peak GPU allocated | 3.987 GiB |
| 프로세스 최대 RSS | 5.719 GiB |
| 첫 샘플 이후 allocated 증가 | 0.00 MiB |

이 수치는 짧고 또렷한 낭독 음성에서 실시간보다 빠르게 처리했고, 10회 동안 PyTorch allocated memory가 누적되지 않았다는 증거다.

장시간 경로는 같은 10개 샘플을 순서를 바꾸며 이어 붙인 3,635.59초(60분 35.59초) 타임라인으로 별도 검증했다. 브라우저와 같은 pause-aware 분할 및 3초 겹침 때문에 모델이 실제 받은 입력은 4,880.59초였고, 416청크를 1,608.08초에 처리했다. 결과는 CER 3.51%, 원본 타임라인 기준 RTF 0.442, 마지막 기준 문자열 25자 완전 일치였다. PCM coverage error와 청크 분할 gap/overlap은 0 sample, 경계 텍스트 반복 후보와 동일 인접 결과는 0건이었다. 모델 로드 뒤 GPU allocated는 처음과 끝 모두 5.592 GiB(peak 5.679 GiB), reserved는 처음과 끝 차이 6 MiB(peak 5.811 GiB), 프로세스 RSS는 처음과 끝 차이 12.65 MiB였다. 예외·OOM은 없었다.

장시간 시험까지 포함해도 다음을 의미하지는 않는다.

- 교실 뒤쪽 마이크, 울림, 잡음, 작은 목소리, 여러 화자에서도 CER 2.24%라는 보장
- 90분 실제 수업이나 다른 음성 분포에서도 RSS·공유 GPU 메모리가 증가하지 않는다는 보장
- 현재 pause-aware/3초 겹침 경로가 실제 수업의 모든 경계에서 누락·중복이 없다는 보장
- Cloudflare와 학교 Wi-Fi를 거친 전체 지연이 RTF 0.592라는 의미

실제 브라우저와 같은 pause-aware 8~15초, 3초 overlap, 0.6초 guard 경로로 같은 10개를 2.25초 침묵과 이어 붙여 실행한 결과는 다음과 같다.

| 항목 | 결과 |
| --- | ---: |
| 원본 타임라인 | 113.37초 |
| 겹침을 포함해 모델에 입력한 합계 | 149.37초 |
| 청크 수 | 13 |
| 추론 합계 | 52.861초 |
| 원본 타임라인 기준 RTF | 0.466 |
| 정규화 CER | 4.03% |
| hypothesis 문자 수 차이 | -2자 |
| 마지막 기준 25자 | 완전 일치(CER 0) |
| 인접 청크/반복 8-gram 중복 탐지 | 0건 |
| peak GPU allocated | 5.679 GiB |
| 첫·마지막 청크 allocated 증가 | 0.00 MiB |

이 시험에서는 capture coverage와 서버의 시간 partition에 표본 단위 gap/overlap이 없었고 텍스트 중복도 검출되지 않았다. 다만 Qwen과 forced aligner는 각 겹친 WAV를 독립적으로 처리하므로, 경계 단어의 정렬 중앙값이 guard 양쪽으로 흔들리면 실제 수업에서는 누락이나 중복이 생길 수 있다. 이 결과를 exactly-once 보장으로 표현하지 않는다.

FLEURS 원음과 결과는 `.samples/` 아래에 두어 Git에서 제외한다. 데이터셋 라이선스·출처를 보존하고 음성 자체를 저장소에 추가하지 않는다.

## 다른 후보의 이 PC 실측

### Voxtral Mini 4B Realtime

격리한 Transformers 환경에서 공식 2400ms 지연 스트림을 FLEURS 10개에 실행했다. 111.12초 음성의 추론 합계가 176.02초(RTF 1.584), CER 4.70%, WER 21.53%였다. 첫 샘플을 제외해도 RTF 1.568이며, 종료에 필요한 41개 right-pad token(3.28초 음성 상당)을 음성 길이에 포함해도 RTF 1.223이다. 이 경로는 현재 PC에서 실시간 입력을 따라가지 못한다.

- 10/10에서 append한 streaming delta가 최종 decode와 같았고 `stream_end`를 받았다.
- 반복 8-gram 초과는 0건이었다.
- 마지막 4.8초 샘플은 flush를 하면 정규화 25자를 냈지만, flush가 없으면 11자에서 끝났다. 종료 padding과 완료 대기는 필수다.
- GPU allocated는 약 8.94GB, reserved 9.48GB, peak allocated 9.18GB였고, 짧은 독립 스트림 10개 사이 allocated 누적은 보이지 않았다. 이는 긴 단일 수업 state 검증이 아니다.
- vLLM ROCm 경로는 현재 운영 환경을 바꿀 위험 때문에 설치·실측하지 않았다. [vLLM 지원 모델](https://docs.vllm.ai/en/stable/models/supported_models/), [vLLM ROCm 설치 문서](https://docs.vllm.ai/en/stable/getting_started/installation/gpu/), [realtime 출력 의미](https://vllm.ai/blog/2026-01-31-streaming-realtime)를 후속 검토 기준으로 삼는다.

### Whisper turbo

기존 faster-whisper CUDA 경로가 아니라 [OpenAI Whisper](https://github.com/openai/whisper) PyTorch FP16/beam 5 경로로 ROCm에서 측정했다.

| 경로 | 결과 |
| --- | --- |
| 파일 전체 10개 | 추론 21.501초, RTF 0.193, CER 4.03%, peak allocated 3.328 GiB |
| 기존 고정 8초 비겹침 | RTF 0.239, CER 14.77%, 텍스트 중복 0, 마지막 1.37초 보존, 한 발화 전체 누락 |
| pause-aware 3초 겹침 + word timestamps | 추론 29.399초, RTF 0.259, CER 23.71%, 마지막 25자 완전 일치, 중복 검출 0, 한 발화 전체 누락 |

Whisper는 빠르고 전체 파일 기준선도 양호했지만, 현재 수업처럼 짧은 독립 WAV를 시간으로 나눠 확정할 때의 누락이 Qwen보다 컸다. 따라서 현재 로컬 엔진으로 되돌리지 않았다.

## 구현 현황

### 서버

- FastAPI는 `127.0.0.1`에만 bind하며 외부 요청은 Cloudflare 터널로만 받는다.
- CORS 및 별도 origin middleware가 `SITE_ORIGINS`의 정확한 출처만 허용한다.
- 실제 계정 ID 허용 목록의 설정 원본은 ignored `server/.env`의 `ACCOUNT_USERNAMES`이고 GitHub/Pages에는 게시하지 않는다. 런타임에는 로컬 DB, 비공개 Drive 폴더명, 로그인한 본인/관리자 화면·초대 절차·해당 기기의 로컬 대기열에서 소유자 묶음으로 사용한다. 2~10개의 서로 다른 정규화된 ID만 허용하며, 빈 설정이나 기존 DB와의 불일치는 값을 반사하지 않는 오류로 fail closed 한다. 일반 서버 시작은 계정을 자동 추가하지 않고 명시적인 로컬 `add-account` 경로만 기존 집합을 한 개 늘릴 수 있다.
- 관리자 ID도 ignored `server/.env`의 `ADMIN_USERNAME`에만 두고 `Settings` repr에서 계정 목록과 함께 제외한다. 설정이 없거나 구성된 계정 중 하나와 일치하지 않으면 관리자 API는 fail closed 한다.
- `POST /presence`는 인증 계정별 `idle/viewing/recording/uploading/transcribing/correcting/away` 하나만 프로세스 메모리에 45초 TTL로 보관한다. IP·UA·수업 ID/제목/본문을 받거나 DB에 저장하지 않는다.
- `GET /admin/overview`는 관리자에게만 uptime/model, `/proc` RAM/RSS/load, 디스크, 이미 로드된 PyTorch의 ROCm VRAM, 작업 수, 계정 활성화/세션/최소 presence와 최근 안전한 조작 이력을 반환한다. 계정 조작 참조는 프로세스마다 새 무작위 token이며 실제 ID는 관리자 응답의 label에만 있다.
- persisted `operational_state`를 끄면 인증과 health/presence/admin/status는 유지하되 새 lecture/import/녹음 다운로드 요청을 `503`으로 차단한다. 요청 안에서 이미 시작한 추론과 background import/correction은 중간에 죽이지 않는다.
- 관리자는 자기 세션을 실수로 끊을 수 없고 다른 계정의 모든 세션만 해제할 수 있다. 터널 재연결은 202를 먼저 반환한 뒤 고정 script argv로 비동기 실행하며 임의 hook 메시지·PID·URL·로그를 API에 반사하지 않는다.
- body-size middleware가 `Content-Length` 유무와 chunked 전송 모두에서 과대한 JSON/WAV/파일 조각 요청을 parsing 전에 차단한다.
- 계정 ID는 Git에서 제외된 `server/.env`의 `ACCOUNT_USERNAMES`로만 설정하며 공개 프런트엔드에 허용 목록을 내장하지 않는다. 서버가 로그인·소유권을 검사한다.
- 초대 코드는 7일/1회용이고 원문은 `.data/invitations.txt`에만 쓴다. 초대 URL에는 API 주소를 넣지 않으며, 활성화 후 DB의 setup hash도 제거된다.
- 새 비밀번호 정책은 사용자 요청에 따라 4~128자이며 Argon2로 저장한다. 세션·초대 코드는 SHA-256 digest로 저장하고 인증 오류 응답에 비밀번호나 초대 값을 반사하지 않는다.
- 로그인/초기 설정 시 IP별 30회/5분, IP+계정별 10회/5분과 별도로 모든 주소를 합산한 계정별 50회/30분 제한을 적용한다.
- 모든 lecture·실시간 chunk·파일 import 조회/변경은 인증 사용자의 소유권을 검사한다.
- 수업 생성은 브라우저가 보낸 UUID와 소유자·제목·언어를 확인해 응답 손실 뒤 재요청도 idempotent하게 처리한다.
- 한 모델과 inference lock을 사용하며 running+waiting 요청은 기본 2개로 제한한다.
- `lectures.asr_provider`는 schema v6에서 `qwen|clova` allowlist로 저장한다. 기존 수업과 import-created 수업은 `qwen`으로 migration/생성되며 같은 lecture UUID를 다른 provider로 재생성하면 `409`다. `/status`는 인증 뒤 두 provider의 고정 label/configured boolean만 반환하고 adapter endpoint·key·session 진단은 반사하지 않는다.
- `ClovaStreamingTranscriber`는 공식 TLS host/port와 `authorization: Bearer …` metadata, CONFIG 성공 확인, headerless PCM16 16 kHz mono 32,000-byte DATA, 마지막 DATA의 nonzero `seqId`+`epFlag=true` acknowledgement를 사용한다. response의 `position`과 `alignInfos`를 수업·소유자별 bounded continuity state의 텍스트 tail·확정 시각 frontier와 대조한다. healthy 스트림의 지연 경계와 새 스트림의 최대 3초 replay를 구분해 누락과 중복을 함께 줄이며, idle/4분 회전·종료 시 세션·continuity를 bounded하게 정리한다.
- CLOVA의 모호한 timeout/단절은 provider 원문을 로그·응답에 싣지 않고 `424`로 바꾼다. 브라우저는 CLOVA 청크를 자동 retry하거나 Qwen으로 fallback하지 않는다. 같은 프로세스에서 응답 이후 DB 쓰기가 실패한 동일 payload는 adapter 성공 cache로 외부 재호출을 피하지만, 프로세스가 정확히 그 사이 죽는 경우까지 외부 exactly-once 과금은 보장하지 않는다.
- WAV는 최대 512,000 bytes, 0.05~15초, 16 kHz mono PCM16만 받는다. VAD가 침묵 hallucination 저장을 줄인다.
- 새로 처리한 실시간·파일 import 음성은 overlap을 제거한 16 kHz mono PCM WAV로 `.data/recordings/<private-account>/<lecture UUID>.wav`에 먼저 staging한다. 계정 폴더는 `0700`, 파일은 `0600`이며 과거에 이미 버린 음성은 소급 복구하지 않는다. Drive가 꺼진 구성에서는 이 경로가 기존처럼 영구 보존 위치다.
- WAV 저장은 시간축에 이미 있는 PCM과 byte 단위로 대조해 같은 요청/응답 유실 재시도가 파일을 늘리지 않게 한다. 제한된 건너뛴 구간은 무음으로 채우고, 4시간·전체 20 GiB·최소 여유 1 GiB 한도를 적용한다.
- schema v7의 `recording_archives`는 `pending/uploading/ready/attention`, opaque HMAC object key, Drive file locator, resumable session, 원본 크기·checksum과 로컬 삭제 여부를 lecture FK로 저장한다. 녹음 archive 상태·다운로드 API는 locator·세션 URL을 반환하지 않고, 인증된 사용자 식별 외의 추가 계정 목록을 노출하지 않는다.
- schema v8의 singleton `drive_archive_binding`은 첫 업로드의 Drive `user.permissionId`와 OAuth client ID를 deployment identity key로 HMAC한 값 및 archive folder locator를 private DB에 고정한다. cleanup/download/trash와 후속 upload는 `about.get(fields=user(permissionId))`로 같은 계정·client인지 재검증하며 mismatch에서는 로컬 WAV와 DB를 보존한다.
- schema v9의 `drive_archive_user_folders`는 runtime 계정과 opaque HMAC folder key·private Drive folder locator를 로컬 DB에 고정하고, 기존 archive 행의 `folder_layout_version=0`은 사용자 폴더 배치가 확인될 때만 1이 된다. 기존 루트 WAV와 과거 resumable session 결과도 체크섬·부모를 검증해 metadata-only 이동한 뒤에만 로컬 정리를 허용한다.
- `GoogleDriveStorage`는 private authorized-user token을 갱신하고, 정확히 `drive.file` scope만 허용하며, redirect 없는 bounded `httpx` 요청으로 폴더 탐색·재개 업로드·부모 이동·Range download·trash를 처리한다. provider 본문과 비밀 locator는 예외·CLI에 반사하지 않는다.
- `DriveArchiveManager`는 단일 background worker와 process/flock 직렬화로 업로드·폴더 배치·trash를 처리한다. 서버 재시작 시 `uploading`을 재시도 상태로 회복하고, 업로드·이동 응답 유실 시 opaque object key와 실제 parent를 재조회해 중복 생성·중복 이동을 피한다.
- 녹음 파일은 `POST /imports` → 고정 480 KiB `PUT /imports/{id}` → `POST .../complete` 계약으로 계정별 `0700` 폴더의 UUID 파일(`0600`)에 올린다. 최대 1 GiB, 디코딩 음성 4시간, 계정당 활성 작업 1개다.
- 브라우저와 서버는 모든 480 KiB 조각의 SHA-256을 순서대로 묶은 v2 지문으로 재선택 파일 전체를 검증한다. 브라우저/서버 모두 전체 파일을 메모리에 펼치지 않는다.
- PyAV 18.1.0이 첫 오디오 스트림만 16 kHz mono s16으로 스트리밍 디코딩한다. nested URL/playlist I/O, 비정상 채널·sample rate·frame duration을 거절하고 디코딩 뒤 최대 15초 PCM만 유지한다.
- PyAV/FFmpeg 네이티브 디코더는 API 프로세스 안에서 실행된다. 허용된 소수의 인증 사용자가 신뢰할 수 있는 수업 파일만 올린다는 운영 가정이며, 악의적으로 만든 미디어의 네이티브 hang/crash까지 OS sandbox로 격리한 구조는 아니다.
- 결정적 import chunk UUID와 기존 chunk idempotency로 정상 종료 뒤 재시작 시 완료 청크를 재사용한다. 단일 background worker는 loop 예외를 재시도하고 GET/list가 죽은 worker를 다시 확인한다.
- 완료·취소·실패 DB 상태와 별도로 `raw_deleted`를 기록한다. unlink 실패를 삭제 성공으로 표시하지 않으며 60초 maintenance/list 조회에서 재시도한다. 7일간 멈춘 업로드는 서버를 재시작하지 않아도 정리한다.
- 녹음 다운로드는 소유권·완료 상태를 확인한 인증 POST가 60초짜리 무작위 전용 경로를 발급하고, 네이티브 파일 응답은 세션 Bearer를 URL에 넣지 않는다. 로컬 WAV는 `O_NOFOLLOW` descriptor와 WAV 구조를 검사하고, Drive WAV는 같은 ticket 경계 뒤에서 API 서버가 Range를 프록시한다. 브라우저는 Google host로 접속하지 않고 Drive ID·OAuth token을 보지 못한다. 짧은 Wi-Fi 중단 뒤 유효 시간 안에서 최대 16회 Range 재개를 허용한다.
- `MindlogicPostprocessor`는 공식 Gateway로만 나가는 bounded HTTP client, strict schema/문장 매핑 검증, 개인정보 형식 가림과 숫자 보호를 담당한다. 후보정 endpoint는 owner/final 상태와 원문 revision을 확인하고 단일 background worker가 별도 correction 행을 처리한다. worker는 Qwen `inference_lock`과 분리돼 과거 수업 후보정이 다른 수업의 실시간 전사를 막지 않는다. `/status`에는 key가 아니라 configured/model만 보인다.
- 수업 삭제는 진행 중 import, pending chunk, processing correction이 있으면 `409`로 거절한다. queued correction은 같은 transaction에서 먼저 지운다. durable `deleting`을 먼저 기록하고 Drive WAV가 있으면 trash 성공/이미 없음을 확인한 뒤 로컬 WAV와 lecture cascade 데이터를 제거한다. 원격 오류나 로컬 삭제 실패를 성공으로 표시하지 않고 재시작·재요청으로 완료한다. CLOVA Object Storage 사본은 이 범위에 없다.
- `MODEL_WARMUP=1`이면 lifespan 중 모델을 로드하고 첫 경로를 실행한다.

### 웹

- GitHub Pages에는 `web/`에서 복사한 공개 파일과 Actions가 생성한 런타임 `config.json`만 격리 staging을 거쳐 배포하며, 외부 스크립트·폰트·분석 코드를 쓰지 않는다.
- 클라이언트에는 계정 allow-list가 없다. 초대 링크의 opaque `username`/`setup_code`만 폼에 복원한 직후 URL fragment에서 지운다. 초대 링크의 `api=` 값은 무시한다.
- 서버 주소 변경 폼은 후보 주소의 anonymous health를 먼저 확인한다. 확인 중에도 기존에 검증된 origin만 현재 token의 대상으로 유지해 진행 중 업로드를 끊지 않고, 성공하기 전에는 후보 API 주소와 token을 섞어 쓰지 않는다. 로그인 요청 중에는 변경을 막고, 늦은 인증 응답은 요청 당시 origin/generation이 달라졌으면 폐기한다.
- 로그인 token은 JS 메모리에만 있고 API origin만 localStorage에 저장한다.
- Quick Tunnel이 준비되면 운영 스크립트가 `version/state/apiUrl/publishedAt/expiresAt`으로 된 정확한 공개 JSON 전체를 GitHub Actions 저장소 변수에 원자적으로 넣고 Pages를 다시 배포한다. Git의 `web/config.json`은 비어 있으며 ID·초대 코드·비밀번호·token·수업 정보는 게시기가 읽지 않는다.
- 웹은 로그인 전에 같은 Pages 출처의 런타임 설정을 엄격히 검사하고 Bearer 없는 `/health`를 통과한 origin만 설치한다. 새 유효 설정은 stale localStorage보다 우선하며, offline·만료·잘못된 설정은 fail closed 한다. 수동 입력은 자동 게시 장애 복구용으로만 남긴다.
- GitHub Pages 프로젝트들은 `https://superwonso.github.io` 출처를 공유하므로 다른 Pages 저장소까지 신뢰해야 하는 구조적 한계가 있다. 전용 Pages 계정/도메인 없이는 완전 격리할 수 없다.
- AudioWorklet → 스트리밍 resampler → 16 kHz PCM16 WAV 경로다.
- 입력 소스는 마이크와 `getDisplayMedia`의 공유 오디오다. 브라우저가 요구하는 video track은 종료 감지만 하고 오디오 그래프·WAV·네트워크에 연결하지 않는다. 화면/시스템 오디오는 지원 여부가 브라우저·OS·선택 표면·DRM에 달려 있으며 알림/다른 앱 소리 포함 위험을 UI에 표시한다.
- 서버가 아직 확인하지 않은 실시간 WAV 조각은 같은 GitHub Pages 출처의 IndexedDB에 순서·동일 UUID와 함께 임시 보관하고, 서버 ACK 뒤 해당 조각을 삭제한다. 브라우저를 새로 열어 같은 계정으로 로그인하면 남은 조각을 복구한다. 큐에는 비밀번호·로그인 token·초대 코드·API 주소를 넣지 않는다. 저장 실패 때는 메모리 fallback과 사용자 경고를 사용하므로 디스크 여유·브라우저 quota를 운영 전에 확인해야 한다.
- IndexedDB는 origin 단위 저장소다. 이 앱과 같은 `github.io` origin의 다른 Pages 저장소 스크립트도 같은 신뢰 경계 안에 있으므로, 미확인 음성과 수업 메타데이터를 보호하려면 그 저장소들까지 신뢰하거나 전용 Pages 계정/도메인으로 분리해야 한다. 서버 ACK 뒤 정상 삭제되지만 비정상 종료·브라우저 정리 실패 뒤에는 남을 수 있어 앱의 복구/정리 상태를 확인한다.
- 네트워크·서버 혼잡·터널 지연처럼 재전송해도 안전한 오류는 동일 chunk UUID를 유지한 채 상한이 있는 지수 backoff 간격으로 횟수 제한 없이 큐 순서대로 재시도한다. 캡처는 계속되고 대기 음성이 IndexedDB에 쌓이므로 지연이 길면 브라우저 저장공간을 확인한다. 결과 수신 여부가 모호한 CLOVA 오류는 중복 기록·과금 가능성 때문에 이 자동 재시도 대상이 아니며 해당 조각을 명시적으로 보류한다.
- 페이지가 정상적으로 실행 중인 동안의 일시적인 `AudioContext` `suspended`/`interrupted`와 live track `mute`는 수업을 끝내지 않고 입력 복구를 기다린다. mute가 오래 지속돼도 앱이 임의로 그래프를 닫지 않으며, 사용자가 **입력 복구 시도**를 누른 경우에만 같은 수업에서 입력 재연결로 전환한다. 영구적인 track `ended`/context `closed`도 수업·큐·누적 시간축을 끝내지 않고 사용자 동작으로 입력만 다시 연결하며, 실제 **종료**에서만 final 조각을 만든다.
- 브라우저나 OS가 백그라운드 탭·화면 잠금 상태에서 프로세스나 마이크 캡처 자체를 멈춘 동안의 소리는 앱이 나중에 복구할 수 없다. 특히 모바일·태블릿에서는 수업 중 화면을 켜고 앱 탭을 전면에 유지하는 것을 권장하며, 재연결 뒤에는 중단 구간이 기록되지 않았음을 전제로 끝부분을 확인해야 한다.
- 마이크 provider selector는 로그인 첫 인증된 `/status`가 CLOVA configured를 true로 알리면 CLOVA를 우선 선택하고, 미설정·상태 확인 실패 때는 Qwen을 사용한다. 사용자가 고른 마이크 provider는 localStorage에 저장하지 않고 해당 계정 세션에만 보존하며, system audio를 고른 동안 selector는 Qwen으로 고정했다가 마이크로 돌아오면 선호를 복원한다. 로그아웃·계정 scrub에서는 자동 선택으로 초기화한다. CLOVA 선택 화면에는 사이트 운영자가 관리하는 NAVER Cloud 계정으로의 음성 전송, 운영자가 연결한 Object Storage 자동 저장, 앱 삭제와 클라우드 삭제가 다르다는 내용을 표시하되 최종 사용자에게 과금 경고는 표시하지 않는다.
- lecture 생성 전에 provider를 session에 snapshot하고 생성 body와 모든 queued chunk에 유지한다. CLOVA는 한국어/영어 직접 선택만 허용한다. CLOVA 조각은 HTTP 요청 전에 IndexedDB에서 `queued → inflight`를 영속화하고, 응답 전 탭 종료나 네트워크/HTTP `424`처럼 결과가 모호한 경우 자동 retry 없이 해당 조각을 보류한다. 실패 WAV 보관과 위험을 명시한 수동 재전송만 제공하며 캡처와 후속 조각의 로컬 보관은 계속된다.
- 새 음성 8초 이후 조용한 0.24초 지점을 찾고 전체 WAV 최대 15초, 이전 3초 overlap으로 자른다. 사용자 pause는 acknowledgement 전에 받은 새 PCM만 비최종 청크로 flush하고 worklet 입력을 버리는 상태로 바꾼다. resume은 같은 수업·누적 음성 시간축·3초 overlap으로 이어지며, stop 직후에는 overlap만 남아도 final guard 조각을 보내 마지막 확정을 요청한다.
- pause 뒤 브라우저가 사라져 final guard를 잃은 미완료 수업은 기존 owner-only `recording-finalize`가 서버 WAV의 마지막 최대 3초를 overlap-only final로 한 번 재추론한다. 비공개 내부 sentinel chunk를 inference 전에 claim하고, GPU 대기 중 DB·recording lock을 놓으며, commit 직전 owner/deleting/pending/import와 WAV frame revision을 다시 확인한다. 완료/응답 유실 재요청은 같은 segment ID를 replay해 끝 문장을 중복하지 않는다.
- 일시적 네트워크/429/503과 idempotent 처리 중 409는 동일 chunk UUID로 상한이 있는 지수 backoff를 적용해 횟수 제한 없이 큐 순서대로 재시도한다.
- 형식·권한 오류와 모호한 CLOVA 결과처럼 자동 재전송이 안전하지 않은 오류는 해당 조각을 blocked 상태로 두고 캡처와 후속 WAV의 로컬 보관은 계속한다. 사용자는 다시 보내거나 첫 실패 WAV의 다운로드를 요청하고 실제 저장을 별도로 확인한 뒤 그 조각을 건너뛸 수 있다. 건너뛴 구간은 기록에서 빠진다.
- Quick Tunnel 주소가 바뀌면 녹음·전송 중에도 same-origin 런타임 설정의 후보 주소를 Bearer 없이 익명 확인할 수 있다. 새 origin에는 기존 token을 절대 보내지 않고 대기 WAV·UUID·기존 소유자 binding을 유지한 채 같은 계정의 재로그인을 요구한 뒤 전송을 잇는다.
- 인증 화면으로 돌아간 뒤에도 캡처가 살아 있으면 별도 **이 기기의 녹음 종료** 버튼을 제공한다. Web Locks 지원 브라우저에서는 owner fingerprint 기반 capture lock을 수업 전체에 유지하고 uploader lock으로 같은 계정의 탭을 직렬화한다. 저장 실패로 현재 탭 RAM에만 남은 조각이 있으면 명시적 종료 뒤에도 그 조각의 서버 ACK 또는 다운로드 확인 후 건너뛰기까지 capture lock을 유지한다. 삭제·녹음 마무리는 capture lock을 즉시 얻은 뒤 uploader lock까지 차례로 얻고, 그 안에서 메모리 큐와 공유 IndexedDB에 해당 수업의 미전송 조각이 0개임을 다시 확인한 경우에만 실행한다. 큐를 읽지 못해도 fail-closed한다. API가 있는데 lock 요청 자체가 실패하면 capture·파괴 작업은 fail-closed하고, 아직 HTTP를 시작하지 않은 uploader lock 실패만 backoff 재시도한다. Web Locks 미지원 시에는 같은 탭만 조정할 수 있어 UI 경고와 한 계정 한 실시간 탭 운영 규칙이 필요하다.
- `recording`/`paused`로 복구된 IndexedDB 세션을 다른 탭이 자동 종료하거나 삭제하지 않는다. crash가 남긴 미완료 수업은 완료로 표시하지 않고 지속 경고하며, 다른 탭의 녹음 여부를 확인한 사용자가 해당 수업의 **녹음 WAV 마무리**를 실행해야 final guard를 복구한다. 다른 기기까지 막는 서버 capture lease는 아직 없다.
- 큐는 개수 8개에서 강제 중단하지 않고 미확인 조각을 IndexedDB에 보존한다. 서버 지연이 길수록 저장량이 계속 늘 수 있으며 quota/디스크 고갈은 별도 실패 조건이다.
- 서버 응답 segment ID도 다시 검사해 같은 응답 렌더링을 중복 append하지 않는다.
- `RecordingFileUploader`는 파일을 480 KiB씩 지문 확인·해시·순차 업로드하고 서버 offset으로 재개한다. 업로드 중 탭을 닫으면 같은 전체 지문의 파일 재선택이 필요하지만 queued/processing은 파일 참조 없이 polling을 복구한다.
- import polling delay의 abort listener는 매 회 제거한다. 로그아웃/탭 이탈은 watcher만 detach하고 서버 작업을 취소하지 않으며, 취소/완료 경쟁과 오래된 lecture/list 응답은 terminal 상태·generation/sequence로 판별한다.
- 지난 수업을 `Asia/Seoul` 날짜로 묶고 단일 날짜를 필터링한다. 상세 기록은 UTF-8 TXT 또는 Markdown으로 내보내며 Markdown 제어문자와 파일명 문자를 정리한다.
- 녹음 WAV 버튼은 `recording_available`인 수업에만 열린다. 미확정 WAV는 owner-only idempotent finalize POST로 받은 범위까지 먼저 닫고, 로그인 Bearer로 ticket을 받은 뒤 같은 API origin의 상대 경로만 세션 토큰 없이 브라우저 네이티브 다운로드로 연다.
- API의 `recording_storage_state`를 `local_recording/upload_queued/uploading/retrying/drive_cleanup_pending/drive_ready/attention_required/none`만 허용해 저장 상태로 표시한다. 전송 중 상태는 20초 서버 polling과 수업 목록을 연계해 갱신하며 예상하지 못한 값은 Drive locator나 provider 문구로 렌더링하지 않는다.
- 완료 수업 후보정은 `queued/processing`을 주기적으로 확인한다. 진행 수업에서 누른 버튼은 캡처를 멈추지 않고 예약만 보관하며, 명시적 종료와 final 저장 성공 뒤 자동 시작한다. 과거 수업 후보정은 다른 수업의 녹음·일시정지와 병행할 수 있다. 원문/AI 후보정본을 명시적으로 전환하고 현재 선택한 버전만 TXT/Markdown으로 내보낸다. 완료 뒤에도 원문이 기본이며 `uncertain_terms`를 최대 5개와 나머지 개수로 표시한다. 수업·계정 전환은 poll timer와 늦은 결과를 generation/sequence로 폐기한다.
- 후보정 패널은 텍스트만 NOVA로 가고 오디오는 가지 않으며 형식 기반 가림이 이름 등을 보호하지 못한다는 opt-in 안내를 항상 표시한다. 결과는 모두 `textContent`로 렌더링한다.
- 로그인 세션 만료나 인증 거절은 pause로 취급하지 않는다. 현재 캡처와 영속 큐는 소유자 binding을 유지하고, 같은 계정으로 재로그인해야 남은 전송을 재개한다. 다른 계정은 그 큐를 인수하거나 전송할 수 없다.
- 수업 삭제 확인창은 제목과 원문·후보정본·녹음 삭제 범위, Drive WAV는 휴지통으로 이동한다는 점, CLOVA Object Storage는 별도라는 점을 보여 준다. 삭제·다운로드·녹음·파일 변환 중 동작을 상호 잠그고 generation/sequence로 늦게 도착한 이전 수업·계정 응답을 버린다.
- 로그인 뒤 관리자 권한은 `/admin/overview`의 200/403으로 서버에서 판별한다. 전용 dialog는 10초마다 상태를 갱신하고 자가 세션 종료를 렌더링하지 않는다. 운영 중지·터널 재연결·다른 계정 세션 종료는 확인 단계를 두고, 안전한 운영 재개는 즉시 적용한다.
- 모든 로그인 브라우저는 15초 heartbeat와 상태 전환 때 최소 presence만 전송한다. 로그아웃·계정/서버 전환은 timer와 늦은 응답을 폐기한다.
- 터널 재연결 202 뒤 Pages `config.json`과 anonymous health를 5초 후부터 8초 간격, 최대 3분 재확인한다. 새 origin이면 기존 보안 경계대로 token을 버리고 재로그인을 요구한다.

### 운영 스크립트

- `scripts/setup.sh`: ROCm Python을 system-site-packages로 재사용하는 venv, Qwen 모델, cloudflared 설치/검증
- `scripts/start-server.sh`: import-time 운영 DB 접근을 피하는 Uvicorn factory, 단일 worker, loopback bind, 기본 warmup, PID·로그 관리
- `scripts/start-tunnel.sh`: 서버 health 확인 후 Quick Tunnel 생성, URL 파일 기록, GitHub Pages 런타임 주소 게시와 배포 확인. 게시 실패 시 health-checked 터널은 남겨 재게시나 수동 복구가 가능하지만 명령은 성공으로 표시하지 않음
- `scripts/publish-api-url.sh`와 `scripts/runtime_config.py`: 공개 tunnel origin 또는 `OFFLINE` 입력을 정확한 상태·주소·게시/만료 시각 JSON으로 만들고, 로그인된 GitHub CLI로 그 JSON 전체를 Actions 변수에 저장해 Pages workflow를 실행한다. URL·스키마·만료를 엄격히 검사하며 Git 커밋에는 동적 주소를 쓰지 않음
- `scripts/status.sh`: PID 소유권, 로컬 health, 실행 중인 tunnel URL과 외부 HTTPS health 표시. 죽은 터널의 마지막 URL은 현재 주소와 구분
- `scripts/stop.sh`: tunnel 먼저, 서버 다음으로 SIGTERM 종료; PID가 다른 프로세스를 가리키면 건드리지 않음
- `scripts/backup.sh`: SQLite online backup으로 WAL의 최신 커밋까지 일관된 스냅샷 생성; 기존 대상은 덮어쓰지 않음. WAV·Drive 파일·OAuth는 포함하지 않음. 전체 복원을 위해 DB, 로컬 staging, private Drive 폴더, `.data/google-drive/identity.key`·OAuth client/token을 같은 시점에 암호화한 개인 저장소로만 백업해야 함
- `scripts/google_drive.py`: 개인 Gmail Desktop OAuth의 loopback PKCE 승인, 집계만 보이는 `status`, 변경 없는 `migrate --dry-run`, 중단·재개 가능한 기존 WAV 이전을 제공. 관리 server PID가 살아 있으면 auth·실제 migrate를 거절하고 제목·계정·경로·Drive ID·인증 값을 출력하지 않음
- `server/manage.py`: private `ACCOUNT_USERNAMES`의 미활성 계정 invite 생성과 `SITE_ORIGINS` 갱신. `add-account`는 TTY echo를 끈 입력으로 기존 환경설정·DB 집합에 비활성 계정 하나만 함께 추가하며, 일반 시작의 exact-set 검사는 유지함. 서버 주소와 초대 링크를 같은 로컬 파일에 쓰되 링크 자체나 서버 설정에는 임시 API 주소를 결합하지 않음
- `server/manage.py configure-admin`: 활성 계정이 하나면 자동 선택하고 여럿이면 TTY echo를 끈 입력 또는 `--position 1` 같은 비공개 목록 위치로 선택해 실제 ID를 출력·shell history에 남기지 않고 `ADMIN_USERNAME`을 기록
- `server/manage.py configure-clova`: Basic 스트리밍/장문 도메인의 Secret Key를 TTY echo 없이 받아 권한 `0600`의 ignored `server/.env`에 저장한다. gRPC 목적지는 입력받지 않고 공식 host에 고정한다.
- `server/tunnel_control.py`: fixed project script·argv, process/script ownership, in-process lock와 script flock을 겹쳐 검증하고 12초 응답 유예 뒤 Quick Tunnel stop/start를 수행. 물리적 stop은 원격 재시작 경로도 없앤다는 상태를 명시하며 웹에는 restart만 연결

### Google Drive 운영 설정

- 개인 Gmail에서 Google Drive API를 켜고 External OAuth 동의 화면과 **Desktop app** client를 만든다. Testing이면 본인을 test user로 넣는다. 다운로드한 JSON을 `.data/google-drive/oauth-client.json`에 `0600`으로 두고 비밀값을 문서·채팅에 복사하지 않는다.
- API 서버만 종료한 뒤 `./.venv/bin/python scripts/google_drive.py auth`로 loopback 승인한다. 브라우저 자동 실행 실패 시 명령이 대기할 때만 생기는 private `authorization-url.txt`를 본인 브라우저에만 연다. 승인 후 `server/.env`의 `GOOGLE_DRIVE_RECORDINGS=1`, `GOOGLE_DRIVE_OAUTH_CLIENT_FILE`, `GOOGLE_DRIVE_TOKEN_FILE`을 확인한다. chunk/connect/read/retry 튜닝은 `GOOGLE_DRIVE_UPLOAD_CHUNK_BYTES`, `GOOGLE_DRIVE_CONNECT_TIMEOUT_SECONDS`, `GOOGLE_DRIVE_READ_TIMEOUT_SECONDS`, `GOOGLE_DRIVE_RETRY_MAX_SECONDS`이며 처음에는 `server/env.example`을 유지한다.
- 기존 WAV는 `status` → `migrate --dry-run` → `migrate --limit 1 --keep-local` → 의도한 Gmail Drive의 계정 하위 폴더에서 첫 파일 확인 → `migrate` 순서로 옮긴다. 첫 파일에서 계정·OAuth client binding이 확정된다. v8 루트 파일은 `addParents/removeParents`로 사용자 폴더에 옮긴 뒤 검증하며 재업로드하지 않는다. 실제 migrate는 관리 API 서버가 종료된 상태에서만 허용되며 제목·계정·로컬 경로·Drive ID를 출력하지 않는다. 정확한 명령과 권한은 README의 `개인 Google Drive에 녹음 보관`을 따른다.
- External OAuth 게시 상태가 Testing이면 Drive scope refresh token이 일반적으로 7일 뒤 만료한다. 장기 운영은 Production 전환 여부를 확인하고, Testing을 유지하면 API를 끄고 주기적으로 `auth`를 다시 실행한다.
- `.data/google-drive/oauth-client.json`, `token.json`, `identity.key`, 재개·lock 상태는 `.gitignore`와 `.data/` 경계 안에 두고 `0700/0600`을 유지한다. DB와 Drive 연결을 복원하려면 이 파일과 SQLite를 암호화된 개인 저장소에만 같이 백업하고 절대 Git에 넣지 않는다.
- 마지막 resumable PUT 응답 유실 직후 삭제하는 경계에서는 session URI가 `308`이면 DB/local을 지우지 않는다. 빈 PUT이 비활성 만료를 계속 늦추지 않도록 `next_attempt_at`을 8일 뒤로 영속 저장하고, 그 전에는 exact object 검색만 수행한다. 완료 파일은 checksum·opaque object를 확인해 휴지통으로 옮기며, 공식적으로 session 404 만료가 확인된 뒤 exact 검색도 비어 있을 때만 로컬/DB 삭제를 승인한다.

## 검증 범위

### 최신 릴리스에서 확인

- Python 전체 274개(37.491초), Node 전체 136개(1.796초), Python/JavaScript/Bash 구문과 diff 검사가 통과했다. 최초 Drive 업로드 전 삭제 경계 3개와 실제 운영 포트와 분리한 ephemeral socket 재시작 회귀 3개를 추가했다.
- 실제 Drive 메타데이터 10건, 표본 1건 전체·Range 재조립 checksum과 연결 조기 종료 후 재다운로드를 확인했다. 실계정 업로드·폴더 이전·전수 메타데이터 검증은 앞선 이전 단계에서 완료했다. 이번 릴리스에서는 기존 녹음을 수정하거나 삭제하지 않았다.
- API 재시작 후 local/external health 200, 무인증 `/status` 401, 잘못된 Origin 403, Pages CORS preflight 200, runtime config의 현재 tunnel·online·만료 전 일치와 공개 자산 9개 byte-for-byte 일치를 확인했다. `.nojekyll`은 Pages 제어 파일이므로 공개 브라우저 자산 비교 대상에서 제외한다.
- 비공개 계정·env/DB/OAuth 값 41개를 릴리스 후보 파일과 reachable Git history 전체에 대조해 일치 0이었다. 개인정보·키·녹음·개인 수업은 staging에 포함하지 않았다. 아래 이전 단계의 미푸시 문구는 당시 체크포인트를 뜻하며 최신 배포 상태가 아니다.
- ASR 코드 검토는 [ASR_REVIEW.md](ASR_REVIEW.md)에 기록했다. 모델·ASR 처리 방식은 바꾸지 않았으며, 개선안에 대한 새 실음성 정확도·브라우저 종단 지연·장시간 평가는 하지 않았다.

### Drive 구현 직후 확인 (릴리스 전 기록)

- 제한 없는 호스트 환경에서 `./.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q`: 서버/API/DB/설정/전사기/importer/녹음·후보정·관리자·터널·CLOVA·Google Drive 계약 268개가 36.307초에 모두 통과했다. 이 체크포인트의 Drive/CLI 집중 계약은 OAuth 최소 scope·비밀 파일 권한, checksum, resumable 재개/응답 유실, Gmail/client binding, 404 fail-closed, 8일 session inactivity, 사용자별 폴더·기존 루트 파일 이동·이동 응답 유실, 동시 유지보수 경합, PID 파일이 유실된 서버 탐지, Range·disconnect close, startup 비차단, owner 격리, 휴지통-before-DB 삭제를 포함한다.
- 실제 Secret Key와 Basic 스트리밍 도메인으로 공개 KSS 한국어 낭독 12.645초를 보냈고, 대기하지 않고 clock을 강제로 241초 전진시켜 4분 rotation 경로를 실행했다. 최종 코드의 wall time은 12.281초, native transport는 2개였으며 두 번째 스트림이 overlap 안의 `이다.`를 회수해 목표 문장이 전체 결과에 정확히 한 번 남았다. final 뒤 adapter의 활성 session과 reader thread는 모두 0이었다. 이는 짧은 단일 화자·강제 시각 시험이지 자연 경과 4분 또는 장시간 수업 시험은 아니다.
- 수정 전 구현의 300.818초 진단 실행도 native transport 2개와 프로세스 RSS 약 12.6 MiB 증가로 끝났지만, 바로 그 실행에서 회전 경계의 정확한 누락을 재현했다. 따라서 이 이전 결과를 최신 경계 보완의 5분 통과 기록으로 취급하면 안 된다.
- VS Code Server Node.js 24.18.0으로 `node --test --test-isolation=none tests/*.test.mjs`의 앱 상태 99개, 오디오 20개, 파일 가져오기 10개, 정적 웹 경계 7개 등 136개가 2.522초에 모두 통과했다. 설정된 CLOVA를 새 마이크 수업의 기본값으로 고르는 계약과, 최종 사용자 화면에는 비용·과금 문구를 표시하지 않으면서 사이트 운영자가 관리하는 NAVER Cloud 계정으로의 음성 전송·연결된 Object Storage 저장은 고지하는 계약을 포함한다. Google Drive 추가분은 저장 상태 8종 매핑, locator 미노출, 브라우저가 Google host로 직접 접속하지 않는 CSP, Drive trash와 CLOVA 사본 삭제 범위 문구를 검사한다.
- 앱 테스트 대역도 실제 UUID·owner·provider·capture 시간축·CLOVA `inflight` 전이·final-last·영속 삭제 계약을 검사하도록 강화했고, PCM 없는 종료에 합성 final WAV를 넣지 않는다. 따라서 새 내구성 경로의 실패를 기대값만 완화해 숨기지 않았다.
- 최신 서버 변경에는 Python compile, `git diff --check`, shell syntax 검사와 private env/DB/OAuth의 실제 계정 ID·수업/Drive ID·CLOVA/NOVA/Google 비밀값 35개를 Git 후보 파일 67개 및 전체 reachable history blob 199개와 대조했고 일치 0을 확인했다. 운영 DB는 schema v9, `integrity_check=ok`, FK 오류 0이다. 실제 Gmail Drive에 기존 WAV 10개/236,776,806바이트를 3개의 사용자 폴더로 이전했고, 기존 루트 pilot 1개는 재업로드 없이 parent만 옮겼다. 이전 후 10개 전부의 object·크기·checksum·정확한 사용자 parent를 전수 재검증했으며 mismatch 0, organization/attention/retry/deleting 0, 로컬 WAV 0개/0바이트다. 이전 전 DB는 `.data/backups/pre-drive-user-folders-20260905.sqlite3`(schema v8, mode `0600`, integrity ok)에 보존했다. 최신 변경은 아직 GitHub에 푸시·Pages 배포하지 않았다.

### 이전까지 확인된 누적 기록

아래 결과에는 이번 영속 대기열·입력 재연결·탭 간 잠금 보완 직전 또는 그보다 이전 코드에서 실행한 실제 모델·장시간 검증이 포함된다. 최신 자동 회귀 결과는 바로 위에 적었고, 이번 변경 뒤의 외부·실기기 미실행 범위는 다음 절에 별도로 적는다.

- Windows 11/WSL2, Ryzen AI MAX+ 395, Radeon 8060S(gfx1151), WSL RAM 47 GiB, ROCm 7.2.4/PyTorch 2.10 환경을 직접 확인했다.
- 대상 Radeon/ROCm에서 Qwen3-ASR-1.7B BF16/SDPA 로드와 한국어 FLEURS 10개 추론
- 위 10개 구간의 RTF, CER, peak allocated/RSS, 샘플 간 allocated growth 측정
- 같은 10개를 실제 pause-aware 8~15초/3초 overlap/0.6초 guard로 처리: 13청크, CER 4.03%, RTF 0.466, 마지막 25자 일치, 텍스트 중복 탐지 0건, capture/partition gap 0. 이는 낭독 샘플에 한정되고 exactly-once 보장은 아니다.
- 같은 경로의 60분 35.59초 합성 타임라인: 416청크, CER 3.51%, RTF 0.442, 마지막 25자 일치, 경계 텍스트 중복 탐지 0건, capture/partition error 0 sample. GPU allocated 처음→끝 0 MiB, reserved 6 MiB, 프로세스 RSS 12.65 MiB 증가로 예외·OOM 없이 완주했다.
- 새 PyAV 파일 디코더→pause-aware/3초 overlap→실제 Qwen/forced aligner 경로로 FLEURS 한국어 WAV 10개(111.12초)를 처리했다. 19청크, CER 4.03%, 첫 모델 로드 포함 RTF 0.685, peak allocated 5.664 GiB, 첫 파일 뒤 allocated 증가 0 MiB였다. 반복 8-gram 초과는 0이지만 첫 샘플에는 틀린 날짜 구절 삽입이 있었다.
- 운영 DB와 분리한 TestClient/임시 계정에서 실제 FLEURS 한국어 WAV 4.8초를 `POST /imports`→분할 `PUT`→complete→실제 Qwen/forced aligner→lecture 조회까지 end-to-end 실행했다. 결과는 `completed`, 원본 파일 0개, `raw_deleted=true`였고 "교전이 발발한 직후 영국은 독일에 대한 해상봉쇄를 시작한다"로 기록됐다.
- 실제 WAV/합성 AAC-in-M4A의 PyAV decode/resample, 15초 bounded buffer, 4시간 한도 거절을 테스트했다. 표시된 다른 컨테이너/코덱의 실파일을 전부 시험한 것은 아니다.
- Whisper turbo OpenAI PyTorch/ROCm 기준선: 파일 전체 CER 4.03%/RTF 0.193, 고정 8초 CER 14.77%/RTF 0.239, 동일 3초 겹침·단어 시각 분할 CER 23.71%/RTF 0.259. 두 분할 시험에서 한 발화 전체 누락을 확인했다.
- Voxtral 공식 2400ms 스트림으로 FLEURS 10개: CER 4.70%, WER 21.53%, RTF 1.584. 10/10 delta와 final decode 일치, 반복 8-gram 초과 0, 마지막 right-padding 없을 때 tail이 잘리는 현상을 확인했다.
- 대상 Radeon/ROCm에서 VibeVoice-ASR-Streaming-7B(8.674B 파라미터) BF16/SDPA 로드: 23.72초, 로드 후 allocated 16.16 GiB, 짧은 실험 peak allocated 16.29 GiB
- VibeVoice 공식 live-state API로 FLEURS 한국어 3개 추론: RTF 1.016(첫 encoder compile 포함), 0.753, 0.724; 화자 표지만 제거한 간이 aggregate CER 25.2%. 숫자와 한글 수사 차이 및 `[Silence]` 태그까지 벌점인 비공식 지표다.
- 별도의 실제 음성 3개를 0.5초 간격으로 14회 반복한 472.08초 단일 VibeVoice state 완주: 누적 RTF 0.882, custom content CER 15.75%. 이 지표는 화자·태그·공백·문장부호를 제거했고 반복 음성이므로 앞의 25.2%와 직접 비교할 수 없다.
- 위 7:52 실험에서 2/4/6분/종료의 KV 1,702/3,368/5,034/6,574, allocated 16.324/16.412/16.501/16.585 GiB, reserved 16.586/17.887/19.820/22.193 GiB, 최근 10청크 평균 연산 2.149/2.398/3.018/3.253초를 기록했다. peak allocated는 16.957 GiB, 최대 연속 backlog 청크는 3개였다.
- VibeVoice 짧은 종료 처리에서 존재하지 않는 `Speaker 1: 그치.`와, 이미 lookahead로 처리한 0.14초를 공식 flush loop가 다시 넣어 `[Silence]`를 덧붙이는 현상을 재현했다. 7:52 반복 실험은 기대한 42개 발화 라벨을 한 번씩 냈고 마지막 flush는 빈 문자열이어서 발화 단위 누락·중복은 보이지 않았지만, 이는 반복 낭독 음성에 한정된 결과다.
- VibeVoice 벤치마크 종료 후 별도 프로세스에서 PyTorch allocated/reserved 0과 free 46.80/47.47 GiB를 확인했다.
- 실제 NOVA key로 `/models/?type=llm` 200·`solar-pro4` 존재·`/credits/` 200을 확인했다. 모든 아라비아 숫자를 보호 표식으로 바꾸는 최종 코드에서도 비개인 한국어 3문장을 strict JSON Schema로 실제 후보정해 같은 문장 ID/순서, 기존 숫자 `15`, 이메일 placeholder 복원, 띄어쓰기 교정과 새 숫자 표기 로컬 경고를 확인했다. key와 credits 본문은 출력·로그에 남기지 않았다.
- mocked Gateway에서 청크 문맥이 target 결과에 중복되지 않는지, 한국어 조사에 붙은 이메일/전화·주민·카드·모든 아라비아 숫자 가림과 단일 복원, placeholder/ID/숫자 훼손 거절, 기존 수치를 바꾼 뒤 원래 수치를 덧붙이는 우회 거절, 새 숫자 경고, HTTP 408/425/429/5xx·network transient retry와 402 무재시도, 요청·응답 상한을 검증했다. correction API에서 owner-only, raw 불변, idempotent retry, final gate, processing 중 DELETE 거절, safe status/error를 검증했다.
- `threading.Event` 기반 격리 API 회귀 테스트에서 correction worker를 멈춰 둔 동안 다른 수업의 전사·녹음 final 저장이 끝나고, 반대로 Qwen fake 추론을 멈춰 둔 동안 완료 수업 후보정이 DB `completed`까지 진행되는 것을 확인했다. 두 수업의 raw segment와 별도 후보정 행은 섞이지 않았다.
- 코드 검토로 확인한 방어선: ignored 환경설정 기반 서버 측 계정 allow-list·DB exact-set 검사·소유권 검사, exact-origin 제한, chunked body 제한, 계정별 녹음 권한·UUID 경로·WAV 검증, import 원본 권한/삭제 상태, 동일 lecture/chunk/import UUID의 idempotent 응답, 초대 링크의 API 주소 배제, 계정 전체 주소 합산 로그인 제한
- 이번 내구성 변경 직전의 회귀 기록: 당시 Python 서버/API/DB/설정/전사기/importer/녹음 저장·후보정·관리자 API·터널 제어·Pages 런타임 설정 115개 테스트와 Node 웹 테스트 4개 파일(앱 상태 테스트 86개, 오디오 테스트 20개, 정적 웹 경계 테스트 7개)가 통과했다. 실제 ID/키 비공개 설정, 2~10개 계정 검증과 명시적 계정 추가 rollback, 기존 자격정보·세션·수업 보존, 세 번째 계정의 관리자 조작 거절·수업 격리, presence TTL·비영속성, 세션 해제 경쟁, persisted 운영 중지, 터널 요청 경합·PID/실행 파일 위조 거절, legacy DB 계정 제약 제거와 텍스트 전용 수업 완료 승격, 행·FK 보존, 설정 불일치 무변경 거절, 3자 비밀번호 거절·4자 허용, 파일 전체 지문, offset 재개, 소유권, raw 삭제 실패·재시도, 7일 정리, 취소/완료 경쟁, 로그인·로그아웃·계정·수업 전환의 오래된 UI 응답, system-audio track 분리, pause/resume 경계·제어 경쟁, 진행 수업 후보정 예약, 당시 인증 만료·tunnel 교체 복구, 다른 계정 인수 차단, 과거 수업 후보정과 live chunk 격리, final guard의 응답 유실·중복 요청·동시 WAV 변경·소유권 UUID 재사용 방어가 포함됐다. 이후 수정된 경로에는 이 통과 기록을 적용하지 않는다.
- 녹음 전용 시험은 overlap PCM 제거, byte-identical retry, bounded silence gap, quota 실패 시 기존 파일 불변, 부분 write rollback, symlink 거부/안전 삭제, 전송 중단 fd close, 60초 ticket Range 재개·만료, 명시적 final 복구, 다른 계정 불변, DELETE/inference 경합과 응답 유실 idempotency를 포함한다.
- 당시 웹 시험은 KST 자정 경계 날짜 그룹, TXT/Markdown 내용·이스케이프·안전 파일명, 동일 API origin ticket 제한, 마지막 실패 조각 확정과 WAV 없음의 정확한 안내, 507 수동 복구, 다운로드·삭제·계정 전환의 stale 응답 방어를 포함했다. 자동 주소의 익명 health 선행, stale 저장 주소 차단, 24시간 lease 만료 뒤 민감 본문 차단, 새 tunnel 재로그인 복구도 검증했으며 최신 정적 회귀 결과는 위 절에 갱신했다.
- GitHub Actions Pages 배포 성공 후 공개 URL의 `index.html`, `app.js`, `audio.js`, `file-import.js`, `pcm-worklet.js`, `style.css`가 로컬 배포본과 byte-for-byte 일치하고 HTTPS 200임을 확인했다.
- 실제 Quick Tunnel edge를 통해 `/health` 200, Pages Origin의 CORS 헤더와 새 파일 조각 `PUT` preflight를 확인했고, 허용하지 않은 Origin은 서버 경계에서 403으로 거절됐다. cloudflared의 DNS·UDP/QUIC·TCP·Cloudflare API 사전 점검도 모두 PASS였다.

### 아직 확인하지 못함

- 첫 pilot WAV는 사용자가 Drive에서 수동 재생해 확인했고, 이후 같은 Drive file ID를 유지한 metadata-only parent 이동과 전체 10개의 checksum/parent 재검증까지 완료했다. 다만 사용자별 폴더로 옮긴 후 브라우저에서 10개를 전부 새로 재생하는 수동 검사는 하지 않았다.
- 실제 Drive로 옮긴 WAV를 외부 브라우저에서 Range 중단·재개해 내려받기, 다른 계정의 404 격리, 수업 삭제 후 Drive 휴지통 이동, 만료/철회된 refresh token과 용량 부족 시 로컬 보존은 fake Drive 회귀 외에 실계정 end-to-end로 다시 확인해야 한다.
- Windows Chrome의 네이티브 임시 프로필과 localhost 합성 WAV 하네스로 IndexedDB/Web Locks 실동작 확인을 시도했다. WSL UNC 프로필의 Chrome DB 잠금 오류는 Windows 네이티브 프로필로 분리했지만, `--dump-dom` 실행기가 비동기 완료 상태를 폴링하지 못해 제품 성공·실패를 판정할 결과 마커를 얻지 못했다. 운영 origin·계정·API에는 접근하지 않았고 제품 실패로 판정된 항목도 없다. 따라서 영속 IndexedDB 대기열의 페이지 재실행 복구, Web Locks의 두 탭 상호 배제, 새 Quick Tunnel 재로그인, AudioContext 재개와 영구 track 종료 뒤 입력 재연결을 함께 반복하는 실제 브라우저 장시간 시험은 아직 하지 않았다. 자동 상태/오디오 계약은 위 136개 Node 회귀에 포함된다.
- 사용자가 실제로 들을 45~90분 한국어 수업 샘플의 인식 품질
- 교실 거리·잔향·잡음·여러 화자·전문용어 조건
- 90분 연속 실행 중 thermal throttling과 WSL 공유 메모리 회수
- 실제 학교 Wi-Fi와 태블릿을 사용한 Quick Tunnel 장기 연결 중단/복구
- 실사용 종료 버튼, 마이크 강제 중단, 화면 잠금 각각에서 마지막 음절·문장 보존
- 실제 데스크톱 Chrome/Edge에서 유튜브·강의 플랫폼 탭 오디오 선택, 알림 포함 범위, DRM 차단, 일시 mute·AudioContext 중단 복구, 영구 공유 종료 뒤 재연결 tail
- 실제 브라우저·Quick Tunnel에서 마이크와 공유 소리의 pause/resume을 여러 번 또는 45~90분 동안 반복했을 때의 경계 음절·3초 문맥·누적 시간축·메모리 증가, pause/stop 제어 경쟁과 인증 만료 순간의 final tail
- Web Locks 지원/미지원 브라우저 각각에서 같은 계정의 두 탭을 열었을 때 두 번째 capture 차단, uploader 인계, ACK 뒤 stale 메타데이터 정리, 삭제·finalize 차단을 실제 확인해야 한다. 운영 지침은 지원 여부와 관계없이 실시간 수업을 한 탭에서만 진행하는 것이다.
- 첫 입력 50ms 미만에서 pause 직후 페이지 프로세스가 사라지는 경우의 브라우저 전용 PCM. API 최소 WAV보다 짧고 IndexedDB enqueue 전이면 서버와 영속 큐 모두에 없어 현재 복구 범위 밖이다.
- 실제 NOVA Gateway 후보정과 로컬 Qwen 실시간 전사를 장시간 병행하는 외부 end-to-end. 현재 확인은 Event로 제어한 격리 fake 계약까지다.
- 실제 브라우저→Quick Tunnel로 큰 녹음 파일 업로드/중단/재선택/백그라운드 변환. 로컬 격리 API의 실제 Qwen E2E 한 파일은 통과했지만 활성 계정 외부 E2E는 아직이다.
- MP3/FLAC/OGG/Opus/WebM/MP4/MKV/MOV 각 실파일의 디코딩 호환성과 실제 압축 강의 음질
- 새 UUID로 동일 오디오가 다시 생성되는 앱/브라우저 수준의 의미적 중복
- 실제 구성 계정들의 외부 로그인·기록 격리 end-to-end
- 새로 추가한 비활성 계정의 실제 초대 링크 열기, 4자 이상 비밀번호 설정과 로그인. 운영 초대는 1회용이므로 자동 검증에서 소비하지 않았다.
- Quick Tunnel을 통한 실제 활성 계정 로그인, 실시간 WAV/녹음 파일 업로드, 기록 조회·다운로드. 외부 `/health`와 CORS까지만 확인했다.
- 실제 브라우저와 Quick Tunnel에서 새 녹음 WAV 다운로드를 중단·재개하거나, 다운로드 도중 같은 수업을 삭제하는 동작
- 기능 추가 전에 만든 과거 수업에는 원본 음성이 남아 있지 않으므로 녹음 다운로드를 제공할 수 없음
- VibeVoice를 8분보다 긴 하나의 state 또는 실제 45~90분 수업으로 운용했을 때의 품질·메모리·처리량. 이번 실험은 공식 목표에 가까운 7:52에서 끝났고 반복 낭독 음성을 사용했다.
- 실제 5~10분 이상 한국어 교실 원문에서 `solar-pro4` 후보정 전후 CER, 고유명사·전문용어·수식 보존, 의미 왜곡, 처리 시간과 credits 사용량. 현재 실제 Gateway 검증은 비개인 합성 텍스트 3문장뿐이다.
- NOVA/Mindlogic proxy 자체의 요청 보관·삭제 세부 정책과 외부 요청의 idempotency key 지원. 확인 전에는 완전한 비보관·비학습이나 exactly-once 과금이라고 주장하지 않는다.
- 실제 관리자 계정으로 Quick Tunnel을 거쳐 관리자 dialog를 열고 상태 갱신·다른 계정 세션 해제·운영 중지/재개를 누르는 브라우저 end-to-end. 현재는 격리 API와 Node UI 계약까지만 검증했다.
- 실제 운영 Quick Tunnel의 관리자 **재연결** 버튼. 단위 테스트는 고정 argv·응답 유예·동시 요청·실패 복구를 통과했지만, 임시 주소가 바뀌는 외부 작업은 관리자 계정 설정 뒤 별도로 확인해야 한다.
- 실제 Secret Key 인증·Basic 스트리밍 도메인·짧은 한국어 응답과 clock 강제 4분 rotation은 확인했다. 다만 같은 음성에서 Qwen보다 수업 정확도가 높은지, 실제 15초 과금 집계와 고객 Object Storage 결과가 로컬 segment와 일치하는지는 아직 확인하지 않았다.
- 최신 코드로 실제 시간이 자연스럽게 4분 이상 흐르는 연속 CLOVA 스트림, 45~90분 수업, 여러 번의 pause/resume, 학교 Wi-Fi 단절을 거쳤을 때 문맥·마지막 문장·중복·누락·메모리/스레드/channel 회수는 아직 확인하지 않았다. 현재 실계정 경계 시험은 12.645초 음성과 강제 clock으로 12.281초 안에 수행했다.
- CLOVA 무음 ACK, dead-stream 재연결, control 응답, 종료 경합은 fake gRPC를 포함한 최신 Python 전체 회귀에 들어갔다. 실계정으로는 짧은 정상 스트림과 강제 rotation 경계까지만 확인했으므로 무음·실제 단절·pause 경로를 외부 서비스에서 별도로 확인해야 한다.

실제 수업 음성 샘플이 제공되면 원본은 `.samples/`에만 두고 다음 순서로 검증한다.

1. 5~10분 익명화 구간으로 Qwen/Vibe/Voxtral/Whisper의 누락·고유명사·문장 경계를 수동 비교한다.
2. 같은 파일을 60~90분 길이로 반복 또는 연속 구성해 RSS, `torch.cuda.memory_allocated/reserved`, RTF를 청크마다 기록한다.
3. 같은 기준문으로 Qwen 원문과 Solar 후보정본의 CER 및 숫자·수식·고유명사 의미 왜곡을 비교한다.
4. 중간에 tunnel을 끊고 복구해 DB chunk 수, segment 순서, 중복 텍스트를 확인한다.
5. 녹음 종료·마이크 강제 중단·화면 잠금으로 final tail을 각각 검증한다.
6. 서로 다른 구성 계정으로 같은 lecture UUID 접근을 시도해 404와 기록 분리를 재확인한다.

## 복구 이력

처음 전달된 작업 폴더는 파일 내용과 이름이 한 칸씩 어긋난 형태로 평탄화되어 있었다. 예를 들어 루트 `README.md`는 favicon SVG였고 `HANDOFF.md`는 웹 `config.json`이었으며, `.git`도 저장소 metadata가 아니라 `.gitattributes` 파일 하나만 든 빈 디렉터리였다.

원본은 `/tmp/stt_server_flattened_20260903.tar`에 보존한 뒤 실제 내용을 판별해 `server/`, `web/`, `tests/`, `.github/workflows/`, `scripts/` 구조로 복구했다. `/tmp` 백업은 재부팅 후 사라질 수 있다.

복구 당시 만들어진 잘못된 루트 중복 파일이 남아 있다면 커밋 전에 내용 비교 후 제거해야 한다. 특히 다음 이름은 정식 파일이 아니라 평탄화 잔여물이었다.

```text
__init__ (1).py  __init__.py  app.py  app.test.mjs  audio.js
audio.test.mjs   config.json  download  env.example  favicon.svg
index.html       manage.py    requirements.txt  security.py  settings.py
setup.ps1        style.css    test_api.py  transcriber.py  tunnel.ps1
```

정식 경로는 `server/*`, `web/*`, `tests/*`, `scripts/*`다. 이 정리에서 현재의 루트 `README.md`, `HANDOFF.md`, `.gitignore`는 보존한다.

## 저장소·배포 체크포인트

사용자가 지정한 원격은 <https://github.com/superwonso/stt_server_cdh>이고 Pages 주소는 <https://superwonso.github.io/stt_server_cdh/>다. `superwonso` GitHub 인증을 연결해 공개 앱을 `main`에 push했고, Pages source는 GitHub Actions로 설정됐다. `Deploy classroom to GitHub Pages` 실행과 공개 자산 비교가 성공했다.

2026-09-04 관리자 기능 적용 전 `.data/backups/pre-admin-console-20260904.sqlite3`에 권한 `0600`의 일관된 백업을 만들었다. 운영 DB는 schema v5로 migration한 뒤 당시 구성된 계정은 모두 활성화돼 있었고 integrity/FK를 확인했다. 사용자가 선택한 `ACCOUNT_USERNAMES` 순서의 첫 번째 계정을 private `ADMIN_USERNAME`으로 설정했으며 실제 ID는 문서·Git에 기록하지 않는다. 동적 터널 주소도 Git 커밋에 고정하지 않고 실행 중에는 `.data/tunnel-url.txt`와 공개 Pages 런타임 설정에만 둔다.

후보정 구현 커밋 `ea1a934`의 Pages 실행 `33780005582`가 성공했다. 공개 `index.html`, JavaScript, CSS, favicon 7개를 로컬 파일과 SHA-256으로 비교해 모두 일치했고, 런타임 설정도 현재 터널을 `online`으로 가리켰다. 같은 외부 edge에서 health 200, 무인증 후보정 401, 외부 Origin 403, Pages Origin CORS 허용을 다시 확인했다.

관리자 콘솔 구현 커밋 `df7203d`도 `main`에 push했다. Pages push 실행 `33829281812`와 현재 터널 런타임 설정 재게시 실행 `33829478729`가 모두 성공했다. 새 관리자 설정으로 로컬 Qwen 서버를 warmup 재시작한 뒤 외부 edge health 200, 무인증 관리자 API 401, 허용하지 않은 Origin 403, Pages Origin의 presence preflight 허용을 확인했다. 공개 자산 7개는 로컬 `web/`과 byte-for-byte 일치했고, 공개 `config.json`은 현재 tunnel origin과 일치하며 만료 전이고 `version/state/apiUrl/publishedAt/expiresAt` 외 필드가 없었다. 관리자 비밀번호를 보거나 운영 DB에 시험 세션을 주입하지 않았으므로 실제 관리자 로그인 뒤 dialog 조작은 사용자 브라우저 확인 범위로 남긴다.

2026-09-04 계정 추가 전 `.data/backups/pre-account-add-20260904.sqlite3`와 `.env` 사본을 각각 `0600`으로 만들었다. 계정 수를 2~10개로 일반화하되 일반 시작의 exact-set 검사는 유지하고, echo-disabled `add-account`만 기존 DB와 private env를 한 계정씩 늘리도록 했다. 운영 목록에는 세 계정이 있고 기존 두 계정은 활성 상태, 새 계정은 7일짜리 초대가 있는 비활성 상태다. 기존 두 사용자 행과 다른 모든 DB 테이블이 백업과 같고, 첫 번째 관리자 지정·목록 순서·환경설정·integrity/FK가 보존됐음을 실제 값 출력 없이 확인했다. 새 초대 링크 하나는 권한 `0600`의 `.data/invitations.txt`에만 있으며 API 주소 매개변수를 포함하지 않는다. Qwen warmup 서버와 Quick Tunnel을 다시 연결한 뒤 local/external health 200, 무인증 관리자 401, 잘못된 Origin 403, Pages CORS와 현재 런타임 설정 일치를 확인했다.

같은 날 사용자의 요청으로 세 번째 구성 계정의 초대를 다시 발급했다. 변경 전 DB는 `.data/backups/pre-account3-invite-reset-20260904.sqlite3`에 보존했고, 해당 계정의 자격정보·세션·설정 상태만 초기화했으며 기존 수업은 보존했다. 새 일회용 링크는 권한 `0600`의 `.data/invitations.txt`에만 있고 이전 링크와 비밀번호는 무효다. 이 작업에서는 실행 중 서버와 터널을 중단하거나 재시작하지 않았다.

2026-09-04 실시간 영속 대기열·입력 재연결·탭 간 잠금·선택형 CLOVA 구현 커밋 `5489afd`를 `main`에 push했고 Pages 실행 `33860357377`이 성공했다. 적용 전 운영 DB는 `.data/backups/pre-durable-queue-clova-deploy-20260904.sqlite3`에 권한 `0600`으로 백업했다. 최종 Python 회귀 152개와 Node 회귀 130개, Python/JavaScript/Bash 구문 및 `git diff --check`를 통과했다. API 서버를 최종 코드로 Qwen warmup 재시작했고 DB schema v6, integrity, FK, 대기 chunk/import/correction 0건을 읽기 전용으로 확인했다. 공개 자산 9개는 로컬 `web/`과 byte-for-byte 일치하고, 공개 `config.json`은 허용된 다섯 필드만 가지며 현재 tunnel origin과 일치하고 만료 전이었다. 외부 edge에서 health 200, 무인증 수업 API 401, 허용하지 않은 Origin 403, Pages Origin의 authorization 포함 preflight 허용을 확인했다. 이 프로젝트를 같은 loopback API에 노출하던 이전 고아 cloudflared 하나는 정확한 PID·cwd·고정 target을 확인한 뒤 SIGTERM으로 종료했고, 현재 관리되는 터널 하나와 외부 health 200을 다시 확인했다. CLOVA 비밀 키는 아직 운영 설정에 없으므로 화면에서 해당 선택은 비활성이고 실계정 gRPC 호출은 검증하지 않았다.

2026-09-05 새 마이크 수업에서 CLOVA를 우선하는 `d2505ec`와 경계 continuity/reconciliation 보완 `93ebcfa`를 `main`에 push했다. Pages push 실행 `33891354210`과 현재 Quick Tunnel 주소를 게시한 workflow dispatch 실행 `33891506118`이 모두 같은 `93ebcfa`에서 성공했다. 최신 코드로 API 서버를 Qwen fallback warmup과 함께 재시작했고 Quick Tunnel을 연결했다. 로컬/외부 health 200, 무인증 `/status` 401, 허용하지 않은 Origin 403, Pages runtime config의 현재 tunnel origin·online lease 일치와 공개 정적 파일 9개의 byte-for-byte 일치를 확인했다. 실제 Basic/KSS 강제 rotation 결과는 ignored `.data/test-results/`에 권한 `0600`으로만 두었고, 비밀 키·계정 ID·음성은 추적 파일에 추가하지 않았다. 위 2026-09-04 문단의 CLOVA 미설정·실계정 미검증 내용은 당시 배포 상태를 기록한 것이며 현재 상태를 뜻하지 않는다.

같은 날 최종 사용자에게 비용이 청구되는 것처럼 보이던 CLOVA 문구를 `cfa8e79`에서 제거하고, 사이트 운영자가 관리하는 NAVER Cloud 계정의 CLOVA Speech 도메인과 운영자가 연결한 Object Storage라는 설명으로 바꿨다. 운영자용 README의 요금 정보에는 앱 사용자가 아니라 운영자 계정에 청구된다고 명시했다. Pages 실행 `33935173634`가 성공했고 배포본 `index.html`·`app.js`에서 최종 사용자 과금 문구 없음과 운영자 계정 고지를 직접 확인했다. 공개 파일 9개 일치, runtime config 일치, local/external health 200, 무인증 `/status` 401, 잘못된 Origin 403을 재확인했으며 API도 같은 커밋으로 재시작했다.

푸시 전에 반드시 확인할 것:

- 평탄화 잔여 루트 파일이 staging에 없음
- `server/.env`, `.data/`, `.models/`, `.samples/`, 지원 미디어 확장자(WAV/M4A/MP3/FLAC/OGG/OGA/Opus/WebM/MP4/MKV/MOV/AIFF/APE/ASF/WMA/AU), 로그/PID가 staging에 없음
- workflow가 `web/` 복사본과 생성된 런타임 `config.json`만 담은 격리 staging을 Pages 아티팩트로 업로드함
- Git의 `web/config.json`에 Quick Tunnel URL, 계정, 초대 코드가 없음. Actions가 만든 공개 산출물에는 현재 Quick Tunnel origin과 상태·시각만 있음
- Pages source가 GitHub Actions이고 배포 URL에서 정적 자산과 마이크 UI가 정상임

2026-09-05 Google Drive archive 적용 전 운영 DB를 `.data/backups/pre-drive-user-folders-20260905.sqlite3`에 mode `0600`으로 백업했다. 실제 개인 Gmail Desktop OAuth와 첫 pilot WAV 수동 재생 확인 뒤, 기존 루트 pilot을 재업로드 없이 소유자 폴더로 이동하고 나머지 9개를 업로드했다. 최종 상태는 schema v9, Drive ready 10개, 사용자 폴더 3개, organization/attention/retry/deleting 0, 로컬 WAV 0개이다. 원격 10개의 object·크기·checksum·정확한 parent를 전수 재검증해 mismatch 0, DB integrity ok/FK 오류 0을 확인했다. 재시작 과정에서 PID 파일이 없는 이 프로젝트의 예전 Uvicorn을 발견해 정확한 cwd/argv와 DB의 pending chunk·import·correction·deleting 건수가 모두 0임을 확인한 뒤 SIGTERM으로 정상 종료했다. 이후 PID 파일이 없어도 `/proc`에서 같은 project Uvicorn을 탐지해 auth/migrate를 차단하도록 보강했고, 최신 API를 Qwen warmup으로 재시작해 managed PID·로컬 health 200·기존 Quick Tunnel 외부 health 200을 확인했다. 이 변경분은 아직 커밋·푸시·Pages 배포하지 않았다.

### Google Drive 릴리스 사전 검증 (2026-09-05)

- Python 전체 271개(37.096초), Node 전체 136개(1.796초)가 통과했다. 최초 Drive 바인딩 전 pristine pending 수업의 삭제 영구 대기를 수정하고, 업로드 claim·응답 유실·원격 locator 흔적을 보존하는 회귀 3개를 추가했다.
- 기존 원격 파일 10개의 크기·MD5·SHA-256 appProperty·object·사용자 parent를 다시 확인했다. 표본 1개(255,446바이트)는 실제 전체 다운로드 SHA-256, 두 HTTP Range의 재조립 SHA-256, 조기 연결 종료 후 재다운로드가 모두 통과했다. 녹음을 출력·수정·삭제하지 않았다. 이는 서버의 Drive storage 계층 실통신 검증이며, 실제 로그인 브라우저에서 API 티켓을 받아 다운로드·삭제하는 end-to-end 시험과는 다르다.
- 운영 DB schema v9, integrity ok/FK 오류 0, ready 10개/사용자 폴더 3개, pending chunk/import/correction/deleting/미완료 수업 0, 로컬 WAV 0개를 읽기 전용으로 확인했다. 배포 직전 `.data/backups/pre-drive-release-20260905.sqlite3`에 일관된 private DB 백업을 추가했다.
- 정확도·실시간성 검토 결과는 [ASR_REVIEW.md](ASR_REVIEW.md)에 있다. CLOVA 전송 전 실패의 안전한 자동 복구, 전문용어 사전, IndexedDB 정리 전 결과 표시, Qwen 경계 보강과 CLOVA 전송·확정 분리를 제안했다. ASR 동작은 이번 배포에서 바꾸지 않았고 새 유료 ASR 호출·실음성 정확도 평가를 하지 않았다.

### Google Drive 배포와 재시작 결과 (2026-09-05)

구현 커밋 `aaffb4e`와 Pages 실행 `33950879705`가 성공했고, 배포된 자산 9개와 현재 API 연결·접근 차단을 위와 같이 확인했다. API는 대기·처리 중 작업과 미완료 수업이 모두 0인 상태에서 정상 SIGTERM 종료 후 Qwen warmup으로 재시작했다. 직후 TCP TIME_WAIT 때문에 기존 포트 사전검사가 잘못 실패하는 현상을 확인했다. 실제 listener가 없음을 확인하고 서버를 다시 켠 뒤, uvicorn과 동일하게 probe에 `SO_REUSEADDR`를 설정했다. 임시 포트 회귀에서 기존 probe 실패·수정 probe 성공과 활성 listener 거절을 모두 확인했다. 운영 서버를 반복 재시작해 시험하지는 않았다. SQLite schema v9/integrity ok/FK 0, Drive ready 10/사용자 폴더 3/로컬 WAV 0 상태는 유지됐다.

## 다음 단계 우선순위

1. 서버 코드를 적용한 뒤 로컬/외부 API Range 다운로드, 소유권 격리, Drive trash를 실계정 end-to-end로 확인한다. token 만료·철회·용량 부족에서 로컬 staging을 보존하는 것도 별도로 확인한다.
2. 사용자가 개인 Drive의 `STT 수업 녹음/<계정 ID>` 3개 폴더 구조와 표본 WAV를 필요한 범위에서 다시 열어 확인한다.
3. 실계정 CLOVA에서 자연 경과 4분 이상 스트림, 완전 무음 ACK, pause/resume과 실제 단절·재연결을 비개인 샘플로 확인한다.
4. 같은 실제 한국어 수업 샘플로 CLOVA와 Qwen의 누락·고유명사·경계 중복 및 비용을 비교한다.
5. 실제 관리자 계정으로 외부 dialog를 열어 상태 갱신·다른 계정 세션 해제·운영 중지/재개를 확인한다.
6. 구성된 각 계정으로 실제 외부 로그인, 마이크 WAV와 녹음 파일 업로드, 기록 조회·텍스트 다운로드와 계정 간 격리를 확인한다.
7. 데스크톱 Chrome/Edge에서 유튜브 탭 오디오를 공유해 영상 미전송, 종료 tail, 일시 mute를 실제 확인한다.
8. 학교 Wi-Fi/태블릿에서 연결 중단·복구, 종료 tail, 화면 잠금까지 실제 운용 시험을 한다.

## 재현 명령

```bash
# 서버/API 단위·통합 테스트
./.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v

# 웹 테스트(Node.js 20+)
node --test tests/*.test.mjs

# Qwen 짧은 한국어 직접 벤치마크
./.venv/bin/python scripts/benchmark_qwen.py --limit 10

# 실제 pause-aware 8~15초, 3초 overlap 최종 청크 경로
./.venv/bin/python scripts/validate_chunk_pipeline.py --limit 10 --warmup

# Qwen 60분 soak 결과를 재현할 때
./.venv/bin/python scripts/validate_chunk_pipeline.py --limit 10 --minimum-minutes 60 --quiet-chunks
```

## 보안상 절대 커밋하지 않을 것

```text
server/.env
.data/                       # DB, 초대 원문, PID, 로그, tunnel URL
.data/google-drive/          # OAuth client/token, identity key, upload session/lock
.models/
.samples/                    # 실제/평가 음성
*.wav *.webm *.m4a *.mp3 *.flac *.ogg *.oga *.opus *.mp4 *.mkv *.mov
*.aif *.aiff *.ape *.asf *.wma *.au
GitHub token, 비밀번호, 초대 링크
Mindlogic/NOVA API key, provider 요청·응답 원문
Google OAuth client secret, refresh/access token, Drive file ID·resumable URL
```

`.gitignore`는 사고를 줄이는 장치일 뿐이다. push 직전 staging 목록을 사람이 다시 확인해야 한다.
