/** Read-only assembly of local PCM WAVs. This module never sends or deletes audio. */
const RATE = 16000;
const HEADER_BYTES = 44;
const MAX_CHUNK_BYTES = 4 * 1024 * 1024;
const COMPARE_BYTES = 64 * 1024;
const MAX_ITEMS = 50000;
const MAX_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_CAPTURE_SAMPLES = RATE * 4 * 60 * 60;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MESSAGES = {
  invalid_owner: '복구할 계정 정보를 확인할 수 없습니다.',
  owner_mismatch: '다른 계정의 음성이 포함되어 내보내지 않았습니다.',
  invalid_record: '음성 조각의 저장 위치나 길이를 확인할 수 없습니다.',
  invalid_wav: '비어 있거나 올바르지 않은 WAV 조각이 있어 내보내지 않았습니다.',
  read_failed: '기기에 남은 음성 조각을 읽지 못했습니다.',
  overlap_conflict: '같은 시각의 음성 조각 내용이 서로 달라 안전하게 합칠 수 없습니다.',
  export_limit: '한 번에 내보낼 수 있는 음성 크기나 길이를 초과했습니다.',
  aborted: '음성 내보내기를 취소했습니다.',
};

export class LocalAudioExportError extends Error {
  constructor(code) {
    const safeCode = Object.hasOwn(MESSAGES, code) ? code : 'invalid_record';
    super(MESSAGES[safeCode]);
    this.name = 'LocalAudioExportError';
    this.code = safeCode;
  }
}

function fail(code) { throw new LocalAudioExportError(code); }
function validInteger(value, min = 0, max = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= min && value <= max;
}
function checkOwner(owner) {
  if (typeof owner !== 'string' || !owner || owner !== owner.trim() || Array.from(owner).length > 32
      || /[\u0000-\u001f\u007f]/.test(owner)) fail('invalid_owner');
}
function cancelled(signal) { if (signal?.aborted) fail('aborted'); }
function ascii(view, start, length) {
  return Array.from({length}, (_, index) => String.fromCharCode(view.getUint8(start + index))).join('');
}
async function readSlice(blob, start, end) {
  try {
    const value = await blob.slice(start, end).arrayBuffer();
    if (value.byteLength !== end - start) fail('read_failed');
    return value;
  } catch (error) {
    if (error instanceof LocalAudioExportError) throw error;
    fail('read_failed');
  }
}

/** Validate only the bounded canonical header, never allocate the whole audio. */
export async function validateLocalWav(blob, durationSamples = undefined) {
  if (!blob || typeof blob.slice !== 'function' || typeof blob.arrayBuffer !== 'function'
      || !validInteger(blob.size, HEADER_BYTES + 2, MAX_CHUNK_BYTES) || (blob.size - HEADER_BYTES) % 2) fail('invalid_wav');
  const frames = (blob.size - HEADER_BYTES) / 2;
  if (durationSamples !== undefined && (!validInteger(durationSamples, 1) || durationSamples !== frames)) fail('invalid_wav');
  const view = new DataView(await readSlice(blob, 0, HEADER_BYTES));
  if (ascii(view,0,4) !== 'RIFF' || view.getUint32(4,true) !== blob.size - 8 || ascii(view,8,4) !== 'WAVE'
      || ascii(view,12,4) !== 'fmt ' || view.getUint32(16,true) !== 16 || view.getUint16(20,true) !== 1
      || view.getUint16(22,true) !== 1 || view.getUint32(24,true) !== RATE || view.getUint32(28,true) !== RATE * 2
      || view.getUint16(32,true) !== 2 || view.getUint16(34,true) !== 16 || ascii(view,36,4) !== 'data'
      || view.getUint32(40,true) !== frames * 2) fail('invalid_wav');
  return {durationSamples:frames, byteLength:blob.size};
}

function header(samples) {
  const value = new ArrayBuffer(HEADER_BYTES), view = new DataView(value);
  const text = (offset, source) => { for (let i = 0; i < source.length; i += 1) view.setUint8(offset+i,source.charCodeAt(i)); };
  text(0,'RIFF'); view.setUint32(4,36+samples*2,true); text(8,'WAVE'); text(12,'fmt ');
  view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,RATE,true); view.setUint32(28,RATE*2,true); view.setUint16(32,2,true);
  view.setUint16(34,16,true); text(36,'data'); view.setUint32(40,samples*2,true);
  return value;
}

async function samePcm(left, leftFrame, right, rightFrame, frames, signal) {
  if (left === right && leftFrame === rightFrame) return true;
  const total = frames * 2;
  for (let offset = 0; offset < total; offset += COMPARE_BYTES) {
    cancelled(signal);
    const amount = Math.min(COMPARE_BYTES,total-offset);
    const leftStart = HEADER_BYTES+leftFrame*2+offset, rightStart = HEADER_BYTES+rightFrame*2+offset;
    const [a,b] = await Promise.all([readSlice(left,leftStart,leftStart+amount),readSlice(right,rightStart,rightStart+amount)]);
    const first = new Uint8Array(a), second = new Uint8Array(b);
    for (let index = 0; index < first.length; index += 1) if (first[index] !== second[index]) return false;
  }
  return true;
}

async function verifyOverlap(part, record, end, signal) {
  const pieces = part.pieces;
  let low = 0, high = pieces.length;
  while (low < high) {
    const middle = Math.floor((low+high)/2);
    if (pieces[middle].endSamples <= record.startSamples) low = middle+1;
    else high = middle;
  }
  let position = record.startSamples;
  for (let index = low; position < end && index < pieces.length; index += 1) {
    const piece = pieces[index], until = Math.min(piece.endSamples,end);
    if (piece.startSamples > position) fail('overlap_conflict');
    if (!await samePcm(piece.blob,piece.offsetSamples+position-piece.startSamples,
      record.blob,position-record.startSamples,until-position,signal)) fail('overlap_conflict');
    position = until;
  }
  if (position !== end) fail('overlap_conflict');
}

/**
 * owner/capture/time are authoritative, not id/sequence equality. Snapshots and
 * chunks may duplicate each other, so only byte-equal absolute spans are cut.
 * A gap produces a separate WAV, and a conflict fails without partial output.
 * Four hours is a per-capture absolute-timeline cap; 2 GiB bounds the whole job.
 */
export async function buildLocalAudioExports({owner, chunks = [], snapshots = [],
  maxInputBytes = MAX_BYTES, maxOutputBytes = MAX_BYTES, maxCaptureSamples = MAX_CAPTURE_SAMPLES,
  signal = undefined} = {}) {
  checkOwner(owner); cancelled(signal);
  if (!Array.isArray(chunks) || !Array.isArray(snapshots) || chunks.length + snapshots.length > MAX_ITEMS
      || !validInteger(maxInputBytes,46,MAX_BYTES) || !validInteger(maxOutputBytes,46,MAX_BYTES)
      || !validInteger(maxCaptureSamples,1,MAX_CAPTURE_SAMPLES)) fail('export_limit');
  const records = [], groupsById = new Map();
  let inputBytes = 0;
  // Pin metadata and immutable Blob references before the first await. A live
  // uploader/recorder is free to replace its own objects while we assemble.
  for (const values of [chunks,snapshots]) {
    for (const value of values) {
      if (!value || typeof value !== 'object' || value.owner !== owner) fail('owner_mismatch');
      if (typeof value.captureId !== 'string' || !UUID.test(value.captureId)
          || !validInteger(value.sequence) || !validInteger(value.startSamples)
          || !validInteger(value.durationSamples,1) || !validInteger(value.overlapSamples)
          || value.overlapSamples > value.durationSamples
          || !validInteger(value.startSamples+value.durationSamples)) fail('invalid_record');
      if (value.startSamples+value.durationSamples > maxCaptureSamples) fail('export_limit');
      const record = {captureId:value.captureId.toLowerCase(), sequence:value.sequence,
        startSamples:value.startSamples, durationSamples:value.durationSamples,
        endSamples:value.startSamples+value.durationSamples, blob:value.blob};
      if (!record.blob || !validInteger(record.blob.size,46,MAX_CHUNK_BYTES)) fail('invalid_wav');
      inputBytes += record.blob.size;
      if (inputBytes > maxInputBytes) fail('export_limit');
      records.push(record);
    }
  }
  for (let index = 0; index < records.length; index += 1) {
    cancelled(signal);
    const record = records[index];
    await validateLocalWav(record.blob,record.durationSamples);
    const group = groupsById.get(record.captureId) || [];
    group.push(record); groupsById.set(record.captureId,group);
    if (index && index % 64 === 0) await new Promise(resolve => setTimeout(resolve,0));
  }
  const groups = [];
  let totalBytes = 0, totalSamples = 0;
  for (const [captureId,groupRecords] of [...groupsById].sort(([a],[b]) => a.localeCompare(b))) {
    groupRecords.sort((a,b) => a.startSamples-b.startSamples || b.endSamples-a.endSamples || a.sequence-b.sequence);
    const parts = [], warnings = [];
    let part = null;
    for (const record of groupRecords) {
      cancelled(signal);
      if (!part || record.startSamples > part.endSamples) {
        const gapStart = part ? part.endSamples : 0;
        if (record.startSamples > gapStart) warnings.push({code:part ? 'gap' : 'missing_prefix',
          startSamples:gapStart,endSamples:record.startSamples});
        part = {startSamples:record.startSamples,endSamples:record.startSamples,pieces:[]};
        parts.push(part);
      }
      if (record.startSamples < part.endSamples) {
        await verifyOverlap(part,record,Math.min(record.endSamples,part.endSamples),signal);
      }
      if (record.endSamples > part.endSamples) {
        part.pieces.push({blob:record.blob,startSamples:part.endSamples,endSamples:record.endSamples,
          offsetSamples:part.endSamples-record.startSamples});
        part.endSamples = record.endSamples;
      }
    }
    const output = [];
    for (const value of parts) {
      const durationSamples = value.endSamples-value.startSamples;
      totalBytes += HEADER_BYTES+durationSamples*2;
      totalSamples += durationSamples;
      if (totalBytes > maxOutputBytes) fail('export_limit');
      const slices = value.pieces.map(piece => piece.blob.slice(HEADER_BYTES+piece.offsetSamples*2,
        HEADER_BYTES+(piece.offsetSamples+piece.endSamples-piece.startSamples)*2));
      const blob = new Blob([header(durationSamples),...slices],{type:'audio/wav'});
      if (blob.size !== HEADER_BYTES+durationSamples*2) fail('read_failed');
      output.push({blob,startSamples:value.startSamples,endSamples:value.endSamples,durationSamples});
    }
    groups.push({captureId,parts:output,warnings});
  }
  cancelled(signal);
  return {groups,inputCount:records.length,totalBytes,totalSamples,warnings:[]};
}

/**
 * Explicit partial recovery for unreadable WAVs only. Ownership, metadata and
 * input limits are checked for ALL records before any one can be skipped.
 * Nothing is removed from the caller's RAM/IndexedDB queue; warnings describe
 * missing inputs without including their IDs, owners or private lesson titles.
 */
export async function buildRecoverableLocalAudioExports({owner, chunks = [], snapshots = [],
  maxInputBytes = MAX_BYTES, maxOutputBytes = MAX_BYTES, maxCaptureSamples = MAX_CAPTURE_SAMPLES,
  signal = undefined} = {}) {
  checkOwner(owner); cancelled(signal);
  if (!Array.isArray(chunks) || !Array.isArray(snapshots) || chunks.length + snapshots.length > MAX_ITEMS
      || !validInteger(maxInputBytes,46,MAX_BYTES) || !validInteger(maxOutputBytes,46,MAX_BYTES)
      || !validInteger(maxCaptureSamples,1,MAX_CAPTURE_SAMPLES)) fail('export_limit');
  const pinned = [];
  let inputBytes = 0;
  for (const values of [chunks,snapshots]) {
    for (const value of values) {
      if (!value || typeof value !== 'object' || value.owner !== owner) fail('owner_mismatch');
      if (typeof value.captureId !== 'string' || !UUID.test(value.captureId)
          || !validInteger(value.sequence) || !validInteger(value.startSamples)
          || !validInteger(value.durationSamples,1) || !validInteger(value.overlapSamples)
          || value.overlapSamples > value.durationSamples
          || !validInteger(value.startSamples+value.durationSamples)) fail('invalid_record');
      if (value.startSamples+value.durationSamples > maxCaptureSamples) fail('export_limit');
      const record = {owner,captureId:value.captureId,sequence:value.sequence,startSamples:value.startSamples,
        durationSamples:value.durationSamples,overlapSamples:value.overlapSamples,blob:value.blob};
      // Invalid/empty Blob handles can be reported below, but known bytes still
      // count toward the original input cap even when their header is corrupt.
      if (record.blob && validInteger(record.blob.size)) inputBytes += record.blob.size;
      if (!Number.isSafeInteger(inputBytes) || inputBytes > maxInputBytes) fail('export_limit');
      pinned.push(record);
    }
  }
  const readable = [];
  let unreadable = 0;
  for (let index = 0; index < pinned.length; index += 1) {
    cancelled(signal);
    const record = pinned[index];
    try {
      await validateLocalWav(record.blob,record.durationSamples);
      readable.push(record);
    } catch (error) {
      cancelled(signal);
      if (!(error instanceof LocalAudioExportError) || !['invalid_wav','read_failed'].includes(error.code)) throw error;
      unreadable += 1;
    }
    if (index && index % 64 === 0) await new Promise(resolve => setTimeout(resolve,0));
  }
  cancelled(signal);
  // Do not catch assembly/overlap errors: an ambiguous conflict is not evidence
  // that either readable waveform can safely be discarded.
  const result = await buildLocalAudioExports({owner,chunks:readable,maxInputBytes,maxOutputBytes,maxCaptureSamples,signal});
  return {...result,inputCount:pinned.length,
    warnings:unreadable ? [{code:'unreadable_records',count:unreadable}] : result.warnings};
}
