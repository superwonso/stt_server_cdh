import assert from 'node:assert/strict';
import { test } from 'node:test';
import { LiveCoordination, LiveCoordinationValidationError } from '../web/live-coordination.js';

const OWNER = 'coordination-test';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// A deterministic, FIFO Web Locks adapter. Separate LiveCoordination instances
// share it like same-origin tabs; this is not a real-browser integration test.
class LockManager {
  active = new Set();
  queues = new Map();
  requests = [];

  request(name, options, callback) {
    assert.equal(options.mode, 'exclusive');
    this.requests.push(name);
    return new Promise((resolve, reject) => {
      if (options.ifAvailable && this.active.has(name)) {
        Promise.resolve().then(() => callback(null)).then(resolve, reject);
        return;
      }
      const queue = this.queues.get(name) || [];
      queue.push({ callback, resolve, reject });
      this.queues.set(name, queue);
      this.pump(name);
    });
  }

  pump(name) {
    if (this.active.has(name)) return;
    const queue = this.queues.get(name);
    const next = queue?.shift();
    if (!next) { this.queues.delete(name); return; }
    this.active.add(name);
    Promise.resolve().then(() => next.callback({ name, mode: 'exclusive' })).then(
      value => this.finish(name, () => next.resolve(value)),
      error => this.finish(name, () => next.reject(error)),
    );
  }

  finish(name, settle) {
    this.active.delete(name);
    settle();
    this.pump(name);
  }
}

function environment(t, manager) {
  for (const [name, value] of [['navigator', manager ? { locks: manager } : {}], ['crypto', undefined]]) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, value });
    t.after(() => {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    });
  }
}

async function turns() {
  // Drain nested promise/lock acquisition callbacks without timing assumptions.
  for (let index = 0; index < 30; index += 1) await Promise.resolve();
}

for (const supported of [true, false]) {
  const label = supported ? 'Web Locks' : 'same-realm fallback';

  test(`${label}: raw audio progresses during slow ASR but serializes its own lane`, { timeout: 2000 }, async t => {
    const manager = supported ? new LockManager() : null;
    environment(t, manager);
    const firstTab = new LiveCoordination(), secondTab = new LiveCoordination();
    const asrGate = deferred(), rawGate = deferred(), events = [];
    const asr = firstTab.runUploader(OWNER, async () => {
      events.push('asr-start'); await asrGate.promise; events.push('asr-end'); return 'text';
    });
    await turns();
    const raw = secondTab.runAudioUploader(OWNER, async () => {
      events.push('raw-start'); await rawGate.promise; events.push('raw-end'); return 'wav';
    });
    await turns();
    const nextRaw = firstTab.runAudioUploader(OWNER, () => events.push('next-raw'));
    const otherOwner = secondTab.runAudioUploader('different-owner', () => 'independent');
    assert.equal((await otherOwner).value, 'independent');
    assert.deepEqual(events, ['asr-start', 'raw-start']);
    rawGate.resolve();
    assert.deepEqual(await raw, { supported, value: 'wav' });
    await nextRaw;
    assert.deepEqual(events, ['asr-start', 'raw-start', 'raw-end', 'next-raw']);
    asrGate.resolve();
    assert.deepEqual(await asr, { supported, value: 'text' });
    if (manager) {
      assert.ok(manager.requests.some(name => name.startsWith('yeobaek-live-v1:uploader:')),
        'keep the existing ASR lock namespace for older tabs');
      assert.ok(manager.requests.some(name => name.startsWith('yeobaek-live-v1:audio-uploader:')));
      assert.ok(manager.requests.every(name => !name.includes(OWNER)), 'do not expose plain owner in lock names');
      assert.equal(manager.active.size, 0);
    }
  });

  test(`${label}: slow raw upload does not block ASR`, { timeout: 2000 }, async t => {
    environment(t, supported ? new LockManager() : null);
    const coordination = new LiveCoordination(), gate = deferred();
    let rawStarted = false;
    const raw = coordination.runAudioUploader(OWNER, async () => { rawStarted = true; await gate.promise; });
    await turns();
    assert.equal(rawStarted, true);
    assert.deepEqual(await coordination.runUploader(OWNER, () => 'recognition'), { supported, value: 'recognition' });
    gate.resolve(); await raw;
  });

  test(`${label}: transition waits for both lanes and excludes new uploads atomically`, { timeout: 2000 }, async t => {
    const manager = supported ? new LockManager() : null;
    environment(t, manager);
    const coordination = new LiveCoordination();
    const asrGate = deferred(), rawGate = deferred(), transitionGate = deferred(), events = [];
    const asr = coordination.runUploader(OWNER, () => asrGate.promise);
    const raw = coordination.runAudioUploader(OWNER, () => rawGate.promise);
    await turns();
    const transition = coordination.runAllUploaders(OWNER, async () => {
      events.push('transition'); await transitionGate.promise; return 'changed';
    });
    await turns();
    assert.deepEqual(events, []);
    asrGate.resolve(); await asr; await turns();
    assert.deepEqual(events, [], 'ASR release alone does not authorize changing origin or held state');
    rawGate.resolve(); await raw; await turns();
    assert.deepEqual(events, ['transition']);
    const nextAsr = coordination.runUploader(OWNER, () => events.push('next-asr'));
    const nextRaw = coordination.runAudioUploader(OWNER, () => events.push('next-raw'));
    await turns();
    assert.deepEqual(events, ['transition']);
    transitionGate.resolve();
    const result = await transition;
    assert.deepEqual(result, { supported, value: 'changed' });
    assert.equal(Object.isFrozen(result), true);
    await Promise.all([nextAsr, nextRaw]);
    assert.deepEqual(new Set(events), new Set(['transition', 'next-asr', 'next-raw']));
    if (manager) assert.equal(manager.active.size, 0);
  });

  test(`${label}: simultaneous transitions acquire ASR then audio without lock inversion`, { timeout: 2000 }, async t => {
    const manager = supported ? new LockManager() : null;
    environment(t, manager);
    const first = new LiveCoordination(), second = new LiveCoordination();
    const gate = deferred(), events = [];
    const one = first.runAllUploaders(OWNER, async () => {
      events.push('one-start'); await gate.promise; events.push('one-end');
    });
    await turns();
    const two = second.runAllUploaders(OWNER, () => events.push('two'));
    await turns();
    assert.deepEqual(events, ['one-start']);
    gate.resolve(); await Promise.all([one, two]);
    assert.deepEqual(events, ['one-start', 'one-end', 'two']);
    if (manager) {
      const lanes = manager.requests.map(name => name.split(':')[1]);
      assert.deepEqual(lanes, ['uploader', 'audio-uploader', 'uploader', 'audio-uploader']);
      assert.equal(manager.active.size, 0);
    }
  });

  test(`${label}: operation failures release raw and transition locks without replaying work`, { timeout: 2000 }, async t => {
    environment(t, supported ? new LockManager() : null);
    const coordination = new LiveCoordination(), error = new Error('storage or transition failed');
    let attempts = 0;
    for (const method of ['runAudioUploader', 'runAllUploaders']) {
      await assert.rejects(coordination[method](OWNER, async () => { attempts += 1; throw error; }),
        actual => actual === error);
      assert.deepEqual(await coordination.runAllUploaders(OWNER, () => 'next'), { supported, value: 'next' });
    }
    assert.equal(attempts, 2);
  });

  test(`${label}: falsy thrown values do not become successful upload receipts`, async t => {
    environment(t, supported ? new LockManager() : null);
    const coordination = new LiveCoordination();
    for (const method of ['runUploader', 'runAudioUploader', 'runAllUploaders']) {
      for (const failure of [undefined, null, false, 0, '']) {
        let rejected = false;
        try { await coordination[method](OWNER, () => { throw failure; }); }
        catch (actual) { rejected = true; assert.equal(actual, failure); }
        assert.equal(rejected, true, `${method} must preserve rejection`);
      }
    }
    assert.deepEqual(await coordination.runAllUploaders(OWNER, () => 'released'), { supported, value: 'released' });
  });

  test(`${label}: ASR-held transition takes only audio and raw never upgrades its lock`, { timeout: 2000 }, async t => {
    environment(t, supported ? new LockManager() : null);
    const coordination = new LiveCoordination(), rawGate = deferred(), events = [];
    const raw = coordination.runAudioUploader(OWNER, async () => {
      events.push('raw'); await rawGate.promise;
      // An expired lease must be reported outside this work. Acquiring ASR
      // here would invert the order and deadlock the waiting ASR transition.
      events.push('raw-lease-expired');
    });
    await turns();
    const asr = coordination.runUploader(OWNER, async () => {
      events.push('asr');
      return coordination.runAudioUploader(OWNER, () => events.push('lease-refresh'));
    });
    await turns();
    assert.deepEqual(events, ['raw', 'asr']);
    rawGate.resolve(); await Promise.all([raw, asr]);
    assert.deepEqual(events, ['raw', 'asr', 'raw-lease-expired', 'lease-refresh']);
    assert.equal((await coordination.runAllUploaders(OWNER, () => 'unlocked')).value, 'unlocked');
  });

  test(`${label}: invalid owners or work never enter either upload lane`, async t => {
    const manager = supported ? new LockManager() : null;
    environment(t, manager);
    const coordination = new LiveCoordination();
    let calls = 0;
    for (const method of ['runUploader', 'runAudioUploader', 'runAllUploaders']) {
      for (const owner of ['', ' owner', 'owner ', 'a'.repeat(33), 'owner\n', null, 7]) {
        await assert.rejects(coordination[method](owner, () => { calls += 1; }), LiveCoordinationValidationError);
      }
      for (const work of [null, undefined, {}, 4]) {
        await assert.rejects(coordination[method](OWNER, work), LiveCoordinationValidationError);
      }
    }
    assert.equal(calls, 0);
    if (manager) assert.deepEqual(manager.requests, []);
  });
}

test('Web Locks acquisition failure is retry-safe without a fallback upload', async t => {
  const failure = new Error('permission denied');
  environment(t, { request: async () => { throw failure; } });
  let calls = 0;
  const coordination = new LiveCoordination();
  for (const method of ['runAudioUploader', 'runAllUploaders']) {
    await assert.rejects(coordination[method](OWNER, () => { calls += 1; }), error =>
      error.code === 'uploader_lock_failed' && error.coordinationRetrySafe === true && error.cause === failure);
  }
  assert.equal(calls, 0);
});

test('Web Locks missing lock never executes or acknowledges raw audio', async t => {
  environment(t, { request: async (name, options, callback) => callback(null) });
  let calls = 0;
  await assert.rejects(new LiveCoordination().runAudioUploader(OWNER, () => { calls += 1; }),
    error => error.code === 'uploader_lock_missing' && error.coordinationRetrySafe === false);
  assert.equal(calls, 0);
});

test('Web Locks failure after entering work is not reported as safe to replay', async t => {
  const failure = new Error('lock manager failed after upload');
  environment(t, { request: async (name, options, callback) => {
    await callback({ name }); throw failure;
  } });
  let calls = 0;
  await assert.rejects(new LiveCoordination().runAudioUploader(OWNER, () => { calls += 1; return 'saved'; }),
    error => error.code === 'uploader_lock_failed' && error.coordinationRetrySafe === false && error.cause === failure);
  assert.equal(calls, 1);
});

test('audio lane acquisition failure releases the already-held ASR transition lock', { timeout: 2000 }, async t => {
  const manager = new LockManager(), request = manager.request.bind(manager);
  let refuseAudio = true;
  manager.request = (name, options, callback) => {
    if (refuseAudio && name.includes(':audio-uploader:')) return Promise.reject(new Error('audio gate unavailable'));
    return request(name, options, callback);
  };
  environment(t, manager);
  const coordination = new LiveCoordination();
  let calls = 0;
  await assert.rejects(coordination.runAllUploaders(OWNER, () => { calls += 1; }),
    error => error.code === 'uploader_lock_failed' && error.coordinationRetrySafe === true);
  assert.equal(calls, 0);
  assert.equal(manager.active.size, 0);
  refuseAudio = false;
  assert.deepEqual(await coordination.runAllUploaders(OWNER, () => 'recovered'), { supported: true, value: 'recovered' });
});

test('tryAllUploaders requires Web Locks and never runs the local fallback operation', async t => {
  environment(t, null);
  let calls = 0;
  const result = await new LiveCoordination().tryAllUploaders(OWNER, () => { calls += 1; });
  assert.deepEqual(result, { supported: false, acquired: false });
  assert.equal(Object.isFrozen(result), true);
  assert.equal(calls, 0);
});

test('tryAllUploaders acquires ASR then audio immediately and holds both throughout work', { timeout: 2000 }, async t => {
  const manager = new LockManager(), request = manager.request.bind(manager), optionsSeen = [];
  manager.request = (name, options, callback) => {
    optionsSeen.push({name, options});
    return request(name, options, callback);
  };
  environment(t, manager);
  const coordination = new LiveCoordination(), gate = deferred(), events = [];
  const transition = coordination.tryAllUploaders(OWNER, async () => {
    assert.equal(manager.active.size, 2);
    events.push('transition'); await gate.promise; return 'archived';
  });
  await turns();
  assert.deepEqual(optionsSeen.map(item => item.name.split(':')[1]), ['uploader', 'audio-uploader']);
  assert.ok(optionsSeen.every(item => item.options.mode === 'exclusive' && item.options.ifAvailable === true));
  const asr = coordination.runUploader(OWNER, () => events.push('asr'));
  const audio = coordination.runAudioUploader(OWNER, () => events.push('audio'));
  await turns(); assert.deepEqual(events, ['transition']);
  gate.resolve();
  const result = await transition;
  assert.deepEqual(result, { supported: true, acquired: true, value: 'archived' });
  assert.equal(Object.isFrozen(result), true);
  await Promise.all([asr, audio]);
  assert.deepEqual(new Set(events), new Set(['transition', 'asr', 'audio']));
  assert.equal(manager.active.size, 0);
});

test('tryAllUploaders returns while a busy ASR operation remains unresolved without requesting audio', { timeout: 2000 }, async t => {
  const manager = new LockManager(); environment(t, manager);
  const coordination = new LiveCoordination(), gate = deferred();
  const occupied = coordination.runUploader(OWNER, () => gate.promise);
  await turns(); const previousRequests = manager.requests.length;
  let calls = 0;
  try {
    const result = await coordination.tryAllUploaders(OWNER, () => { calls += 1; });
    assert.deepEqual(result, { supported: true, acquired: false, value: undefined });
    assert.equal(Object.isFrozen(result), true);
    assert.equal(calls, 0);
    assert.equal(manager.requests.length, previousRequests + 1);
    assert.equal(manager.active.size, 1);
    assert.ok([...manager.queues.values()].every(queue => queue.length === 0));
  } finally { gate.resolve(); await occupied; }
});

test('tryAllUploaders releases ASR immediately when audio is busy and never queues its work', { timeout: 2000 }, async t => {
  const manager = new LockManager(); environment(t, manager);
  const coordination = new LiveCoordination(), gate = deferred();
  const occupied = coordination.runAudioUploader(OWNER, () => gate.promise);
  await turns(); const previousRequests = manager.requests.length;
  let calls = 0;
  try {
    assert.deepEqual(await coordination.tryAllUploaders(OWNER, () => { calls += 1; }),
      { supported: true, acquired: false, value: undefined });
    assert.equal(calls, 0);
    assert.deepEqual(manager.requests.slice(previousRequests).map(name => name.split(':')[1]),
      ['uploader', 'audio-uploader']);
    assert.equal(manager.active.size, 1);
    assert.ok([...manager.queues.values()].every(queue => queue.length === 0));
    assert.equal((await coordination.runUploader(OWNER, () => 'ASR released')).value, 'ASR released');
  } finally { gate.resolve(); await occupied; }
  assert.equal(calls, 0, 'releasing the busy lane must not start an earlier refused operation');
});

test('simultaneous tryAllUploaders calls do not queue the loser and other owners remain independent', { timeout: 2000 }, async t => {
  const manager = new LockManager(); environment(t, manager);
  const first = new LiveCoordination(), second = new LiveCoordination(), gate = deferred(), events = [];
  const held = first.tryAllUploaders(OWNER, async () => { events.push('first'); await gate.promise; return 'first'; });
  const refused = second.tryAllUploaders(OWNER, () => events.push('second'));
  assert.deepEqual(await refused, { supported: true, acquired: false, value: undefined });
  assert.deepEqual(await second.tryAllUploaders('different-owner', () => 'independent'),
    { supported: true, acquired: true, value: 'independent' });
  assert.deepEqual(events, ['first']);
  assert.equal(manager.active.size, 2);
  gate.resolve(); await held;
  assert.deepEqual(events, ['first']);
  assert.ok(manager.requests.every(name => !name.includes(OWNER) && !name.includes('different-owner')));
  assert.equal(manager.active.size, 0);
});

test('tryAllUploaders propagates synchronous asynchronous and falsy work failures and releases both locks', async t => {
  const manager = new LockManager(); environment(t, manager);
  const coordination = new LiveCoordination();
  const failures = [new Error('synthetic storage failure'), undefined, null, false, 0, ''];
  let calls = 0;
  for (const failure of failures) {
    for (const asynchronous of [false, true]) {
      let rejected = false;
      const work = asynchronous
        ? async () => { calls += 1; await Promise.resolve(); throw failure; }
        : () => { calls += 1; throw failure; };
      try { await coordination.tryAllUploaders(OWNER, work); }
      catch (error) { rejected = true; assert.equal(error, failure); }
      assert.equal(rejected, true);
      assert.equal(manager.active.size, 0);
    }
  }
  assert.equal(calls, failures.length * 2);
  assert.deepEqual(await coordination.tryAllUploaders(OWNER, () => undefined),
    { supported: true, acquired: true, value: undefined });
});

test('tryAllUploaders rejects invalid owners and callbacks before requesting any lock', async t => {
  const manager = new LockManager(); environment(t, manager);
  const coordination = new LiveCoordination();
  let calls = 0;
  for (const owner of ['', ' owner', 'owner ', 'a'.repeat(33), 'owner\n', null, 7]) {
    await assert.rejects(coordination.tryAllUploaders(owner, () => { calls += 1; }), LiveCoordinationValidationError);
  }
  for (const work of [null, undefined, {}, 4]) {
    await assert.rejects(coordination.tryAllUploaders(OWNER, work), LiveCoordinationValidationError);
  }
  assert.equal(calls, 0);
  assert.deepEqual(manager.requests, []);
});

test('tryAllUploaders acquisition errors execute no work and release a partially acquired ASR lock', async t => {
  const manager = new LockManager(), request = manager.request.bind(manager), failure = new Error('synthetic lock refusal');
  let refusedLane = 'uploader';
  manager.request = (name, options, callback) => name.split(':')[1] === refusedLane
    ? Promise.reject(failure) : request(name, options, callback);
  environment(t, manager);
  const coordination = new LiveCoordination();
  let calls = 0;
  for (refusedLane of ['uploader', 'audio-uploader']) {
    await assert.rejects(coordination.tryAllUploaders(OWNER, () => { calls += 1; }), error =>
      error.code === 'uploader_lock_failed' && error.coordinationRetrySafe === true && error.cause === failure);
    assert.equal(manager.active.size, 0);
  }
  assert.equal(calls, 0);
  refusedLane = '';
  assert.equal((await coordination.tryAllUploaders(OWNER, () => 'recovered')).value, 'recovered');
});

test('tryAllUploaders manager failure after work is not declared safe to replay', async t => {
  const failure = new Error('synthetic failure after callback');
  environment(t, {request:async (name, options, callback) => {
    const value = await callback({name});
    if (name.includes(':audio-uploader:')) throw failure;
    return value;
  }});
  let calls = 0;
  await assert.rejects(new LiveCoordination().tryAllUploaders(OWNER, () => { calls += 1; return 'saved'; }), error =>
    error.code === 'uploader_lock_failed' && error.coordinationRetrySafe === false && error.cause === failure);
  assert.equal(calls, 1);
});
