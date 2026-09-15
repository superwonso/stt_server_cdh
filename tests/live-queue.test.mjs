import assert from 'node:assert/strict';
import { test } from 'node:test';
import { encodeWav } from '../web/audio.js';
import {
  DurableLiveQueue, LiveQueueConflictError, LiveQueueOwnershipError, LiveQueueValidationError, LiveQueueCorruptError,
  LIVE_QUEUE_DB_VERSION, heldRecoveryManifest,
} from '../web/live-queue.js';

const OWNER = 'test-owner';
const CAPTURE_ID = '10000000-0000-4000-8000-000000000000';
const CHUNK_ID = '20000000-0000-4000-8000-000000000000';

function asynchronousRequest(result, error = null) {
  const request = {result,error};
  queueMicrotask(() => error ? request.onerror?.() : request.onsuccess?.());
  return request;
}

// Exercise the real record validation and ACK transaction body using a tiny
// transaction adapter. This does not substitute for a browser IndexedDB test.
function storedQueue({final = false, count = 1, failDelete = false} = {}) {
  const now = 1700000000000;
  const blob = encodeWav(new Float32Array(800));
  const session = {id:CAPTURE_ID,owner:OWNER,title:'테스트 수업',language:'ko',source:'microphone',asrProvider:'clova',
    createdAt:now,updatedAt:now,state:final ? 'stopped' : 'recording',lectureCreated:true,finalQueued:final,
    nextSequence:1,capturedSamples:800};
  const chunk = {id:CHUNK_ID,captureId:CAPTURE_ID,owner:OWNER,sessionCreatedAt:now,sequence:0,startSamples:0,
    durationSamples:800,overlapSamples:0,final,asrProvider:'clova',blob,byteLength:blob.size,state:'blocked',
    attempts:1,errorKind:'response_lost',downloadRequested:false,createdAt:now,updatedAt:now};
  const queue = new DurableLiveQueue({now:() => now + 1000,keyRange:{bound:(lower,upper) => ({lower,upper})}});
  const data = {sessions:new Map([[session.id,session]]),chunks:new Map([[chunk.id,chunk]]),pcmSnapshots:new Map()};
  const writes = [];
  queue._transaction = async (names,mode,operation) => {
    assert.equal(mode,'readwrite');
    const staged = Object.fromEntries(names.map(name => [name,new Map(data[name])]));
    const stores = Object.fromEntries(names.map(name => [name,{
      get:key => asynchronousRequest(staged[name].has(key) ? {...staged[name].get(key)} : undefined),
      delete(key) {
        writes.push({type:'delete',store:name,key});
        if (failDelete) return asynchronousRequest(undefined,new Error('storage unavailable'));
        staged[name].delete(key);
        return asynchronousRequest(undefined);
      },
      put() { throw new Error('acknowledgement must never requeue the audio'); },
      index(indexName) {
        assert.equal(indexName,'captureOrder');
        return {count:() => asynchronousRequest(count)};
      },
    }]));
    const result = await operation(stores);
    for (const name of names) data[name] = staged[name];
    return result;
  };
  return {queue,data,writes};
}

test('a blocked CLOVA row requires explicit server confirmation and is deleted without becoming queued', async () => {
  const {queue,data,writes} = storedQueue();
  await assert.rejects(queue.ackChunk(OWNER,CHUNK_ID),LiveQueueConflictError);
  assert.equal(data.chunks.get(CHUNK_ID).state,'blocked');
  assert.equal(writes.length,0);
  const acknowledged = await queue.ackChunk(OWNER,CHUNK_ID,{serverConfirmed:true});
  assert.equal(acknowledged.chunk.id,CHUNK_ID);
  assert.equal(data.chunks.size,0);
  assert.equal(data.sessions.size,1);
  assert.deepEqual(writes,[{type:'delete',store:'chunks',key:CHUNK_ID}]);
});

test('server confirmation does not bypass queue ownership or final-chunk ordering', async () => {
  const ordinary = storedQueue();
  await assert.rejects(ordinary.queue.ackChunk('other-owner',CHUNK_ID,{serverConfirmed:true}),LiveQueueOwnershipError);
  assert.equal(ordinary.data.chunks.size,1);
  const final = storedQueue({final:true,count:2});
  await assert.rejects(final.queue.ackChunk(OWNER,CHUNK_ID,{serverConfirmed:true}),LiveQueueConflictError);
  assert.equal(final.data.chunks.size,1);
  assert.equal(final.data.sessions.size,1);
  assert.equal(final.writes.length,0);
});

test('a confirmed last chunk removes its session atomically while a storage failure preserves both', async () => {
  const success = storedQueue({final:true});
  await success.queue.ackChunk(OWNER,CHUNK_ID,{serverConfirmed:true});
  assert.equal(success.data.chunks.size,0);
  assert.equal(success.data.sessions.size,0);
  const failure = storedQueue({final:true,failDelete:true});
  await assert.rejects(failure.queue.ackChunk(OWNER,CHUNK_ID,{serverConfirmed:true}),/storage unavailable/);
  assert.equal(failure.data.chunks.size,1);
  assert.equal(failure.data.sessions.size,1);
  assert.equal(failure.data.chunks.get(CHUNK_ID).state,'blocked');
});

test('server confirmation is boolean and cannot be activated by a truthy string', async () => {
  const {queue,data,writes} = storedQueue();
  await assert.rejects(queue.ackChunk(OWNER,CHUNK_ID,{serverConfirmed:'true'}),LiveQueueValidationError);
  assert.equal(data.chunks.size,1);
  assert.equal(writes.length,0);
});

test('existing-only rescue opens without a version and never upgrades existing v1 or v2',async () => {
  for (const version of [1,2]) {
    let closed = false;
    const database = {version,objectStoreNames:{contains:name => name !== 'pcmSnapshots' || version === 2},
      close(){closed = true;},createObjectStore(){throw new Error('rescue must not create stores');}};
    const queue = new DurableLiveQueue({readOnlyExisting:true,keyRange:{bound(){}},indexedDB:{open(...args){
      assert.equal(args.length,1,'must not request a version upgrade');
      const request = {result:database}; queueMicrotask(() => request.onsuccess()); return request;
    }}});
    await queue.open(); assert.equal(queue.database,database);
    queue.close(); assert.equal(closed,true);
  }
});

test('existing-only rescue aborts absent database creation and rejects unknown schema',async () => {
  let aborted = false;
  const absent = new DurableLiveQueue({readOnlyExisting:true,keyRange:{bound(){}},indexedDB:{open(...args){
    assert.equal(args.length,1);
    const request = {result:{createObjectStore(){throw new Error('must never create');}},
      transaction:{abort(){aborted = true;}}};
    queueMicrotask(() => request.onupgradeneeded({oldVersion:0})); return request;
  }}});
  await assert.rejects(absent.open(),error => error.code === 'live_queue_not_found');
  assert.equal(aborted,true); assert.equal(absent.database,null);
  for (const [version,missing] of [[3,null],[2,'pcmSnapshots'],[1,'chunks']]) {
    let closed = false;
    const unknown = new DurableLiveQueue({readOnlyExisting:true,keyRange:{bound(){}},indexedDB:{open(){
      const request = {result:{version,objectStoreNames:{contains:name => name !== missing},close(){closed = true;}}};
      queueMicrotask(() => request.onsuccess()); return request;
    }}});
    await assert.rejects(unknown.open(),error => error.code === 'live_queue_corrupt');
    assert.equal(closed,true); assert.equal(unknown.database,null);
  }
});

test('existing-only reader rejects all write transactions and persistence changes before opening',async () => {
  let opens = 0, persists = 0;
  const queue = new DurableLiveQueue({readOnlyExisting:true,indexedDB:{open(){opens += 1;}},
    storageManager:{persist(){persists += 1;}}});
  await assert.rejects(queue._transaction(['chunks'],'readwrite',()=>{}),error => error.code === 'live_queue_read_only');
  await assert.rejects(queue.requestPersistence(),error => error.code === 'live_queue_read_only');
  assert.equal(opens,0); assert.equal(persists,0);
});

function memoryQueue() {
  const data = { sessions: new Map(), chunks: new Map(), pcmSnapshots: new Map() };
  const queue = new DurableLiveQueue({ now: () => 1700000000000,
    keyRange: { bound: (lower, upper) => ({ lower, upper }) } });
  const failures = new Set();
  let tail = Promise.resolve();
  const keyCompare = (left, right) => {
    if (!Array.isArray(left)) return left < right ? -1 : left > right ? 1 : 0;
    for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
      const value = keyCompare(left[i], right[i]);
      if (value) return value;
    }
    return left.length - right.length;
  };
  const cursor = (rows, erase) => {
    const request = {};
    let i = 0;
    const next = () => queueMicrotask(() => {
      const row = rows[i++];
      request.result = row ? { value: structuredClone(row), continue: next, delete: () => erase(row) } : null;
      request.onsuccess?.();
    });
    next();
    return request;
  };
  queue._transaction = (names, mode, operation) => {
    const run = async () => {
      const staged = Object.fromEntries(names.map(name => [name, new Map(
        [...data[name]].map(([key, value]) => [key, structuredClone(value)]),
      )]));
      const stores = Object.fromEntries(names.map(name => {
        const key = value => name === 'pcmSnapshots' ? value.captureId : value.id;
        const write = (value, add) => {
          if (failures.has(name)) return asynchronousRequest(null, new Error('fake write failure'));
          if (add && staged[name].has(key(value))) return asynchronousRequest(null, new Error('duplicate key'));
          staged[name].set(key(value), structuredClone(value));
          return asynchronousRequest(key(value));
        };
        const indexKey = (value, index) => ({
          owner: value.owner,
          ownerCreated: [value.owner, value.createdAt, value.id],
          ownerOrder: [value.owner, value.sessionCreatedAt, value.captureId, value.sequence, value.id],
          captureOrder: [value.owner, value.captureId, value.sequence, value.id],
        })[index];
        const values = (index, range) => [...staged[name].values()].filter(value => !range
          || (keyCompare(indexKey(value, index), range.lower) >= 0 && keyCompare(indexKey(value, index), range.upper) <= 0))
          .sort((a, b) => index ? keyCompare(indexKey(a, index), indexKey(b, index)) : 0);
        return [name, {
          get: id => asynchronousRequest(staged[name].get(id)),
          add: value => write(value, true), put: value => write(value, false),
          delete: id => {
            if (failures.has(name)) return asynchronousRequest(null, new Error('fake write failure'));
            staged[name].delete(id);
            return asynchronousRequest(undefined);
          },
          index: index => ({
            count: range => asynchronousRequest(values(index, range).length),
            openCursor: range => cursor(values(index, range), row => staged[name].delete(key(row))),
          }),
          openCursor: () => cursor(values(), row => staged[name].delete(key(row))),
        }];
      }));
      const result = await operation(stores);
      if (mode === 'readwrite') for (const name of names) data[name] = staged[name];
      return result;
    };
    const result = tail.then(run);
    tail = result.catch(() => {});
    return result;
  };
  return { queue, data, failures };
}

async function journalQueue() {
  const fixture = memoryQueue();
  await fixture.queue.createSession({ id: CAPTURE_ID, owner: OWNER, title: '가상 수업',
    source: 'microphone', asrProvider: 'qwen', language: 'en' });
  return fixture;
}

test('continuation parent survives persistent session recovery without changing old audio',async () => {
  const {queue,data} = await journalQueue();
  const old = structuredClone(data.sessions.get(CAPTURE_ID));
  const child = '30000000-0000-4000-8000-000000000000';
  const input = {id:child,owner:OWNER,title:'이어서',language:'en',source:'microphone',asrProvider:'qwen',
    createdAt:1700000000001,continuationOf:CAPTURE_ID};
  const created = await queue.createSession(input);
  assert.equal(created.continuationOf,CAPTURE_ID);
  const reopened = new DurableLiveQueue({keyRange:{bound:(lower,upper) => ({lower,upper})}});
  reopened._transaction = queue._transaction;
  const recovered = await reopened.recoverOwner(OWNER);
  assert.equal(recovered.sessions.find(row=>row.id===child).continuationOf,CAPTURE_ID);
  assert.deepEqual(data.sessions.get(CAPTURE_ID),old);
  assert.equal(Object.hasOwn(data.sessions.get(CAPTURE_ID),'continuationOf'),false);
  assert.equal((await queue.readExportSnapshot(OWNER)).sessions.find(row=>row.id===child).continuationOf,CAPTURE_ID);
  assert.deepEqual(await queue.createSession(input),created);
  await assert.rejects(queue.createSession({...input,continuationOf:null}),LiveQueueConflictError);
  await assert.rejects(queue.createSession({...input,continuationOf:CHUNK_ID}),LiveQueueConflictError);
  await assert.rejects(queue.getSession('other-owner',child),LiveQueueOwnershipError);
});

test('legacy sessions remain v2-compatible and invalid continuation references cannot be saved',async () => {
  const {queue,data} = memoryQueue();
  const input = {id:CAPTURE_ID,owner:OWNER,title:'기존 수업',source:'microphone',asrProvider:'qwen',language:'ko',createdAt:1700000000000};
  const legacy = await queue.createSession({...input,continuationOf:null});
  assert.equal(Object.hasOwn(legacy,'continuationOf'),false);
  assert.equal(Object.keys(data.sessions.get(CAPTURE_ID)).length,13);
  assert.equal(LIVE_QUEUE_DB_VERSION,2);
  for (const continuationOf of [CAPTURE_ID,'invalid',{},[],false,1]) {
    await assert.rejects(queue.createSession({...input,continuationOf}),LiveQueueValidationError);
  }
  data.sessions.get(CAPTURE_ID).continuationOf = 'invalid';
  await assert.rejects(queue.recoverOwner(OWNER),LiveQueueCorruptError);
});

test('export snapshot reads owner sessions chunks and PCM once without mutations or Blob reads',async () => {
  const {queue,data} = await journalQueue();
  await queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput({duration:128000}));
  await queue.saveSnapshot(OWNER,CAPTURE_ID,snapshotInput({sequence:1,start:80000,duration:64000,overlap:48000}));
  const other = 'another-owner', otherId = '30000000-0000-4000-8000-000000000000';
  await queue.createSession({id:otherId,owner:other,title:'other private lesson',source:'microphone',asrProvider:'qwen',language:'ko'});
  await queue.saveSnapshot(other,otherId,snapshotInput());
  const before = structuredClone(data), actualTransaction = queue._transaction;
  const calls = [];
  queue._transaction = (names,mode,operation,...rest) => {
    calls.push({names,mode}); return actualTransaction(names,mode,operation,...rest);
  };
  const original = Blob.prototype.arrayBuffer;
  let result;
  try {
    Blob.prototype.arrayBuffer = () => { throw new Error('no Blob read inside readonly transaction'); };
    result = await queue.readExportSnapshot(OWNER);
  } finally { Blob.prototype.arrayBuffer = original; }
  assert.deepEqual(calls,[{names:['sessions','chunks','pcmSnapshots'],mode:'readonly'}]);
  assert.equal(result.owner,OWNER); assert.equal(result.sessions.length,1);
  assert.equal(result.chunks.length,1); assert.equal(result.snapshots.length,1);
  for (const row of [...result.sessions,...result.chunks,...result.snapshots]) assert.equal(row.owner,OWNER);
  assert.equal(result.chunks[0].blob.size,128000*2+44);
  assert.equal(result.snapshots[0].blob.size,64000*2+44);
  assert.deepEqual(data,before);
  // Subsequent ACK/deletion cannot invalidate already captured immutable blobs.
  data.chunks.clear(); data.pcmSnapshots.clear();
  assert.equal((await result.chunks[0].blob.arrayBuffer()).byteLength,128000*2+44);
});

test('v1 existing-only snapshot reads its two stores and does not create PCM store',async () => {
  const {queue} = await journalQueue();
  await queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput());
  queue.readOnlyExisting = true;
  queue.open = async () => { queue.database = {version:1}; return queue; };
  const actual = queue._transaction;
  queue._transaction = (names,mode,operation,...rest) => {
    assert.deepEqual(names,['sessions','chunks']); assert.equal(mode,'readonly');
    return actual(names,mode,operation,...rest);
  };
  const result = await queue.readExportSnapshot(OWNER);
  assert.equal(result.chunks.length,1); assert.deepEqual(result.snapshots,[]);
});

test('export snapshot leaves invalid WAV metadata visible for later validation, never repairs it',async () => {
  const {queue,data} = await journalQueue();
  await queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput());
  const row = data.chunks.get(CHUNK_ID);
  row.blob = new Blob(); row.byteLength = 0;
  const result = await queue.readExportSnapshot(OWNER);
  assert.equal(result.chunks[0].blob.size,0);
  assert.equal(data.chunks.get(CHUNK_ID).byteLength,0);
  assert.equal(data.chunks.get(CHUNK_ID).state,'queued');
});

function snapshotInput({ sequence = 0, start = 0, duration = 32000, overlap = 0, sample = 0.125 } = {}) {
  return { sequence, startSamples: start, durationSamples: duration, overlapSamples: overlap,
    blob: encodeWav(new Float32Array(duration).fill(sample)) };
}

function chunkInput({ id = CHUNK_ID, start = 0, duration = 240000, overlap = 0, final = false, sample = 0.125 } = {}) {
  return { id, startSamples: start, durationSamples: duration, overlapSamples: overlap, final,
    blob: encodeWav(new Float32Array(duration).fill(sample)) };
}

test('v2 upgrades add only the PCM store and preserve existing v1 store definitions', async () => {
  const existing = new Map(['sessions', 'chunks'].map(name => [name, {
    indexNames: { contains: () => true }, marker: Symbol(name),
  }]));
  const markers = [...existing.values()].map(value => value.marker);
  const database = { objectStoreNames: { contains: name => existing.has(name) },
    createObjectStore(name, options) {
      assert.equal(name, 'pcmSnapshots');
      assert.equal(options.keyPath, 'captureId');
      const store = { createIndex(index, path) { assert.equal(index, 'owner'); assert.equal(path, 'owner'); } };
      existing.set(name, store);
      return store;
    }, close() {} };
  const indexedDB = { open(name, version) {
    assert.equal(version, 2);
    assert.equal(version, LIVE_QUEUE_DB_VERSION);
    const request = { result: database, transaction: { objectStore: name => existing.get(name) } };
    queueMicrotask(() => { request.onupgradeneeded(); request.onsuccess(); });
    return request;
  } };
  const queue = new DurableLiveQueue({ indexedDB, keyRange: { bound() {} } });
  await queue.open();
  assert.deepEqual([...existing.values()].slice(0, 2).map(value => value.marker), markers);
  queue.close();
});

test('a live old tab blocks the v2 upgrade explicitly and a late open is closed', async () => {
  let request, closed = false;
  const indexedDB = { open() {
    request = { result: { close() { closed = true; } } };
    queueMicrotask(() => request.onblocked());
    return request;
  } };
  const queue = new DurableLiveQueue({ indexedDB, keyRange: { bound() {} } });
  await assert.rejects(queue.open(), error => error.code === 'indexeddb_blocked');
  request.onsuccess();
  assert.equal(closed, true);
  assert.equal(queue.database, null);
});

test('partial PCM survives a new queue instance and explicitly promotes once with exact final samples', async () => {
  const { queue, data } = await journalQueue();
  const saved = await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ duration: 64037 }));
  const recovered = await queue.recoverOwner(OWNER);
  assert.equal(recovered.chunks.length, 0);
  assert.equal(recovered.snapshots.length, 1);
  assert.equal(recovered.snapshots[0].blob, undefined);
  assert.equal(recovered.stats.snapshotFreshSamples, 64037);
  const reopened = new DurableLiveQueue();
  reopened._transaction = queue._transaction;
  const chunk = await reopened.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: CHUNK_ID, expectedRevision: saved.snapshot.revision });
  assert.equal(chunk.final, true);
  assert.equal(chunk.durationSamples, 64037);
  assert.equal(chunk.startSamples, 0);
  assert.equal(chunk.overlapSamples, 0);
  assert.equal(data.pcmSnapshots.size, 0);
  const again = await reopened.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: CHUNK_ID, expectedRevision: saved.snapshot.revision });
  assert.equal(again.id, chunk.id);
  assert.equal(data.chunks.size, 1);
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 1);
});

test('normal chunk promotion atomically replaces PCM with its exact three-second guard', async () => {
  const { queue, data } = await journalQueue();
  const input = chunkInput();
  await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ duration: 224000 }));
  const chunk = await queue.enqueueChunk(OWNER, CAPTURE_ID, input);
  const guard = await queue.getSnapshot(OWNER, CAPTURE_ID);
  assert.equal(guard.sequence, 1);
  assert.equal(guard.startSamples, 192000);
  assert.equal(guard.durationSamples, 48000);
  assert.equal(guard.overlapSamples, 48000);
  assert.deepEqual(new Uint8Array(await guard.blob.slice(44).arrayBuffer()),
    new Uint8Array(await input.blob.slice(44 + 192000 * 2).arrayBuffer()));
  await queue.ackChunk(OWNER, chunk.id);
  assert.equal(data.chunks.size, 0);
  const final = await queue.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: '30000000-0000-4000-8000-000000000000', expectedRevision: guard.revision });
  assert.equal(final.durationSamples - final.overlapSamples, 0);
  assert.equal(final.startSamples + final.durationSamples, 240000);
  assert.equal(data.pcmSnapshots.size, 0);
});

test('failed atomic PCM promotion preserves both journal and cursor for exact-ID retry', async () => {
  const { queue, data, failures } = await journalQueue();
  const saved = await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput());
  failures.add('pcmSnapshots');
  await assert.rejects(queue.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: CHUNK_ID, expectedRevision: saved.snapshot.revision }), /fake write failure/);
  assert.equal(data.chunks.size, 0);
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 0);
  assert.equal(data.pcmSnapshots.get(CAPTURE_ID).revision, saved.snapshot.revision);
  failures.clear();
  await queue.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: CHUNK_ID, expectedRevision: saved.snapshot.revision });
  assert.equal(data.chunks.size, 1);
});

test('late snapshots before and after queued/ACKed audio never resurrect old PCM', async () => {
  const { queue, data, failures } = await journalQueue();
  const stale = snapshotInput();
  failures.add('pcmSnapshots');
  await assert.rejects(queue.saveSnapshot(OWNER, CAPTURE_ID, stale), /fake write failure/);
  failures.clear();
  const chunk = await queue.enqueueChunk(OWNER, CAPTURE_ID, chunkInput());
  const guard = await queue.getSnapshot(OWNER, CAPTURE_ID);
  assert.equal((await queue.saveSnapshot(OWNER, CAPTURE_ID, stale)).reason, 'settled');
  await queue.ackChunk(OWNER, chunk.id);
  assert.equal((await queue.saveSnapshot(OWNER, CAPTURE_ID, stale)).reason, 'settled');
  assert.equal(data.pcmSnapshots.get(CAPTURE_ID).revision, guard.revision);
  await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ sequence: 1, start: 192000, overlap: 48000, duration: 80000 }));
  const next = await queue.getSnapshot(OWNER, CAPTURE_ID);
  const final = await queue.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: '30000000-0000-4000-8000-000000000000', expectedRevision: next.revision });
  assert.equal(final.durationSamples - final.overlapSamples, 32000);
  assert.equal(final.startSamples + final.durationSamples, 272000);
});

test('PCM validation rejects owner changes, future sequence, wrong offsets and oversized buffers', async () => {
  const { queue, data } = await journalQueue();
  await assert.rejects(queue.saveSnapshot('other-owner', CAPTURE_ID, snapshotInput()), LiveQueueOwnershipError);
  await assert.rejects(queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ sequence: 1 })), LiveQueueConflictError);
  await assert.rejects(queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ start: 1 })), LiveQueueConflictError);
  await assert.rejects(queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ duration: 240001 })), LiveQueueValidationError);
  assert.equal(data.pcmSnapshots.size, 0);
  await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput());
  await assert.rejects(queue.getSnapshot('other-owner', CAPTURE_ID), LiveQueueOwnershipError);
  assert.equal((await queue.recoverOwner('other-owner')).snapshots.length, 0);
});

test('older/equal length snapshots never overwrite newer PCM and stale revision cannot finalize', async () => {
  const { queue, data } = await journalQueue();
  const first = await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput());
  const latest = await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ duration: 64000 }));
  assert.equal((await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput())).reason, 'not_newer');
  await assert.rejects(queue.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: CHUNK_ID, expectedRevision: first.snapshot.revision }), LiveQueueConflictError);
  await assert.rejects(queue.enqueueChunk(OWNER, CAPTURE_ID, chunkInput({ duration: 32000 })), LiveQueueConflictError);
  assert.equal(data.pcmSnapshots.get(CAPTURE_ID).revision, latest.snapshot.revision);
});

test('snapshot compare-and-promote detects a newer write between read and transaction', async () => {
  const { queue, data } = await journalQueue();
  const first = await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput());
  const enqueue = queue.enqueueChunk.bind(queue);
  queue.enqueueChunk = async (...args) => {
    await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ duration: 64000 }));
    return enqueue(...args);
  };
  await assert.rejects(queue.promoteSnapshot(OWNER, CAPTURE_ID,
    { id: CHUNK_ID, expectedRevision: first.snapshot.revision }), LiveQueueConflictError);
  assert.equal(data.chunks.size, 0);
  assert.equal(data.pcmSnapshots.get(CAPTURE_ID).durationSamples, 64000);
});

test('snapshot-only work prevents premature cleanup, and explicit deletion is owner-scoped', async () => {
  const { queue, data } = await journalQueue();
  await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput());
  assert.equal(await queue.hasPendingChunks(OWNER, CAPTURE_ID), true);
  const stats = await queue.getStats(OWNER);
  assert.equal(stats.count, 0);
  assert.equal(stats.snapshotCount, 1);
  assert.equal(stats.snapshotBytes, 44 + 64000);
  await assert.rejects(queue.deleteSession('other-owner', CAPTURE_ID), LiveQueueOwnershipError);
  await queue.deleteSession(OWNER, CAPTURE_ID);
  assert.equal(data.sessions.size, 0);
  assert.equal(data.pcmSnapshots.size, 0);
});

test('final ACK removes all local audio and late callbacks cannot recreate a finished session', async () => {
  const { queue, data } = await journalQueue();
  const value = snapshotInput();
  const first = await queue.saveSnapshot(OWNER, CAPTURE_ID, value);
  await queue.promoteSnapshot(OWNER, CAPTURE_ID, { id: CHUNK_ID, expectedRevision: first.snapshot.revision });
  assert.equal((await queue.saveSnapshot(OWNER, CAPTURE_ID, value)).reason, 'settled');
  await queue.ackChunk(OWNER, CHUNK_ID);
  await assert.rejects(queue.saveSnapshot(OWNER, CAPTURE_ID, value));
  assert.equal(data.sessions.size + data.chunks.size + data.pcmSnapshots.size, 0);
});

function receipt({ sequence = 0, ...options } = {}) {
  const { blob, ...metadata } = chunkInput(options);
  return { ...metadata, sequence };
}

test('trusted legacy RAM-only ACK advances exact cursor without recreating a WAV', async () => {
  const { queue, data } = await journalQueue();
  const stale = snapshotInput();
  await queue.saveSnapshot(OWNER, CAPTURE_ID, stale);
  const result = await queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt(), { serverConfirmed: true });
  assert.equal(result.advanced, true);
  assert.equal(data.chunks.size, 0);
  assert.equal(data.pcmSnapshots.size, 0);
  assert.equal(data.sessions.get(CAPTURE_ID).capturedSamples, 240000);
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 1);
  const repeated = await queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt(), { serverConfirmed: true });
  assert.equal(repeated.advanced, false);
  assert.equal((await queue.saveSnapshot(OWNER, CAPTURE_ID, stale)).reason, 'settled');
  await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ sequence: 1, start: 192000, duration: 64000, overlap: 48000 }));
  assert.equal(data.pcmSnapshots.get(CAPTURE_ID).durationSamples - data.pcmSnapshots.get(CAPTURE_ID).overlapSamples, 16000);
});

test('settled receipts require explicit booleans, owner, order and exact existing chunk identity', async () => {
  const { queue, data } = await journalQueue();
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt()), LiveQueueValidationError);
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt(), { serverConfirmed: 'true' }), LiveQueueValidationError);
  await assert.rejects(queue.advanceSettledChunk('other-owner', CAPTURE_ID, receipt(), { serverConfirmed: true }), LiveQueueOwnershipError);
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt({ sequence: 1 }), { serverConfirmed: true }), LiveQueueConflictError);
  await queue.enqueueChunk(OWNER, CAPTURE_ID, chunkInput());
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID,
    receipt({ id: '30000000-0000-4000-8000-000000000000' }), { serverConfirmed: true }), LiveQueueConflictError);
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt({ duration: 239999 }), { serverConfirmed: true }), LiveQueueConflictError);
  const result = await queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt(), { downloadRequested: true });
  assert.equal(result.advanced, false);
  assert.equal(data.chunks.size, 0);
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 1);
  assert.equal(data.pcmSnapshots.get(CAPTURE_ID).overlapSamples, 48000);
});

test('a settled receipt cannot erase fresh PCM beyond its acknowledged end', async () => {
  const { queue, data } = await journalQueue();
  await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput({ duration: 64000 }));
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID,
    receipt({ duration: 32000 }), { serverConfirmed: true }), LiveQueueConflictError);
  assert.equal(data.pcmSnapshots.get(CAPTURE_ID).durationSamples, 64000);
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 0);
});

test('final receipt cannot jump over pending chunks and is idempotent after they settle', async () => {
  const { queue, data } = await journalQueue();
  await queue.enqueueChunk(OWNER, CAPTURE_ID, chunkInput());
  const final = receipt({ id: '30000000-0000-4000-8000-000000000000', sequence: 1, start: 192000, duration: 64000, overlap: 48000, final: true });
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID, final, { downloadRequested: true }), LiveQueueConflictError);
  await queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt(), { serverConfirmed: true });
  await queue.advanceSettledChunk(OWNER, CAPTURE_ID, final, { downloadRequested: true });
  await queue.advanceSettledChunk(OWNER, CAPTURE_ID, final, { downloadRequested: true });
  assert.equal(data.chunks.size + data.pcmSnapshots.size, 0);
  assert.equal(data.sessions.get(CAPTURE_ID).state, 'completed');
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 2);
  assert.equal((await queue.recoverOwner(OWNER)).sessions.length, 0);
});

test('receipt write failure rolls back cursor and keeps PCM for a bounded retry', async () => {
  const { queue, data, failures } = await journalQueue();
  await queue.saveSnapshot(OWNER, CAPTURE_ID, snapshotInput());
  failures.add('sessions');
  await assert.rejects(queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt(), { serverConfirmed: true }), /fake write failure/);
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 0);
  assert.equal(data.pcmSnapshots.size, 1);
  failures.clear();
  await queue.advanceSettledChunk(OWNER, CAPTURE_ID, receipt(), { serverConfirmed: true });
  assert.equal(data.sessions.get(CAPTURE_ID).nextSequence, 1);
});

async function heldQueueFixture() {
  const fixture = memoryQueue();
  const {queue} = fixture;
  await queue.createSession({id:CAPTURE_ID, owner:OWNER, title:'보관할 합성 수업',
    source:'microphone', asrProvider:'clova', language:'ko'});
  const ids = [CHUNK_ID, '20000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002'];
  for (let index = 0; index < ids.length; index += 1) {
    await queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput({id:ids[index],start:index * 800,duration:800}));
  }
  await queue.markChunkInflight(OWNER,ids[0]);
  await queue.markChunkInflight(OWNER,ids[1]);
  await queue.markChunkBlocked(OWNER,ids[1],'response_lost');
  await queue.saveSnapshot(OWNER,CAPTURE_ID,snapshotInput({sequence:3,start:1600,duration:1600,overlap:800}));
  return {...fixture,ids};
}

async function audioRowsWithBytes(data) {
  return Promise.all(['chunks','pcmSnapshots'].map(async name => [name,await Promise.all(
    [...data[name]].map(async ([id,row]) => {
      // Compare every original field and byte, separately asserting the only
      // newly permitted field (the strict-reader uploadHeld guard) below.
      const {uploadHeld,...original} = row;
      return [id,{...original,blob:new Uint8Array(await row.blob.arrayBuffer())}];
    }),
  )]));
}

test('explicit upload hold survives reopen and preserves every uncertain chunk and unfinished PCM byte',async () => {
  const {queue,data} = await heldQueueFixture();
  const before = await audioRowsWithBytes(data);
  const previousSession = await queue.getSession(OWNER,CAPTURE_ID);
  const held = await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  assert.deepEqual(held,{...previousSession,uploadHeld:true});
  assert.equal([...data.chunks.values()].every(row=>row.uploadHeld===true),true);
  assert.deepEqual(await audioRowsWithBytes(data),before);
  const reopened = new DurableLiveQueue({keyRange:queue.keyRange});
  reopened._transaction = queue._transaction;
  const recovered = await reopened.recoverOwner(OWNER);
  assert.equal(recovered.sessions[0].uploadHeld,true);
  assert.deepEqual(recovered.chunks.map(row => row.state),['inflight','blocked','queued']);
  assert.equal(recovered.chunks.every(row=>row.uploadHeld===true),true);
  assert.equal(recovered.snapshots[0].durationSamples,1600);
  assert.equal(recovered.inflightChunks.length,1);
  assert.equal(recovered.stats.count,3);
  assert.equal(recovered.stats.snapshotFreshSamples,800);
  const exported = await reopened.readExportSnapshot(OWNER);
  assert.equal(exported.sessions[0].uploadHeld,true);
  assert.equal(exported.chunks.length,3);
  assert.equal(exported.snapshots.length,1);
  assert.deepEqual(await audioRowsWithBytes(data),before);
});

test('explicit resume removes only the hold gate and never clears CLOVA inflight or blocked uncertainty',async () => {
  const {queue,data,ids} = await heldQueueFixture();
  const before = await audioRowsWithBytes(data);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  for (const id of ids) {
    for (const change of ['markChunkQueued','markChunkInflight']) {
      await assert.rejects(queue[change](OWNER,id),error => error instanceof LiveQueueConflictError
        && error.code === 'live_queue_upload_held');
    }
  }
  assert.deepEqual(await audioRowsWithBytes(data),before);
  const resumed = await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  assert.equal(Object.hasOwn(resumed,'uploadHeld'),false);
  assert.equal(Object.hasOwn(data.sessions.get(CAPTURE_ID),'uploadHeld'),false);
  assert.equal([...data.chunks.values()].every(row=>!Object.hasOwn(row,'uploadHeld')),true);
  assert.deepEqual(await audioRowsWithBytes(data),before);
  await assert.rejects(queue.markChunkInflight(OWNER,ids[0]),LiveQueueConflictError);
  await assert.rejects(queue.markChunkInflight(OWNER,ids[1]),LiveQueueConflictError);
  assert.equal((await queue.markChunkInflight(OWNER,ids[2])).state,'inflight');
});

test('hold and resume are strict owner-only decisions and repeated decisions do not write',async () => {
  const {queue,data,failures} = await heldQueueFixture();
  const before = structuredClone(data.sessions);
  for (const value of [undefined,null,1,0,'true','false',{},[]]) {
    await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,value),LiveQueueValidationError);
  }
  await assert.rejects(queue.setSessionUploadHeld('other-owner',CAPTURE_ID,true),LiveQueueOwnershipError);
  await assert.rejects(queue.setSessionUploadHeld(OWNER,'30000000-0000-4000-8000-000000000000',true),
    error => error.code === 'live_queue_not_found');
  assert.deepEqual(data.sessions,before);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  failures.add('sessions');
  assert.equal((await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true)).uploadHeld,true);
  await assert.rejects(queue.setSessionUploadHeld('other-owner',CAPTURE_ID,false),LiveQueueOwnershipError);
  failures.clear();
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  failures.add('sessions');
  assert.equal(Object.hasOwn(await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false),'uploadHeld'),false);
});

test('failed hold or resume commits preserve the prior gate and all local audio for explicit retry',async () => {
  const {queue,data,failures} = await heldQueueFixture();
  const before = await audioRowsWithBytes(data);
  const original = structuredClone(data.sessions);
  failures.add('sessions');
  await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true),/fake write failure/);
  assert.deepEqual(data.sessions,original);
  assert.deepEqual(await audioRowsWithBytes(data),before);
  failures.clear();
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  const held = structuredClone(data.sessions);
  failures.add('sessions');
  await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false),/fake write failure/);
  assert.deepEqual(data.sessions,held);
  assert.deepEqual(await audioRowsWithBytes(data),before);
  failures.clear();
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  assert.deepEqual(await audioRowsWithBytes(data),before);
});

test('a held older capture does not gate a new capture or disclose another owner recordings',async () => {
  const {queue,data} = await heldQueueFixture();
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  const oldBytes = await audioRowsWithBytes(data);
  const newId = '30000000-0000-4000-8000-000000000000';
  const newChunk = '40000000-0000-4000-8000-000000000000';
  await queue.createSession({id:newId,owner:OWNER,title:'새 합성 수업',source:'microphone',asrProvider:'qwen'});
  await queue.enqueueChunk(OWNER,newId,chunkInput({id:newChunk,duration:800,final:true}));
  assert.equal((await queue.markChunkQueued(OWNER,newChunk)).state,'queued');
  await queue.ackChunk(OWNER,newChunk,{serverConfirmed:true});
  assert.equal(data.sessions.has(newId),false);
  assert.equal(data.chunks.has(newChunk),false);
  assert.deepEqual(await audioRowsWithBytes(data),oldBytes);
  assert.equal((await queue.getSession(OWNER,CAPTURE_ID)).uploadHeld,true);
  const other = await queue.recoverOwner('other-owner');
  assert.equal(other.sessions.length,0);
  assert.equal(other.chunks.length,0);
  assert.equal(other.snapshots.length,0);
});

test('held capture still persists trailing chunks and snapshots without changing its transmission gate',async () => {
  const {queue,data} = await journalQueue();
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  await queue.saveSnapshot(OWNER,CAPTURE_ID,snapshotInput({duration:800}));
  const final = await queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput({duration:800,final:true}));
  assert.equal(final.state,'queued');
  assert.equal(final.uploadHeld,true);
  assert.equal(final.attempts,0);
  assert.equal(final.downloadRequested,false);
  assert.equal(data.pcmSnapshots.size,0);
  const stored = await queue.getSession(OWNER,CAPTURE_ID);
  assert.equal(stored.uploadHeld,true);
  assert.equal(stored.state,'stopped');
  assert.equal(stored.finalQueued,true);
  await assert.rejects(queue.markChunkQueued(OWNER,CHUNK_ID),error => error.code === 'live_queue_upload_held');
});

test('late session updates and stale create retries cannot implicitly clear or reactivate a hold',async () => {
  const {queue} = memoryQueue();
  const input = {id:CAPTURE_ID,owner:OWNER,title:'합성 세션',source:'microphone',asrProvider:'qwen',createdAt:1700000000000};
  await queue.createSession(input);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  for (const state of ['paused','input-unavailable','recording','blocked','stopped']) {
    assert.equal((await queue.updateSession(OWNER,CAPTURE_ID,{state})).uploadHeld,true);
  }
  assert.equal((await queue.createSession(input)).uploadHeld,true);
  await assert.rejects(queue.updateSession(OWNER,CAPTURE_ID,{uploadHeld:false}),LiveQueueValidationError);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  assert.equal(Object.hasOwn(await queue.createSession({...input,uploadHeld:true}),'uploadHeld'),false);
});

test('RAM-only recovery can create a held session atomically while ordinary v2 rows stay compatible',async () => {
  const {queue,data} = memoryQueue();
  const input = {id:CAPTURE_ID,owner:OWNER,title:'합성 복구',source:'microphone',asrProvider:'qwen'};
  for (const uploadHeld of [false,undefined,null,'true',1,{}]) {
    await assert.rejects(queue.createSession({...input,uploadHeld}),LiveQueueValidationError);
  }
  const held = await queue.createSession({...input,uploadHeld:true});
  assert.equal(held.uploadHeld,true);
  assert.equal(LIVE_QUEUE_DB_VERSION,2);
  const legacyKeys = new Set(['id','owner','title','language','source','asrProvider','createdAt','updatedAt',
    'state','lectureCreated','finalQueued','nextSequence','capturedSamples']);
  // The deployed legacy reader only accepts these exact keys, so it cannot
  // silently interpret a newly held session as an ordinary queued session.
  assert.deepEqual(Object.keys(data.sessions.get(CAPTURE_ID)).filter(key => !legacyKeys.has(key)),['uploadHeld']);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  assert.equal(Object.keys(data.sessions.get(CAPTURE_ID)).every(key => legacyKeys.has(key)),true);
});

test('malformed stored hold markers fail closed and completed sessions cannot be held',async () => {
  const {queue,data} = await journalQueue();
  for (const value of [false,null,'true',1]) {
    data.sessions.get(CAPTURE_ID).uploadHeld = value;
    await assert.rejects(queue.getSession(OWNER,CAPTURE_ID),LiveQueueCorruptError);
    await assert.rejects(queue.recoverOwner(OWNER),LiveQueueCorruptError);
    await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false),LiveQueueCorruptError);
  }
  delete data.sessions.get(CAPTURE_ID).uploadHeld;
  data.sessions.get(CAPTURE_ID).state = 'completed';
  data.sessions.get(CAPTURE_ID).finalQueued = true;
  await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true),LiveQueueConflictError);
});

test('hold and CLOVA inflight transitions serialize across queue instances without erasing ambiguity',async () => {
  const {queue,data} = memoryQueue();
  await queue.createSession({id:CAPTURE_ID,owner:OWNER,title:'합성 경합',source:'microphone',asrProvider:'clova'});
  await queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput({duration:800}));
  const another = new DurableLiveQueue({keyRange:queue.keyRange});
  another._transaction = queue._transaction;
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  await assert.rejects(another.markChunkInflight(OWNER,CHUNK_ID),error => error.code === 'live_queue_upload_held');
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  await Promise.all([
    another.markChunkInflight(OWNER,CHUNK_ID),
    queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true),
  ]);
  assert.equal(data.chunks.get(CHUNK_ID).state,'inflight');
  assert.equal(data.chunks.get(CHUNK_ID).attempts,1);
  await another.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  assert.equal(data.chunks.get(CHUNK_ID).state,'inflight');
  await assert.rejects(queue.markChunkInflight(OWNER,CHUNK_ID),LiveQueueConflictError);
});

// Frozen pre-hold v2 chunk whitelist. These legacy methods intentionally do
// not inspect the session: stopped old tabs used only this strict chunk reader
// for getChunk/markChunkQueued/markChunkInflight. No Git revision is needed in
// CI, and accepting new fields here would invalidate this compatibility test.
function frozenLegacyChunkReader(queue) {
  const keys = new Set(['id','captureId','owner','sessionCreatedAt','sequence','startSamples',
    'durationSamples','overlapSamples','final','asrProvider','blob','byteLength','state','attempts',
    'errorKind','downloadRequested','createdAt','updatedAt']);
  const read = (owner,id,change) => queue._transaction(['chunks'],change?'readwrite':'readonly',async stores=>{
    const value=await new Promise((resolve,reject)=>{
      const request=stores.chunks.get(id);request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });
    if(!value)return null;
    if(Object.keys(value).length!==keys.size || Object.keys(value).some(key=>!keys.has(key))) {
      throw new Error('legacy_strict_chunk_rejected');
    }
    if(value.owner!==owner)throw new Error('legacy_owner_mismatch');
    if(change){
      change(value);
      await new Promise((resolve,reject)=>{
        const request=stores.chunks.put(value);request.onsuccess=()=>resolve();request.onerror=()=>reject(request.error);
      });
    }
    return value;
  });
  return {
    getChunk:(owner,id)=>read(owner,id),
    markChunkQueued:(owner,id)=>read(owner,id,row=>{row.state='queued';row.errorKind='';}),
    markChunkInflight:(owner,id)=>read(owner,id,row=>{
      if(row.asrProvider!=='clova'||row.state!=='queued')throw new Error('legacy_conflict');
      row.state='inflight';row.attempts+=1;row.errorKind='';
    }),
  };
}

test('all held chunk guards block legacy stopped-tab read and transitions until explicit resume',async () => {
  const {queue,data,ids} = await heldQueueFixture();
  const legacy=frozenLegacyChunkReader(queue);
  assert.equal((await legacy.getChunk(OWNER,ids[2])).state,'queued');
  const original=await audioRowsWithBytes(data);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  for(const id of ids){
    assert.equal((await queue.getChunk(OWNER,id)).uploadHeld,true);
    for(const method of ['getChunk','markChunkQueued','markChunkInflight']){
      await assert.rejects(legacy[method](OWNER,id),/legacy_strict_chunk_rejected/);
    }
  }
  assert.deepEqual(await audioRowsWithBytes(data),original);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  for(const id of ids)assert.equal(Object.hasOwn(await legacy.getChunk(OWNER,id),'uploadHeld'),false);
  assert.deepEqual(await audioRowsWithBytes(data),original);
});

test('hold and resume guards roll back together after a middle chunk write fails',async () => {
  for(const held of [true,false]){
    const {queue,data}=await heldQueueFixture();
    if(!held)await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
    const original=structuredClone(data);
    const originalBytes=await audioRowsWithBytes(data);
    const transaction=queue._transaction;
    queue._transaction=(names,mode,operation)=>transaction(names,mode,stores=>{
      let writes=0;
      if(stores.chunks){
        const put=stores.chunks.put;
        stores.chunks.put=value=>++writes===2
          ? asynchronousRequest(null,new DOMException('synthetic quota','QuotaExceededError')) : put(value);
      }
      return operation(stores);
    });
    await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,held),error=>error.name==='QuotaExceededError');
    assert.deepEqual(data,original);
    assert.deepEqual(await audioRowsWithBytes(data),originalBytes);
    queue._transaction=transaction;
    await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,held);
    assert.equal(data.sessions.get(CAPTURE_ID).uploadHeld===true,held);
    assert.equal([...data.chunks.values()].every(row=>(row.uploadHeld===true)===held),true);
  }
});

test('queued writes racing a hold inherit its guard and keep all sample metadata',async () => {
  for(const holdFirst of [true,false]){
    const {queue,data}=await journalQueue();
    const save=()=>queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput({duration:800,final:true}));
    const hold=()=>queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
    if(holdFirst){await hold();await save();}else{await save();await hold();}
    const row=data.chunks.get(CHUNK_ID);
    assert.equal(row.uploadHeld,true);assert.equal(row.state,'queued');assert.equal(row.attempts,0);
    assert.equal(row.durationSamples,800);assert.equal(row.final,true);assert.equal(row.updatedAt,1700000000000);
    assert.equal(data.sessions.get(CAPTURE_ID).uploadHeld,true);
    await assert.rejects(frozenLegacyChunkReader(queue).getChunk(OWNER,CHUNK_ID),/legacy_strict_chunk_rejected/);
  }
});

test('holding one capture does not mark another capture or owner and never reads WAV payloads',async () => {
  const {queue,data}=await heldQueueFixture();
  const otherId='50000000-0000-4000-8000-000000000000',otherChunk='60000000-0000-4000-8000-000000000000';
  await queue.createSession({id:otherId,owner:'other-owner',title:'다른 합성 수업',source:'microphone',asrProvider:'qwen'});
  await queue.enqueueChunk('other-owner',otherId,chunkInput({id:otherChunk,duration:800}));
  const untouched=structuredClone(data.chunks.get(otherChunk));
  const oldArrayBuffer=Blob.prototype.arrayBuffer,oldSlice=Blob.prototype.slice;
  Blob.prototype.arrayBuffer=()=>{throw new Error('hold must not read a WAV');};
  Blob.prototype.slice=()=>{throw new Error('hold must not slice a WAV');};
  try{await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);}
  finally{Blob.prototype.arrayBuffer=oldArrayBuffer;Blob.prototype.slice=oldSlice;}
  assert.deepEqual(data.chunks.get(otherChunk),untouched);
  assert.equal(Object.hasOwn(data.sessions.get(otherId),'uploadHeld'),false);
});

test('malformed chunk guard fails closed while a stray true guard blocks transitions until explicit resume',async () => {
  const {queue,data}=await journalQueue();
  await queue.enqueueChunk(OWNER,CAPTURE_ID,chunkInput({duration:800}));
  for(const uploadHeld of [false,null,'true',1]){
    data.chunks.get(CHUNK_ID).uploadHeld=uploadHeld;
    await assert.rejects(queue.getChunk(OWNER,CHUNK_ID),LiveQueueCorruptError);
    await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true),LiveQueueCorruptError);
    assert.equal(Object.hasOwn(data.sessions.get(CAPTURE_ID),'uploadHeld'),false);
  }
  data.chunks.get(CHUNK_ID).uploadHeld=true;
  await assert.rejects(queue.markChunkQueued(OWNER,CHUNK_ID),error=>error.code==='live_queue_upload_held');
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  assert.equal(Object.hasOwn(data.chunks.get(CHUNK_ID),'uploadHeld'),false);
});

async function closableHeldQueue() {
  const fixture = await heldQueueFixture();
  await fixture.queue.setSessionUploadHeld(OWNER,CAPTURE_ID,true);
  const snapshot = await fixture.queue.readExportSnapshot(OWNER);
  return {...fixture,snapshot,expectedManifest:heldRecoveryManifest(snapshot,CAPTURE_ID)};
}

test('held recovery manifest is stable across record ordering and ignores unrelated captures',async () => {
  const {snapshot,expectedManifest} = await closableHeldQueue();
  const reordered = structuredClone(snapshot);
  reordered.chunks.reverse();
  for (const group of ['sessions','chunks','snapshots']) {
    reordered[group] = reordered[group].map(row => Object.fromEntries(Object.entries(row).reverse()));
  }
  assert.equal(heldRecoveryManifest(reordered,CAPTURE_ID),expectedManifest);
  reordered.sessions.unshift({id:'30000000-0000-4000-8000-000000000000'});
  reordered.chunks.push({captureId:'30000000-0000-4000-8000-000000000000'});
  assert.equal(heldRecoveryManifest(reordered,CAPTURE_ID),expectedManifest);
  assert.equal(snapshot.sessions[0].recoveryClosedAt,undefined);
});

test('held recovery closure survives requery and changes only the session marker and update time',async () => {
  const {queue,data,expectedManifest,ids} = await closableHeldQueue();
  const before = structuredClone(data), bytes = await audioRowsWithBytes(data);
  queue.now = () => 1700000001000;
  const calls = [], transaction = queue._transaction;
  queue._transaction = (names,mode,operation) => {
    calls.push({names,mode}); return transaction(names,mode,operation);
  };
  const closed = await queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true});
  assert.deepEqual(calls,[{names:['sessions','chunks','pcmSnapshots'],mode:'readwrite'}]);
  assert.deepEqual(closed,{...before.sessions.get(CAPTURE_ID),recoveryClosedAt:1700000001000,updatedAt:1700000001000});
  assert.deepEqual(data.chunks,before.chunks); assert.deepEqual(data.pcmSnapshots,before.pcmSnapshots);
  assert.deepEqual(await audioRowsWithBytes(data),bytes);
  const reopenedQueue = new DurableLiveQueue({keyRange:queue.keyRange});
  reopenedQueue._transaction = transaction;
  assert.deepEqual(await reopenedQueue.getSession(OWNER,CAPTURE_ID),closed);
  const recovered = await reopenedQueue.recoverOwner(OWNER);
  assert.equal(recovered.sessions[0].recoveryClosedAt,closed.recoveryClosedAt);
  assert.equal(recovered.chunks.length,3); assert.equal(recovered.snapshots.length,1);
  assert.deepEqual(recovered.chunks.map(row => row.state),['inflight','blocked','queued']);
  assert.equal((await reopenedQueue.readExportSnapshot(OWNER)).sessions[0].recoveryClosedAt,closed.recoveryClosedAt);
  for (const id of ids) {
    await assert.rejects(queue.markChunkQueued(OWNER,id),error => error.code === 'live_queue_upload_held');
    await assert.rejects(frozenLegacyChunkReader(queue).getChunk(OWNER,id),/legacy_strict_chunk_rejected/);
  }
  await assert.rejects(queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false),LiveQueueConflictError);
  assert.deepEqual(data.chunks,before.chunks);
  assert.equal(LIVE_QUEUE_DB_VERSION,2);
});

test('closure is owner-only and requires explicit file confirmation for the exact downloaded manifest',async () => {
  const {queue,data,expectedManifest} = await closableHeldQueue();
  const before = structuredClone(data);
  for (const options of [undefined,{}, {expectedManifest}, {expectedManifest,filesConfirmed:false},
    {expectedManifest,filesConfirmed:'true'},{expectedManifest:1,filesConfirmed:true},
    {expectedManifest:'',filesConfirmed:true},{expectedManifest,filesConfirmed:true,skip:true}]) {
    await assert.rejects(queue.closeHeldRecovery(OWNER,CAPTURE_ID,options),LiveQueueValidationError);
  }
  await assert.rejects(queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest:'wrong',filesConfirmed:true}),LiveQueueConflictError);
  await assert.rejects(queue.closeHeldRecovery('other-owner',CAPTURE_ID,{expectedManifest,filesConfirmed:true}),LiveQueueOwnershipError);
  await assert.rejects(queue.reopenHeldRecovery('other-owner',CAPTURE_ID),LiveQueueOwnershipError);
  await assert.rejects(queue.closeHeldRecovery(OWNER,'30000000-0000-4000-8000-000000000000',{expectedManifest,filesConfirmed:true}),
    error => error.code === 'live_queue_not_found');
  assert.deepEqual(data,before);
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  await assert.rejects(queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true}),LiveQueueConflictError);
  await assert.rejects(queue.reopenHeldRecovery(OWNER,CAPTURE_ID),LiveQueueConflictError);
});

test('changes to downloaded session chunk or PCM metadata invalidate closure without hiding audio',async () => {
  for (const change of [
    data => { data.sessions.get(CAPTURE_ID).updatedAt += 1; },
    data => { data.sessions.get(CAPTURE_ID).nextSequence += 1; data.pcmSnapshots.get(CAPTURE_ID).sequence += 1; },
    data => { data.chunks.get(CHUNK_ID).updatedAt += 1; },
    data => { data.chunks.get(CHUNK_ID).attempts += 1; },
    data => { data.chunks.get(CHUNK_ID).downloadRequested = true; },
    data => { data.chunks.get(CHUNK_ID).state = 'blocked'; data.chunks.get(CHUNK_ID).errorKind = 'response_lost'; },
    data => { data.chunks.delete(CHUNK_ID); },
    data => { data.pcmSnapshots.get(CAPTURE_ID).revision += 1; },
    data => { data.pcmSnapshots.get(CAPTURE_ID).updatedAt += 1; },
  ]) {
    const {queue,data,expectedManifest} = await closableHeldQueue();
    change(data);
    const changed = structuredClone(data);
    await assert.rejects(queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true}),LiveQueueConflictError);
    assert.deepEqual(data,changed);
    assert.equal(Object.hasOwn(data.sessions.get(CAPTURE_ID),'recoveryClosedAt'),false);
  }
});

test('manifest rejects malformed or mismatched target records without reading WAV payloads',async () => {
  const {queue,snapshot,expectedManifest} = await closableHeldQueue();
  for (const change of [
    value => { value.sessions.push(value.sessions[0]); },
    value => { value.chunks.push(value.chunks[0]); },
    value => { value.snapshots.push(value.snapshots[0]); },
    value => { value.chunks[0].byteLength = 0; },
    value => { value.chunks[0].sessionCreatedAt += 1; },
    value => { value.chunks[0].asrProvider = 'qwen'; },
    value => { delete value.chunks[0].uploadHeld; },
    value => { value.snapshots[0].sequence += 1; },
    value => { value.sessions[0].finalQueued = true; },
  ]) {
    const invalid = structuredClone(snapshot); change(invalid);
    assert.throws(() => heldRecoveryManifest(invalid,CAPTURE_ID),LiveQueueCorruptError);
  }
  for (const group of ['sessions','chunks','snapshots']) {
    const invalid = structuredClone(snapshot); invalid[group][0].owner = 'other-owner';
    assert.throws(() => heldRecoveryManifest(invalid,CAPTURE_ID),LiveQueueOwnershipError);
  }
  for (const value of [null,{}, {...snapshot,chunks:null}]) {
    assert.throws(() => heldRecoveryManifest(value,CAPTURE_ID),LiveQueueValidationError);
  }
  const arrayBuffer = Blob.prototype.arrayBuffer, slice = Blob.prototype.slice;
  Blob.prototype.arrayBuffer = () => { throw new Error('must not read WAV data'); };
  Blob.prototype.slice = () => { throw new Error('must not slice WAV data'); };
  try {
    assert.equal(heldRecoveryManifest(snapshot,CAPTURE_ID),expectedManifest);
    await queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true});
    await queue.reopenHeldRecovery(OWNER,CAPTURE_ID);
  } finally { Blob.prototype.arrayBuffer = arrayBuffer; Blob.prototype.slice = slice; }
});

test('reopen changes only the closure marker and update time while keeping uncertainty and upload guards',async () => {
  const {queue,data,expectedManifest,ids} = await closableHeldQueue();
  await queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true});
  const before = structuredClone(data), bytes = await audioRowsWithBytes(data);
  queue.now = () => 1700000002000;
  const reopened = await queue.reopenHeldRecovery(OWNER,CAPTURE_ID);
  const {recoveryClosedAt,...withoutMarker} = before.sessions.get(CAPTURE_ID);
  assert.ok(recoveryClosedAt);
  assert.deepEqual(reopened,{...withoutMarker,updatedAt:1700000002000});
  assert.equal(reopened.uploadHeld,true);
  assert.deepEqual(data.chunks,before.chunks); assert.deepEqual(data.pcmSnapshots,before.pcmSnapshots);
  assert.deepEqual(await audioRowsWithBytes(data),bytes);
  for (const id of ids) await assert.rejects(queue.markChunkQueued(OWNER,id),error => error.code === 'live_queue_upload_held');
  await queue.setSessionUploadHeld(OWNER,CAPTURE_ID,false);
  assert.equal(Object.hasOwn(await queue.getSession(OWNER,CAPTURE_ID),'recoveryClosedAt'),false);
  assert.deepEqual([...data.chunks.values()].map(row => row.state),['inflight','blocked','queued']);
});

test('closure and reopen roll back failed writes and repeated decisions remain idempotent',async () => {
  const {queue,data,failures,expectedManifest} = await closableHeldQueue();
  const before = structuredClone(data);
  failures.add('sessions');
  await assert.rejects(queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true}),/fake write failure/);
  assert.deepEqual(data,before);
  failures.clear();
  const closed = await queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true});
  const after = structuredClone(data);
  queue.now = () => 1700000003000;
  failures.add('sessions');
  assert.deepEqual(await queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true}),closed);
  await assert.rejects(queue.reopenHeldRecovery(OWNER,CAPTURE_ID),/fake write failure/);
  assert.deepEqual(data,after);
  failures.clear();
  const reopened = await queue.reopenHeldRecovery(OWNER,CAPTURE_ID);
  failures.add('sessions');
  assert.deepEqual(await queue.reopenHeldRecovery(OWNER,CAPTURE_ID),reopened);
});

test('malformed closure markers fail closed and ordinary creates or updates cannot clear a closure',async () => {
  for (const value of [undefined,null,false,true,0,-1,1.5,'1700000000000',Infinity,8640000000000001]) {
    const {queue,data} = await closableHeldQueue();
    data.sessions.get(CAPTURE_ID).recoveryClosedAt = value;
    await assert.rejects(queue.getSession(OWNER,CAPTURE_ID),LiveQueueCorruptError);
    await assert.rejects(queue.recoverOwner(OWNER),LiveQueueCorruptError);
    await assert.rejects(queue.reopenHeldRecovery(OWNER,CAPTURE_ID),LiveQueueCorruptError);
  }
  const {queue,data,expectedManifest} = await closableHeldQueue();
  await queue.closeHeldRecovery(OWNER,CAPTURE_ID,{expectedManifest,filesConfirmed:true});
  const closedAt = data.sessions.get(CAPTURE_ID).recoveryClosedAt;
  assert.equal((await queue.updateSession(OWNER,CAPTURE_ID,{state:'stopped'})).recoveryClosedAt,closedAt);
  assert.equal((await queue.createSession({id:CAPTURE_ID,owner:OWNER,title:'보관할 합성 수업',
    source:'microphone',asrProvider:'clova',language:'ko'})).recoveryClosedAt,closedAt);
  await assert.rejects(queue.updateSession(OWNER,CAPTURE_ID,{recoveryClosedAt:null}),LiveQueueValidationError);
  delete data.sessions.get(CAPTURE_ID).uploadHeld;
  await assert.rejects(queue.getSession(OWNER,CAPTURE_ID),LiveQueueCorruptError);
});
