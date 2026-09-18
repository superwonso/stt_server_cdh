import assert from 'node:assert/strict';
import {test} from 'node:test';
import {encodeWav} from '../web/audio.js';
import {IMPORT_PART_BYTES,recordingFileFingerprint} from '../web/file-import.js';
import {mergeLocalAudioExportParts} from '../web/local-audio-export.js';
import {HeldImportReceiptStore,verifyHeldImportCompletion,recheckHeldImportCompletion} from '../web/held-import-recovery.js';

const OWNER='synthetic-owner',SERVER='https://synthetic.example';
const CAPTURE='10000000-0000-4000-8000-000000000000';
const OTHER_CAPTURE='20000000-0000-4000-8000-000000000000';
const JOB='30000000-0000-4000-8000-000000000000';
const JOB2='30000000-0000-4000-8000-000000000001';
const LECTURE='40000000-0000-4000-8000-000000000000';
const name=(part,capture=CAPTURE)=>`보관음성_${capture}_${part.startSamples}-${part.endSamples}.wav`;
function parts() {
  return [part(10,50,.25),part(80,120,-.25)];
}
function part(start,end,sample=.25) {
  return {startSamples:start,endSamples:end,durationSamples:end-start,
    blob:encodeWav(new Float32Array(end-start).fill(sample))};
}
async function completed(part,id=JOB,patch={}) {
  const filename=name(part);
  const file={name:filename,size:part.blob.size,slice:(start,end)=>part.blob.slice(start,end)};
  return {id,lecture_id:LECTURE,filename,status:'completed',total_bytes:file.size,uploaded_bytes:file.size,
    next_offset:file.size,part_bytes:IMPORT_PART_BYTES,file_fingerprint:await recordingFileFingerprint(file),
    raw_deleted:false,cancel_requested:false,error:null,...patch};
}
function requests(list,states=new Map(list.map(row=>[row.id,row]))) {
  const calls=[];
  const request=async(path,options,timeout)=>{
    calls.push({path,signal:options?.signal,timeout});
    if(path==='/imports')return structuredClone(list);
    const state=states.get(path.slice('/imports/'.length));
    if(state instanceof Error)throw state;
    if(!state)throw Object.assign(new Error('synthetic missing import'),{status:404});
    return structuredClone(state);
  };
  return {request,calls};
}
function storage() {
  const values=new Map();
  return {values,getItem:key=>values.get(key) ?? null,setItem:(key,value)=>values.set(key,value)};
}

test('hints persist only scoped import IDs, survive reload and deduplicate updates',()=>{
  const disk=storage(),store=new HeldImportReceiptStore({storage:disk}),filename=name(part(0,10));
  assert.equal(store.remember(OWNER,SERVER,{id:JOB,filename,title:'do-not-store-title',token:'do-not-store-token',blob:'do-not-store-audio'}),true);
  assert.equal(store.remember(OWNER,SERVER+'/',{id:JOB,filename}),true);
  assert.deepEqual(new HeldImportReceiptStore({storage:disk}).ids(OWNER,SERVER,CAPTURE),[JOB]);
  assert.deepEqual(store.ids('other-owner',SERVER,CAPTURE),[]);
  assert.deepEqual(store.ids(OWNER,'https://other.example',CAPTURE),[]);
  assert.deepEqual(store.ids(OWNER,SERVER,OTHER_CAPTURE),[]);
  const raw=[...disk.values.values()][0];
  assert.doesNotMatch(raw,/do-not-store|filename|title|token|audio|fingerprint/);
  assert.deepEqual(Object.keys(JSON.parse(raw).ids[0]).sort(),['captureId','importId','owner','server']);
});

test('hints reject nonbridge names and unsafe scope; storage failures are optional',()=>{
  const disk=storage(),store=new HeldImportReceiptStore({storage:disk}),good={id:JOB,filename:name(part(0,10))};
  for(const filename of ['ordinary.wav',`보관음성_${CAPTURE}_10-1.wav`,`보관음성_${CAPTURE}_00-10.wav`,
    `보관음성_${CAPTURE}_0-230400001.wav`,`../${good.filename}`])assert.equal(store.remember(OWNER,SERVER,{...good,filename}),false);
  for(const server of ['javascript:alert(1)','https://user:pass@example.test','https://example.test/?token=x','https://example.test/path'])
    assert.equal(store.remember(OWNER,server,good),false);
  assert.equal(store.remember('',SERVER,good),false);
  assert.equal(store.remember(OWNER,SERVER,{...good,id:'../wrong'}),false);
  const broken=new HeldImportReceiptStore({storage:{getItem(){throw new Error('disabled');},setItem(){throw new Error('quota');}}});
  assert.equal(broken.remember(OWNER,SERVER,good),false);assert.deepEqual(broken.ids(OWNER,SERVER,CAPTURE),[]);
  assert.equal(new HeldImportReceiptStore({storage:null}).remember(OWNER,SERVER,good),false);
});

test('hints are bounded and malformed saved values never become completion evidence',()=>{
  const disk=storage(),store=new HeldImportReceiptStore({storage:disk});
  for(let i=0;i<1002;i++)store.remember(OWNER,SERVER,{id:`30000000-0000-4000-8000-${i.toString(16).padStart(12,'0')}`,filename:name(part(0,1))});
  const ids=store.ids(OWNER,SERVER,CAPTURE);assert.equal(ids.length,128);
  assert.equal(ids.includes(JOB),false);assert.equal(ids.at(-1),'30000000-0000-4000-8000-0000000003e9');
  const key=[...disk.values.keys()][0];
  for(const raw of ['not-json','x'.repeat(512*1024+1),JSON.stringify({version:2,ids:[]}),
    JSON.stringify({version:1,ids:[{owner:OWNER,server:SERVER,captureId:CAPTURE,importId:JOB,token:'untrusted'}]})]){
    disk.values.set(key,raw);assert.deepEqual(store.ids(OWNER,SERVER,CAPTURE),[]);
  }
});

test('every individual current WAV must have an independently fresh completed import',async()=>{
  const local=parts(),first=await completed(local[0]),second=await completed(local[1],JOB2);
  let transport=requests([first]);
  let result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request});
  assert.equal(result.complete,false);assert.equal(result.completedParts,1);assert.equal(result.partCount,2);
  assert.deepEqual(transport.calls.map(call=>call.path),['/imports',`/imports/${JOB}`]);
  transport=requests([second,first]);
  result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request});
  assert.equal(result.complete,true);assert.equal(result.completedParts,2);
  assert.deepEqual(result.imports.map(value=>value.id),[JOB,JOB2]);
  assert.deepEqual(Object.keys(result.imports[0]),['id','filename','total_bytes','file_fingerprint','lecture_id']);
});

test('merged WAV proves all retained parts including the exact silence and header bytes',async()=>{
  const local=parts(),merged=await mergeLocalAudioExportParts(local);
  const job=await completed(merged,JOB,{cancel_requested:true,error:'',raw_deleted:false});
  const transport=requests([job]);
  const result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request});
  assert.equal(result.complete,true);assert.equal(result.completedParts,2);assert.equal(result.imports.length,1);
  const wrong={...job,file_fingerprint:'0'.repeat(64)};
  assert.equal((await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:requests([wrong]).request})).complete,false);
});

test('known IDs recover jobs older than the latest twenty; stale list and duplicate hints are not authority',async()=>{
  const local=[part(0,100)],job=await completed(local[0]),transport=requests([],new Map([[JOB,job]]));
  const result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request,knownIds:[JOB,JOB,'invalid']});
  assert.equal(result.complete,true);assert.equal(transport.calls.length,2);
  const stale=requests([job],new Map([[JOB,{...job,status:'processing'}]]));
  assert.equal((await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:stale.request})).complete,false);
});

test('strict completed geometry and identifiers reject incomplete or inconsistent server states',async()=>{
  const local=[part(0,50)],job=await completed(local[0]);
  const patches=[{status:'processing'},{status:'failed'},{lecture_id:null},{lecture_id:'invalid'},
    {uploaded_bytes:job.total_bytes-1},{next_offset:job.total_bytes-1},{part_bytes:1024},
    {total_bytes:job.total_bytes+2},{total_bytes:'144'},{raw_deleted:0},{cancel_requested:1},{error:'failed'},
    {file_fingerprint:'wrong'},{filename:name(local[0],OTHER_CAPTURE)}];
  for(const patch of patches){
    const state={...job,...patch},transport=requests([state],new Map([[JOB,state]]));
    const result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request,knownIds:[JOB]});
    assert.equal(result.complete,false,JSON.stringify(patch));
  }
});

test('404 excludes a candidate but authentication and network failures abort verification',async()=>{
  const local=[part(0,20)],job=await completed(local[0]);
  assert.equal((await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:requests([job],new Map()).request})).complete,false);
  for(const status of [401,403,500]){
    const error=Object.assign(new Error('synthetic request failure'),{status});
    const transport=requests([job],new Map([[JOB,error]]));
    await assert.rejects(verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request}),value=>value===error);
  }
  const network=new TypeError('synthetic network failure');
  await assert.rejects(verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:async()=>{throw network;}}),value=>value===network);
  const wrong=requests([job],new Map([[JOB,{...job,id:JOB2}]]));
  await assert.rejects(verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:wrong.request}));
});

test('matching name and size never hides a changed waveform or a missing part',async()=>{
  const local=parts(),job=await completed(local[0]),duplicate=await completed(local[0],JOB2);
  const changed=[part(10,50,.75),local[1]];
  assert.equal((await verifyHeldImportCompletion({captureId:CAPTURE,parts:changed,request:requests([job]).request})).completedParts,0);
  const repeated=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:requests([job,duplicate]).request});
  assert.equal(repeated.completedParts,1);assert.equal(repeated.complete,false);
  assert.equal(repeated.imports.length,1);
});

test('parts are validated and pinned before requests; caller mutations cannot swap the source',async()=>{
  const local=parts(),job=await completed(await mergeLocalAudioExportParts(local)),transport=requests([job]);
  const operation=verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request});
  local[0].startSamples=999;local[1].blob=new Blob();local.length=0;
  assert.equal((await operation).complete,true);
  let calls=0;
  for(const invalid of [[],[part(0,10),part(5,15)],[{...part(0,10),blob:new Blob()}]]){
    await assert.rejects(verifyHeldImportCompletion({captureId:CAPTURE,parts:invalid,request:async()=>{calls++;return [];}}));
  }
  assert.equal(calls,0);
});

test('abort propagates to authenticated requests and rejects late success',async()=>{
  const local=[part(0,10)],job=await completed(local[0]),controller=new AbortController(),calls=[];
  const request=async(path,options)=>{
    calls.push(path);assert.equal(options.signal,controller.signal);
    if(path==='/imports')return [job];
    controller.abort();return job;
  };
  await assert.rejects(verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request,signal:controller.signal}),error=>error.name==='AbortError');
  assert.equal(calls.length,2);
  let issued=false;
  await assert.rejects(verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:async()=>{issued=true;},signal:controller.signal}),
    error=>error.name==='AbortError');
  assert.equal(issued,false);
});

test('abort during bounded fingerprint reads cannot produce a completion receipt',async()=>{
  const original=part(0,500),job=await completed(original),controller=new AbortController();
  const blob={size:original.blob.size,arrayBuffer(){throw new Error('whole read');},slice(start,end){
    const slice=original.blob.slice(start,end);
    return {async arrayBuffer(){if(end!==44)controller.abort();return slice.arrayBuffer();}};
  }};
  await assert.rejects(verifyHeldImportCompletion({captureId:CAPTURE,parts:[{...original,blob}],request:requests([job]).request,
    signal:controller.signal}),error=>error.name==='AbortError');
});

test('final recheck requires fresh exact receipts and accepts completed cleanup or cancel races',async()=>{
  const local=[part(0,50)],job=await completed(local[0]),first=requests([job]);
  const result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:first.request});
  const newer=requests([] ,new Map([[JOB,{...job,raw_deleted:true,cancel_requested:true,error:''}]]));
  assert.equal(await recheckHeldImportCompletion(result.imports,newer.request),true);
  assert.deepEqual(newer.calls.map(call=>call.path),[`/imports/${JOB}`]);
  for(const patch of [{status:'cancelled'},{lecture_id:JOB2},{file_fingerprint:'0'.repeat(64)},
    {total_bytes:job.total_bytes+2},{filename:name(part(1,51))}]){
    await assert.rejects(recheckHeldImportCompletion(result.imports,requests([],new Map([[JOB,{...job,...patch}]])).request));
  }
  await assert.rejects(recheckHeldImportCompletion(result.imports,requests([],new Map()).request),error=>error.status===404);
  for(const invalid of [[],[...result.imports,...result.imports],[{...result.imports[0],status:'completed'}]]){
    await assert.rejects(recheckHeldImportCompletion(invalid,newer.request));
  }
});

test('final recheck pins input receipt metadata and rejects an aborted response',async()=>{
  const local=[part(0,10)],job=await completed(local[0]);
  const result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:requests([job]).request});
  const operation=recheckHeldImportCompletion(result.imports,requests([],new Map([[JOB,job]])).request);
  result.imports[0].file_fingerprint='0'.repeat(64);
  assert.equal(await operation,true);
  const controller=new AbortController();
  const fresh=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:requests([job]).request});
  await assert.rejects(recheckHeldImportCompletion(fresh.imports,async(path,options)=>{
    assert.equal(options.signal,controller.signal);controller.abort();return job;
  },{signal:controller.signal}),error=>error.name==='AbortError');
});

test('recent complete coverage avoids older failing hints and merged candidate is preferred',async()=>{
  const local=parts(),first=await completed(local[0]),second=await completed(local[1],JOB2);
  const old='30000000-0000-4000-8000-000000000009';
  const broken=Object.assign(new Error('old synthetic server failure'),{status:500});
  let transport=requests([first,second],new Map([[JOB,first],[JOB2,second],[old,broken]]));
  let result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request,knownIds:[old]});
  assert.equal(result.complete,true);assert.equal(transport.calls.some(call=>call.path.endsWith(old)),false);
  const merged=await completed(await mergeLocalAudioExportParts(local),old);
  transport=requests([first,second,merged],new Map([[JOB,broken],[JOB2,broken],[old,merged]]));
  result=await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request,knownIds:[JOB,JOB2]});
  assert.equal(result.complete,true);assert.equal(result.imports.length,1);
  assert.deepEqual(transport.calls.map(call=>call.path),['/imports',`/imports/${old}`]);
});

test('old optional hints are queried newest first and stop as soon as all audio is proven',async()=>{
  const local=[part(0,10)],job=await completed(local[0],JOB2);
  const transport=requests([],new Map([[JOB,new Error('obsolete hint')],[JOB2,job]]));
  assert.equal((await verifyHeldImportCompletion({captureId:CAPTURE,parts:local,request:transport.request,knownIds:[JOB,JOB2]})).complete,true);
  assert.deepEqual(transport.calls.map(call=>call.path),['/imports',`/imports/${JOB2}`]);
});
