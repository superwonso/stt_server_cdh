# 열린 탭에서 음성 응급 저장 — 배포·서버 재시작 없음

문제가 생긴 **그 기기·그 브라우저 프로필·원래 탭**에서 진행한다. 서버 PC에서 사이트를 새로 열어서는 다른 기기의 임시 음성을 읽을 수 없다.

원래 탭을 닫거나 새로고침하지 않는다. 로그아웃, 사이트 데이터 삭제, 실패 음성 건너뛰기, 강제 종료도 하지 않는다. 마이크가 실제로 중단된 동안의 소리는 복구할 수 없지만, 중단 전에 저장된 부분은 남아 있을 수 있다.

## 1. IndexedDB에 남은 WAV 조각 받기

1. 원래 서비스 탭에서 개발자 도구를 연다. Windows Chrome/Edge는 `F12` 또는 `Ctrl+Shift+J`를 사용할 수 있다.
2. **Console** 실행 대상이 원래 서비스 페이지인지 확인한다. 다른 사이트·확장 프로그램·iframe에서는 실행하지 않는다.
3. 전달받은 `scripts/emergency-audio-console.js` 파일을 메모장 등 텍스트 편집기로 열어 **전체 내용**을 확인하고 Console에서 실행한다. Windows에서 `.js` 파일 자체를 더블클릭해 실행하는 것이 아니다. 이 파일은 외부 스크립트를 불러오거나 서버에 요청하지 않는다. 붙여넣기가 보안 경고로 차단되면 보호 기능을 끄지 말고 그 화면에서 진행을 멈춘다.
4. 페이지에 표시되는 입력란에 **음성을 녹음한 본인의 계정 ID만** 입력하고 읽기를 시작한다. 비밀번호·API 키는 입력하지 않는다. 이 입력란은 브라우저의 자바스크립트 처리를 멈추는 `prompt()` 창이 아니다.
5. 페이지에 나타난 안내를 확인하고 **ZIP 내려받기** 링크를 직접 클릭한다. ZIP 안에는 수업별로 번호를 붙인 원본 WAV 조각과 `manifest.json`이 있다.
6. ZIP을 압축 해제해서 용량과 WAV 재생을 확인한다. 완료 표시만 보고 원래 탭을 닫지 않는다. 추가로 복구해야 할 RAM 음성이 있을 수 있다.

이 스크립트는 기존 `yeobaek-live-audio` DB를 버전 변경 없이 열고, 해당 owner의 `sessions`·`chunks`·`pcmSnapshots`를 단일 **읽기 전용** 트랜잭션으로 확보한다. DB 생성·변경·삭제, 로그인·재전송·ASR 요청, 녹음 종료를 하지 않는다. 파일을 준비하는 계산 부하까지 없다는 뜻은 아니다.

### 결과를 해석할 때

- **전체 수업 한 파일이 아니다.** 서버가 수신 확인한 앞부분은 브라우저에서 이미 정리됐을 수 있다.
- 원본 보존이 우선이므로 청크 사이 겹침과 snapshot 중복을 제거하지 않는다. 연속 재생하면 같은 말이 반복될 수 있다. 시간 위치·중첩·원천은 `manifest.json`에 기록한다.
- 0바이트 Blob은 소리를 복원할 수 없다. 원본은 ZIP에 보존하되 경고한다. 아예 읽히지 않는 항목은 누락 목록에 남기고 읽을 수 있는 나머지를 제공한다.
- 별도의 구조 페이지와 마찬가지로, 이 스크립트만 실행하면 원래 앱 모듈의 **RAM에만 남은 데이터**에는 접근할 수 없다. 아래 방법으로 RAM을 먼저 고정한 경우에는 그것도 포함한다.
- ZIP에도 사적인 음성과 수업 정보가 들어 있다. GitHub나 공개 게시판에 올리지 않는다.

## 2. RAM에만 남은 음성이 의심될 때 — 고급 절차

이 절차는 배포된 앱의 `pending`, `liveSessions`, `capture`가 일반 전역 변수가 아닌 **ES module 내부 변수**라는 점을 이용한다. Console에서 `window.pending`를 찾거나 `app.js`를 새로 import하지 않는다. 새 import는 앱을 중복 초기화할 수 있다.

1. 원래 탭의 개발자 도구 **Sources**에서 해당 서비스의 `app.js`를 연다.
2. `function updateControls()`를 검색하고, 함수 안 **첫 실행문** 줄 번호를 우클릭해 **Add conditional breakpoint / 조건부 중단점 추가**를 선택한다. 배포본에 따라 줄 번호가 달라지므로 고정 줄 번호를 사용하지 않는다.
3. 아래 식 전체를 조건으로 넣는다. 이 식은 같은 계정의 Blob 참조와 최신 PCM 복사만 별도 변수에 고정하고 마지막에 `false`를 반환한다. 일반 중단점처럼 녹음 처리를 멈춰 놓기 위한 것이 아니다.

```javascript
(() => {
  if (globalThis.__yeobaekEmergencyRam || typeof user !== 'string' || !user) return false;
  const owner = user;
  const chunks = pending.filter(x => x.owner === owner).map(x => ({
    owner, captureId: x.captureId, id: x.id, sequence: x.sequence,
    startSamples: Math.round(x.startSeconds * 16000),
    durationSamples: Math.round(x.durationSeconds * 16000),
    overlapSamples: Math.round(x.overlapSeconds * 16000), blob: x.blob,
  }));
  const snapshots = [...liveSessions.values()]
    .filter(s => s.owner === owner && !s.discardAudio && s.latestSnapshot)
    .map(s => ({
      owner, captureId: s.id, sequence: s.latestSnapshot.sequence,
      startSamples: s.latestSnapshot.startSamples,
      durationSamples: s.latestSnapshot.durationSamples,
      overlapSamples: s.latestSnapshot.overlapSamples, blob: s.latestSnapshot.blob,
    }));
  let tail = null;
  if (captureSession?.owner === owner && capture?._chunk instanceof Float32Array
      && Number.isSafeInteger(capture._chunkUsed) && capture._chunkUsed > 0
      && capture._chunkUsed <= 240000 && capture._chunkUsed <= capture._chunk.length) {
    tail = {
      owner, captureId: captureSession.id, sequence: capture._snapshotSequence,
      startSamples: capture._chunkStartSamples, durationSamples: capture._chunkUsed,
      overlapSamples: capture._chunkOverlap,
      samples: capture._chunk.slice(0, capture._chunkUsed),
    };
  }
  globalThis.__yeobaekEmergencyRam = { owner, chunks, snapshots, tail };
  return false;
})()
```

4. 평소 상태 갱신으로 이 함수가 호출될 때까지 기다린다. Console에서 `Boolean(globalThis.__yeobaekEmergencyRam)`만 실행해 `true`인지 확인한다. 음성 객체 전체를 출력하거나 다른 사람에게 공유할 필요가 없다.
5. `true`면 조건부 중단점을 제거하고, 1절의 ZIP 스크립트를 실행한다. 이전에 만든 ZIP이 있다면 새 ZIP을 별도로 보관한다. 고정한 RAM은 그 시점의 복사이며 이후 녹음은 포함하지 않는다.
6. 계속 `false`라면 아직 함수가 실행되지 않은 것이다. 중단·종료·재전송 버튼으로 억지로 실행하지 않는다. 화면 상태에 맞는 추가 안내가 필요하다. 일반 중단점을 잘못 넣어 **Paused in debugger**가 나타나면 즉시 `F8`로 재개하고 중단점을 제거한다.

## 3. 컴퓨터 내부 경로는 어디인가?

### 문제의 사용자가 녹음한 Windows PC

새 탭에서 Chrome은 `chrome://version`, Edge는 `edge://version`을 열어 **프로필 경로(Profile Path)**를 확인한다. 대개 그 경로 아래 `IndexedDB`에 저장되지만 `Default` 프로필이라고 단정하지 않는다. [Chromium 공식 안내](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/user_data_dir.md), [Edge 공식 안내](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/UserDataDir)

이 폴더는 **바로 재생할 수 있는 WAV 폴더가 아니다.** DB 파일과 Blob 저장 파일의 조합이므로 확장자를 WAV로 바꾸지 않는다. 열린 DB 파일을 이동·삭제·덮어쓰거나, 복사하려고 브라우저부터 닫지 않는다. RAM 음성을 잃을 수 있다.

읽기 전용 화면 확인은 개발자 도구 **Application → IndexedDB → yeobaek-live-audio → chunks / pcmSnapshots**에서 한다. `Delete database`, `Clear object store`, `Clear site data`는 누르지 않는다. [Chrome 공식 IndexedDB 안내](https://developer.chrome.com/docs/devtools/storage/indexeddb/)

### 이 프로젝트의 서버 PC

코드상 기본 녹음 경로는 다음이다. 실제 `DATA_DIR` 설정과 해당 수업 파일의 존재는 별도 확인이 필요하다.

```text
/home/wonso/stt_server/.data/recordings/<계정>/<수업 UUID>.wav
```

서버에 **도착한 부분만** 여기에 있을 수 있다. Google Drive 이전 검증을 마친 녹음은 로컬 원본이 이미 정리되고 비공개 Drive 폴더에만 있을 수 있다. 아직 전송되지 않은 다른 기기의 임시 음성은 이 서버 경로에 없다. 진행 중인 WAV 원본은 이동·수정하지 않는다.

Windows 탐색기에서는 `\\wsl$`에서 해당 WSL 배포판을 선택해 Linux 경로로 접근할 수 있다. [Microsoft WSL 파일 안내](https://learn.microsoft.com/en-us/windows/wsl/filesystems)

## 적용 상태

이 문서와 콘솔 스크립트는 응급 수동 저장용 로컬 산출물이다. 웹 화면·별도 구조 창의 배포나 API 서버 재시작을 뜻하지 않는다. 실제 사용자의 브라우저에 접근하거나 녹음 복구가 끝났다고 확인한 상태도 아니다.
