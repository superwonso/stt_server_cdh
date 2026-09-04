const LOCK_NAMESPACE = 'yeobaek-live-v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

// These fallbacks coordinate only instances in the current JavaScript realm. They
// intentionally never touch localStorage/IndexedDB and cannot coordinate tabs.
const localCaptureLocks = new Set();
const localUploaderTails = new Map();

export class LiveCoordinationValidationError extends TypeError {
  constructor(message) {
    super(message);
    this.name = 'LiveCoordinationValidationError';
    this.code = 'live_coordination_validation';
  }
}

export class LiveCoordinationError extends Error {
  constructor(message, { code = 'live_coordination_error', cause, retrySafe = false } = {}) {
    super(message);
    this.name = 'LiveCoordinationError';
    this.code = code;
    this.coordinationRetrySafe = retrySafe === true;
    if (cause !== undefined) this.cause = cause;
  }
}

function cleanOwner(value) {
  if (typeof value !== 'string' || value !== value.trim()
      || !value.length || Array.from(value).length > 32 || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new LiveCoordinationValidationError('계정 정보가 올바르지 않습니다.');
  }
  return value;
}

function cleanLectureId(value) {
  if (typeof value !== 'string' || !UUID.test(value)) {
    throw new LiveCoordinationValidationError('수업 ID가 올바르지 않습니다.');
  }
  return value.toLowerCase();
}

function cleanWork(value, label) {
  if (typeof value !== 'function') {
    throw new LiveCoordinationValidationError(`${label} 작업이 올바르지 않습니다.`);
  }
  return value;
}

function browserLockManager() {
  if (typeof navigator === 'undefined') return null;
  return navigator.locks || null;
}

function browserCrypto() {
  if (typeof globalThis === 'undefined') return null;
  return globalThis.crypto || null;
}

function toHex(bytes) {
  let value = '';
  for (const byte of bytes) value += byte.toString(16).padStart(2, '0');
  return value;
}

async function ownerFingerprint(owner) {
  const bytes = new TextEncoder().encode(owner);
  const cryptoObject = browserCrypto();
  if (cryptoObject?.subtle?.digest) {
    try {
      const digest = await cryptoObject.subtle.digest('SHA-256', bytes);
      return toHex(new Uint8Array(digest));
    } catch {
      // UTF-8 hex remains exact and collision-free if SubtleCrypto is unavailable.
    }
  }
  return `utf8-${toHex(bytes)}`;
}

function lockName(kind, ownerKey) {
  return `${LOCK_NAMESPACE}:${kind}:${ownerKey}`;
}

function frozenResult(value) {
  return Object.freeze(value);
}

async function runLocalExclusive(tails, key, work) {
  const previous = tails.get(key) || Promise.resolve();
  let releaseTurn;
  const turn = new Promise((resolve) => {
    releaseTurn = resolve;
  });
  const tail = previous.catch(() => undefined).then(() => turn);
  tails.set(key, tail);

  await previous.catch(() => undefined);
  try {
    return await work();
  } finally {
    releaseTurn();
    if (tails.get(key) === tail) tails.delete(key);
  }
}

/**
 * Coordinates live-capture ownership and queue mutation between same-origin tabs.
 * No account value, credential, server URL, or lock state is persisted.
 */
export class LiveCoordination {
  get supported() {
    return typeof browserLockManager()?.request === 'function';
  }

  /**
   * Attempts to acquire an owner-scoped capture lock without waiting.
   * An acquired handle holds the browser lock until release() is awaited/called.
   */
  async acquireLiveCapture(owner) {
    const clean = cleanOwner(owner);
    const ownerKey = await ownerFingerprint(clean);
    if (!this.supported) return this.#acquireLocalCapture(ownerKey);

    const manager = browserLockManager();
    const name = lockName('capture', ownerKey);
    let callbackEntered = false;
    let resolveAcquisition;
    let rejectAcquisition;
    let releaseGate;
    const hold = new Promise((resolve) => {
      releaseGate = resolve;
    });
    const acquisition = new Promise((resolve, reject) => {
      resolveAcquisition = resolve;
      rejectAcquisition = reject;
    });

    const request = Promise.resolve().then(() => manager.request(
      name,
      { mode: 'exclusive', ifAvailable: true },
      async (lock) => {
        callbackEntered = true;
        if (!lock) {
          resolveAcquisition(frozenResult({
            supported: true,
            acquired: false,
            reason: 'capture-active',
            release: null,
          }));
          return;
        }

        let released = false;
        const handle = {
          supported: true,
          acquired: true,
          reason: null,
          get released() {
            return released;
          },
          release: async () => {
            if (!released) {
              released = true;
              releaseGate();
            }
            await request;
          },
        };
        resolveAcquisition(Object.freeze(handle));
        await hold;
      },
    ));

    request.catch((error) => {
      if (!callbackEntered) rejectAcquisition(error);
    });

    try {
      return await acquisition;
    } catch (error) {
      return frozenResult({
        supported: true,
        acquired: false,
        reason: 'coordination-failed',
        error: new LiveCoordinationError(
          '브라우저의 탭 간 녹음 잠금을 얻지 못했습니다.',
          { code: 'capture_lock_failed', cause: error },
        ),
        release: null,
      });
    }
  }

  /** Runs one owner's uploader work serially across same-origin tabs. */
  async runUploader(owner, work) {
    const clean = cleanOwner(owner);
    const operation = cleanWork(work, '업로드');
    const ownerKey = await ownerFingerprint(clean);
    if (!this.supported) return this.#runLocalUploader(ownerKey, operation);

    const manager = browserLockManager();
    const name = lockName('uploader', ownerKey);
    let callbackEntered = false;
    let outcome;
    try {
      await manager.request(name, { mode: 'exclusive' }, async (lock) => {
        callbackEntered = true;
        if (!lock) {
          outcome = { coordinationError: new LiveCoordinationError(
            '업로드 잠금을 얻지 못했습니다.',
            { code: 'uploader_lock_missing' },
          ) };
          return;
        }
        try {
          outcome = { value: await operation() };
        } catch (error) {
          outcome = { operationError: error };
        }
      });
    } catch (error) {
      if (callbackEntered) {
        if (outcome?.operationError) throw outcome.operationError;
        throw outcome?.coordinationError || new LiveCoordinationError(
          '업로드 잠금 실행 중 브라우저 조정 기능이 중단되었습니다.',
          { code: 'uploader_lock_failed', cause: error },
        );
      }
      throw new LiveCoordinationError(
        '브라우저의 탭 간 업로드 잠금을 얻지 못했습니다. 잠시 후 다시 시도합니다.',
        { code: 'uploader_lock_failed', cause: error, retrySafe: true },
      );
    }

    if (outcome?.operationError) throw outcome.operationError;
    if (outcome?.coordinationError) throw outcome.coordinationError;
    return frozenResult({ supported: true, value: outcome?.value });
  }

  /**
   * Runs a destructive lecture operation only while the owner's capture lock is
   * immediately available. With no Web Locks support, hasActiveSession is
   * mandatory so callers check legacy/current in-tab recording state first.
   */
  async runDestructiveLectureAction(owner, lectureId, work, { hasActiveSession } = {}) {
    const clean = cleanOwner(owner);
    cleanLectureId(lectureId);
    const operation = cleanWork(work, '수업 변경');
    if (hasActiveSession !== undefined && typeof hasActiveSession !== 'function') {
      throw new LiveCoordinationValidationError('활성 녹음 확인 함수가 올바르지 않습니다.');
    }
    const ownerKey = await ownerFingerprint(clean);
    if (!this.supported) {
      return this.#runLocalDestructive(ownerKey, operation, hasActiveSession);
    }

    const manager = browserLockManager();
    const name = lockName('capture', ownerKey);
    let callbackEntered = false;
    let outcome;
    try {
      await manager.request(
        name,
        { mode: 'exclusive', ifAvailable: true },
        async (lock) => {
          callbackEntered = true;
          if (!lock) {
            outcome = { executed: false, reason: 'capture-active' };
            return;
          }
          try {
            outcome = { executed: true, value: await operation() };
          } catch (error) {
            outcome = { operationError: error };
          }
        },
      );
    } catch (error) {
      if (callbackEntered) {
        if (outcome?.operationError) throw outcome.operationError;
        throw new LiveCoordinationError(
          '수업 변경 잠금 실행 중 브라우저 조정 기능이 중단되었습니다.',
          { code: 'destructive_lock_failed', cause: error },
        );
      }
      throw new LiveCoordinationError(
        '브라우저의 탭 간 녹음 상태를 확인하지 못해 작업을 시작하지 않았습니다.',
        { code: 'destructive_lock_failed', cause: error },
      );
    }

    if (outcome?.operationError) throw outcome.operationError;
    return frozenResult({
      supported: true,
      executed: outcome?.executed === true,
      reason: outcome?.reason || null,
      value: outcome?.value,
    });
  }

  #acquireLocalCapture(ownerKey, cause) {
    if (localCaptureLocks.has(ownerKey)) {
      return frozenResult({
        supported: false,
        acquired: false,
        reason: 'capture-active',
        release: null,
      });
    }

    localCaptureLocks.add(ownerKey);
    let released = false;
    const handle = {
      supported: false,
      acquired: true,
      reason: cause ? 'web-locks-failed' : null,
      get released() {
        return released;
      },
      release: async () => {
        if (released) return;
        released = true;
        localCaptureLocks.delete(ownerKey);
      },
    };
    return Object.freeze(handle);
  }

  async #runLocalUploader(ownerKey, operation) {
    const value = await runLocalExclusive(localUploaderTails, ownerKey, operation);
    return frozenResult({ supported: false, value });
  }

  async #runLocalDestructive(ownerKey, operation, hasActiveSession) {
    if (localCaptureLocks.has(ownerKey)) {
      return frozenResult({
        supported: false,
        executed: false,
        reason: 'capture-active',
        value: undefined,
      });
    }
    if (typeof hasActiveSession !== 'function') {
      return frozenResult({
        supported: false,
        executed: false,
        reason: 'active-session-check-required',
        value: undefined,
      });
    }

    localCaptureLocks.add(ownerKey);
    try {
      if (await hasActiveSession()) {
        return frozenResult({
          supported: false,
          executed: false,
          reason: 'capture-active',
          value: undefined,
        });
      }
      const value = await operation();
      return frozenResult({
        supported: false,
        executed: true,
        reason: null,
        value,
      });
    } finally {
      localCaptureLocks.delete(ownerKey);
    }
  }
}

export const liveCoordination = new LiveCoordination();
