import { MicrophoneCapture } from './audio.js';
import { FileImportCancelledError, RecordingFileUploader, isTerminalImportState } from './file-import.js';

const $ = id => document.getElementById(id);
let apiUrl = '', token = '', user = '', activation = false, lectures = [], current = null;
let capture = null, recording = false, starting = false, stopping = false, sending = false, authenticating = false, loggingOut = false;
let pending = [], sendError = '', sampleSeconds = 0, timer = null, requestGeneration = 0;
let draft = null, captureSession = null, stopPromise = null;
let noticeTimer, statusTimer, retryTimer = null, retryAttempt = 0, retryMessage = '';
let captureWarning = '';
let fileUploader = null, importJob = null, importProgress = null, importError = '';
let importStarting = false, importCancelling = false, importPromise = null, importGeneration = 0;
let importLectureRequest = null, lastImportLectureRefresh = 0, selectImportLecture = false;
let lectureRefreshGeneration = 0, importLectureSequence = 0;
const MAX_PENDING = 8;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30000;
const MAX_AUTO_UPLOAD_RETRIES = 8;
const RETRYABLE_UPLOAD_STATUSES = new Set([408, 425, 429]);
const PERMANENT_UPLOAD_STATUSES = new Set([400, 401, 403, 404, 409, 413, 415, 422]);
const UPLOAD_TIMEOUT_MS = 60000;
const fmt = seconds => { const n = Math.max(0, Math.floor(seconds || 0)); return `${Math.floor(n / 60).toString().padStart(2, '0')}:${(n % 60).toString().padStart(2, '0')}`; };
const dateLabel = value => new Date(value).toLocaleDateString('ko-KR', {year:'numeric',month:'long',day:'numeric'});
const bytesLabel = value => { const bytes = Math.max(0, Number(value) || 0); return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB`; };
const storage = { get() { try { return localStorage.getItem('yeobaek-server') || ''; } catch { return ''; } }, set(value) { try { localStorage.setItem('yeobaek-server', value); } catch {} } };
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').hidden = true, 6000); }
function errorText(error) { return error?.message || '잠시 후 다시 시도해 주세요.'; }
function normalizeUrl(value) {
  const url = new URL(value.trim());
  const loopback = ['localhost', '127.0.0.1', '[::1]'];
  const local = loopback.includes(url.hostname);
  const localPage = loopback.includes(location.hostname);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('경로 없이 서버 주소만 입력해 주세요.');
  const quickTunnel = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com$/.test(url.hostname);
  if (local) {
    if (!localPage) throw new Error('로컬 서버 주소는 로컬 개발 페이지에서만 사용할 수 있어요.');
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('로컬 서버 주소는 http:// 또는 https://로 시작해야 합니다.');
  } else if (url.protocol !== 'https:' || !quickTunnel || url.port) {
    throw new Error('외부 서버는 https://…trycloudflare.com 임시 연결만 사용할 수 있어요.');
  }
  return url.origin;
}
function setServer(value) { apiUrl = normalizeUrl(value); storage.set(apiUrl); $('server-label').textContent = new URL(apiUrl).host; $('api-url').value = apiUrl; }
async function api(path, options = {}, timeout = 15000, baseUrl = apiUrl) {
  if (!baseUrl) throw new Error('먼저 연결 설정에서 서버 주소를 입력해 주세요.');
  const controller = new AbortController();
  const callerSignal = options.signal;
  let timedOut = false;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) abortFromCaller();
  else callerSignal?.addEventListener?.('abort', abortFromCaller, {once:true});
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  const headers = new Headers(options.headers);
  const requestToken = options.anonymous ? '' : token, requestServer = baseUrl;
  if (requestToken) headers.set('Authorization', `Bearer ${requestToken}`);
  if (options.body && !(options.body instanceof Blob)) headers.set('Content-Type', 'application/json');
  try {
    const response = await fetch(baseUrl + path, {...options, headers, signal:controller.signal, credentials:'omit', cache:'no-store', referrerPolicy:'no-referrer'});
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      let message = typeof data?.detail === 'string' ? data.detail : `요청을 처리하지 못했습니다 (${response.status}).`;
      if (response.status === 401 && requestToken && token === requestToken && apiUrl === requestServer) { message = '로그인이 만료됐어요. 같은 계정으로 다시 로그인해 주세요.'; token = ''; showLogin(false); }
      const error = new Error(message); error.status = response.status;
      const retryAfterHeader = response.headers?.get?.('Retry-After');
      const retryAfter = retryAfterHeader === null || retryAfterHeader === undefined || retryAfterHeader.trim() === ''
        ? Number.NaN : Number(retryAfterHeader);
      if (Number.isFinite(retryAfter) && retryAfter >= 0) error.retryAfterMs = retryAfter * 1000;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError' && !callerSignal?.aborted && timedOut) {
      const transient = new Error('서버 응답이 늦어지고 있어요. 연결 상태를 확인한 뒤 다시 시도해 주세요.');
      transient.transient = true; throw transient;
    }
    if (callerSignal?.aborted) throw error;
    if (error instanceof TypeError) {
      const transient = new Error('서버에 연결할 수 없어요. 컴퓨터와 연결 프로그램이 켜져 있는지 확인해 주세요.');
      transient.transient = true; throw transient;
    }
    throw error;
  } finally {
    clearTimeout(deadline);
    callerSignal?.removeEventListener?.('abort', abortFromCaller);
  }
}
function setActivation(value) {
  activation = value;
  $('auth-title').textContent = value ? '내 비밀번호를 정해 주세요.' : '다시 만나 반가워요.';
  $('auth-description').textContent = value ? '초대 코드로 처음 한 번만 설정하면 돼요.' : '내 계정으로 로그인해 수업을 기록하세요.';
  $('setup-code-field').hidden = !value; $('confirm-field').hidden = !value;
  $('setup-code').required = value; $('password-confirm').required = value;
  $('password').minLength = value ? 4 : 1; $('password-confirm').minLength = 4;
  $('password').autocomplete = value ? 'new-password' : 'current-password';
  $('password-label').textContent = value ? '새 비밀번호 · 4자 이상' : '비밀번호';
  $('login-button').textContent = value ? '비밀번호 설정하고 시작' : '로그인 ↗';
  $('auth-toggle').textContent = value ? '이미 비밀번호가 있어요 · 로그인' : '처음이라면 · 비밀번호 설정하기';
  $('auth-error').hidden = true;
}
function importIsActive() { return !!importJob && !isTerminalImportState(importJob); }
function importTransportBusy() {
  return importStarting || (!!fileUploader?.running && importJob?.status === 'uploading');
}
function detachImportWatcher({ clear = true } = {}) {
  ++importGeneration;
  ++importLectureSequence;
  fileUploader?.detach();
  fileUploader = null; importPromise = null; importStarting = false; importCancelling = false;
  importLectureRequest = null; lastImportLectureRefresh = 0; selectImportLecture = false;
  if (clear) { importJob = null; importProgress = null; importError = ''; }
}
function showLogin(clear = true) {
  ++requestGeneration;
  if (recording || starting) void stopRecording('로그인이 만료되어 받아쓰기를 멈췄어요.');
  detachImportWatcher();
  $('workspace').hidden = true; $('auth-screen').hidden = false; clearInterval(statusTimer);
  if (clear) { user = ''; current = null; lectures = []; }
  setActivation(false);
}
$('auth-toggle').onclick = () => setActivation(!activation);
$('connection-open').onclick = () => { $('connection-error').hidden = true; $('connection-dialog').showModal(); };
$('connection-close').onclick = () => $('connection-dialog').close();
$('connection-form').onsubmit = async event => {
  event.preventDefault(); const before = apiUrl; let changed = false;
  $('connection-save').disabled = true; $('connection-error').hidden = true;
  try {
    if (authenticating || loggingOut || recording || starting || stopping || sending || importTransportBusy()) throw new Error('로그인, 로그아웃, 녹음 또는 파일 전송이 끝난 뒤 서버 주소를 바꿔 주세요.');
    // A dead Quick Tunnel must not strand the in-memory queue. Stop its old
    // retry timer, verify the candidate anonymously, then require the same
    // account to log in before stable chunk UUIDs are sent to the new origin.
    clearUploadRetry();
    const candidate = normalizeUrl($('api-url').value);
    // Keep the active origin unchanged while checking the candidate. Otherwise
    // a status timer could send the current Bearer token to an untrusted host.
    await api('/health', {anonymous:true}, 10000, candidate);
    if (candidate !== before) {
      const preserveOwner = !!user && (!!draft || pending.length > 0);
      token = '';
      ++requestGeneration;
      setServer(candidate);
      showLogin(!preserveOwner);
      if (preserveOwner) $('username').value = user;
      changed = true;
    } else {
      setServer(candidate);
    }
    $('connection-dialog').close(); notice('서버에 연결했어요.');
  } catch (error) { $('connection-error').textContent = errorText(error); $('connection-error').hidden = false; }
  finally {
    $('connection-save').disabled = false;
    if (!changed && token && pending.length && !sendError && retryTimer === null) void drain();
  }
};
$('auth-form').onsubmit = async event => {
  event.preventDefault(); $('auth-error').hidden = true; $('login-button').disabled = true; authenticating = true;
  const authServer = apiUrl, generation = requestGeneration;
  try {
    const username = $('username').value, password = $('password').value;
    const ownerLocked = recording || starting || stopping || !!draft || pending.length > 0 || sending;
    if (ownerLocked && user && username !== user) throw new Error('전송 대기 중인 음성이 있어요. 이전 계정으로 다시 로그인해 주세요.');
    if (stopPromise) await stopPromise;
    if (activation && password !== $('password-confirm').value) throw new Error('입력한 두 비밀번호가 일치하지 않아요.');
    const body = {username,password}; if (activation) body.setup_code = $('setup-code').value.trim();
    const response = await api(activation ? '/auth/activate' : '/auth/login', {method:'POST',body:JSON.stringify(body)});
    if (apiUrl !== authServer || requestGeneration !== generation) throw new Error('서버 연결 상태가 바뀌어 로그인 응답을 적용하지 않았어요. 다시 로그인해 주세요.');
    const previousUser = user;
    token = response.token; user = response.user.username; ++requestGeneration;
    if (previousUser !== user) { current = null; lectures = []; sampleSeconds = 0; $('elapsed').textContent = '00:00'; }
    $('password').value = ''; $('password-confirm').value = ''; $('setup-code').value = '';
    try { await refreshLectures(); await recoverFileImport(); } catch (error) { notice(errorText(error)); }
    if (!token) return;
    $('auth-screen').hidden = true; $('workspace').hidden = false; $('current-user').textContent = user;
    document.querySelector('.user-avatar').textContent = user[0].toUpperCase();
    renderCurrent(); void updateStatus(); clearInterval(statusTimer); statusTimer = setInterval(updateStatus, 20000);
    if (pending.length || draft) { sendError = ''; void retryPending(); }
  } catch (error) { $('auth-error').textContent = errorText(error); $('auth-error').hidden = false; }
  finally { authenticating = false; $('login-button').disabled = false; updateControls(); }
};
async function updateStatus() {
  if (!token) return;
  try { const status = await api('/status'); $('model-status').textContent = ({unloaded:'첫 받아쓰기 준비됨',loading:'음성 인식 모델 준비 중',ready:'음성 인식 모델 연결됨',error:'음성 인식 모델 확인 필요'})[status.model_state] || '서버 연결됨'; }
  catch { $('model-status').textContent = '서버 연결 확인 필요'; }
}
async function refreshLectures() {
  const owner = user, sessionToken = token, refreshGeneration = ++lectureRefreshGeneration;
  const result = await api('/lectures');
  if (owner !== user || sessionToken !== token || refreshGeneration !== lectureRefreshGeneration) return;
  lectures = Array.isArray(result) ? result : result.lectures || []; renderHistory();
}
function defaultImportTitle(file) {
  const base = String(file?.name || '').replace(/\.[^.]+$/, '').trim() || `${dateLabel(new Date())} 녹음`;
  return Array.from(base).slice(0, 120).join('');
}
function makeFileUploader(generation) {
  return new RecordingFileUploader({
    request: api,
    onState: state => {
      if (generation !== importGeneration) return;
      if (state?.status) {
        const previousLecture = importJob?.lecture_id;
        const firstState = !importJob || importJob.id !== state.id;
        importJob = state; importStarting = false;
        if (isTerminalImportState(state) && !state.lecture_id && current?.id === previousLecture) {
          current = null; renderCurrent();
        }
        if (firstState) {
          void refreshLectures().catch(error => notice(errorText(error)));
          void refreshImportLecture(state, true, generation);
        } else if (state.status === 'processing') {
          void refreshImportLecture(state, false, generation);
        }
      }
      if (state?.retry_attempt) {
        importProgress = {
          ...(importProgress || {}),
          retryAttempt: state.retry_attempt,
          retryDelayMs: state.retry_delay_ms,
        };
      } else if (state?.status) {
        importProgress = {...(importProgress || {}), retryAttempt: 0, retryDelayMs: 0};
      }
      updateControls();
    },
    onProgress: progress => {
      if (generation !== importGeneration) return;
      const retryAttempt = importProgress?.retryAttempt || 0;
      const retryDelayMs = importProgress?.retryDelayMs || 0;
      importProgress = {...progress,retryAttempt,retryDelayMs};
      updateControls();
    },
  });
}
async function refreshImportLecture(state = importJob, force = false, generation = importGeneration) {
  if (!token || !state?.lecture_id || isTerminalImportState(state) && state.status !== 'completed') return;
  const now = Date.now();
  const shouldSelect = force && (selectImportLecture || current?.id === state.lecture_id);
  if (!shouldSelect && current?.id !== state.lecture_id) return;
  if (!force && (importLectureRequest || now - lastImportLectureRefresh < 2500)) return;
  lastImportLectureRefresh = now;
  const lectureId = state.lecture_id;
  const navigationGeneration = requestGeneration;
  const sequence = ++importLectureSequence;
  const request = api(`/lectures/${lectureId}`);
  importLectureRequest = request;
  try {
    const lecture = await request;
    if (generation !== importGeneration || sequence !== importLectureSequence
        || navigationGeneration !== requestGeneration || !token) return;
    if (shouldSelect || current?.id === lectureId) {
      current = lecture; selectImportLecture = false; renderCurrent(); renderHistory();
    }
  } catch (error) {
    if (generation === importGeneration && error?.status !== 404) notice(errorText(error));
  } finally {
    if (importLectureRequest === request) importLectureRequest = null;
  }
}
async function runFileImport(operation, generation) {
  const running = Promise.resolve(operation);
  importPromise = running;
  try {
    const result = await running;
    if (generation !== importGeneration) return;
    importJob = result; importError = ''; importProgress = {...(importProgress || {}),phase:'completed',percent:100};
    if (fileUploader) fileUploader.file = null;
    $('recording-file').value = '';
    await refreshLectures();
    await refreshImportLecture(result, true, generation);
    notice(result.raw_deleted
      ? '녹음 파일 변환을 마쳤어요. 원본 임시 파일은 서버에서 삭제했습니다.'
      : '녹음 파일 변환을 마쳤지만 원본 임시 파일 삭제를 재시도하고 있어요.');
  } catch (error) {
    if (generation !== importGeneration) return;
    if (!importJob && error?.importId && fileUploader && token) {
      try {
        const recoveredState = await api(`/imports/${error.importId}`);
        if (generation !== importGeneration) return;
        const recovered = fileUploader.recover(recoveredState, fileUploader.file);
        importError = '';
        return await runFileImport(recovered, generation);
      } catch (recoveryError) {
        if (generation !== importGeneration) return;
        if (recoveryError?.status !== 404) error = recoveryError;
      }
    }
    if (error?.importState) importJob = error.importState;
    if (!(error instanceof FileImportCancelledError && importCancelling)) {
      importError = errorText(error);
      if (importJob?.status === 'uploading') {
        importError += ' 같은 파일을 다시 선택하고 이어 올리기를 눌러 주세요.';
      } else if (!(error instanceof FileImportCancelledError)) {
        notice(importError);
      }
    }
    if (isTerminalImportState(importJob)) {
      if (fileUploader) fileUploader.file = null;
      $('recording-file').value = '';
      await refreshLectures().catch(() => {});
    }
  } finally {
    if (generation === importGeneration) {
      importStarting = false;
      if (importPromise === running) importPromise = null;
      updateControls();
    }
  }
}
async function recoverFileImport() {
  const owner = user, sessionToken = token, navigationGeneration = requestGeneration;
  const startingGeneration = importGeneration;
  const result = await api('/imports');
  if (!sessionToken || owner !== user || sessionToken !== token
      || startingGeneration !== importGeneration) return;
  const jobs = Array.isArray(result) ? result : result?.imports || [];
  const active = jobs.find(job => !isTerminalImportState(job));
  if (!active) {
    const previous = importJob;
    const previousLecture = previous?.lecture_id;
    const terminal = previous ? jobs.find(job => job.id === previous.id && isTerminalImportState(job)) : null;
    detachImportWatcher();
    const generation = importGeneration;
    if (terminal) importJob = terminal;
    await refreshLectures();
    if (owner !== user || sessionToken !== token || generation !== importGeneration) return;
    if (previousLecture && current?.id === previousLecture && !lectures.some(lecture => lecture.id === previousLecture)) {
      current = null; renderCurrent(); renderHistory();
    }
    updateControls();
    return;
  }
  detachImportWatcher();
  const generation = ++importGeneration;
  importJob = active; importError = '';
  selectImportLecture = navigationGeneration === requestGeneration
    && (!current || current.id === active.lecture_id);
  fileUploader = makeFileUploader(generation);
  const recovered = fileUploader.recover(active);
  if (active.status === 'uploading') {
    importError = `업로드를 계속하려면 “${active.filename}” 파일을 다시 선택해 주세요.`;
    await refreshImportLecture(active, true, generation);
    updateControls();
    return;
  }
  void runFileImport(recovered, generation);
  await refreshImportLecture(active, true, generation);
}
function queuedCount() { return pending.length + (draft?.buffered.length || 0); }
function isBusy() { return authenticating || loggingOut || recording || starting || stopping || !!draft || pending.length > 0 || sending || importTransportBusy(); }
function renderHistory() {
  $('lecture-count').textContent = lectures.length; const list = $('lecture-list'); list.replaceChildren();
  if (!lectures.length) { const empty = document.createElement('p'); empty.className = 'empty-history'; empty.textContent = '아직 기록한 수업이 없어요.'; list.append(empty); }
  for (const lecture of lectures) {
    const button = document.createElement('button'); button.type = 'button'; button.className = `lecture-item${current?.id === lecture.id ? ' selected' : ''}`; button.disabled = isBusy();
    const title = document.createElement('strong'); title.textContent = lecture.title;
    const date = document.createElement('span'); date.textContent = dateLabel(lecture.created_at); button.append(title, date);
    button.onclick = async () => { if (isBusy()) return; selectImportLecture = false; ++importLectureSequence; const generation = ++requestGeneration; try { const note = await api(`/lectures/${lecture.id}`); if (generation !== requestGeneration || isBusy()) return; current = note; renderCurrent(); renderHistory(); } catch (error) { notice(errorText(error)); } };
    list.append(button);
  }
}
function renderCurrent() {
  $('note-date').textContent = dateLabel(current?.created_at || new Date());
  $('view-label').textContent = current?.title || '새 수업';
  document.querySelector('.note-heading h1').textContent = current?.title || '오늘의 배움을 담아보세요.';
  if (current) { $('lecture-title').value = current.title; $('language').value = current.language || 'auto'; }
  const segments = current?.segments || [];
  const transcript = $('transcript'); transcript.replaceChildren();
  if (!segments.length) {
    const empty = document.createElement('div'); empty.className = 'empty-note';
    const mark = document.createElement('span'); mark.className = 'empty-symbol'; mark.ariaHidden = 'true'; mark.textContent = '≋';
    const heading = document.createElement('h3'); heading.textContent = current && !recording && !starting ? '아직 받아쓴 내용이 없어요.' : '첫 문장을 기다리고 있어요.';
    const text = document.createElement('p'); text.textContent = recording || starting ? '목소리가 들어오면 이곳에 글이 나타나요.' : '수업 이름을 적고 받아쓰기를 시작해 보세요.'; empty.append(mark,heading,text); transcript.append(empty);
  } else {
    for (const segment of segments) { const row = document.createElement('div'); row.className = 'segment'; const time = document.createElement('time'); time.textContent = fmt(segment.start); const text = document.createElement('p'); text.textContent = segment.text; row.append(time,text); transcript.append(row); }
  }
  $('segment-count').textContent = segments.length; $('download').disabled = segments.length === 0;
  updateControls();
}
function selectedCaptureSource() { return $('audio-source').value === 'system' ? 'system' : 'microphone'; }
function updateSourceGuidance() {
  const system = selectedCaptureSource() === 'system';
  $('source-guidance').textContent = system
    ? '데스크톱 Chrome·Edge에서 탭·화면과 오디오 공유를 켜세요. 선택 범위의 알림이나 다른 앱 소리도 함께 들어갈 수 있으며, 화면 영상은 서버로 보내지 않습니다.'
    : '마이크로 주변의 수업 소리를 받아씁니다.';
  $('source-privacy').hidden = !system;
}
$('audio-source').onchange = () => { updateSourceGuidance(); updateControls(); };
function resetNewNote({ focus = false } = {}) {
  ++requestGeneration;
  selectImportLecture = false; ++importLectureSequence;
  current = null; sampleSeconds = 0;
  $('lecture-title').value = '';
  $('language').value = 'ko';
  $('elapsed').textContent = '00:00';
  renderCurrent(); renderHistory();
  if (focus) $('lecture-title').focus();
}
function renderImportStatus() {
  const panel = $('import-status');
  const state = importJob?.status;
  if (!importStarting && !importJob && !importError) { panel.hidden = true; return; }
  panel.hidden = false;
  const fingerprinting = importProgress?.phase === 'fingerprinting';
  const labels = {
    uploading: fingerprinting ? '선택한 파일 전체가 같은지 확인하고 있어요' : fileUploader?.running ? '녹음 파일을 안전하게 올리고 있어요' : '파일 업로드를 이어서 할 수 있어요',
    queued: '업로드 완료 · 서버 변환 대기 중',
    processing: importJob?.cancel_requested ? '취소 요청을 처리하고 있어요' : '서버에서 녹음을 받아쓰고 있어요',
    completed: '녹음 파일 변환 완료',
    failed: '녹음 파일을 변환하지 못했어요',
    cancelled: '녹음 파일 변환을 취소했어요',
  };
  $('import-state').textContent = importStarting && !state ? '파일 전체를 안전하게 확인하고 있어요' : labels[state] || '파일 상태를 확인하고 있어요';
  const progress = $('import-progress');
  let percent = Number(importProgress?.percent);
  if (state === 'uploading' && importJob?.total_bytes && !fingerprinting) percent = importJob.uploaded_bytes / importJob.total_bytes * 100;
  if (state === 'completed') percent = 100;
  if (Number.isFinite(percent) && (fingerprinting || state === 'uploading' || state === 'completed' || importProgress?.durationSeconds)) {
    progress.value = Math.min(100, Math.max(0, percent));
    progress.textContent = `${Math.round(progress.value)}%`;
  } else {
    progress.removeAttribute('value');
    progress.textContent = '처리 중';
  }
  let detail = '';
  if (importStarting && !state) detail = '긴 파일도 전체를 브라우저 메모리에 올리지 않고 조각별로 읽어 확인합니다.';
  else if (state === 'uploading') {
    detail = `${importJob.filename} · ${bytesLabel(importJob.uploaded_bytes)} / ${bytesLabel(importJob.total_bytes)}`;
    detail += fingerprinting ? ' · 모든 바이트가 처음 파일과 같은지 확인 중입니다.' : fileUploader?.running ? ' · 업로드가 끝날 때까지 이 탭을 닫지 마세요.' : ' · 같은 파일을 다시 선택하면 받은 지점부터 이어집니다.';
  } else if (state === 'queued') detail = `${importJob.filename} · 서버에서 순서를 기다립니다. 이제 탭을 닫아도 변환은 계속됩니다.`;
  else if (state === 'processing') detail = `${importJob.filename} · ${fmt(importJob.processed_seconds)} 분량 처리됨 · 탭을 닫아도 서버에서 계속됩니다.`;
  else if (state === 'completed') detail = `${importJob.filename} · ${fmt(importJob.duration_seconds)} 분량 · ${importJob.raw_deleted ? '원본 임시 파일 삭제됨' : '원본 임시 파일 삭제 재시도 중'}`;
  else if (state === 'failed') detail = `${importJob.filename} · 불완전한 기록 삭제됨 · ${importJob.raw_deleted ? '원본 임시 파일 삭제됨' : '원본 임시 파일 삭제 재시도 중'}`;
  else if (state === 'cancelled') detail = `${importJob.filename} · 불완전한 기록 삭제됨 · ${importJob.raw_deleted ? '원본 임시 파일 삭제됨' : '원본 임시 파일 삭제 재시도 중'}`;
  if (importProgress?.retryAttempt) detail += ` · 연결 오류로 ${Math.ceil((importProgress.retryDelayMs || 0) / 1000)}초 후 재시도 (${importProgress.retryAttempt}/8)`;
  if (importError) detail += `${detail ? ' · ' : ''}${importError}`;
  $('import-detail').textContent = detail;
  $('import-cancel').hidden = !importIsActive();
  $('import-cancel').disabled = importCancelling || !!importJob?.cancel_requested;
}
function updateControls() {
  const busy = isBusy(), queued = queuedCount(), system = selectedCaptureSource() === 'system', activeImport = importIsActive(); $('new-note').disabled = busy; $('logout').disabled = busy;
  $('lecture-title').disabled = busy || !!current; $('language').disabled = busy || !!current;
  $('audio-source').disabled = busy || !!current;
  $('record-button').disabled = authenticating || loggingOut || (starting && !recording) || stopping || activeImport || importStarting || (!recording && (!!draft || pending.length > 0 || sending));
  $('record-button').classList.toggle('stop', recording);
  $('record-button').textContent = recording ? '■ 받아쓰기 중지' : starting ? (system ? '공유 화면 준비 중…' : '마이크 준비 중…') : stopping ? '마지막 음성 정리 중…' : activeImport || importStarting ? '파일 변환이 끝난 뒤 시작' : current ? '＋ 새 수업 시작' : system ? '● 화면 소리 받아쓰기' : '● 받아쓰기 시작';
  $('record-dot').classList.toggle('live', recording);
  $('record-state').textContent = recording ? (system ? '공유한 화면의 소리를 듣고 있어요' : '수업을 듣고 있어요') : starting ? (system ? '공유할 화면과 오디오를 준비하고 있어요' : '마이크와 노트를 준비하고 있어요') : queued ? '남은 음성을 받아쓰고 있어요' : current ? '수업 기록을 저장했어요' : '시작할 준비가 됐어요';
  $('record-hint').textContent = recording ? (system ? '선택한 탭이나 화면을 재생해 주세요. 화면 영상은 전송하지 않아요.' : '창을 닫지 않고 수업에 집중해 주세요.') : current ? '새 수업을 시작하거나 기록을 내려받을 수 있어요.' : system ? '시작한 뒤 재생할 탭·화면을 고르고 오디오 공유를 켜세요.' : '약 8초 뒤 말이 잠시 멈출 때마다 정확하게 기록해요.';
  $('save-state').textContent = retryMessage ? `${queued}개 음성 · 자동 재전송 대기` : queued ? `${queued}개 음성 처리 대기` : captureWarning ? '마지막 오디오 일부 누락 가능 · 받은 내용만 저장됨' : current ? '서버 컴퓨터에 저장됨' : '서버 컴퓨터에 저장';
  $('processing').hidden = !recording && !starting && !queued && !sending;
  $('processing-text').textContent = sendError ? '자동 전송을 멈췄어요. 안내를 확인해 주세요.' : retryMessage || (sending ? '음성을 글로 바꾸고 있어요. 첫 실행은 준비 시간이 필요해요…' : '다음 문장을 듣고 있어요…');
  $('queue-warning').hidden = !sendError; $('queue-message').textContent = `${sendError} 이 페이지를 닫으면 대기 중인 음성은 사라져요.`; $('retry').disabled = sending || starting || stopping;
  $('save-failed').hidden = !sendError || !pending.length;
  $('save-failed').disabled = sending || starting || stopping || !pending.length;
  $('skip-failed').hidden = !sendError || !pending[0]?.downloadRequested;
  $('skip-failed').disabled = sending || starting || stopping || !pending[0]?.downloadRequested;
  const canResumeImport = importJob?.status === 'uploading' && !fileUploader?.running;
  const liveAudioBusy = authenticating || loggingOut || recording || starting || stopping || !!draft || pending.length > 0 || sending;
  $('recording-file').disabled = liveAudioBusy || importStarting || (activeImport && !canResumeImport);
  $('import-button').disabled = liveAudioBusy || importStarting || (activeImport && !canResumeImport) || !$('recording-file').files?.length;
  $('import-button').textContent = canResumeImport ? '같은 파일 이어 올리기' : '파일 올려 변환';
  for (const button of $('lecture-list').querySelectorAll('button')) button.disabled = busy;
  renderImportStatus();
}
$('recording-file').onchange = () => {
  const file = $('recording-file').files?.[0];
  if (file && current && !isBusy() && !importIsActive()) resetNewNote();
  if (file && !current && !importIsActive() && !$('lecture-title').value.trim()) {
    $('lecture-title').value = defaultImportTitle(file);
  }
  updateControls();
};
async function startOrResumeFileImport() {
  if (authenticating || loggingOut) return;
  const file = $('recording-file').files?.[0];
  if (!file) { notice('먼저 변환할 녹음 파일을 선택해 주세요.'); return; }
  if (recording || starting || stopping || draft || pending.length || sending) {
    notice('실시간 음성 전송이 모두 끝난 뒤 녹음 파일을 올려 주세요.'); return;
  }
  const reconcilingUnknownJob = !importJob && fileUploader?.importId && importError;
  if (current && !reconcilingUnknownJob && !importIsActive()) resetNewNote();
  // If every response after the idempotent init POST was lost, the browser may
  // not yet know whether the server created this exact ID. Reconcile it before
  // generating another ID that would collide with the one-active-job rule.
  if (!importJob && fileUploader?.importId && importError && !fileUploader.running) {
    importStarting = true; updateControls();
    try {
      const state = await api(`/imports/${fileUploader.importId}`);
      const generation = importGeneration;
      importError = '';
      const recovered = fileUploader.recover(state, file);
      updateControls();
      void runFileImport(recovered, generation);
      return;
    } catch (error) {
      if (error?.status !== 404) {
        importError = `${errorText(error)} 기존 파일 작업 상태를 확인한 뒤 다시 시도해 주세요.`;
        notice(importError); return;
      }
      // The server confirms that the previous ID never existed, so a new
      // idempotent import may now be created safely.
    } finally {
      importStarting = false; updateControls();
    }
  }
  if (importIsActive()) {
    if (importJob.status !== 'uploading' || fileUploader?.running) return;
    importError = ''; selectImportLecture = !current || current.id === importJob.lecture_id;
    const generation = importGeneration;
    const operation = fileUploader.resume(file);
    updateControls();
    void runFileImport(operation, generation);
    return;
  }
  detachImportWatcher();
  const generation = ++importGeneration;
  importStarting = true; importError = ''; importProgress = null; selectImportLecture = true;
  fileUploader = makeFileUploader(generation);
  const title = (!current ? $('lecture-title').value.trim() : '') || defaultImportTitle(file);
  const language = $('language').value === 'auto' ? null : $('language').value;
  updateControls();
  void runFileImport(fileUploader.start(file, {title,language}), generation);
}
$('import-button').onclick = () => { void startOrResumeFileImport(); };
async function cancelFileImport() {
  if (!importIsActive() || !fileUploader || importCancelling) return;
  const owner = user, sessionToken = token, generation = importGeneration, uploader = fileUploader;
  const sessionIsCurrent = () => !!sessionToken && owner === user && sessionToken === token
    && generation === importGeneration && uploader === fileUploader;
  importCancelling = true; importError = ''; updateControls();
  try {
    const state = await uploader.cancel();
    if (!sessionIsCurrent()) return;
    importJob = state;
    if (isTerminalImportState(state)) {
      uploader.file = null; $('recording-file').value = '';
      await refreshLectures();
      if (!sessionIsCurrent()) return;
      if (!state.lecture_id && current) {
        const stillExists = lectures.some(lecture => lecture.id === current.id);
        if (!stillExists) { current = null; renderCurrent(); }
      }
      if (state.status === 'completed') {
        notice(state.raw_deleted
          ? '취소 요청보다 먼저 변환이 완료됐어요. 원본 임시 파일은 삭제했습니다.'
          : '취소 요청보다 먼저 변환이 완료됐어요. 원본 임시 파일 삭제를 재시도하고 있습니다.');
      } else if (state.status === 'cancelled') {
        notice(state.raw_deleted
          ? '녹음 파일 변환을 취소하고 원본 임시 파일을 삭제했어요.'
          : '변환은 취소됐으며 원본 임시 파일 삭제를 재시도하고 있어요.');
      } else {
        notice(importJob.error || (state.raw_deleted
          ? '파일 변환이 실패해 원본 임시 파일과 불완전한 기록을 삭제했어요.'
          : '파일 변환이 실패했으며 원본 임시 파일 삭제를 재시도하고 있어요.'));
      }
    } else {
      importError = '현재 음성 조각을 마친 뒤 서버가 원본과 불완전한 기록을 삭제합니다.';
      setTimeout(() => {
        if (sessionIsCurrent() && importJob?.cancel_requested) {
          void recoverFileImport().catch(error => {
            if (!sessionIsCurrent()) return;
            importError = errorText(error); updateControls();
          });
        }
      }, 1000);
    }
  } catch (error) {
    if (!sessionIsCurrent()) return;
    importError = error?.cancelRequestFailed
      ? `${errorText(error)} 서버에서 계속 처리 중일 수 있으니 상태를 다시 확인해 주세요.`
      : errorText(error);
    notice(importError);
  } finally {
    if (sessionIsCurrent()) { importCancelling = false; updateControls(); }
  }
}
$('import-cancel').onclick = () => { void cancelFileImport(); };
$('new-note').onclick = () => { if (!isBusy()) resetNewNote({focus:true}); };
$('record-button').onclick = () => recording ? void stopRecording() : void startRecording();
async function startRecording() {
  if (isBusy() || importIsActive() || importStarting) return;
  if (current) resetNewNote();
  ++requestGeneration; starting = true; sendError = ''; captureWarning = ''; sampleSeconds = 0; $('elapsed').textContent = '00:00'; updateControls();
  const title = $('lecture-title').value.trim() || `${dateLabel(new Date())} 수업`;
  const language = $('language').value === 'auto' ? null : $('language').value;
  const session = {id:crypto.randomUUID(),lecture:null,buffered:[],title,language,cancelled:false,creating:null};
  draft = session; captureSession = session;
  const queue = chunk => {
    sampleSeconds = chunk.startSeconds + chunk.durationSeconds;
    if (!session.lecture) session.buffered.push(chunk);
    else enqueueChunk(session, chunk);
    updateControls();
    if (queuedCount() >= MAX_PENDING && recording && !stopping) void stopRecording('서버 처리가 늦어져 녹음을 멈췄어요. 남은 음성을 먼저 전송할게요.');
  };
  capture = new MicrophoneCapture({source:selectedCaptureSource(),onChunk:queue,onLevel:level => { $('mic-level').style.width = `${Math.min(100, Math.max(0,level) * 180)}%`; },onInterrupted:error => { void stopRecording(errorText(error)); }});
  try {
    // Invoke microphone/display capture directly in the click gesture, before network awaits.
    await capture.start();
    if (!session.cancelled) {
      recording = true;
      const startTime = performance.now();
      timer = setInterval(() => { $('elapsed').textContent = fmt((performance.now() - startTime) / 1000); }, 500);
      updateControls();
    }
    await assignLecture(session);
  } catch (error) {
    await stopRecording();
    if (draft === session && !session.buffered.length) draft = null;
    if (queuedCount()) sendError = errorText(error);
    notice(errorText(error));
  }
  finally { starting = false; updateControls(); }
}
function enqueueChunk(session, chunk) {
  pending.push({...chunk,id:crypto.randomUUID(),lectureId:session.lecture.id});
  void drain();
}
async function assignLecture(session) {
  if (session.lecture) return;
  if (session.creating) return session.creating;
  session.creating = (async () => {
    const lecture = await api('/lectures',{
      method:'POST',
      body:JSON.stringify({title:session.title,language:session.language}),
      headers:{'X-Lecture-Id':session.id},
    });
    session.lecture = lecture;
    if (draft === session) draft = null;
    current = {...lecture,segments:lecture.segments || []};
    lectures.unshift(lecture);
    for (const chunk of session.buffered.splice(0)) enqueueChunk(session, chunk);
    renderHistory(); renderCurrent();
  })().finally(() => { session.creating = null; });
  return session.creating;
}
function stopRecording(message) {
  if (stopPromise) return stopPromise;
  if (!recording && !starting && !capture) return Promise.resolve();
  if (captureSession) captureSession.cancelled = true;
  const microphone = capture;
  stopping = true; recording = false; clearInterval(timer); updateControls();
  stopPromise = (async () => {
    let flushWarning = '';
    try { await microphone?.stop(); }
    catch (error) { flushWarning = errorText(error); captureWarning = flushWarning; }
    finally {
      if (capture === microphone) capture = null;
      stopping = false;
      $('mic-level').style.width = '0%';
      $('elapsed').textContent = fmt(sampleSeconds);
      updateControls();
      const combined = [message,flushWarning].filter(Boolean).join(' ');
      if (combined) notice(combined);
    }
  })().finally(() => { stopPromise = null; });
  return stopPromise;
}
async function uploadBlob(chunk) {
  if (chunk.durationSeconds >= 0.05) return chunk.blob;
  // The API requires >= 50 ms. Preserve short final audio and pad only its end.
  const original = new Uint8Array(await chunk.blob.arrayBuffer());
  const padded = new Uint8Array(Math.max(original.length, 44 + 800 * 2));
  padded.set(original);
  const header = new DataView(padded.buffer);
  header.setUint32(4, padded.length - 8, true);
  header.setUint32(40, padded.length - 44, true);
  return new Blob([padded], {type:'audio/wav'});
}
function retryableUpload(error) {
  // A retry can arrive while the original request is still finishing after a
  // browser/Cloudflare timeout. The server distinguishes this idempotent race
  // from a changed-payload 409 by supplying Retry-After only for the former.
  const status = Number(error?.status);
  return error?.transient === true
    || RETRYABLE_UPLOAD_STATUSES.has(status)
    || (status >= 500 && status <= 599)
    || (status === 409 && Number.isFinite(error?.retryAfterMs));
}
function clearUploadRetry() {
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null; retryAttempt = 0; retryMessage = '';
}
function scheduleUploadRetry(error) {
  retryAttempt += 1;
  const exponential = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** Math.min(retryAttempt - 1, 10)));
  const delay = Math.max(exponential, Math.min(error?.retryAfterMs || 0, 120000));
  retryMessage = `${errorText(error)} ${Math.ceil(delay / 1000)}초 후 자동으로 다시 전송할게요.`;
  retryTimer = setTimeout(() => {
    retryTimer = null; retryMessage = ''; updateControls(); void drain();
  }, delay);
}
function manualUploadError(error) {
  const reason = PERMANENT_UPLOAD_STATUSES.has(error?.status)
    ? '서버가 요청을 거절해 자동 재시도를 멈췄어요.'
    : '예상하지 못한 오류라 자동 재시도를 멈췄어요.';
  return `${errorText(error)} ${reason}`;
}
async function drain() {
  if (sending || sendError || retryTimer !== null || !token || !pending.length) return;
  sending = true; updateControls();
  try {
    while (pending.length && token) {
      const chunk = pending[0];
      const blob = await uploadBlob(chunk);
      let response;
      try {
        response = await api(`/lectures/${chunk.lectureId}/chunks`,{method:'POST',body:blob,headers:{'Content-Type':'audio/wav','X-Chunk-Id':chunk.id,'X-Start-Seconds':String(chunk.startSeconds),'X-Overlap-Seconds':String(chunk.overlapSeconds ?? 0),'X-Final-Chunk':chunk.final ? 'true' : 'false'}},UPLOAD_TIMEOUT_MS);
      } catch (error) {
        if (retryableUpload(error) && retryAttempt < MAX_AUTO_UPLOAD_RETRIES) {
          scheduleUploadRetry(error);
          break;
        }
        if (retryableUpload(error)) {
          sendError = `${errorText(error)} 자동 재전송 ${MAX_AUTO_UPLOAD_RETRIES}회 후에도 처리되지 않아 멈췄어요.`;
          if (recording || starting) void stopRecording('서버 오류가 계속되어 녹음을 멈췄어요. 안내를 확인한 뒤 다시 전송해 주세요.');
          break;
        }
        sendError = manualUploadError(error);
        if (recording || starting) void stopRecording('서버가 음성을 처리하지 못해 녹음을 멈췄어요. 안내를 확인한 뒤 다시 전송해 주세요.');
        break;
      }
      if (current?.id === chunk.lectureId) {
        const ids = new Set(current.segments.map(x => x.id));
        for (const segment of response.segments) if (!ids.has(segment.id)) current.segments.push(segment);
        current.segments.sort((a,b) => a.start - b.start);
      }
      pending.shift(); retryAttempt = 0; retryMessage = ''; renderCurrent();
    }
  } catch (error) {
    sendError = manualUploadError(error);
    if (recording || starting) void stopRecording('음성 전송을 안전하게 멈췄어요. 안내를 확인한 뒤 다시 전송해 주세요.');
  } finally {
    sending = false; updateControls();
    // Pick up a final microphone tail that may have arrived while an upload awaited.
    if (pending.length && token && !sendError && retryTimer === null) void drain();
  }
}
async function retryPending() {
  if (sending || starting || stopping) return;
  clearUploadRetry(); sendError = '';
  if (draft) {
    starting = true; updateControls();
    try { await assignLecture(draft); }
    catch (error) { sendError = errorText(error); }
    finally { starting = false; updateControls(); }
  }
  void drain();
}
$('retry').onclick = () => { void retryPending(); };
async function saveFailedChunk() {
  if (!sendError || sending || starting || stopping || !pending.length) return;
  const chunk = pending[0];
  try {
    const blob = await uploadBlob(chunk);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const title = (current?.title || '수업').replace(/[<>:"/\\|?*\u0000-\u001F]/g,'_').slice(0,80);
    link.href = url;
    link.download = `${title}_${fmt(chunk.startSeconds).replace(':','-')}_처리실패.wav`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url),1000);
    chunk.downloadRequested = true;
    notice('다운로드한 WAV 파일이 기기에 저장됐는지 확인한 뒤 건너뛰기를 눌러 주세요.');
    updateControls();
  } catch (error) {
    notice(`WAV를 저장하지 못했습니다. ${errorText(error)}`);
  }
}
$('save-failed').onclick = () => { void saveFailedChunk(); };
function skipFailedChunk() {
  if (!sendError || sending || starting || stopping || !pending[0]?.downloadRequested) return;
  pending.shift();
  clearUploadRetry(); sendError = '';
  notice('파일 저장을 확인한 음성 조각을 건너뛰었어요. 해당 구간은 기록에서 빠집니다.');
  renderCurrent();
  void drain();
}
$('skip-failed').onclick = skipFailedChunk;
$('download').onclick = () => {
  if (!current?.segments?.length) return;
  const text = `${current.title}\n${dateLabel(current.created_at)}\n\n${current.segments.map(s => `[${fmt(s.start)}] ${s.text}`).join('\n\n')}\n`;
  const url = URL.createObjectURL(new Blob(['\uFEFF',text],{type:'text/plain;charset=utf-8'})); const link = document.createElement('a'); link.href = url; link.download = `${current.title.replace(/[<>:"/\\|?*\u0000-\u001F]/g,'_').slice(0,100)}.txt`; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
};
$('logout').onclick = async () => {
  if (isBusy()) return;
  loggingOut = true; updateControls();
  try { await api('/auth/logout',{method:'POST'}); } catch {}
  finally {
    token = '';
    const preserveOwner = !!user && (!!draft || pending.length > 0);
    loggingOut = false;
    showLogin(!preserveOwner);
  }
};
window.addEventListener('beforeunload', event => { if (isBusy()) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('visibilitychange', () => { if (document.hidden && recording) notice(selectedCaptureSource() === 'system' ? '공유 중인 탭이나 화면을 유지해 주세요. 브라우저가 오디오 공유를 중단할 수 있어요.' : '이 탭과 화면을 유지해 주세요. 기기가 녹음을 중단할 수 있어요.'); });

async function init() {
  // Keep invitation codes out of the URL as soon as the document runs.
  const invite = new URLSearchParams(location.hash.slice(1));
  if (location.hash) history.replaceState(null,'',location.pathname + location.search);
  let configured = '';
  try { const response = await fetch('./config.json',{cache:'no-store'}); configured = (await response.json()).apiUrl || ''; } catch {}
  const local = ['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  // Invitation fragments are easy to forge. Never let one choose the server
  // that receives a setup code and new password; users set that origin apart.
  try { const preferred = storage.get() || configured || (local ? 'http://127.0.0.1:8765' : ''); if (preferred) setServer(preferred); } catch { notice('서버 주소를 다시 설정해 주세요.'); }
  if (invite.get('setup_code')) {
    setActivation(true);
    $('setup-code').value = invite.get('setup_code');
    // The server owns the account allow-list and validates the single-use code.
    // The client only restores the opaque invitation fields into the form.
    if (invite.get('username')) $('username').value = invite.get('username');
  }
  updateSourceGuidance();
  renderCurrent();
}
void init();
