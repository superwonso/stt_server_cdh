import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createTimetableCatalog, fillTimetableSelect, timetableSubjects} from '../web/timetable-subjects.js';

const response = (...subjects) => ({timetable:{classes:subjects.map(subject=>({subject,day:2,start:'13:00',end:'',room:''})),from:'2026-09-29',until:null}});
const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick = async () => {await Promise.resolve();await Promise.resolve();};

function harness(api) {
  let scope=JSON.stringify(['synthetic-owner','synthetic-token','https://synthetic.invalid']);
  const calls=[],changes=[];
  const catalog=createTimetableCatalog({scopeKey:()=>scope,onChange:state=>changes.push(state),api:(path,options)=>{
    calls.push({path,options});return api(path,options,calls.length);
  }});
  return {catalog,calls,changes,get scope(){return scope;},set scope(value){scope=value;}};
}

class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.value='';this.disabled=false;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  set innerHTML(_){assert.fail('Subject suggestions must never interpret HTML');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
}
const document={createElement:tag=>new Element(tag)};

test('extracts only bounded subject names, trims and deduplicates without altering input',()=>{
  const input=response('통계학',' 글로벌문화 ','통계학','글로벌문화');
  const before=structuredClone(input);
  assert.deepEqual(timetableSubjects(input),['글로벌문화','통계학']);
  assert.deepEqual(input,before);
  assert.deepEqual(timetableSubjects({timetable:null}),[]);
  assert.deepEqual(timetableSubjects(response()),[]);
  assert.equal(timetableSubjects(response(...Array(60).fill('통계학'))).length,1);
});

test('malformed or oversized response is rejected as a whole, with no partial choices',()=>{
  for(const input of [null,{},[],{timetable:undefined},{timetable:{}},{timetable:{classes:'bad'}},
    response(...Array(61).fill('통계학')),response(''),response('   '),response('가'.repeat(41)),
    {timetable:{classes:[null]}},{timetable:{classes:[{subject:1}]}},
    {timetable:{classes:[{subject:'정상'},{subject:false}]}}]){
    assert.throws(()=>timetableSubjects(input));
  }
});

test('loads the read-only timetable endpoint once and caches copied choices without writes',async()=>{
  const h=harness(()=>response('통계학','글로벌문화'));
  assert.deepEqual(h.catalog.snapshot(),{subjects:[],status:'idle'});
  const first=await h.catalog.load();assert.deepEqual(first,['글로벌문화','통계학']);
  first.push('caller mutation');
  const snapshot=h.catalog.snapshot();snapshot.subjects.push('snapshot mutation');
  const second=await h.catalog.load();assert.deepEqual(second,['글로벌문화','통계학']);
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].path,'/review/timetable');
  assert.equal(h.calls[0].options.method,undefined);assert.equal(h.calls[0].options.body,undefined);
  assert.ok(h.calls[0].options.signal instanceof AbortSignal);
  assert.deepEqual(h.changes.map(row=>row.status),['loading','ready']);
  h.changes[1].subjects.length=0;
  assert.deepEqual(h.catalog.snapshot().subjects,['글로벌문화','통계학']);
});

test('simultaneous consumers and force while pending share exactly one request',async()=>{
  const gate=deferred(),h=harness(()=>gate.promise);
  const first=h.catalog.load(),second=h.catalog.load(),third=h.catalog.load({force:true});
  assert.strictEqual(first,second);assert.strictEqual(second,third);
  await tick();assert.equal(h.calls.length,1);assert.equal(h.catalog.snapshot().status,'loading');
  gate.resolve(response('통계학'));
  assert.deepEqual(await Promise.all([first,second,third]),[['통계학'],['통계학'],['통계학']]);
  assert.equal(h.calls.length,1);
});

test('a force refresh replaces ready choices and never silently keeps removed subjects',async()=>{
  const h=harness((_path,_options,n)=>n===1?response('옛 과목'):response('새 과목'));
  await h.catalog.load();const refresh=h.catalog.load({force:true});
  assert.deepEqual(h.catalog.snapshot(),{subjects:[],status:'loading'});
  assert.deepEqual(await refresh,['새 과목']);assert.equal(h.calls.length,2);
  assert.deepEqual(h.catalog.snapshot(),{subjects:['새 과목'],status:'ready'});
});

test('no registered timetable is a ready empty result and is cached',async()=>{
  const h=harness(()=>({timetable:null}));
  assert.deepEqual(await h.catalog.load(),[]);assert.deepEqual(await h.catalog.load(),[]);
  assert.deepEqual(h.catalog.snapshot(),{subjects:[],status:'ready'});assert.equal(h.calls.length,1);
});

test('synchronous API throw settles unavailable and the next load genuinely retries',async()=>{
  const h=harness((_path,_options,n)=>{if(n===1)throw Error('synthetic transport error');return response('다시 읽은 과목');});
  assert.deepEqual(await h.catalog.load(),[]);assert.equal(h.catalog.snapshot().status,'unavailable');
  assert.deepEqual(await h.catalog.load(),['다시 읽은 과목']);assert.equal(h.calls.length,2);
  assert.deepEqual(h.changes.map(row=>row.status),['loading','unavailable','loading','ready']);
});

test('invalid response and asynchronous errors clear stale choices and remain retryable',async()=>{
  const h=harness((_path,_options,n)=>{
    if(n===1)return response('옛 과목');
    if(n===2)return response('정상',42);
    if(n===3)return Promise.reject(Error('synthetic failure body must not be exposed'));
    return response('복구한 과목');
  });
  await h.catalog.load();assert.deepEqual(await h.catalog.load({force:true}),[]);
  assert.deepEqual(h.catalog.snapshot(),{subjects:[],status:'unavailable'});
  assert.deepEqual(await h.catalog.load(),[]);assert.deepEqual(await h.catalog.load(),['복구한 과목']);
  assert.ok(!JSON.stringify(h.changes).includes('failure body'));
});

test('reset before request dispatch cancels the queued read without calling the API',async()=>{
  const h=harness(()=>assert.fail('A reset before dispatch must prevent the request'));
  const result=h.catalog.load();h.catalog.reset();
  assert.deepEqual(await result,[]);assert.equal(h.calls.length,0);
  assert.deepEqual(h.catalog.snapshot(),{subjects:[],status:'idle'});
});

test('reset aborts an in-flight read and its ignored late success cannot repopulate choices',async()=>{
  const old=deferred(),h=harness((_path,_options,n)=>n===1?old.promise:response('새 목록'));
  const first=h.catalog.load();await tick();h.catalog.reset();
  assert.equal(h.calls[0].options.signal.aborted,true);
  await h.catalog.load();old.resolve(response('지워진 옛 목록'));
  assert.deepEqual(await first,[]);assert.deepEqual(h.catalog.snapshot(),{subjects:['새 목록'],status:'ready'});
  assert.ok(!h.changes.some(row=>row.subjects.includes('지워진 옛 목록')));
});

test('owner, token, and server changes all hide the old cache and isolate late responses',async t=>{
  for(const [part,value] of [[0,'second-owner'],[1,'second-token'],[2,'https://second.invalid']]){
    await t.test(['owner','token','server'][part],async()=>{
      const old=deferred(),h=harness((_path,_options,n)=>n===1?old.promise:response('새 범위 과목'));
      const first=h.catalog.load();await tick();
      const identity=JSON.parse(h.scope);identity[part]=value;h.scope=JSON.stringify(identity);
      assert.deepEqual(h.catalog.snapshot(),{subjects:[],status:'idle'});
      assert.deepEqual(await h.catalog.load(),['새 범위 과목']);
      assert.equal(h.calls[0].options.signal.aborted,true);
      old.resolve(response('이전 범위 과목'));assert.deepEqual(await first,[]);
      assert.deepEqual(h.catalog.snapshot(),{subjects:['새 범위 과목'],status:'ready'});
      assert.ok(!h.changes.some(row=>row.subjects.includes('이전 범위 과목')));
    });
  }
});

test('a scope change during a read cannot expose its result even before the next load',async()=>{
  const gate=deferred(),h=harness(()=>gate.promise),result=h.catalog.load();await tick();
  h.scope='new-scope';gate.resolve(response('이전 계정 과목'));
  assert.deepEqual(await result,[]);assert.deepEqual(h.catalog.snapshot(),{subjects:[],status:'idle'});
  assert.deepEqual(h.changes.map(row=>row.status),['loading']);
});

test('an old rejected request cannot clear or settle the new in-flight request',async()=>{
  const old=deferred(),current=deferred(),h=harness((_path,_options,n)=>n===1?old.promise:current.promise);
  const first=h.catalog.load();await tick();h.scope='second-scope';
  const second=h.catalog.load();await tick();old.reject(Error('old failure'));
  assert.deepEqual(await first,[]);assert.equal(h.catalog.snapshot().status,'loading');
  assert.strictEqual(h.catalog.load(),second);
  current.resolve(response('현재 계정 과목'));assert.deepEqual(await second,['현재 계정 과목']);
  assert.equal(h.calls.length,2);assert.equal(h.catalog.snapshot().status,'ready');
});

test('select renders subject text literally, replaces old options, and never chooses a subject automatically',()=>{
  const select=new Element('select');select.value='old';select.append(new Element('script'));
  const html='<img src=x onerror=alert(1)>',values=timetableSubjects(response(html,'통계학'));
  fillTimetableSelect(select,document,{subjects:values,status:'ready'});
  assert.equal(select.value,'');assert.equal(select.disabled,false);assert.equal(select.children.length,3);
  assert.ok(select.children.every(node=>node.tagName==='option'));
  const option=select.children.find(node=>node.value===html);assert.equal(option.textContent,html);assert.equal(option.children.length,0);
  assert.equal(select.children[0].textContent,'시간표에서 과목 선택');
});

test('select uses distinct disabled loading, empty, and unavailable states without fake subjects',()=>{
  const expected={loading:'시간표 불러오는 중…',ready:'등록된 시간표 과목이 없어요',idle:'등록된 시간표 과목이 없어요',unavailable:'시간표를 불러오지 못했어요'};
  const select=new Element('select');
  for(const [status,label] of Object.entries(expected)){
    fillTimetableSelect(select,document,{subjects:[],status});
    assert.equal(select.disabled,true);assert.equal(select.value,'');assert.equal(select.children.length,1);
    assert.equal(select.children[0].value,'');assert.equal(select.children[0].textContent,label);
  }
});
