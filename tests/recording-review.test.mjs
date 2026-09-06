import assert from 'node:assert/strict';
import {test} from 'node:test';
import {encodeWav} from '../web/audio.js';
import {readRecordingClip,RecordingClipPlayer,filterTranscript} from '../web/recording-review.js';

function response(blob, changes = {}) {
  return new Response(blob,{headers:{'Content-Type':'audio/wav','Content-Length':String(blob.size),
    'X-Clip-Start-Seconds':'12.5','X-Clip-Duration-Seconds':String((blob.size - 44) / 32000),...changes}});
}
test('a bounded clip preserves PCM and its absolute lesson position', async () => {
  const blob = encodeWav(new Float32Array(16000).fill(0.25));
  const result = await readRecordingClip(response(blob));
  assert.equal(result.startSeconds,12.5); assert.equal(result.durationSeconds,1);
  assert.deepEqual(await result.blob.arrayBuffer(),await blob.arrayBuffer());
});
test('oversize, missing metadata, truncated and non-WAV responses are rejected', async () => {
  const blob = encodeWav(new Float32Array(16000));
  for (const changes of [{'Content-Length':'999999999'}, {'X-Clip-Start-Seconds':''},
    {'X-Clip-Duration-Seconds':'NaN'}, {'Content-Type':'text/html'}, {'Content-Length':String(blob.size + 1)}]) {
    await assert.rejects(readRecordingClip(response(blob,changes)));
  }
  const bytes = new Uint8Array(await blob.arrayBuffer()); bytes[0] = 0;
  await assert.rejects(readRecordingClip(response(new Blob([bytes]))));
});
function playerFixture(fetchClip) {
  const states = [], revoked = [];
  let counter = 0;
  const audio = {currentTime:0,pause(){},load(){},removeAttribute(key){delete this[key];},async play(){}};
  const player = new RecordingClipPlayer({audio,fetchClip,onState:value=>states.push(value),url:{
    createObjectURL:()=>`blob:test-${++counter}`,revokeObjectURL:value=>revoked.push(value)}});
  return {player,audio,states,revoked};
}
const clip = start => ({blob:encodeWav(new Float32Array(16000)),startSeconds:start,durationSeconds:1});
test('only the latest playback request can install audio, and reset revokes it', async () => {
  let release, firstSignal;
  const {player,audio,revoked} = playerFixture((start,signal)=>start === 1
    ? new Promise(resolve=>{release=resolve;firstSignal=signal;}) : Promise.resolve(clip(start)));
  const first = player.play(1); await player.play(2);
  assert.equal(firstSignal.aborted,true);
  release(clip(1)); await first;
  assert.equal(player.clip.startSeconds,2);
  audio.currentTime = 0.5; assert.equal(player.positionSeconds,2.5);
  await player.play(3); assert.equal(revoked.length,1);
  player.reset(); assert.equal(revoked.length,2); assert.equal(player.clip,null);
  assert.equal(audio.src,undefined);
});
test('logout/reset during a pending fetch cannot play a late private result', async () => {
  let release;
  const {player,audio,states} = playerFixture(()=>new Promise(resolve=>{release=resolve;}));
  const waiting = player.play(0); player.reset(); release(clip(0)); await waiting;
  assert.equal(audio.src,undefined); assert.equal(states.at(-1).state,'idle');
});
test('autoplay denial leaves native controls available without discarding the clip', async () => {
  const {player,audio,states} = playerFixture(async()=>clip(0));
  audio.play = async()=>{throw new Error('user activation required');};
  await player.play(0); assert.equal(states.at(-1).manualPlay,true); assert.ok(audio.src);
});
test('transcript search is case-normalized and does not mutate raw text', () => {
  const segments = [{id:'a',text:'A Bank near the river'}, {id:'b',text:'강둑 설명'}];
  assert.equal(filterTranscript(segments,'ＢＡＮＫ')[0],segments[0]);
  assert.deepEqual(filterTranscript(segments,'강둑'),[segments[1]]);
  assert.equal(filterTranscript(segments,''),segments);
  assert.equal(segments[0].text,'A Bank near the river');
});
