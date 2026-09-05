export const AUTH_SESSION_STORAGE_KEY = 'yeobaek-auth-session-v1';
export const MAX_AUTH_SESSION_AGE_MS = 24 * 60 * 60 * 1000;

function validOrigin(value) {
  if (typeof value !== 'string') return false;
  try {
    const url = new URL(value);
    const local = ['localhost','127.0.0.1','[::1]'].includes(url.hostname);
    return url.origin === value && !url.username && !url.password
      && (url.protocol === 'https:' || (local && url.protocol === 'http:'));
  } catch { return false; }
}

function validIdentity(token, username) {
  return typeof token === 'string' && /^[A-Za-z0-9._~-]{1,512}$/.test(token)
    && typeof username === 'string' && username === username.trim()
    && username.length > 0 && username.length <= 128 && !/[\u0000-\u001f\u007f]/.test(username);
}

/** A tab-scoped convenience record, never proof of authentication. */
export class TabAuthSessionStore {
  constructor({getStorage = () => globalThis.sessionStorage, now = () => Date.now()} = {}) {
    this.getStorage = getStorage;
    this.now = now;
  }

  clear() {
    try { this.getStorage()?.removeItem(AUTH_SESSION_STORAGE_KEY); } catch { /* Storage can be disabled. */ }
  }

  read() {
    let raw;
    try { raw = this.getStorage()?.getItem(AUTH_SESSION_STORAGE_KEY); }
    catch { return null; }
    if (!raw) return null;
    if (typeof raw !== 'string' || raw.length > 4096) { this.clear(); return null; }
    let record;
    try { record = JSON.parse(raw); } catch { this.clear(); return null; }
    const now = this.now();
    if (!Number.isFinite(now) || !record || typeof record !== 'object' || Array.isArray(record)
        || Object.keys(record).sort().join(',') !== 'apiOrigin,expiresAt,token,username,version'
        || record.version !== 1 || !validIdentity(record.token,record.username) || !validOrigin(record.apiOrigin)
        || !Number.isSafeInteger(record.expiresAt) || record.expiresAt <= now
        || record.expiresAt > now + MAX_AUTH_SESSION_AGE_MS) {
      this.clear();
      return null;
    }
    return record;
  }

  save({token, username, apiOrigin, sessionExpiresAt, notAfter = Infinity}) {
    const now = this.now();
    if (!Number.isFinite(now) || !validIdentity(token,username) || !validOrigin(apiOrigin)
        || typeof sessionExpiresAt !== 'number' || !Number.isFinite(sessionExpiresAt)
        || !(notAfter === Infinity || Number.isSafeInteger(notAfter))) {
      this.clear();
      return null;
    }
    const expiresAt = Math.floor(Math.min(sessionExpiresAt * 1000,now + MAX_AUTH_SESSION_AGE_MS,notAfter));
    if (!Number.isSafeInteger(expiresAt) || expiresAt <= now) { this.clear(); return null; }
    const record = {version:1,token,username,apiOrigin,expiresAt};
    try { this.getStorage()?.setItem(AUTH_SESSION_STORAGE_KEY,JSON.stringify(record)); }
    catch { this.clear(); /* A failed replacement must not leave an older account's record. */ }
    return record;
  }

  clearMatching(token, apiOrigin) {
    const saved = this.read();
    if (saved?.token === token && saved.apiOrigin === apiOrigin) this.clear();
  }

  discardOtherOrigin(apiOrigin) {
    const saved = this.read();
    if (saved && saved.apiOrigin !== apiOrigin) this.clear();
  }
}
