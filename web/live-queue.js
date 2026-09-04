export const LIVE_QUEUE_DB_NAME = 'yeobaek-live-audio';
export const LIVE_QUEUE_DB_VERSION = 1;
export const LIVE_AUDIO_SAMPLE_RATE = 16000;
export const MAX_LIVE_CHUNK_BYTES = 4 * 1024 * 1024;
const INDEXED_DB_OPEN_TIMEOUT_MS = 10000;
const INDEXED_DB_TRANSACTION_TIMEOUT_MS = 10000;

const SESSION_STORE = 'sessions';
const CHUNK_STORE = 'chunks';
const SESSION_STATES = new Set([
  'recording',
  'paused',
  'input-unavailable',
  'stopped',
  'blocked',
  'completed',
]);
// `inflight` is deliberately durable.  A CLOVA request can reach the provider
// even when the browser never receives its response, so a restored inflight
// chunk must not be sent again automatically.
const CHUNK_STATES = new Set(['queued', 'inflight', 'blocked']);
const SOURCES = new Set(['microphone', 'system']);
const PROVIDERS = new Set(['qwen', 'clova']);
const LANGUAGES = new Set([null, 'ko', 'en']);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ERROR_KIND = /^(?:|[a-z][a-z0-9_-]{0,63})$/;
const MAX_TIMESTAMP = 8_640_000_000_000_000;
const HIGH_TEXT = '\uffff';

export class LiveQueueError extends Error {
  constructor(message, { code = 'live_queue_error', cause } = {}) {
    super(message);
    this.name = 'LiveQueueError';
    this.code = code;
    if (cause !== undefined) this.cause = cause;
  }
}

export class LiveQueueUnavailableError extends LiveQueueError {
  constructor(message = '이 브라우저에서 안전한 임시 음성 저장소를 사용할 수 없습니다.', options = {}) {
    super(message, { ...options, code: options.code || 'live_queue_unavailable' });
    this.name = 'LiveQueueUnavailableError';
  }
}

export class LiveQueueQuotaError extends LiveQueueError {
  constructor(message = '브라우저의 음성 임시 저장 공간이 부족합니다.', options = {}) {
    super(message, { ...options, code: 'live_queue_quota' });
    this.name = 'LiveQueueQuotaError';
  }
}

export class LiveQueueValidationError extends LiveQueueError {
  constructor(message, options = {}) {
    super(message, { ...options, code: 'live_queue_validation' });
    this.name = 'LiveQueueValidationError';
  }
}

export class LiveQueueCorruptError extends LiveQueueError {
  constructor(message = '기기에 보관된 음성 대기열을 안전하게 읽을 수 없습니다.', options = {}) {
    super(message, { ...options, code: 'live_queue_corrupt' });
    this.name = 'LiveQueueCorruptError';
  }
}

export class LiveQueueConflictError extends LiveQueueError {
  constructor(message, options = {}) {
    super(message, { ...options, code: 'live_queue_conflict' });
    this.name = 'LiveQueueConflictError';
  }
}

export class LiveQueueOwnershipError extends LiveQueueError {
  constructor(options = {}) {
    super('이 음성은 현재 계정의 대기열에 속하지 않습니다.', {
      ...options,
      code: 'live_queue_owner_mismatch',
    });
    this.name = 'LiveQueueOwnershipError';
  }
}

export class LiveQueueNotFoundError extends LiveQueueError {
  constructor(message = '기기에 보관된 음성 대기열 항목을 찾을 수 없습니다.', options = {}) {
    super(message, { ...options, code: 'live_queue_not_found' });
    this.name = 'LiveQueueNotFoundError';
  }
}

export function isLiveQueueUnavailableError(error) {
  return error instanceof LiveQueueUnavailableError
    || error?.code === 'live_queue_unavailable'
    || error?.code === 'indexeddb_blocked'
    || error?.code === 'indexeddb_open_failed';
}

function storageError(error, operation = '음성 대기열을 처리하지 못했습니다.') {
  if (error instanceof LiveQueueError) return error;
  if (error?.name === 'QuotaExceededError') return new LiveQueueQuotaError(undefined, { cause: error });
  if (['SecurityError', 'InvalidStateError', 'NotSupportedError', 'VersionError'].includes(error?.name)) {
    return new LiveQueueUnavailableError('브라우저가 안전한 임시 음성 저장소 사용을 허용하지 않았습니다.', {
      code: 'indexeddb_open_failed',
      cause: error,
    });
  }
  return new LiveQueueError(operation, { code: 'indexeddb_operation_failed', cause: error });
}

function ownObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function onlyKeys(value, allowed, label) {
  if (!ownObject(value)) throw new LiveQueueValidationError(`${label} 형식이 올바르지 않습니다.`);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new LiveQueueValidationError(`${label}에 허용되지 않은 값이 있습니다.`);
  }
}

function finiteInteger(value, label, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new LiveQueueValidationError(`${label} 값이 올바르지 않습니다.`);
  }
  return value;
}

function cleanOwner(value) {
  if (typeof value !== 'string' || value !== value.trim()
      || !value.length || Array.from(value).length > 32 || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new LiveQueueValidationError('대기열 계정 정보가 올바르지 않습니다.');
  }
  return value;
}

function cleanUuid(value, label) {
  if (typeof value !== 'string' || !UUID.test(value)) {
    throw new LiveQueueValidationError(`${label}이 올바르지 않습니다.`);
  }
  return value.toLowerCase();
}

function cleanTitle(value) {
  if (typeof value !== 'string' || value !== value.trim()
      || !value.length || Array.from(value).length > 120 || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new LiveQueueValidationError('수업 이름이 올바르지 않습니다.');
  }
  return value;
}

function cleanLanguage(value) {
  if (!LANGUAGES.has(value)) throw new LiveQueueValidationError('수업 언어가 올바르지 않습니다.');
  return value;
}

function cleanSource(value) {
  if (!SOURCES.has(value)) throw new LiveQueueValidationError('녹음 입력 종류가 올바르지 않습니다.');
  return value;
}

function cleanProvider(value) {
  if (!PROVIDERS.has(value)) throw new LiveQueueValidationError('음성 인식 방식이 올바르지 않습니다.');
  return value;
}

function cleanSessionState(value) {
  if (!SESSION_STATES.has(value)) throw new LiveQueueValidationError('녹음 세션 상태가 올바르지 않습니다.');
  return value;
}

function validateSessionPolicy(source, provider, language) {
  if (source === 'system' && provider !== 'qwen') {
    throw new LiveQueueValidationError('화면 소리는 로컬 음성 인식 대기열에만 저장할 수 있습니다.');
  }
  if (provider === 'clova' && (source !== 'microphone' || !['ko', 'en'].includes(language))) {
    throw new LiveQueueValidationError('CLOVA 마이크 수업은 한국어 또는 영어를 직접 선택해야 합니다.');
  }
}

function cleanErrorKind(value) {
  if (typeof value !== 'string' || !ERROR_KIND.test(value)) {
    throw new LiveQueueValidationError('대기열 오류 종류가 올바르지 않습니다.');
  }
  return value;
}

function isBlob(value) {
  return !!value && typeof value === 'object' && typeof value.arrayBuffer === 'function'
    && typeof value.slice === 'function' && Number.isSafeInteger(value.size);
}

function ascii(view, offset, length) {
  let value = '';
  for (let index = 0; index < length; index += 1) value += String.fromCharCode(view.getUint8(offset + index));
  return value;
}

async function validateWavBlob(blob, durationSamples) {
  if (!isBlob(blob) || blob.size < 46 || blob.size > MAX_LIVE_CHUNK_BYTES
      || String(blob.type || '').toLowerCase() !== 'audio/wav') {
    throw new LiveQueueValidationError('음성 조각 파일이 올바르지 않습니다.');
  }
  const expectedSize = 44 + durationSamples * 2;
  if (blob.size !== expectedSize) {
    throw new LiveQueueValidationError('음성 조각 길이와 WAV 크기가 일치하지 않습니다.');
  }
  let header;
  try {
    header = new DataView(await blob.slice(0, 44).arrayBuffer());
  } catch (error) {
    throw new LiveQueueValidationError('음성 조각 WAV 머리말을 읽지 못했습니다.', { cause: error });
  }
  if (header.byteLength !== 44
      || ascii(header, 0, 4) !== 'RIFF'
      || header.getUint32(4, true) !== blob.size - 8
      || ascii(header, 8, 4) !== 'WAVE'
      || ascii(header, 12, 4) !== 'fmt '
      || header.getUint32(16, true) !== 16
      || header.getUint16(20, true) !== 1
      || header.getUint16(22, true) !== 1
      || header.getUint32(24, true) !== LIVE_AUDIO_SAMPLE_RATE
      || header.getUint32(28, true) !== LIVE_AUDIO_SAMPLE_RATE * 2
      || header.getUint16(32, true) !== 2
      || header.getUint16(34, true) !== 16
      || ascii(header, 36, 4) !== 'data'
      || header.getUint32(40, true) !== durationSamples * 2) {
    throw new LiveQueueValidationError('지원하지 않는 WAV 음성 조각입니다.');
  }
}

async function sameBlobBytes(left, right) {
  if (left.size !== right.size || String(left.type) !== String(right.type)) return false;
  if (left === right) return true;
  let leftBytes;
  let rightBytes;
  try {
    [leftBytes, rightBytes] = await Promise.all([left.arrayBuffer(), right.arrayBuffer()]);
  } catch (error) {
    throw new LiveQueueValidationError('중복 음성 조각의 내용을 확인하지 못했습니다.', { cause: error });
  }
  const first = new Uint8Array(leftBytes);
  const second = new Uint8Array(rightBytes);
  if (first.length !== second.length) return false;
  for (let index = 0; index < first.length; index += 1) {
    if (first[index] !== second[index]) return false;
  }
  return true;
}

function normalizeSessionInput(value, now) {
  onlyKeys(value, new Set(['id', 'owner', 'title', 'language', 'source', 'asrProvider', 'createdAt']), '녹음 세션');
  const createdAt = value.createdAt === undefined ? now : value.createdAt;
  const session = {
    id: cleanUuid(value.id, '녹음 세션 ID'),
    owner: cleanOwner(value.owner),
    title: cleanTitle(value.title),
    language: cleanLanguage(value.language === undefined ? 'ko' : value.language),
    source: cleanSource(value.source),
    asrProvider: cleanProvider(value.asrProvider),
    createdAt: finiteInteger(createdAt, '녹음 시작 시각', { min: 1, max: MAX_TIMESTAMP }),
  };
  validateSessionPolicy(session.source, session.asrProvider, session.language);
  return session;
}

function newSession(input, now) {
  return {
    ...input,
    updatedAt: now,
    state: 'recording',
    lectureCreated: false,
    finalQueued: false,
    nextSequence: 0,
    capturedSamples: 0,
  };
}

function validateSessionRecord(value) {
  const expected = new Set([
    'id', 'owner', 'title', 'language', 'source', 'asrProvider', 'createdAt', 'updatedAt',
    'state', 'lectureCreated', 'finalQueued', 'nextSequence', 'capturedSamples',
  ]);
  onlyKeys(value, expected, '저장된 녹음 세션');
  if (Object.keys(value).length !== expected.size) {
    throw new LiveQueueValidationError('저장된 녹음 세션에 필요한 값이 없습니다.');
  }
  cleanUuid(value.id, '녹음 세션 ID');
  cleanOwner(value.owner);
  cleanTitle(value.title);
  cleanLanguage(value.language);
  cleanSource(value.source);
  cleanProvider(value.asrProvider);
  validateSessionPolicy(value.source, value.asrProvider, value.language);
  finiteInteger(value.createdAt, '녹음 시작 시각', { min: 1, max: MAX_TIMESTAMP });
  finiteInteger(value.updatedAt, '녹음 갱신 시각', { min: 1, max: MAX_TIMESTAMP });
  cleanSessionState(value.state);
  if (typeof value.lectureCreated !== 'boolean' || typeof value.finalQueued !== 'boolean') {
    throw new LiveQueueValidationError('저장된 녹음 세션 표시가 올바르지 않습니다.');
  }
  finiteInteger(value.nextSequence, '다음 음성 순서');
  finiteInteger(value.capturedSamples, '저장된 음성 길이');
  return value;
}

function sessionCopy(value) {
  return {
    id: value.id,
    owner: value.owner,
    title: value.title,
    language: value.language,
    source: value.source,
    asrProvider: value.asrProvider,
    createdAt: value.createdAt,
    updatedAt: value.updatedAt,
    state: value.state,
    lectureCreated: value.lectureCreated,
    finalQueued: value.finalQueued,
    nextSequence: value.nextSequence,
    capturedSamples: value.capturedSamples,
  };
}

function normalizeChunkInput(value) {
  onlyKeys(value, new Set([
    'id', 'startSamples', 'durationSamples', 'overlapSamples', 'final', 'blob',
  ]), '음성 조각');
  const normalized = {
    id: cleanUuid(value.id, '음성 조각 ID'),
    startSamples: finiteInteger(value.startSamples, '음성 시작 위치'),
    durationSamples: finiteInteger(value.durationSamples, '음성 길이', { min: 1 }),
    overlapSamples: finiteInteger(value.overlapSamples, '음성 겹침 길이'),
    final: value.final,
    blob: value.blob,
  };
  if (typeof normalized.final !== 'boolean') {
    throw new LiveQueueValidationError('마지막 음성 조각 표시가 올바르지 않습니다.');
  }
  if (normalized.overlapSamples > normalized.durationSamples
      || (!normalized.final && normalized.overlapSamples >= normalized.durationSamples)) {
    throw new LiveQueueValidationError('음성 조각의 겹침 길이가 올바르지 않습니다.');
  }
  if (!Number.isSafeInteger(normalized.startSamples + normalized.durationSamples)) {
    throw new LiveQueueValidationError('음성 조각의 끝 위치가 너무 큽니다.');
  }
  return normalized;
}

function validateChunkRecord(value) {
  const expected = new Set([
    'id', 'captureId', 'owner', 'sessionCreatedAt', 'sequence', 'startSamples',
    'durationSamples', 'overlapSamples', 'final', 'asrProvider', 'blob', 'byteLength',
    'state', 'attempts', 'errorKind', 'downloadRequested', 'createdAt', 'updatedAt',
  ]);
  onlyKeys(value, expected, '저장된 음성 조각');
  if (Object.keys(value).length !== expected.size) {
    throw new LiveQueueValidationError('저장된 음성 조각에 필요한 값이 없습니다.');
  }
  cleanUuid(value.id, '음성 조각 ID');
  cleanUuid(value.captureId, '녹음 세션 ID');
  cleanOwner(value.owner);
  finiteInteger(value.sessionCreatedAt, '녹음 시작 시각', { min: 1, max: MAX_TIMESTAMP });
  finiteInteger(value.sequence, '음성 순서');
  finiteInteger(value.startSamples, '음성 시작 위치');
  finiteInteger(value.durationSamples, '음성 길이', { min: 1 });
  finiteInteger(value.overlapSamples, '음성 겹침 길이');
  if (typeof value.final !== 'boolean' || typeof value.downloadRequested !== 'boolean') {
    throw new LiveQueueValidationError('저장된 음성 조각 표시가 올바르지 않습니다.');
  }
  if (value.overlapSamples > value.durationSamples
      || (!value.final && value.overlapSamples >= value.durationSamples)
      || !Number.isSafeInteger(value.startSamples + value.durationSamples)) {
    throw new LiveQueueValidationError('저장된 음성 조각 범위가 올바르지 않습니다.');
  }
  cleanProvider(value.asrProvider);
  if (!isBlob(value.blob) || value.blob.size !== value.byteLength
      || value.byteLength !== 44 + value.durationSamples * 2
      || value.byteLength > MAX_LIVE_CHUNK_BYTES) {
    throw new LiveQueueValidationError('저장된 음성 조각 파일 크기가 올바르지 않습니다.');
  }
  if (!CHUNK_STATES.has(value.state)) throw new LiveQueueValidationError('저장된 음성 조각 상태가 올바르지 않습니다.');
  finiteInteger(value.attempts, '음성 전송 시도 횟수');
  cleanErrorKind(value.errorKind);
  finiteInteger(value.createdAt, '음성 저장 시각', { min: 1, max: MAX_TIMESTAMP });
  finiteInteger(value.updatedAt, '음성 갱신 시각', { min: 1, max: MAX_TIMESTAMP });
  return value;
}

function chunkCopy(value, { includeBlob = true } = {}) {
  const copy = {
    id: value.id,
    captureId: value.captureId,
    lectureId: value.captureId,
    owner: value.owner,
    sessionCreatedAt: value.sessionCreatedAt,
    sequence: value.sequence,
    startSamples: value.startSamples,
    durationSamples: value.durationSamples,
    overlapSamples: value.overlapSamples,
    final: value.final,
    asrProvider: value.asrProvider,
    byteLength: value.byteLength,
    state: value.state,
    attempts: value.attempts,
    errorKind: value.errorKind,
    downloadRequested: value.downloadRequested,
    createdAt: value.createdAt,
    updatedAt: value.updatedAt,
  };
  if (includeBlob) copy.blob = value.blob;
  return copy;
}

function storedValue(value, validator, label) {
  try {
    return validator(value);
  } catch (error) {
    throw new LiveQueueCorruptError(`${label}을 안전하게 읽을 수 없습니다.`, { cause: error });
  }
}

function requestPromise(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
  });
}

function transactionPromise(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error || new Error('IndexedDB transaction aborted'));
    transaction.onerror = () => {
      // `abort` is the terminal event and carries the transaction error.
    };
  });
}

function cursorValues(request, map) {
  return new Promise((resolve, reject) => {
    const values = [];
    request.onerror = () => reject(request.error || new Error('IndexedDB cursor failed'));
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve(values);
        return;
      }
      try {
        values.push(map(cursor.value));
        cursor.continue();
      } catch (error) {
        reject(error);
      }
    };
  });
}

function firstCursorValue(request) {
  return new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error || new Error('IndexedDB cursor failed'));
    request.onsuccess = () => resolve(request.result?.value || null);
  });
}

function countCursor(request, callback) {
  return new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error || new Error('IndexedDB cursor failed'));
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve();
        return;
      }
      try {
        callback(cursor.value, cursor);
        cursor.continue();
      } catch (error) {
        reject(error);
      }
    };
  });
}

function sameSessionIdentity(left, right) {
  return left.id === right.id && left.owner === right.owner && left.title === right.title
    && left.language === right.language && left.source === right.source
    && left.asrProvider === right.asrProvider && left.createdAt === right.createdAt;
}

/**
 * Ask the browser to make site storage less susceptible to automatic eviction.
 * A false `persisted` result is not an error: the queue can still be used, but
 * the caller should make that limitation visible during a long recording.
 */
export async function requestPersistentStorage(storageManager = globalThis.navigator?.storage) {
  if (!storageManager || typeof storageManager.persist !== 'function') {
    return { supported: false, persisted: false, requested: false };
  }
  try {
    const already = typeof storageManager.persisted === 'function'
      ? await storageManager.persisted() : false;
    if (already) return { supported: true, persisted: true, requested: false };
    const persisted = await storageManager.persist();
    return { supported: true, persisted: persisted === true, requested: true };
  } catch (error) {
    throw new LiveQueueUnavailableError('브라우저의 영구 임시 저장 권한을 확인하지 못했습니다.', {
      code: 'storage_persistence_failed',
      cause: error,
    });
  }
}

/** Return a conservative storage snapshot without manufacturing a quota. */
export async function estimateStorage(storageManager = globalThis.navigator?.storage) {
  if (!storageManager || typeof storageManager.estimate !== 'function') {
    return { supported: false, usage: null, quota: null, remaining: null };
  }
  try {
    const estimate = await storageManager.estimate();
    const usage = Number.isFinite(estimate?.usage) && estimate.usage >= 0
      ? Math.floor(estimate.usage) : null;
    const quota = Number.isFinite(estimate?.quota) && estimate.quota >= 0
      ? Math.floor(estimate.quota) : null;
    return {
      supported: usage !== null || quota !== null,
      usage,
      quota,
      remaining: usage !== null && quota !== null ? Math.max(0, quota - usage) : null,
    };
  } catch (error) {
    throw new LiveQueueUnavailableError('브라우저의 남은 임시 저장 공간을 확인하지 못했습니다.', {
      code: 'storage_estimate_failed',
      cause: error,
    });
  }
}

/**
 * Durable browser-side queue for live WAV chunks.
 *
 * Passwords, bearer tokens, invitation codes, and API origins are deliberately
 * outside this schema. `owner` is used only to prevent one signed-in account
 * from adopting another account's local audio; the API server remains the
 * authoritative ownership check.
 */
export class DurableLiveQueue {
  constructor({
    indexedDB = globalThis.indexedDB,
    keyRange = globalThis.IDBKeyRange,
    storageManager = globalThis.navigator?.storage,
    now = () => Date.now(),
  } = {}) {
    this.indexedDB = indexedDB;
    this.keyRange = keyRange;
    this.storageManager = storageManager;
    this.now = now;
    this.database = null;
    this.opening = null;
  }

  async open() {
    if (this.database) return this;
    if (this.opening) return this.opening;
    if (!this.indexedDB?.open || !this.keyRange?.bound) {
      throw new LiveQueueUnavailableError();
    }
    this.opening = new Promise((resolve, reject) => {
      let settled = false;
      let request;
      const deadline = setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new LiveQueueUnavailableError(
          '브라우저의 음성 저장소가 응답하지 않습니다. 잠시 후 다시 시도합니다.',
          { code: 'indexeddb_open_timeout' },
        ));
      },INDEXED_DB_OPEN_TIMEOUT_MS);
      try {
        request = this.indexedDB.open(LIVE_QUEUE_DB_NAME, LIVE_QUEUE_DB_VERSION);
      } catch (error) {
        clearTimeout(deadline);
        reject(storageError(error, '음성 대기열 저장소를 열지 못했습니다.'));
        return;
      }
      request.onupgradeneeded = () => {
        const database = request.result;
        let sessions;
        if (!database.objectStoreNames.contains(SESSION_STORE)) {
          sessions = database.createObjectStore(SESSION_STORE, { keyPath: 'id' });
        } else {
          sessions = request.transaction.objectStore(SESSION_STORE);
        }
        if (!sessions.indexNames.contains('ownerCreated')) {
          sessions.createIndex('ownerCreated', ['owner', 'createdAt', 'id'], { unique: false });
        }
        if (!sessions.indexNames.contains('ownerStateCreated')) {
          sessions.createIndex('ownerStateCreated', ['owner', 'state', 'createdAt', 'id'], { unique: false });
        }

        let chunks;
        if (!database.objectStoreNames.contains(CHUNK_STORE)) {
          chunks = database.createObjectStore(CHUNK_STORE, { keyPath: 'id' });
        } else {
          chunks = request.transaction.objectStore(CHUNK_STORE);
        }
        if (!chunks.indexNames.contains('ownerOrder')) {
          chunks.createIndex(
            'ownerOrder',
            ['owner', 'sessionCreatedAt', 'captureId', 'sequence', 'id'],
            { unique: false },
          );
        }
        if (!chunks.indexNames.contains('captureOrder')) {
          chunks.createIndex('captureOrder', ['owner', 'captureId', 'sequence', 'id'], { unique: false });
        }
        if (!chunks.indexNames.contains('ownerStateOrder')) {
          chunks.createIndex(
            'ownerStateOrder',
            ['owner', 'state', 'sessionCreatedAt', 'captureId', 'sequence', 'id'],
            { unique: false },
          );
        }
      };
      request.onblocked = () => {
        if (settled) return;
        settled = true;
        clearTimeout(deadline);
        reject(new LiveQueueUnavailableError(
          '다른 탭이 이전 음성 저장소를 사용 중입니다. 다른 탭을 닫은 뒤 다시 시도해 주세요.',
          { code: 'indexeddb_blocked' },
        ));
      };
      request.onerror = () => {
        if (settled) return;
        settled = true;
        clearTimeout(deadline);
        reject(storageError(request.error, '음성 대기열 저장소를 열지 못했습니다.'));
      };
      request.onsuccess = () => {
        if (settled) {
          request.result.close();
          return;
        }
        settled = true;
        clearTimeout(deadline);
        const database = request.result;
        this.database = database;
        database.onclose = () => {
          if (this.database === database) {
            this.database = null;
            this.opening = null;
          }
        };
        database.onversionchange = () => {
          database.close();
          if (this.database === database) this.database = null;
          this.opening = null;
        };
        resolve(this);
      };
    }).finally(() => {
      if (!this.database) this.opening = null;
    });
    return this.opening;
  }

  close() {
    this.database?.close();
    this.database = null;
    this.opening = null;
  }

  async requestPersistence() {
    return requestPersistentStorage(this.storageManager);
  }

  async estimateStorage() {
    return estimateStorage(this.storageManager);
  }

  _clock() {
    return finiteInteger(this.now(), '현재 시각', { min: 1, max: MAX_TIMESTAMP });
  }

  _ownerChunkRange(owner) {
    return this.keyRange.bound(
      [owner, 0, '', 0, ''],
      [owner, MAX_TIMESTAMP, HIGH_TEXT, Number.MAX_SAFE_INTEGER, HIGH_TEXT],
    );
  }

  _ownerSessionRange(owner) {
    return this.keyRange.bound([owner, 0, ''], [owner, MAX_TIMESTAMP, HIGH_TEXT]);
  }

  _captureRange(owner, captureId) {
    return this.keyRange.bound(
      [owner, captureId, 0, ''],
      [owner, captureId, Number.MAX_SAFE_INTEGER, HIGH_TEXT],
    );
  }

  async _transaction(storeNames, mode, operation, message, reconnectRetries = 1) {
    await this.open();
    const database = this.database;
    let transaction;
    try {
      transaction = database.transaction(storeNames, mode);
    } catch (error) {
      if (reconnectRetries > 0 && ['InvalidStateError', 'AbortError', 'UnknownError'].includes(error?.name)) {
        try { database?.close(); } catch { /* The connection is already unusable. */ }
        if (this.database === database) this.database = null;
        this.opening = null;
        return this._transaction(storeNames, mode, operation, message, reconnectRetries - 1);
      }
      throw storageError(error, message);
    }
    const completed = transactionPromise(transaction);
    let timeoutId;
    const timedOut = new Promise((_, reject) => {
      timeoutId = setTimeout(() => {
        try { transaction.abort(); } catch { /* The transaction may already be terminal. */ }
        const error = new Error('브라우저의 음성 저장소 작업이 너무 오래 걸리고 있습니다.');
        error.name = 'TimeoutError';
        reject(error);
      },INDEXED_DB_TRANSACTION_TIMEOUT_MS);
    });
    try {
      const stores = Object.fromEntries(storeNames.map(name => [name, transaction.objectStore(name)]));
      const result = await Promise.race([operation(stores, transaction),timedOut]);
      await Promise.race([completed,timedOut]);
      return result;
    } catch (error) {
      try { transaction.abort(); } catch { /* The transaction may already be terminal. */ }
      if (error?.name === 'TimeoutError') {
        // The timeout exists specifically for a browser/IDB implementation
        // which may never dispatch abort/complete. Observe a late rejection,
        // but never wait on that broken event path before returning control.
        void completed.catch(() => {});
        try { database?.close(); } catch { /* Best-effort invalidation. */ }
        if (this.database === database) this.database = null;
        this.opening = null;
        throw storageError(error, message);
      }
      await completed.catch(() => {});
      if (!(error instanceof LiveQueueError) && reconnectRetries > 0
          && ['InvalidStateError', 'TransactionInactiveError', 'AbortError', 'UnknownError'].includes(error?.name)) {
        try { database?.close(); } catch { /* The connection is already unusable. */ }
        if (this.database === database) this.database = null;
        this.opening = null;
        return this._transaction(storeNames, mode, operation, message, reconnectRetries - 1);
      }
      throw storageError(error, message);
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async createSession(value) {
    const now = this._clock();
    const input = normalizeSessionInput(value, now);
    return this._transaction([SESSION_STORE], 'readwrite', async stores => {
      const existing = await requestPromise(stores[SESSION_STORE].get(input.id));
      if (existing) {
        const session = storedValue(existing, validateSessionRecord, '저장된 녹음 세션');
        if (!sameSessionIdentity(session, input)) {
          throw new LiveQueueConflictError('같은 녹음 세션 ID로 다른 수업을 저장할 수 없습니다.');
        }
        return sessionCopy(session);
      }
      const session = newSession(input, now);
      await requestPromise(stores[SESSION_STORE].add(session));
      return sessionCopy(session);
    }, '녹음 세션을 기기에 저장하지 못했습니다.');
  }

  async getSession(ownerValue, captureIdValue) {
    const owner = cleanOwner(ownerValue);
    const captureId = cleanUuid(captureIdValue, '녹음 세션 ID');
    return this._transaction([SESSION_STORE], 'readonly', async stores => {
      const value = await requestPromise(stores[SESSION_STORE].get(captureId));
      if (!value) return null;
      const session = storedValue(value, validateSessionRecord, '저장된 녹음 세션');
      if (session.owner !== owner) throw new LiveQueueOwnershipError();
      return sessionCopy(session);
    }, '녹음 세션을 기기에서 읽지 못했습니다.');
  }

  async updateSession(ownerValue, captureIdValue, updates) {
    const owner = cleanOwner(ownerValue);
    const captureId = cleanUuid(captureIdValue, '녹음 세션 ID');
    const allowed = new Set(['state', 'lectureCreated', 'finalQueued']);
    onlyKeys(updates, allowed, '녹음 세션 변경');
    if (!Object.keys(updates).length) throw new LiveQueueValidationError('변경할 녹음 세션 상태가 없습니다.');
    if ('state' in updates) cleanSessionState(updates.state);
    if ('lectureCreated' in updates && typeof updates.lectureCreated !== 'boolean') {
      throw new LiveQueueValidationError('수업 생성 상태가 올바르지 않습니다.');
    }
    if ('finalQueued' in updates && typeof updates.finalQueued !== 'boolean') {
      throw new LiveQueueValidationError('마지막 음성 저장 상태가 올바르지 않습니다.');
    }
    const now = this._clock();
    return this._transaction([SESSION_STORE, CHUNK_STORE], 'readwrite', async stores => {
      const value = await requestPromise(stores[SESSION_STORE].get(captureId));
      if (!value) throw new LiveQueueNotFoundError('녹음 세션을 찾을 수 없습니다.');
      const session = storedValue(value, validateSessionRecord, '저장된 녹음 세션');
      if (session.owner !== owner) throw new LiveQueueOwnershipError();
      if (session.state === 'completed' && updates.state !== undefined && updates.state !== 'completed') {
        throw new LiveQueueConflictError('완료된 녹음 세션을 다시 열 수 없습니다.');
      }
      if (session.lectureCreated && updates.lectureCreated === false) {
        throw new LiveQueueConflictError('생성된 서버 수업 상태를 되돌릴 수 없습니다.');
      }
      if (session.finalQueued && updates.finalQueued === false) {
        throw new LiveQueueConflictError('저장된 마지막 음성 상태를 되돌릴 수 없습니다.');
      }
      if (updates.finalQueued === true && !session.finalQueued) {
        const values = await cursorValues(
          stores[CHUNK_STORE].index('captureOrder').openCursor(this._captureRange(owner, captureId)),
          item => storedValue(item, validateChunkRecord, '저장된 음성 조각'),
        );
        if (!values.some(item => item.final)) {
          throw new LiveQueueConflictError('마지막 음성 조각을 저장한 뒤 세션을 종료해 주세요.');
        }
      }
      if (updates.state === 'completed') {
        const remaining = await requestPromise(
          stores[CHUNK_STORE].index('captureOrder').count(this._captureRange(owner, captureId)),
        );
        if (!session.finalQueued || remaining !== 0) {
          throw new LiveQueueConflictError('모든 음성 조각의 서버 저장을 확인한 뒤 세션을 완료해 주세요.');
        }
      }
      Object.assign(session, updates, { updatedAt: now });
      validateSessionRecord(session);
      await requestPromise(stores[SESSION_STORE].put(session));
      return sessionCopy(session);
    }, '녹음 세션 상태를 기기에 저장하지 못했습니다.');
  }

  async enqueueChunk(ownerValue, captureIdValue, value) {
    const owner = cleanOwner(ownerValue);
    const captureId = cleanUuid(captureIdValue, '녹음 세션 ID');
    const input = normalizeChunkInput(value);
    await validateWavBlob(input.blob, input.durationSamples);
    const now = this._clock();
    return this._transaction([SESSION_STORE, CHUNK_STORE], 'readwrite', async stores => {
      const sessionValue = await requestPromise(stores[SESSION_STORE].get(captureId));
      if (!sessionValue) throw new LiveQueueNotFoundError('음성을 저장할 녹음 세션을 찾을 수 없습니다.');
      const session = storedValue(sessionValue, validateSessionRecord, '저장된 녹음 세션');
      if (session.owner !== owner) throw new LiveQueueOwnershipError();
      const existing = await requestPromise(stores[CHUNK_STORE].get(input.id));
      if (existing) {
        const previous = storedValue(existing, validateChunkRecord, '저장된 음성 조각');
        if (previous.owner !== owner) throw new LiveQueueOwnershipError();
        if (previous.captureId !== captureId
            || previous.startSamples !== input.startSamples
            || previous.durationSamples !== input.durationSamples
            || previous.overlapSamples !== input.overlapSamples
            || previous.final !== input.final
            || previous.byteLength !== input.blob.size
            || !(await sameBlobBytes(previous.blob, input.blob))) {
          throw new LiveQueueConflictError('같은 음성 조각 ID로 다른 내용을 저장할 수 없습니다.');
        }
        await validateWavBlob(previous.blob, previous.durationSamples);
        return chunkCopy(previous);
      }
      if (session.state === 'completed' || session.finalQueued) {
        throw new LiveQueueConflictError('이미 마지막 음성이 저장된 세션에는 음성을 더 추가할 수 없습니다.');
      }
      if (input.startSamples + input.overlapSamples !== session.capturedSamples) {
        throw new LiveQueueConflictError('음성 조각의 순서가 현재 녹음 길이와 맞지 않습니다.');
      }
      if (session.nextSequence >= Number.MAX_SAFE_INTEGER) {
        throw new LiveQueueConflictError('한 녹음 세션에 저장할 수 있는 음성 조각 수를 초과했습니다.');
      }
      const sequence = session.nextSequence;
      const chunk = {
        id: input.id,
        captureId,
        owner,
        sessionCreatedAt: session.createdAt,
        sequence,
        startSamples: input.startSamples,
        durationSamples: input.durationSamples,
        overlapSamples: input.overlapSamples,
        final: input.final,
        asrProvider: session.asrProvider,
        blob: input.blob,
        byteLength: input.blob.size,
        state: 'queued',
        attempts: 0,
        errorKind: '',
        downloadRequested: false,
        createdAt: now,
        updatedAt: now,
      };
      session.nextSequence += 1;
      session.capturedSamples = input.startSamples + input.durationSamples;
      session.updatedAt = now;
      if (input.final) {
        session.finalQueued = true;
        session.state = 'stopped';
      }
      validateChunkRecord(chunk);
      validateSessionRecord(session);
      await requestPromise(stores[CHUNK_STORE].add(chunk));
      await requestPromise(stores[SESSION_STORE].put(session));
      return chunkCopy(chunk);
    }, '음성 조각을 기기에 안전하게 저장하지 못했습니다.');
  }

  async getChunk(ownerValue, chunkIdValue) {
    const owner = cleanOwner(ownerValue);
    const chunkId = cleanUuid(chunkIdValue, '음성 조각 ID');
    const chunk = await this._transaction([CHUNK_STORE], 'readonly', async stores => {
      const value = await requestPromise(stores[CHUNK_STORE].get(chunkId));
      if (!value) return null;
      const stored = storedValue(value, validateChunkRecord, '저장된 음성 조각');
      if (stored.owner !== owner) throw new LiveQueueOwnershipError();
      return chunkCopy(stored);
    }, '음성 조각을 기기에서 읽지 못했습니다.');
    if (chunk) {
      try { await validateWavBlob(chunk.blob, chunk.durationSamples); }
      catch (error) { throw new LiveQueueCorruptError(undefined, { cause: error }); }
    }
    return chunk;
  }

  async peekNextChunk(ownerValue) {
    const owner = cleanOwner(ownerValue);
    const chunk = await this._transaction([CHUNK_STORE], 'readonly', async stores => {
      const value = await firstCursorValue(
        stores[CHUNK_STORE].index('ownerOrder').openCursor(this._ownerChunkRange(owner)),
      );
      if (!value) return null;
      return chunkCopy(storedValue(value, validateChunkRecord, '저장된 음성 조각'));
    }, '다음 음성 조각을 기기에서 읽지 못했습니다.');
    if (chunk) {
      try { await validateWavBlob(chunk.blob, chunk.durationSamples); }
      catch (error) { throw new LiveQueueCorruptError(undefined, { cause: error }); }
    }
    return chunk;
  }

  async ackChunk(ownerValue, chunkIdValue) {
    const owner = cleanOwner(ownerValue);
    const chunkId = cleanUuid(chunkIdValue, '음성 조각 ID');
    const now = this._clock();
    return this._transaction([SESSION_STORE, CHUNK_STORE], 'readwrite', async stores => {
      const value = await requestPromise(stores[CHUNK_STORE].get(chunkId));
      if (!value) return null;
      const chunk = storedValue(value, validateChunkRecord, '저장된 음성 조각');
      if (chunk.owner !== owner) throw new LiveQueueOwnershipError();
      const explicitlyDownloadedChunk = chunk.downloadRequested === true;
      if (chunk.asrProvider === 'clova' && chunk.state !== 'inflight'
          && !explicitlyDownloadedChunk) {
        throw new LiveQueueConflictError(
          'CLOVA 음성은 전송 중 상태를 저장하거나 파일을 내려받은 뒤에만 완료할 수 있습니다.',
        );
      }
      const sessionValue = await requestPromise(stores[SESSION_STORE].get(chunk.captureId));
      if (!sessionValue) throw new LiveQueueCorruptError('음성 조각의 녹음 세션을 찾을 수 없습니다.');
      const session = storedValue(sessionValue, validateSessionRecord, '저장된 녹음 세션');
      if (session.owner !== owner) throw new LiveQueueOwnershipError();
      if (chunk.final) {
        const remaining = await requestPromise(
          stores[CHUNK_STORE].index('captureOrder').count(this._captureRange(owner, chunk.captureId)),
        );
        if (remaining !== 1) {
          throw new LiveQueueConflictError('앞선 음성 조각을 모두 저장한 뒤 마지막 조각을 완료해 주세요.');
        }
      }
      await requestPromise(stores[CHUNK_STORE].delete(chunkId));
      if (chunk.final) {
        session.state = 'completed';
        session.updatedAt = now;
        // The final ACK is the durable hand-off to the server. Remove the
        // browser-side session metadata in the same transaction as its final
        // WAV so a later cleanup failure cannot leave account/title metadata
        // orphaned on a shared device.
        await requestPromise(stores[SESSION_STORE].delete(chunk.captureId));
      }
      return {
        chunk: chunkCopy(chunk, { includeBlob: false }),
        session: sessionCopy(session),
      };
    }, '서버에 저장된 음성을 기기 대기열에서 정리하지 못했습니다.');
  }

  async _changeChunk(ownerValue, chunkIdValue, change, message) {
    const owner = cleanOwner(ownerValue);
    const chunkId = cleanUuid(chunkIdValue, '음성 조각 ID');
    const now = this._clock();
    return this._transaction([CHUNK_STORE], 'readwrite', async stores => {
      const value = await requestPromise(stores[CHUNK_STORE].get(chunkId));
      if (!value) throw new LiveQueueNotFoundError('음성 조각을 찾을 수 없습니다.');
      const chunk = storedValue(value, validateChunkRecord, '저장된 음성 조각');
      if (chunk.owner !== owner) throw new LiveQueueOwnershipError();
      change(chunk);
      chunk.updatedAt = now;
      validateChunkRecord(chunk);
      await requestPromise(stores[CHUNK_STORE].put(chunk));
      return chunkCopy(chunk, { includeBlob: false });
    }, message);
  }

  async markChunkBlocked(owner, chunkId, errorKind) {
    const kind = cleanErrorKind(errorKind);
    if (!kind) throw new LiveQueueValidationError('차단된 음성의 오류 종류가 필요합니다.');
    return this._changeChunk(owner, chunkId, chunk => {
      const attemptAlreadyRecorded = chunk.state === 'inflight';
      chunk.state = 'blocked';
      chunk.errorKind = kind;
      if (!attemptAlreadyRecorded) chunk.attempts += 1;
      if (!Number.isSafeInteger(chunk.attempts)) {
        throw new LiveQueueValidationError('음성 전송 시도 횟수가 너무 큽니다.');
      }
    }, '음성 조각의 전송 대기 상태를 저장하지 못했습니다.');
  }

  /**
   * Persist the CLOVA uncertainty boundary before starting the HTTP request.
   *
   * This transition is intentionally not idempotent: finding `inflight` after
   * a crash means the provider may already have accepted the audio.  A caller
   * must explicitly resolve that state instead of silently submitting it again.
   */
  async markChunkInflight(owner, chunkId) {
    return this._changeChunk(owner, chunkId, chunk => {
      if (chunk.asrProvider !== 'clova') {
        throw new LiveQueueConflictError('CLOVA 음성 조각만 전송 중 상태로 변경할 수 있습니다.');
      }
      if (chunk.state !== 'queued') {
        throw new LiveQueueConflictError('전송 대기 중인 CLOVA 음성 조각만 전송을 시작할 수 있습니다.');
      }
      chunk.state = 'inflight';
      chunk.errorKind = '';
      chunk.attempts += 1;
      if (!Number.isSafeInteger(chunk.attempts)) {
        throw new LiveQueueValidationError('음성 전송 시도 횟수가 너무 큽니다.');
      }
    }, 'CLOVA 음성 조각의 전송 중 상태를 저장하지 못했습니다.');
  }

  async markChunkQueued(owner, chunkId) {
    return this._changeChunk(owner, chunkId, chunk => {
      chunk.state = 'queued';
      chunk.errorKind = '';
    }, '음성 조각의 재전송 상태를 저장하지 못했습니다.');
  }

  async setDownloadRequested(owner, chunkId, requested = true) {
    if (typeof requested !== 'boolean') {
      throw new LiveQueueValidationError('음성 다운로드 확인 상태가 올바르지 않습니다.');
    }
    return this._changeChunk(owner, chunkId, chunk => {
      chunk.downloadRequested = requested;
    }, '음성 조각의 다운로드 확인 상태를 저장하지 못했습니다.');
  }

  async getStats(ownerValue) {
    const owner = cleanOwner(ownerValue);
    return this._transaction([CHUNK_STORE], 'readonly', async stores => {
      const stats = { count: 0, bytes: 0, queued: 0, inflight: 0, blocked: 0 };
      await countCursor(
        stores[CHUNK_STORE].index('ownerOrder').openCursor(this._ownerChunkRange(owner)),
        value => {
          const chunk = storedValue(value, validateChunkRecord, '저장된 음성 조각');
          stats.count += 1;
          stats.bytes += chunk.byteLength;
          stats[chunk.state] += 1;
          if (!Number.isSafeInteger(stats.bytes)) {
            throw new LiveQueueCorruptError('저장된 음성 조각의 전체 크기가 너무 큽니다.');
          }
        },
      );
      return stats;
    }, '음성 대기열 크기를 확인하지 못했습니다.');
  }

  async recoverOwner(ownerValue) {
    const owner = cleanOwner(ownerValue);
    const recovered = await this._transaction([SESSION_STORE, CHUNK_STORE], 'readonly', async stores => {
      const sessionRequest = stores[SESSION_STORE].index('ownerCreated')
        .openCursor(this._ownerSessionRange(owner));
      const chunkRequest = stores[CHUNK_STORE].index('ownerOrder')
        .openCursor(this._ownerChunkRange(owner));
      const [sessions, chunks] = await Promise.all([
        cursorValues(sessionRequest, value => sessionCopy(
          storedValue(value, validateSessionRecord, '저장된 녹음 세션'),
        )),
        cursorValues(chunkRequest, value => chunkCopy(
          storedValue(value, validateChunkRecord, '저장된 음성 조각'),
          { includeBlob: false },
        )),
      ]);
      return { sessions, chunks };
    }, '계정의 음성 대기열을 복구하지 못했습니다.');

    // Keep recovery memory-bounded even after a long outage. Structural record
    // validation happens above, while the WAV header/body is validated lazily by
    // getChunk() for the single item being uploaded or downloaded.
    const sessionsById = new Map(recovered.sessions.map(session => [session.id, session]));
    const sequencesBySession = new Map();
    const finalBySession = new Set();
    for (const chunk of recovered.chunks) {
      const session = sessionsById.get(chunk.captureId);
      if (!session || session.owner !== owner || chunk.asrProvider !== session.asrProvider
          || chunk.sessionCreatedAt !== session.createdAt || chunk.sequence >= session.nextSequence) {
        throw new LiveQueueCorruptError('음성 조각과 녹음 세션의 연결 정보가 올바르지 않습니다.');
      }
      const sequences = sequencesBySession.get(session.id) || new Set();
      if (sequences.has(chunk.sequence)) {
        throw new LiveQueueCorruptError('한 녹음 세션에 같은 순서의 음성 조각이 중복되어 있습니다.');
      }
      sequences.add(chunk.sequence);
      sequencesBySession.set(session.id, sequences);
      if (chunk.final) {
        if (finalBySession.has(session.id)) {
          throw new LiveQueueCorruptError('한 녹음 세션에 마지막 음성 조각이 중복되어 있습니다.');
        }
        finalBySession.add(session.id);
      }
    }
    for (const session of recovered.sessions) {
      const hasFinal = finalBySession.has(session.id);
      if ((hasFinal && !session.finalQueued)
          || (session.finalQueued && session.state !== 'completed' && !hasFinal)) {
        throw new LiveQueueCorruptError('녹음 세션의 마지막 음성 상태가 올바르지 않습니다.');
      }
    }
    const stats = { count: 0, bytes: 0, queued: 0, inflight: 0, blocked: 0 };
    const chunkMetadata = recovered.chunks.map(chunk => {
      stats.count += 1;
      stats.bytes += chunk.byteLength;
      stats[chunk.state] += 1;
      return chunkCopy(chunk, { includeBlob: false });
    });
    const inflightChunks = chunkMetadata.filter(chunk => chunk.state === 'inflight');
    const activeIds = new Set(chunkMetadata.map(chunk => chunk.captureId));
    const sessions = recovered.sessions.filter(session => session.state !== 'completed' || activeIds.has(session.id));
    return { sessions, chunks: chunkMetadata, inflightChunks, stats };
  }

  async hasWorkForOtherOwner(ownerValue) {
    const owner = cleanOwner(ownerValue);
    const chunkFound = await this._transaction([CHUNK_STORE], 'readonly', async stores => {
      const values = await cursorValues(stores[CHUNK_STORE].openCursor(), value => {
        const chunk = storedValue(value, validateChunkRecord, '저장된 음성 조각');
        return chunk.owner !== owner;
      });
      return values.some(Boolean);
    }, '다른 계정의 음성 대기열 상태를 확인하지 못했습니다.');
    if (chunkFound) return true;
    return this._transaction([SESSION_STORE], 'readonly', async stores => {
      const values = await cursorValues(stores[SESSION_STORE].openCursor(), value => {
        const session = storedValue(value, validateSessionRecord, '저장된 녹음 세션');
        return session.owner !== owner && session.state !== 'completed';
      });
      return values.some(Boolean);
    }, '다른 계정의 녹음 세션 상태를 확인하지 못했습니다.');
  }

  async hasPendingChunks(ownerValue, captureIdValue) {
    const owner = cleanOwner(ownerValue);
    const captureId = cleanUuid(captureIdValue, '녹음 세션 ID');
    return this._transaction([CHUNK_STORE], 'readonly', async stores => {
      const count = await requestPromise(
        stores[CHUNK_STORE].index('captureOrder').count(this._captureRange(owner,captureId)),
      );
      return count > 0;
    }, '수업의 미전송 음성을 확인하지 못했습니다.');
  }

  /** Permanently remove one local session and all of its unsent audio. */
  async deleteSession(ownerValue, captureIdValue) {
    const owner = cleanOwner(ownerValue);
    const captureId = cleanUuid(captureIdValue, '녹음 세션 ID');
    return this._transaction([SESSION_STORE, CHUNK_STORE], 'readwrite', async stores => {
      const value = await requestPromise(stores[SESSION_STORE].get(captureId));
      if (!value) return { deletedChunks: 0, deletedBytes: 0 };
      const session = storedValue(value, validateSessionRecord, '저장된 녹음 세션');
      if (session.owner !== owner) throw new LiveQueueOwnershipError();
      let deletedChunks = 0;
      let deletedBytes = 0;
      await countCursor(
        stores[CHUNK_STORE].index('captureOrder').openCursor(this._captureRange(owner, captureId)),
        (chunkValue, cursor) => {
          const chunk = storedValue(chunkValue, validateChunkRecord, '저장된 음성 조각');
          deletedChunks += 1;
          deletedBytes += chunk.byteLength;
          cursor.delete();
        },
      );
      await requestPromise(stores[SESSION_STORE].delete(captureId));
      return { deletedChunks, deletedBytes };
    }, '기기의 녹음 세션을 정리하지 못했습니다.');
  }
}
