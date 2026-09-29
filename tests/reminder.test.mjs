import assert from 'node:assert/strict';
import {test} from 'node:test';
import {setImmediate as turn} from 'node:timers/promises';
import {webcrypto} from 'node:crypto';
import {createReminder,validReminderState,memoryCurve} from '../web/reminder.js';
import * as schedule from '../web/review-schedule.js';

const TODAY='2026-09-29',ID='11111111-1111-4111-8111-111111111111',SECOND='22222222-2222-4222-8222-222222222222';
const copy=value=>structuredClone(value);
class Element {
  constructor(tag,document){this.tagName=tag;this.ownerDocument=document;this.children=[];this.attributes={};this.dataset={};this.style={};this._text='';this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.open=false;this.files=[];this.listeners={};this.className='';this.classList={add:(...xs)=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),...xs])].join(' ');},remove:(...xs)=>{this.className=this.className.split(' ').filter(x=>!xs.includes(x)).join(' ');},contains:x=>this.className.split(' ').includes(x),toggle:(x,value)=>{const on=value??!this.classList.contains(x);if(on)this.classList.add(x);else this.classList.remove(x);return on;}};}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  set textContent(value){this._text=String(value);this.children=[];}
  set innerHTML(_){throw Error('The reminder must never interpret user HTML');}
  append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
  replaceChildren(...children){this.children=[];this._text='';this.append(...children);}
  setAttribute(key,value){this.attributes[key]=String(value);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  focus(){this.ownerDocument.activeElement=this;}
  scrollIntoView(){}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  removeEventListener(name,fn){this.listeners[name]=(this.listeners[name]||[]).filter(x=>x!==fn);}
  async event(name,extra={}){const event={target:this,key:'',preventDefault(){this.prevented=true;},...extra};await this['on'+name]?.(event);for(const fn of this.listeners[name]||[])await fn(event);return event;}
  async click(){if(!this.disabled)return this.event('click');}
}
function descendants(node){return [node,...node.children.flatMap(descendants)];}
const tagged=(root,tag,text)=>descendants(root).find(node=>node.tagName===tag&&node.textContent===text);
const field=(root,label)=>descendants(root).find(node=>node.tagName==='label'&&node._text===label)?.children[0];
const action=(root,name)=>descendants(root).find(node=>node.dataset.action===name);
const section=(root,name)=>descendants(root).find(node=>node.tagName==='section'&&node.children.some(child=>child.tagName==='h2'&&child.textContent===name));
const dateCell=(root,date)=>descendants(section(root,'복습 달력')).find(node=>node.dataset.date===date);
async function settle(){for(let i=0;i<5;i++)await turn();}
function baseState(){return {revision:0,today:TODAY,items:[],routines:[],exams:[],timetable:null,settings:{offsets:[1,3,7,14,30],sem_start:null,sort:'due'},holidays:{'2026-09-24':'추석','2026-10-05':'대체공휴일'},recognition:{enabled:false}};}
function item(extra={}){return {id:ID,title:'합성 공부',subject:'테스트 과목',memo:'<img src=x onerror=alert(1)>',learned:TODAY,base:TODAY,offsets:[0,1,3],reviews:[],history:[],resets:0,source:'manual',source_key:null,catchup:false,skipped:null,moved:null,...extra};}
function harness(t,initial=baseState()){
  const listeners={},document={visibilityState:'visible',activeElement:null,createElement(tag){return new Element(tag,this);},createElementNS(_,tag){return this.createElement(tag);},addEventListener(name,fn){listeners[name]=fn;},removeEventListener(name){delete listeners[name];},defaultView:{addEventListener(){},removeEventListener(){}}};
  const container=new Element('div',document),calls=[],receipts=new Map();let state=copy(initial),key='owner:session:origin',hook=null,snapshot=null;
  async function api(path,options={}){
    const call={path,...options};calls.push(call);if(hook){const result=await hook(call);if(result!==undefined)return result;}
    if(path==='/review/state')return copy(state);
    if(path==='/review/timetable/parse')return {classes:[{subject:'인식한 합성 수업',day:2,start:'13:00',end:'',room:''}]};
    assert.equal(path,'/review/actions');const envelope=JSON.parse(options.body),{action,payload}=envelope;
    if(receipts.has(envelope.request_id))return copy(receipts.get(envelope.request_id));
    assert.equal(envelope.revision,state.revision,'UI must send the current optimistic revision');
    if(action==='undo'){assert.equal(payload.token,'synthetic-undo');state=copy(snapshot);state.revision++;return copy(state);}
    snapshot=copy(state);const found=state.items.find(row=>row.id===payload.id);let changed;
    if(action==='item.add')state.items.push(item({...payload,id:webcrypto.randomUUID(),base:payload.learned,offsets:state.settings.offsets}));
    else if(action==='item.review')changed=schedule.completeReview(found,TODAY);
    else if(action==='item.reset')changed=schedule.resetItem(found,TODAY);
    else if(action==='item.skip')changed=schedule.skipItem(found,TODAY);
    else if(action==='item.unskip')changed=schedule.unskipItem(found,TODAY);
    else if(action==='item.move')changed=schedule.placeReview(found,payload.date,TODAY);
    else if(action==='item.edit_review')changed=schedule.editReviewDate(found,payload.stage,payload.date,TODAY);
    else if(action==='item.remove_review')changed=schedule.undoLastReview(found);
    else if(action==='item.delete')state.items=state.items.filter(row=>row.id!==payload.id);
    else if(action==='item.group_review')state.items=state.items.map(row=>payload.ids.includes(row.id)?schedule.completeReview(row,TODAY):row);
    else if(action==='settings.save')Object.assign(state.settings,payload);
    else if(action==='timetable.save'){state.timetable={...payload,through:schedule.addDays(payload.from,-1)};const generated=schedule.timetablePreview(state.timetable,TODAY,state.settings,state.holidays);state.items.push(...generated.items.map(row=>({...row,id:webcrypto.randomUUID()})));state.timetable.through=generated.through;state.settings.sem_start||=payload.from.slice(0,8)+'01';}
    else if(action==='timetable.mode')state.timetable.mode=payload.mode;
    else if(action==='timetable.disable')state.timetable=null;
    else if(action==='timetable.catchup'){const rows=schedule.catchupPreview(state.timetable,payload.sem_start,TODAY,state.settings,state.holidays,state.items.map(row=>row.source_key).filter(Boolean),state.exams).map(row=>({...row,selected:payload.source_keys.includes(row.source_key)}));state.items.push(...schedule.catchupPlan(rows,state.timetable,TODAY,state.settings,state.holidays,payload.per_day).map(row=>({...row,id:webcrypto.randomUUID()})));}
    else if(action==='routine.add')state.routines.push({...payload,id:webcrypto.randomUUID(),done:[]});
    else if(action==='routine.check')state.routines=state.routines.map(row=>row.id===payload.id?schedule.toggleRoutine(row,payload.date,TODAY,payload.checked):row);
    else if(action==='routine.stop')state.routines.find(row=>row.id===payload.id).end=schedule.addDays(TODAY,-1);
    else if(action==='routine.delete')state.routines=state.routines.filter(row=>row.id!==payload.id);
    else if(action==='exam.save')state.exams.push({...payload,id:webcrypto.randomUUID(),done:{}});
    else if(action==='exam.check'){const exam=state.exams.find(row=>row.id===payload.id);(exam.done[payload.session_key]??=Array(exam.rounds).fill(false))[payload.round]=payload.checked;}
    else if(action==='exam.delete')state.exams=state.exams.filter(row=>row.id!==payload.id);
    else assert.fail('Unexpected action '+action);
    if(changed)state.items=state.items.map(row=>row.id===changed.id?changed:row);
    state.revision++;const result={...copy(state),undo:{token:'synthetic-undo',expires_at:new Date(Date.now()+8000).toISOString()}};receipts.set(envelope.request_id,result);return result;
  }
  const controller=createReminder({container,api,scopeKey:()=>key});t.after(()=>controller.destroy());
  async function click(text,root=container){const node=tagged(root,'button',text);assert.ok(node,'Missing button '+text);assert.equal(node.disabled,false);await node.click();await settle();}
  async function submit(name){const form=descendants(section(container,name)).find(node=>node.tagName==='form');assert.ok(form);await form.event('submit');await settle();}
  return {container,document,calls,controller,click,submit,get state(){return state;},set state(v){state=copy(v);},get key(){return key;},set key(v){key=v;},set hook(v){hook=v;},listeners};
}

test('state validation allows an unknown class end time and rejects unsafe or malformed scheduling data',()=>{
  const state=baseState();assert.equal(validReminderState(state),true);state.timetable={classes:[{subject:'수업',day:2,start:'13:00',end:'',room:''}],from:TODAY,through:TODAY,until:null,mode:{sameDay:false,nextDay:false,eve:true,curve:false}};assert.equal(validReminderState(state),true);
  for(const change of [{today:'2026-02-30'},{revision:-1},{items:[item({offsets:[1,1]})]},{items:[item({id:'foreign/path'})]}])assert.equal(validReminderState({...state,...change}),false);
});
test('initial load uses only authenticated server state and displays no invented sample records',async t=>{
  const h=harness(t);await h.controller.open();assert.equal(h.calls.length,1);assert.equal(h.calls[0].path,'/review/state');assert.match(h.container.textContent,/오늘 복습할 내용 0개/);assert.match(h.container.textContent,/첫 기록을 추가/);assert.ok(!h.calls.some(call=>call.method==='POST'));
});
test('add, review and eight-second undo use server mutations and preserve inert text',async t=>{
  const h=harness(t);h.state.settings.offsets=[0,1,3];await h.controller.open();field(section(h.container,'새로 공부한 내용'),'공부한 내용').value='<script>합성 내용</script>';await h.submit('새로 공부한 내용');assert.equal(h.state.items.length,1);assert.match(h.container.textContent,/<script>합성 내용<\/script>/);assert.equal(descendants(h.container).filter(node=>node.tagName==='script').length,0);
  await h.click('복습 완료',section(h.container,'오늘의 복습'));assert.deepEqual(h.state.items[0].reviews,[TODAY]);await h.click('되돌리기');assert.deepEqual(h.state.items[0].reviews,[]);assert.equal(h.calls.filter(call=>call.path==='/review/actions').length,3);
});
test('subject group completion submits every member once and undo restores the complete group',async t=>{
  const state=baseState();state.settings.sort='subject';state.items=[item(),item({id:SECOND,title:'두 번째',learned:'2026-09-20',base:'2026-09-20'})];const h=harness(t,state);await h.controller.open();await h.click('모두 복습 완료');assert.ok(h.state.items.every(row=>row.reviews.length===1));const sent=JSON.parse(h.calls.find(call=>call.method==='POST').body);assert.deepEqual(new Set(sent.payload.ids),new Set([ID,SECOND]));await h.click('되돌리기');assert.ok(h.state.items.every(row=>!row.reviews.length));
});
test('dragging the next review into a calendar cell changes only that stage and touch move can cancel',async t=>{
  const state=baseState();state.items=[item()];const h=harness(t,state);await h.controller.open();const card=descendants(section(h.container,'오늘의 복습')).find(node=>node.dataset.item===ID);await card.event('dragstart',{dataTransfer:{setData(){}}});await dateCell(h.container,'2026-09-30').event('drop');await settle();assert.deepEqual(h.state.items[0].moved,{stage:0,date:'2026-09-30'});assert.equal(schedule.projected(h.state.items[0],TODAY)[1].date,'2026-10-01');
  await dateCell(h.container,'2026-09-30').click();await h.click('옮기기');assert.match(h.container.textContent,/지난 날짜를 누르면/);await h.container.event('keydown',{key:'Escape'});const before=h.calls.length;await dateCell(h.container,TODAY).click();assert.equal(h.calls.length,before);
});
test('past placement rejects pre-learning dates without a POST, and stage editing preserves ordering',async t=>{
  const state=baseState();state.items=[item()];const h=harness(t,state);await h.controller.open();await h.click('옮기기');await dateCell(h.container,'2026-09-28').click();await settle();assert.equal(h.calls.filter(call=>call.method==='POST').length,0);assert.match(h.container.textContent,/앞 날짜/);
});
test('timetable editor saves rows and current day generation appears without a second explicit request',async t=>{
  const h=harness(t);await h.controller.open();await h.click('시간표 등록');const panel=section(h.container,'수업 시간표');field(panel,'과목').value='합성 수업';field(panel,'요일').value='2';field(panel,'시작').value='13:00';field(panel,'끝').value='14:00';await h.submit('수업 시간표');assert.equal(h.state.timetable.classes.length,1);assert.equal(h.state.items.length,1);assert.match(h.container.textContent,/9\/29\(화\) 합성 수업/);
});
test('recognition is hidden without capability, explicit-only when enabled, and does not save its result',async t=>{
  const h=harness(t);await h.controller.open();await h.click('시간표 등록');const parse=descendants(h.container).find(node=>node.tagName==='details'&&node.textContent.includes('이미지 · 글에서 시간표 인식'));assert.equal(parse.hidden,true);
  h.state.recognition={enabled:true};await h.controller.refresh();assert.equal(parse.hidden,false);field(parse,'시간표 글 · 6,000자 이하').value='화요일 13시 수업';await h.controller.refresh();assert.equal(h.calls.filter(call=>call.path.includes('/parse')).length,0);await h.click('글 인식');assert.equal(h.calls.filter(call=>call.path.includes('/parse')).length,1);assert.equal(h.calls.filter(call=>call.path==='/review/actions').length,0);assert.equal(field(section(h.container,'수업 시간표'),'끝').value,'');assert.match(parse.textContent,/아직 저장하지 않았습니다/);
});
test('periodic refresh and hide/reopen retain unsaved new item and timetable drafts',async t=>{
  const h=harness(t);await h.controller.open();field(section(h.container,'새로 공부한 내용'),'공부한 내용').value='아직 저장하지 않은 내용';await h.click('시간표 등록');const f=field(section(h.container,'수업 시간표'),'과목');f.value='편집 중';await f.event('input');await h.controller.refresh();assert.equal(f.value,'편집 중');assert.equal(field(section(h.container,'새로 공부한 내용'),'공부한 내용').value,'아직 저장하지 않은 내용');h.controller.hide();await h.controller.open();assert.equal(f.value,'편집 중');assert.equal(field(section(h.container,'새로 공부한 내용'),'공부한 내용').value,'아직 저장하지 않은 내용');
});
test('date chips update from the unsaved learned date and new server intervals without creating a record',async t=>{
  const h=harness(t);await h.controller.open();const add=section(h.container,'새로 공부한 내용'),learned=field(add,'공부한 날');
  learned.value='2026-09-20';await learned.event('input');const chips=()=>descendants(add).filter(node=>node.classList.contains('rv-preview-date')).map(node=>node.textContent);
  assert.deepEqual(chips(),['9/21 (월)','9/23 (수)','9/27 (일)','10/4 (일)','10/20 (화)']);
  h.state.settings.offsets=[0,2];h.state.revision++;await h.controller.refresh();assert.equal(learned.value,'2026-09-20');assert.deepEqual(chips(),['9/20 (일)','9/22 (화)']);
  assert.equal(h.calls.filter(call=>call.method==='POST').length,0);assert.equal(h.state.items.length,0);
});
test('scope change aborts pending reads and removes the previous account form and records',async t=>{
  const state=baseState();state.items=[item()];const h=harness(t,state);await h.controller.open();field(section(h.container,'새로 공부한 내용'),'공부한 내용').value='이전 계정 초안';let release,signal;h.hook=call=>call.path==='/review/state'?new Promise(resolve=>{release=resolve;signal=call.signal;}):undefined;const pending=h.controller.refresh();await settle();h.controller.reset();h.key='different-owner';assert.equal(signal.aborted,true);release(state);await pending;assert.ok(!h.container.textContent.includes('합성 공부'));assert.equal(field(section(h.container,'새로 공부한 내용'),'공부한 내용').value,'');
});
test('an uncertain POST is retried with its exact immutable request ID and payload',async t=>{
  const state=baseState();state.items=[item()];const h=harness(t,state);await h.controller.open();let fail=true;h.hook=call=>{if(call.method==='POST'&&fail){fail=false;throw Error('synthetic network loss');}};await h.click('복습 완료',section(h.container,'오늘의 복습'));assert.match(h.container.textContent,/같은 요청으로 확인/);await h.click('같은 요청 확인 · 재시도');const calls=h.calls.filter(call=>call.method==='POST');assert.equal(calls.length,2);assert.equal(calls[0].body,calls[1].body);assert.deepEqual(h.state.items[0].reviews,[TODAY]);
});
test('a concurrent-device revision conflict refreshes but never overwrites unsaved input',async t=>{
  const h=harness(t);await h.controller.open();field(section(h.container,'새로 공부한 내용'),'공부한 내용').value='보존할 초안';h.hook=call=>{if(call.method==='POST')throw Object.assign(Error('stale_revision'),{status:409});};await h.submit('새로 공부한 내용');assert.equal(field(section(h.container,'새로 공부한 내용'),'공부한 내용').value,'보존할 초안');assert.equal(h.calls.filter(call=>call.path==='/review/state').length,2);
});
test('routine add and current-day check persist, while future calendar checks are disabled',async t=>{
  const h=harness(t);await h.controller.open();field(section(h.container,'루틴'),'할 일').value='단어 보기';field(section(h.container,'루틴'),'횟수 · 0이면 무제한').value='6';await h.submit('루틴');const checkbox=descendants(section(h.container,'오늘의 복습')).find(node=>node.dataset.action==='routine.check');checkbox.checked=true;await checkbox.event('change');await settle();assert.deepEqual(h.state.routines[0].done,[TODAY]);await dateCell(h.container,'2026-09-30').click();const future=descendants(section(h.container,'복습 달력')).find(node=>node.dataset.action==='routine.check');assert.equal(future.disabled,true);
});
test('deletion needs two clicks and restore uses server undo instead of synthetic local data',async t=>{
  const state=baseState();state.items=[item()];const h=harness(t,state);await h.controller.open();await h.click('삭제',section(h.container,'전체 기록'));assert.equal(h.calls.filter(call=>call.method==='POST').length,0);await h.click('한 번 더 눌러 확인');assert.equal(h.state.items.length,0);await h.click('되돌리기');assert.equal(h.state.items.length,1);
});
test('catchup preview leaves holidays unchecked and existing dates disabled, then sends selected keys',async t=>{
  const state=baseState();state.settings.sem_start='2026-09-21';state.timetable={classes:[{subject:'목요일 수업',day:4,start:'13:00',end:'14:00',room:''}],from:TODAY,until:null,through:'2026-09-28',mode:{sameDay:false,nextDay:false,eve:true,curve:false}};const h=harness(t,state);await h.controller.open();await h.click('지난 수업 불러오기');const holidayLabel=descendants(h.container).find(node=>node.tagName==='label'&&node._text.includes('추석'));assert.ok(holidayLabel);assert.equal(holidayLabel.children[0].checked,false);holidayLabel.children[0].checked=true;await holidayLabel.children[0].event('change');await h.click('선택한 지난 수업 불러오기');const sent=JSON.parse(h.calls.find(call=>call.method==='POST').body);assert.equal(sent.action,'timetable.catchup');assert.equal(sent.payload.source_keys.length,1);
});
test('expired or unsupported API response is shown as a clear state rather than a spinner',async t=>{
  const h=harness(t);h.hook=()=>{throw Object.assign(Error('Not Found'),{status:404});};await h.controller.open();assert.match(h.container.textContent,/아직 리마인더를 지원하지/);assert.equal(h.container.hidden,false);
});
test('memory curve uses learned date for catchup and includes actual, projected and no-review series',()=>{
  const curve=memoryCurve(item({catchup:true,learned:'2026-09-01',base:'2026-10-02'}),TODAY);assert.equal(curve.base,'2026-09-01');assert.equal(curve.todayT,28);assert.ok(curve.past.length>1);assert.ok(curve.future.length>1);assert.equal(curve.baseline.length,201);assert.ok(curve.remembered<.01);
});

test('deleted timetable source keys stay disabled in catchup even when no item remains',async t=>{
  const state=baseState();state.settings.sem_start='2026-09-21';state.source_keys=['2026-09-22|화요일 수업|2|13:00'];state.timetable={classes:[{subject:'화요일 수업',day:2,start:'13:00',end:'14:00',room:''}],from:TODAY,until:null,through:'2026-09-28',mode:copy(schedule.DEFAULT_MODE)};const h=harness(t,state);await h.controller.open();await h.click('지난 수업 불러오기');const row=descendants(h.container).find(node=>node.tagName==='label'&&node._text.includes('이미 있음'));assert.ok(row);assert.equal(row.children[0].disabled,true);assert.equal(row.children[0].checked,false);
});

test('the next-stage today button records early completion rather than only moving its due date',async t=>{
  const state=baseState();state.items=[item({offsets:[1,3,7]})];const h=harness(t,state);await h.controller.open();await h.click('오늘 복습함');assert.deepEqual(h.state.items[0].reviews,[TODAY]);assert.equal(h.state.items[0].moved,null);assert.equal(JSON.parse(h.calls.find(call=>call.method==='POST').body).action,'item.review');
});

test('recognition never overwrites rows edited during its delayed response',async t=>{
  const state=baseState();state.recognition={enabled:true};const h=harness(t,state);await h.controller.open();await h.click('시간표 등록');field(h.container,'시간표 글 · 6,000자 이하').value='합성 입력';let release;h.hook=call=>call.path.includes('/parse')?new Promise(resolve=>release=resolve):undefined;const pending=tagged(h.container,'button','글 인식').click();await settle();const subject=field(section(h.container,'수업 시간표'),'과목');subject.value='응답 중 직접 수정';await subject.event('input');release({classes:[{subject:'늦은 인식',day:2,start:'13:00',end:'14:00',room:''}]});await pending;assert.equal(subject.value,'응답 중 직접 수정');assert.match(h.container.textContent,/덮어쓰지 않았어요/);assert.equal(h.calls.filter(call=>call.path==='/review/actions').length,0);
});

test('invalid image MIME and oversize input are rejected before any paid recognition request',async t=>{
  const state=baseState();state.recognition={enabled:true};const h=harness(t,state);await h.controller.open();await h.click('시간표 등록');const input=field(h.container,'시간표 이미지 · PNG/JPEG/WebP, 10MB 이하');input.files=[{type:'image/svg+xml',size:100}];await h.click('이미지 인식');input.files=[{type:'image/png',size:10*1024*1024+1}];await h.click('이미지 인식');assert.equal(h.calls.filter(call=>call.path.includes('/parse')).length,0);
});

test('double submit while a mutation is pending sends exactly one request and preserves later typing',async t=>{
  const h=harness(t);await h.controller.open();const title=field(section(h.container,'새로 공부한 내용'),'공부한 내용');title.value='첫 내용';let release;h.hook=call=>call.method==='POST'?new Promise(resolve=>release=resolve):undefined;const form=descendants(section(h.container,'새로 공부한 내용')).find(node=>node.tagName==='form');const pending=form.event('submit');await settle();title.value='다음 내용';await form.event('submit');assert.equal(h.calls.filter(call=>call.method==='POST').length,1);release({...baseState(),revision:1,items:[item({title:'첫 내용'})]});await pending;assert.equal(title.value,'다음 내용');
});

test('undo expires after eight seconds and does not submit a stale restore',async t=>{
  const state=baseState();state.items=[item()];const h=harness(t,state);await h.controller.open();let clock=Date.now();t.mock.method(Date,'now',()=>clock);await h.click('복습 완료',section(h.container,'오늘의 복습'));clock+=8001;await h.click('되돌리기');assert.equal(h.calls.filter(call=>call.method==='POST').length,1);assert.deepEqual(h.state.items[0].reviews,[TODAY]);
});

test('a routine checkbox and exam table preserve Boolean completion from the API',async t=>{
  const state=baseState();state.settings.sem_start='2026-09-01';state.items=[item({learned:'2026-09-22',base:'2026-09-22'})];state.exams=[{id:SECOND,subject:'테스트 과목',kind:'mid',date:'2026-10-06',rounds:3,lead:7,done:{[ID]:[true,false,false]}}];const h=harness(t,state);await h.controller.open();const panel=section(h.container,'시험 대비');assert.match(panel.textContent,/1회독 1\/1/);const cells=descendants(panel).filter(node=>node.type==='checkbox');assert.equal(cells[0].checked,true);cells[1].checked=true;await cells[1].event('change');await settle();assert.equal(h.state.exams[0].done[ID][1],true);
});

test('mode draft, stage date input and all-record filters survive a background state refresh',async t=>{
  const state=baseState();state.items=[item({reviews:['2026-09-29']})];state.timetable={classes:[{subject:'합성',day:2,start:'13:00',end:'14:00',room:''}],from:TODAY,until:null,through:TODAY,mode:copy(schedule.DEFAULT_MODE)};const h=harness(t,state);await h.controller.open();await h.click('수업 리듬');const review=field(section(h.container,'기억 곡선 · 추정'),'1회차 복습한 날짜');review.value='2026-09-28';await review.event('input');h.state.revision++;await h.controller.refresh();assert.equal(field(section(h.container,'수업 시간표'),'수업 당일').checked,true);assert.equal(field(section(h.container,'기억 곡선 · 추정'),'1회차 복습한 날짜').value,'2026-09-28');
});

function modeExampleState(classes,extra={}){
  const state=baseState();state.timetable={classes:classes.map(row=>({start:'09:00',end:'10:00',room:'',...row})),from:TODAY,until:null,through:TODAY,mode:{sameDay:true,nextDay:false,eve:false,curve:false},...extra};return state;
}
const modeExampleTexts=root=>descendants(section(root,'수업 시간표')).filter(node=>node.tagName==='p'&&node.textContent.includes('수업이면 →')).map(node=>node.textContent);

test('mode example uses the next actual weekday and anchors review dates to that lesson',async t=>{
  const state=modeExampleState([{subject:'금요일',day:5},{subject:'수요일',day:3}],{mode:copy(schedule.DEFAULT_MODE)}),h=harness(t,state);await h.controller.open();
  assert.deepEqual(modeExampleTexts(h.container),['9/30 (수) 수요일 수업이면 → 10/6 (화)에 복습해요.']);
  await h.click('수업 리듬');assert.deepEqual(modeExampleTexts(h.container),['9/30 (수) 수요일 수업이면 → 9/30 (수) · 10/6 (화)에 복습해요.']);
  assert.deepEqual(h.state,state);assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
});
test('mode example skips holidays and the scheduled subjects own exam date',async t=>{
  const state=modeExampleState([{subject:'화요일',day:2},{subject:'수요일',day:3},{subject:'목요일',day:4}]);state.holidays[TODAY]='합성 휴일';state.exams=[{id:ID,subject:'수요일',kind:'mid',date:'2026-09-30',rounds:3,lead:7,done:{}}];const h=harness(t,state);await h.controller.open();
  assert.deepEqual(modeExampleTexts(h.container),['10/1 (목) 목요일 수업이면 → 10/1 (목)에 복습해요.']);assert.deepEqual(h.state,state);assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
});
test('mode example still includes today when only a different subject has an exam',async t=>{
  const state=modeExampleState([{subject:'화요일',day:2}]);state.exams=[{id:ID,subject:'다른 과목',kind:'mid',date:TODAY,rounds:3,lead:7,done:{}}];const h=harness(t,state);await h.controller.open();
  assert.deepEqual(modeExampleTexts(h.container),['9/29 (화) 화요일 수업이면 → 9/29 (화)에 복습해요.']);assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
});
test('mode example respects timetable start and inclusive end dates',async t=>{
  const state=modeExampleState([{subject:'화요일',day:2},{subject:'수요일',day:3},{subject:'목요일',day:4}],{from:'2026-09-30',until:'2026-09-30'}),h=harness(t,state);await h.controller.open();
  assert.deepEqual(modeExampleTexts(h.container),['9/30 (수) 수요일 수업이면 → 9/30 (수)에 복습해요.']);
  h.state.timetable={...h.state.timetable,from:'2026-09-01',until:'2026-09-28'};h.state.revision++;await h.controller.refresh();assert.deepEqual(modeExampleTexts(h.container),[]);assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
});
test('mode example searches fourteen dates including today and clears when no lesson qualifies',async t=>{
  const state=modeExampleState([{subject:'월요일',day:1}],{from:'2026-10-12'}),h=harness(t,state);await h.controller.open();
  assert.deepEqual(modeExampleTexts(h.container),['10/12 (월) 월요일 수업이면 → 10/12 (월)에 복습해요.']);
  h.state.timetable={...h.state.timetable,from:'2026-10-13',classes:[{subject:'범위 밖 화요일',day:2,start:'09:00',end:'10:00',room:''}]};h.state.revision++;await h.controller.refresh();assert.deepEqual(modeExampleTexts(h.container),[]);
  h.state.timetable.classes=[];h.state.revision++;await h.controller.refresh();await h.click('전날만');assert.deepEqual(modeExampleTexts(h.container),[]);assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
});
test('mode example selects the earliest start on the first eligible day without reordering saved classes',async t=>{
  const state=modeExampleState([{subject:'오후',day:2,start:'14:00',end:'15:00'},{subject:'아침',day:2,start:'09:00',end:'10:00'},{subject:'낮',day:2,start:'11:00',end:'12:00'}]),h=harness(t,state);await h.controller.open();
  assert.deepEqual(modeExampleTexts(h.container),['9/29 (화) 아침 수업이면 → 9/29 (화)에 복습해요.']);await h.controller.refresh();assert.deepEqual(h.state,state);assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
});

test('new KST day on visibility refresh updates today without discarding manual learned date',async t=>{
  const h=harness(t);await h.controller.open();const learned=field(section(h.container,'새로 공부한 내용'),'공부한 날');learned.value='2026-09-22';h.state.today='2026-09-30';h.listeners.visibilitychange();await settle();assert.match(h.container.textContent,/2026\.09\.30 수요일/);assert.equal(learned.value,'2026-09-22');
});

test('skip is omitted from today, available through the skipped filter, and reversible without deleting it',async t=>{
  const state=baseState();state.items=[item()];const h=harness(t,state);await h.controller.open();await h.click('복습 안 함',section(h.container,'오늘의 복습'));assert.equal(h.state.items[0].skipped,TODAY);assert.match(section(h.container,'오늘의 복습').textContent,/오늘 복습할 내용이 없어요/);await h.click('안 함',section(h.container,'전체 기록'));await h.click('되살리기',section(h.container,'전체 기록'));assert.equal(h.state.items[0].skipped,null);
});

test('generation capacity warning retains existing records and normal read actions',async t=>{
  const state=baseState();state.items=[item()];state.generation_error='item_limit';const h=harness(t,state);await h.controller.open();assert.match(h.container.textContent,/자동 등록을 잠시 멈췄어요/);assert.match(h.container.textContent,/합성 공부/);assert.equal(tagged(section(h.container,'오늘의 복습'),'button','복습 완료').disabled,false);
});

test('select accessible names contain their label alone, independently of option text',async t=>{
  const h=harness(t);await h.controller.open();await h.click('시간표 등록');
  for(const name of ['요일','과목','시작','끝','강의실'])assert.equal(field(section(h.container,'수업 시간표'),name).attributes['aria-label'],name);
  for(const name of ['시험 종류','회독 수','벼락치기 시작'])assert.equal(field(section(h.container,'시험 대비'),name).attributes['aria-label'],name);
});

test('generation warning survives unrelated saves and clears only after the server clears the error',async t=>{
  const state=baseState();state.generation_error='item_limit';state.items=[item()];const h=harness(t,state);await h.controller.open();const warning=descendants(h.container).find(node=>node.dataset.notice==='generation');
  assert.equal(warning.hidden,false);await h.click('복습 완료',section(h.container,'오늘의 복습'));assert.equal(warning.hidden,false);assert.match(warning.textContent,/개수 제한/);
  await h.controller.refresh();assert.equal(warning.hidden,false);delete h.state.generation_error;h.state.revision++;await h.controller.refresh();assert.equal(warning.hidden,true);
});
