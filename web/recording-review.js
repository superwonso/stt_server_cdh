const MAX_CLIP_BYTES = 44 + 120 * 16000 * 2;
const SAMPLE_RATE = 16000;

/** Read only a bounded, canonical WAV from an already authenticated response. */
export async function readRecordingClip(response) {
  const number = name => {
    const raw = response.headers.get(name);
    return typeof raw === 'string' && raw.trim() ? Number(raw) : Number.NaN;
  };
  const length = number('Content-Length');
  const start = number('X-Clip-Start-Seconds');
  const duration = number('X-Clip-Duration-Seconds');
  if (!Number.isSafeInteger(length) || length < 46 || length > MAX_CLIP_BYTES
      || !Number.isFinite(start) || start < 0 || start > 14400
      || !Number.isFinite(duration) || duration <= 0 || duration > 120
      || !/^audio\/wav(?:;|$)/i.test(response.headers.get('Content-Type') || '')
      || !response.body?.getReader) {
    await response.body?.cancel?.().catch(()=>{});
    throw new Error('녹음 구간의 형식과 길이를 확인하지 못했습니다.');
  }
  const reader = response.body.getReader(), parts = [];
  let total = 0, complete = false;
  try {
    while (true) {
      const {done,value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > length || total > MAX_CLIP_BYTES) throw new Error('녹음 구간의 크기가 허용 범위를 넘었습니다.');
      parts.push(value);
    }
    if (total !== length) throw new Error('녹음 구간이 끝까지 도착하지 않았습니다. 다시 재생해 주세요.');
    const blob = new Blob(parts,{type:'audio/wav'});
    const header = new DataView(await blob.slice(0,44).arrayBuffer());
    const text = (offset,count) => Array.from({length:count},(_,index)=>String.fromCharCode(header.getUint8(offset + index))).join('');
    if (text(0,4) !== 'RIFF' || text(8,4) !== 'WAVE' || text(12,4) !== 'fmt ' || text(36,4) !== 'data'
        || header.getUint32(4,true) !== total - 8 || header.getUint32(16,true) !== 16
        || header.getUint16(20,true) !== 1 || header.getUint16(22,true) !== 1
        || header.getUint32(24,true) !== SAMPLE_RATE || header.getUint32(28,true) !== 32000
        || header.getUint16(32,true) !== 2 || header.getUint16(34,true) !== 16
        || header.getUint32(40,true) !== total - 44 || (total - 44) % 2
        || Math.abs((total - 44) / 32000 - duration) > 1 / SAMPLE_RATE) {
      throw new Error('녹음 구간의 WAV 데이터가 올바르지 않습니다.');
    }
    complete = true;
    return {blob,startSeconds:start,durationSeconds:duration};
  } finally {
    if (!complete) await reader.cancel().catch(()=>{});
    reader.releaseLock();
  }
}

/** One bounded clip and one abortable fetch; never loads a whole class into RAM. */
export class RecordingClipPlayer {
  constructor({audio,fetchClip,onState = () => {},url = URL}) {
    this.audio = audio; this.fetchClip = fetchClip; this.onState = onState; this.url = url;
    this.generation = 0; this.controller = null; this.objectUrl = null; this.clip = null;
    this.audio.onended = () => this.onState({state:'ended',clip:this.clip});
  }
  _release() {
    this.audio.pause();
    this.audio.removeAttribute('src');
    this.audio.load();
    if (this.objectUrl) this.url.revokeObjectURL(this.objectUrl);
    this.objectUrl = null; this.clip = null;
  }
  reset() {
    this.generation += 1; this.controller?.abort(); this.controller = null;
    this._release(); this.onState({state:'idle',clip:null});
  }
  get positionSeconds() {
    return this.clip ? this.clip.startSeconds + Math.max(0,Math.min(this.clip.durationSeconds,Number(this.audio.currentTime) || 0)) : null;
  }
  async play(startSeconds) {
    if (!Number.isFinite(startSeconds) || startSeconds < 0 || startSeconds > 14400) throw new Error('재생할 시각이 올바르지 않습니다.');
    const generation = ++this.generation;
    this.controller?.abort(); this._release();
    const controller = new AbortController(); this.controller = controller;
    this.onState({state:'loading',clip:null});
    try {
      const clip = await this.fetchClip(Math.floor(startSeconds * SAMPLE_RATE) / SAMPLE_RATE,controller.signal);
      if (generation !== this.generation || controller.signal.aborted) return;
      this.clip = clip; this.objectUrl = this.url.createObjectURL(clip.blob);
      this.audio.src = this.objectUrl;
      this.onState({state:'ready',clip});
      try { await this.audio.play(); }
      catch {
        if (generation === this.generation) this.onState({state:'ready',clip,manualPlay:true});
      }
    } catch (error) {
      if (generation === this.generation && !controller.signal.aborted) {
        this.onState({state:'error',clip:null,error:error?.message || '녹음을 불러오지 못했습니다.'});
      }
    } finally {
      if (this.controller === controller) this.controller = null;
    }
  }
}

export function filterTranscript(segments, query) {
  const normalized = String(query || '').slice(0,120).normalize('NFKC').toLocaleLowerCase();
  return normalized ? segments.filter(segment => String(segment?.text || '').normalize('NFKC').toLocaleLowerCase().includes(normalized)) : segments;
}
