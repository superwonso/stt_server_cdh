# 개발 인수인계

기준일: 2026-09-04 (Asia/Seoul)

## 현재 결론

현재 기본 엔진은 `Qwen/Qwen3-ASR-1.7B`의 Transformers 경로(`qwen-asr==0.0.6`, BF16, SDPA)와 `Qwen3-ForcedAligner-0.6B`다. 브라우저 마이크와 `getDisplayMedia`의 오디오 트랙은 16 kHz 모노 PCM을 모아 새 음성이 8초 이상이고 최근 0.24초가 조용하면 자르며, 조용한 지점이 없으면 전체 WAV 15초에서 자른다. 첫 조각 뒤에는 이전 3초를 겹쳐 보낸다. 업로드 파일도 PyAV로 스트리밍 디코딩한 뒤 같은 분할 규칙을 쓴다. 서버는 0.6초 stability guard와 단어 정렬 시각으로 각 조각에서 확정할 범위를 정한다. Qwen의 vLLM 네이티브 스트리밍은 현재 코드에서 사용하지 않는다.

정확도 보조 기능으로 완료된 원문을 충북대 NOVA Gateway의 `solar-pro4`에 보내는 명시적 opt-in 후보정을 추가했다. Qwen 모델은 바꾸지 않았고 음성은 후보정 제공자에게 보내지 않는다. 원문과 후보정본은 분리 저장하며 화면은 원문을 기본으로 연다. 문장 ID·순서·개수, 기존 숫자, 개인정보 임시 표식과 응답 크기가 맞지 않으면 후보정본을 저장하지 않고, 새 숫자 표기가 생기면 원문 비교 경고를 붙인다. 따라서 이 기능은 원문을 대체하는 정답 생성기가 아니라 검토 가능한 별도 초안이다.

이 선택은 “Qwen이 언제나 가장 정확하다”는 결론이 아니다. 대상 Radeon/ROCm에서 실제 한국어 음성을 실시간보다 빠르게 처리했고, 현재 3초 겹침 경로가 짧은 낭독 시험에서 CER 4.03%, 마지막 기준 25자 완전 일치, 탐지된 텍스트 중복 0건을 낸 점과 요청 단위 복구가 단순하다는 운영상 이유로 정한 보수적 기본값이다. 서로 독립적으로 인식한 청크의 정렬 시각은 흔들릴 수 있으므로 실제 수업에서 exactly-once를 수학적으로 보장하지 않는다.

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
| PyTorch | 2.10.0 ROCm 7.2.4 빌드 | `/home/wonso/miniconda3/envs/torch-rocm/bin/python` |

샌드박스 내부에서는 GPU 장치가 가려질 수 있었기 때문에 실제 GPU 확인과 벤치마크는 WSL 호스트 권한이 보이는 셸에서 수행했다.

## 모델 비교

서로 다른 데이터셋·오디오 조건·오류 지표를 모델 간 절대 순위처럼 비교하면 안 된다. 아래 공개 수치는 각 모델의 적합성과 위험을 거르는 자료이고, 최종 선택은 같은 한국어 수업 샘플을 같은 PC에서 돌린 결과로 다시 판단해야 한다.

| 모델 | 공식 자료에서 확인한 내용 | 이 PC와 긴 수업에 대한 판단 | 현재 상태 |
| --- | --- | --- | --- |
| `microsoft/VibeVoice-ASR-Streaming-7B` | 이름은 7B지만 [모델 카드](https://huggingface.co/microsoft/VibeVoice-ASR-Streaming-7B)는 9B BF16로 표시한다. [기술 보고서](https://arxiv.org/html/2609.02812v1)는 2.9초 청크 + 0.5초 lookahead, 예상 화자 귀속 지연 2.00초, 한국어 MLC CER 9.09/cpCER 23.22를 보고한다. 이전 음성·텍스트를 유지하지만 비용이 선형 증가하고 공개 체크포인트는 최대 480초 녹음을 대상으로 한다. [공식 실행 문서](https://github.com/microsoft/VibeVoice/blob/main/docs/vibevoice-asr-streaming.md)는 NVIDIA PyTorch 컨테이너를 검증 환경으로 제시한다. | 이 PC의 공식 `init_streaming_state`→`streaming_generate_step` 경로에서 472.08초를 완주했으나 최근 평균 청크 연산이 2.15→3.25초로 증가해 종료 시 입력 간격을 초과했다. allocated는 16.32→16.58 GiB, reserved는 16.59→22.19 GiB, KV는 1,702→6,574토큰이었다. | **ROCm 실측 완료, 기본값 아님.** 짧은 FLEURS 3개 간이 aggregate CER 25.2%; 별도의 14회 반복 7:52 실험은 custom content CER 15.75%. 두 값은 음성 구성과 정규화가 달라 서로 직접 비교할 수 없다. |
| `mistralai/Voxtral-Mini-4B-Realtime-2602` | [공식 모델 카드](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602)는 4B BF16, 16GB 이상 GPU, sliding-window attention, 기본 131072 context(3시간 이상)를 설명한다. Korean FLEURS 공식값은 WER로 480ms 지연 15.74, 2400ms 지연 14.30이다. [기술 보고서](https://arxiv.org/html/2602.11298v3)도 긴 연속 스트림을 주목적으로 설명한다. | 공식 2400ms stream을 호환성 shim이 있는 격리 환경에서 실행했다. 111.12초 음성 추론이 176.02초(RTF 1.584)여서 현재 경로는 입력을 따라가지 못한다. 종료 시 3.28초 right padding이 필수였다. | **ROCm 실측 완료, 기본값 아님.** CER 4.70%, WER 21.53%; GPU allocated 약 8.94GB, peak 약 9.18GB. |
| `Qwen/Qwen3-ASR-1.7B` | [공식 모델 카드](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)와 [저장소](https://github.com/QwenLM/Qwen3-ASR)는 한국어를 포함한 30개 언어, 오프라인·스트리밍 모드를 명시한다. 패키지의 스트리밍은 현재 vLLM backend에서만 제공되고, Transformers 경로는 확정 결과와 forced alignment를 제공한다. | 모델이 작고 현재 ROCm PyTorch/SDPA에서 실제 작동했다. HTTP 청크는 네이티브 스트리밍보다 느리게 확정되지만, 연결 재시도와 DB idempotency가 단순하다. 3초 겹침이 경계 손실을 완화하나 독립 ASR/정렬의 jitter는 남는다. | **현재 기본값.** 전체 파일 CER 2.24%/RTF 0.592, 실제 짧은 청크 경로 CER 4.03%/RTF 0.466. 60분 35초 연속 청크 경로도 CER 3.51%/RTF 0.442로 완주했다. |
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
- 작업 상태는 `queued/processing/completed/failed`이며 별도 `transcript_corrections` 행에 저장한다. 단일 background worker와 attempt/revision/status CAS가 늦게 끝난 결과를 버리고, 재시작 때 `processing`을 안전하게 다시 `queued`로 돌린다.
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

Whisper는 빠르고 전체 파일 기준선도 양호했지만, 현재 수업처럼 짧은 독립 WAV를 시간으로 나눠 확정할 때의 누락이 Qwen보다 컸다. 따라서 현재 기본값으로 되돌리지 않았다.

## 구현 현황

### 서버

- FastAPI는 `127.0.0.1`에만 bind하며 외부 요청은 Cloudflare 터널로만 받는다.
- CORS 및 별도 origin middleware가 `SITE_ORIGINS`의 정확한 출처만 허용한다.
- 실제 계정 ID 두 개는 ignored `server/.env`의 `ACCOUNT_USERNAMES`와 로컬 DB에만 존재한다. 빈 설정이나 기존 DB와의 불일치는 값을 반사하지 않는 오류로 fail closed 한다.
- body-size middleware가 `Content-Length` 유무와 chunked 전송 모두에서 과대한 JSON/WAV/파일 조각 요청을 parsing 전에 차단한다.
- 계정 ID 두 개는 Git에서 제외된 `server/.env`의 `ACCOUNT_USERNAMES`로만 설정하며 앱에서도 그 목록으로 제한한다.
- 초대 코드는 7일/1회용이고 원문은 `.data/invitations.txt`에만 쓴다. 초대 URL에는 API 주소를 넣지 않으며, 활성화 후 DB의 setup hash도 제거된다.
- 새 비밀번호 정책은 사용자 요청에 따라 4~128자이며 Argon2로 저장한다. 세션·초대 코드는 SHA-256 digest로 저장하고 인증 오류 응답에 비밀번호나 초대 값을 반사하지 않는다.
- 로그인/초기 설정 시 IP별 30회/5분, IP+계정별 10회/5분과 별도로 모든 주소를 합산한 계정별 50회/30분 제한을 적용한다.
- 모든 lecture·실시간 chunk·파일 import 조회/변경은 인증 사용자의 소유권을 검사한다.
- 수업 생성은 브라우저가 보낸 UUID와 소유자·제목·언어를 확인해 응답 손실 뒤 재요청도 idempotent하게 처리한다.
- 한 모델과 inference lock을 사용하며 running+waiting 요청은 기본 2개로 제한한다.
- WAV는 최대 512,000 bytes, 0.05~15초, 16 kHz mono PCM16만 받는다. VAD가 침묵 hallucination 저장을 줄인다.
- 새로 처리한 실시간·파일 import 음성은 overlap을 제거한 16 kHz mono PCM WAV로 `.data/recordings/<private-account>/<lecture UUID>.wav`에 보존한다. 계정 폴더는 `0700`, 파일은 `0600`이며 과거에 이미 버린 음성은 소급 복구하지 않는다.
- WAV 저장은 시간축에 이미 있는 PCM과 byte 단위로 대조해 같은 요청/응답 유실 재시도가 파일을 늘리지 않게 한다. 제한된 건너뛴 구간은 무음으로 채우고, 4시간·전체 20 GiB·최소 여유 1 GiB 한도를 적용한다.
- 녹음 파일은 `POST /imports` → 고정 480 KiB `PUT /imports/{id}` → `POST .../complete` 계약으로 계정별 `0700` 폴더의 UUID 파일(`0600`)에 올린다. 최대 1 GiB, 디코딩 음성 4시간, 계정당 활성 작업 1개다.
- 브라우저와 서버는 모든 480 KiB 조각의 SHA-256을 순서대로 묶은 v2 지문으로 재선택 파일 전체를 검증한다. 브라우저/서버 모두 전체 파일을 메모리에 펼치지 않는다.
- PyAV 18.1.0이 첫 오디오 스트림만 16 kHz mono s16으로 스트리밍 디코딩한다. nested URL/playlist I/O, 비정상 채널·sample rate·frame duration을 거절하고 디코딩 뒤 최대 15초 PCM만 유지한다.
- PyAV/FFmpeg 네이티브 디코더는 API 프로세스 안에서 실행된다. 두 명의 인증된 사용자가 신뢰할 수 있는 수업 파일만 올린다는 운영 가정이며, 악의적으로 만든 미디어의 네이티브 hang/crash까지 OS sandbox로 격리한 구조는 아니다.
- 결정적 import chunk UUID와 기존 chunk idempotency로 정상 종료 뒤 재시작 시 완료 청크를 재사용한다. 단일 background worker는 loop 예외를 재시도하고 GET/list가 죽은 worker를 다시 확인한다.
- 완료·취소·실패 DB 상태와 별도로 `raw_deleted`를 기록한다. unlink 실패를 삭제 성공으로 표시하지 않으며 60초 maintenance/list 조회에서 재시도한다. 7일간 멈춘 업로드는 서버를 재시작하지 않아도 정리한다.
- 녹음 다운로드는 소유권·완료 상태를 확인한 인증 POST가 60초짜리 무작위 전용 경로를 발급하고, 네이티브 파일 응답은 세션 Bearer를 URL에 넣지 않는다. 짧은 Wi-Fi 중단 뒤 Range 재개를 위해 최대 16회만 재사용하며, 경로는 UUID에서만 계산하고 다운로드 시 열린 `O_NOFOLLOW` descriptor의 WAV 구조와 일반 파일 여부를 다시 검사한다.
- `MindlogicPostprocessor`는 공식 Gateway로만 나가는 bounded HTTP client, strict schema/문장 매핑 검증, 개인정보 형식 가림과 숫자 보호를 담당한다. 후보정 endpoint는 owner/final 상태와 원문 revision을 확인하고 단일 background worker가 별도 correction 행을 처리한다. `/status`에는 key가 아니라 configured/model만 보인다.
- 수업 삭제는 진행 중 import, pending chunk, processing correction이 있으면 `409`로 거절한다. queued correction은 같은 transaction에서 먼저 지운다. 연결된 import metadata를 지우고 durable `deleting` 상태를 먼저 기록한 뒤 WAV와 lecture cascade 데이터를 제거하며, 중간 파일 삭제 실패는 성공으로 표시하지 않고 재시작 때 다시 처리한다.
- `MODEL_WARMUP=1`이면 lifespan 중 모델을 로드하고 첫 경로를 실행한다.

### 웹

- GitHub Pages에는 `web/`에서 복사한 공개 파일과 Actions가 생성한 런타임 `config.json`만 격리 staging을 거쳐 배포하며, 외부 스크립트·폰트·분석 코드를 쓰지 않는다.
- 클라이언트에는 계정 allow-list가 없다. 초대 링크의 opaque `username`/`setup_code`만 폼에 복원한 직후 URL fragment에서 지운다. 초대 링크의 `api=` 값은 무시한다.
- 서버 주소 변경 폼은 후보 주소의 anonymous health를 먼저 확인하고, 성공하기 전에는 기존 API 주소와 token을 섞어 쓰지 않는다. 로그인 요청 중에는 변경을 막고, 늦은 인증 응답은 요청 당시 origin/generation이 달라졌으면 폐기한다.
- 로그인 token은 JS 메모리에만 있고 API origin만 localStorage에 저장한다.
- Quick Tunnel이 준비되면 운영 스크립트가 `version/state/apiUrl/publishedAt/expiresAt`으로 된 정확한 공개 JSON 전체를 GitHub Actions 저장소 변수에 원자적으로 넣고 Pages를 다시 배포한다. Git의 `web/config.json`은 비어 있으며 ID·초대 코드·비밀번호·token·수업 정보는 게시기가 읽지 않는다.
- 웹은 로그인 전에 같은 Pages 출처의 런타임 설정을 엄격히 검사하고 Bearer 없는 `/health`를 통과한 origin만 설치한다. 새 유효 설정은 stale localStorage보다 우선하며, offline·만료·잘못된 설정은 fail closed 한다. 수동 입력은 자동 게시 장애 복구용으로만 남긴다.
- GitHub Pages 프로젝트들은 `https://superwonso.github.io` 출처를 공유하므로 다른 Pages 저장소까지 신뢰해야 하는 구조적 한계가 있다. 전용 Pages 계정/도메인 없이는 완전 격리할 수 없다.
- AudioWorklet → 스트리밍 resampler → 16 kHz PCM16 WAV 경로다.
- 입력 소스는 마이크와 `getDisplayMedia`의 공유 오디오다. 브라우저가 요구하는 video track은 종료 감지만 하고 오디오 그래프·WAV·네트워크에 연결하지 않는다. 화면/시스템 오디오는 지원 여부가 브라우저·OS·선택 표면·DRM에 달려 있으며 알림/다른 앱 소리 포함 위험을 UI에 표시한다.
- 새 음성 8초 이후 조용한 0.24초 지점을 찾고 전체 WAV 최대 15초, 이전 3초 overlap으로 자른다. stop 직후에는 overlap만 남아도 final guard 조각을 보내 마지막 확정을 요청한다.
- 일시적 네트워크/429/503과 idempotent 처리 중 409는 동일 chunk UUID로 지수 backoff하며 최대 8회, 큐 순서대로 재시도한다.
- 8회 실패나 영구 오류에는 자동 loop와 녹음을 멈추고 tail을 포함한 WAV 메모리 큐를 남긴다. 사용자는 다시 보내거나 첫 실패 WAV의 다운로드를 요청하고 실제 저장을 별도로 확인한 뒤 그 조각을 건너뛸 수 있다. 건너뛴 구간은 기록에서 빠진다.
- Quick Tunnel 주소가 바뀌어도 실제 녹음/전송 중이 아니면 후보 주소를 익명 확인할 수 있다. 대기 WAV·UUID·기존 사용자를 유지하고 같은 계정의 재로그인만 허용한 뒤 새 origin으로 전송을 잇는다.
- 최대 정상 큐는 8개이며 final tail은 보존을 위해 한 개 더 들어갈 수 있다. 새로고침·탭 종료 전까지의 메모리 보존일 뿐 영구 저장은 아니다.
- 서버 응답 segment ID도 다시 검사해 같은 응답 렌더링을 중복 append하지 않는다.
- `RecordingFileUploader`는 파일을 480 KiB씩 지문 확인·해시·순차 업로드하고 서버 offset으로 재개한다. 업로드 중 탭을 닫으면 같은 전체 지문의 파일 재선택이 필요하지만 queued/processing은 파일 참조 없이 polling을 복구한다.
- import polling delay의 abort listener는 매 회 제거한다. 로그아웃/탭 이탈은 watcher만 detach하고 서버 작업을 취소하지 않으며, 취소/완료 경쟁과 오래된 lecture/list 응답은 terminal 상태·generation/sequence로 판별한다.
- 지난 수업을 `Asia/Seoul` 날짜로 묶고 단일 날짜를 필터링한다. 상세 기록은 UTF-8 TXT 또는 Markdown으로 내보내며 Markdown 제어문자와 파일명 문자를 정리한다.
- 녹음 WAV 버튼은 `recording_available`인 수업에만 열린다. 미확정 WAV는 owner-only idempotent finalize POST로 받은 범위까지 먼저 닫고, 로그인 Bearer로 ticket을 받은 뒤 같은 API origin의 상대 경로만 세션 토큰 없이 브라우저 네이티브 다운로드로 연다.
- 후보정 버튼은 final transcript에서만 열고 `queued/processing`을 2.5초마다 확인한다. 원문/AI 후보정본을 명시적으로 전환하고 현재 선택한 버전만 TXT/Markdown으로 내보낸다. 완료 뒤에도 원문이 기본이며 `uncertain_terms`를 최대 5개와 나머지 개수로 표시한다. 수업·계정 전환은 poll timer와 늦은 결과를 generation/sequence로 폐기한다.
- 후보정 패널은 텍스트만 NOVA로 가고 오디오는 가지 않으며 형식 기반 가림이 이름 등을 보호하지 못한다는 opt-in 안내를 항상 표시한다. 결과는 모두 `textContent`로 렌더링한다.
- 수업 삭제 확인창은 제목과 원문·후보정본·녹음 삭제 범위를 보여 준다. 삭제·다운로드·녹음·파일 변환 중 동작을 상호 잠그고 generation/sequence로 늦게 도착한 이전 수업·계정 응답을 버린다.

### 운영 스크립트

- `scripts/setup.sh`: ROCm Python을 system-site-packages로 재사용하는 venv, Qwen 모델, cloudflared 설치/검증
- `scripts/start-server.sh`: import-time 운영 DB 접근을 피하는 Uvicorn factory, 단일 worker, loopback bind, 기본 warmup, PID·로그 관리
- `scripts/start-tunnel.sh`: 서버 health 확인 후 Quick Tunnel 생성, URL 파일 기록, GitHub Pages 런타임 주소 게시와 배포 확인. 게시 실패 시 health-checked 터널은 남겨 재게시나 수동 복구가 가능하지만 명령은 성공으로 표시하지 않음
- `scripts/publish-api-url.sh`와 `scripts/runtime_config.py`: 공개 tunnel origin 또는 `OFFLINE` 입력을 정확한 상태·주소·게시/만료 시각 JSON으로 만들고, 로그인된 GitHub CLI로 그 JSON 전체를 Actions 변수에 저장해 Pages workflow를 실행한다. URL·스키마·만료를 엄격히 검사하며 Git 커밋에는 동적 주소를 쓰지 않음
- `scripts/status.sh`: PID 소유권, 로컬 health, 실행 중인 tunnel URL과 외부 HTTPS health 표시. 죽은 터널의 마지막 URL은 현재 주소와 구분
- `scripts/stop.sh`: tunnel 먼저, 서버 다음으로 SIGTERM 종료; PID가 다른 프로세스를 가리키면 건드리지 않음
- `scripts/backup.sh`: SQLite online backup으로 WAL의 최신 커밋까지 일관된 스냅샷 생성; 기존 대상은 덮어쓰지 않음. 보존 WAV는 포함하지 않으므로 음성 백업은 서버를 끈 상태에서 `.data/recordings/`를 같은 암호화 저장소에 별도로 복사해야 함
- `server/manage.py`: private `ACCOUNT_USERNAMES`의 두 미활성 계정 invite 생성과 `SITE_ORIGINS` 갱신. 서버 주소와 초대 링크를 같은 로컬 파일에 쓰되 링크 자체나 서버 설정에는 임시 API 주소를 결합하지 않음

## 검증 범위

### 확인됨

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
- 코드 검토로 확인한 방어선: ignored 환경설정 기반 서버 측 계정 allow-list·DB exact-set 검사·소유권 검사, exact-origin 제한, chunked body 제한, 계정별 녹음 권한·UUID 경로·WAV 검증, import 원본 권한/삭제 상태, 동일 lecture/chunk/import UUID의 idempotent 응답, 초대 링크의 API 주소 배제, 계정 전체 주소 합산 로그인 제한
- 최종 회귀 실행: Python 서버/API/DB/설정/전사기/importer/녹음 저장·후보정·Pages 런타임 설정 80개 테스트와 Node 웹 테스트 4개 파일 모두 통과. 실제 ID/키 비공개 설정 검증, legacy DB 계정 제약 제거와 텍스트 전용 수업 완료 승격, 행·FK 보존, 설정 불일치 무변경 거절, 3자 비밀번호 거절·4자 허용, 파일 전체 지문, offset 재개, 소유권, raw 삭제 실패·재시도, 7일 정리, 취소/완료 경쟁, 로그인·로그아웃·계정·수업 전환의 오래된 UI 응답, system-audio track 분리도 포함한다.
- 녹음 전용 시험은 overlap PCM 제거, byte-identical retry, bounded silence gap, quota 실패 시 기존 파일 불변, 부분 write rollback, symlink 거부/안전 삭제, 전송 중단 fd close, 60초 ticket Range 재개·만료, 명시적 final 복구, 다른 계정 불변, DELETE/inference 경합과 응답 유실 idempotency를 포함한다.
- 웹 시험은 KST 자정 경계 날짜 그룹, TXT/Markdown 내용·이스케이프·안전 파일명, 동일 API origin ticket 제한, 마지막 실패 조각 확정과 WAV 없음의 정확한 안내, 507 수동 복구, 다운로드·삭제·계정 전환의 stale 응답 방어를 포함한다. 자동 주소의 익명 health 선행, stale 저장 주소 차단, 24시간 lease 만료 뒤 Bearer·비밀번호·음성 본문 차단, 새 tunnel에서 같은 계정 재로그인 후 큐 재개도 검증했다. Python source `py_compile`, JavaScript `--check`, `git diff --check`도 통과했다.
- GitHub Actions Pages 배포 성공 후 공개 URL의 `index.html`, `app.js`, `audio.js`, `file-import.js`, `pcm-worklet.js`, `style.css`가 로컬 배포본과 byte-for-byte 일치하고 HTTPS 200임을 확인했다.
- 실제 Quick Tunnel edge를 통해 `/health` 200, Pages Origin의 CORS 헤더와 새 파일 조각 `PUT` preflight를 확인했고, 허용하지 않은 Origin은 서버 경계에서 403으로 거절됐다. cloudflared의 DNS·UDP/QUIC·TCP·Cloudflare API 사전 점검도 모두 PASS였다.

### 아직 확인하지 못함

- 사용자가 실제로 들을 45~90분 한국어 수업 샘플의 인식 품질
- 교실 거리·잔향·잡음·여러 화자·전문용어 조건
- 90분 연속 실행 중 thermal throttling과 WSL 공유 메모리 회수
- 실제 학교 Wi-Fi와 태블릿을 사용한 Quick Tunnel 장기 연결 중단/복구
- 실사용 종료 버튼, 마이크 강제 중단, 화면 잠금 각각에서 마지막 음절·문장 보존
- 실제 데스크톱 Chrome/Edge에서 유튜브·강의 플랫폼 탭 오디오 선택, 알림 포함 범위, DRM 차단, 5초 mute 복구, 공유 종료 tail
- 실제 브라우저→Quick Tunnel로 큰 녹음 파일 업로드/중단/재선택/백그라운드 변환. 로컬 격리 API의 실제 Qwen E2E 한 파일은 통과했지만 활성 계정 외부 E2E는 아직이다.
- MP3/FLAC/OGG/Opus/WebM/MP4/MKV/MOV 각 실파일의 디코딩 호환성과 실제 압축 강의 음질
- 새 UUID로 동일 오디오가 다시 생성되는 앱/브라우저 수준의 의미적 중복
- 실제 두 계정의 외부 활성화·로그인·기록 격리 end-to-end
- Quick Tunnel을 통한 실제 활성 계정 로그인, 실시간 WAV/녹음 파일 업로드, 기록 조회·다운로드. 외부 `/health`와 CORS까지만 확인했다.
- 실제 브라우저와 Quick Tunnel에서 새 녹음 WAV 다운로드를 중단·재개하거나, 다운로드 도중 같은 수업을 삭제하는 동작
- 기능 추가 전에 만든 과거 수업에는 원본 음성이 남아 있지 않으므로 녹음 다운로드를 제공할 수 없음
- 두 사용자 본인이 일회용 링크를 열어 비밀번호를 설정하는 단계
- VibeVoice를 8분보다 긴 하나의 state 또는 실제 45~90분 수업으로 운용했을 때의 품질·메모리·처리량. 이번 실험은 공식 목표에 가까운 7:52에서 끝났고 반복 낭독 음성을 사용했다.
- 실제 5~10분 이상 한국어 교실 원문에서 `solar-pro4` 후보정 전후 CER, 고유명사·전문용어·수식 보존, 의미 왜곡, 처리 시간과 credits 사용량. 현재 실제 Gateway 검증은 비개인 합성 텍스트 3문장뿐이다.
- NOVA/Mindlogic proxy 자체의 요청 보관·삭제 세부 정책과 외부 요청의 idempotency key 지원. 확인 전에는 완전한 비보관·비학습이나 exactly-once 과금이라고 주장하지 않는다.

실제 수업 음성 샘플이 제공되면 원본은 `.samples/`에만 두고 다음 순서로 검증한다.

1. 5~10분 익명화 구간으로 Qwen/Vibe/Voxtral/Whisper의 누락·고유명사·문장 경계를 수동 비교한다.
2. 같은 파일을 60~90분 길이로 반복 또는 연속 구성해 RSS, `torch.cuda.memory_allocated/reserved`, RTF를 청크마다 기록한다.
3. 같은 기준문으로 Qwen 원문과 Solar 후보정본의 CER 및 숫자·수식·고유명사 의미 왜곡을 비교한다.
4. 중간에 tunnel을 끊고 복구해 DB chunk 수, segment 순서, 중복 텍스트를 확인한다.
5. 녹음 종료·마이크 강제 중단·화면 잠금으로 final tail을 각각 검증한다.
6. 두 계정으로 같은 lecture UUID 접근을 시도해 404와 기록 분리를 재확인한다.

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

2026-09-04 새 후보정 코드로 로컬 Qwen API를 재시작해 warmup·health와 DB v4 migration/integrity/FK를 확인했고, 기존 Quick Tunnel도 외부 HTTPS health가 정상인 상태다. 동적 주소는 Git 커밋에 고정하지 않고 실행 중에는 `.data/tunnel-url.txt`와 공개 Pages 런타임 설정에만 둔다. 두 계정 중 한 계정은 비밀번호 설정을 완료했고, 나머지 한 계정에는 아직 유효한 일회용 초대가 있다. 따라서 두 사용자 설정이 모두 끝났다고 표시하면 안 된다.

푸시 전에 반드시 확인할 것:

- 평탄화 잔여 루트 파일이 staging에 없음
- `server/.env`, `.data/`, `.models/`, `.samples/`, 지원 미디어 확장자(WAV/M4A/MP3/FLAC/OGG/OGA/Opus/WebM/MP4/MKV/MOV/AIFF/APE/ASF/WMA/AU), 로그/PID가 staging에 없음
- workflow가 `web/` 복사본과 생성된 런타임 `config.json`만 담은 격리 staging을 Pages 아티팩트로 업로드함
- Git의 `web/config.json`에 Quick Tunnel URL, 계정, 초대 코드가 없음. Actions가 만든 공개 산출물에는 현재 Quick Tunnel origin과 상태·시각만 있음
- Pages source가 GitHub Actions이고 배포 URL에서 정적 자산과 마이크 UI가 정상임

## 다음 단계 우선순위

1. 새 정적 자산의 Pages 배포와 현재 Quick Tunnel 자동 주소 게시를 확인한다. 아직 미활성인 한 사용자에게 `.data/invitations.txt`의 본인 초대 링크만 전달하고 사용자가 직접 비밀번호를 정해야 두 계정 설정이 완료된다.
2. 두 계정 활성화 뒤 실제 외부 로그인, 마이크 WAV와 녹음 파일 업로드, 기록 조회·텍스트 다운로드와 두 계정 격리를 확인한다.
3. 데스크톱 Chrome/Edge에서 유튜브 탭 오디오를 공유해 영상 미전송, 종료 tail, 일시 mute를 실제 확인한다.
4. 사용자에게 개인정보를 제거한 실제 한국어 수업 음성 5~10분 샘플과 가능하면 교정문을 요청해 Qwen 원문 품질·경계 누락/중복과 Solar 후보정 전후 CER·의미 보존을 함께 검증한다.
5. 학교 Wi-Fi/태블릿에서 연결 중단·복구, 종료 tail, 화면 잠금까지 실제 운용 시험을 한다.

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
.models/
.samples/                    # 실제/평가 음성
*.wav *.webm *.m4a *.mp3 *.flac *.ogg *.oga *.opus *.mp4 *.mkv *.mov
*.aif *.aiff *.ape *.asf *.wma *.au
GitHub token, 비밀번호, 초대 링크
Mindlogic/NOVA API key, provider 요청·응답 원문
```

`.gitignore`는 사고를 줄이는 장치일 뿐이다. push 직전 staging 목록을 사람이 다시 확인해야 한다.
