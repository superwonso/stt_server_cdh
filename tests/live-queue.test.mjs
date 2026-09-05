import assert from 'node:assert/strict';
import { test } from 'node:test';
import { encodeWav } from '../web/audio.js';
import {
  DurableLiveQueue, LiveQueueConflictError, LiveQueueOwnershipError, LiveQueueValidationError,
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
  const data = {sessions:new Map([[session.id,session]]),chunks:new Map([[chunk.id,chunk]])};
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
