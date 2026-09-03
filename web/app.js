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
let lectureDateFilter = '', recordingDownloadPending = false, recordingFinalizePending = false;
let deletingLecture = false, deleteTarget = null;
let noteActionSequence = 0;
let correction = null, correctionView = 'raw', correctionLectureId = '';
let correctionLoading = false, correctionStarting = false, correctionError = '', correctionCreditExhausted = false;
let correctionSequence = 0, correctionPollTimer = null;
let connectionState = 'unverified', verifiedApiUrl = '', verifiedApiExpiresAt = 0;
let connectionGeneration = 0, connectionController = null, connectionLeaseTimer = null, leaseRefreshPromise = null;
const MAX_PENDING = 8;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30000;
const MAX_AUTO_UPLOAD_RETRIES = 8;
const RETRYABLE_UPLOAD_STATUSES = new Set([408, 425, 429]);
const PERMANENT_UPLOAD_STATUSES = new Set([400, 401, 403, 404, 409, 413, 415, 422, 507]);
const UPLOAD_TIMEOUT_MS = 60000;
const CONFIG_TIMEOUT_MS = 8000;
const RUNTIME_CONFIG_TTL_MS = 24 * 60 * 60 * 1000;
const CONFIG_CLOCK_SKEW_MS = 5 * 60 * 1000;
const CORRECTION_POLL_MS = 2500;
const fmt = seconds => { const n = Math.max(0, Math.floor(seconds || 0)); return `${Math.floor(n / 60).toString().padStart(2, '0')}:${(n % 60).toString().padStart(2, '0')}`; };
const KST_DATE_FORMATTER = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',year:'numeric',month:'long',day:'numeric'});
const KST_DATE_PARTS = new Intl.DateTimeFormat('en', {timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'});
const dateLabel = value => KST_DATE_FORMATTER.format(new Date(value));
function dateKey(value) {
  const parts = Object.fromEntries(KST_DATE_PARTS.formatToParts(new Date(value)).map(part => [part.type,part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
const bytesLabel = value => { const bytes = Math.max(0, Number(value) || 0); return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB`; };
function safeFilename(value) {
  const cleaned = Array.from(String(value || '').replace(/[<>:"/\\|?*\u0000-\u001F]/g,'_').trim())
    .slice(0,100).join('').replace(/[. ]+$/g,'');
  return cleaned || '수업';
}
function escapeMarkdown(value) {
  return String(value ?? '').replace(/\r\n?/g,'\n')
    .replace(/[\u0021-\u002F\u003A-\u0040\u005B-\u0060\u007B-\u007E]/g, character => `\\${character}`)
    .replace(/\n/g,'  \n');
}
const hasSegmentStart = segment => segment?.start !== null && segment?.start !== undefined
  && segment.start !== '' && Number.isFinite(Number(segment.start));
function exportText(lecture, format) {
  const segments = lecture?.segments || [];
  const corrected = lecture?.transcript_version === 'corrected';
  const versionLine = corrected ? 'AI 후보정본 · 받아쓴 원문은 서버에 별도 보관\n' : '';
  if (format === 'text') {
    const body = segments.map(segment => hasSegmentStart(segment)
      ? `[${fmt(segment.start)}] ${segment.text}` : segment.text).join('\n\n');
    return `${lecture.title}\n${dateLabel(lecture.created_at)}\n${versionLine}\n${body}\n`;
  }
  const language = ({ko:'한국어',en:'영어'})[lecture.language] || '자동 감지';
  const body = segments.map(segment => hasSegmentStart(segment)
    ? `**\\[${fmt(segment.start)}\\]** ${escapeMarkdown(segment.text)}` : escapeMarkdown(segment.text)).join('\n\n');
  const version = corrected ? '\n- 버전: AI 후보정본 (받아쓴 원문 별도 보관)' : '';
  return `# ${escapeMarkdown(lecture.title)}\n\n- 날짜: ${dateLabel(lecture.created_at)}\n- 언어: ${language}${version}\n\n## ${corrected ? 'AI 후보정본' : '받아쓴 원문'}\n\n${body}\n`;
}
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
function nativeDownloadUrl(value, server = apiUrl) {
  if (typeof value !== 'string' || !/^\/recording-downloads\/[A-Za-z0-9_-]{32,128}$/.test(value)) {
    throw new Error('서버가 안전하지 않은 다운로드 주소를 보냈습니다.');
  }
  const base = new URL(server);
  const url = new URL(value, `${base.origin}/`);
  if (url.origin !== base.origin || url.username || url.password || url.search || url.hash || url.pathname !== value) {
    throw new Error('서버가 안전하지 않은 다운로드 주소를 보냈습니다.');
  }
  return url.href;
}
function setServer(value) { apiUrl = normalizeUrl(value); storage.set(apiUrl); $('server-label').textContent = new URL(apiUrl).host; $('api-url').value = apiUrl; }
async function api(path, options = {}, timeout = 15000, baseUrl = '') {
  const anonymous = options.anonymous === true;
  const {anonymous:_anonymous, ...requestOptions} = options;
  const requestedServer = baseUrl || apiUrl, requestedToken = token;
  if (!anonymous) {
    if (!hasVerifiedServer()) await ensureTrustedApiRequest();
    if (!hasVerifiedServer() || apiUrl !== requestedServer || token !== requestedToken) {
      throw connectionChangedBeforeRequestError();
    }
    baseUrl = apiUrl;
  } else {
    baseUrl = baseUrl || apiUrl;
  }
  if (!baseUrl) throw new Error('먼저 연결 설정에서 서버 주소를 입력해 주세요.');
  if (requestOptions.signal?.aborted) {
    const error = new Error('요청이 취소됐습니다.'); error.name = 'AbortError'; throw error;
  }
  const controller = new AbortController();
  const callerSignal = requestOptions.signal;
  let timedOut = false;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) abortFromCaller();
  else callerSignal?.addEventListener?.('abort', abortFromCaller, {once:true});
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  const headers = new Headers(requestOptions.headers);
  const requestToken = anonymous ? '' : token, requestServer = baseUrl;
  if (requestToken) headers.set('Authorization', `Bearer ${requestToken}`);
  if (requestOptions.body && !(requestOptions.body instanceof Blob)) headers.set('Content-Type', 'application/json');
  try {
    const response = await fetch(baseUrl + path, {...requestOptions, headers, signal:controller.signal, credentials:'omit', cache:'no-store', referrerPolicy:'no-referrer'});
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      let message = typeof data?.detail === 'string' ? data.detail : `요청을 처리하지 못했습니다 (${response.status}).`;
      if (response.status === 401 && requestToken && token === requestToken && apiUrl === requestServer) { message = '로그인이 만료됐어요. 같은 계정으로 다시 로그인해 주세요.'; token = ''; showLogin(false); }
      const error = new Error(message); error.status = response.status;
      error.code = typeof data?.error_code === 'string' ? data.error_code : typeof data?.code === 'string' ? data.code : '';
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
function updateAuthControls() {
  $('login-button').disabled = authenticating || !hasVerifiedServer();
}
function automaticLeaseExpired(now = Date.now()) {
  return verifiedApiExpiresAt > 0 && !!apiUrl && apiUrl === verifiedApiUrl && now >= verifiedApiExpiresAt;
}
function hasTrustedApiOrigin() {
  return !!apiUrl && apiUrl === verifiedApiUrl && !automaticLeaseExpired();
}
function hasVerifiedServer() {
  return connectionState === 'connected' && hasTrustedApiOrigin();
}
function connectionChangedBeforeRequestError() {
  const error = new Error('서버 연결이 바뀌어 요청을 보내지 않았어요. 연결을 확인한 뒤 다시 시도해 주세요.');
  error.connectionChanged = true;
  return error;
}
function expiredLeaseError() {
  const error = new Error('자동 서버 주소가 만료되어 요청을 보내지 않았어요. 새 서버 연결을 확인한 뒤 다시 시도해 주세요.');
  error.connectionLeaseExpired = true;
  return error;
}
function clearConnectionLeaseTimer() {
  if (connectionLeaseTimer !== null) clearTimeout(connectionLeaseTimer);
  connectionLeaseTimer = null;
}
function scheduleConnectionLeaseTimer() {
  clearConnectionLeaseTimer();
  if (!(verifiedApiExpiresAt > 0) || apiUrl !== verifiedApiUrl) return;
  const server = verifiedApiUrl, expiresAt = verifiedApiExpiresAt;
  const delay = Math.max(0,expiresAt - Date.now() + 1);
  connectionLeaseTimer = setTimeout(() => {
    connectionLeaseTimer = null;
    if (apiUrl !== server || verifiedApiUrl !== server || verifiedApiExpiresAt !== expiresAt
        || !automaticLeaseExpired()) return;
    // Do not cancel an explicit user verification at the lease boundary. It is
    // anonymous, keeps login locked, and will replace the automatic lease with
    // tab-scoped manual trust only after its own health check succeeds.
    if (connectionState === 'checking') { updateAuthControls(); return; }
    setConnectionState('unverified','자동 서버 주소가 만료되어 새 연결을 확인하고 있어요.');
    void refreshExpiredAutomaticServer().catch(() => {});
  },delay);
}
function setConnectionState(state, message = '') {
  connectionState = state;
  const labels = {
    unverified:'연결 확인 필요',
    discovering:'서버 찾는 중…',
    checking:'서버 확인 중…',
    connected:apiUrl ? new URL(apiUrl).host : '서버 연결됨',
    'manual-needed':'연결 설정 필요',
  };
  const messages = {
    unverified:'서버 연결을 확인해야 로그인할 수 있어요.',
    discovering:'현재 서버 주소를 자동으로 찾고 있어요.',
    checking:'입력한 서버가 안전하게 연결되는지 확인하고 있어요.',
    connected:'서버 연결을 확인했어요. 로그인할 수 있습니다.',
    'manual-needed':'서버를 자동으로 연결하지 못했어요. 현재 주소를 직접 입력해 주세요.',
  };
  const busy = state === 'discovering' || state === 'checking';
  const label = labels[state] || labels.unverified;
  const status = message || messages[state] || messages.unverified;
  $('server-label').textContent = label;
  $('connection-open').setAttribute('data-state',state);
  $('connection-open').setAttribute('aria-label',state === 'connected'
    ? `내 서버 ${label}, 연결 설정 열기`
    : `${label}, 연결 설정 열기`);
  if (busy) $('connection-open').setAttribute('aria-busy','true');
  else $('connection-open').removeAttribute('aria-busy');
  $('auth-server-status').textContent = status;
  $('connection-status').textContent = status;
  updateAuthControls();
}
function cancelConnectionAttempt() {
  ++connectionGeneration;
  connectionController?.abort();
  connectionController = null;
}
function runtimeConfigError(code, message, transient = false) {
  const error = new Error(message);
  error.discoveryCode = code;
  error.configTransient = transient;
  return error;
}
function parseRuntimeTimestamp(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) {
    throw runtimeConfigError('malformed','자동 연결 설정의 시간이 올바르지 않습니다.');
  }
  const milliseconds = Date.parse(value);
  const canonical = Number.isFinite(milliseconds)
    ? new Date(milliseconds).toISOString().replace('.000Z','Z') : '';
  if (canonical !== value) throw runtimeConfigError('malformed','자동 연결 설정의 시간이 올바르지 않습니다.');
  return milliseconds;
}
function validateRuntimeConfig(value, now = Date.now()) {
  const expectedKeys = 'apiUrl,expiresAt,publishedAt,state,version';
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).sort().join(',') !== expectedKeys
      || value.version !== 1 || !['online','offline'].includes(value.state)) {
    throw runtimeConfigError('malformed','자동 연결 설정 형식이 올바르지 않습니다.');
  }
  const publishedAt = parseRuntimeTimestamp(value.publishedAt);
  const expiresAt = parseRuntimeTimestamp(value.expiresAt);
  if (publishedAt > now + CONFIG_CLOCK_SKEW_MS) {
    throw runtimeConfigError('malformed','자동 연결 설정의 게시 시간이 올바르지 않습니다.');
  }
  if (value.state === 'offline') {
    if (value.apiUrl !== '' || expiresAt !== publishedAt) {
      throw runtimeConfigError('malformed','꺼진 서버의 자동 연결 설정이 올바르지 않습니다.');
    }
    return {state:'offline',apiUrl:''};
  }
  if (expiresAt - publishedAt !== RUNTIME_CONFIG_TTL_MS) {
    throw runtimeConfigError('malformed','자동 연결 설정의 유효 기간이 올바르지 않습니다.');
  }
  if (expiresAt <= now) throw runtimeConfigError('expired','게시된 서버 주소가 만료됐습니다.');
  let candidate;
  try { candidate = normalizeUrl(value.apiUrl); }
  catch { throw runtimeConfigError('malformed','자동 연결 설정의 서버 주소가 올바르지 않습니다.'); }
  if (candidate !== value.apiUrl) throw runtimeConfigError('malformed','자동 연결 설정의 서버 주소 형식이 올바르지 않습니다.');
  return {state:'online',apiUrl:candidate,expiresAt};
}
async function fetchRuntimeConfig(signal) {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(signal?.reason);
  if (signal?.aborted) abortFromCaller();
  else signal?.addEventListener?.('abort',abortFromCaller,{once:true});
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); },CONFIG_TIMEOUT_MS);
  let response;
  try {
    // Pages/CDN caches can briefly retain the previous tunnel after a publish.
    // The query contains no user data; it only makes each reload a fresh lookup.
    response = await fetch(`./config.json?v=${Date.now()}`,{cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer',signal:controller.signal});
  } catch (error) {
    if (signal?.aborted) throw error;
    throw runtimeConfigError('config-unavailable',timedOut
      ? '자동 연결 설정을 불러오는 데 시간이 오래 걸리고 있습니다.'
      : '자동 연결 설정을 불러오지 못했습니다.',true);
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener?.('abort',abortFromCaller);
  }
  if (!response.ok) {
    const transient = response.status === 408 || response.status === 429 || response.status >= 500;
    throw runtimeConfigError(transient ? 'config-unavailable' : 'malformed',
      `자동 연결 설정을 불러오지 못했습니다 (${response.status}).`,transient);
  }
  try { return await response.json(); }
  catch { throw runtimeConfigError('malformed','자동 연결 설정을 읽지 못했습니다.'); }
}
async function verifyServerCandidate(value, signal, timeout = 10000) {
  const candidate = normalizeUrl(value);
  const health = await api('/health',{anonymous:true,signal},timeout,candidate);
  if (health?.status !== 'ok') throw new Error('서버 상태 응답을 확인하지 못했습니다.');
  return candidate;
}
function installVerifiedServer(candidate, message, {expiresAt = 0} = {}) {
  const before = apiUrl;
  let changed = false;
  if (candidate !== before) {
    const preserveOwner = !!user && (!!draft || pending.length > 0);
    const hasExistingSession = !!before || !!token || !!user || !!draft || pending.length > 0;
    if (hasExistingSession) {
      token = '';
      ++requestGeneration;
      setServer(candidate);
      showLogin(!preserveOwner);
      if (preserveOwner) $('username').value = user;
    } else {
      setServer(candidate);
    }
    changed = true;
  } else {
    setServer(candidate);
  }
  verifiedApiUrl = candidate;
  verifiedApiExpiresAt = expiresAt;
  setConnectionState('connected',message);
  scheduleConnectionLeaseTimer();
  return changed;
}
function discoveryFailureMessage(error) {
  if (error?.discoveryCode === 'offline') return '서버가 꺼져 있어요. 서버 컴퓨터를 켠 뒤 새로고침하거나 현재 주소를 직접 입력해 주세요.';
  if (error?.discoveryCode === 'expired') return '자동으로 게시된 서버 주소가 만료됐어요. 서버를 다시 켠 뒤 새로고침하거나 현재 주소를 직접 입력해 주세요.';
  if (error?.discoveryCode === 'malformed') return '자동 연결 설정을 확인하지 못했어요. 현재 서버 주소를 직접 입력해 주세요.';
  return '서버를 자동으로 찾지 못했어요. 서버가 켜져 있는지 확인하고 현재 주소를 직접 입력해 주세요.';
}
async function discoverServer() {
  const retainedVerifiedOrigin = connectionState === 'connected' && hasVerifiedServer() ? apiUrl : '';
  cancelConnectionAttempt();
  const sequence = connectionGeneration;
  const controller = new AbortController();
  connectionController = controller;
  const operationIsCurrent = () => sequence === connectionGeneration && connectionController === controller;
  setConnectionState('discovering');
  const local = ['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  const saved = storage.get();
  let savedCandidate = '';
  try { if (saved) savedCandidate = normalizeUrl(saved); } catch {}
  if (!$('api-url').value && savedCandidate) $('api-url').value = savedCandidate;
  try {
    let candidate, connectedMessage, expiresAt = 0;
    if (local) {
      const localCandidate = savedCandidate || 'http://127.0.0.1:8765';
      $('api-url').value = localCandidate;
      candidate = await verifyServerCandidate(localCandidate,controller.signal);
      connectedMessage = '로컬 서버 연결을 확인했어요. 로그인할 수 있습니다.';
    } else {
      // A saved Quick Tunnel cannot safely replace the same-origin runtime
      // lease: it may be expired, stopped, or later reassigned. Keep it only as
      // a manual prefill when Pages config itself is unavailable.
      const config = await fetchRuntimeConfig(controller.signal);
      if (!operationIsCurrent()) return;
      const runtime = validateRuntimeConfig(config);
      if (runtime.state === 'offline') throw runtimeConfigError('offline','서버가 꺼져 있습니다.');
      expiresAt = runtime.expiresAt;
      $('api-url').value = runtime.apiUrl;
      try { candidate = await verifyServerCandidate(runtime.apiUrl,controller.signal); }
      catch (error) {
        if (!operationIsCurrent()) return;
        throw runtimeConfigError('unreachable',errorText(error));
      }
      connectedMessage = '현재 서버를 자동으로 찾아 연결을 확인했어요. 로그인할 수 있습니다.';
    }
    if (!operationIsCurrent()) return;
    installVerifiedServer(candidate,connectedMessage,{expiresAt});
  } catch (error) {
    if (!operationIsCurrent()) return;
    const canRetainConnection = !!retainedVerifiedOrigin
      && apiUrl === retainedVerifiedOrigin && verifiedApiUrl === retainedVerifiedOrigin
      && !automaticLeaseExpired();
    setConnectionState(canRetainConnection ? 'connected' : 'manual-needed',canRetainConnection
      ? '기존 서버 연결을 유지하고 있어요.' : discoveryFailureMessage(error));
  } finally {
    if (operationIsCurrent()) connectionController = null;
  }
}
function invalidateExpiredAutomaticServer(server, expiresAt) {
  if (apiUrl !== server || verifiedApiUrl !== server || verifiedApiExpiresAt !== expiresAt) return;
  const owner = user;
  const preserveOwner = !!owner && (!!draft || pending.length > 0);
  const hadSession = !!token || !!owner || !!draft || pending.length > 0 || !$('workspace').hidden;
  clearConnectionLeaseTimer();
  verifiedApiUrl = ''; verifiedApiExpiresAt = 0; token = '';
  if (hadSession) {
    showLogin(!preserveOwner);
    if (preserveOwner) $('username').value = owner;
  } else {
    ++requestGeneration;
  }
  setConnectionState('manual-needed','자동 서버 주소가 만료되어 새 연결을 확인하지 못했어요. 서버를 다시 켠 뒤 새로고침하거나 현재 주소를 직접 입력해 주세요.');
}
async function refreshExpiredAutomaticServer() {
  if (hasVerifiedServer()) return apiUrl;
  if (!automaticLeaseExpired()) throw connectionChangedBeforeRequestError();
  const expiredServer = apiUrl, expiredAt = verifiedApiExpiresAt;
  if (!leaseRefreshPromise) {
    const refresh = discoverServer();
    const tracked = refresh.finally(() => {
      if (leaseRefreshPromise === tracked) leaseRefreshPromise = null;
    });
    leaseRefreshPromise = tracked;
  }
  try {
    await leaseRefreshPromise;
  } catch {
    invalidateExpiredAutomaticServer(expiredServer,expiredAt);
    throw expiredLeaseError();
  }
  if (hasVerifiedServer()) return apiUrl;
  invalidateExpiredAutomaticServer(expiredServer,expiredAt);
  throw expiredLeaseError();
}
async function ensureTrustedApiRequest() {
  if (hasVerifiedServer()) return apiUrl;
  if (automaticLeaseExpired()) return refreshExpiredAutomaticServer();
  throw connectionChangedBeforeRequestError();
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
  updateAuthControls();
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
  ++noteActionSequence;
  recordingDownloadPending = false; recordingFinalizePending = false; deletingLecture = false; deleteTarget = null;
  if ($('delete-dialog').open) $('delete-dialog').close();
  if (recording || starting) void stopRecording('로그인이 만료되어 받아쓰기를 멈췄어요.');
  detachImportWatcher();
  $('workspace').hidden = true; $('auth-screen').hidden = false; clearInterval(statusTimer);
  if (clear) { user = ''; current = null; lectures = []; lectureDateFilter = ''; }
  resetCorrectionState(current?.id || '');
  setActivation(false);
}
$('auth-toggle').onclick = () => setActivation(!activation);
function openConnectionDialog() {
  if (connectionState === 'discovering') {
    cancelConnectionAttempt();
    setConnectionState(hasTrustedApiOrigin() ? 'connected' : 'manual-needed',hasTrustedApiOrigin()
      ? '기존 서버 연결을 유지하고 있어요.'
      : '자동 찾기를 멈췄어요. 현재 서버 주소를 직접 입력해 주세요.');
  }
  if (!$('api-url').value) {
    try { $('api-url').value = normalizeUrl(storage.get()); } catch {}
  }
  $('connection-error').hidden = true;
  $('api-url').removeAttribute('aria-invalid');
  $('connection-dialog').showModal();
  $('api-url').focus();
}
function cancelManualConnection() {
  if (connectionState === 'checking') cancelConnectionAttempt();
  $('api-url').disabled = false;
  $('connection-save').disabled = false;
  setConnectionState(hasTrustedApiOrigin() ? 'connected' : 'manual-needed',hasTrustedApiOrigin()
    ? '기존 서버 연결을 유지하고 있어요.'
    : '현재 서버 주소를 직접 입력해 주세요.');
}
$('connection-open').onclick = openConnectionDialog;
$('auth-server-open').onclick = openConnectionDialog;
$('connection-close').onclick = () => { cancelManualConnection(); $('connection-dialog').close(); };
$('connection-dialog').oncancel = cancelManualConnection;
$('connection-form').onsubmit = async event => {
  event.preventDefault(); let changed = false, sequence = null, controller = null;
  $('connection-error').hidden = true; $('api-url').removeAttribute('aria-invalid');
  try {
    if (authenticating || loggingOut || recording || starting || stopping || sending || importTransportBusy() || recordingDownloadPending || recordingFinalizePending || deletingLecture) throw new Error('로그인, 로그아웃, 녹음, 다운로드 또는 파일 전송이 끝난 뒤 서버 주소를 바꿔 주세요.');
    const candidate = normalizeUrl($('api-url').value);
    // A dead Quick Tunnel must not strand the in-memory queue. Stop its old
    // retry timer, verify the candidate anonymously, then require the same
    // account to log in before stable chunk UUIDs are sent to the new origin.
    clearUploadRetry();
    cancelConnectionAttempt(); sequence = connectionGeneration;
    controller = new AbortController(); connectionController = controller;
    const operationIsCurrent = () => sequence === connectionGeneration && connectionController === controller;
    $('connection-save').disabled = true; $('api-url').disabled = true;
    setConnectionState('checking');
    // Keep the active origin unchanged while checking the candidate. Otherwise
    // a status timer could send the current Bearer token to an untrusted host.
    await verifyServerCandidate(candidate,controller.signal);
    if (!operationIsCurrent()) return;
    changed = installVerifiedServer(candidate,'입력한 서버 연결을 확인했어요. 로그인할 수 있습니다.');
    $('connection-dialog').close(); notice('서버에 연결했어요.');
  } catch (error) {
    if (sequence !== null && (sequence !== connectionGeneration || connectionController !== controller)) return;
    setConnectionState(hasTrustedApiOrigin() ? 'connected' : 'manual-needed',hasTrustedApiOrigin()
      ? '기존 서버 연결을 유지하고 있어요.' : '서버 주소를 확인하고 다시 시도해 주세요.');
    $('api-url').setAttribute('aria-invalid','true');
    $('connection-error').textContent = errorText(error); $('connection-error').hidden = false;
    $('api-url').focus();
  }
  finally {
    if (sequence === null || sequence === connectionGeneration) {
      if (connectionController === controller) connectionController = null;
      $('connection-save').disabled = false; $('api-url').disabled = false;
      if (!changed && token && pending.length && !sendError && retryTimer === null) void drain();
    }
  }
};
$('auth-form').onsubmit = async event => {
  event.preventDefault(); $('auth-error').hidden = true;
  const refreshingExpiredLease = automaticLeaseExpired();
  if (!hasVerifiedServer() && !refreshingExpiredLease) {
    $('auth-error').textContent = '서버 연결을 먼저 확인해 주세요.'; $('auth-error').hidden = false;
    $('auth-server-open').focus(); return;
  }
  if (!refreshingExpiredLease) cancelConnectionAttempt();
  authenticating = true; updateAuthControls();
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
    if (previousUser !== user) { current = null; lectures = []; lectureDateFilter = ''; sampleSeconds = 0; $('elapsed').textContent = '00:00'; }
    $('password').value = ''; $('password-confirm').value = ''; $('setup-code').value = '';
    try { await refreshLectures(); await recoverFileImport(); } catch (error) { notice(errorText(error)); }
    if (!token) return;
    $('auth-screen').hidden = true; $('workspace').hidden = false; $('current-user').textContent = user;
    document.querySelector('.user-avatar').textContent = user[0].toUpperCase();
    renderCurrent();
    if (current?.id && current.recording_finalized === true && current.segments?.length) void loadCorrection(current.id);
    void updateStatus(); clearInterval(statusTimer); statusTimer = setInterval(updateStatus, 20000);
    if (pending.length || draft) { sendError = ''; void retryPending(); }
  } catch (error) { $('auth-error').textContent = errorText(error); $('auth-error').hidden = false; }
  finally { authenticating = false; updateAuthControls(); updateControls(); }
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
function isBusy() { return authenticating || loggingOut || recording || starting || stopping || !!draft || pending.length > 0 || sending || importTransportBusy() || recordingDownloadPending || recordingFinalizePending || deletingLecture; }
function renderHistory() {
  const dateCounts = new Map();
  for (const lecture of lectures) {
    const key = dateKey(lecture.created_at);
    const entry = dateCounts.get(key) || {count:0,label:dateLabel(lecture.created_at)};
    entry.count += 1; dateCounts.set(key,entry);
  }
  if (lectureDateFilter && !dateCounts.has(lectureDateFilter)) lectureDateFilter = '';
  const dateSelect = $('lecture-date'); dateSelect.replaceChildren();
  const allDates = document.createElement('option'); allDates.value = ''; allDates.textContent = '전체 날짜'; dateSelect.append(allDates);
  for (const [key,entry] of dateCounts) {
    const option = document.createElement('option'); option.value = key;
    option.textContent = `${entry.label} (${entry.count})`; dateSelect.append(option);
  }
  dateSelect.value = lectureDateFilter;

  const visible = lectureDateFilter ? lectures.filter(lecture => dateKey(lecture.created_at) === lectureDateFilter) : lectures;
  $('lecture-count').textContent = lectureDateFilter ? `${visible.length}/${lectures.length}` : lectures.length;
  $('lecture-count').ariaLabel = lectureDateFilter ? `선택한 날짜 수업 ${visible.length}개, 전체 ${lectures.length}개` : `저장된 수업 ${lectures.length}개`;
  const list = $('lecture-list'); list.replaceChildren();
  if (!lectures.length) {
    const empty = document.createElement('p'); empty.className = 'empty-history'; empty.textContent = '아직 기록한 수업이 없어요.'; list.append(empty); return;
  }
  if (!visible.length) {
    const empty = document.createElement('p'); empty.className = 'empty-history'; empty.textContent = '이 날짜에 저장된 수업이 없어요.'; list.append(empty); return;
  }
  const groups = new Map();
  for (const lecture of visible) {
    const key = dateKey(lecture.created_at);
    if (!groups.has(key)) groups.set(key,[]);
    groups.get(key).push(lecture);
  }
  for (const groupLectures of groups.values()) {
    const group = document.createElement('section'); group.className = 'lecture-day';
    const heading = document.createElement('h3'); heading.textContent = dateLabel(groupLectures[0].created_at);
    const items = document.createElement('div'); items.className = 'lecture-day-items';
    for (const lecture of groupLectures) {
      const button = document.createElement('button'); button.type = 'button'; button.className = `lecture-item${current?.id === lecture.id ? ' selected' : ''}`; button.disabled = isBusy();
      if (current?.id === lecture.id) button.setAttribute('aria-current','page');
      const title = document.createElement('strong'); title.textContent = lecture.title;
      const date = document.createElement('span'); date.textContent = dateLabel(lecture.created_at); button.append(title,date);
      button.onclick = async () => { if (isBusy()) return; selectImportLecture = false; ++importLectureSequence; const generation = ++requestGeneration; try { const note = await api(`/lectures/${lecture.id}`); if (generation !== requestGeneration || isBusy()) return; current = note; renderCurrent(); renderHistory(); if (note.recording_finalized === true && note.segments?.length) void loadCorrection(note.id); } catch (error) { notice(errorText(error)); } };
      items.append(button);
    }
    group.append(heading,items); list.append(group);
  }
}
$('lecture-date').onchange = () => { lectureDateFilter = $('lecture-date').value; renderHistory(); updateControls(); };
function clearCorrectionPoll() {
  if (correctionPollTimer !== null) clearTimeout(correctionPollTimer);
  correctionPollTimer = null;
}
function resetCorrectionState(lectureId = '') {
  ++correctionSequence;
  clearCorrectionPoll();
  correction = null; correctionView = 'raw'; correctionLectureId = lectureId;
  correctionLoading = false; correctionStarting = false; correctionError = ''; correctionCreditExhausted = false;
}
function correctionPayload(value) {
  const payload = value?.correction && typeof value.correction === 'object' ? value.correction : value;
  if (!payload || typeof payload !== 'object') return null;
  const aliases = {pending:'queued',running:'processing',done:'completed',error:'failed'};
  const status = aliases[payload.status] || payload.status;
  return {
    ...payload,
    status: ['queued','processing','completed','failed'].includes(status) ? status : 'failed',
    corrected_text: typeof payload.corrected_text === 'string' ? payload.corrected_text
      : typeof payload.result?.corrected_text === 'string' ? payload.result.corrected_text : '',
    corrected_segments: Array.isArray(payload.corrected_segments) ? payload.corrected_segments
      : Array.isArray(payload.result?.corrected_segments) ? payload.result.corrected_segments : [],
    error: typeof payload.error === 'string' ? payload.error
      : typeof payload.detail === 'string' ? payload.detail : '',
    error_code: typeof payload.error_code === 'string' ? payload.error_code
      : typeof payload.code === 'string' ? payload.code : '',
  };
}
function normalizedCorrectedSegments(value = correction) {
  const segments = (value?.corrected_segments || []).flatMap((segment,index) => {
    if (!segment || typeof segment.text !== 'string' || !segment.text.trim()) return [];
    const start = segment.start !== null && segment.start !== undefined && segment.start !== '' ? Number(segment.start) : Number.NaN;
    const end = segment.end !== null && segment.end !== undefined && segment.end !== '' ? Number(segment.end) : Number.NaN;
    return [{
      id:String(segment.id || segment.segment_id || `corrected-${index}`),
      start:Number.isFinite(start) ? start : null,
      end:Number.isFinite(end) ? end : null,
      text:segment.text.trim(),
    }];
  });
  if (segments.length) return segments;
  const text = String(value?.corrected_text || '').trim();
  return text ? [{id:'corrected-text',start:null,end:null,text}] : [];
}
function correctionIsReady() {
  return correction?.status === 'completed' && normalizedCorrectedSegments().length > 0;
}
function displayedTranscriptSegments() {
  return correctionView === 'corrected' && correctionIsReady()
    ? normalizedCorrectedSegments() : current?.segments || [];
}
function selectedTranscriptLecture() {
  return {
    ...current,
    segments:displayedTranscriptSegments(),
    transcript_version:correctionView === 'corrected' && correctionIsReady() ? 'corrected' : 'raw',
  };
}
function isCreditExhaustion(value) {
  const code = String(value?.error_code || value?.code || '').toLowerCase();
  const message = String(value?.error || value?.message || value?.detail || '');
  return value?.status === 402 || ['credit_exhausted','credits_exhausted','quota_exhausted'].includes(code)
    || /(?:credit|quota|크레딧).*(?:exhaust|insufficient|부족|소진)/i.test(message);
}
function correctionStatus() {
  if (correctionStarting) return 'starting';
  if (correctionLoading && !correction) return 'loading';
  if (correctionCreditExhausted) return 'credit-exhausted';
  if (correctionError) return 'failed';
  if (current?.segments?.length && current.recording_finalized !== true && !correctionIsReady()) return 'unfinished';
  return correction?.status || 'idle';
}
function updateCorrectionControls(noteToolsBusy = isBusy() || importIsActive() || importStarting || importCancelling) {
  const hasTranscript = !!current?.segments?.length;
  const ready = correctionIsReady();
  const status = correctionStatus();
  const running = ['loading','starting','queued','processing'].includes(status);
  $('transcript-versions').hidden = !hasTranscript;
  $('transcript-raw').disabled = !hasTranscript;
  $('transcript-corrected').disabled = !hasTranscript || !ready;
  $('correct-transcript').disabled = !hasTranscript || current?.recording_finalized !== true
    || noteToolsBusy || running || ready;
}
function renderCorrection() {
  const hasTranscript = !!current?.segments?.length;
  const ready = correctionIsReady();
  if (!ready && correctionView === 'corrected') correctionView = 'raw';
  const corrected = correctionView === 'corrected' && ready;
  $('transcript-raw').classList.toggle('active', !corrected);
  $('transcript-corrected').classList.toggle('active', corrected);
  $('transcript-raw').setAttribute('aria-pressed',String(!corrected));
  $('transcript-corrected').setAttribute('aria-pressed',String(corrected));
  $('correction-panel').hidden = !hasTranscript;
  const status = correctionStatus();
  $('correction-panel').setAttribute('data-state',status);
  $('correction-panel').setAttribute('aria-busy',String(['loading','starting','queued','processing'].includes(status)));
  const copies = {
    idle:['AI 후보정','받아쓴 원문은 그대로 두고, 전사 텍스트만 학교 AI 서버에 보내 별도의 후보정본을 만듭니다.','AI 후보정 만들기'],
    unfinished:['수업 기록을 마무리해 주세요','마지막 음성 저장이 끝난 뒤 AI 후보정을 시작할 수 있습니다. 받아쓴 원문은 지금도 내려받을 수 있어요.','마무리 후 사용'],
    loading:['후보정 확인 중','이 수업에 저장된 후보정본이 있는지 확인하고 있어요.','확인하는 중…'],
    starting:['후보정 요청 중','원문을 보존한 채 후보정 작업을 요청하고 있어요.','요청하는 중…'],
    queued:['후보정 대기 중','학교 AI 서버의 처리 순서를 기다리고 있어요. 이 화면을 벗어나도 서버에서 계속됩니다.','대기 중…'],
    processing:['AI가 후보정 중','문장과 문맥을 확인하고 있어요. 원문은 변경되지 않습니다.','후보정 중…'],
    completed:['AI 후보정 완료','후보정본을 별도로 저장했어요. 위에서 원문과 후보정본을 바꿔 볼 수 있습니다.','후보정 완료'],
    failed:['후보정을 마치지 못했어요',correctionError || correction?.error || '받아쓴 원문은 안전하게 남아 있습니다. 잠시 후 다시 시도해 주세요.','다시 시도'],
    'credit-exhausted':['AI 크레딧이 부족해요','후보정만 중단됐으며 받아쓴 원문은 안전하게 남아 있습니다. 크레딧을 확인한 뒤 다시 시도해 주세요.','다시 확인'],
  };
  let [title,detail,action] = copies[status] || copies.failed;
  const allUncertain = Array.isArray(correction?.uncertain_terms)
    ? correction.uncertain_terms.filter(term => typeof term === 'string' && term.trim()) : [];
  const uncertain = allUncertain.slice(0,5);
  if (status === 'completed' && uncertain.length) {
    const remaining = allUncertain.length - uncertain.length;
    detail += ` 확인이 필요한 표현: ${uncertain.join(', ')}${remaining ? ` 외 ${remaining}개` : ''}`;
  }
  $('correction-state').textContent = title;
  $('correction-detail').textContent = detail;
  $('correction-detail').setAttribute('role',['failed','credit-exhausted'].includes(status) ? 'alert' : 'status');
  $('correct-transcript').textContent = action;
  updateCorrectionControls();
}
function applyCorrectionResponse(value, lectureId) {
  const previousStatus = correction?.status;
  const next = correctionPayload(value);
  correction = next;
  correctionError = next?.status === 'failed' ? next.error || '후보정 작업을 완료하지 못했습니다.' : '';
  correctionCreditExhausted = isCreditExhaustion(next);
  if (correctionCreditExhausted) correctionError = '';
  if (next?.status === 'completed' && !normalizedCorrectedSegments(next).length) {
    correctionError = '서버가 비어 있는 후보정 결과를 보냈습니다. 원문을 이용해 주세요.';
    correction = {...next,status:'failed'};
  }
  if (['queued','processing'].includes(correction?.status)) scheduleCorrectionPoll(lectureId);
  return ['queued','processing'].includes(previousStatus) && correctionIsReady();
}
function scheduleCorrectionPoll(lectureId) {
  clearCorrectionPoll();
  const sequence = correctionSequence;
  correctionPollTimer = setTimeout(() => {
    correctionPollTimer = null;
    if (sequence === correctionSequence && current?.id === lectureId && token) void loadCorrection(lectureId,{quiet:true});
  },CORRECTION_POLL_MS);
}
async function loadCorrection(lectureId, {quiet = false} = {}) {
  if (!lectureId || current?.id !== lectureId || !token) return;
  clearCorrectionPoll();
  const owner = user, sessionToken = token, server = apiUrl;
  const sequence = ++correctionSequence;
  const operationIsCurrent = () => sequence === correctionSequence && current?.id === lectureId
    && owner === user && sessionToken === token && server === apiUrl;
  if (!quiet) { correctionLoading = true; correctionError = ''; correctionCreditExhausted = false; renderCorrection(); }
  try {
    const result = await api(`/lectures/${encodeURIComponent(lectureId)}/correction`);
    if (!operationIsCurrent()) return;
    const completedWhilePolling = applyCorrectionResponse(result,lectureId);
    if (completedWhilePolling) notice('AI 후보정본을 만들었어요. 원문과 비교해 보세요.');
  } catch (error) {
    if (!operationIsCurrent()) return;
    if (error?.status === 404) {
      correction = null; correctionError = ''; correctionCreditExhausted = false;
    } else {
      correctionError = errorText(error); correctionCreditExhausted = isCreditExhaustion(error);
    }
  } finally {
    if (operationIsCurrent()) { correctionLoading = false; renderCurrent(); }
  }
}
async function requestCorrection() {
  if (!current?.id || !current.segments?.length || correctionStarting || correctionIsReady()
      || current.recording_finalized !== true || isBusy() || importIsActive() || importStarting) return;
  if (['queued','processing'].includes(correction?.status)) {
    if (correctionError || correctionCreditExhausted) {
      correctionError = ''; correctionCreditExhausted = false;
      void loadCorrection(current.id);
    }
    return;
  }
  const lectureId = current.id, owner = user, sessionToken = token, server = apiUrl;
  clearCorrectionPoll();
  const sequence = ++correctionSequence;
  const operationIsCurrent = () => sequence === correctionSequence && current?.id === lectureId
    && owner === user && sessionToken === token && server === apiUrl;
  correctionStarting = true; correctionError = ''; correctionCreditExhausted = false; renderCorrection();
  try {
    const result = await api(`/lectures/${encodeURIComponent(lectureId)}/correction`,{method:'POST'},30000);
    if (!operationIsCurrent()) return;
    applyCorrectionResponse(result,lectureId);
    if (correction?.status === 'completed') notice('AI 후보정본을 만들었어요. 원문과 비교해 보세요.');
  } catch (error) {
    if (!operationIsCurrent()) return;
    correctionError = errorText(error); correctionCreditExhausted = isCreditExhaustion(error);
  } finally {
    if (operationIsCurrent()) { correctionStarting = false; renderCurrent(); }
  }
}
$('transcript-raw').onclick = () => {
  if (correctionView === 'raw') return;
  correctionView = 'raw'; renderCurrent();
};
$('transcript-corrected').onclick = () => {
  if (!correctionIsReady() || correctionView === 'corrected') return;
  correctionView = 'corrected'; renderCurrent();
};
$('correct-transcript').onclick = () => { void requestCorrection(); };
function renderCurrent() {
  const lectureId = current?.id || '';
  if (lectureId !== correctionLectureId) {
    resetCorrectionState(lectureId);
    if (current?.correction) correction = correctionPayload(current.correction);
  }
  $('note-date').textContent = dateLabel(current?.created_at || new Date());
  $('view-label').textContent = current?.title || '새 수업';
  document.querySelector('.note-heading h1').textContent = current?.title || '오늘의 배움을 담아보세요.';
  if (current) { $('lecture-title').value = current.title; $('language').value = current.language || 'auto'; }
  renderCorrection();
  const segments = displayedTranscriptSegments();
  const transcript = $('transcript'); transcript.replaceChildren();
  if (!segments.length) {
    const empty = document.createElement('div'); empty.className = 'empty-note';
    const mark = document.createElement('span'); mark.className = 'empty-symbol'; mark.ariaHidden = 'true'; mark.textContent = '≋';
    const heading = document.createElement('h3'); heading.textContent = current && !recording && !starting ? '아직 받아쓴 내용이 없어요.' : '첫 문장을 기다리고 있어요.';
    const text = document.createElement('p'); text.textContent = recording || starting ? '목소리가 들어오면 이곳에 글이 나타나요.' : '수업 이름을 적고 받아쓰기를 시작해 보세요.'; empty.append(mark,heading,text); transcript.append(empty);
  } else {
    for (const segment of segments) {
      const row = document.createElement('div');
      const timed = hasSegmentStart(segment);
      row.className = `segment${timed ? '' : ' without-time'}`;
      const text = document.createElement('p'); text.textContent = segment.text;
      if (timed) { const time = document.createElement('time'); time.textContent = fmt(segment.start); row.append(time,text); }
      else row.append(text);
      transcript.append(row);
    }
  }
  $('segment-count').textContent = segments.length;
  $('transcript-title').textContent = correctionView === 'corrected' && correctionIsReady() ? 'AI 후보정본' : '받아쓴 원문';
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
  current = null; lectureDateFilter = ''; sampleSeconds = 0;
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
  const noteToolsBusy = busy || activeImport || importStarting || importCancelling;
  const hasTranscript = !!current?.segments?.length;
  $('lecture-date').disabled = busy;
  $('export-format').disabled = noteToolsBusy || !hasTranscript;
  $('download').disabled = noteToolsBusy || !hasTranscript;
  $('recording-download').disabled = noteToolsBusy || !current?.recording_available;
  $('recording-download').textContent = !current ? '↓ 녹음 WAV'
    : !current.recording_available ? '저장된 녹음 없음'
      : current.recording_finalized ? '↓ 녹음 WAV' : '녹음 WAV 마무리';
  $('delete-lecture').disabled = noteToolsBusy || correctionLoading || correctionStarting || !current;
  $('delete-close').disabled = deletingLecture;
  $('delete-cancel').disabled = deletingLecture;
  $('delete-confirm').disabled = deletingLecture;
  $('delete-confirm').textContent = deletingLecture ? '삭제하는 중…' : '수업 영구 삭제';
  $('lecture-title').disabled = busy || !!current; $('language').disabled = busy || !!current;
  $('audio-source').disabled = busy || !!current;
  $('record-button').disabled = authenticating || loggingOut || (starting && !recording) || stopping || activeImport || importStarting || recordingFinalizePending || (!recording && (!!draft || pending.length > 0 || sending));
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
  const liveAudioBusy = authenticating || loggingOut || recording || starting || stopping || !!draft || pending.length > 0 || sending || recordingFinalizePending;
  $('recording-file').disabled = liveAudioBusy || importStarting || (activeImport && !canResumeImport);
  $('import-button').disabled = liveAudioBusy || importStarting || (activeImport && !canResumeImport) || !$('recording-file').files?.length;
  $('import-button').textContent = canResumeImport ? '같은 파일 이어 올리기' : '파일 올려 변환';
  for (const button of $('lecture-list').querySelectorAll('button')) button.disabled = busy;
  updateCorrectionControls(noteToolsBusy);
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
  lectureDateFilter = ''; renderHistory();
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
  else if (lectureDateFilter) { lectureDateFilter = ''; renderHistory(); }
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
    lectureDateFilter = '';
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
    || (status >= 500 && status <= 599 && !PERMANENT_UPLOAD_STATUSES.has(status))
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
  if (error?.connectionLeaseExpired || error?.connectionChanged) return errorText(error);
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
        current.recording_available = !!response.recording_available;
        current.recording_finalized = !!response.recording_finalized;
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
function applyRecordingFlags(lectureId, result) {
  const state = {
    recording_available: !!result?.recording_available,
    recording_finalized: !!result?.recording_finalized,
  };
  const summary = lectures.find(lecture => lecture.id === lectureId);
  if (summary) Object.assign(summary,state);
  if (current?.id === lectureId) Object.assign(current,state);
  return state;
}
async function requestRecordingFinalization(lectureId, operationIsCurrent) {
  const path = `/lectures/${encodeURIComponent(lectureId)}/recording-finalize`;
  try {
    return await api(path, {method:'POST'});
  } catch (error) {
    if (!error?.transient || !operationIsCurrent()) throw error;
  }
  return api(path, {method:'POST'});
}
async function finalizeSkippedRecording(lectureId) {
  if (!lectureId || !token || recordingFinalizePending) return;
  const owner = user, sessionToken = token, server = apiUrl;
  const generation = requestGeneration, sequence = ++noteActionSequence;
  const operationIsCurrent = () => sequence === noteActionSequence && generation === requestGeneration
    && owner === user && sessionToken === token && server === apiUrl;
  recordingFinalizePending = true; updateControls();
  try {
    const result = await requestRecordingFinalization(lectureId, operationIsCurrent);
    if (!operationIsCurrent()) return;
    const state = applyRecordingFlags(lectureId,result);
    if (current?.id === lectureId) renderCurrent();
    renderHistory();
    notice(state.recording_available && state.recording_finalized
      ? '건너뛴 구간을 제외한 녹음을 WAV로 마무리했어요.'
      : '건너뛴 구간을 제외한 받아쓰기 기록을 저장했어요. 내려받을 녹음은 없습니다.');
  } catch (error) {
    if (operationIsCurrent()) notice(`받아쓰기 기록은 저장했지만 녹음 WAV를 마무리하지 못했습니다. ${errorText(error)}`);
  } finally {
    if (sequence === noteActionSequence) { recordingFinalizePending = false; updateControls(); }
  }
}
function skipFailedChunk() {
  if (!sendError || sending || starting || stopping || !pending[0]?.downloadRequested) return;
  const skipped = pending.shift();
  clearUploadRetry(); sendError = '';
  notice('파일 저장을 확인한 음성 조각을 건너뛰었어요. 해당 구간은 기록에서 빠집니다.');
  renderCurrent();
  if (!pending.some(chunk => chunk.lectureId === skipped.lectureId)) void finalizeSkippedRecording(skipped.lectureId);
  void drain();
}
$('skip-failed').onclick = skipFailedChunk;
$('download').onclick = () => {
  if (isBusy() || importIsActive() || importStarting || !current?.segments?.length) return;
  const format = $('export-format').value === 'text' ? 'text' : 'markdown';
  const extension = format === 'text' ? 'txt' : 'md';
  const type = format === 'text' ? 'text/plain;charset=utf-8' : 'text/markdown;charset=utf-8';
  const displayed = selectedTranscriptLecture();
  const url = URL.createObjectURL(new Blob(['\uFEFF',exportText(displayed,format)],{type}));
  const suffix = displayed.transcript_version === 'corrected' ? '_AI후보정' : '';
  const link = document.createElement('a'); link.href = url; link.download = `${safeFilename(current.title)}${suffix}.${extension}`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
};
async function downloadRecording() {
  if (isBusy() || importIsActive() || importStarting || !current?.recording_available) return;
  const lectureId = current.id, title = current.title, owner = user, sessionToken = token, server = apiUrl;
  const generation = requestGeneration, sequence = ++noteActionSequence;
  const operationIsCurrent = () => sequence === noteActionSequence && generation === requestGeneration
    && owner === user && sessionToken === token && server === apiUrl && current?.id === lectureId;
  recordingDownloadPending = true; updateControls();
  try {
    if (!current.recording_finalized) {
      const result = await requestRecordingFinalization(lectureId,operationIsCurrent);
      if (!operationIsCurrent()) return;
      const state = applyRecordingFlags(lectureId,result);
      renderCurrent(); renderHistory();
      if (!state.recording_available || !state.recording_finalized) throw new Error('내려받을 녹음을 준비하지 못했습니다.');
    }
    const ticket = await api(`/lectures/${encodeURIComponent(lectureId)}/recording-download-ticket`, {method:'POST'});
    if (!operationIsCurrent()) return;
    const link = document.createElement('a');
    link.href = nativeDownloadUrl(ticket?.path,server);
    link.download = `${safeFilename(title)}.wav`;
    link.rel = 'noreferrer'; link.referrerPolicy = 'no-referrer';
    link.click(); notice('녹음 WAV 다운로드를 시작했어요.');
  } catch (error) {
    if (operationIsCurrent()) {
      notice(`녹음을 내려받지 못했습니다. ${errorText(error)}`);
    }
  } finally {
    if (sequence === noteActionSequence) { recordingDownloadPending = false; updateControls(); }
  }
}
$('recording-download').onclick = () => { void downloadRecording(); };
function closeDeleteDialog() {
  if (deletingLecture) return;
  deleteTarget = null;
  if ($('delete-dialog').open) $('delete-dialog').close();
}
$('delete-lecture').onclick = () => {
  if (isBusy() || importIsActive() || importStarting || !current) return;
  deleteTarget = {id:current.id,title:current.title,owner:user,sessionToken:token,server:apiUrl};
  $('delete-lecture-title').textContent = current.title;
  $('delete-dialog').showModal(); $('delete-confirm').focus();
};
$('delete-close').onclick = closeDeleteDialog;
$('delete-cancel').onclick = closeDeleteDialog;
$('delete-dialog').oncancel = event => {
  if (deletingLecture) event.preventDefault();
  else deleteTarget = null;
};
async function requestLectureDeletion(target, operationIsCurrent) {
  const path = `/lectures/${encodeURIComponent(target.id)}`;
  try {
    await api(path, {method:'DELETE'});
    return;
  } catch (error) {
    if (!error?.transient || !operationIsCurrent()) throw error;
  }
  // A timeout or broken connection can hide a successful response. The server
  // keeps deletion idempotent, so one retry safely completes or confirms it.
  await api(path, {method:'DELETE'});
}
function finishDeletedLecture(target) {
  lectures = lectures.filter(lecture => lecture.id !== target.id);
  deleteTarget = null;
  if ($('delete-dialog').open) $('delete-dialog').close();
  if (current?.id === target.id) resetNewNote();
  else { renderCurrent(); renderHistory(); }
  notice('수업 기록과 저장된 녹음을 삭제했어요.');
}
$('delete-confirm').onclick = async () => {
  const target = deleteTarget;
  if (!target || deletingLecture || recordingDownloadPending || importIsActive() || importStarting
      || target.id !== current?.id || target.owner !== user || target.sessionToken !== token || target.server !== apiUrl) {
    closeDeleteDialog(); return;
  }
  const generation = ++requestGeneration, sequence = ++noteActionSequence;
  ++lectureRefreshGeneration;
  ++correctionSequence; clearCorrectionPoll();
  deletingLecture = true; updateControls();
  const operationIsCurrent = () => sequence === noteActionSequence && generation === requestGeneration
    && target.owner === user && target.sessionToken === token && target.server === apiUrl
    && current?.id === target.id;
  try {
    await requestLectureDeletion(target, operationIsCurrent);
    if (!operationIsCurrent()) return;
    finishDeletedLecture(target);
  } catch (error) {
    if (operationIsCurrent()) {
      deleteTarget = null;
      if ($('delete-dialog').open) $('delete-dialog').close();
      notice(`수업을 삭제하지 못했습니다. ${errorText(error)}`);
    }
  } finally {
    if (sequence === noteActionSequence) {
      deletingLecture = false;
      if (current?.id === target.id && ['queued','processing'].includes(correction?.status)) {
        scheduleCorrectionPoll(target.id);
      }
      updateControls();
    }
  }
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
  // Invitation fragments are easy to forge. Never let one choose the server
  // that receives a setup code and new password; users set that origin apart.
  if (invite.get('setup_code')) {
    setActivation(true);
    $('setup-code').value = invite.get('setup_code');
    // The server owns the account allow-list and validates the single-use code.
    // The client only restores the opaque invitation fields into the form.
    if (invite.get('username')) $('username').value = invite.get('username');
  }
  updateSourceGuidance();
  renderCurrent();
  await discoverServer();
}
void init();
