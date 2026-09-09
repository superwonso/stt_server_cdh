import assert from 'node:assert/strict';
import { test } from 'node:test';
import { encodeWav } from '../web/audio.js';
import {
  DurableLiveQueue, LiveQueueConflictError, LiveQueueOwnershipError, LiveQueueValidationError, LiveQueueCorruptError,
  LIVE_QUEUE_DB_VERSION,
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
