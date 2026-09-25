import assert from 'node:assert/strict';
import {test} from 'node:test';
import {setImmediate as turn} from 'node:timers/promises';
import {createCourseWorkspace,localDateTime,validCoursePage} from '../web/course-workspace.js';

const COURSE='11111111-1111-4111-8111-111111111111',OTHER='22222222-2222-4222-8222-222222222222';
const LECTURE='33333333-3333-4333-8333-333333333333',SECOND='44444444-4444-4444-8444-444444444444';
const REVIEW='55555555-5555-4555-8555-555555555555';
const clone=value=>structuredClone(value);
class Element {
  constructor(tag,document){this.tagName=tag;this.ownerDocument=document;this.children=[];this.attributes={};this.listeners={};this.dataset={};this._text='';this.value='';this.disabled=false;this.hidden=false;this.checked=false;this.open=false;}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(node=>node.textContent).join('');}
  set innerHTML(_){throw Error('Untrusted HTML must remain inert text');}
  append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
  replaceChildren(...children){this._text='';this.children=[];this.append(...children);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  async event(name){const event={target:this,preventDefault(){this.prevented=true;}};await this['on'+name]?.(event);for(const fn of this.listeners[name]||[])await fn(event);return event;}
  click(){if(!this.disabled)return this.event('click');}
}
const all=(node,predicate)=>[node,...node.children.flatMap(child=>all(child,predicate))].filter(predicate);
const byText=(root,tag,text)=>all(root,node=>node.tagName===tag&&node.textContent===text)[0];
const byClass=(root,name)=>all(root,node=>(node.className||'').split(' ').includes(name))[0];
const labeled=(root,text)=>all(root,node=>node.tagName==='label'&&node._text===text)[0]?.children[0];
async function settle(){for(let i=0;i<12;i++)await turn();}
function fixture(){
  const source={id:'raw-one',start:0,end:2,text:'<script>합성 원문은 그대로 보존</script>'};
  const lecture={id:LECTURE,title:'합성 수업',created_at:'2026-09-25T00:00:00Z',recording_finalized:true,segments:[source]};
  const note={format:'unified_study_note',version:1,overview:[{text:'합성 개요',source_ids:['raw-one']}],
    sections:[{heading:'핵심 개념',text:'합성 AI 상세 설명',source_ids:['raw-one'],originals:[clone(source)],edits:[],status:'mapped',warnings:[],citations:[]}],
    supporting_sources:[],coverage:{source_count:1,preserved_count:1,mapped_count:1,unverified_count:0,fallback_count:0,complete:true,semantic_verified:false},warnings:[]};
  return {lecture,note};
}
function harness(t){
  const document={createElement(tag){return new Element(tag,this);},defaultView:{confirm:()=>true}};
  const container=new Element('div',document),calls=[],selected=[],sessionChanges=[],created=[],revoked=[];
  const {lecture,note}=fixture();
  let current=lecture,key='synthetic-owner-token-server',hook=null;
  const courses=[{id:COURSE,name:'합성 강의 A',semester:'2026 가을',revision:1},{id:OTHER,name:'합성 강의 B',semester:'',revision:1}];
  const sessions=[{...lecture,session_name:'첫 회차',session_at:'2026-09-25T00:00:00Z'},
    {...lecture,id:SECOND,title:'두 번째 합성 수업',session_name:'둘째 회차'}];
  let reviewList=[],review={id:REVIEW,course_id:COURSE,status:'completed',created_at:'2026-09-25T00:00:00Z',stale:false,
    document:{format:'course_review',version:1,sessions:[{lecture_id:LECTURE,session_name:'첫 회차',session_at:'2026-09-25T00:00:00Z',document:note}],warnings:[]},markdown:'# 합성 복습\n전체 원문'};
  const assignment={lecture_id:LECTURE,course_id:COURSE,session_revision:3,session_name:'저장된 회차',session_at:'2026-09-25T00:00:00Z'};
  t.mock.method(URL,'createObjectURL',blob=>{const url='blob:synthetic-review-'+created.length;created.push({url,blob});return url;});
  t.mock.method(URL,'revokeObjectURL',url=>revoked.push(url));
  async function api(path,options={}){
    const call={path,method:options.method||'GET',...options};calls.push(call);
    if(hook){const value=await hook(call);if(value!==undefined)return value;}
    if(path.startsWith('/courses?'))return {courses:clone(courses),total:courses.length,next_offset:null};
    if(path==='/courses'&&call.method==='POST'){const data=JSON.parse(call.body);const row={...data,revision:1};courses.push(row);return clone(row);}
    if(path.endsWith('/course-session')){
      if(call.method==='PUT')return {...assignment,...JSON.parse(call.body),session_revision:4};
      return clone(assignment);
    }
    const courseDetail=path.match(/^\/courses\/([^/?]+)\?offset=(\d+)&limit=50$/);
    if(courseDetail)return {course:clone(courses.find(row=>row.id===courseDetail[1])),sessions:clone(sessions),materials:[],total:sessions.length,next_offset:null};
    if(/^\/courses\/[^/]+\/materials$/.test(path))return {materials:[]};
    if(/^\/courses\/[^/]+\/reviews(?:\?offset=\d+)?$/.test(path)){
      if(call.method==='POST')return {id:JSON.parse(call.body).id};
      return {reviews:clone(reviewList)};
    }
    if(/^\/courses\/[^/]+\/reviews\/[^/]+$/.test(path))return clone(review);
    if(path===`/lectures/${LECTURE}`)return clone(lecture);
    throw Error('Unexpected synthetic endpoint '+path);
  }
  const workspace=createCourseWorkspace({container,api,scopeKey:()=>key,getCurrent:()=>current,
    onSessionChanged:row=>sessionChanges.push(row),onSelectLecture:identifier=>selected.push(identifier)});
  t.after(()=>workspace.destroy());
  async function click(text){const button=byText(container,'button',text);assert.ok(button,'Missing button '+text);assert.equal(button.disabled,false,'Disabled '+text);await button.click();await settle();}
  async function pick(identifier){const picker=all(container,node=>node.tagName==='select'&&node.attributes['aria-label']==='복습할 강의')[0];picker.value=identifier;await picker.event('change');await settle();}
  return {container,workspace,calls,courses,sessions,assignment,lecture,note,selected,sessionChanges,created,revoked,click,pick,
    get review(){return review;},set review(value){review=value;},set reviewList(value){reviewList=value;},
    set current(value){current=value;},set key(value){key=value;},set hook(value){hook=value;}};
}

test('course list and local datetime helpers reject malformed dates and foreign-shaped identifiers',()=>{
  assert.equal(localDateTime('not a date'),'');assert.equal(localDateTime(null),'');
  assert.match(localDateTime('2026-09-25T00:00:00Z'),/^2026-09-25T\d\d:\d\d$/);
  const value={courses:[{id:COURSE,name:'강의',semester:'',revision:1}],total:1};assert.equal(validCoursePage(value),true);
  for(const change of [{total:501},{total:-1},{courses:[{...value.courses[0],id:'../foreign'}]},
    {courses:[{...value.courses[0],revision:NaN}]},{courses:null}])assert.equal(validCoursePage({...value,...change}),false);
});

test('current lecture metadata is loaded and explicit save preserves optimistic revision',async t=>{
  const h=harness(t);await h.workspace.open();
  assert.equal(labeled(h.container,'연결할 강의').value,COURSE);
  assert.equal(labeled(h.container,'회차 제목').value,'저장된 회차');
  labeled(h.container,'회차 제목').value='수정한 회차';labeled(h.container,'연결할 강의').value=OTHER;
  const form=byText(h.container,'h3','현재 수업의 강의 · 일시').parent;
  await form.event('submit');await settle();
  const call=h.calls.find(call=>call.method==='PUT');assert.equal(call.path,`/lectures/${LECTURE}/course-session`);
  assert.deepEqual(JSON.parse(call.body),{revision:3,course_id:OTHER,session_name:'수정한 회차',session_at:'2026-09-25T00:00:00.000Z'});
  assert.equal(h.sessionChanges.length,1);assert.equal(h.sessionChanges[0].session_revision,4);
});

test('without a current lecture assignment remains hidden and no metadata request is made',async t=>{
  const h=harness(t);h.current=null;await h.workspace.open();
  assert.equal(byText(h.container,'h3','현재 수업의 강의 · 일시').parent.hidden,true);
  assert.equal(h.calls.filter(call=>call.path.endsWith('/course-session')).length,0);
});

test('changed current lecture blocks a stale assignment submission and late saved callback',async t=>{
  const h=harness(t);await h.workspace.open();const form=byText(h.container,'h3','현재 수업의 강의 · 일시').parent;
  h.current={...h.lecture,id:SECOND};await form.event('submit');await settle();assert.equal(h.calls.filter(call=>call.method==='PUT').length,0);
  h.current=h.lecture;let release;
  h.hook=call=>call.method==='PUT'?new Promise(resolve=>release=resolve):undefined;
  await form.event('submit');await settle();h.current={...h.lecture,id:SECOND};
  release({...h.assignment,session_revision:4});await settle();assert.equal(h.sessionChanges.length,0);
});

test('whole-course request omits lecture_ids while selected request freezes checked lecture IDs',async t=>{
  const h=harness(t);await h.workspace.open();
  h.hook=call=>call.method==='POST'&&call.path.endsWith('/reviews')?{id:REVIEW}:undefined;
  await h.click('복습 정리본 만들기');let calls=h.calls.filter(call=>call.method==='POST'&&call.path.endsWith('/reviews'));
  assert.deepEqual(Object.keys(JSON.parse(calls[0].body)),['id']);
  const checkbox=all(byClass(h.container,'course-session-list'),node=>node.type==='checkbox')[1];checkbox.checked=true;await checkbox.event('change');
  assert.equal(labeled(h.container,'이 강의의 전체 수업').checked,false);
  await h.click('복습 정리본 만들기');calls=h.calls.filter(call=>call.method==='POST'&&call.path.endsWith('/reviews'));
  assert.deepEqual(JSON.parse(calls[1].body).lecture_ids,[SECOND]);
  assert.notEqual(JSON.parse(calls[0].body).id,JSON.parse(calls[1].body).id);
});

test('uncertain generation POST reuses the exact request ID and selection without duplicate work',async t=>{
  const h=harness(t);await h.workspace.open();let failed=false;
  h.hook=call=>{if(call.method==='POST'&&call.path.endsWith('/reviews')){if(!failed){failed=true;throw Error('synthetic network loss');}return {id:REVIEW};}};
  await h.click('복습 정리본 만들기');await h.click('같은 요청 상태 확인 · 재시도');
  const calls=h.calls.filter(call=>call.method==='POST'&&call.path.endsWith('/reviews'));
  assert.equal(calls.length,2);assert.equal(calls[0].body,calls[1].body);
});

test('empty selected scope does not send a generation request',async t=>{
  const h=harness(t);await h.workspace.open();labeled(h.container,'이 강의의 전체 수업').checked=false;
  await h.click('복습 정리본 만들기');assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
  assert.match(h.container.textContent,/복습할 수업을 선택/);
});

test('late previous course detail cannot replace a newer selected course',async t=>{
  const h=harness(t);await h.workspace.open();let release;
  h.hook=call=>call.path.startsWith(`/courses/${OTHER}?`)?new Promise(resolve=>release=resolve):undefined;
  const pending=h.pick(OTHER);await settle();await h.pick(COURSE);
  release({course:h.courses[1],sessions:[{...h.sessions[0],session_name:'지연된 다른 강의'}],materials:[],total:1,next_offset:null});await pending;
  assert.doesNotMatch(h.container.textContent,/지연된 다른 강의/);assert.match(h.container.textContent,/첫 회차/);
});

test('reset aborts old API and suppresses late metadata callback and account data rendering',async t=>{
  const h=harness(t);await h.workspace.open();let release,signal;
  h.hook=call=>call.method==='PUT'?new Promise(resolve=>{release=resolve;signal=call.signal;}):undefined;
  await byText(h.container,'h3','현재 수업의 강의 · 일시').parent.event('submit');await settle();
  h.workspace.reset();h.key='new-synthetic-account';release({...h.assignment,session_revision:4});await settle();
  assert.equal(signal.aborted,true);assert.equal(h.sessionChanges.length,0);
  assert.doesNotMatch(byClass(h.container,'course-session-list').textContent,/첫 회차/);
});

test('old detached lecture-open callback cannot act after an account reset',async t=>{
  const h=harness(t);await h.workspace.open();const old=byText(h.container,'button','이 수업 열기');
  h.workspace.reset();h.key='new-synthetic-account';await h.workspace.open();await old.click();await settle();
  assert.deepEqual(h.selected,[]);
});

test('reset while saving metadata or requesting review re-enables controls for the next context',async t=>{
  const h=harness(t);await h.workspace.open();let release;
  h.hook=call=>call.method==='PUT'?new Promise(resolve=>release=resolve):undefined;
  await byText(h.container,'h3','현재 수업의 강의 · 일시').parent.event('submit');await settle();
  h.workspace.reset();h.hook=null;await h.workspace.open();release({...h.assignment,session_revision:4});await settle();
  assert.equal(byText(h.container,'button','현재 수업 정보 저장').disabled,false);
  let releaseReview;h.hook=call=>call.method==='POST'?new Promise(resolve=>releaseReview=resolve):undefined;
  await byText(h.container,'button','복습 정리본 만들기').click();await settle();h.workspace.reset();h.hook=null;
  await h.workspace.open();releaseReview({id:REVIEW});await settle();
  assert.equal(byText(h.container,'button','복습 정리본 만들기').disabled,false);
});

test('validated review renders exact source as inert text and download URLs are revoked on reset',async t=>{
  const h=harness(t);h.reviewList=[{id:REVIEW,status:'completed',created_at:'2026-09-25T00:00:00Z'}];await h.workspace.open();
  const output=byClass(h.container,'course-review-output');assert.match(output.textContent,/합성 AI 상세 설명/);
  assert.match(output.textContent,/<script>합성 원문은 그대로 보존<\/script>/);
  assert.match(output.textContent,/의미상 완전성은 확인하지 못했습니다/);
  assert.equal(all(output,node=>node.tagName==='script').length,0);
  assert.equal(h.created.length,1);assert.match(await h.created[0].blob.text(),/전체 원문/);
  h.workspace.reset();assert.deepEqual(h.revoked,h.created.map(item=>item.url));
});

test('mismatched source text and incomplete source coverage prevent review rendering and download',async t=>{
  const h=harness(t);h.reviewList=[{id:REVIEW,status:'completed',created_at:'2026-09-25T00:00:00Z'}];
  h.review.document.sessions[0].document.sections[0].originals[0].text='invented source text';
  await h.workspace.open();assert.equal(h.created.length,0);
  assert.equal(byClass(h.container,'course-review-output').children.length,0);
  assert.match(h.container.textContent,/복습 원문과 저장 결과가 달라/);
});

test('a late review detail after account reset never fetches source or creates a download',async t=>{
  const h=harness(t);h.reviewList=[{id:REVIEW,status:'completed',created_at:'2026-09-25T00:00:00Z'}];let release,signal;
  h.hook=call=>call.path===`/courses/${COURSE}/reviews/${REVIEW}`?new Promise(resolve=>{release=resolve;signal=call.signal;}):undefined;
  const pending=h.workspace.open();for(let i=0;i<50&&!release;i++)await turn();assert.equal(typeof release,'function');h.workspace.reset();h.key='another-account';release(h.review);await pending;
  assert.equal(signal.aborted,true);assert.equal(h.created.length,0);
  assert.equal(h.calls.filter(call=>call.path===`/lectures/${LECTURE}`).length,0);
});
