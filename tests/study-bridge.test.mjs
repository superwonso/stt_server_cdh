import assert from 'node:assert/strict';
import {test} from 'node:test';
import {webcrypto} from 'node:crypto';
import {setImmediate as turn, setTimeout as delay} from 'node:timers/promises';
import {performance} from 'node:perf_hooks';
import {readFile} from 'node:fs/promises';
import {createStudyWorkspace, studyScope, STUDY_CHANNEL} from '../web/study-bridge.js';

const ID = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const EPOCH = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const NEXT_EPOCH = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const ORIGIN = 'https://student.example';
const ROW = {chapter:'합성 단원',sub:'',number:1,question:'합성 문제',answer:'합성 답',type:'short',source_excerpt:'합성 자료',source_page:null};
const generate = extra => ({type:'generate',requestId:ID,text:'합성 자료',count:1,consent:true,...extra});
const result = id => ({request_id:id,status:'completed',questions:[ROW],warnings:['AI 초안입니다.']});
async function until(predicate, label = 'bridge response') {
  // WebCrypto runs on another thread; a fixed number of immediate turns can
  // finish before it does when the complete regression suite runs in parallel.
  const deadline = performance.now() + 5000;
  while (!predicate() && performance.now() < deadline) await delay(1);
  assert.ok(predicate(), `${label} did not settle`);
}

function harness(t) {
  const listeners = {}, docListeners = {}, messages = [], calls = [];
  let owner = 'alpha', key = 'alpha:token-one:server-one', hook = null, interactions = 0;
  const win = {location:{origin:ORIGIN},crypto:webcrypto,
    addEventListener(name,fn){listeners[name]=fn;}, removeEventListener(name){delete listeners[name];}};
  const document = {defaultView:win,visibilityState:'visible',
    addEventListener(name,fn){docListeners[name]=fn;}, removeEventListener(name){delete docListeners[name];},
    createElement(tag){return {tagName:tag,ownerDocument:this,style:{},attributes:{},
      setAttribute(name,value){this.attributes[name]=value;},
      contentWindow:tag==='iframe'?{postMessage(message,origin){messages.push({message,origin,frame:this});}}:undefined};}};
  const container = {ownerDocument:document,children:[],replaceChildren(...children){this.children=children;}};
  const api = async(path,options,timeout) => {
    const call = {path,options,timeout}; calls.push(call);
    if (hook) {const value = await hook(call); if (value !== undefined) return value;}
    if (path === '/study/capabilities') return {configured:true,generation_epoch:EPOCH};
    return result(JSON.parse(options.body).request_id);
  };
  const controller = createStudyWorkspace({container,api,scopeKey:()=>key,getOwner:()=>owner,onInteraction:()=>interactions++});
  t.after(()=>controller.destroy());
  const frame = () => container.children.find(child=>child.tagName==='iframe');
  const send = (data,extra={}) => listeners.message?.({origin:ORIGIN,source:frame()?.contentWindow,data:{channel:STUDY_CHANNEL,...data},...extra});
  return {controller,container,document,win,messages,calls,listeners,docListeners,frame,send,
    get interactions(){return interactions;},get owner(){return owner;},set owner(value){owner=value;},
    get key(){return key;},set key(value){key=value;},set hook(value){hook=value;}};
}

test('stable user scope is not coupled to session tokens or tunnel addresses', async()=>{
  const first = await studyScope('alpha',webcrypto);
  assert.match(first,/^[a-f0-9]{64}$/);
  assert.equal(await studyScope('alpha',webcrypto),first);
  assert.notEqual(await studyScope('beta',webcrypto),first);
  await assert.rejects(studyScope('',webcrypto));
});

test('opening creates only a local sandboxed iframe and sends no credentials or API calls', async t=>{
  const h = harness(t); await h.controller.open();
  assert.equal(h.frame().src,'./study/');
  assert.equal(h.frame().attributes.sandbox,'allow-scripts allow-same-origin allow-downloads allow-modals');
  assert.equal(h.frame().attributes.referrerpolicy,'no-referrer');
  h.send({type:'ready'});
  assert.deepEqual(Object.keys(h.messages[0].message).sort(),['active','channel','scope','type']);
  assert.equal(h.messages[0].origin,ORIGIN);
  assert.equal(h.messages[0].message.active,true);
  assert.equal(h.calls.length,0);
  assert.ok(!JSON.stringify(h.messages).includes('token-one'));
});

test('foreign origin, foreign window and wrong channel cannot generate or mark activity', async t=>{
  const h = harness(t); await h.controller.open();
  h.send(generate(),{origin:'https://foreign.example'});
  h.send(generate(),{source:{}});
  h.send(generate(),{data:{channel:'other',type:'generate'}});
  h.send({type:'interaction'},{source:{}});
  assert.equal(h.calls.length,0); assert.equal(h.interactions,0);
});

test('height is bounded and hidden contexts pause the child without removing it', async t=>{
  const h = harness(t); await h.controller.open(); const frame = h.frame();
  h.send({type:'height',height:12}); assert.equal(frame.style.height,'600px');
  h.send({type:'height',height:30000}); assert.equal(frame.style.height,'20000px');
  h.send({type:'height',height:NaN}); assert.equal(frame.style.height,'20000px');
  h.send({type:'interaction'}); assert.equal(h.interactions,1);
  h.controller.hide(); assert.equal(h.messages.at(-1).message.active,false);
  h.send({type:'interaction'}); assert.equal(h.interactions,1);
  await h.controller.open(); assert.equal(h.frame(),frame); assert.equal(h.messages.at(-1).message.active,true);
  h.document.visibilityState='hidden'; h.docListeners.visibilitychange(); assert.equal(h.messages.at(-1).message.active,false);
});

test('explicit consent and bounded valid input are required before any API call', async t=>{
  const h = harness(t); await h.controller.open();
  for (const extra of [{consent:false},{text:''},{text:'x'.repeat(24001)},{count:31},{count:0},{count:true}]) h.send(generate(extra));
  assert.equal(h.calls.length,0); assert.equal(h.messages.length,6);
  h.send(generate({requestId:'invalid'})); assert.equal(h.calls.length,0);
});

test('generation uses the existing API wrapper with one epoch and no exposed secrets', async t=>{
  const h = harness(t); await h.controller.open(); h.send(generate()); await until(()=>h.messages.length===1);
  assert.equal(h.calls.length,2); assert.equal(h.calls[0].path,'/study/capabilities');
  const call = h.calls[1]; assert.equal(call.path,'/study/generate'); assert.equal(call.timeout,180000);
  assert.deepEqual(JSON.parse(call.options.body),{request_id:ID,generation_epoch:EPOCH,text:'합성 자료',count:1,accepted_external_processing:true});
  const output = h.messages.at(-1).message;
  assert.equal(output.type,'generated'); assert.equal(output.requestId,ID); assert.deepEqual(output.questions,[ROW]);
  assert.equal(output.error,undefined); assert.equal(output.model,undefined);
  h.send(generate()); await until(()=>h.messages.length===2); assert.equal(h.calls.length,2,'completed receipt reused locally');
});

test('retry retains the old epoch even if the backend restarted; changed UUID input is rejected', async t=>{
  const h = harness(t); let posts = 0;
  h.hook = call => {
    if (call.path === '/study/generate' && posts++ === 0) throw Error('private provider diagnostic');
    if (call.path === '/study/capabilities') return {configured:true,generation_epoch:posts?NEXT_EPOCH:EPOCH};
  };
  await h.controller.open(); h.send(generate()); await until(()=>h.messages.length===1);
  assert.ok(!h.messages.at(-1).message.error.includes('private'));
  h.send(generate()); await until(()=>h.messages.length===2);
  assert.equal(h.calls.filter(call=>call.path==='/study/capabilities').length,1);
  for (const call of h.calls.filter(call=>call.path==='/study/generate')) assert.equal(JSON.parse(call.options.body).generation_epoch,EPOCH);
  const before = h.calls.length; h.send(generate({text:'changed'})); await until(()=>h.messages.length===3);
  assert.equal(h.calls.length,before); assert.match(h.messages.at(-1).message.error,/변경할 수 없어요/);
});

test('pending duplicates and competing requests do not create duplicate paid calls', async t=>{
  const h = harness(t); let finish;
  h.hook = call => call.path==='/study/generate'?new Promise(resolve=>{finish=resolve;}):undefined;
  await h.controller.open(); h.send(generate()); await until(()=>typeof finish==='function','pending API request');
  h.send(generate()); h.send(generate({requestId:OTHER})); await until(()=>h.messages.some(item=>item.message.requestId===OTHER));
  assert.equal(h.calls.length,2); finish(result(ID)); await until(()=>h.messages.at(-1)?.message.questions);
  assert.equal(h.messages.at(-1).message.questions.length,1);
});

test('a restart invalidates capabilities only for new UUIDs, never an old retry', async t=>{
  const h=harness(t); let restarted=false;
  h.hook=call=>{
    if(call.path==='/study/capabilities')return {configured:true,generation_epoch:restarted?NEXT_EPOCH:EPOCH};
    const data=JSON.parse(call.options.body);
    if(data.request_id===ID){restarted=true;throw Object.assign(Error('서버가 다시 시작되어 이전 요청을 확인할 수 없어요. 자동으로 재전송하지 않습니다.'),{status:409});}
    return result(data.request_id);
  };
  await h.controller.open();h.send(generate());await until(()=>h.messages.length===1);
  h.send(generate());await until(()=>h.messages.length===2);
  assert.equal(h.calls.filter(call=>call.path==='/study/capabilities').length,1);
  h.send(generate({requestId:OTHER}));await until(()=>h.messages.length===3);
  const posts=h.calls.filter(call=>call.path==='/study/generate').map(call=>JSON.parse(call.options.body));
  assert.deepEqual(posts.map(row=>row.generation_epoch),[EPOCH,EPOCH,NEXT_EPOCH]);
});

test('logout clears the iframe and aborts API work; late private results are ignored', async t=>{
  const h = harness(t); let finish;
  h.hook = call => call.path==='/study/generate'?new Promise(resolve=>{finish=resolve;}):undefined;
  await h.controller.open(); h.send(generate()); await until(()=>typeof finish==='function','pending API request');
  const signal = h.calls.at(-1).options.signal;
  h.controller.reset(); assert.equal(signal.aborted,true); assert.equal(h.container.children.length,0);
  const sent = h.messages.length; finish(result(ID)); await turn(); assert.equal(h.messages.length,sent);
  h.owner='beta'; h.key='beta:token-two:server-two'; await h.controller.open(); h.send({type:'ready'});
  assert.equal(h.messages.at(-1).message.scope,await studyScope('beta',webcrypto));
});

test('a changed session or server discards in-flight results before a new frame opens', async t=>{
  const h = harness(t); let finish;
  h.hook = call => call.path==='/study/generate'?new Promise(resolve=>{finish=resolve;}):undefined;
  await h.controller.open(); const oldFrame=h.frame(); h.send(generate()); await until(()=>typeof finish==='function','pending API request');
  const sent=h.messages.length; h.key='alpha:new-token:new-server'; finish(result(ID)); await turn();
  assert.equal(h.messages.length,sent);
  await h.controller.open(); assert.notEqual(h.frame(),oldFrame); h.send({type:'ready'});
  assert.equal(h.messages.at(-1).message.scope,await studyScope('alpha',webcrypto));
});

test('malformed responses are rejected and extra response fields do not reach the child', async t=>{
  const h=harness(t); await h.controller.open();
  h.hook=call=>call.path==='/study/generate'?{...result(ID),questions:[{...ROW,answer:''}]}:undefined;
  h.send(generate()); await until(()=>h.messages.length===1); assert.ok(h.messages.at(-1).message.error);
  h.hook=call=>call.path==='/study/generate'?{...result(ID),private:'hidden',questions:[{...ROW,credential:'hidden'}]}:undefined;
  h.send(generate()); await until(()=>h.messages.length===2); assert.equal(h.messages.at(-1).message.error,undefined);
  assert.ok(!JSON.stringify(h.messages.at(-1)).includes('hidden'));
});

test('authenticated third tab keeps the existing parent CSP restrictions', async()=>{
  const html=await readFile(new URL('../web/index.html',import.meta.url),'utf8');
  assert.match(html,/id="service-study"[^>]+aria-controls="study-panel"/);
  assert.match(html,/id="study-panel"[^>]+role="tabpanel"[^>]+hidden/);
  assert.match(html,/frame-src 'self';/); assert.match(html,/script-src 'self';/);
  assert.ok(!html.includes("frame-src https:"));
});
