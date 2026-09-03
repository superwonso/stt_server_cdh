import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { test } from 'node:test';
import {
  FileImportCancelledError,
  IMPORT_PART_BYTES,
  MAX_RECORDING_FILE_BYTES,
  RecordingFileUploader,
  abortableDelay,
  isTerminalImportState,
  recordingFileFingerprint,
  sha256Hex,
} from '../web/file-import.js';

const IMPORT_ID = '12345678-1234-4123-8123-123456789abc';

function recordingFile(size, name = '한국어 수업.m4a') {
  const bytes = Uint8Array.from({ length: size }, (_, index) => (index * 37 + 11) & 0xff);
  const file = new Blob([bytes], { type: 'audio/mp4' });
  Object.defineProperty(file, 'name', { value: name });
  return { file, bytes };
}

function job(overrides = {}) {
  const uploaded = overrides.uploaded_bytes ?? 0;
  const status = overrides.status ?? 'uploading';
  return {
    id: IMPORT_ID,
    lecture_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    title: '온라인 강의',
    language: 'ko',
    filename: '한국어 수업.m4a',
    status,
    total_bytes: overrides.total_bytes,
    uploaded_bytes: uploaded,
    next_offset: uploaded,
    part_bytes: IMPORT_PART_BYTES,
    processed_seconds: 0,
    duration_seconds: null,
    error: null,
    cancel_requested: false,
    raw_deleted: overrides.raw_deleted ?? ['completed', 'failed', 'cancelled'].includes(status),
    file_fingerprint: overrides.file_fingerprint,
    ...overrides,
  };
}

test('file parts are hashed, uploaded sequentially, completed, and polled without buffering the whole file', async () => {
  const { file, bytes } = recordingFile(IMPORT_PART_BYTES * 2 + 37);
  let serverOffset = 0;
  let fingerprint = '';
  let firstTimedOut = true;
  let polls = 0;
  const puts = [];
  const progress = [];
  const requests = [];
  const request = async (path, options = {}, timeout) => {
    requests.push({ path, method: options.method || 'GET', timeout });
    assert.equal(options.signal?.aborted, false);
    if (path === '/imports' && options.method === 'POST') {
      assert.equal(options.headers['X-Import-Id'], IMPORT_ID);
      const metadata = JSON.parse(options.body);
      fingerprint = metadata.file_fingerprint;
      assert.deepEqual(metadata, {
        title: '온라인 강의', language: 'ko', filename: '한국어 수업.m4a', size: file.size,
        file_fingerprint: await recordingFileFingerprint(file, webcrypto.subtle),
      });
      return job({ total_bytes: file.size, file_fingerprint: fingerprint });
    }
    if (path === `/imports/${IMPORT_ID}` && options.method === 'PUT') {
      const offset = Number(options.headers['X-Upload-Offset']);
      const body = new Uint8Array(await options.body.arrayBuffer());
      puts.push({ offset, size: body.length, hash: options.headers['X-Part-SHA256'] });
      assert.equal(options.headers['Content-Type'], 'application/octet-stream');
      assert.equal(options.body.type, 'application/octet-stream');
      assert.equal(options.headers['X-Part-SHA256'], await sha256Hex(body.buffer, webcrypto.subtle));
      assert.deepEqual(body, bytes.slice(offset, offset + body.length));
      if (offset === serverOffset) serverOffset += body.length;
      // The first request reached the server but its response was lost. A retry
      // with the same offset/hash must use the authoritative server offset.
      if (firstTimedOut) {
        firstTimedOut = false;
        const error = new TypeError('Cloudflare response lost');
        error.transient = true;
        throw error;
      }
      return job({ total_bytes: file.size, uploaded_bytes: serverOffset, file_fingerprint: fingerprint });
    }
    if (path.endsWith('/complete')) {
      assert.equal(serverOffset, file.size);
      return job({ total_bytes: file.size, uploaded_bytes: file.size, status: 'queued', file_fingerprint: fingerprint });
    }
    if (path === `/imports/${IMPORT_ID}` && !options.method) {
      polls += 1;
      return polls === 1
        ? job({ total_bytes: file.size, uploaded_bytes: file.size, status: 'processing', processed_seconds: 12, duration_seconds: 90, file_fingerprint: fingerprint })
        : job({ total_bytes: file.size, uploaded_bytes: file.size, status: 'completed', processed_seconds: 90, duration_seconds: 90, file_fingerprint: fingerprint });
    }
    throw new Error(`unexpected ${options.method || 'GET'} ${path}`);
  };
  const uploader = new RecordingFileUploader({
    request,
    onProgress: value => progress.push(value),
    sleep: async () => {},
    subtle: webcrypto.subtle,
    randomUUID: () => IMPORT_ID,
  });

  const result = await uploader.start(file, { title: '온라인 강의', language: 'ko' });
  assert.equal(result.status, 'completed');
  assert.equal(uploader.running, false);
  assert.deepEqual(puts.map(({ offset, size }) => ({ offset, size })), [
    { offset: 0, size: IMPORT_PART_BYTES },
    { offset: 0, size: IMPORT_PART_BYTES },
    { offset: IMPORT_PART_BYTES, size: IMPORT_PART_BYTES },
    { offset: IMPORT_PART_BYTES * 2, size: 37 },
  ]);
  assert.equal(requests.filter(item => item.path.endsWith('/complete')).length, 1);
  assert.ok(progress.some(item => item.phase === 'uploading' && item.completedBytes === file.size));
  assert.ok(progress.some(item => item.phase === 'processing' && item.processedSeconds === 12));
  assert.equal(progress.at(-1).phase, 'completed');
  assert.equal(progress.at(-1).percent, 100);
  assert.equal(isTerminalImportState(result), true);
  assert.equal(isTerminalImportState({ status: 'processing' }), false);
});

test('a failed transfer resumes at the server offset with the same import id and file', async () => {
  const { file } = recordingFile(IMPORT_PART_BYTES + 19, 'lesson.wav');
  let serverOffset = 0;
  let fingerprint = '';
  let fail = true;
  const putOffsets = [];
  const request = async (path, options = {}) => {
    if (path === '/imports') {
      fingerprint = JSON.parse(options.body).file_fingerprint;
      return job({ total_bytes: file.size, filename: 'lesson.wav', file_fingerprint: fingerprint });
    }
    if (path === `/imports/${IMPORT_ID}` && !options.method) {
      return job({ total_bytes: file.size, uploaded_bytes: serverOffset, filename: 'lesson.wav', file_fingerprint: fingerprint });
    }
    if (options.method === 'PUT') {
      const offset = Number(options.headers['X-Upload-Offset']);
      putOffsets.push(offset);
      if (fail && offset === IMPORT_PART_BYTES) {
        const error = new Error('bad request'); error.status = 400; throw error;
      }
      serverOffset = offset + options.body.size;
      return job({ total_bytes: file.size, uploaded_bytes: serverOffset, filename: 'lesson.wav', file_fingerprint: fingerprint });
    }
    if (path.endsWith('/complete')) {
      return job({ total_bytes: file.size, uploaded_bytes: file.size, filename: 'lesson.wav', status: 'completed', file_fingerprint: fingerprint });
    }
    throw new Error('unexpected request');
  };
  const uploader = new RecordingFileUploader({
    request,
    sleep: async () => {},
    subtle: webcrypto.subtle,
    randomUUID: () => IMPORT_ID,
  });
  await assert.rejects(uploader.start(file, { title: '수업' }), error => {
    assert.equal(error.status, 400);
    assert.equal(error.importId, IMPORT_ID);
    return true;
  });
  assert.equal(serverOffset, IMPORT_PART_BYTES);
  fail = false;
  const result = await uploader.resume(file);
  assert.equal(result.status, 'completed');
  assert.deepEqual(putOffsets, [0, IMPORT_PART_BYTES, IMPORT_PART_BYTES]);

  const other = recordingFile(file.size, 'different.wav').file;
  await assert.rejects(uploader.resume(other), /같은 녹음 파일/);
});

test('cancel aborts polling immediately and asks the server to cancel its background job', async () => {
  const { file } = recordingFile(29);
  let cancelCalls = 0;
  let fingerprint = '';
  const request = async (path, options = {}) => {
    if (path === '/imports') {
      fingerprint = JSON.parse(options.body).file_fingerprint;
      return job({ total_bytes: file.size, file_fingerprint: fingerprint });
    }
    if (options.method === 'PUT') return job({ total_bytes: file.size, uploaded_bytes: file.size, file_fingerprint: fingerprint });
    if (path.endsWith('/complete')) return job({ total_bytes: file.size, uploaded_bytes: file.size, status: 'processing', file_fingerprint: fingerprint });
    if (path.endsWith('/cancel')) {
      cancelCalls += 1;
      return job({ total_bytes: file.size, uploaded_bytes: file.size, status: 'processing', cancel_requested: true, file_fingerprint: fingerprint });
    }
    throw new Error(`unexpected ${path}`);
  };
  const uploader = new RecordingFileUploader({
    request,
    pollIntervalMs: 60000,
    subtle: webcrypto.subtle,
    randomUUID: () => IMPORT_ID,
  });
  const running = uploader.start(file, { title: '취소할 파일' });
  while (uploader.state?.status !== 'processing') await new Promise(resolve => setImmediate(resolve));
  const cancelState = await uploader.cancel();
  assert.equal(cancelState.cancel_requested, true);
  await assert.rejects(running, FileImportCancelledError);
  assert.equal(cancelCalls, 1);
  assert.equal(uploader.running, false);
});

test('detach stops this tab watcher without cancelling the server job', async () => {
  const { file } = recordingFile(31);
  let fingerprint = '';
  let cancelCalls = 0;
  const request = async (path, options = {}) => {
    if (path === '/imports') {
      fingerprint = JSON.parse(options.body).file_fingerprint;
      return job({ total_bytes: file.size, file_fingerprint: fingerprint });
    }
    if (options.method === 'PUT') return job({ total_bytes: file.size, uploaded_bytes: file.size, file_fingerprint: fingerprint });
    if (path.endsWith('/complete')) return job({ total_bytes: file.size, uploaded_bytes: file.size, status: 'processing', file_fingerprint: fingerprint });
    if (path.endsWith('/cancel')) { cancelCalls += 1; }
    throw new Error(`unexpected ${path}`);
  };
  const uploader = new RecordingFileUploader({
    request,
    pollIntervalMs: 60000,
    subtle: webcrypto.subtle,
    randomUUID: () => IMPORT_ID,
  });
  const running = uploader.start(file, { title: '다른 화면에서 계속할 파일' });
  while (uploader.state?.status !== 'processing') await new Promise(resolve => setImmediate(resolve));
  uploader.detach();
  await assert.rejects(running, FileImportCancelledError);
  assert.equal(cancelCalls, 0);
  assert.equal(uploader.file, null);
});

test('an uploading job can be recovered after reload only with matching bounded file content', async () => {
  const { file } = recordingFile(IMPORT_PART_BYTES + 41, 'reload.m4a');
  const fingerprint = await recordingFileFingerprint(file, webcrypto.subtle);
  const initial = job({
    total_bytes: file.size,
    uploaded_bytes: IMPORT_PART_BYTES,
    filename: 'reload.m4a',
    file_fingerprint: fingerprint,
  });
  const putOffsets = [];
  const request = async (path, options = {}) => {
    if (path === `/imports/${IMPORT_ID}` && !options.method) return initial;
    if (options.method === 'PUT') {
      putOffsets.push(Number(options.headers['X-Upload-Offset']));
      return job({
        total_bytes: file.size,
        uploaded_bytes: file.size,
        filename: 'reload.m4a',
        file_fingerprint: fingerprint,
      });
    }
    if (path.endsWith('/complete')) return job({
      total_bytes: file.size,
      uploaded_bytes: file.size,
      filename: 'reload.m4a',
      file_fingerprint: fingerprint,
      status: 'completed',
    });
    throw new Error(`unexpected ${path}`);
  };
  const recovered = new RecordingFileUploader({ request, subtle: webcrypto.subtle });
  const adopted = recovered.recover(initial);
  assert.equal(adopted, initial);
  assert.equal(recovered.running, false);
  assert.equal(recovered.file, null);
  const result = await recovered.resume(file);
  assert.equal(result.status, 'completed');
  assert.deepEqual(putOffsets, [IMPORT_PART_BYTES]);

  const changedBytes = new Uint8Array(await file.arrayBuffer());
  // This byte was outside the old first/middle/last 64 KiB sampling windows.
  changedBytes[100_000] ^= 0xff;
  const changed = new Blob([changedBytes], { type: 'audio/mp4' });
  Object.defineProperty(changed, 'name', { value: 'reload.m4a' });
  const mismatch = new RecordingFileUploader({ request, subtle: webcrypto.subtle });
  await assert.rejects(mismatch.recover(initial, changed), /내용이 다릅니다/);
  assert.deepEqual(putOffsets, [IMPORT_PART_BYTES], 'mismatched content is rejected before any new part');
});

test('queued and processing jobs recover after reload without retaining or reselecting the local file', async () => {
  const fingerprint = 'ab'.repeat(32);
  const total = 123456;
  const queued = job({
    total_bytes: total,
    uploaded_bytes: total,
    file_fingerprint: fingerprint,
    status: 'queued',
  });
  let polls = 0;
  const request = async (path, options = {}) => {
    assert.equal(path, `/imports/${IMPORT_ID}`);
    assert.equal(options.method, undefined);
    polls += 1;
    return job({
      total_bytes: total,
      uploaded_bytes: total,
      file_fingerprint: fingerprint,
      status: polls < 2 ? 'processing' : 'completed',
      duration_seconds: 100,
      processed_seconds: polls < 2 ? 50 : 100,
    });
  };
  const uploader = new RecordingFileUploader({ request, sleep: async () => {} });
  const result = await uploader.recover(queued);
  assert.equal(result.status, 'completed');
  assert.equal(uploader.file, null);
  assert.equal(polls, 2);
});

test('the recovery fingerprint covers every byte while reading one bounded transfer part at a time', async () => {
  const known = recordingFile(1000).file;
  assert.equal(
    await recordingFileFingerprint(known, webcrypto.subtle),
    '494adb2178312f5b5f56dbcdb6e5ebb491b6ef4f8f2bd025aadb9ec9729f98e1',
    'the browser manifest must stay byte-identical to the Python server algorithm',
  );
  const size = 20 * 1024 * 1024;
  const reads = [];
  const sparseFile = {
    name: 'long-class.m4a',
    size,
    slice(start, end) {
      reads.push({ start, end });
      return new Blob([new Uint8Array(end - start).fill(start / 65536 & 0xff)]);
    },
  };
  const fingerprint = await recordingFileFingerprint(sparseFile, webcrypto.subtle);
  assert.match(fingerprint, /^[0-9a-f]{64}$/);
  assert.equal(reads.length, Math.ceil(size / IMPORT_PART_BYTES));
  assert.deepEqual(reads[0], { start: 0, end: IMPORT_PART_BYTES });
  assert.deepEqual(reads.at(-1), {
    start: Math.floor(size / IMPORT_PART_BYTES) * IMPORT_PART_BYTES,
    end: size,
  });
  assert.ok(reads.every(({ start, end }) => end - start <= IMPORT_PART_BYTES));
  assert.equal(reads.reduce((total, { start, end }) => total + end - start, 0), size);
});

test('normal polling delays remove their abort listeners instead of accumulating for a long class', async () => {
  const listeners = new Set();
  const signal = {
    aborted: false,
    addEventListener(_name, listener) { listeners.add(listener); },
    removeEventListener(_name, listener) { listeners.delete(listener); },
  };
  for (let index = 0; index < 25; index += 1) {
    await abortableDelay(0, signal);
    assert.equal(listeners.size, 0);
  }
});

test('cancelling a full-file fingerprint stops before another bounded part is read', async () => {
  const size = IMPORT_PART_BYTES * 4;
  let reads = 0;
  const file = {
    name: 'cancel-during-check.wav',
    size,
    slice(start, end) {
      reads += 1;
      return new Blob([new Uint8Array(end - start)]);
    },
  };
  let uploader;
  uploader = new RecordingFileUploader({
    request: async () => { throw new Error('network must not be reached'); },
    subtle: webcrypto.subtle,
    randomUUID: () => IMPORT_ID,
    onProgress: progress => {
      if (progress.phase === 'fingerprinting' && progress.completedBytes === IMPORT_PART_BYTES) uploader.detach();
    },
  });
  await assert.rejects(uploader.start(file, {title:'취소 시험'}), FileImportCancelledError);
  assert.equal(reads, 1);
});

test('unsafe sizes, names, ids, and contradictory server offsets are rejected', async () => {
  const oversized = { size: MAX_RECORDING_FILE_BYTES + 1, name: 'huge.m4a', slice() {} };
  const never = async () => { throw new Error('must not request'); };
  const invalidFileUploader = new RecordingFileUploader({ request: never, randomUUID: () => IMPORT_ID });
  await assert.rejects(invalidFileUploader.start(oversized, { title: '큰 파일' }), /1 GiB/);

  const { file } = recordingFile(5);
  const invalidIdUploader = new RecordingFileUploader({ request: never, randomUUID: () => '../../escape' });
  await assert.rejects(invalidIdUploader.start(file, { title: '수업' }), /ID/);

  const badStateUploader = new RecordingFileUploader({
    request: async (_path, options) => {
      const fingerprint = JSON.parse(options.body).file_fingerprint;
      return job({ total_bytes: file.size, next_offset: 1, file_fingerprint: fingerprint });
    },
    randomUUID: () => IMPORT_ID,
  });
  await assert.rejects(badStateUploader.start(file, { title: '수업' }), /분할 전송 규칙/);

  const fingerprint = 'cd'.repeat(32);
  const cancelled = job({
    total_bytes: file.size,
    uploaded_bytes: file.size,
    file_fingerprint: fingerprint,
    status: 'cancelled',
    lecture_id: null,
  });
  const cancelledUploader = new RecordingFileUploader({ request: async () => cancelled });
  await assert.rejects(cancelledUploader.recover(cancelled), FileImportCancelledError);
});
