export const IMPORT_PART_BYTES = 480 * 1024;
export const MAX_RECORDING_FILE_BYTES = 1024 * 1024 * 1024;
export const MAX_IMPORT_RETRIES = 8;

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30000;
const PART_TIMEOUT_MS = 60000;
const STATUS_TIMEOUT_MS = 15000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const STATUSES = new Set(['uploading', 'queued', 'processing', 'completed', 'failed', 'cancelled']);
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const RETRYABLE = new Set([408, 425, 429]);

export class FileImportCancelledError extends Error {
  constructor() {
    super('녹음 파일 가져오기를 취소했어요.');
    this.name = 'FileImportCancelledError';
  }
}

function abortError() {
  return new FileImportCancelledError();
}

export function abortableDelay(milliseconds, signal) {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      signal?.removeEventListener?.('abort', onAbort);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener?.('abort', onAbort);
      resolve();
    }, milliseconds);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function retryable(error) {
  const status = Number(error?.status);
  return error?.transient === true
    || error?.name === 'TimeoutError'
    || error instanceof TypeError
    || RETRYABLE.has(status)
    || (status >= 500 && status <= 599 && status !== 507)
    || (status === 409 && Number.isFinite(error?.retryAfterMs));
}

function validateFile(file) {
  if (!file || typeof file.slice !== 'function' || !Number.isSafeInteger(file.size) || file.size <= 0) {
    throw new Error('내용이 있는 녹음 파일을 선택해 주세요.');
  }
  if (file.size > MAX_RECORDING_FILE_BYTES) {
    throw new Error('녹음 파일은 1 GiB 이하여야 합니다. 더 긴 파일은 나눠서 올려 주세요.');
  }
  if (typeof file.name !== 'string' || !file.name.trim()) {
    throw new Error('파일 이름을 확인할 수 없습니다.');
  }
}

function cleanFilename(name) {
  // Paths and control characters have no meaning in this protocol. The server
  // independently validates and never uses this value as a storage path.
  const cleaned = name.replace(/[\\/\u0000-\u001f\u007f]/g, '_').trim();
  if (!cleaned) throw new Error('파일 이름을 확인할 수 없습니다.');
  return Array.from(cleaned).slice(0, 255).join('');
}

function cleanTitle(title) {
  const cleaned = String(title || '').trim();
  const length = Array.from(cleaned).length;
  if (!length || length > 120) throw new Error('수업 이름은 1~120자로 입력해 주세요.');
  return cleaned;
}

function cleanLanguage(language) {
  if (language === null || language === undefined || language === '') return null;
  if (language !== 'ko' && language !== 'en') throw new Error('말하는 언어를 한국어, 영어 또는 자동 감지로 선택해 주세요.');
  return language;
}

function validOffset(value, total) {
  return Number.isSafeInteger(value) && value >= 0 && value <= total;
}

function stateError(state) {
  const message = typeof state?.error === 'string' && state.error.trim()
    ? state.error.trim()
    : state?.status === 'cancelled'
      ? '녹음 파일 가져오기가 서버에서 취소됐어요.'
      : '서버가 녹음 파일을 변환하지 못했습니다.';
  const error = state?.status === 'cancelled' ? new FileImportCancelledError() : new Error(message);
  error.importState = state;
  return error;
}

/** Convert a part to the lowercase SHA-256 representation expected by the API. */
export async function sha256Hex(buffer, subtle = globalThis.crypto?.subtle) {
  if (!subtle?.digest) throw new Error('이 브라우저에서는 안전한 파일 전송을 위한 SHA-256을 사용할 수 없습니다.');
  const digest = await subtle.digest('SHA-256', buffer);
  return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
}

/**
 * A bounded-memory identity that cryptographically commits to every file byte.
 * Each 480 KiB part is hashed independently, then the ordered binary digests
 * and exact transfer geometry are hashed once more. This avoids materializing
 * a recording of up to 1 GiB in browser memory while still detecting a changed
 * byte anywhere when an interrupted upload is resumed.
 */
async function recordingFileIdentity(
  file,
  subtle = globalThis.crypto?.subtle,
  onProgress = () => {},
  checkCancelled = () => {},
) {
  validateFile(file);
  if (!subtle?.digest) throw new Error('이 브라우저에서는 안전한 파일 전송을 위한 SHA-256을 사용할 수 없습니다.');
  const partCount = Math.ceil(file.size / IMPORT_PART_BYTES);
  const binaryDigests = [];
  const partHashes = [];
  for (let offset = 0; offset < file.size; offset += IMPORT_PART_BYTES) {
    checkCancelled();
    const end = Math.min(file.size, offset + IMPORT_PART_BYTES);
    const part = await file.slice(offset, end).arrayBuffer();
    const digest = new Uint8Array(await subtle.digest('SHA-256', part));
    checkCancelled();
    binaryDigests.push(digest);
    partHashes.push(Array.from(digest, value => value.toString(16).padStart(2, '0')).join(''));
    onProgress(end, file.size);
  }
  const framing = new TextEncoder().encode(
    `stt-import-fingerprint-v2\0${file.size}\0${IMPORT_PART_BYTES}\0${partCount}\0`,
  );
  const manifest = await new Blob([framing, ...binaryDigests]).arrayBuffer();
  const fingerprint = await sha256Hex(manifest, subtle);
  checkCancelled();
  return { fingerprint, partHashes };
}

export async function recordingFileFingerprint(file, subtle = globalThis.crypto?.subtle) {
  return (await recordingFileIdentity(file, subtle)).fingerprint;
}

/**
 * Resumable, bounded-memory uploader for the server-side media importer.
 *
 * `request(path, options, timeoutMs)` must use the app's authenticated request
 * helper and must pass `options.signal` to fetch. No password, bearer token, or
 * file contents are persisted by this class. At most one 480 KiB part is read
 * into JS memory at a time. The server's `uploaded_bytes` is authoritative, so
 * a request that completed behind a Cloudflare timeout resumes without appending
 * the same bytes twice.
 */
export class RecordingFileUploader {
  constructor({
    request,
    onProgress = () => {},
    onState = () => {},
    pollIntervalMs = 1500,
    sleep = abortableDelay,
    subtle = globalThis.crypto?.subtle,
    randomUUID = () => globalThis.crypto.randomUUID(),
  } = {}) {
    if (typeof request !== 'function') throw new TypeError('인증된 request 함수가 필요합니다.');
    this.request = request;
    this.onProgress = onProgress;
    this.onState = onState;
    this.pollIntervalMs = pollIntervalMs;
    this.sleep = sleep;
    this.subtle = subtle;
    this.randomUUID = randomUUID;
    this.running = false;
    this.importId = '';
    this.file = null;
    this.metadata = null;
    this.state = null;
    this._controller = null;
    this._cancelRequested = false;
    this._verifySelectedFile = false;
    this._partHashes = null;
  }

  async start(file, { title, language = null } = {}) {
    if (this.running) throw new Error('이미 녹음 파일을 가져오고 있습니다.');
    validateFile(file);
    const importId = this.randomUUID();
    if (!UUID.test(importId)) throw new Error('안전한 파일 가져오기 ID를 만들지 못했습니다.');
    this.importId = importId;
    this.file = file;
    this.state = null;
    this.metadata = {
      title: cleanTitle(title),
      language: cleanLanguage(language),
      filename: cleanFilename(file.name),
      size: file.size,
      file_fingerprint: '',
    };
    this._verifySelectedFile = false;
    return this._run(true);
  }

  /** Retry after an interrupted upload while this tab still holds the File. */
  async resume(file = this.file) {
    if (this.running) throw new Error('이미 녹음 파일을 가져오고 있습니다.');
    if (!this.importId || !this.metadata) throw new Error('다시 시작할 파일 가져오기가 없습니다.');
    validateFile(file);
    if (file.size !== this.metadata.size || cleanFilename(file.name) !== this.metadata.filename) {
      throw new Error('처음 선택한 것과 같은 녹음 파일을 선택해 주세요.');
    }
    this.file = file;
    this._verifySelectedFile = true;
    return this._run(false);
  }

  /**
   * Adopt an authenticated GET /imports item after a reload. Uploading jobs
   * require the user to reselect the file; queued/processing jobs only poll.
   */
  recover(state, file = null) {
    if (this.running) throw new Error('이미 녹음 파일을 가져오고 있습니다.');
    if (!state || !UUID.test(state.id || '')) throw new Error('복구할 파일 가져오기 ID가 올바르지 않습니다.');
    if (!Number.isSafeInteger(state.total_bytes) || state.total_bytes < 1
        || state.total_bytes > MAX_RECORDING_FILE_BYTES) {
      throw new Error('복구할 녹음 파일의 크기가 올바르지 않습니다.');
    }
    const filename = cleanFilename(state.filename);
    if (filename !== state.filename || !SHA256.test(state.file_fingerprint || '')) {
      throw new Error('복구할 녹음 파일의 이름 또는 지문이 올바르지 않습니다.');
    }
    this.importId = state.id;
    this.metadata = {
      title: cleanTitle(state.title),
      language: cleanLanguage(state.language),
      filename,
      size: state.total_bytes,
      file_fingerprint: state.file_fingerprint,
    };
    this.file = file;
    this.state = null;
    this._acceptState(state);
    if (state.status === 'uploading') {
      if (!file) {
        this._verifySelectedFile = false;
        return state;
      }
      validateFile(file);
      if (file.size !== state.total_bytes || cleanFilename(file.name) !== filename) {
        throw new Error('업로드를 계속하려면 처음 선택한 것과 같은 이름과 크기의 파일을 선택해 주세요.');
      }
      this._verifySelectedFile = true;
    } else {
      this._verifySelectedFile = false;
    }
    return this._run(false);
  }

  async _run(initialize) {
    this.running = true;
    this._cancelRequested = false;
    this._controller = new AbortController();
    try {
      let state;
      if (initialize) {
        const identity = await recordingFileIdentity(
          this.file,
          this.subtle,
          (completed, total) => this._reportProgress('fingerprinting', completed, total),
          () => this._throwIfCancelled(),
        );
        this.metadata.file_fingerprint = identity.fingerprint;
        this._partHashes = identity.partHashes;
        this._throwIfCancelled();
        state = await this._retry(() => this.request('/imports', {
          method: 'POST',
          body: JSON.stringify(this.metadata),
          headers: { 'X-Import-Id': this.importId },
          signal: this._controller.signal,
        }, STATUS_TIMEOUT_MS));
      } else {
        state = await this._retry(() => this.request(`/imports/${this.importId}`, {
          signal: this._controller.signal,
        }, STATUS_TIMEOUT_MS));
      }
      this._acceptState(state);
      if (this._verifySelectedFile && this.file) {
        const identity = await recordingFileIdentity(
          this.file,
          this.subtle,
          (completed, total) => this._reportProgress('fingerprinting', completed, total),
          () => this._throwIfCancelled(),
        );
        this._throwIfCancelled();
        if (identity.fingerprint !== this.metadata.file_fingerprint) {
          throw new Error('처음 선택한 파일과 내용이 다릅니다. 같은 녹음 파일을 다시 선택해 주세요.');
        }
        this._partHashes = identity.partHashes;
        this._verifySelectedFile = false;
      }
      if (state.status === 'failed' || state.status === 'cancelled') throw stateError(state);
      if (state.status === 'completed') return state;

      if (state.status === 'uploading') {
        state = await this._uploadParts(state.uploaded_bytes);
        this._acceptState(state);
        state = await this._retry(() => this.request(`/imports/${this.importId}/complete`, {
          method: 'POST',
          signal: this._controller.signal,
        }, STATUS_TIMEOUT_MS));
        this._acceptState(state);
      }
      return await this.waitForCompletion();
    } catch (error) {
      if (this._cancelRequested || error?.name === 'AbortError' || error instanceof FileImportCancelledError) {
        throw new FileImportCancelledError();
      }
      if (error && typeof error === 'object') error.importId = this.importId;
      throw error;
    } finally {
      this.running = false;
      this._controller = null;
      if (this._cancelRequested || TERMINAL.has(this.state?.status)) {
        this.file = null;
        this._partHashes = null;
      }
    }
  }

  async _uploadParts(initialOffset) {
    let offset = initialOffset;
    if (!validOffset(offset, this.file.size)) throw new Error('서버가 올바르지 않은 파일 위치를 반환했습니다.');
    this._reportProgress('uploading', offset, this.file.size);
    let state = this.state;
    while (offset < this.file.size) {
      this._throwIfCancelled();
      const end = Math.min(this.file.size, offset + IMPORT_PART_BYTES);
      const part = this.file.slice(offset, end, 'application/octet-stream');
      const partBytes = await part.arrayBuffer();
      this._throwIfCancelled();
      const partIndex = offset / IMPORT_PART_BYTES;
      const hash = Number.isInteger(partIndex) && this._partHashes?.[partIndex]
        ? this._partHashes[partIndex]
        : await sha256Hex(partBytes, this.subtle);
      this._throwIfCancelled();
      state = await this._retry(() => this.request(`/imports/${this.importId}`, {
        method: 'PUT',
        body: part,
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Upload-Offset': String(offset),
          'X-Part-SHA256': hash,
        },
        signal: this._controller.signal,
      }, PART_TIMEOUT_MS));
      this._acceptState(state);
      const next = state.uploaded_bytes;
      if (!validOffset(next, this.file.size) || next !== end) {
        throw new Error('서버가 파일 전송 위치를 올바르게 갱신하지 않았습니다.');
      }
      // A timed-out request may have committed this part before its retry. The
      // idempotent response then returns exactly this part's end offset.
      offset = next;
      this._reportProgress('uploading', offset, this.file.size);
    }
    return state;
  }

  async waitForCompletion() {
    while (true) {
      this._throwIfCancelled();
      const state = this.state;
      if (state?.status === 'completed') return state;
      if (state?.status === 'failed' || state?.status === 'cancelled') throw stateError(state);
      this._reportProcessing(state);
      await this.sleep(this.pollIntervalMs, this._controller.signal);
      const next = await this._retry(() => this.request(`/imports/${this.importId}`, {
        signal: this._controller.signal,
      }, STATUS_TIMEOUT_MS));
      this._acceptState(next);
    }
  }

  /** Abort local work immediately and ask the server to delete/cancel its job. */
  async cancel() {
    if (!this.importId) return null;
    this._cancelRequested = true;
    this._controller?.abort();
    try {
      const state = await this.request(`/imports/${this.importId}/cancel`, {
        method: 'POST',
      }, STATUS_TIMEOUT_MS);
      this._acceptState(state);
      return state;
    } catch (error) {
      // The local task is still cancelled. Surface a network failure so the UI
      // can explain that the server may continue processing until checked again.
      if (error && typeof error === 'object') error.cancelRequestFailed = true;
      throw error;
    }
  }

  /** Stop only this tab's upload/poll watcher; the server job is not cancelled. */
  detach() {
    this._cancelRequested = true;
    this._controller?.abort();
    this.file = null;
    this._partHashes = null;
  }

  async _retry(operation) {
    let attempt = 0;
    while (true) {
      this._throwIfCancelled();
      try {
        return await operation();
      } catch (error) {
        if (this._cancelRequested || error?.name === 'AbortError') throw abortError();
        if (!retryable(error) || attempt >= MAX_IMPORT_RETRIES) throw error;
        const exponential = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** attempt));
        const delay = Math.max(exponential, Math.min(Number(error.retryAfterMs) || 0, 120000));
        attempt += 1;
        this._safeProgress({
          phase: 'retrying',
          state: this.state,
          retry_attempt: attempt,
          retry_delay_ms: delay,
        });
        await this.sleep(delay, this._controller.signal);
      }
    }
  }

  _throwIfCancelled() {
    if (this._cancelRequested || this._controller?.signal.aborted) throw abortError();
  }

  _acceptState(state) {
    if (!state || state.id !== this.importId || !UUID.test(state.id)) {
      throw new Error('서버가 다른 파일 가져오기 ID를 반환했습니다.');
    }
    if (!validOffset(state.uploaded_bytes, this.metadata.size)) {
      throw new Error('서버가 올바르지 않은 업로드 진행 상태를 반환했습니다.');
    }
    if (state.total_bytes !== this.metadata.size
        || state.next_offset !== state.uploaded_bytes
        || state.part_bytes !== IMPORT_PART_BYTES) {
      throw new Error('서버의 파일 크기 또는 분할 전송 규칙이 현재 파일과 다릅니다.');
    }
    if (!STATUSES.has(state.status)) throw new Error('서버의 파일 변환 상태를 확인할 수 없습니다.');
    if (typeof state.raw_deleted !== 'boolean') throw new Error('서버의 임시 원본 삭제 상태를 확인할 수 없습니다.');
    const discardedLecture = state.status === 'failed' || state.status === 'cancelled';
    if ((discardedLecture ? state.lecture_id !== null : !UUID.test(state.lecture_id || ''))
        || state.file_fingerprint !== this.metadata.file_fingerprint) {
      throw new Error('서버가 다른 수업 또는 녹음 파일 정보를 반환했습니다.');
    }
    this.state = state;
    this._safeState(state);
    if (state.status === 'uploading') this._reportProgress('uploading', state.uploaded_bytes, this.metadata.size);
    else this._reportProcessing(state);
  }

  _reportProgress(phase, completed, total) {
    const percent = total ? Math.min(100, Math.max(0, completed / total * 100)) : 0;
    this._safeProgress({ phase, completedBytes: completed, totalBytes: total, percent, state: this.state });
  }

  _reportProcessing(state) {
    const duration = Number(state?.duration_seconds);
    const processed = Number(state?.processed_seconds);
    const hasDuration = Number.isFinite(duration) && duration > 0 && Number.isFinite(processed) && processed >= 0;
    this._safeProgress({
      phase: state?.status || 'queued',
      completedBytes: state?.uploaded_bytes || 0,
      totalBytes: this.metadata.size,
      processedSeconds: hasDuration ? Math.min(processed, duration) : null,
      durationSeconds: hasDuration ? duration : null,
      percent: state?.status === 'completed' ? 100 : hasDuration ? Math.min(100, processed / duration * 100) : null,
      state,
    });
  }

  _safeProgress(value) {
    try { this.onProgress(value); } catch { /* UI callbacks cannot corrupt the transfer. */ }
  }

  _safeState(value) {
    try { this.onState(value); } catch { /* UI callbacks cannot corrupt the transfer. */ }
  }
}

export function isTerminalImportState(state) {
  return TERMINAL.has(state?.status);
}
