import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import {normalizeRescueOrigin,validateRescueConfig,readRescueJson,RescueController,mountRescuePage} from '../web/rescue.js';
import {LocalAudioExportError} from '../web/local-audio-export.js';

const PAGE = 'https://student.github.io/classroom/rescue.html';
const ORIGIN = 'https://synthetic-rescue.trycloudflare.com';
const NOW = Date.parse('2026-09-09T00:00:00Z');
const DAY = 86400000;
const USER = 'member.beta';
const TOKEN = 'synthetic-session-token';
const response = (value,status=200) => new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const config = () => ({version:1,state:'online',apiUrl:ORIGIN,publishedAt:'2026-09-08T23:00:00Z',expiresAt:'2026-09-09T23:00:00Z'});
const deferred = () => {let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const tick = () => new Promise(resolve=>setImmediate(resolve));
const exportsResult = () => ({groups:[],inputCount:0,totalBytes:0,totalSamples:0,warnings:[]});

function fixture(overrides={}) {
  let clock=NOW, timerId=0;
  const calls=[],reads=[],builds=[],views=[],timers=new Map();
  const defaults={pageUrl:PAGE,now:()=>clock,setTimer:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimer:id=>timers.delete(id),
    fetch:async(url,options)=>{
      calls.push({url,options}); const path=new URL(url).pathname;
      if(path.endsWith('/config.json')) return response(config());
      if(path==='/health') return response({status:'ok'});
      if(path==='/auth/login') return response({token:TOKEN,user:{username:JSON.parse(options.body).username},session_expires_at:(clock+3600000)/1000});
      if(path==='/auth/me') return response({username:USER,session_expires_at:(clock+3600000)/1000});
      throw new Error('Unexpected network call');
    },readSnapshot:async owner=>{reads.push(owner);return {owner,sessions:[],chunks:[],snapshots:[]};},
    buildExport:async data=>{builds.push(data);return exportsResult();},onChange:view=>views.push(view)};
  const options={...defaults,...overrides},controller=new RescueController(options);
  return {controller,options,calls,reads,builds,views,timers,setNow:value=>{clock=value;}};
}

test('rescue accepts only approved HTTPS tunnel origins or loopback from a loopback page',()=>{
  assert.equal(normalizeRescueOrigin(ORIGIN,PAGE),ORIGIN);
  assert.equal(normalizeRescueOrigin('http://127.0.0.1:43210','http://127.0.0.1:1234/rescue.html'),'http://127.0.0.1:43210');
  for(const value of ['http://synthetic-rescue.trycloudflare.com','https://other.example',
    'https://synthetic-rescue.trycloudflare.com.evil.invalid',`${ORIGIN}/path`,`${ORIGIN}?key=secret`,
    `${ORIGIN}#token`,`${ORIGIN}:4433`,'https://name:secret@synthetic-rescue.trycloudflare.com',
    'http://127.0.0.1:8765','javascript:alert(1)']) assert.throws(()=>normalizeRescueOrigin(value,PAGE),value);
});

test('automatic rescue address follows strict canonical timestamps and the existing 24-hour lease',()=>{
  assert.deepEqual(validateRescueConfig(config(),PAGE,NOW),{origin:ORIGIN,expiresAt:NOW+23*3600000});
  for(const mutate of [value=>{value.version=2;},value=>{value.extra='ignored';},value=>{value.apiUrl+='/'},
    value=>{value.expiresAt='2026-09-09T22:00:00Z'},value=>{value.publishedAt='2026-09-09T01:00:00Z'},
    value=>{value.state='offline';},value=>{value.publishedAt='2026-02-30T00:00:00Z'},value=>{value.expiresAt='2026-09-08T23:00:00Z'}]) {
    const value=config();mutate(value);assert.throws(()=>validateRescueConfig(value,PAGE,NOW));
  }
});

test('JSON replies are bounded, reject redirection, and reject malformed content',async()=>{
  assert.deepEqual(await readRescueJson(response({status:'ok'})),{status:'ok'});
  await assert.rejects(readRescueJson(new Response('x'.repeat(32769))));
  await assert.rejects(readRescueJson(new Response('{}',{headers:{'Content-Length':'9999999'}})));
  await assert.rejects(readRescueJson(new Response('{invalid')));
  const redirected=response({});Object.defineProperty(redirected,'redirected',{value:true});
  await assert.rejects(readRescueJson(redirected));
});

test('page initialization discovers anonymously and never scans or restores a locally saved identity',async()=>{
  const f=fixture();await f.controller.connect();
  assert.equal(f.controller.view.connected,true);assert.deepEqual(f.reads,[]);
  assert.deepEqual(f.calls.map(call=>new URL(call.url).pathname),['/classroom/config.json','/health']);
  for(const call of f.calls){assert.equal(call.options.headers.Authorization,undefined);assert.equal(call.options.redirect,'error');
    assert.equal(call.options.credentials,'omit');assert.equal(call.options.referrerPolicy,'no-referrer');assert.equal(call.options.cache,'no-store');}
});

test('default browser fetch and timers retain their Window receiver instead of the controller receiver',async()=>{
  const originals={fetch:globalThis.fetch,setTimeout:globalThis.setTimeout,clearTimeout:globalThis.clearTimeout};
  const requests=[];
  try {
    globalThis.fetch=async function(url){assert.equal(this,globalThis);requests.push(url);return response(new URL(url).pathname==='/health'?{status:'ok'}:config());};
    globalThis.setTimeout=function(){assert.equal(this,globalThis);return 1;};
    globalThis.clearTimeout=function(){assert.equal(this,globalThis);};
    const controller=new RescueController({pageUrl:PAGE,now:()=>NOW});await controller.connect();
    assert.equal(controller.view.connected,true);assert.equal(requests.length,2);
  } finally { Object.assign(globalThis,originals); }
});

test('explicit login checks /auth/me before one exact-owner snapshot and export, without any upload or queue mutation',async()=>{
  const f=fixture();await f.controller.connect();await f.controller.login(USER,'synthetic password');
  assert.deepEqual(f.calls.map(call=>new URL(call.url).pathname),['/classroom/config.json','/health','/auth/login','/auth/me']);
  assert.equal(f.calls[2].options.method,'POST');assert.equal(f.calls[2].options.headers.Authorization,undefined);
  assert.deepEqual(JSON.parse(f.calls[2].options.body),{username:USER,password:'synthetic password'});
  assert.equal(f.calls[3].options.headers.Authorization,`Bearer ${TOKEN}`);
  assert.deepEqual(f.reads,[USER]);assert.equal(f.builds[0].owner,USER);assert.ok(f.builds[0].signal);
  assert.equal(f.controller.view.username,USER);assert.equal(f.controller.view.result.inputCount,0);
});

test('an unavailable server, invalid input or mismatched authenticated account never reaches IndexedDB',async()=>{
  const f=fixture();await f.controller.login(USER,'password');assert.equal(f.calls.length,0);
  await f.controller.connect();await f.controller.login('Invalid.Name','password');assert.equal(f.calls.length,2);
  await f.controller.login(USER,'');assert.equal(f.calls.length,2);
  const g=fixture({fetch:async url=>response(new URL(url).pathname==='/health'?{status:'ok'}:
    new URL(url).pathname==='/auth/login'?{token:TOKEN,user:{username:USER}}:{username:'other-member',session_expires_at:(NOW+3600000)/1000})});
  await g.controller.connect(ORIGIN);await g.controller.login(USER,'password');assert.deepEqual(g.reads,[]);assert.equal(g.controller.auth,null);
});

test('login failure and network ambiguity never automatically retry or echo server diagnostics',async()=>{
  const f=fixture();await f.controller.connect();
  f.controller.fetcher=async()=>response({detail:'server-private-key-and-diagnostic'},503);
  await f.controller.login(USER,'sensitive-password');assert.deepEqual(f.reads,[]);
  assert.doesNotMatch(JSON.stringify(f.controller.view),/server-private-key|sensitive-password/);
  assert.equal(f.controller.auth,null);
});

test('rescanning revalidates the same session and a revoked session cannot read audio',async()=>{
  const f=fixture();await f.controller.connect();await f.controller.login(USER,'password');
  await f.controller.scan();assert.deepEqual(f.reads,[USER,USER]);
  assert.equal(new URL(f.calls.at(-1).url).pathname,'/auth/me');
  f.controller.fetcher=async()=>response({detail:'private'},401);await f.controller.scan();
  assert.equal(f.reads.length,2);assert.equal(f.controller.view.result,null);assert.equal(f.controller.auth,null);
});

test('locking while login is delayed discards the late token and never scans',async()=>{
  const f=fixture(),gate=deferred();await f.controller.connect();
  f.controller.fetcher=()=>gate.promise;const pending=f.controller.login(USER,'password');f.controller.lock();
  gate.resolve(response({token:TOKEN,user:{username:USER}}));await pending;
  assert.deepEqual(f.reads,[]);assert.equal(f.controller.auth,null);assert.equal(f.controller.view.result,null);
});

test('a new origin while snapshot reading is delayed discards its audio and identity',async()=>{
  const gate=deferred(),f=fixture({readSnapshot:()=>gate.promise});await f.controller.connect();
  const pending=f.controller.login(USER,'password');await tick();
  assert.ok(f.controller.auth);await f.controller.connect('https://replacement-rescue.trycloudflare.com');
  gate.resolve({owner:USER,chunks:[],snapshots:[]});await pending;
  assert.equal(f.controller.auth,null);assert.equal(f.builds.length,0);assert.equal(f.controller.view.result,null);
});

test('locking during output assembly aborts the builder and discards all late files',async()=>{
  const gate=deferred();let signal;
  const f=fixture({buildExport:data=>{signal=data.signal;return gate.promise;}});
  await f.controller.connect();const pending=f.controller.login(USER,'password');await tick();
  assert.ok(signal);f.controller.lock();assert.equal(signal.aborted,true);
  gate.resolve(exportsResult());await pending;assert.equal(f.controller.view.result,null);assert.equal(f.controller.view.username,'');
});

test('expiry clears prepared files and prevents reads even if a timer was throttled',async()=>{
  const f=fixture();await f.controller.connect();await f.controller.login(USER,'password');
  assert.ok(f.controller.view.result);f.setNow(NOW+3600001);await f.controller.scan();
  assert.equal(f.controller.auth,null);assert.equal(f.controller.view.result,null);assert.equal(f.reads.length,1);
});

test('wrong-owner snapshots and arbitrary storage error messages are rejected without reflection',async()=>{
  for(const readSnapshot of [async()=>({owner:'other-member',chunks:[],snapshots:[]}),async()=>{throw new Error('서버 private username and path');}]) {
    const f=fixture({readSnapshot});await f.controller.connect();await f.controller.login(USER,'password');
    assert.equal(f.builds.length,0);assert.equal(f.controller.view.result,null);assert.equal(f.controller.auth,null);
    assert.doesNotMatch(f.controller.view.error,/private username|path/);
  }
});

test('missing existing storage is reported without pretending the old tab RAM is empty',async()=>{
  const f=fixture({readSnapshot:async()=>{throw Object.assign(new Error('private'),{code:'live_queue_not_found'});}});
  await f.controller.connect();await f.controller.login(USER,'password');
  assert.match(f.controller.view.error,/RAM/);assert.equal(f.controller.view.result,null);
});

test('known export conflicts have a fixed explanation and never echo modified exception text',async()=>{
  const error=new LocalAudioExportError('overlap_conflict');error.message='private diagnostic';
  const f=fixture({buildExport:async()=>{throw error;}});await f.controller.connect();await f.controller.login(USER,'password');
  assert.match(f.controller.view.error,/같은 시간 위치/);assert.doesNotMatch(f.controller.view.error,/private/);
});

function domFixture(){
  const elements=new Map();
  const node=()=>({value:'',textContent:'',hidden:false,disabled:false,children:[],append(...values){this.children.push(...values);},replaceChildren(...values){this.children=[...values];}});
  const document={getElementById:id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);},createElement:node};
  const created=[],revoked=[],events=new Map(),history=[];
  const window={location:{href:PAGE+'?username=ignored#token=ignored',pathname:'/classroom/rescue.html',search:'?username=ignored',hash:'#token=ignored'},
    URL:{createObjectURL:blob=>{const url=`blob:synthetic-${created.length}`;created.push({url,blob});return url;},revokeObjectURL:url=>revoked.push(url)},
    history:{replaceState:(_state,_title,path)=>history.push(path)},addEventListener:(event,fn)=>events.set(event,fn)};
  return {document,window,elements,created,revoked,events,history};
}

test('rescue DOM makes explicit anonymous WAV links and revokes them on locking without touching the original tab',async()=>{
  const blob=new Blob(['synthetic-wav'],{type:'audio/wav'}),result={...exportsResult(),groups:[{captureId:'private-id',parts:[{blob,startSamples:16000,endSamples:32000,durationSamples:16000}],warnings:[]}]};
  const f=fixture({buildExport:async()=>result}),dom=domFixture();
  const controller=mountRescuePage(dom.document,dom.window,f.options);await tick();
  assert.deepEqual(dom.history,['/classroom/rescue.html']);assert.equal(dom.created.length,0);
  dom.document.getElementById('rescue-password').value='synthetic password';
  dom.document.getElementById('rescue-username').value=USER;
  dom.document.getElementById('rescue-login-form').onsubmit({preventDefault(){}});await tick();
  assert.equal(dom.document.getElementById('rescue-password').value,'');assert.equal(dom.created.length,1);
  const link=dom.document.getElementById('rescue-files').children[0].children[0];
  assert.equal(link.download,'local-audio-001-part-001.wav');assert.doesNotMatch(link.download,/member|private/);
  assert.match(link.textContent,/1\.0–2\.0초/);assert.match(dom.document.getElementById('rescue-summary').textContent,/전체 수업.*아니라/);
  assert.equal(f.calls.filter(call=>call.options.method==='POST').length,1);
  let prevented=false;link.onclick({preventDefault(){prevented=true;}});assert.equal(prevented,false);
  f.setNow(NOW+3600001);link.onclick({preventDefault(){prevented=true;}});assert.equal(prevented,true);
  assert.equal(controller.auth,null,'download gesture enforces expiry even before the timer runs');
  controller.lock();assert.deepEqual(dom.revoked,['blob:synthetic-0']);assert.equal(dom.document.getElementById('rescue-files').children.length,0);
  assert.equal(dom.document.getElementById('rescue-owner').textContent,'');dom.events.get('pagehide')();
});

test('rescue explicitly counts unreadable records while retaining downloadable good parts',async()=>{
  const result={...exportsResult(),inputCount:3,warnings:[{code:'unreadable_records',count:2}],groups:[{
    captureId:'private-id',parts:[{blob:new Blob(['wav']),startSamples:100,endSamples:200,durationSamples:100}],
    warnings:[{code:'missing_prefix',startSamples:0,endSamples:100}]}]};
  const f=fixture({buildExport:async()=>result}),dom=domFixture(),controller=mountRescuePage(dom.document,dom.window,f.options);
  await tick();await controller.login(USER,'password');
  assert.equal(dom.created.length,1);assert.match(dom.document.getElementById('rescue-warnings').textContent,/2개 음성.*제외/);
  assert.match(dom.document.getElementById('rescue-warnings').textContent,/빠진 음성/);
  assert.match(dom.document.getElementById('rescue-warnings').textContent,/빈 구간이 1곳/);
  controller.lock();
});

test('standalone assets do not load the app uploader, persist credentials, accept URL credentials, or expose external resources',async()=>{
  const [html,script]=await Promise.all([readFile(new URL('../web/rescue.html',import.meta.url),'utf8'),readFile(new URL('../web/rescue.js',import.meta.url),'utf8')]);
  assert.match(html,/src="\.\/rescue\.js"/);assert.doesNotMatch(html,/src="\.\/app\.js"/);
  assert.match(html,/원래 탭을 새로고침하거나 닫지 말고/);assert.match(html,/메모리에만 남은 음성은 여기서 가져올 수 없습니다/);
  assert.match(html,/<p class="rescue-limits">[^]*?메모리\(RAM\)[^]*?<\/p>/,'critical RAM-only limits stay outside the mobile-hidden privacy card');
  assert.match(script,/new DurableLiveQueue\(\{readOnlyExisting:true\}\)/);
  assert.doesNotMatch(script,/(?:localStorage|sessionStorage|auth-session|ackChunk|enqueueChunk|promoteSnapshot|setDownloadRequested|runUploader|MicrophoneCapture|\/presence|\/auth\/reset-password|\/auth\/logout)/);
  assert.match(html,/form-action 'none'/);assert.match(html,/no-referrer/);
});
