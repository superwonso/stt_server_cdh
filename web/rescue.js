import { DurableLiveQueue } from './live-queue.js';
import { buildRecoverableLocalAudioExports, LocalAudioExportError } from './local-audio-export.js';

const DAY = 24 * 60 * 60 * 1000;
const USERNAME = /^[a-z0-9](?:[a-z0-9._-]{0,30}[a-z0-9])?$/;
const TOKEN = /^[A-Za-z0-9._~-]{1,512}$/;
const LOCAL_HOSTS = new Set(['localhost','127.0.0.1','[::1]']);
const MAX_JSON_BYTES = 32768;
class RescueError extends Error {}

export function normalizeRescueOrigin(value, pageUrl) {
  if (typeof value !== 'string' || value.length > 256) throw new RescueError('서버 주소가 올바르지 않습니다.');
  const url = new URL(value.trim()), page = new URL(pageUrl);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new RescueError('서버 주소에는 경로·계정 정보·쿼리를 넣을 수 없습니다.');
  }
  const local = LOCAL_HOSTS.has(url.hostname) && LOCAL_HOSTS.has(page.hostname);
  if (local ? !['http:','https:'].includes(url.protocol)
    : url.protocol !== 'https:' || url.port || !/^[a-z0-9-]+\.trycloudflare\.com$/.test(url.hostname)) {
    throw new RescueError('현재 HTTPS 임시 서버 주소를 사용하세요.');
  }
  return url.origin;
}

function stamp(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) throw new RescueError('자동 주소의 시각이 올바르지 않습니다.');
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || new Date(parsed).toISOString().replace('.000Z','Z') !== value) throw new RescueError('자동 주소의 시각이 올바르지 않습니다.');
  return parsed;
}

export function validateRescueConfig(value, pageUrl, now = Date.now()) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== 'apiUrl,expiresAt,publishedAt,state,version'
    || value.version !== 1 || !['online','offline'].includes(value.state)) throw new RescueError('자동 서버 주소를 확인하지 못했습니다.');
  const published = stamp(value.publishedAt), expires = stamp(value.expiresAt);
  if (published > now + 300000) throw new RescueError('자동 주소의 게시 시간이 올바르지 않습니다.');
  if (value.state === 'offline') throw new RescueError('서버가 꺼져 있습니다. 원래 탭은 유지하고 서버 연결을 기다리세요.');
  if (expires - published !== DAY || expires <= now) throw new RescueError('자동 서버 주소가 만료되었습니다. 연결을 다시 확인하세요.');
  const origin = normalizeRescueOrigin(value.apiUrl,pageUrl);
  if (origin !== value.apiUrl) throw new RescueError('자동 서버 주소 형식이 올바르지 않습니다.');
  return {origin,expiresAt:expires};
}

export async function readRescueJson(response) {
  if (response.redirected) throw new RescueError('서버 주소가 바뀐 응답은 사용하지 않습니다.');
  const declared = response.headers.get('content-length');
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > MAX_JSON_BYTES)) throw new RescueError('서버 응답이 너무 큽니다.');
  if (!response.body?.getReader) throw new RescueError('서버 응답을 안전하게 읽을 수 없습니다.');
  const reader = response.body.getReader(), parts = [];
  let size = 0;
  try {
    while (true) {
      const {done,value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_JSON_BYTES) throw new RescueError('서버 응답이 너무 큽니다.');
      parts.push(value);
    }
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  const body = new Uint8Array(size); let offset = 0;
  for (const part of parts) { body.set(part,offset); offset += part.length; }
  try { return JSON.parse(new TextDecoder().decode(body)); }
  catch { throw new RescueError('서버 응답 형식을 확인하지 못했습니다.'); }
}

async function existingSnapshot(owner) {
  const queue = new DurableLiveQueue({readOnlyExisting:true});
  try { return await queue.readExportSnapshot(owner); }
  finally { queue.close(); }
}

/** Independent read-only rescue: never import the main app or start an uploader. */
export class RescueController {
  constructor({pageUrl,fetch:fetcher = (...args) => globalThis.fetch(...args),now = () => Date.now(),
    readSnapshot = existingSnapshot,buildExport = buildRecoverableLocalAudioExports,onChange = () => {},
    setTimer = (...args) => globalThis.setTimeout(...args),clearTimer = (...args) => globalThis.clearTimeout(...args)} = {}) {
    this.pageUrl = pageUrl; this.fetcher = fetcher; this.now = now;
    this.readSnapshot = readSnapshot; this.buildExport = buildExport; this.onChange = onChange;
    this.setTimer = setTimer; this.clearTimer = clearTimer;
    this.sequence = 0; this.controller = null; this.expiryTimer = null;
    this.origin = ''; this.originExpiresAt = 0; this.auth = null;
    this.view = {busy:false,connected:false,username:'',error:'',message:'서버 연결을 확인하세요.',result:null};
  }
  emit() { this.onChange({...this.view}); }
  clearIdentity() {
    this.auth = null; this.view.username = ''; this.view.result = null;
    if (this.expiryTimer !== null) this.clearTimer(this.expiryTimer);
    this.expiryTimer = null;
  }
  begin() {
    ++this.sequence; this.controller?.abort(); this.controller = new AbortController();
    this.view.busy = true; this.view.error = ''; return this.sequence;
  }
  current(sequence) { return sequence === this.sequence && !this.controller?.signal.aborted; }
  lock(message = '이 화면을 잠갔습니다. 원래 탭의 음성은 변경하지 않았습니다.') {
    ++this.sequence; this.controller?.abort(); this.controller = null; this.clearIdentity();
    this.view.busy = false; this.view.error = ''; this.view.message = message; this.emit();
  }
  trusted() { return !!this.origin && this.originExpiresAt > this.now(); }
  async request(url,{method = 'GET',body,token,signal} = {}) {
    const headers = {'Accept':'application/json'};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = `Bearer ${token}`;
    const timeout = new AbortController();
    const cancel = () => timeout.abort();
    if (signal?.aborted) cancel(); else signal?.addEventListener('abort',cancel,{once:true});
    const timer = this.setTimer(cancel,15000);
    try {
      const response = await this.fetcher(url,{method,headers,body:body === undefined ? undefined : JSON.stringify(body),
        cache:'no-store',credentials:'omit',redirect:'error',referrerPolicy:'no-referrer',signal:timeout.signal});
      if (!response.ok) {
        await response.body?.cancel().catch(() => {});
        const error = new RescueError(response.status === 401 ? '아이디·비밀번호 또는 로그인 만료를 확인하세요.'
          : response.status === 429 ? '요청이 많습니다. 잠시 기다린 뒤 직접 다시 시도하세요.' : '서버 요청을 완료하지 못했습니다. 원래 탭을 유지하고 다시 시도하세요.');
        error.status = response.status; throw error;
      }
      return await readRescueJson(response);
    } finally { this.clearTimer(timer); signal?.removeEventListener('abort',cancel); }
  }
  async connect(manual = '') {
    const sequence = this.begin(); this.clearIdentity(); this.origin = ''; this.originExpiresAt = 0;
    this.view.connected = false; this.view.message = '현재 서버 연결을 확인합니다.'; this.emit();
    try {
      let selected;
      if (manual) selected = {origin:normalizeRescueOrigin(manual,this.pageUrl),expiresAt:this.now()+DAY};
      else {
        const url = new URL('./config.json',this.pageUrl); url.searchParams.set('v',String(this.now()));
        selected = validateRescueConfig(await this.request(url.href,{signal:this.controller.signal}),this.pageUrl,this.now());
      }
      if (!this.current(sequence)) return;
      const health = await this.request(`${selected.origin}/health`,{signal:this.controller.signal});
      if (!this.current(sequence)) return;
      if (health?.status !== 'ok' || selected.expiresAt <= this.now()) throw new RescueError('현재 서버 연결을 확인하지 못했습니다.');
      this.origin = selected.origin; this.originExpiresAt = selected.expiresAt;
      this.view.connected = true; this.view.message = '서버 연결 확인 완료. 원래 탭과 같은 계정으로 로그인하세요.';
    } catch (error) { if (this.current(sequence)) this.view.error = safeMessage(error); }
    finally { if (this.current(sequence)) { this.view.busy = false; this.emit(); } }
  }
  async login(username,password) {
    if (!this.trusted()) { this.lock('서버 연결을 다시 확인하세요.'); return; }
    if (typeof username !== 'string' || !USERNAME.test(username) || typeof password !== 'string' || password.length < 1 || password.length > 128) {
      this.view.error = '아이디와 비밀번호를 확인하세요.'; this.emit(); return;
    }
    const sequence = this.begin(); this.clearIdentity(); this.emit();
    const origin = this.origin;
    try {
      const result = await this.request(`${origin}/auth/login`,{method:'POST',body:{username,password},signal:this.controller.signal});
      if (!this.current(sequence)) return;
      if (typeof result?.token !== 'string' || !TOKEN.test(result.token) || result?.user?.username !== username) throw new RescueError('로그인 계정을 확인하지 못했습니다.');
      const me = await this.request(`${origin}/auth/me`,{token:result.token,signal:this.controller.signal});
      if (!this.current(sequence)) return;
      const expiresAt = Math.min(me?.session_expires_at * 1000,this.now()+DAY,this.originExpiresAt);
      if (me?.username !== username || typeof me.session_expires_at !== 'number'
          || !Number.isFinite(expiresAt) || expiresAt <= this.now() || !this.trusted()) throw new RescueError('로그인 유효 기간을 확인하지 못했습니다.');
      this.auth = {username,token:result.token,expiresAt,origin}; this.view.username = username;
      this.expiryTimer = this.setTimer(() => this.lock('로그인 또는 서버 주소가 만료되어 이 화면을 잠갔습니다.'),expiresAt-this.now());
      await this.collect(sequence);
    } catch (error) { if (this.current(sequence)) { this.clearIdentity(); this.view.error = safeMessage(error); } }
    finally { if (this.current(sequence)) { this.view.busy = false; this.emit(); } }
  }
  async collect(sequence) {
    const auth = this.auth;
    if (!auth || auth.expiresAt <= this.now() || !this.trusted()) throw new RescueError('로그인이 만료되었습니다.');
    const snapshot = await this.readSnapshot(auth.username);
    if (!this.current(sequence) || this.auth !== auth || auth.expiresAt <= this.now()) return;
    if (!snapshot || snapshot.owner !== auth.username) throw new RescueError('음성 소유자를 확인하지 못했습니다.');
    const result = await this.buildExport({owner:auth.username,chunks:snapshot.chunks,snapshots:snapshot.snapshots,signal:this.controller.signal});
    if (!this.current(sequence) || this.auth !== auth || auth.expiresAt <= this.now()) return;
    this.view.result = result; this.view.message = '기기에 남은 구간을 읽었습니다. 원래 탭과 대기열은 변경하지 않았습니다.';
  }
  async scan() {
    if (!this.auth || this.auth.expiresAt <= this.now() || !this.trusted()) { this.lock('로그인과 서버 연결을 다시 확인하세요.'); return; }
    const sequence = this.begin(), auth = this.auth; this.view.result = null; this.emit();
    try {
      const me = await this.request(`${auth.origin}/auth/me`,{token:auth.token,signal:this.controller.signal});
      if (!this.current(sequence)) return;
      if (me?.username !== auth.username || typeof me.session_expires_at !== 'number' || !Number.isFinite(me.session_expires_at)
          || me.session_expires_at*1000 <= this.now()) throw new RescueError('로그인 계정을 다시 확인하지 못했습니다.');
      await this.collect(sequence);
    } catch (error) { if (this.current(sequence)) { this.clearIdentity(); this.view.error = safeMessage(error); } }
    finally { if (this.current(sequence)) { this.view.busy = false; this.emit(); } }
  }
}

function safeMessage(error) {
  if (error?.code === 'live_queue_not_found') return '이 브라우저 저장소에 보관된 음성이 없습니다. 다른 프로필이나 원래 탭의 RAM 음성은 여기서 확인할 수 없습니다.';
  if (error?.code === 'live_queue_corrupt') return '저장된 음성 정보가 손상되었거나 지원하지 않는 버전입니다. 음성을 변경하지 않았습니다. 원래 탭을 유지해 주세요.';
  if (error instanceof LocalAudioExportError) {
    const messages = {
      invalid_owner:'음성 계정을 확인하지 못했습니다. 원래 탭과 같은 계정인지 확인하세요.',
      owner_mismatch:'다른 계정의 음성이 섞여 있어 내보내지 않았습니다.',
      invalid_record:'일부 음성의 시간 위치나 길이가 올바르지 않아 내보내지 않았습니다. 원래 탭을 유지해 주세요.',
      invalid_wav:'일부 WAV 조각이 손상되었거나 비어 있어 내보내지 않았습니다. 원래 탭을 유지해 주세요.',
      read_failed:'음성 조각을 읽지 못했습니다. 저장된 음성을 변경하지 않았습니다. 원래 탭을 유지해 주세요.',
      overlap_conflict:'같은 시간 위치의 음성이 서로 달라 합치지 않았습니다. 원래 탭을 유지해 주세요.',
      export_limit:'한 번에 처리할 수 있는 크기나 길이를 초과했습니다. 음성을 변경하지 않았습니다.',
      aborted:'음성 파일 준비를 취소했습니다. 저장된 음성은 변경하지 않았습니다.',
    };
    return messages[error.code] || '음성 파일을 안전하게 준비하지 못했습니다. 원래 탭을 유지해 주세요.';
  }
  if (error?.name === 'AbortError') return '응답을 기다리다 중단했습니다. 원래 탭은 유지하고 직접 다시 시도하세요.';
  // Never reflect arbitrary provider/IndexedDB diagnostics, account data or paths.
  return error instanceof RescueError
    ? error.message : '기기에 남은 음성을 안전하게 읽지 못했습니다. 원래 탭을 유지하고 다시 시도하세요.';
}

export function mountRescuePage(document,window,options = {}) {
  const element = id => document.getElementById(id);
  const urls = new Set(); let rendered = null;
  const clearFiles = () => { for (const url of urls) window.URL.revokeObjectURL(url); urls.clear(); element('rescue-files').replaceChildren(); rendered = null; };
  const controller = new RescueController({...options,pageUrl:window.location.href,onChange:view => {
    element('rescue-status').textContent = view.message;
    element('rescue-error').textContent = view.error; element('rescue-error').hidden = !view.error;
    element('rescue-login').disabled = view.busy || !view.connected;
    element('rescue-connect').disabled = view.busy;
    element('rescue-login-form').hidden = !!view.username;
    element('rescue-results').hidden = !view.username;
    element('rescue-scan').disabled = view.busy;
    element('rescue-owner').textContent = view.username ? `${view.username} 계정 · 이 브라우저에 저장된 음성` : '';
    if (rendered !== view.result) {
      clearFiles(); rendered = view.result;
      let fileCount = 0;
      for (const [groupIndex,group] of (view.result?.groups || []).entries()) {
        for (const [partIndex,part] of group.parts.entries()) {
          const row = document.createElement('p'), link = document.createElement('a');
          const url = window.URL.createObjectURL(part.blob); urls.add(url);
          link.href = url; link.download = `local-audio-${String(groupIndex+1).padStart(3,'0')}-part-${String(partIndex+1).padStart(3,'0')}.wav`;
          link.onclick = event => {
            // Background tabs can delay expiry timers. Check again at the
            // user's explicit download gesture, without trusting the timer.
            if (!controller.auth || controller.auth.expiresAt <= controller.now() || !controller.trusted()) {
              event.preventDefault(); controller.lock('로그인 또는 서버 주소가 만료되었습니다. 다시 확인하세요.');
            }
          };
          link.textContent = `음성 ${groupIndex+1} · 구간 ${partIndex+1} 저장 (${(part.startSamples/16000).toFixed(1)}–${(part.endSamples/16000).toFixed(1)}초)`;
          row.append(link); element('rescue-files').append(row); fileCount++;
        }
      }
      element('rescue-summary').textContent = view.result ? fileCount
        ? `${fileCount}개 WAV 파일을 준비했습니다. 전체 수업 길이가 아니라 남은 구간입니다.` : '준비할 수 있는 WAV 파일이 없습니다.' : '';
      const gapCount = (view.result?.groups || []).reduce((n,group) => n+group.warnings.length,0);
      const unreadable = (view.result?.warnings || []).reduce((n,warning) => warning.code === 'unreadable_records' ? n+warning.count : n,0);
      const warnings = [];
      if (unreadable) warnings.push(`${unreadable}개 음성 조각·임시 저장본을 읽지 못해 파일에서 제외했습니다. 준비된 파일에는 빠진 음성이 있습니다. 원래 탭을 유지해 주세요.`);
      if (gapCount) warnings.push(`앞부분 누락 또는 빈 구간이 ${gapCount}곳 있습니다. 표시된 시작·끝 위치를 확인하고 모든 필요한 파일을 각각 저장하세요.`);
      if (view.result) warnings.push(fileCount ? '파일을 저장해도 대기열의 상태나 음성은 바뀌지 않습니다.'
        : '저장된 음성이 보이지 않아도 원래 탭에 RAM 음성이 남았을 수 있습니다. 원래 탭을 닫지 마세요.');
      element('rescue-warnings').textContent = warnings.join(' ');
    }
    if (!view.username) { element('rescue-password').value = ''; }
  }});
  element('rescue-connect').onclick = () => controller.connect();
  element('rescue-server-form').onsubmit = event => { event.preventDefault(); void controller.connect(element('rescue-origin').value); };
  element('rescue-login-form').onsubmit = event => {
    event.preventDefault(); const password = element('rescue-password').value;
    element('rescue-password').value = ''; void controller.login(element('rescue-username').value,password);
  };
  element('rescue-scan').onclick = () => controller.scan();
  element('rescue-lock').onclick = () => { controller.lock(); element('rescue-username').value = ''; };
  window.addEventListener('pagehide',() => { controller.lock(); clearFiles(); });
  // Recovery credentials/API choices must never be accepted from a URL.
  if (window.location.hash || window.location.search) window.history.replaceState(null,'',window.location.pathname);
  void controller.connect();
  return controller;
}

if (typeof document !== 'undefined' && document.getElementById('rescue-title')) mountRescuePage(document,window);
