// A local safety deadline is independent of fetches, Web Locks and UI activity.
export const NO_TEXT_TIMEOUT_MS = 180000;
export class RecordingDeadline {
  constructor(now = Date.now()) { this.remaining = NO_TEXT_TIMEOUT_MS; this.since = now; this.paused = false; this.stopped = false; this.requests = new Map(); }
  budget(now) { return Math.max(0, this.remaining - (this.paused || this.stopped ? 0 : Math.max(0, now - this.since))); }
  pause(now = Date.now()) { if (this.paused || this.stopped) return; this.remaining = this.budget(now); this.since = now; this.paused = true; }
  resume(now = Date.now()) { if (!this.paused || this.stopped) return; this.since = now; this.paused = false; }
  progress(now = Date.now()) { if (this.stopped) return; this.remaining = NO_TEXT_TIMEOUT_MS; this.since = now; }
  requestStarted(id, now = Date.now()) {
    if (this.stopped || this.requests.has(id)) return;
    // A paused microphone does not pause an already submitted ASR request.
    // Keep both the original no-text deadline and the request's hard limit;
    // retries, result lookups, resume and unrelated progress never extend it.
    this.requests.set(id, Math.min(this.since + this.remaining, now + NO_TEXT_TIMEOUT_MS));
  }
  requestConfirmed(id) { this.requests.delete(id); }
  expired(now = Date.now()) {
    return !this.stopped && ((!this.paused && this.budget(now) <= 0)
      || [...this.requests.values()].some(deadline => now >= deadline));
  }
  stop() { this.stopped = true; this.requests.clear(); }
}
export function validRecordingControl(value, captureId) {
  return !!value && typeof value === 'object'
    && ['unregistered','active','paused','stopped','finished'].includes(value.state)
    && (value.state === 'unregistered' || value.capture_id === captureId)
    && Number.isSafeInteger(value.revision) && value.revision >= 0;
}
export function recordingStopMessage(reason) {
  return (reason === 'no_text_timeout' ? '3분 동안 새 전사 결과가 없어 녹음과 전사를 중단했어요.' : '녹음과 전사를 중단했어요.')
    + ' 원본 음성은 보관하며 서버 저장을 계속합니다. 파일 변환으로 보내거나 새 수업을 시작할 수 있어요.';
}
