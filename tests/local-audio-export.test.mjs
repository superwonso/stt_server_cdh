import assert from 'node:assert/strict';
import {test} from 'node:test';
import {encodeWav} from '../web/audio.js';
import {buildLocalAudioExports,buildRecoverableLocalAudioExports,mergeLocalAudioExportParts,validateLocalWav,LocalAudioExportError} from '../web/local-audio-export.js';

const OWNER = 'synthetic-owner';
const CAPTURE = '10000000-0000-4000-8000-000000000000';
const OTHER_CAPTURE = '20000000-0000-4000-8000-000000000000';
function source(count) { return Float32Array.from({length:count},(_,i) => ((i % 200)-100)/200); }
function chunk(samples,start=0,end=samples.length,{captureId=CAPTURE,owner=OWNER,sequence=0,overlapSamples=0,id='test-chunk'}={}) {
  return {owner,captureId,id,sequence,startSamples:start,durationSamples:end-start,overlapSamples,
    blob:encodeWav(samples.slice(start,end))};
}
async function pcm(blob) { return new Uint8Array(await blob.slice(44).arrayBuffer()); }
async function assertAudio(part,samples,start,end) {
  assert.equal(part.startSamples,start); assert.equal(part.endSamples,end);
  assert.equal(part.durationSamples,end-start);
  assert.deepEqual(await pcm(part.blob),await pcm(encodeWav(samples.slice(start,end))));
  const header = new DataView(await part.blob.slice(0,44).arrayBuffer());
  assert.equal(header.getUint32(4,true),part.blob.size-8);
  assert.equal(header.getUint32(40,true),(end-start)*2);
}

test('validates canonical mono16k PCM header without reading a full Blob',async () => {
  const original = encodeWav(source(10000));
  const guarded = {size:original.size,arrayBuffer(){throw new Error('must not read whole audio');},
    slice(start,end){assert.equal(start,0); assert.equal(end,44); return original.slice(start,end);}};
  assert.deepEqual(await validateLocalWav(guarded,10000),{durationSamples:10000,byteLength:20044});
  assert.deepEqual(await validateLocalWav(original),{durationSamples:10000,byteLength:20044});
});

test('empty, zero-frame and malformed WAVs fail explicitly without partial success',async () => {
  for (const blob of [new Blob(),encodeWav(new Float32Array()),new Blob([new Uint8Array(100)])]) {
    await assert.rejects(validateLocalWav(blob),error => error.code === 'invalid_wav');
  }
  const valid = encodeWav(source(10));
  await assert.rejects(validateLocalWav(valid,11),error => error.code === 'invalid_wav');
  for (const offset of [0,4,8,12,16,20,22,24,28,32,34,36,40]) {
    const bytes = new Uint8Array(await valid.arrayBuffer()); bytes[offset] ^= 1;
    await assert.rejects(validateLocalWav(new Blob([bytes])),error => error.code === 'invalid_wav');
  }
  await assert.rejects(buildLocalAudioExports({owner:OWNER,chunks:[chunk(source(10)),{...chunk(source(10)),blob:new Blob()}]}),
    error => error.code === 'invalid_wav');
});

test('three-second overlapping chunks export one exact continuous waveform',async () => {
  const samples = source(16000*18);
  const chunks = [chunk(samples,0,16000*8),chunk(samples,16000*5,16000*13,{sequence:1,overlapSamples:48000}),
    chunk(samples,16000*10,16000*18,{sequence:2,overlapSamples:48000})];
  const result = await buildLocalAudioExports({owner:OWNER,chunks});
  assert.equal(result.groups.length,1); assert.equal(result.groups[0].parts.length,1);
  assert.equal(result.totalSamples,samples.length);
  assert.equal(result.totalBytes,44+samples.length*2);
  assert.deepEqual(result.groups[0].warnings,[]);
  await assertAudio(result.groups[0].parts[0],samples,0,samples.length);
});

test('unfinished newest PCM extends older snapshot and pending chunk without duplication',async () => {
  const samples = source(16000*10);
  const chunks = [chunk(samples,0,128000)];
  const snapshots = [chunk(samples,80000,144000,{sequence:1,overlapSamples:48000}),
    chunk(samples,80000,160000,{sequence:1,overlapSamples:48000})];
  const result = await buildLocalAudioExports({owner:OWNER,chunks,snapshots});
  assert.equal(result.inputCount,3);
  await assertAudio(result.groups[0].parts[0],samples,0,160000);
});

test('a pre-first-chunk PCM snapshot is exportable by itself',async () => {
  const samples = source(32123);
  const result = await buildLocalAudioExports({owner:OWNER,snapshots:[chunk(samples)]});
  await assertAudio(result.groups[0].parts[0],samples,0,samples.length);
});

test('overlap-only final guard and duplicate RAM/IDB rows are byte-proven duplicates',async () => {
  const samples = source(128000), original = chunk(samples);
  const duplicate = {...original,blob:original.blob.slice(0,original.blob.size,'audio/wav')};
  const result = await buildLocalAudioExports({owner:OWNER,chunks:[original,duplicate],
    snapshots:[chunk(samples,80000,128000,{sequence:1,overlapSamples:48000})]});
  assert.equal(result.totalSamples,128000);
  await assertAudio(result.groups[0].parts[0],samples,0,128000);
});

test('real repeated sounds at later absolute times are preserved',async () => {
  const samples = new Float32Array(1000).fill(0.1);
  const result = await buildLocalAudioExports({owner:OWNER,chunks:[chunk(samples,0,500),chunk(samples,500,1000,{sequence:1})]});
  await assertAudio(result.groups[0].parts[0],samples,0,1000);
});

test('missing prefix and gaps remain explicit separate files with no invented silence',async () => {
  const samples = source(300);
  const result = await buildLocalAudioExports({owner:OWNER,chunks:[chunk(samples,200,300,{sequence:8}),chunk(samples,50,100,{sequence:3})]});
  const group = result.groups[0];
  assert.equal(group.parts.length,2);
  assert.deepEqual(group.warnings,[{code:'missing_prefix',startSamples:0,endSamples:50},{code:'gap',startSamples:100,endSamples:200}]);
  assert.equal(result.totalSamples,150);
  await assertAudio(group.parts[0],samples,50,100); await assertAudio(group.parts[1],samples,200,300);
});

test('captures stay separate even if IDs, sequence and timestamps match',async () => {
  const first = source(100), second = new Float32Array(100).fill(0.25);
  const result = await buildLocalAudioExports({owner:OWNER,chunks:[chunk(first),chunk(second,0,100,{captureId:OTHER_CAPTURE})]});
  assert.equal(result.groups.length,2);
  await assertAudio(result.groups[0].parts[0],first,0,100);
  await assertAudio(result.groups[1].parts[0],second,0,100);
  assert.equal(result.totalSamples,200);
  assert.equal(JSON.stringify(result).includes(OWNER),false);
});

test('conflicting overlapping PCM and same-ID differing data fail rather than dropping either',async () => {
  const samples = source(100), altered = source(100); altered[75] = 0.75;
  for (const second of [chunk(altered),chunk(altered,50,100,{sequence:1,overlapSamples:50})]) {
    await assert.rejects(buildLocalAudioExports({owner:OWNER,chunks:[chunk(samples),second]}),
      error => error.code === 'overlap_conflict');
  }
});

test('overlap comparison crosses multiple pieces and stays below 64 KiB reads',async () => {
  const samples = source(120000);
  const guard = blob => ({size:blob.size,arrayBuffer(){throw new Error('whole read forbidden');},slice(start,end){
    if (end !== undefined && end-start <= 65536) return blob.slice(start,end);
    // Output Blob assembly slices can be large; their arrayBuffer must never be called.
    const slice = blob.slice(start,end); Object.defineProperty(slice,'arrayBuffer',{value(){throw new Error('large read');}});
    return slice;
  }});
  const chunks = [chunk(samples,0,40000),chunk(samples,40000,80000,{sequence:1}),chunk(samples,80000,120000,{sequence:2}),
    chunk(samples,10000,110000,{sequence:3,overlapSamples:0})].map(row => ({...row,blob:guard(row.blob)}));
  const result = await buildLocalAudioExports({owner:OWNER,chunks});
  await assertAudio(result.groups[0].parts[0],samples,0,120000);
});

test('foreign owner is rejected before any audio read; errors never reflect input',async () => {
  const malicious = 'private-owner-do-not-reflect';
  const original = chunk(source(10));
  original.blob = {size:64,arrayBuffer(){throw new Error('read forbidden');},slice(){throw new Error('read forbidden');}};
  await assert.rejects(buildLocalAudioExports({owner:OWNER,chunks:[original,{...original,owner:malicious}]}),error => {
    assert.equal(error.code,'owner_mismatch'); assert.equal(String(error).includes(malicious),false); return true;
  });
});

test('invalid metadata is not silently filtered or coerced',async () => {
  const valid = chunk(source(20));
  for (const patch of [{captureId:'private title'},{sequence:-1},{sequence:true},{startSamples:0.5},{durationSamples:0},
    {overlapSamples:21},{startSamples:Number.MAX_SAFE_INTEGER},{durationSamples:21}]) {
    await assert.rejects(buildLocalAudioExports({owner:OWNER,chunks:[{...valid,...patch}]}),LocalAudioExportError);
  }
  await assert.rejects(buildLocalAudioExports({owner:' invalid ',chunks:[valid]}),error => error.code === 'invalid_owner');
});

test('input/output size and per-capture limits fail without allocating huge buffers',async () => {
  const record = chunk(source(100));
  for (const options of [{maxInputBytes:200},{maxOutputBytes:200},{maxCaptureSamples:99},{maxInputBytes:Infinity}]) {
    await assert.rejects(buildLocalAudioExports({owner:OWNER,chunks:[record],...options}),error => error.code === 'export_limit');
  }
  const oversized = {...record,startSamples:16000*4*3600};
  await assert.rejects(buildLocalAudioExports({owner:OWNER,chunks:[oversized]}),error => error.code === 'export_limit');
});

test('source objects and state flags remain unchanged and input mutation after start is pinned',async () => {
  const samples = source(100), record = {...chunk(samples),state:'inflight',downloadRequested:false,attempts:2};
  const before = {...record};
  const job = buildLocalAudioExports({owner:OWNER,chunks:[record]});
  record.startSamples = 999;
  const result = await job;
  await assertAudio(result.groups[0].parts[0],samples,0,100);
  record.startSamples = before.startSamples;
  assert.deepEqual(record,before);
});

test('cancellation and storage read failures use fixed errors and never partial output',async () => {
  const controller = new AbortController(); controller.abort();
  await assert.rejects(buildLocalAudioExports({owner:OWNER,signal:controller.signal}),error => error.code === 'aborted');
  const broken = {...chunk(source(10)),blob:{size:64,arrayBuffer(){},slice(){throw new Error('secret disk detail');}}};
  await assert.rejects(buildLocalAudioExports({owner:OWNER,chunks:[broken]}),error => {
    assert.equal(error.code,'read_failed'); assert.equal(String(error).includes('secret'),false); return true;
  });
});

test('no remaining audio returns an explicit empty inventory',async () => {
  assert.deepEqual(await buildLocalAudioExports({owner:OWNER}),{groups:[],inputCount:0,totalBytes:0,totalSamples:0,warnings:[]});
});

test('recoverable export reports a zero-byte first input and corrupt middle header while preserving later parts',async () => {
  const samples = source(500);
  const empty = {...chunk(samples,0,100),blob:new Blob(),state:'inflight',downloadRequested:false};
  const corrupt = {...chunk(samples,200,300,{sequence:2}),blob:new Blob([new Uint8Array(244)])};
  const before = {...empty};
  const result = await buildRecoverableLocalAudioExports({owner:OWNER,chunks:[empty,chunk(samples,100,200,{sequence:1}),corrupt],
    snapshots:[chunk(samples,300,500,{sequence:3})]});
  assert.equal(result.inputCount,4);
  assert.deepEqual(result.warnings,[{code:'unreadable_records',count:2}]);
  assert.deepEqual(result.groups[0].warnings,[{code:'missing_prefix',startSamples:0,endSamples:100},
    {code:'gap',startSamples:200,endSamples:300}]);
  assert.equal(result.groups[0].parts.length,2);
  await assertAudio(result.groups[0].parts[0],samples,100,200);
  await assertAudio(result.groups[0].parts[1],samples,300,500);
  assert.deepEqual(empty,before); assert.equal(corrupt.blob.size,244);
});

test('all unreadable inputs produce explicit empty recovery with missing count, not false success',async () => {
  const empty = {...chunk(source(10)),blob:new Blob()};
  const unreadable = {...chunk(source(10)),blob:{size:64,arrayBuffer(){},slice(){throw new Error('private storage diagnostic');}}};
  const result = await buildRecoverableLocalAudioExports({owner:OWNER,chunks:[empty],snapshots:[unreadable]});
  assert.deepEqual(result,{groups:[],inputCount:2,totalBytes:0,totalSamples:0,warnings:[{code:'unreadable_records',count:2}]});
  assert.equal(JSON.stringify(result).includes('private'),false);
  assert.equal(empty.blob.size,0); assert.equal(unreadable.blob.size,64);
});

test('recovery validates every owner and metadata before deciding an unreadable file can be skipped',async () => {
  const good = chunk(source(10)), bad = {...good,blob:new Blob()};
  for (const [patch,code] of [[{owner:'foreign-owner'},'owner_mismatch'],[{sequence:-1},'invalid_record'],
    [{startSamples:0.5},'invalid_record'],[{durationSamples:0},'invalid_record'],[{overlapSamples:99},'invalid_record'],
    [{captureId:'private-title'},'invalid_record']]) {
    await assert.rejects(buildRecoverableLocalAudioExports({owner:OWNER,chunks:[good,{...bad,...patch}]}),error => error.code === code);
  }
});

test('recovery preserves all input/output/time caps and refuses readable overlap conflicts',async () => {
  const samples = source(100), good = chunk(samples), bad = {...good,blob:new Blob([new Uint8Array(244)])};
  await assert.rejects(buildRecoverableLocalAudioExports({owner:OWNER,chunks:[good,bad],maxInputBytes:300}),error => error.code === 'export_limit');
  await assert.rejects(buildRecoverableLocalAudioExports({owner:OWNER,chunks:[good,bad],maxOutputBytes:200}),error => error.code === 'export_limit');
  await assert.rejects(buildRecoverableLocalAudioExports({owner:OWNER,chunks:[{...bad,blob:new Blob()}],maxCaptureSamples:99}),
    error => error.code === 'export_limit');
  const changed = new Float32Array(samples); changed[50] = 0.8;
  await assert.rejects(buildRecoverableLocalAudioExports({owner:OWNER,chunks:[bad,good,chunk(changed)]}),
    error => error.code === 'overlap_conflict');
});

test('recoverable export retains cancellation and pins caller metadata before its first read',async () => {
  const samples = source(20), value = chunk(samples);
  const job = buildRecoverableLocalAudioExports({owner:OWNER,chunks:[value]});
  value.owner = 'changed-after-start'; value.startSamples = 100;
  const result = await job;
  await assertAudio(result.groups[0].parts[0],samples,0,20);
  const controller = new AbortController();
  const aborting = {...chunk(samples),blob:{size:84,arrayBuffer(){},slice(){
    controller.abort(); throw new Error('unreadable');
  }}};
  await assert.rejects(buildRecoverableLocalAudioExports({owner:OWNER,chunks:[aborting],signal:controller.signal}),
    error => error.code === 'aborted');
});

function assembledPart(samples,start=0,end=samples.length) {
  return {blob:encodeWav(samples.slice(start,end)),startSamples:start,endSamples:end,durationSamples:end-start};
}

test('merge keeps every part in time order, pads only internal gaps, and preserves exact PCM',async () => {
  const samples = source(400), first = assembledPart(samples,50,100), middle = assembledPart(samples,140,200),
    last = assembledPart(samples,210,400), parts = [last,first,middle], before = [...parts];
  const result = await mergeLocalAudioExportParts(parts);
  const expected = samples.slice(); expected.fill(0,100,140); expected.fill(0,200,210);
  assert.equal(result.gapSamples,50);
  assert.equal(result.blob.type,'audio/wav');
  await assertAudio(result,expected,50,400);
  assert.deepEqual(parts,before,'the input order and source parts stay unchanged');
  await assertAudio(first,samples,50,100); await assertAudio(last,samples,210,400);
});

test('merge adjacent parts and previously deduplicated overlaps never repeat boundary samples',async () => {
  const samples = source(600);
  const direct = await mergeLocalAudioExportParts([assembledPart(samples,0,200),assembledPart(samples,200,600)]);
  assert.equal(direct.gapSamples,0); await assertAudio(direct,samples,0,600);
  const built = await buildLocalAudioExports({owner:OWNER,chunks:[
    chunk(samples,0,200),chunk(samples,100,300,{sequence:1,overlapSamples:100}),
    chunk(samples,400,600,{sequence:2})]});
  const joined = await mergeLocalAudioExportParts(built.groups[0].parts);
  const expected = samples.slice(); expected.fill(0,300,400);
  assert.equal(joined.gapSamples,100); await assertAudio(joined,expected,0,600);
});

test('merge validates large assembled WAVs beyond the raw chunk cap and preserves a single Blob',async () => {
  const frames = 3 * 1024 * 1024;
  const bytes = new Uint8Array(await encodeWav(source(1)).slice(0,44).arrayBuffer());
  const view = new DataView(bytes.buffer); view.setUint32(4,36+frames*2,true); view.setUint32(40,frames*2,true);
  const zero = new Blob([new Uint8Array(65536)]);
  const blob = new Blob([bytes,...Array(frames*2/zero.size).fill(zero)],{type:'audio/wav'});
  const part = {blob,startSamples:11,endSamples:11+frames,durationSamples:frames};
  const joined = await mergeLocalAudioExportParts([part]);
  assert.equal(joined.blob,blob); assert.equal(joined.gapSamples,0);
  assert.equal(joined.startSamples,11); assert.equal(joined.endSamples,11+frames);
  assert.equal(joined.durationSamples,frames);
  await assert.rejects(validateLocalWav(blob),error=>error.code==='invalid_wav','raw chunks keep their 4 MiB cap');
});

test('merge only reads 44-byte headers and composes long silence without reading full audio',async () => {
  const samples = source(70000), reads = [];
  const guard = blob => ({size:blob.size,arrayBuffer(){throw new Error('whole audio read forbidden');},
    slice(start,end) {
      const slice = blob.slice(start,end);
      const read = slice.arrayBuffer.bind(slice);
      Object.defineProperty(slice,'arrayBuffer',{value(){
        reads.push([start,end]);
        assert.equal(start,0); assert.equal(end,44);
        return read();
      }});
      return slice;
    }});
  const first = assembledPart(samples,0,35000), last = assembledPart(samples,35000,70000);
  const gap = 16000*180+7;
  last.startSamples += gap; last.endSamples += gap;
  first.blob=guard(first.blob); last.blob=guard(last.blob);
  const joined = await mergeLocalAudioExportParts([first,last]);
  assert.deepEqual(reads,[[0,44],[0,44]]);
  assert.equal(joined.gapSamples,gap); assert.equal(joined.durationSamples,70000+gap);
  assert.equal(joined.blob.size,44+(70000+gap)*2);
  assert.deepEqual(new Uint8Array(await joined.blob.slice(44,44+70000).arrayBuffer()),await pcm(encodeWav(samples.slice(0,35000))));
  assert.deepEqual(new Uint8Array(await joined.blob.slice(44+70000,44+70000+65536).arrayBuffer()),new Uint8Array(65536));
  assert.deepEqual(new Uint8Array(await joined.blob.slice(44+(35000+gap)*2).arrayBuffer()),await pcm(encodeWav(samples.slice(35000))));
});

test('merge rejects empty, invalid ranges and overlaps before reading any source audio',async () => {
  const part = assembledPart(source(20));
  const unread = {...part,blob:{size:part.blob.size,arrayBuffer(){},slice(){throw new Error('should not read');}}};
  for (const parts of [null,{},[],[null],[{...part,startSamples:-1}],[{...part,endSamples:20.5}],
    [{...part,durationSamples:0}],[{...part,durationSamples:19}],[{...part,startSamples:true}],
    [{...part,endSamples:Number.MAX_SAFE_INTEGER+1}]]) {
    await assert.rejects(mergeLocalAudioExportParts(parts),error=>error.code==='invalid_record');
  }
  for (const second of [part,{...part,startSamples:10,endSamples:30}]) {
    await assert.rejects(mergeLocalAudioExportParts([unread,second]),error=>error.code==='overlap_conflict');
  }
});

test('merge enforces absolute timeline, part count and WAV size limits without huge allocations',async () => {
  const part = assembledPart(source(1)), cap = 16000*4*60*60;
  const last = await mergeLocalAudioExportParts([{...part,startSamples:cap-1,endSamples:cap}]);
  assert.equal(last.durationSamples,1); assert.equal(last.startSamples,cap-1);
  for (const parts of [[{...part,startSamples:cap,endSamples:cap+1}],Array(50001).fill(part)]) {
    await assert.rejects(mergeLocalAudioExportParts(parts),error=>error.code==='export_limit');
  }
  const huge = {...part,blob:{size:1024*1024*1024+2,arrayBuffer(){},slice(){throw new Error('must not read');}}};
  await assert.rejects(mergeLocalAudioExportParts([huge]),error=>error.code==='invalid_wav');
});

test('merge rejects malformed canonical headers and mismatched PCM lengths',async () => {
  const part = assembledPart(source(10));
  for (const offset of [0,4,8,12,16,20,22,24,28,32,34,36,40]) {
    const bytes = new Uint8Array(await part.blob.arrayBuffer()); bytes[offset] ^= 1;
    await assert.rejects(mergeLocalAudioExportParts([{...part,blob:new Blob([bytes])}]),error=>error.code==='invalid_wav');
  }
  for (const blob of [new Blob(),encodeWav(source(9)),new Blob([new Uint8Array(65)])]) {
    await assert.rejects(mergeLocalAudioExportParts([{...part,blob}]),error=>error.code==='invalid_wav');
  }
});

test('merge pins all caller metadata and Blob references before asynchronous validation',async () => {
  const samples = source(100), first = assembledPart(samples,10,40), second = assembledPart(samples,60,100);
  const originalFirst = first.blob, originalSecond = second.blob;
  const parts = [second,first], job = mergeLocalAudioExportParts(parts);
  first.startSamples=999; second.endSamples=1234; first.blob=new Blob(); parts.length=0;
  const joined = await job, expected = samples.slice(); expected.fill(0,40,60);
  await assertAudio(joined,expected,10,100); assert.equal(joined.gapSamples,20);
  assert.equal(originalFirst.size,104); assert.equal(originalSecond.size,124);
});

test('merge cancellation before and during header reads never returns partial output or private errors',async () => {
  const part = assembledPart(source(10)), controller = new AbortController(); controller.abort();
  await assert.rejects(mergeLocalAudioExportParts([part],{signal:controller.signal}),error=>error.code==='aborted');
  for (const readFails of [false,true]) {
    const pending = new AbortController();
    const blob = {size:part.blob.size,arrayBuffer(){},slice(start,end) {
      const slice = part.blob.slice(start,end);
      return {async arrayBuffer() {
        pending.abort();
        if(readFails)throw new Error('private storage detail');
        return slice.arrayBuffer();
      }};
    }};
    await assert.rejects(mergeLocalAudioExportParts([{...part,blob}],{signal:pending.signal}),error=>error.code==='aborted');
  }
  const broken = {...part,blob:{size:part.blob.size,arrayBuffer(){},slice(){throw new Error('private storage detail');}}};
  await assert.rejects(mergeLocalAudioExportParts([broken]),error=>{
    assert.equal(error.code,'read_failed'); assert.doesNotMatch(String(error),/private/); return true;
  });
});
