import { MicrophoneCapture } from './audio.js';
import { FileImportCancelledError, RecordingFileUploader, isTerminalImportState } from './file-import.js';
import { liveCoordination } from './live-coordination.js';
import {
  DurableLiveQueue,
  estimateStorage,
  isLiveQueueUnavailableError,
  requestPersistentStorage,
} from './live-queue.js';

const $ = id => document.getElementById(id);
let apiUrl = '', token = '', user = '', activation = false, lectures = [], current = null;
let capture = null, recording = false, paused = false, starting = false, pausing = false, resuming = false, stopping = false, sending = false, authenticating = false, loggingOut = false;
let pending = [], sendError = '', sampleSeconds = 0, timer = null, requestGeneration = 0;
let draft = null, captureSession = null, pausePromise = null, resumePromise = null, stopPromise = null;
let elapsedActiveMs = 0, elapsedStartedAt = 0;
let noticeTimer, statusTimer, statusSequence = 0, retryTimer = null, retryAttempt = 0, retryMessage = '';
let captureWarning = '';
let inputUnavailable = false, inputReconnectNeeded = false, inputUnavailableMessage = '';
let liveQueue = null, liveQueueReady = null, liveQueueAvailable = false;
let liveQueueRecoveryPromise = null;
let liveQueueWarning = '', liveQueueBytes = 0, liveQueuePersisting = 0;
const recoveryFinalizationRequired = new Set();
const manualRetryApprovedIds = new Set();
let liveCoordinationWarning = '';
let volatilePendingWarning = '';
let captureCoordinationLease = null;
let connectionRecoveryPromise = null, lastConnectionRecoveryAt = 0;
const liveSessions = new Map();
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
const scheduledCorrections = new Map();
let adminAuthorized = false, adminOverview = null, adminLoading = false, adminError = '', adminAction = '';
let adminSequence = 0, adminRefreshTimer = null, adminProbeTimer = null, adminConfirmation = null;
let tunnelRecoveryTimer = null, tunnelRecoveryDeadline = 0, tunnelRecoveryContext = null;
let presenceSequence = 0, presenceTimer = null, presenceIdleTimer = null, presenceSending = false;
let presenceLastSent = '', presenceQueued = '', lastPresenceInteraction = Date.now();
let connectionState = 'unverified', verifiedApiUrl = '', verifiedApiExpiresAt = 0;
let connectionGeneration = 0, connectionController = null, connectionLeaseTimer = null, leaseRefreshPromise = null;
let transcriptionProviders = {qwen:{configured:true},clova:{configured:false}};
// null means "automatic": prefer CLOVA for a new microphone lesson when the
// authenticated server advertises it, otherwise stay on the local Qwen path.
// Keep an explicit user choice only in this account session; never persist it
// in shared browser storage or let status polling rewrite an active lecture.
let micProviderPreference = null;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30000;
const MAX_RECORDING_FINALIZE_CONTENTION_RETRIES = 8;
const RETRYABLE_UPLOAD_STATUSES = new Set([408, 425, 429]);
const PERMANENT_UPLOAD_STATUSES = new Set([400, 401, 403, 404, 409, 413, 415, 422, 424, 507]);
const UPLOAD_TIMEOUT_MS = 60000;
const CONFIG_TIMEOUT_MS = 8000;
const RUNTIME_CONFIG_TTL_MS = 24 * 60 * 60 * 1000;
const CONFIG_CLOCK_SKEW_MS = 5 * 60 * 1000;
const CORRECTION_POLL_MS = 2500;
const ADMIN_REFRESH_MS = 10000;
const PRESENCE_INTERVAL_MS = 15000;
const PRESENCE_IDLE_MS = 5 * 60 * 1000;
const CLOVA_PRIVACY_NOTICE = '선택한 마이크 음성은 브라우저 → 이 서버 → 이 사이트 운영자가 관리하는 NAVER Cloud 계정의 CLOVA Speech 도메인으로 전송됩니다. 인식 결과는 운영자가 연결한 Object Storage에 자동 저장되며, 이 앱에서 수업을 삭제해도 그 클라우드 사본은 삭제되지 않습니다.';
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

function setLiveQueueWarning(error) {
  const unavailable = isLiveQueueUnavailableError(error);
  liveQueueWarning = unavailable
    ? '이 브라우저의 영구 임시 저장소를 사용할 수 없어 대기 음성을 현재 탭 메모리에 보관하고 있어요. 탭을 닫지 마세요.'
    : `${errorText(error)} 대기 음성은 현재 탭 메모리에 계속 보관하지만 탭을 닫으면 사라질 수 있어요.`;
  updateControls();
}

function noteCoordinationSupport(supported) {
  if (supported === false) {
    liveCoordinationWarning = '이 브라우저는 탭 간 녹음 잠금을 지원하지 않아요. 같은 계정의 실시간 수업은 반드시 한 탭에서만 열어 주세요.';
    updateControls();
  }
}

function activeCaptureInThisTab(owner = user) {
  return !!capture && !!captureSession && captureSession.owner === owner
    && (recording || paused || starting || pausing || resuming || stopping || inputUnavailable
      || capture.reconnectNeeded === true);
}

async function runWhenOwnerCaptureIdle(owner, lectureId, work) {
  const result = await liveCoordination.runDestructiveLectureAction(owner,lectureId,async () => {
    // Capture may already be stopped while its final queue is still uploading
    // in another tab. Serialize deletion/finalization with that uploader too,
    // so a late chunk cannot race the destructive server request.
    const coordinated = await liveCoordination.runUploader(owner,async () => {
      if (liveQueueRecoveryPromise) await liveQueueRecoveryPromise;
      if (pending.some(chunk => chunk.owner === owner
          && (chunk.captureId === lectureId || chunk.lectureId === lectureId))) {
        throw new Error('이 수업에 아직 서버가 확인하지 않은 음성이 있어 삭제하거나 마무리하지 않았어요. 먼저 전송 또는 실패 음성 처리를 끝내 주세요.');
      }
      const queue = await openLiveQueue();
      if (!queue) {
        throw new Error('기기의 미전송 음성 상태를 확인할 수 없어 삭제하거나 마무리하지 않았어요. 브라우저 저장소를 확인한 뒤 다시 시도해 주세요.');
      }
      if (await queue.hasPendingChunks(owner,lectureId)) {
        throw new Error('다른 탭이 보관 중인 미전송 음성이 있어 삭제하거나 마무리하지 않았어요. 해당 탭의 전송 또는 실패 음성 처리를 끝내 주세요.');
      }
      return work();
    });
    noteCoordinationSupport(coordinated.supported);
    return coordinated.value;
  },{
    hasActiveSession:() => activeCaptureInThisTab(owner),
  });
  noteCoordinationSupport(result.supported);
  if (!result.executed) {
    const error = new Error('같은 계정이 다른 탭에서 녹음 중일 수 있어 이 작업을 시작하지 않았어요. 그 수업 탭에서 녹음을 종료한 뒤 다시 시도해 주세요.');
    error.code = 'capture_active_elsewhere';
    throw error;
  }
  return result.value;
}

async function releaseSessionCaptureLease(session) {
  const lease = session?.captureLease;
  if (!lease) return;
  session.captureLease = null;
  if (captureCoordinationLease === lease) captureCoordinationLease = null;
  await lease.release?.().catch(() => {});
}

function hasVolatilePendingAudio(session) {
  return !!session && pending.some(chunk => chunk.captureId === session.id && !chunk.durable);
}

async function releaseStoppedSessionCaptureLeaseWhenSafe(session) {
  if (!session?.captureLease) return true;
  if (captureSession === session && capture) return false;
  // IndexedDB rows are visible to the destructive-action check in every tab.
  // RAM-only rows are not, so keep the cross-tab capture lock until each one
  // has either reached the server or been explicitly downloaded and skipped.
  if (hasVolatilePendingAudio(session)) return false;
  await releaseSessionCaptureLease(session);
  return true;
}

async function finishRemovedPendingChunk(chunk) {
  const session = liveSessions.get(chunk?.captureId);
  if (session) await releaseStoppedSessionCaptureLeaseWhenSafe(session);
  if (!pending.some(item => !item.durable && liveSessions.get(item.captureId)?.captureLease)) {
    volatilePendingWarning = '';
  }
  if (chunk?.final && !pending.some(item => item.captureId === chunk.captureId)) {
    if (session?.durable && liveQueueAvailable) {
      try {
        const cleanup = await liveQueue.deleteSession(session.owner,session.id);
        liveQueueBytes = Math.max(0,liveQueueBytes - (cleanup?.deletedBytes || 0));
      } catch (error) {
        setLiveQueueWarning(error);
      }
    }
    liveSessions.delete(chunk.captureId);
    recoveryFinalizationRequired.delete(chunk.captureId);
  }
}

function openLiveQueue() {
  if (liveQueueReady) return liveQueueReady;
  try { liveQueue = new DurableLiveQueue(); }
  catch (error) {
    setLiveQueueWarning(error);
    liveQueue = null;
    return Promise.resolve(null);
  }
  let opening;
  opening = liveQueue.open().then(() => {
    liveQueueAvailable = true;
    return liveQueue;
  }).catch(error => {
    liveQueueAvailable = false;
    setLiveQueueWarning(error);
    liveQueue = null;
    return null;
  }).finally(() => {
    // A failed/blocked IndexedDB open must not be cached for the entire page
    // lifetime. Later chunks may retry after another tab or transient browser
    // storage failure has cleared.
    if (!liveQueueAvailable && liveQueueReady === opening) liveQueueReady = null;
  });
  liveQueueReady = opening;
  return liveQueueReady;
}

async function refreshLiveQueueStats() {
  if (!user) return;
  const queue = await openLiveQueue();
  if (!queue) return;
  try {
    const stats = await queue.getStats(user);
    liveQueueBytes = stats.bytes;
  } catch (error) {
    setLiveQueueWarning(error);
  }
  updateControls();
}

async function prepareStoredLiveSession(session) {
  let stored = null, lastError = null;
  for (let attempt = 0; attempt < 4 && !stored; attempt += 1) {
    const queue = await openLiveQueue();
    if (queue) {
      try {
        stored = await queue.createSession({
          id:session.id,
          owner:session.owner,
          title:session.title,
          language:session.language,
          source:session.source,
          asrProvider:session.asrProvider,
          createdAt:session.createdAt,
        });
      } catch (error) {
        lastError = error;
      }
    } else {
      lastError = new Error('브라우저의 음성 임시 저장소를 아직 열지 못했습니다.');
    }
    if (!stored && attempt < 3) {
      await new Promise(resolve => setTimeout(resolve,500 * (2 ** attempt)));
    }
  }
  if (!stored) {
    session.durable = false;
    session.storageFailed = true;
    setLiveQueueWarning(lastError);
    return null;
  }
  session.durable = true;
  // This request is advisory. Refusal does not disable IndexedDB, but users
  // should know that the browser may evict queued audio under storage pressure.
  void requestPersistentStorage().then(result => {
    if (result.supported && !result.persisted) {
      liveQueueWarning = '대기 음성은 이 기기에 임시 보관되지만 브라우저가 영구 저장을 허용하지 않았어요. 저장 공간을 충분히 유지하고 탭을 닫지 마세요.';
      updateControls();
    }
  }).catch(() => {
    liveQueueWarning = '대기 음성은 IndexedDB에 보관하지만 브라우저의 영구 저장 허용 여부를 확인하지 못했어요. 저장 공간을 충분히 유지하고 전송이 끝나기 전에는 사이트 데이터를 지우지 마세요.';
    updateControls();
  });
  return stored;
}

function storedChunkInput(chunk) {
  return {
    id:chunk.id,
    startSamples:Math.max(0,Math.round(chunk.startSeconds * 16000)),
    durationSamples:Math.max(0,Math.round(chunk.durationSeconds * 16000)),
    overlapSamples:Math.max(0,Math.round((chunk.overlapSeconds || 0) * 16000)),
    final:!!chunk.final,
    blob:chunk.blob,
  };
}

function persistPendingChunk(session, item) {
  liveQueuePersisting += 1;
  const previous = session.persistChain || Promise.resolve();
  item.persistPromise = session.persistChain = previous.then(async () => {
    await session.storeReady;
    if (!liveQueueAvailable || !session.durable || session.storageFailed) return;
    let stored = null, lastError = null;
    for (let attempt = 0; attempt < 4 && !stored; attempt += 1) {
      try {
        stored = await liveQueue.enqueueChunk(session.owner,session.id,storedChunkInput(item));
      } catch (error) {
        lastError = error;
        if (attempt < 3) await new Promise(resolve => setTimeout(resolve,500 * (2 ** attempt)));
      }
    }
    if (!stored) {
      session.storageFailed = true;
      item.durable = false;
      setLiveQueueWarning(lastError);
      return;
    }
    item.durable = true;
    if (Number.isSafeInteger(item.sequence) && stored.sequence !== item.sequence) {
      throw new Error('기기에 저장된 음성 순서가 현재 녹음 순서와 맞지 않습니다.');
    }
    item.sequence = stored.sequence;
    item.sessionCreatedAt = stored.sessionCreatedAt;
    item.byteLength = stored.byteLength;
    // IndexedDB now owns the large Blob. Keep only metadata in JS memory.
    item.blob = null;
    liveQueueBytes += stored.byteLength;
  }).catch(error => {
    session.storageFailed = true;
    item.durable = false;
    setLiveQueueWarning(error);
  }).finally(() => {
    liveQueuePersisting = Math.max(0,liveQueuePersisting - 1);
    updateControls();
  });
  return item.persistPromise;
}

function persistLiveSessionState(session, state) {
  if (!session) return Promise.resolve();
  const previous = session.persistChain || Promise.resolve();
  session.persistChain = previous.then(async () => {
    await session.storeReady;
    if (!liveQueueAvailable || !session.durable || session.storageFailed) return;
    await liveQueue.updateSession(session.owner,session.id,{state});
  }).catch(error => setLiveQueueWarning(error));
  return session.persistChain;
}

async function pendingChunkBlob(chunk) {
  await chunk.persistPromise;
  if (chunk.blob instanceof Blob) return chunk.blob;
  if (!chunk.durable || !liveQueueAvailable || !user) {
    throw new Error('기기에 임시 보관한 음성 조각을 읽지 못했습니다.');
  }
  const stored = await liveQueue.getChunk(user,chunk.id);
  if (!stored?.blob) {
    const error = new Error('기기에 임시 보관한 음성 조각을 찾지 못했습니다.');
    error.chunkAlreadyHandled = true;
    throw error;
  }
  return stored.blob;
}

async function acknowledgePendingChunk(chunk, { strict = false } = {}) {
  manualRetryApprovedIds.delete(chunk?.id);
  if (strict && chunk?.durable && (!chunk.owner
      || (!liveQueueAvailable && !(await openLiveQueue())))) {
    throw new Error('기기에 보관된 음성의 삭제를 확인할 수 없어 건너뛰지 않았어요. 브라우저 저장소를 확인한 뒤 다시 시도해 주세요.');
  }
  if (!chunk?.durable || !liveQueueAvailable || !chunk.owner) {
    return;
  }
  let lastError = null;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const result = await liveQueue.ackChunk(chunk.owner,chunk.id);
      liveQueueBytes = Math.max(0,liveQueueBytes - (result?.chunk?.byteLength || chunk.byteLength || 0));
      return;
    } catch (error) {
      lastError = error;
      if (attempt < 3) await new Promise(resolve => setTimeout(resolve,500 * (2 ** attempt)));
    }
  }
  // The server already acknowledged this stable UUID. Do not resend solely
  // because local cleanup failed; an orphan can be reconciled after reload.
  setLiveQueueWarning(lastError);
  // A user-requested skip has no server ACK to make a retained durable row
  // harmless. Keep the RAM/error gate until IndexedDB confirms deletion, or a
  // reload could resurrect and later submit audio the user explicitly skipped.
  if (strict) throw lastError;
}

async function markPendingBlocked(chunk, error) {
  if (chunk) chunk.blocked = true;
  if (!chunk?.durable || !liveQueueAvailable || !chunk.owner) return;
  const raw = String(error?.code || error?.status || 'upload_error').toLowerCase();
  const kind = /^[a-z][a-z0-9_-]{0,63}$/.test(raw) ? raw : 'upload_error';
  try { await liveQueue.markChunkBlocked(chunk.owner,chunk.id,kind); }
  catch (storeError) { setLiveQueueWarning(storeError); }
}

async function markPendingQueued(chunk) {
  if (chunk?.durable && liveQueueAvailable && chunk.owner) {
    await liveQueue.markChunkQueued(chunk.owner,chunk.id);
  }
  if (chunk) { chunk.blocked = false; chunk.inflight = false; }
}

async function markPendingInflight(chunk) {
  await chunk?.persistPromise;
  if (!chunk?.durable || !liveQueueAvailable || !chunk.owner) {
    throw new Error('CLOVA 전송 상태를 기기에 안전하게 저장하지 못해 음성을 보내지 않았어요. 브라우저 저장 공간을 확인해 주세요.');
  }
  await liveQueue.markChunkInflight(chunk.owner,chunk.id);
  chunk.inflight = true;
}

function lectureForStoredProvider(session, candidate) {
  if (!candidate || candidate.id !== session.id) return null;
  const declared = candidate.asr_provider;
  const legacyMissingQwen = session.asrProvider === 'qwen'
    && (declared === undefined || declared === null || declared === '');
  const provider = ['qwen','clova'].includes(declared)
    ? declared : legacyMissingQwen ? 'qwen' : '';
  if (provider !== session.asrProvider) return null;
  candidate.asr_provider = provider;
  return candidate;
}

function runtimeSessionFromStored(stored) {
  const candidate = lectures.find(item => item.id === stored.id) || null;
  const lecture = lectureForStoredProvider(stored,candidate);
  return {
    id:stored.id,
    owner:stored.owner,
    lecture,
    buffered:[],
    title:stored.title,
    language:stored.language,
    source:stored.source,
    asrProvider:stored.asrProvider,
    cancelled:true,
    creating:null,
    assignmentTimer:null,
    assignmentAttempt:0,
    createdAt:stored.createdAt,
    nextRuntimeSequence:stored.nextSequence,
    storedState:stored.state,
    lectureCreated:stored.lectureCreated,
    persistChain:Promise.resolve(),
    storeReady:Promise.resolve(stored),
    durable:true,
    recovered:true,
    providerMismatch:!!candidate && !lecture,
  };
}

function recoverDurableLiveAudio(owner) {
  if (liveQueueRecoveryPromise) return liveQueueRecoveryPromise;
  let tracked;
  tracked = performDurableLiveAudioRecovery(owner).finally(() => {
    if (liveQueueRecoveryPromise === tracked) liveQueueRecoveryPromise = null;
    updateControls();
    if (token && pending.length && !sendError) void drain();
  });
  liveQueueRecoveryPromise = tracked;
  return tracked;
}

async function performDurableLiveAudioRecovery(owner) {
  const queue = await openLiveQueue();
  if (!queue || !owner || owner !== user) return;
  let recovered;
  try { recovered = await queue.recoverOwner(owner); }
  catch (error) {
    setLiveQueueWarning(error);
    return;
  }
  if (owner !== user) return;
  recoveryFinalizationRequired.clear();
  liveQueueBytes = recovered.stats.bytes;
  const byId = new Map(recovered.sessions.map(stored => {
    let session = liveSessions.get(stored.id);
    if (!session) {
      session = runtimeSessionFromStored(stored);
      liveSessions.set(stored.id,session);
    } else {
      const candidate = lectures.find(item => item.id === stored.id) || null;
      session.lecture = lectureForStoredProvider(session,candidate);
      session.providerMismatch = !!candidate && !session.lecture;
      session.createdAt = Number.isSafeInteger(session.createdAt) ? session.createdAt : stored.createdAt;
      session.nextRuntimeSequence = Math.max(
        Number.isSafeInteger(session.nextRuntimeSequence) ? session.nextRuntimeSequence : 0,
        stored.nextSequence,
      );
      if (session.recovered) {
        session.storedState = stored.state;
      }
    }
    return [stored.id,session];
  }));
  const knownChunks = new Map(pending.map(chunk => [chunk.id,chunk]));
  for (const stored of recovered.chunks) {
    const existing = knownChunks.get(stored.id);
    if (existing) {
      const session = byId.get(stored.captureId);
      existing.durable = true;
      existing.sequence = stored.sequence;
      existing.sessionCreatedAt = stored.sessionCreatedAt;
      existing.byteLength = stored.byteLength;
      existing.blocked = stored.state === 'blocked' || stored.state === 'inflight';
      existing.inflight = stored.state === 'inflight';
      existing.downloadRequested = existing.downloadRequested || stored.downloadRequested;
      existing.lectureReady = !!session?.lecture;
      continue;
    }
    const session = byId.get(stored.captureId);
    if (!session) {
      setLiveQueueWarning(new Error('기기에 보관된 음성의 수업 정보를 찾지 못했습니다.'));
      continue;
    }
    pending.push({
      id:stored.id,
      captureId:stored.captureId,
      lectureId:stored.captureId,
      owner:stored.owner,
      asrProvider:stored.asrProvider,
      startSeconds:stored.startSamples / 16000,
      durationSeconds:stored.durationSamples / 16000,
      overlapSeconds:stored.overlapSamples / 16000,
      final:stored.final,
      blob:null,
      byteLength:stored.byteLength,
      sequence:stored.sequence,
      durable:true,
      lectureReady:!!session.lecture,
      downloadRequested:stored.downloadRequested,
      persistPromise:Promise.resolve(),
      blocked:stored.state === 'blocked' || stored.state === 'inflight',
      inflight:stored.state === 'inflight',
    });
  }
  const recoveredSessionCreatedAt = new Map(recovered.sessions.map(item => [item.id,item.createdAt]));
  const insertionOrder = new Map(pending.map((chunk,index) => [chunk,index]));
  pending.sort((left,right) => {
    const leftCreatedAt = Number.isSafeInteger(left.sessionCreatedAt)
      ? left.sessionCreatedAt : recoveredSessionCreatedAt.get(left.captureId) || 0;
    const rightCreatedAt = Number.isSafeInteger(right.sessionCreatedAt)
      ? right.sessionCreatedAt : recoveredSessionCreatedAt.get(right.captureId) || 0;
    const sessionOrder = leftCreatedAt - rightCreatedAt || left.captureId.localeCompare(right.captureId);
    if (sessionOrder) return sessionOrder;
    if (Number.isSafeInteger(left.sequence) && Number.isSafeInteger(right.sequence)) {
      const sequenceOrder = left.sequence - right.sequence;
      if (sequenceOrder) return sequenceOrder;
    }
    // Older in-memory items may predate runtime sequence assignment. Preserve
    // their append order instead of using random UUIDs as a timeline.
    return insertionOrder.get(left) - insertionOrder.get(right);
  });
  for (const [id,session] of byId) {
    const sessionChunks = pending.filter(chunk => chunk.captureId === id);
    const stillCapturedHere = session === captureSession && !!capture
      && (recording || paused || starting || pausing || resuming || inputUnavailable);
    const recoveredActiveSession = session.recovered && !stillCapturedHere
      && ['recording','paused','input-unavailable'].includes(session.storedState);
    if (recoveredActiveSession) recoveryFinalizationRequired.add(id);
    if (stillCapturedHere && !sessionChunks.length) continue;
    if (!sessionChunks.length) {
      if (session.lecture?.recording_finalized === true) {
        recoveryFinalizationRequired.delete(id);
        liveSessions.delete(id);
        await queue.deleteSession(owner,id).catch(() => {});
        continue;
      }
      if (['recording','paused','input-unavailable'].includes(session.storedState)) {
        // It can belong to another still-live tab/device. A Web Lock prevents
        // new destructive work in this browser but is not persisted in this
        // record, so recovery never guesses that an active row is stale.
        continue;
      } else {
        recoveryFinalizationRequired.delete(id);
        liveSessions.delete(id);
        await queue.deleteSession(owner,id).catch(() => {});
      }
      continue;
    }
    if (session.lecture) {
      for (const chunk of sessionChunks) chunk.lectureReady = true;
      if (!recovered.sessions.find(item => item.id === id)?.lectureCreated) {
        await queue.updateSession(owner,id,{lectureCreated:true}).catch(() => {});
      }
    } else {
      void ensureLectureAssigned(session);
    }
  }
  const blocked = pending[0]?.owner === owner && pending[0].blocked ? pending[0] : null;
  if (blocked && !sendError) {
    sendError = blocked.inflight
      ? '브라우저가 닫히기 직전에 보낸 CLOVA 음성은 처리 여부를 확인할 수 없어 자동 재전송하지 않았어요.'
      : blocked.asrProvider === 'clova'
      ? '기기에 보관된 CLOVA 음성 조각은 이전 응답을 확인하지 못해 자동 재전송하지 않았어요.'
      : '기기에 보관된 음성 조각에 확인이 필요한 오류가 있어요.';
  }
  try {
    const estimate = await estimateStorage();
    if (estimate.remaining !== null && estimate.remaining < Math.max(32 * 1024 * 1024,liveQueueBytes * 2)) {
      liveQueueWarning = '브라우저의 남은 저장 공간이 적어요. 대기 음성을 서버로 보낼 때까지 기기 공간을 확보해 주세요.';
    }
  } catch { /* Storage estimates are advisory. */ }
  if (await queue.hasWorkForOtherOwner(owner).catch(() => false)) {
    notice('이 기기에 다른 계정의 미전송 음성이 별도로 보관되어 있어요. 해당 계정으로 로그인해야 전송할 수 있습니다.');
  }
  updateControls();
}
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
  const uploaderAlreadyLocked = options.uploaderAlreadyLocked === true;
  const {
    anonymous:_anonymous,
    uploaderAlreadyLocked:_uploaderAlreadyLocked,
    ...requestOptions
  } = options;
  const requestedServer = baseUrl || apiUrl, requestedToken = token;
  if (!anonymous) {
    if (!hasRequestableServer()) await ensureTrustedApiRequest({uploaderAlreadyLocked});
    if (!hasRequestableServer() || apiUrl !== requestedServer || token !== requestedToken) {
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
function hasRequestableServer() {
  // While a replacement tunnel is checked anonymously, the already verified
  // origin remains the only destination allowed to receive the active token.
  return (connectionState === 'connected' || connectionState === 'checking')
    && hasTrustedApiOrigin();
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
    const leaseOwner = user;
    setConnectionState('unverified','자동 서버 주소가 만료되어 새 연결을 확인하고 있어요.');
    // A timer may fire while a live chunk is already awaiting its response.
    // Let that uploader merge and ACK the trusted old-origin response before
    // installing a replacement origin and clearing its token. Capture itself
    // remains independent and continues filling the local durable queue.
    void refreshExpiredAutomaticServerAfterUploads(leaseOwner).catch(() => {});
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
async function fetchRuntimeConfig(signal, timeout = CONFIG_TIMEOUT_MS) {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(signal?.reason);
  if (signal?.aborted) abortFromCaller();
  else signal?.addEventListener?.('abort',abortFromCaller,{once:true});
  const boundedTimeout = Math.max(1,Math.min(CONFIG_TIMEOUT_MS,Number(timeout) || CONFIG_TIMEOUT_MS));
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); },boundedTimeout);
  try {
    // Pages/CDN caches can briefly retain the previous tunnel after a publish.
    // The query contains no user data; it only makes each reload a fresh lookup.
    const response = await fetch(`./config.json?v=${Date.now()}`,{cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer',signal:controller.signal});
    if (!response.ok) {
      const transient = response.status === 408 || response.status === 429 || response.status >= 500;
      throw runtimeConfigError(transient ? 'config-unavailable' : 'malformed',
        `자동 연결 설정을 불러오지 못했습니다 (${response.status}).`,transient);
    }
    try { return await response.json(); }
    catch (error) {
      if (controller.signal.aborted) throw error;
      throw runtimeConfigError('malformed','자동 연결 설정을 읽지 못했습니다.');
    }
  } catch (error) {
    if (signal?.aborted) throw error;
    if (error?.discoveryCode) throw error;
    throw runtimeConfigError('config-unavailable',timedOut
      ? '자동 연결 설정을 불러오는 데 시간이 오래 걸리고 있습니다.'
      : '자동 연결 설정을 불러오지 못했습니다.',true);
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener?.('abort',abortFromCaller);
  }
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
    const preserveOwner = !!user && hasOwnerLockedWork();
    const hasExistingSession = !!before || !!token || !!user || hasOwnerLockedWork();
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
async function discoverServer({deadline = 0, serializeOwner = ''} = {}) {
  const retainedVerifiedOrigin = connectionState === 'connected' && hasVerifiedServer() ? apiUrl : '';
  cancelConnectionAttempt();
  const sequence = connectionGeneration;
  const controller = new AbortController();
  connectionController = controller;
  const operationIsCurrent = () => sequence === connectionGeneration && connectionController === controller;
  const remainingTimeout = fallback => deadline > 0
    ? Math.max(1,Math.min(fallback,deadline - Date.now())) : fallback;
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
      candidate = await verifyServerCandidate(localCandidate,controller.signal,remainingTimeout(10000));
      connectedMessage = '로컬 서버 연결을 확인했어요. 로그인할 수 있습니다.';
    } else {
      // A saved Quick Tunnel cannot safely replace the same-origin runtime
      // lease: it may be expired, stopped, or later reassigned. Keep it only as
      // a manual prefill when Pages config itself is unavailable.
      const config = await fetchRuntimeConfig(controller.signal,remainingTimeout(CONFIG_TIMEOUT_MS));
      if (!operationIsCurrent()) return;
      const runtime = validateRuntimeConfig(config);
      if (runtime.state === 'offline') throw runtimeConfigError('offline','서버가 꺼져 있습니다.');
      expiresAt = runtime.expiresAt;
      $('api-url').value = runtime.apiUrl;
      try { candidate = await verifyServerCandidate(runtime.apiUrl,controller.signal,remainingTimeout(10000)); }
      catch (error) {
        if (!operationIsCurrent()) return;
        throw runtimeConfigError('unreachable',errorText(error));
      }
      connectedMessage = '현재 서버를 자동으로 찾아 연결을 확인했어요. 로그인할 수 있습니다.';
    }
    if (!operationIsCurrent()) return;
    const installCandidate = () => {
      if (!operationIsCurrent() || (serializeOwner && user !== serializeOwner)) return false;
      installVerifiedServer(candidate,connectedMessage,{expiresAt});
      return true;
    };
    if (serializeOwner) {
      const coordinated = await liveCoordination.runUploader(serializeOwner,installCandidate);
      noteCoordinationSupport(coordinated.supported);
      if (!coordinated.value) return;
    } else {
      installCandidate();
    }
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
  const preserveOwner = !!owner && hasOwnerLockedWork();
  const hadSession = !!token || !!owner || hasOwnerLockedWork() || !$('workspace').hidden;
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
async function refreshExpiredAutomaticServerAfterUploads(owner) {
  if (!owner) return refreshExpiredAutomaticServer();
  const coordinated = await liveCoordination.runUploader(
    owner,
    () => refreshExpiredAutomaticServer(),
  );
  noteCoordinationSupport(coordinated.supported);
  return coordinated.value;
}
async function ensureTrustedApiRequest({uploaderAlreadyLocked = false} = {}) {
  if (hasRequestableServer()) return apiUrl;
  if (automaticLeaseExpired()) {
    // Ordinary status/history/import calls can become the first overdue timer
    // to notice an expired lease. They must wait behind an in-flight live WAV
    // before replacing its origin. Calls already inside that uploader lock use
    // the plain refresh path to avoid acquiring the same exclusive lock twice.
    if (!uploaderAlreadyLocked && user) {
      return refreshExpiredAutomaticServerAfterUploads(user);
    }
    return refreshExpiredAutomaticServer();
  }
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
function scrubAccountWorkspace({ clearLoginIdentity = false } = {}) {
  current = null; lectures = []; lectureDateFilter = '';
  sampleSeconds = 0; elapsedActiveMs = 0; elapsedStartedAt = 0; captureWarning = '';
  inputUnavailable = false; inputReconnectNeeded = false; inputUnavailableMessage = '';
  liveQueueBytes = 0; liveSessions.clear(); recoveryFinalizationRequired.clear(); manualRetryApprovedIds.clear();
  if (!capture && !draft && !pending.length && !sending) captureSession = null;
  if (!pending.length) { sendError = ''; clearUploadRetry(); }
  $('recording-file').value = '';
  transcriptionProviders = {qwen:{configured:true},clova:{configured:false}};
  micProviderPreference = null;
  $('lecture-title').value = ''; $('language').value = 'ko'; $('audio-source').value = 'microphone';
  $('asr-provider').value = 'qwen'; $('asr-provider-clova').disabled = true;
  $('asr-provider-clova').textContent = 'NAVER CLOVA Speech · 운영자 설정 필요';
  $('elapsed').textContent = '00:00';
  $('current-user').textContent = '내 계정'; document.querySelector('.user-avatar').textContent = '–';
  $('delete-lecture-title').textContent = '선택한 수업';
  $('import-status').hidden = true; $('import-state').textContent = '파일을 준비하고 있어요';
  $('import-detail').textContent = ''; $('import-progress').value = 0; $('import-progress').textContent = '0%';
  $('queue-message').textContent = '';
  clearTimeout(noticeTimer); noticeTimer = null; $('notice').textContent = ''; $('notice').hidden = true;
  if (clearLoginIdentity) {
    $('username').value = ''; $('password').value = ''; $('password-confirm').value = ''; $('setup-code').value = '';
  }
  resetCorrectionState('');
  updateSourceGuidance();
  renderCurrent(); renderHistory();
}
function showLogin(clear = true) {
  const preserveOwner = !!user && hasOwnerLockedWork();
  const retainWorkspaceState = !clear || preserveOwner;
  ++requestGeneration;
  ++noteActionSequence;
  if (!retainWorkspaceState) scheduledCorrections.clear();
  else retainScheduledCorrectionsForLogin();
  resetAdminState(); resetPresence();
  recordingDownloadPending = false; recordingFinalizePending = false; deletingLecture = false; deleteTarget = null;
  if ($('delete-dialog').open) $('delete-dialog').close();
  // Losing an HTTP session must not end a class. The microphone may continue
  // writing durable local chunks while the same owner signs in again; no old
  // bearer token is ever copied to a replacement tunnel origin.
  if (!preserveOwner && (recording || paused || starting || pausing || resuming)) {
    void stopRecording('로그인이 만료되어 받아쓰기를 마무리했어요.');
  }
  detachImportWatcher();
  $('workspace').hidden = true; $('auth-screen').hidden = false; clearInterval(statusTimer);
  if (!retainWorkspaceState) { user = ''; scrubAccountWorkspace({clearLoginIdentity:true}); }
  else resetCorrectionState(current?.id || '');
  setActivation(false);
  $('auth-capture-stop').hidden = !preserveOwner || !capture;
  if (preserveOwner) {
    $('username').value = user;
    $('auth-description').textContent = recording
      ? '녹음은 이 기기에 계속 임시 보관 중입니다. 같은 계정으로 다시 로그인하면 서버 전송을 이어갑니다.'
      : '전송 대기 중인 음성이 이 기기에 보관되어 있습니다. 같은 계정으로 다시 로그인해 주세요.';
  }
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
  const connectionOwner = user;
  $('connection-error').hidden = true; $('api-url').removeAttribute('aria-invalid');
  try {
    if (authenticating || loggingOut || starting || pausing || resuming || stopping || sending
        || importTransportBusy() || recordingDownloadPending || recordingFinalizePending || deletingLecture) {
      throw new Error('진행 중인 연결 작업이나 파일 전송이 끝난 뒤 서버 주소를 바꿔 주세요.');
    }
    const candidate = normalizeUrl($('api-url').value);
    // A dead Quick Tunnel must not strand the in-memory queue. Stop its old
    // retry timer, verify the candidate anonymously, then require the same
    // account to log in before stable chunk UUIDs are sent to the new origin.
    clearUploadRetry();
    cancelConnectionAttempt(); sequence = connectionGeneration;
    controller = new AbortController(); connectionController = controller;
    const operationIsCurrent = () => sequence === connectionGeneration && connectionController === controller;
    $('connection-save').disabled = true; $('api-url').disabled = true;
    const verifyAndInstall = async () => {
      if (!operationIsCurrent() || user !== connectionOwner) return false;
      setConnectionState('checking');
      // Keep the active origin unchanged while checking the candidate. Otherwise
      // a status timer could send the current Bearer token to an untrusted host.
      await verifyServerCandidate(candidate,controller.signal);
      if (!operationIsCurrent() || user !== connectionOwner) return false;
      changed = installVerifiedServer(candidate,'입력한 서버 연결을 확인했어요. 로그인할 수 있습니다.');
      return true;
    };
    if (connectionOwner) {
      // Pause only network submission, not capture. This lock spans anonymous
      // health verification and origin installation, so an old-origin upload
      // cannot begin or finish in the middle of the switch in any same-origin tab.
      const coordinated = await liveCoordination.runUploader(connectionOwner,verifyAndInstall);
      noteCoordinationSupport(coordinated.supported);
      if (!coordinated.value) return;
    } else if (!await verifyAndInstall()) {
      return;
    }
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
    const ownerLocked = hasOwnerLockedWork();
    if (ownerLocked && user && username !== user) throw new Error('전송 대기 중인 음성이 있어요. 이전 계정으로 다시 로그인해 주세요.');
    if (stopPromise) await stopPromise;
    if (activation && password !== $('password-confirm').value) throw new Error('입력한 두 비밀번호가 일치하지 않아요.');
    const body = {username,password}; if (activation) body.setup_code = $('setup-code').value.trim();
    const response = await api(activation ? '/auth/activate' : '/auth/login', {method:'POST',body:JSON.stringify(body)});
    if (apiUrl !== authServer || requestGeneration !== generation) throw new Error('서버 연결 상태가 바뀌어 로그인 응답을 적용하지 않았어요. 다시 로그인해 주세요.');
    const previousUser = user;
    token = response.token; user = response.user.username; ++requestGeneration;
    if (previousUser !== user) {
      scheduledCorrections.clear();
      scrubAccountWorkspace();
    } else {
      rebindScheduledCorrectionsToLogin();
    }
    $('password').value = ''; $('password-confirm').value = ''; $('setup-code').value = '';
    try {
      await refreshLectures();
      await recoverDurableLiveAudio(user);
      await recoverFileImport();
    } catch (error) { notice(errorText(error)); }
    if (!token) return;
    // Resolve the authenticated provider list before enabling the workspace.
    // Otherwise a fast first click could start Qwen while the CLOVA-default
    // status request is still in flight.
    const statusToken = token, statusUser = user;
    await updateStatus();
    if (!token || token !== statusToken || user !== statusUser || apiUrl !== authServer) return;
    $('auth-capture-stop').hidden = true;
    $('auth-screen').hidden = true; $('workspace').hidden = false; $('current-user').textContent = user;
    document.querySelector('.user-avatar').textContent = user[0].toUpperCase();
    renderCurrent();
    if (current?.id && current.recording_finalized === true && current.segments?.length) void loadCorrection(current.id);
    startPresence();
    if (response.user?.is_admin !== false) void loadAdminOverview({probe:true});
    clearInterval(statusTimer); statusTimer = setInterval(updateStatus, 20000);
    if (pending.length || draft) {
      if (clovaManualRetryRequired()) {
        notice('CLOVA 대기 음성은 중복 기록을 막기 위해 자동 재전송하지 않았어요. 안내를 확인한 뒤 직접 선택해 주세요.');
      } else {
        sendError = ''; void retryPending();
      }
    }
    resumeFinalizedScheduledCorrections();
  } catch (error) {
    $('password').value = ''; $('password-confirm').value = '';
    $('auth-error').textContent = errorText(error); $('auth-error').hidden = false;
  }
  finally { authenticating = false; updateAuthControls(); updateControls(); }
};
async function updateStatus() {
  if (!token) return;
  const sequence = ++statusSequence, statusToken = token, statusUser = user, statusServer = apiUrl;
  const requestIsCurrent = () => sequence === statusSequence && token === statusToken
    && user === statusUser && apiUrl === statusServer;
  try {
    const status = await api('/status');
    if (!requestIsCurrent()) return;
    const advertised = status?.transcription_providers;
    const qwenConfigured = advertised?.qwen?.configured !== false;
    const clovaConfigured = advertised?.clova?.configured === true;
    transcriptionProviders = {qwen:{configured:qwenConfigured},clova:{configured:clovaConfigured}};
    $('asr-provider-qwen').disabled = !qwenConfigured;
    $('asr-provider-clova').disabled = !clovaConfigured;
    $('asr-provider-clova').textContent = clovaConfigured
      ? 'NAVER CLOVA Speech · 운영자 클라우드 · 기본' : 'NAVER CLOVA Speech · 운영자 설정 필요';
    applyNewLectureProvider();
    $('model-status').textContent = ({unloaded:'첫 받아쓰기 준비됨',loading:'음성 인식 모델 준비 중',ready:'음성 인식 모델 연결됨',error:'음성 인식 모델 확인 필요'})[status.model_state] || '서버 연결됨';
    updateProviderGuidance(); updateControls();
  } catch {
    if (!requestIsCurrent()) return;
    transcriptionProviders = {qwen:{configured:true},clova:{configured:false}};
    $('asr-provider-qwen').disabled = false; $('asr-provider-clova').disabled = true;
    $('asr-provider-clova').textContent = 'NAVER CLOVA Speech · 운영자 설정 필요';
    applyNewLectureProvider();
    updateProviderGuidance(); updateControls();
    $('model-status').textContent = '서버 연결 확인 필요';
  }
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
function queuedCount() { return pending.length; }
function clovaManualRetryRequired() {
  return !!sendError && (draft?.asrProvider === 'clova'
    || pending.some(chunk => chunk.asrProvider === 'clova'));
}
function ownerScheduledCorrectionPending(inFlightOnly = false) {
  return [...scheduledCorrections.values()].some(scheduled =>
    scheduled.owner === user && (scheduled.status === 'starting'
      || (!inFlightOnly && scheduled.status === 'scheduled')));
}
function isBusy() { return authenticating || loggingOut || recording || paused || starting || pausing || resuming || stopping || !!draft || pending.length > 0 || sending || importTransportBusy() || recordingDownloadPending || recordingFinalizePending || deletingLecture || ownerScheduledCorrectionPending(true); }
function hasOwnerLockedWork() {
  return recording || paused || starting || pausing || resuming || stopping || !!capture
    || !!draft || pending.length > 0 || sending || recordingFinalizePending || ownerScheduledCorrectionPending();
}
function retainScheduledCorrectionsForLogin() {
  for (const [lectureId,scheduled] of scheduledCorrections) {
    if (!user || scheduled.owner !== user) {
      scheduledCorrections.delete(lectureId);
      continue;
    }
    // Keep only the owner/capture reservation while signed out. The expired
    // bearer and previous origin are neither needed nor retained in memory.
    scheduled.sessionToken = '';
    scheduled.server = '';
    if (scheduled.status === 'starting') {
      // A changed/expired credential makes the outcome of the old request
      // unusable. Replaying after the same owner logs in is server-idempotent.
      scheduled.status = 'scheduled';
    }
  }
}
function rebindScheduledCorrectionsToLogin() {
  for (const [lectureId,scheduled] of scheduledCorrections) {
    if (!user || scheduled.owner !== user) {
      scheduledCorrections.delete(lectureId);
      continue;
    }
    scheduled.sessionToken = token;
    scheduled.server = apiUrl;
    if (scheduled.status === 'starting') scheduled.status = 'scheduled';
  }
}
function resumeFinalizedScheduledCorrections() {
  for (const lectureId of scheduledCorrections.keys()) {
    const active = captureSession?.lecture?.id === lectureId ? captureSession.lecture : null;
    const selected = current?.id === lectureId ? current : null;
    const summary = lectures.find(lecture => lecture.id === lectureId);
    if ([active,selected,summary].some(lecture => lecture?.recording_finalized === true)) {
      void submitScheduledCorrection(lectureId);
    }
  }
}
function releaseFinalizedCapture(lectureId) {
  if (captureSession?.lecture?.id !== lectureId) return;
  const summary = lectures.find(lecture => lecture.id === lectureId);
  if (summary && summary !== current && Array.isArray(summary.segments)) {
    summary.segment_count = summary.segments.length;
    delete summary.segments;
  }
  captureSession = null;
}
function activeCaptureLectureId() { return captureSession?.lecture?.id || ''; }
function hasLiveCaptureSession() {
  return !!activeCaptureLectureId() && !!capture
    && (recording || paused || starting || pausing || resuming || stopping);
}
function stableLiveCapture() {
  return !!activeCaptureLectureId() && !!capture && (recording || paused)
    && !starting && !pausing && !resuming && !stopping && !draft;
}
function viewingActiveCaptureLecture() {
  return stableLiveCapture() && current?.id === activeCaptureLectureId();
}
function historyNavigationBusy() { return isBusy() && !stableLiveCapture(); }
function returnToLiveCapture() {
  if (!stableLiveCapture()) return;
  const lecture = captureSession.lecture;
  selectImportLecture = false; ++importLectureSequence; ++requestGeneration;
  current = {...lecture,segments:[...(lecture.segments || [])]};
  lectureDateFilter = '';
  renderCurrent(); renderHistory();
  $('main-content').focus();
  if (current.recording_finalized === true && current.segments.length) void loadCorrection(current.id);
}
async function selectLecture(lecture) {
  if (!lecture?.id || historyNavigationBusy()) return;
  if (stableLiveCapture() && lecture.id === activeCaptureLectureId()) {
    returnToLiveCapture(); return;
  }
  selectImportLecture = false; ++importLectureSequence;
  const generation = ++requestGeneration;
  try {
    const note = await api(`/lectures/${encodeURIComponent(lecture.id)}`);
    if (generation !== requestGeneration || historyNavigationBusy()) return;
    current = note; renderCurrent(); renderHistory();
    $('main-content').focus();
    if (note.recording_finalized === true && note.segments?.length) void loadCorrection(note.id);
  } catch (error) { notice(errorText(error)); }
}
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
      const live = hasLiveCaptureSession() && activeCaptureLectureId() === lecture.id;
      const livePaused = live && paused;
      const button = document.createElement('button'); button.type = 'button';
      button.className = `lecture-item${current?.id === lecture.id ? ' selected' : ''}${live ? ' live-capture' : ''}`;
      button.disabled = historyNavigationBusy();
      if (current?.id === lecture.id) button.setAttribute('aria-current','page');
      const title = document.createElement('strong'); title.textContent = lecture.title;
      const date = document.createElement('span'); date.textContent = dateLabel(lecture.created_at); button.append(title,date);
      if (live) {
        const badge = document.createElement('span'); badge.className = 'lecture-live-label';
        badge.textContent = livePaused ? 'Ⅱ 일시정지' : '● 현재 녹음'; button.append(badge);
        button.setAttribute('aria-label',livePaused
          ? `${lecture.title}, 현재 받아쓰기 일시정지`
          : `${lecture.title}, 현재 녹음 중인 수업`);
      }
      button.onclick = () => selectLecture(lecture);
      items.append(button);
    }
    group.append(heading,items); list.append(group);
  }
}
$('lecture-date').onchange = () => { lectureDateFilter = $('lecture-date').value; renderHistory(); updateControls(); };
$('return-live-capture').onclick = returnToLiveCapture;
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
function scheduledCorrectionFor(lectureId = current?.id) {
  const scheduled = lectureId ? scheduledCorrections.get(lectureId) : null;
  return scheduled && scheduled.owner === user && scheduled.sessionToken === token
    && scheduled.server === apiUrl ? scheduled : null;
}
function scheduleCurrentCorrection() {
  if (!viewingActiveCaptureLecture() || !current?.segments?.length || current.recording_finalized === true) return;
  const lectureId = current.id;
  if (scheduledCorrectionFor(lectureId)) return;
  scheduledCorrections.set(lectureId,{
    lectureId, owner:user, sessionToken:token, server:apiUrl,
    captureId:captureSession.id, finalized:false, status:'scheduled',
  });
  renderCorrection();
  notice('받아쓰기는 계속합니다. 수업을 종료하고 마지막 음성을 저장한 뒤 AI 후보정을 자동으로 시작할게요.');
}
function correctionStatus() {
  const scheduled = scheduledCorrectionFor();
  if (scheduled?.status === 'starting') return 'scheduled-starting';
  if (scheduled) return 'scheduled';
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
  const running = ['loading','starting','scheduled','scheduled-starting','queued','processing'].includes(status);
  const liveAction = stableLiveCapture() && !!current?.id;
  const activeDraft = viewingActiveCaptureLecture() && current?.recording_finalized !== true;
  const sourceReady = current?.recording_finalized === true || activeDraft;
  $('transcript-versions').hidden = !hasTranscript;
  $('transcript-raw').disabled = !hasTranscript;
  $('transcript-corrected').disabled = !hasTranscript || !ready;
  $('correct-transcript').disabled = !hasTranscript || !sourceReady
    || (noteToolsBusy && !liveAction) || running || ready;
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
  $('correction-panel').setAttribute('aria-busy',String(['loading','starting','scheduled-starting','queued','processing'].includes(status)));
  const copies = {
    idle:['AI 후보정','받아쓴 원문은 그대로 두고, 전사 텍스트만 학교 AI 서버에 보내 별도의 후보정본을 만듭니다.','AI 후보정 만들기'],
    unfinished:['수업 기록을 마무리해 주세요','마지막 음성 저장이 끝난 뒤 AI 후보정을 시작할 수 있습니다. 받아쓴 원문은 지금도 내려받을 수 있어요.','마무리 후 사용'],
    scheduled:['종료 후 자동 후보정 예약됨','받아쓰기는 계속됩니다. 수업을 종료하면 마지막 음성이 서버에 저장된 뒤 후보정을 자동으로 시작합니다.','자동 후보정 예약됨'],
    'scheduled-starting':['마지막 저장 완료 · 후보정 요청 중','녹음을 종료했고 완전한 원문으로 AI 후보정을 시작하고 있어요.','요청하는 중…'],
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
  if (!current?.id || !current.segments?.length || correctionStarting || correctionIsReady()) return;
  if (viewingActiveCaptureLecture() && current.recording_finalized !== true) {
    scheduleCurrentCorrection(); return;
  }
  const alongsideLiveCapture = stableLiveCapture() && current.id !== activeCaptureLectureId();
  if (current.recording_finalized !== true || (isBusy() && !alongsideLiveCapture)
      || importIsActive() || importStarting) return;
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

async function submitScheduledCorrection(lectureId) {
  const scheduled = scheduledCorrectionFor(lectureId);
  if (!scheduled || scheduled.status !== 'scheduled') return;
  if (!scheduled.finalized && scheduled.captureId !== captureSession?.id) {
    if (scheduledCorrections.get(lectureId) === scheduled) scheduledCorrections.delete(lectureId);
    return;
  }
  scheduled.finalized = true;
  scheduled.status = 'starting';
  releaseFinalizedCapture(lectureId);
  const owner = scheduled.owner, sessionToken = scheduled.sessionToken, server = scheduled.server;
  const displaySequence = current?.id === lectureId ? ++correctionSequence : null;
  const sessionIsCurrent = () => owner === user && sessionToken === token && server === apiUrl;
  if (displaySequence !== null) {
    correctionError = ''; correctionCreditExhausted = false; renderCorrection();
  }
  try {
    const result = await api(`/lectures/${encodeURIComponent(lectureId)}/correction`,{method:'POST'},30000);
    if (!sessionIsCurrent()) return;
    if (scheduledCorrections.get(lectureId) === scheduled) scheduledCorrections.delete(lectureId);
    const stored = correctionPayload(result);
    const summary = lectures.find(lecture => lecture.id === lectureId);
    if (summary) summary.correction = stored;
    if (captureSession?.lecture?.id === lectureId) captureSession.lecture.correction = stored;
    if (displaySequence !== null && displaySequence === correctionSequence && current?.id === lectureId) {
      applyCorrectionResponse(result,lectureId); renderCurrent();
    } else if (current?.id === lectureId) {
      void loadCorrection(lectureId);
    }
    notice('AI 후보정을 자동으로 시작했어요. 받아쓴 원문은 그대로 보관합니다.');
  } catch (error) {
    if (!sessionIsCurrent()) return;
    if (scheduledCorrections.get(lectureId) === scheduled) scheduledCorrections.delete(lectureId);
    if (displaySequence !== null && displaySequence === correctionSequence && current?.id === lectureId) {
      correctionError = errorText(error); correctionCreditExhausted = isCreditExhaustion(error);
      renderCurrent();
    } else {
      notice(`자동 후보정을 시작하지 못했어요. ${errorText(error)}`);
    }
  } finally {
    if (sessionIsCurrent() && displaySequence !== null && displaySequence === correctionSequence
        && current?.id === lectureId) renderCurrent();
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

function clearAdminRefresh() {
  if (adminRefreshTimer !== null) clearTimeout(adminRefreshTimer);
  adminRefreshTimer = null;
}
function clearAdminProbe() {
  if (adminProbeTimer !== null) clearTimeout(adminProbeTimer);
  adminProbeTimer = null;
}
function clearTunnelRecovery() {
  if (tunnelRecoveryTimer !== null) clearTimeout(tunnelRecoveryTimer);
  tunnelRecoveryTimer = null; tunnelRecoveryDeadline = 0; tunnelRecoveryContext = null;
}
function scrubAdminDom() {
  // A hidden dialog is still inspectable from the DOM.  Remove every value and
  // event closure derived from an administrator response when the identity or
  // server changes, rather than relying on `hidden` or a closed dialog.
  $('admin-accounts').replaceChildren();
  $('admin-audit').replaceChildren();
  $('admin-error').textContent = ''; $('admin-error').hidden = true;
  $('admin-updated').textContent = '상태를 불러오는 중입니다.';
  $('admin-access-detail').textContent = '현재 운영 접속 상태를 확인하고 있어요.';
  $('admin-access-toggle').textContent = '상태 확인 중…'; $('admin-access-toggle').disabled = true;
  document.querySelector('.admin-access').setAttribute('data-state','unknown');
  $('admin-server-state').textContent = '확인 중'; $('admin-server-state').setAttribute('data-state','unknown');
  $('admin-server-detail').textContent = '서버 가동 시간을 확인하고 있어요.';
  for (const prefix of ['gpu','ram','disk']) {
    $(`admin-${prefix}-value`).textContent = '확인 중';
    $(`admin-${prefix}-progress`).removeAttribute('value');
    $(`admin-${prefix}-progress`).textContent = '사용량 정보 없음';
    $(`admin-${prefix}-detail`).textContent = '정보를 불러오고 있어요.';
  }
  for (const name of ['transcription','import','correction']) $(`admin-${name}-queue`).textContent = '0';
  $('admin-tunnel-state').textContent = '확인 중'; $('admin-tunnel-state').setAttribute('data-state','unknown');
  $('admin-tunnel-detail').textContent = '터널 상태를 확인하고 있어요.';
  $('admin-tunnel-restart').textContent = '터널 재연결'; $('admin-tunnel-restart').disabled = true;
  $('admin-confirm-title').textContent = '관리 작업을 실행할까요?';
  $('admin-confirm-description').textContent = '선택한 작업을 확인해 주세요.';
  $('admin-confirm-accept').textContent = '확인';
}
function resetAdminState() {
  ++adminSequence;
  clearAdminRefresh(); clearAdminProbe(); clearTunnelRecovery();
  adminAuthorized = false; adminOverview = null; adminLoading = false; adminError = ''; adminAction = ''; adminConfirmation = null;
  $('admin-open').hidden = true;
  if ($('admin-dialog').open) $('admin-dialog').close();
  if ($('admin-confirm-dialog').open) $('admin-confirm-dialog').close();
  scrubAdminDom();
}
function adminOperationIsCurrent(sequence, owner, sessionToken, server) {
  return sequence === adminSequence && owner === user && sessionToken === token && server === apiUrl;
}
function adminDateTime(value) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return '기록 없음';
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone:'Asia/Seoul',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,
  }).format(date);
}
function durationLabel(value) {
  const total = Math.max(0,Math.floor(Number(value) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor(total % 86400 / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  return [days ? `${days}일` : '',hours ? `${hours}시간` : '',`${minutes}분`].filter(Boolean).join(' ');
}
function safeNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}
function usagePercent(value) {
  const explicit = Number(value?.percent ?? value?.usage_percent ?? value?.utilization_percent);
  if (Number.isFinite(explicit)) return Math.min(100,Math.max(0,explicit));
  const used = safeNumber(value?.used_bytes,Number.NaN), total = safeNumber(value?.total_bytes,Number.NaN);
  return Number.isFinite(used) && Number.isFinite(total) && total > 0 ? Math.min(100,used / total * 100) : 0;
}
function hasUsageCapacity(value) {
  return Number.isFinite(Number(value?.used_bytes))
    && Number.isFinite(Number(value?.total_bytes))
    && Number(value.total_bytes) > 0;
}
function setAdminUsage(prefix, value, {unavailable = false, detailPrefix = ''} = {}) {
  const progress = $(`admin-${prefix}-progress`);
  const used = Number(value?.used_bytes), total = Number(value?.total_bytes);
  const explicit = Number(value?.percent ?? value?.usage_percent ?? value?.utilization_percent);
  const known = Number.isFinite(explicit) || (Number.isFinite(used) && Number.isFinite(total) && total > 0);
  if (unavailable || !known) progress.removeAttribute('value');
  const percent = usagePercent(value);
  progress.textContent = known && !unavailable ? `${Math.round(percent)}%` : '사용량 정보 없음';
  if (known && !unavailable) progress.value = percent;
  $(`admin-${prefix}-value`).textContent = unavailable ? '사용할 수 없음' : known ? `${Math.round(percent)}% 사용` : '확인할 수 없음';
  const size = Number.isFinite(used) && Number.isFinite(total) && total > 0
    ? `${bytesLabel(used)} / ${bytesLabel(total)}` : '용량 정보 없음';
  $(`admin-${prefix}-detail`).textContent = [detailPrefix,size].filter(Boolean).join(' · ');
}
function queueLabel(value) {
  if (typeof value === 'number') return String(Math.max(0,Math.floor(value)));
  const queued = Math.max(0,Math.floor(Number(value?.queued) || 0));
  const processing = Math.max(0,Math.floor(Number(value?.processing) || 0));
  return `${queued} · ${processing}`;
}
function adminActivityLabel(account) {
  if (typeof account?.activity_label === 'string' && account.activity_label.trim()) return account.activity_label.trim();
  return ({
    offline:'오프라인',idle:'사용하지 않는 중',viewing:'기록 확인 중',recording:'녹음 중',uploading:'파일 업로드 중',
    transcribing:'받아쓰기 처리 중',correcting:'AI 후보정 중',away:'자리 비움',
  })[account?.activity] || (account?.online ? '접속 중' : '오프라인');
}
function accountJobLabel(jobs) {
  const values = [jobs?.transcription,jobs?.imports,jobs?.corrections].map(value => {
    if (typeof value === 'number') return Math.max(0,Math.floor(value));
    return Math.max(0,Math.floor(Number(value?.queued) || 0)) + Math.max(0,Math.floor(Number(value?.processing) || 0));
  });
  const total = values.reduce((sum,value) => sum + value,0);
  return total ? `진행 작업 ${total}개` : '';
}
function renderAdminAccounts(accounts) {
  const container = $('admin-accounts');
  // Do not replace a keyboard-focused account action underneath the user.
  // The following refresh will render the newest snapshot after focus moves.
  if (typeof container.contains === 'function' && document.activeElement
      && container.contains(document.activeElement)) return;
  container.replaceChildren();
  const safeAccounts = Array.isArray(accounts) ? accounts.slice(0,50) : [];
  if (!safeAccounts.length) {
    const empty = document.createElement('p'); empty.className = 'admin-empty'; empty.textContent = '표시할 계정이 없습니다.'; container.append(empty); return;
  }
  for (const account of safeAccounts) {
    const row = document.createElement('article'); row.className = 'admin-account';
    const identity = document.createElement('div'); identity.className = 'admin-account-identity';
    const label = document.createElement('strong'); label.textContent = String(account?.label || '이름 없는 계정');
    const activation = document.createElement('small'); activation.textContent = account?.activated === false ? '초대·비밀번호 설정 대기' : '계정 활성화됨';
    identity.append(label,activation);
    const activity = document.createElement('div'); activity.className = 'admin-account-activity';
    const activityText = document.createElement('strong'); activityText.textContent = adminActivityLabel(account);
    const sessions = Math.max(0,Math.floor(Number(account?.session_count) || 0));
    const jobText = accountJobLabel(account?.jobs);
    const detail = document.createElement('small');
    detail.textContent = [`세션 ${sessions}개`,jobText,`최근 활동 ${adminDateTime(account?.last_activity_at)}`].filter(Boolean).join(' · ');
    activity.append(activityText,detail);
    const self = account?.is_self === true || account?.label === user;
    let action;
    if (self) {
      action = document.createElement('span'); action.className = 'admin-account-current'; action.textContent = '현재 계정';
    } else {
      action = document.createElement('button'); action.type = 'button'; action.className = 'secondary-button';
      action.textContent = sessions ? '세션 종료' : '세션 없음';
      action.disabled = !sessions || typeof account?.account_id !== 'string' || !account.account_id || !!adminAction;
      action.onclick = () => openAdminConfirmation('session-revoke',{...account,is_self:self});
    }
    row.append(identity,activity,action); container.append(row);
  }
}
function renderAdminAudit(entries) {
  const container = $('admin-audit'); container.replaceChildren();
  const safeEntries = Array.isArray(entries) ? entries.slice(0,20) : [];
  if (!safeEntries.length) {
    const empty = document.createElement('p'); empty.className = 'admin-empty'; empty.textContent = '최근 관리 작업이 없습니다.'; container.append(empty); return;
  }
  const actionLabels = {
    access_open:'원격 접속 열기',access_close:'원격 접속 닫기',access_changed:'원격 접속 변경',
    tunnel_restart:'터널 재연결',tunnel_restarted:'터널 재연결',sessions_revoke:'세션 종료',sessions_revoked:'세션 종료',
  };
  const resultLabels = {success:'완료',failed:'실패',accepted:'요청됨'};
  const targetLabels = {service:'운영 접속',tunnel:'터널'};
  for (const entry of safeEntries) {
    const row = document.createElement('article'); row.className = 'admin-audit-entry';
    const copy = document.createElement('div');
    const title = document.createElement('strong'); title.textContent = actionLabels[entry?.action] || String(entry?.action || '관리 작업');
    const targetValue = entry?.target_label ?? entry?.target;
    const targetText = typeof targetValue === 'string' ? targetValue.trim() : '';
    const target = targetText ? ` · ${targetLabels[targetText] || targetText}` : '';
    const detail = document.createElement('small'); detail.textContent = `${adminDateTime(entry?.timestamp ?? entry?.created_at)}${target}`;
    const result = document.createElement('span');
    const resultValue = String(entry?.result || ''); result.textContent = resultLabels[resultValue] || resultValue || '확인되지 않음';
    result.setAttribute('data-state',resultValue === 'success' || resultValue === 'accepted' ? 'ready' : resultValue === 'failed' ? 'error' : 'unknown');
    result.className = 'admin-badge';
    copy.append(title,detail); row.append(copy,result); container.append(row);
  }
}
function renderAdminOverview() {
  $('admin-open').hidden = !adminAuthorized;
  const overview = adminOverview || {};
  const busy = adminLoading || !!adminAction;
  $('admin-refresh').disabled = busy;
  $('admin-refresh').textContent = adminLoading ? '새로고침 중…' : '새로고침';
  $('admin-error').hidden = !adminError;
  $('admin-error').textContent = adminError;
  $('admin-updated').textContent = adminLoading && !adminOverview ? '상태를 불러오는 중입니다.'
    : `자동 새로고침 · 기준 ${adminDateTime(overview.generated_at)}`;

  const enabled = overview.access?.enabled === true;
  $('admin-access-detail').textContent = enabled
    ? '로그인한 사용자의 새 수업·조회·업로드·다운로드 요청을 처리하고 있습니다.'
    : '모든 새 수업 데이터 요청을 일시 중지했습니다. 현재 관리자 연결에서는 다시 열 수 있습니다.';
  const accessSection = document.querySelector('.admin-access');
  accessSection.setAttribute('data-state',enabled ? 'open' : 'paused');
  $('admin-access-toggle').textContent = adminAction === 'access' ? '적용 중…' : enabled ? '운영 접속 닫기' : '운영 접속 열기';
  $('admin-access-toggle').disabled = busy || typeof overview.access?.enabled !== 'boolean';
  $('admin-access-toggle').classList.toggle('danger-button',enabled);
  $('admin-access-toggle').classList.toggle('secondary-button',!enabled);

  const server = overview.server || {};
  const modelStateToServer = {ready:'ready',loading:'starting',unloaded:'running',error:'error'};
  const reportedServerState = String(modelStateToServer[server.model_state] || server.state || server.status || 'unknown');
  const serverState = new Set(['running','ready','online','starting','error','offline','unknown']).has(reportedServerState)
    ? reportedServerState : 'unknown';
  const serverLabels = {running:'서버 실행 중',ready:'음성 모델 준비됨',online:'API 응답 중',starting:'음성 모델 준비 중',error:'모델 오류',offline:'서버 중지됨',unknown:'확인 중'};
  $('admin-server-state').textContent = serverLabels[serverState] || serverState;
  $('admin-server-state').setAttribute('data-state',serverState);
  const load = overview.resources?.load || {};
  const loadValues = [load.one ?? load.load_1m,load.five ?? load.load_5m,load.fifteen ?? load.load_15m]
    .map(value => Number(value)).filter(Number.isFinite);
  $('admin-server-detail').textContent = [
    Number.isFinite(Number(server.uptime_seconds)) ? `가동 ${durationLabel(server.uptime_seconds)}` : '',
    typeof server.model === 'string' && server.model ? `모델 ${server.model}` : '',
    typeof server.engine === 'string' && server.engine ? `엔진 ${server.engine}` : '',
    typeof server.device === 'string' && server.device ? `장치 ${server.device}` : '',
    loadValues.length ? `시스템 부하 ${loadValues.map(value => value.toFixed(2)).join(' / ')}` : '',
  ].filter(Boolean).join(' · ') || '서버 상태 세부 정보가 없습니다.';

  const resources = overview.resources || {};
  const gpu = resources.gpu || {};
  const gpuUnavailable = gpu.available === false || (!gpu.available && !gpu.total_bytes && !gpu.vram_total_bytes);
  const gpuUsed = gpu.used_bytes ?? gpu.vram_used_bytes;
  const gpuTotal = gpu.total_bytes ?? gpu.vram_total_bytes;
  const gpuAllocated = gpu.allocated_bytes ?? gpu.process_allocated_bytes;
  const gpuReserved = gpu.reserved_bytes ?? gpu.process_reserved_bytes;
  if (!gpuUnavailable && !Number.isFinite(Number(gpuTotal)) && Number.isFinite(Number(gpuAllocated))) {
    $('admin-gpu-progress').removeAttribute('value'); $('admin-gpu-progress').textContent = '전체 VRAM 정보 없음';
    $('admin-gpu-value').textContent = `${bytesLabel(gpuAllocated)} 할당`;
    $('admin-gpu-detail').textContent = [gpu.name,Number.isFinite(Number(gpuReserved)) ? `예약 ${bytesLabel(gpuReserved)}` : '',
      Number.isFinite(Number(gpu.utilization_percent)) ? `GPU ${Math.round(Number(gpu.utilization_percent))}%` : ''].filter(Boolean).join(' · ') || 'ROCm 메모리 할당 정보';
  } else {
    setAdminUsage('gpu',{used_bytes:gpuUsed,total_bytes:gpuTotal,percent:gpu.vram_percent},
      {unavailable:gpuUnavailable,detailPrefix:gpuUnavailable ? 'ROCm 정보를 사용할 수 없음' : [gpu.name,
        Number.isFinite(Number(gpuAllocated)) ? `서버 프로세스 할당 ${bytesLabel(gpuAllocated)}` : '',
        Number.isFinite(Number(gpuReserved)) ? `예약 ${bytesLabel(gpuReserved)}` : '',
        Number.isFinite(Number(gpu.utilization_percent)) ? `GPU ${Math.round(Number(gpu.utilization_percent))}%` : ''].filter(Boolean).join(' · ')});
  }
  const memory = resources.memory || resources.ram || {};
  setAdminUsage('ram',memory,{unavailable:!hasUsageCapacity(memory),detailPrefix:Number.isFinite(Number(memory.process_rss_bytes))
    ? `서버 프로세스 ${bytesLabel(memory.process_rss_bytes)}` : ''});
  const disk = resources.disk || {};
  setAdminUsage('disk',disk,{unavailable:!hasUsageCapacity(disk)});

  const queues = overview.queues || {};
  $('admin-transcription-queue').textContent = queueLabel(queues.transcription);
  $('admin-import-queue').textContent = queueLabel(queues.imports);
  $('admin-correction-queue').textContent = queueLabel(queues.corrections);

  const tunnel = overview.tunnel || {};
  const reportedTunnelState = String(tunnel.state || 'unknown');
  const tunnelState = new Set(['online','offline','starting','stopping','error','unknown']).has(reportedTunnelState)
    ? reportedTunnelState : 'unknown';
  const tunnelRestarting = ['starting','stopping'].includes(tunnelState) || tunnel.operation === 'restarting' || tunnel.operation?.restarting === true;
  const tunnelDisplayState = tunnelRestarting ? 'starting' : tunnelState;
  const tunnelRestartSupported = tunnel.restart_available ?? tunnel.restart_supported ?? tunnel.operation?.restart_supported ?? (tunnel.operation !== 'unsupported');
  const tunnelLabels = {online:'프로세스 실행 중',offline:'프로세스 중지됨',starting:'재연결 중',stopping:'터널 종료 중',unknown:'확인 중',error:'오류'};
  $('admin-tunnel-state').textContent = tunnelLabels[tunnelDisplayState] || tunnelDisplayState;
  $('admin-tunnel-state').setAttribute('data-state',tunnelDisplayState);
  $('admin-tunnel-detail').textContent = typeof tunnel.message === 'string' && tunnel.message
    ? tunnel.message : tunnelState === 'online'
      ? 'Cloudflare 터널 프로세스가 실행 중입니다. 외부 HTTPS 접속 가능 여부는 별도로 확인해 주세요.'
      : 'Cloudflare 터널 프로세스 상태를 확인해 주세요.';
  $('admin-tunnel-restart').disabled = busy || tunnelRestartSupported === false || tunnelRestarting;
  $('admin-tunnel-restart').textContent = adminAction === 'tunnel' ? '요청 중…' : tunnelRestarting ? '재연결 중…' : '터널 재연결';

  renderAdminAccounts(overview.accounts);
  renderAdminAudit(overview.recent_audit);
}
function scheduleAdminRefresh() {
  clearAdminRefresh();
  if (!adminAuthorized || !$('admin-dialog').open || !token) return;
  const sequence = adminSequence;
  adminRefreshTimer = setTimeout(() => {
    adminRefreshTimer = null;
    if (sequence !== adminSequence || !$('admin-dialog').open) return;
    if (document.hidden) scheduleAdminRefresh();
    else void loadAdminOverview();
  },ADMIN_REFRESH_MS);
}
function scheduleAdminProbeRetry(owner, sessionToken, server) {
  clearAdminProbe();
  adminProbeTimer = setTimeout(() => {
    adminProbeTimer = null;
    if (!token || owner !== user || sessionToken !== token || server !== apiUrl) return;
    void loadAdminOverview({probe:true});
  },ADMIN_REFRESH_MS);
}
async function loadAdminOverview({probe = false} = {}) {
  if (!token) return;
  clearAdminRefresh();
  const owner = user, sessionToken = token, server = apiUrl, sequence = ++adminSequence;
  adminLoading = true;
  if (adminAuthorized) renderAdminOverview();
  try {
    const result = await api('/admin/overview');
    if (!adminOperationIsCurrent(sequence,owner,sessionToken,server)) return;
    clearAdminProbe();
    adminAuthorized = true; adminOverview = result && typeof result === 'object' ? result : {};
    adminError = ''; renderAdminOverview();
  } catch (error) {
    if (!adminOperationIsCurrent(sequence,owner,sessionToken,server)) return;
    if (error?.status === 403) {
      resetAdminState();
      return;
    }
    if (adminAuthorized && !probe) {
      adminError = errorText(error); renderAdminOverview();
    } else {
      adminAuthorized = false; adminOverview = null; $('admin-open').hidden = true;
      if (probe) scheduleAdminProbeRetry(owner,sessionToken,server);
    }
  } finally {
    if (adminOperationIsCurrent(sequence,owner,sessionToken,server)) {
      adminLoading = false; renderAdminOverview(); scheduleAdminRefresh();
    }
  }
}
function closeAdminDialog() {
  clearAdminRefresh();
  if ($('admin-dialog').open) $('admin-dialog').close();
}
$('admin-open').onclick = () => {
  if (!adminAuthorized || !token) return;
  renderAdminOverview(); $('admin-dialog').showModal(); $('admin-close').focus(); void loadAdminOverview();
};
$('admin-close').onclick = closeAdminDialog;
$('admin-dialog').oncancel = () => { clearAdminRefresh(); };
$('admin-refresh').onclick = () => { if (!adminLoading && !adminAction) void loadAdminOverview(); };
function closeAdminConfirmation() {
  adminConfirmation = null;
  if ($('admin-confirm-dialog').open) $('admin-confirm-dialog').close();
}
function openAdminConfirmation(type, account = null) {
  if (!adminAuthorized || adminAction || account?.is_self) return;
  const copies = {
    'access-close':['운영 접속을 닫을까요?','새 수업·조회·업로드·다운로드를 포함한 모든 수업 데이터 요청이 일시 중지됩니다. 진행 중인 전송에도 영향을 줄 수 있으며, 현재 관리자 연결에서는 다시 열 수 있습니다.','운영 접속 닫기'],
    'tunnel-restart':['터널을 재연결할까요?','외부 주소가 바뀌며 이 페이지 연결이 끊길 수 있습니다. 재연결 뒤 자동 주소가 게시될 때까지 기다린 다음 다시 로그인해야 할 수 있습니다.','터널 재연결'],
    'session-revoke':['계정 세션을 종료할까요?',`${String(account?.label || '선택한 계정')}의 모든 로그인 세션을 종료합니다. 해당 기기에서 다시 로그인해야 합니다.`,'세션 종료'],
  };
  const copy = copies[type]; if (!copy) return;
  adminConfirmation = {type,accountId:account?.account_id || '',label:account?.label || ''};
  $('admin-confirm-title').textContent = copy[0]; $('admin-confirm-description').textContent = copy[1]; $('admin-confirm-accept').textContent = copy[2];
  $('admin-confirm-dialog').showModal(); $('admin-confirm-cancel').focus();
}
$('admin-confirm-close').onclick = closeAdminConfirmation;
$('admin-confirm-cancel').onclick = closeAdminConfirmation;
$('admin-confirm-dialog').oncancel = () => { adminConfirmation = null; };
async function runAdminAction(type, payload) {
  if (!adminAuthorized || adminAction || !token) return;
  const endpoints = {access:'/admin/access',tunnel:'/admin/tunnel/restart',sessions:'/admin/sessions/revoke'};
  const path = endpoints[type]; if (!path) return;
  const owner = user, sessionToken = token, server = apiUrl, sequence = ++adminSequence;
  clearAdminRefresh(); adminAction = type; adminError = ''; renderAdminOverview();
  try {
    await api(path,{method:'POST',body:JSON.stringify(payload || {})},30000);
    if (!adminOperationIsCurrent(sequence,owner,sessionToken,server)) return;
    if (type === 'access') notice(payload.enabled ? '새 수업 데이터 요청을 다시 받습니다.' : '새 수업 데이터 요청을 일시 중지했어요.');
    else if (type === 'sessions') notice('선택한 계정의 로그인 세션을 종료했어요.');
    else notice('터널 재연결을 요청했어요. 외부 주소가 바뀌면 다시 로그인해 주세요.');
    adminAction = '';
    if (type === 'tunnel') {
      closeAdminDialog();
      startTunnelRecovery({owner,sessionToken,server,requestGeneration,connectionGeneration});
      return;
    }
    await loadAdminOverview();
  } catch (error) {
    if (!adminOperationIsCurrent(sequence,owner,sessionToken,server)) return;
    adminAction = '';
    if (error?.status === 403) { resetAdminState(); return; }
    adminError = errorText(error); renderAdminOverview();
  } finally {
    if (adminOperationIsCurrent(sequence,owner,sessionToken,server)) {
      adminAction = ''; renderAdminOverview(); scheduleAdminRefresh();
    }
  }
}
function startTunnelRecovery(context) {
  clearTunnelRecovery();
  tunnelRecoveryContext = context; tunnelRecoveryDeadline = Date.now() + 3 * 60 * 1000;
  const exhausted = () => {
    clearTunnelRecovery();
    notice('새 터널 주소를 자동으로 찾지 못했어요. 잠시 후 페이지를 새로고침하거나 연결 설정에서 주소를 확인해 주세요.');
  };
  const schedule = delay => {
    const remaining = tunnelRecoveryDeadline - Date.now();
    if (remaining <= 0) { exhausted(); return; }
    tunnelRecoveryTimer = setTimeout(async () => {
      tunnelRecoveryTimer = null;
      const active = tunnelRecoveryContext;
      if (!active || active.owner !== user || active.sessionToken !== token || active.server !== apiUrl
          || active.requestGeneration !== requestGeneration || active.connectionGeneration !== connectionGeneration) {
        clearTunnelRecovery(); return;
      }
      if (Date.now() >= tunnelRecoveryDeadline) { exhausted(); return; }
      const discovery = discoverServer({deadline:tunnelRecoveryDeadline,serializeOwner:active.owner});
      active.connectionGeneration = connectionGeneration;
      await discovery.catch(() => {});
      if (!tunnelRecoveryContext) return;
      if (apiUrl !== active.server || token !== active.sessionToken || user !== active.owner
          || requestGeneration !== active.requestGeneration || connectionGeneration !== active.connectionGeneration) {
        clearTunnelRecovery(); return;
      }
      if (Date.now() >= tunnelRecoveryDeadline) {
        exhausted();
        return;
      }
      schedule(8000);
    },Math.min(delay,remaining));
  };
  schedule(5000);
}
$('admin-access-toggle').onclick = () => {
  if (!adminAuthorized || adminLoading || adminAction || typeof adminOverview?.access?.enabled !== 'boolean') return;
  if (adminOverview.access.enabled) openAdminConfirmation('access-close');
  else void runAdminAction('access',{enabled:true});
};
$('admin-tunnel-restart').onclick = () => openAdminConfirmation('tunnel-restart');
$('admin-confirm-accept').onclick = () => {
  const pendingAction = adminConfirmation; closeAdminConfirmation();
  if (!pendingAction) return;
  if (pendingAction.type === 'access-close') void runAdminAction('access',{enabled:false});
  else if (pendingAction.type === 'tunnel-restart') void runAdminAction('tunnel',{});
  else if (pendingAction.type === 'session-revoke' && pendingAction.accountId) {
    void runAdminAction('sessions',{account_id:pendingAction.accountId});
  }
};

function clearPresenceTimers() {
  if (presenceTimer !== null) clearInterval(presenceTimer);
  if (presenceIdleTimer !== null) clearTimeout(presenceIdleTimer);
  presenceTimer = null; presenceIdleTimer = null;
}
function resetPresence() {
  ++presenceSequence; clearPresenceTimers();
  presenceSending = false; presenceLastSent = ''; presenceQueued = ''; lastPresenceInteraction = Date.now();
}
function presenceActivity() {
  if (document.hidden) return 'away';
  if (recording || paused || starting || pausing || resuming) return 'recording';
  if (importStarting || importJob?.status === 'uploading') return 'uploading';
  if (sending || pending.length || ['queued','processing'].includes(importJob?.status)) return 'transcribing';
  if (correctionStarting || ['queued','processing'].includes(correction?.status)) return 'correcting';
  if (Date.now() - lastPresenceInteraction >= PRESENCE_IDLE_MS) return 'idle';
  return 'viewing';
}
function schedulePresenceIdle() {
  if (presenceIdleTimer !== null) clearTimeout(presenceIdleTimer);
  if (presenceTimer === null || !token) { presenceIdleTimer = null; return; }
  const delay = Math.max(0,lastPresenceInteraction + PRESENCE_IDLE_MS - Date.now());
  const sequence = presenceSequence;
  presenceIdleTimer = setTimeout(() => {
    presenceIdleTimer = null;
    if (sequence === presenceSequence) void sendPresence();
  },delay);
}
async function sendPresence(force = false) {
  if (presenceTimer === null || !token) return;
  const activity = presenceActivity();
  if (presenceSending) { presenceQueued = activity; return; }
  if (!force && activity === presenceLastSent) return;
  const owner = user, sessionToken = token, server = apiUrl, sequence = presenceSequence;
  presenceSending = true; presenceQueued = '';
  try {
    await api('/presence',{method:'POST',body:JSON.stringify({activity})},10000);
    if (sequence === presenceSequence && owner === user && sessionToken === token && server === apiUrl) presenceLastSent = activity;
  } catch (error) {
    if (sequence === presenceSequence && owner === user && sessionToken === token && server === apiUrl
        && (error?.status === 403 || error?.status === 404)) presenceLastSent = activity;
  } finally {
    if (sequence !== presenceSequence || owner !== user || sessionToken !== token || server !== apiUrl) return;
    presenceSending = false;
    const queued = presenceQueued; presenceQueued = '';
    if (queued && queued !== presenceLastSent) void sendPresence();
  }
}
function notePresenceStateChange() {
  if (presenceTimer === null || !token) return;
  const activity = presenceActivity();
  if (activity !== presenceLastSent) void sendPresence();
}
function notePresenceInteraction() {
  const wasIdle = presenceActivity() === 'idle';
  lastPresenceInteraction = Date.now(); schedulePresenceIdle();
  if (wasIdle) notePresenceStateChange();
}
function startPresence() {
  resetPresence();
  const sequence = presenceSequence;
  presenceTimer = setInterval(() => { if (sequence === presenceSequence) void sendPresence(true); },PRESENCE_INTERVAL_MS);
  schedulePresenceIdle();
  setTimeout(() => { if (sequence === presenceSequence) void sendPresence(true); },0);
}
document.addEventListener('pointerdown',notePresenceInteraction,{passive:true});
document.addEventListener('keydown',notePresenceInteraction);

function renderCurrent() {
  const lectureId = current?.id || '';
  if (lectureId !== correctionLectureId) {
    resetCorrectionState(lectureId);
    if (current?.correction) correction = correctionPayload(current.correction);
  }
  $('note-date').textContent = dateLabel(current?.created_at || new Date());
  $('view-label').textContent = current?.title || '새 수업';
  document.querySelector('.note-heading h1').textContent = current?.title || '오늘의 배움을 담아보세요.';
  if (current) {
    $('lecture-title').value = current.title; $('language').value = current.language || 'auto';
    $('asr-provider').value = current.asr_provider === 'clova' ? 'clova' : 'qwen';
  }
  renderCorrection();
  const segments = displayedTranscriptSegments();
  const transcript = $('transcript'); transcript.replaceChildren();
  if (!segments.length) {
    const activeCaptureView = hasLiveCaptureSession() && current?.id === activeCaptureLectureId();
    const empty = document.createElement('div'); empty.className = 'empty-note';
    const mark = document.createElement('span'); mark.className = 'empty-symbol'; mark.ariaHidden = 'true'; mark.textContent = '≋';
    const heading = document.createElement('h3'); heading.textContent = activeCaptureView ? '첫 문장을 기다리고 있어요.' : current ? '아직 받아쓴 내용이 없어요.' : '첫 문장을 기다리고 있어요.';
    const text = document.createElement('p'); text.textContent = activeCaptureView && paused
      ? '재개하면 같은 수업에 이어서 기록해요.'
      : activeCaptureView ? '목소리가 들어오면 이곳에 글이 나타나요.'
        : current ? '이 수업에는 표시할 받아쓰기 문장이 없어요.' : '수업 이름을 적고 받아쓰기를 시작해 보세요.';
    empty.append(mark,heading,text); transcript.append(empty);
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
function selectedAsrProvider() { return $('asr-provider').value === 'clova' ? 'clova' : 'qwen'; }
function preferredMicrophoneProvider() {
  if (micProviderPreference === 'qwen') return 'qwen';
  if (transcriptionProviders.clova.configured) return 'clova';
  return 'qwen';
}
function applyNewLectureProvider() {
  // Historical and in-progress lectures display their immutable persisted
  // provider. Only reconcile the controls for a genuinely new lecture.
  if (current || captureSession || draft) return;
  $('asr-provider').value = selectedCaptureSource() === 'system'
    ? 'qwen' : preferredMicrophoneProvider();
  enforceClovaLanguage(false);
}
function displayedAsrProvider() {
  if (current) return current.asr_provider === 'clova' ? 'clova' : 'qwen';
  if (captureSession?.asrProvider) return captureSession.asrProvider === 'clova' ? 'clova' : 'qwen';
  return selectedCaptureSource() === 'system' ? 'qwen' : selectedAsrProvider();
}
function updateProviderGuidance() {
  const system = !current && selectedCaptureSource() === 'system';
  const provider = displayedAsrProvider();
  const clova = provider === 'clova';
  $('provider-privacy').textContent = CLOVA_PRIVACY_NOTICE;
  $('provider-privacy').hidden = !clova;
  if (system) {
    $('provider-guidance').textContent = '컴퓨터·브라우저 탭 소리는 항상 이 PC의 Qwen으로 처리합니다.';
  } else if (clova && !transcriptionProviders.clova.configured && !current) {
    $('provider-guidance').textContent = 'CLOVA Speech를 사용하려면 서버 컴퓨터에 스트리밍 Secret Key를 먼저 설정해야 합니다.';
  } else if (clova) {
    $('provider-guidance').textContent = '이 수업의 마이크 음성을 사이트 운영자가 설정한 NAVER CLOVA Speech로 처리합니다.';
  } else {
    $('provider-guidance').textContent = '마이크 음성을 이 PC의 Qwen으로 처리합니다. 외부 음성 인식 서비스로 보내지 않습니다.';
  }
}
function enforceClovaLanguage(announce = false) {
  if (current || selectedCaptureSource() !== 'microphone' || selectedAsrProvider() !== 'clova') return;
  if (!['ko','en'].includes($('language').value)) {
    $('language').value = 'ko';
    if (announce) notice('CLOVA 실시간 받아쓰기는 한국어와 영어만 지원해 한국어로 바꿨어요.');
  }
}
function updateSourceGuidance() {
  const system = selectedCaptureSource() === 'system';
  if (!current) applyNewLectureProvider();
  $('source-guidance').textContent = system
    ? '데스크톱 Chrome·Edge에서 탭·화면과 오디오 공유를 켜세요. 선택 범위의 알림이나 다른 앱 소리도 함께 들어갈 수 있으며, 화면 영상은 서버로 보내지 않습니다.'
    : '마이크로 주변의 수업 소리를 받아씁니다.';
  $('source-privacy').hidden = !system;
  updateProviderGuidance();
}
$('audio-source').onchange = () => { updateSourceGuidance(); updateControls(); };
$('asr-provider').onchange = () => {
  micProviderPreference = selectedAsrProvider();
  enforceClovaLanguage(true); updateProviderGuidance(); updateControls();
};
$('language').onchange = () => { enforceClovaLanguage(true); updateControls(); };
function resetNewNote({ focus = false } = {}) {
  ++requestGeneration;
  selectImportLecture = false; ++importLectureSequence;
  current = null; lectureDateFilter = ''; sampleSeconds = 0; elapsedActiveMs = 0; elapsedStartedAt = 0;
  $('lecture-title').value = '';
  $('language').value = 'ko';
  $('elapsed').textContent = '00:00';
  updateSourceGuidance();
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
  const provider = captureSession?.asrProvider || displayedAsrProvider();
  const clova = provider === 'clova';
  const liveSession = recording || paused || pausing || resuming || inputUnavailable;
  const captureTransition = (starting && !recording && !paused) || pausing || resuming || stopping;
  const noteToolsBusy = busy || activeImport || importStarting || importCancelling;
  const hasTranscript = !!current?.segments?.length;
  const viewingOtherLecture = hasLiveCaptureSession() && current?.id !== activeCaptureLectureId();
  $('live-capture-banner').hidden = !viewingOtherLecture;
  $('live-capture-title').textContent = paused
    ? '현재 수업은 일시정지 상태로 안전하게 유지 중이에요.'
    : '지난 기록을 보는 동안에도 현재 수업을 계속 녹음하고 있어요.';
  $('live-capture-detail').textContent = captureSession?.asrProvider === 'clova'
    ? '음성은 현재 녹음 수업에만 연결되며, CLOVA 처리를 위해 이 서버를 거쳐 사이트 운영자가 관리하는 NAVER Cloud 계정의 CLOVA Speech 도메인으로 계속 전송됩니다.'
    : '음성과 받아쓰기 결과는 현재 녹음 수업에만 저장되며, 이 PC의 Qwen으로 계속 처리됩니다.';
  $('return-live-capture').disabled = !stableLiveCapture();
  $('lecture-date').disabled = historyNavigationBusy();
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
  $('language-auto').disabled = clova;
  $('audio-source').disabled = busy || !!current;
  $('asr-provider-qwen').disabled = !transcriptionProviders.qwen.configured;
  $('asr-provider-clova').disabled = !transcriptionProviders.clova.configured;
  $('asr-provider').disabled = busy || !!current || system;
  updateProviderGuidance();
  $('record-button').disabled = authenticating || loggingOut || captureTransition || activeImport || importStarting || recordingFinalizePending || (!liveSession && (!!draft || pending.length > 0 || sending));
  $('record-button').classList.toggle('stop', liveSession);
  $('record-button').textContent = stopping ? '마지막 음성 정리 중…' : liveSession ? '■ 받아쓰기 종료' : starting ? (system ? '공유 화면 준비 중…' : '마이크 준비 중…') : activeImport || importStarting ? '파일 변환이 끝난 뒤 시작' : current ? '＋ 새 수업 시작' : system ? '● 화면 소리 받아쓰기' : '● 받아쓰기 시작';
  $('pause-button').disabled = authenticating || loggingOut || pausing || resuming || stopping
    || (!recording && !paused && !inputUnavailable);
  $('pause-button').classList.toggle('resume', paused || inputUnavailable);
  $('pause-button').setAttribute('aria-pressed',String(paused));
  $('pause-button').textContent = pausing ? '마지막 음성 정리 중…' : resuming ? '다시 연결하는 중…'
    : inputReconnectNeeded ? '▶ 오디오 다시 연결' : inputUnavailable ? '▶ 입력 복구 시도' : paused ? '▶ 받아쓰기 재개' : 'Ⅱ 일시정지';
  $('record-dot').classList.toggle('live', recording);
  $('record-dot').classList.toggle('paused', paused || pausing || inputUnavailable);
  $('record-state').textContent = inputUnavailable ? '오디오 입력이 돌아오기를 기다리고 있어요'
    : pausing ? '마지막 음성까지 저장하고 일시정지해요' : resuming ? '같은 수업의 오디오를 다시 연결하고 있어요'
      : paused ? '받아쓰기를 일시정지했어요' : recording ? (system ? '공유한 화면의 소리를 듣고 있어요' : '수업을 듣고 있어요')
        : starting ? (system ? '공유할 화면과 오디오를 준비하고 있어요' : '마이크와 노트를 준비하고 있어요')
          : queued ? '남은 음성을 받아쓰고 있어요' : current ? '수업 기록을 저장했어요' : '시작할 준비가 됐어요';
  $('record-hint').textContent = inputUnavailable ? (inputUnavailableMessage || '이미 받은 음성은 보관 중입니다. 입력 복구 버튼으로 같은 수업을 이어갈 수 있어요.')
    : paused ? '정지한 동안의 소리는 녹음하거나 전송하지 않으며, 재개하면 같은 수업에 이어집니다.'
      : recording ? (system ? '선택한 탭이나 화면을 재생해 주세요. 화면 영상은 전송하지 않아요.' : clova ? '마이크 음성을 이 서버를 거쳐 운영자가 설정한 NAVER Cloud CLOVA Speech로 보내 받아씁니다.' : '서버가 늦어도 음성을 이 기기에 보관하며 녹음을 계속합니다.')
        : current ? '새 수업을 시작하거나 기록을 내려받을 수 있어요.' : system ? '시작한 뒤 재생할 탭·화면을 고르고 오디오 공유를 켜세요.' : clova ? '사이트 운영자가 설정한 CLOVA Speech로 마이크 음성을 받아써요.' : '약 8초 뒤 말이 잠시 멈출 때마다 정확하게 기록해요.';
  const localQueueSize = liveQueueBytes > 0 ? ` · ${bytesLabel(liveQueueBytes)}` : '';
  $('save-state').textContent = retryMessage ? `${queued}개 음성${localQueueSize} · 기기에 보관하고 재전송 대기`
    : queued ? `${queued}개 음성${localQueueSize} · 기기에 임시 보관`
      : liveQueuePersisting ? '방금 받은 음성을 기기에 안전하게 보관 중'
        : recoveryFinalizationRequired.size ? `${recoveryFinalizationRequired.size}개 수업 · 마지막 문장 마무리 확인 필요`
        : captureWarning ? '마지막 오디오 일부 누락 가능 · 받은 내용만 저장됨' : current ? '서버 컴퓨터에 저장됨' : '서버 컴퓨터에 저장';
  $('processing').hidden = !recording && !starting && !pausing && !resuming && !inputUnavailable && !queued && !sending;
  $('processing-text').textContent = sendError ? '음성은 계속 보관 중입니다. 안내를 확인해 전송을 이어 주세요.'
    : inputUnavailable ? '서버 전송과 별개로 오디오 입력 복구를 기다리고 있어요…'
      : retryMessage || (pausing ? '일시정지 지점까지 빠짐없이 정리하고 있어요…' : resuming ? '오디오 입력을 다시 연결하고 있어요…' : sending ? '음성을 글로 바꾸고 있어요. 녹음은 기다리지 않고 계속됩니다…' : '다음 문장을 듣고 있어요…');
  const recoveryWarning = recoveryFinalizationRequired.size
    ? `비정상적으로 닫힌 수업이 ${recoveryFinalizationRequired.size}개 있어요. 다른 탭에서 아직 녹음 중인지 먼저 확인하고, 중단된 수업이라면 해당 수업을 열어 “녹음 WAV 마무리”를 눌러 마지막 문장을 확정해 주세요.`
    : '';
  const showQueueWarning = !!sendError || !!recoveryWarning || !!liveCoordinationWarning
    || !!volatilePendingWarning
    || (!!liveQueueWarning && (liveSession || queued || liveQueuePersisting));
  $('queue-warning').hidden = !showQueueWarning;
  $('queue-message').textContent = [sendError ? `${sendError} 이미 받은 음성은 기기에 보관되어 있습니다.` : '',recoveryWarning,liveQueueWarning,liveCoordinationWarning,volatilePendingWarning].filter(Boolean).join(' ');
  $('retry').hidden = !sendError;
  $('retry').disabled = !sendError || sending || starting || pausing || resuming || stopping;
  $('retry').textContent = sendError && pending[0]?.asrProvider === 'clova' ? '위험 이해 · 수동 재전송' : '다시 전송';
  $('save-failed').hidden = !sendError || !pending.length;
  $('save-failed').disabled = sending || starting || pausing || resuming || stopping || !pending.length;
  $('skip-failed').hidden = !sendError || !pending[0]?.downloadRequested;
  $('skip-failed').disabled = sending || starting || pausing || resuming || stopping || !pending[0]?.downloadRequested;
  const canResumeImport = importJob?.status === 'uploading' && !fileUploader?.running;
  const liveAudioBusy = authenticating || loggingOut || recording || paused || starting || pausing || resuming || stopping || !!draft || pending.length > 0 || sending || recordingFinalizePending;
  $('recording-file').disabled = liveAudioBusy || importStarting || (activeImport && !canResumeImport);
  $('import-button').disabled = liveAudioBusy || importStarting || (activeImport && !canResumeImport) || !$('recording-file').files?.length;
  $('import-button').textContent = canResumeImport ? '같은 파일 이어 올리기' : '파일 올려 변환';
  for (const button of $('lecture-list').querySelectorAll('button')) button.disabled = historyNavigationBusy();
  updateCorrectionControls(noteToolsBusy);
  renderImportStatus();
  notePresenceStateChange();
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
  if (recording || paused || starting || pausing || resuming || stopping || draft || pending.length || sending) {
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
function startElapsedClock() {
  elapsedStartedAt = performance.now();
  clearInterval(timer);
  timer = setInterval(() => {
    $('elapsed').textContent = fmt((elapsedActiveMs + performance.now() - elapsedStartedAt) / 1000);
  }, 500);
}
function stopElapsedClock() {
  if (timer !== null) {
    elapsedActiveMs += Math.max(0,performance.now() - elapsedStartedAt);
    clearInterval(timer); timer = null;
  }
}
$('record-button').onclick = () => recording || paused || inputUnavailable ? void stopRecording() : void startRecording();
$('pause-button').onclick = () => inputUnavailable || paused ? void resumeRecording() : void pauseRecording();

function handleInputUnavailable(error, details = {}) {
  if (!capture || (!recording && !paused && !starting && !resuming)) return;
  inputUnavailable = true;
  inputReconnectNeeded = details.reconnectNeeded === true || capture.reconnectNeeded === true;
  inputUnavailableMessage = errorText(error);
  stopElapsedClock();
  if (inputReconnectNeeded) {
    recording = false;
    paused = true;
  }
  void persistLiveSessionState(captureSession,'input-unavailable');
  updateControls();
}

function handleInputRecovered() {
  if (!capture || (!inputUnavailable && !inputReconnectNeeded)) return;
  inputUnavailable = false;
  inputReconnectNeeded = false;
  inputUnavailableMessage = '';
  if (capture.recording) {
    paused = false;
    recording = true;
    startElapsedClock();
    void persistLiveSessionState(captureSession,'recording');
  } else if (capture.paused) {
    recording = false;
    paused = true;
    void persistLiveSessionState(captureSession,'paused');
  }
  notice('오디오 입력이 돌아와 같은 수업의 받아쓰기를 이어갑니다.');
  updateControls();
}

function handleReconnectNeeded(error, details = {}) {
  if (!capture) return;
  inputUnavailable = true;
  inputReconnectNeeded = true;
  inputUnavailableMessage = errorText(error);
  recording = false;
  paused = true;
  stopElapsedClock();
  void persistLiveSessionState(captureSession,'input-unavailable');
  updateControls();
}
async function startRecording() {
  if (isBusy() || importIsActive() || importStarting) return;
  if (current) resetNewNote();
  else if (lectureDateFilter) { lectureDateFilter = ''; renderHistory(); }
  const source = selectedCaptureSource();
  const asrProvider = source === 'system' ? 'qwen' : selectedAsrProvider();
  if (!transcriptionProviders[asrProvider]?.configured) {
    notice(asrProvider === 'clova'
      ? 'CLOVA Speech가 서버에 설정되지 않았어요. 서버 설정을 확인하거나 Qwen을 선택해 주세요.'
      : '이 PC의 Qwen을 사용할 수 있는지 서버 상태를 확인해 주세요.');
    updateControls(); return;
  }
  if (asrProvider === 'clova' && !['ko','en'].includes($('language').value)) {
    $('language').value = 'ko';
    notice('CLOVA 실시간 받아쓰기는 한국어와 영어만 지원해 한국어로 바꿨어요.');
  }
  ++requestGeneration; starting = true; paused = false; pausing = false; resuming = false; sendError = ''; captureWarning = '';
  inputUnavailable = false; inputReconnectNeeded = false; inputUnavailableMessage = '';
  sampleSeconds = 0; elapsedActiveMs = 0; elapsedStartedAt = 0; $('elapsed').textContent = '00:00'; updateControls();
  const title = $('lecture-title').value.trim() || `${dateLabel(new Date())} 수업`;
  const language = $('language').value === 'auto' ? null : $('language').value;
  const session = {
    id:crypto.randomUUID(), owner:user, lecture:null, buffered:[], title, language,
    source, asrProvider, cancelled:false, creating:null, assignmentTimer:null,
    assignmentAttempt:0, persistChain:Promise.resolve(), storeReady:null,
    captureLease:null, discardAudio:false, createdAt:Date.now(), nextRuntimeSequence:0,
  };
  const coordinationRequest = liveCoordination.acquireLiveCapture(session.owner);
  liveSessions.set(session.id,session);
  session.storeReady = prepareStoredLiveSession(session);
  draft = session; captureSession = session;
  const queue = chunk => {
    if (session.discardAudio) return;
    sampleSeconds = chunk.startSeconds + chunk.durationSeconds;
    enqueueChunk(session,chunk);
    updateControls();
  };
  capture = new MicrophoneCapture({
    source:session.source,
    onChunk:queue,
    onLevel:level => { $('mic-level').style.width = `${Math.min(100,Math.max(0,level) * 180)}%`; },
    onInputUnavailable:handleInputUnavailable,
    onInputRecovered:handleInputRecovered,
    onReconnectNeeded:handleReconnectNeeded,
  });
  try {
    // Invoke microphone/display capture directly in the click gesture, before network awaits.
    const startOperation = capture.start();
    const [lease] = await Promise.all([coordinationRequest,startOperation]);
    noteCoordinationSupport(lease.supported);
    if (!lease.acquired) {
      session.discardAudio = true;
      await capture.stop().catch(() => {});
      throw lease.error || new Error('같은 계정이 다른 탭에서 이미 녹음 중이에요. 기존 수업 탭에서 녹음을 종료한 뒤 다시 시작해 주세요.');
    }
    session.captureLease = lease;
    captureCoordinationLease = lease;
    if (session.cancelled) {
      await stopPromise?.catch(() => {});
      await releaseStoppedSessionCaptureLeaseWhenSafe(session);
    }
  } catch (error) {
    const unusedLease = session.captureLease ? null : await coordinationRequest.catch(() => null);
    if (unusedLease?.acquired) await unusedLease.release().catch(() => {});
    await stopRecording();
    await session.persistChain.catch(() => {});
    if (draft === session && !pending.some(chunk => chunk.captureId === session.id)) draft = null;
    if (!pending.some(chunk => chunk.captureId === session.id) && liveQueueAvailable) {
      await liveQueue.deleteSession(session.owner,session.id).catch(() => {});
    }
    if (!pending.some(chunk => chunk.captureId === session.id)) liveSessions.delete(session.id);
    if (queuedCount()) sendError = errorText(error);
    notice(errorText(error));
    starting = false; updateControls();
    return;
  }
  if (!session.cancelled) {
    recording = capture.recording;
    paused = capture.paused;
    if (recording && !inputUnavailable) startElapsedClock();
  }
  starting = false; updateControls();
  // Creating the server-side note is retried independently. Audio capture must
  // not end merely because the model PC or tunnel is temporarily unavailable.
  void ensureLectureAssigned(session);
}

function pauseRecording() {
  if (pausePromise) return pausePromise;
  if (!recording || pausing || resuming || stopping || !capture) return Promise.resolve();
  const microphone = capture;
  pausing = true; stopElapsedClock(); updateControls();
  const operation = microphone.pause();
  pausePromise = (async () => {
    try {
      await operation;
      if (capture !== microphone || stopping) return;
      recording = false; paused = true;
      elapsedActiveMs = sampleSeconds * 1000;
      $('elapsed').textContent = fmt(sampleSeconds);
      void persistLiveSessionState(captureSession,'paused');
      renderHistory();
      notice('받아쓰기를 일시정지했어요. 재개하면 같은 수업에 이어서 기록합니다.');
    } catch (error) {
      if (capture !== microphone) return;
      captureWarning = errorText(error);
      pausing = false;
      if (microphone.reconnectNeeded) {
        handleReconnectNeeded(error,{reconnectNeeded:true});
      } else {
        recording = microphone.recording;
        paused = microphone.paused;
        if (recording) startElapsedClock();
        notice(`일시정지 경계를 확인하지 못했지만 수업은 종료하지 않았어요. ${errorText(error)}`);
      }
    } finally {
      pausing = false;
      updateControls();
    }
  })().finally(() => { pausePromise = null; });
  return pausePromise;
}

function resumeRecording() {
  if (resumePromise) return resumePromise;
  if ((!paused && !inputUnavailable) || pausing || resuming || stopping || !capture) return Promise.resolve();
  const microphone = capture;
  resuming = true;
  // Invoke resume immediately in the click gesture so suspended AudioContexts
  // can be restored on browsers that enforce user activation.
  let operation;
  try {
    operation = inputReconnectNeeded || microphone.reconnectNeeded
      ? microphone.reconnect()
      : inputUnavailable ? microphone.resumeInput() : microphone.resume();
  } catch (error) {
    operation = Promise.reject(error);
  }
  updateControls();
  resumePromise = (async () => {
    try {
      const recovered = await operation;
      if (capture !== microphone || stopping) return;
      if (recovered === false) {
        inputUnavailable = true;
        inputReconnectNeeded = microphone.reconnectNeeded;
        paused = microphone.paused;
        recording = microphone.recording;
        stopElapsedClock();
        notice('오디오 처리는 재개됐지만 입력 신호가 아직 돌아오지 않았어요. 연결을 확인한 뒤 다시 시도해 주세요.');
        return;
      }
      inputUnavailable = false; inputReconnectNeeded = false; inputUnavailableMessage = '';
      paused = microphone.paused;
      recording = microphone.recording;
      void persistLiveSessionState(captureSession,paused ? 'paused' : 'recording');
      if (recording) startElapsedClock();
      renderHistory();
    } catch (error) {
      if (capture !== microphone) return;
      captureWarning = errorText(error);
      resuming = false;
      if (microphone.reconnectNeeded) handleReconnectNeeded(error,{reconnectNeeded:true});
      else {
        paused = microphone.paused;
        recording = microphone.recording;
        if (!recording) stopElapsedClock();
        notice(`아직 오디오 입력을 재개하지 못했지만 수업은 종료하지 않았어요. ${errorText(error)}`);
      }
    } finally {
      resuming = false;
      updateControls();
    }
  })().finally(() => { resumePromise = null; });
  return resumePromise;
}

function enqueueChunk(session, chunk) {
  const item = {
    ...chunk,
    id:crypto.randomUUID(),
    captureId:session.id,
    lectureId:session.id,
    owner:session.owner,
    asrProvider:session.asrProvider,
    sessionCreatedAt:session.createdAt,
    sequence:session.nextRuntimeSequence++,
    lectureReady:!!session.lecture,
    durable:false,
    byteLength:chunk.blob?.size || 0,
    persistPromise:Promise.resolve(),
  };
  pending.push(item);
  persistPendingChunk(session,item);
  if (item.lectureReady) void drain();
}
async function assignLecture(session) {
  if (session.lecture) return;
  if (session.creating) return session.creating;
  session.creating = (async () => {
    const assignmentOwner = user;
    const assignmentToken = token;
    const assignmentServer = apiUrl;
    const assignmentGeneration = requestGeneration;
    const lecture = await api('/lectures',{
      method:'POST',
      body:JSON.stringify({title:session.title,language:session.language,asr_provider:session.asrProvider}),
      headers:{'X-Lecture-Id':session.id},
    });
    if (session.owner !== assignmentOwner || user !== assignmentOwner
        || token !== assignmentToken || apiUrl !== assignmentServer
        || requestGeneration !== assignmentGeneration) {
      throw connectionChangedBeforeRequestError();
    }
    if (lecture?.id !== session.id) {
      throw new Error('서버가 요청한 수업 ID와 다른 수업을 반환해 음성 전송을 보류했어요.');
    }
    if (!lectureForStoredProvider(session,lecture)) {
      session.providerMismatch = true;
      throw new Error('서버가 요청한 음성 인식 방식과 다른 수업을 반환해 업로드를 보류했어요. 녹음은 이 기기에 계속 보관합니다.');
    }
    session.providerMismatch = false;
    session.lecture = lecture;
    session.assignmentAttempt = 0;
    if (session.assignmentTimer !== null) clearTimeout(session.assignmentTimer);
    session.assignmentTimer = null;
    for (const chunk of pending) {
      if (chunk.captureId === session.id) chunk.lectureReady = true;
    }
    if (session.durable && liveQueueAvailable) {
      await session.persistChain.catch(() => {});
      await liveQueue.updateSession(session.owner,session.id,{lectureCreated:true}).catch(setLiveQueueWarning);
    }
    if (draft === session) draft = null;
    const known = lectures.find(item => item.id === lecture.id);
    if (known) Object.assign(known,lecture);
    else lectures.unshift(lecture);
    if (captureSession === session || !current) {
      current = {...lecture,segments:lecture.segments || []};
      lectureDateFilter = '';
    }
    renderHistory(); renderCurrent();
  })().finally(() => { session.creating = null; });
  return session.creating;
}

function scheduleLectureAssignmentRetry(session, error) {
  if (!session || session.lecture || session.assignmentTimer !== null) return;
  session.assignmentAttempt = Math.max(0,Number(session.assignmentAttempt) || 0) + 1;
  const delay = Math.min(RETRY_MAX_MS,RETRY_BASE_MS * (2 ** Math.min(session.assignmentAttempt - 1,10)));
  retryMessage = `${errorText(error)} 녹음은 이 기기에 보관하며 ${Math.ceil(delay / 1000)}초 후 수업 연결을 다시 확인할게요.`;
  if (shouldRecoverPublishedServer(error)) void recoverPublishedServerAfterTransportFailure();
  session.assignmentTimer = setTimeout(() => {
    session.assignmentTimer = null;
    if (liveSessions.get(session.id) === session) void ensureLectureAssigned(session);
  },delay);
  updateControls();
}

async function ensureLectureAssigned(session) {
  if (!session || session.lecture || session.creating || !token || session.owner !== user) return;
  try {
    await assignLecture(session);
    if (!sendError) retryMessage = '';
    void drain();
  } catch (error) {
    if (retryableUpload(error) || !token) scheduleLectureAssignmentRetry(session,error);
    else {
      sendError = manualUploadError(error);
      // These queued chunks have not reached an ASR provider. Leave their
      // durable state queued so one explicit retry can resume the whole ordered
      // capture after the server-side lecture contract is repaired. A user who
      // elects to discard them must download each WAV first.
    }
  } finally {
    updateControls();
  }
}
function stopRecording(message) {
  if (stopPromise) return stopPromise;
  if (!recording && !paused && !starting && !pausing && !resuming && !capture) return Promise.resolve();
  if (captureSession) captureSession.cancelled = true;
  const microphone = capture;
  const session = captureSession;
  stopping = true; recording = false; paused = false; stopElapsedClock(); updateControls();
  stopPromise = (async () => {
    let flushWarning = '';
    try { await microphone?.stop(); }
    catch (error) { flushWarning = errorText(error); captureWarning = flushWarning; }
    finally {
      await session?.persistChain?.catch(error => setLiveQueueWarning(error));
      await persistLiveSessionState(session,'stopped');
      if (capture === microphone) capture = null;
      const released = await releaseStoppedSessionCaptureLeaseWhenSafe(session);
      if (!released) {
        volatilePendingWarning = '서버가 확인하지 않은 음성이 이 탭 메모리에 남아 있어 다른 탭의 수업 변경을 잠갔어요. 전송 또는 실패 음성 처리가 끝날 때까지 이 탭을 닫지 마세요.';
      }
      $('auth-capture-stop').hidden = true;
      inputUnavailable = false; inputReconnectNeeded = false; inputUnavailableMessage = '';
      recording = false; paused = false; pausing = false; resuming = false; stopping = false;
      $('mic-level').style.width = '0%';
      $('elapsed').textContent = fmt(sampleSeconds);
      renderHistory();
      updateControls();
      const combined = [message,flushWarning].filter(Boolean).join(' ');
      if (combined) notice(combined);
    }
  })().finally(() => { stopPromise = null; });
  return stopPromise;
}
$('auth-capture-stop').onclick = () => {
  if (capture && !stopping) {
    void stopRecording('이 기기의 녹음을 종료했습니다. 같은 계정으로 로그인하면 남은 음성 전송을 이어갑니다.');
  }
};
async function uploadBlob(chunk) {
  const source = await pendingChunkBlob(chunk);
  if (chunk.durationSeconds >= 0.05) return source;
  // The API requires >= 50 ms. Only a final tail may be padded: padding a
  // resumable non-final boundary would advance the stored WAV beyond the real
  // capture timeline and make the next real samples conflict.
  if (!chunk.final) return source;
  const original = new Uint8Array(await source.arrayBuffer());
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
  return error?.transient === true || error?.connectionChanged === true
    || error?.connectionLeaseExpired === true
    || RETRYABLE_UPLOAD_STATUSES.has(status)
    || (status >= 500 && status <= 599 && !PERMANENT_UPLOAD_STATUSES.has(status))
    || (status === 409 && Number.isFinite(error?.retryAfterMs));
}
function shouldRecoverPublishedServer(error) {
  const status = Number(error?.status);
  return error?.transient === true || error?.connectionChanged === true
    || error?.connectionLeaseExpired === true || (status >= 500 && status <= 599);
}
function clearUploadRetry() {
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null; retryAttempt = 0; retryMessage = '';
}
function nudgeQueuedUpload() {
  if (!pending.length && !draft) return;
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null; retryMessage = '';
  void recoverPublishedServerAfterTransportFailure();
  if (!sendError) {
    if (draft) void ensureLectureAssigned(draft);
    for (const session of liveSessions.values()) {
      if (!session.lecture && pending.some(chunk => chunk.captureId === session.id)) {
        void ensureLectureAssigned(session);
      }
    }
    if (token) void drain();
  }
  updateControls();
}
function scheduleUploadRetry(error) {
  retryAttempt += 1;
  const exponential = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** Math.min(retryAttempt - 1, 10)));
  const delay = Math.max(exponential, Math.min(error?.retryAfterMs || 0, 120000));
  retryMessage = `${errorText(error)} 녹음은 이 기기에 보관하며 ${Math.ceil(delay / 1000)}초 후 다시 전송할게요.`;
  if (shouldRecoverPublishedServer(error)) void recoverPublishedServerAfterTransportFailure();
  retryTimer = setTimeout(() => {
    retryTimer = null; retryMessage = ''; updateControls(); void drain();
  }, delay);
}

async function recoverPublishedServerAfterTransportFailure() {
  const local = ['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  if (local) return false;
  if (connectionRecoveryPromise) return connectionRecoveryPromise;
  const now = Date.now();
  if (now - lastConnectionRecoveryAt < 4000) return false;
  lastConnectionRecoveryAt = now;
  const previousServer = apiUrl;
  const recoveryOwner = user;
  connectionRecoveryPromise = (async () => {
    const controller = new AbortController();
    try {
      const config = await fetchRuntimeConfig(controller.signal,CONFIG_TIMEOUT_MS);
      const runtime = validateRuntimeConfig(config);
      if (runtime.state !== 'online') return false;
      const candidate = await verifyServerCandidate(runtime.apiUrl,controller.signal,10000);
      if (candidate === previousServer) {
        if (apiUrl === previousServer) {
          verifiedApiUrl = candidate;
          verifiedApiExpiresAt = runtime.expiresAt;
          setConnectionState('connected','현재 서버 연결을 다시 확인했어요.');
          scheduleConnectionLeaseTimer();
        }
        return false;
      }
      if (apiUrl !== previousServer) return false;
      const installCandidate = () => {
        if (apiUrl !== previousServer || user !== recoveryOwner) return false;
        const changed = installVerifiedServer(
          candidate,
          '새 서버 주소를 찾았어요. 녹음은 계속 보관 중이며 같은 계정으로 다시 로그인하면 전송을 이어갑니다.',
          {expiresAt:runtime.expiresAt},
        );
        if (changed && recoveryOwner) $('username').value = recoveryOwner;
        return changed;
      };
      if (!recoveryOwner) return installCandidate();
      // The probe is anonymous and may overlap an old-origin request, but the
      // actual origin/token switch waits until every same-owner uploader has
      // left its critical section.
      const coordinated = await liveCoordination.runUploader(recoveryOwner,installCandidate);
      noteCoordinationSupport(coordinated.supported);
      return coordinated.value;
    } catch {
      return false;
    }
  })().finally(() => { connectionRecoveryPromise = null; });
  return connectionRecoveryPromise;
}
function manualUploadError(error) {
  if (error?.connectionLeaseExpired || error?.connectionChanged) return errorText(error);
  const reason = PERMANENT_UPLOAD_STATUSES.has(error?.status)
    ? '서버가 요청을 거절해 자동 재시도를 멈췄어요.'
    : '예상하지 못한 오류라 자동 재시도를 멈췄어요.';
  return `${errorText(error)} ${reason}`;
}
function clovaManualUploadError(error) {
  return `${errorText(error)} CLOVA 처리 결과를 확인할 수 없어 자동 재전송하지 않았어요. 같은 음성 조각을 다시 보내면 중복 기록이 생길 수 있습니다. 실패 WAV를 내려받아 보관하고 새 수업으로 다시 시작하거나, 위험을 이해한 경우에만 수동 재전송하세요.`;
}
function mergeChunkSegments(lecture, response) {
  if (!lecture) return;
  if (!Array.isArray(lecture.segments)) lecture.segments = [];
  const ids = new Set(lecture.segments.map(segment => segment.id));
  for (const segment of response?.segments || []) {
    if (!ids.has(segment.id)) { lecture.segments.push(segment); ids.add(segment.id); }
  }
  lecture.segments.sort((a,b) => a.start - b.start);
}
async function drain() {
  if (sending || liveQueueRecoveryPromise || sendError || retryTimer !== null || !token || !pending.length) return;
  const drainOwner = user;
  sending = true; updateControls();
  try {
    const coordinated = await liveCoordination.runUploader(drainOwner,async () => {
    while (pending.length && token && user === drainOwner) {
      const chunk = pending[0];
      if (chunk.owner !== drainOwner) {
        sendError = '다른 계정의 음성은 현재 로그인으로 전송할 수 없어요.';
        break;
      }
      if (manualRetryApprovedIds.has(chunk.id)) {
        await chunk.persistPromise;
        if (chunk.durable && liveQueueAvailable) {
          const stored = await liveQueue.getChunk(drainOwner,chunk.id);
          if (!stored) {
            pending.splice(0,1);
            manualRetryApprovedIds.delete(chunk.id);
            await finishRemovedPendingChunk(chunk);
            void refreshLiveQueueStats();
            continue;
          }
          if (stored.state === 'blocked' || stored.state === 'inflight') {
            await markPendingQueued(chunk);
          } else {
            chunk.blocked = false;
            chunk.inflight = false;
          }
        } else {
          chunk.blocked = false;
          chunk.inflight = false;
        }
        manualRetryApprovedIds.delete(chunk.id);
      }
      if (chunk.blocked) {
        sendError = chunk.asrProvider === 'clova'
          ? '이 CLOVA 음성 조각은 이전 처리 결과를 확인하지 못해 자동 재전송하지 않았어요.'
          : '이 음성 조각은 이전 오류를 확인한 뒤 직접 재전송해야 해요.';
        break;
      }
      const session = liveSessions.get(chunk.captureId);
      if (!session || session.owner !== chunk.owner || session.asrProvider !== chunk.asrProvider) {
        sendError = '기기에 보관된 음성과 수업의 인식 설정을 안전하게 확인하지 못해 전송을 보류했어요.';
        break;
      }
      // Recheck even a previously ready item. A lecture list refresh or a
      // recovered in-memory flag must never route locally captured Qwen audio
      // into a CLOVA lecture (or the reverse).
      const listedLecture = lectures.find(lecture => lecture.id === chunk.lectureId) || null;
      const candidate = listedLecture || session.lecture || null;
      const known = lectureForStoredProvider(session,candidate);
      if (!known) {
        chunk.lectureReady = false;
        session.lecture = null;
        if (candidate) {
          session.providerMismatch = true;
          sendError = '서버 수업의 음성 인식 방식이 보관된 음성과 달라 전송을 보류했어요.';
        } else {
          void ensureLectureAssigned(session);
        }
        break;
      }
      session.providerMismatch = false;
      session.lecture = known;
      chunk.lectureReady = true;
      let blob;
      try {
        blob = await uploadBlob(chunk);
      } catch (error) {
        if (error?.chunkAlreadyHandled) {
          const handledIndex = pending.findIndex(item => item.id === chunk.id);
          if (handledIndex >= 0) pending.splice(handledIndex,1);
          await finishRemovedPendingChunk(chunk);
          void refreshLiveQueueStats();
          continue;
        }
        throw error;
      }
      let response;
      try {
        // Persist the ambiguity boundary before a CLOVA request leaves the
        // browser. If this tab dies after the provider may have accepted the
        // audio, recovery presents a manual decision instead of replaying it.
        if (chunk.asrProvider === 'clova') await markPendingInflight(chunk);
        response = await api(`/lectures/${chunk.lectureId}/chunks`,{method:'POST',body:blob,uploaderAlreadyLocked:true,headers:{'Content-Type':'audio/wav','X-Chunk-Id':chunk.id,'X-Start-Seconds':String(chunk.startSeconds),'X-Overlap-Seconds':String(chunk.overlapSeconds ?? 0),'X-Final-Chunk':chunk.final ? 'true' : 'false'}},UPLOAD_TIMEOUT_MS);
      } catch (error) {
        if (chunk.asrProvider === 'clova'
            && (error?.connectionChanged === true || error?.connectionLeaseExpired === true)) {
          // api() creates these errors before fetch. The provider cannot have
          // accepted this WAV, so undo the preflight inflight marker instead
          // of presenting a false duplicate/charge warning.
          try {
            await markPendingQueued(chunk);
          } catch (storeError) {
            sendError = `${errorText(storeError)} CLOVA 요청은 보내지 않았지만 안전한 전송 상태를 복구하지 못했어요.`;
            break;
          }
          if (!token && user) {
            retryMessage = '같은 계정으로 다시 로그인하면 보관된 음성을 이어서 전송합니다.';
          } else {
            scheduleUploadRetry(error);
          }
          break;
        }
        if (!token && user) {
          if (chunk.asrProvider === 'clova') {
            sendError = clovaManualUploadError(error);
            await markPendingBlocked(chunk,error);
          } else {
            retryMessage = '같은 계정으로 다시 로그인하면 보관된 음성을 이어서 전송합니다.';
          }
          break;
        }
        if (chunk.asrProvider === 'clova') {
          if (shouldRecoverPublishedServer(error)) void recoverPublishedServerAfterTransportFailure();
          sendError = clovaManualUploadError(error);
          await markPendingBlocked(chunk,error);
          break;
        }
        if (retryableUpload(error)) {
          scheduleUploadRetry(error);
          break;
        }
        sendError = manualUploadError(error);
        await markPendingBlocked(chunk,error);
        break;
      }
      const activeLecture = captureSession?.lecture?.id === chunk.lectureId ? captureSession.lecture : null;
      mergeChunkSegments(activeLecture,response);
      if (current?.id === chunk.lectureId && current !== activeLecture) mergeChunkSegments(current,response);
      applyRecordingFlags(chunk.lectureId,response);
      await acknowledgePendingChunk(chunk);
      const acknowledgedIndex = pending.findIndex(item => item.id === chunk.id);
      if (acknowledgedIndex >= 0) pending.splice(acknowledgedIndex,1);
      await finishRemovedPendingChunk(chunk);
      retryAttempt = 0; retryMessage = '';
      if (current?.id === chunk.lectureId) renderCurrent();
      else updateControls();
    }
    });
    noteCoordinationSupport(coordinated.supported);
  } catch (error) {
    if (error?.coordinationRetrySafe) {
      scheduleUploadRetry(error);
    } else {
      sendError = pending[0]?.asrProvider === 'clova' ? clovaManualUploadError(error) : manualUploadError(error);
      await markPendingBlocked(pending[0],error);
    }
  } finally {
    sending = false; updateControls();
    // Pick up a final microphone tail that may have arrived while an upload awaited.
    if (pending.length && token && !sendError && retryTimer === null) void drain();
  }
}
async function retryPending() {
  if (sending || starting || pausing || resuming || stopping) return;
  clearUploadRetry(); sendError = '';
  if (pending[0]) manualRetryApprovedIds.add(pending[0].id);
  if (draft) {
    await ensureLectureAssigned(draft);
  }
  void drain();
}
$('retry').onclick = () => { void retryPending(); };
async function saveFailedChunk() {
  if (!sendError || sending || starting || pausing || resuming || stopping || !pending.length) return;
  const chunk = pending[0];
  try {
    const blob = await uploadBlob(chunk);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const failedLecture = current?.id === chunk.lectureId ? current
      : captureSession?.lecture?.id === chunk.lectureId ? captureSession.lecture
        : lectures.find(lecture => lecture.id === chunk.lectureId);
    const title = (failedLecture?.title || '수업').replace(/[<>:"/\\|?*\u0000-\u001F]/g,'_').slice(0,80);
    link.href = url;
    link.download = `${title}_${fmt(chunk.startSeconds).replace(':','-')}_처리실패.wav`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url),1000);
    chunk.downloadRequested = true;
    if (chunk.durable && liveQueueAvailable) {
      await liveQueue.setDownloadRequested(chunk.owner,chunk.id,true).catch(setLiveQueueWarning);
    }
    notice('다운로드한 WAV 파일이 기기에 저장됐는지 확인한 뒤 건너뛰기를 눌러 주세요.');
    updateControls();
  } catch (error) {
    notice(`WAV를 저장하지 못했습니다. ${errorText(error)}`);
  }
}
$('save-failed').onclick = () => { void saveFailedChunk(); };
function applyRecordingFlags(lectureId, result) {
  // A manual recording finalization can recover the recognizer's withheld
  // stability tail from the already stored WAV. Merge that additive response
  // before releasing the live capture or starting its reserved correction, so
  // both the screen and the correction revision include the final words.
  const targets = new Set([
    captureSession?.lecture?.id === lectureId ? captureSession.lecture : null,
    lectures.find(lecture => lecture.id === lectureId),
    current?.id === lectureId ? current : null,
  ].filter(Boolean));
  for (const lecture of targets) mergeChunkSegments(lecture,result);
  const state = {
    recording_available: !!result?.recording_available,
    recording_finalized: !!result?.recording_finalized,
  };
  const summary = lectures.find(lecture => lecture.id === lectureId);
  if (summary) Object.assign(summary,state);
  if (current?.id === lectureId) Object.assign(current,state);
  if (state.recording_finalized) {
    recoveryFinalizationRequired.delete(lectureId);
    const recoveredSession = liveSessions.get(lectureId);
    if (recoveredSession?.recovered && !pending.some(chunk => chunk.captureId === lectureId)) {
      liveSessions.delete(lectureId);
      if (liveQueueAvailable) {
        void liveQueue.deleteSession(recoveredSession.owner,lectureId).catch(setLiveQueueWarning);
      }
    }
    const scheduled = scheduledCorrectionFor(lectureId);
    // The server's owner-checked final response is the durable hand-off point.
    // Mark the reservation independently of whichever newer capture may now be
    // in memory, then let the correction endpoint verify the final transcript.
    if (scheduled) scheduled.finalized = true;
    void submitScheduledCorrection(lectureId);
    if (!scheduled) releaseFinalizedCapture(lectureId);
  }
  return state;
}
async function requestRecordingFinalization(lectureId, operationIsCurrent) {
  const path = `/lectures/${encodeURIComponent(lectureId)}/recording-finalize`;
  let retriedLostResponse = false;
  let contentionRetries = 0;
  while (operationIsCurrent()) {
    try {
      return await api(path, {method:'POST',uploaderAlreadyLocked:true},UPLOAD_TIMEOUT_MS);
    } catch (error) {
      if (!operationIsCurrent()) throw error;
      // A browser timeout can leave the deterministic server-side final guard
      // running. Reconcile one lost response immediately; if that retry sees
      // the guard claim, respect Retry-After instead of starting duplicate GPU
      // work or reporting a false failure.
      if (error?.transient && !retriedLostResponse) {
        retriedLostResponse = true;
        continue;
      }
      if (error?.status === 409 && Number.isFinite(error?.retryAfterMs)
          && contentionRetries < MAX_RECORDING_FINALIZE_CONTENTION_RETRIES) {
        contentionRetries += 1;
        const delay = Math.max(0,Math.min(error.retryAfterMs,10000));
        if (delay) await new Promise(resolve => setTimeout(resolve,delay));
        continue;
      }
      throw error;
    }
  }
  throw connectionChangedBeforeRequestError();
}
async function finalizeSkippedRecording(lectureId) {
  if (!lectureId || !token || recordingFinalizePending) return;
  const owner = user, sessionToken = token, server = apiUrl;
  const generation = requestGeneration, sequence = ++noteActionSequence;
  const operationIsCurrent = () => sequence === noteActionSequence && generation === requestGeneration
    && owner === user && sessionToken === token && server === apiUrl;
  recordingFinalizePending = true; updateControls();
  try {
    const result = await runWhenOwnerCaptureIdle(
      owner,
      lectureId,
      () => requestRecordingFinalization(lectureId,operationIsCurrent),
    );
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
async function discardEmptyUnassignedDraft(captureId) {
  const session = draft?.id === captureId ? draft : null;
  if (!session || session.lecture || pending.some(chunk => chunk.captureId === captureId)
      || (captureSession?.id === captureId && capture)) return false;
  session.cancelled = true;
  if (session.assignmentTimer !== null) clearTimeout(session.assignmentTimer);
  session.assignmentTimer = null;
  draft = null;
  if (captureSession?.id === captureId) captureSession = null;
  liveSessions.delete(captureId);
  recoveryFinalizationRequired.delete(captureId);
  if (session.durable && liveQueueAvailable) {
    try { await liveQueue.deleteSession(session.owner,captureId); }
    catch (error) { setLiveQueueWarning(error); }
  }
  return true;
}
async function skipFailedChunk() {
  if (!sendError || sending || starting || pausing || resuming || stopping || !pending[0]?.downloadRequested) return;
  const skipped = pending[0];
  const owner = user;
  const unresolvedError = sendError;
  sending = true; updateControls();
  try {
    const coordinated = await liveCoordination.runUploader(owner,async () => {
      if (owner !== user || pending[0]?.id !== skipped.id) return false;
      await acknowledgePendingChunk(skipped,{strict:true});
      const skippedIndex = pending.findIndex(item => item.id === skipped.id);
      if (skippedIndex >= 0) pending.splice(skippedIndex,1);
      await finishRemovedPendingChunk(skipped);
      return true;
    });
    noteCoordinationSupport(coordinated.supported);
    if (!coordinated.value) return;
    clearUploadRetry();
    const hasRemaining = pending.some(chunk => chunk.captureId === skipped.captureId);
    const activeUnassignedCapture = !hasRemaining && draft?.id === skipped.captureId
      && !draft.lecture && captureSession?.id === skipped.captureId && !!capture;
    sendError = activeUnassignedCapture ? unresolvedError : '';
    const discardedDraft = !hasRemaining && await discardEmptyUnassignedDraft(skipped.captureId);
    notice(activeUnassignedCapture
      ? '파일 저장을 확인한 음성 조각을 건너뛰었어요. 녹음은 계속되며, 수업 연결을 다시 시도하거나 다음 실패 음성을 확인해 주세요.'
      : discardedDraft
      ? '파일 저장을 확인한 음성을 건너뛰고 연결되지 않은 수업을 정리했어요. 새 수업을 시작할 수 있습니다.'
      : '파일 저장을 확인한 음성 조각을 건너뛰었어요. 해당 구간은 기록에서 빠집니다.');
    renderCurrent();
    if (!hasRemaining && !activeUnassignedCapture && !discardedDraft) void finalizeSkippedRecording(skipped.lectureId);
  } catch (error) {
    notice(`음성 조각을 건너뛰지 못했습니다. ${errorText(error)}`);
  } finally {
    sending = false; updateControls();
    if (pending.length && token && !sendError && retryTimer === null) void drain();
  }
}
$('skip-failed').onclick = () => { void skipFailedChunk(); };
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
      const result = await runWhenOwnerCaptureIdle(
        owner,
        lectureId,
        () => requestRecordingFinalization(lectureId,operationIsCurrent),
      );
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
    await api(path, {method:'DELETE',uploaderAlreadyLocked:true});
    return;
  } catch (error) {
    if (!error?.transient || !operationIsCurrent()) throw error;
  }
  // A timeout or broken connection can hide a successful response. The server
  // keeps deletion idempotent, so one retry safely completes or confirms it.
  await api(path, {method:'DELETE',uploaderAlreadyLocked:true});
}
function finishDeletedLecture(target) {
  lectures = lectures.filter(lecture => lecture.id !== target.id);
  scheduledCorrections.delete(target.id);
  recoveryFinalizationRequired.delete(target.id);
  liveSessions.delete(target.id);
  if (liveQueueAvailable) void liveQueue.deleteSession(target.owner,target.id).catch(setLiveQueueWarning);
  if (!capture && captureSession?.lecture?.id === target.id) captureSession = null;
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
    await runWhenOwnerCaptureIdle(
      target.owner,
      target.id,
      () => requestLectureDeletion(target,operationIsCurrent),
    );
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
    const preserveOwner = !!user && hasOwnerLockedWork();
    loggingOut = false;
    showLogin(!preserveOwner);
  }
};
window.addEventListener('beforeunload', event => { if (isBusy()) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('visibilitychange', () => {
  if (document.hidden && recording) notice(selectedCaptureSource() === 'system' ? '공유 중인 탭이나 화면을 유지해 주세요. 브라우저가 오디오 공유를 중단할 수 있어요.' : '이 탭과 화면을 유지해 주세요. 기기가 녹음을 중단할 수 있어요.');
  notePresenceStateChange();
  if (!document.hidden) nudgeQueuedUpload();
  if (!document.hidden && adminAuthorized && $('admin-dialog').open && !adminLoading && !adminAction) void loadAdminOverview();
});
window.addEventListener('online',nudgeQueuedUpload);

async function init() {
  void openLiveQueue();
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
