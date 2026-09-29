import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import * as s from '../web/review-schedule.js';

const HOLIDAYS=JSON.parse(readFileSync(new URL('../data/review-holidays/2026.json',import.meta.url),'utf8'));
const TODAY='2026-09-29',SEM='2026-09-01',SETTINGS={offsets:[1,3,7,14,30],sem_start:SEM};
const CURVE={sameDay:true,nextDay:true,eve:false,curve:true};
const RHYTHM={sameDay:true,nextDay:false,eve:true,curve:false};
const EVE={sameDay:false,nextDay:false,eve:true,curve:false};
function timetable(mode=CURVE){
  const specs=[['행정학의이해(eng)',[1,3],'13:30','14:45','S1415'],['사회조사방법론I',[1,3],'15:00','16:15','S1546'],
    ['현대사회와심리학',[1,3],'16:30','17:45','S1217'],['창업과공동체',[2],'13:30','16:15','S4115'],
    ['글로벌문화',[2,4],'16:30','17:45','S4509'],['공동체활성화론',[4],'12:00','14:45','S1546']];
  return {classes:specs.flatMap(([subject,days,start,end,room])=>days.map(day=>({subject,day,start,end,room}))),
    from:TODAY,until:null,through:'2026-09-28',mode:{...mode}};
}
function item(changes={}){return {id:'synthetic-item',title:'합성 복습',subject:'사회조사방법론I',memo:'',learned:'2026-09-20',base:'2026-09-20',
  offsets:[1,3,7],reviews:[],history:[{date:'2026-09-20',type:'learn'}],resets:0,source:'manual',source_key:null,catchup:false,skipped:null,moved:null,...changes};}
function exams(){return [{id:'mid-admin',subject:'행정학의이해(eng)',kind:'mid',date:'2026-10-21',rounds:3,lead:7,done:{}},
  {id:'final-admin',subject:'행정학의이해(eng)',kind:'final',date:'2026-12-16',rounds:3,lead:7,done:{}},
  {id:'mid-global',subject:'글로벌문화',kind:'mid',date:'2026-10-06',rounds:3,lead:7,done:{}}];}

test('KST midnight and calendar-day arithmetic do not depend on host timezone or DST',()=>{
  assert.equal(s.kstToday(new Date('2026-09-28T14:59:59Z')),'2026-09-28');
  assert.equal(s.kstToday(new Date('2026-09-28T15:00:00Z')),TODAY);
  assert.equal(s.addDays('2024-02-28',1),'2024-02-29');assert.equal(s.diffDays('2026-03-01','2026-03-09'),8);
  assert.equal(s.dayOfWeek(TODAY),2);
  for(const day of ['2026-02-29','2026-9-29','0000-01-01',null])assert.equal(s.validDate(day),false);
  for(const values of [[],[true],[0,0],[3,1],[-1],[366],Array.from({length:13},(_,i)=>i)])assert.equal(s.validOffsets(values),false);
  assert.equal(s.validOffsets([0,1,365]),true);assert.deepEqual(s.DEFAULT_MODE,EVE);
  assert.equal(s.defaultSemStart(TODAY),SEM);assert.equal(s.editedThrough('2026-09-28',SEM),'2026-09-28');
});

test('spec 1 auto generation creates exactly two and through/tombstones prevent recreation after deletion',()=>{
  const tt=timetable(),first=s.timetablePreview(tt,TODAY,SETTINGS,HOLIDAYS);
  assert.deepEqual(first.items.map(row=>row.title),['9/29(화) 창업과공동체 수업','9/29(화) 글로벌문화 수업']);
  assert.ok(first.items.every(row=>JSON.stringify(row.offsets)==='[0,1,3,7,14,30]'));
  assert.equal(first.through,TODAY);tt.through=TODAY;
  assert.deepEqual(s.timetablePreview(tt,TODAY,SETTINGS,HOLIDAYS).items,[]);
  tt.through=null;assert.deepEqual(s.timetablePreview(tt,TODAY,SETTINGS,HOLIDAYS,[],first.items.map(row=>row.source_key)).items,[]);
  assert.equal(s.timetablePreview(timetable(),TODAY,SETTINGS,HOLIDAYS,[{subject:'글로벌문화',date:TODAY}]).items.length,1);
  tt.from='2026-09-24';tt.through='2026-09-23';
  assert.deepEqual(s.timetablePreview(tt,'2026-09-24',SETTINGS,HOLIDAYS),{items:[],through:'2026-09-24'});
});

test('spec 2 rhythm offsets and spec 3 eve targets skip next-class holidays',()=>{
  for(const [subject,date,expected] of [['글로벌문화',TODAY,[0,1]],['창업과공동체',TODAY,[0,6]],['행정학의이해(eng)','2026-09-30',[0,6]]])
    assert.deepEqual(s.offsetsFor(subject,date,timetable(RHYTHM),SETTINGS,HOLIDAYS),expected);
  assert.deepEqual(s.offsetsFor('글로벌문화',TODAY,timetable(EVE),SETTINGS,HOLIDAYS),[1]);
  const targets={'행정학의이해(eng)':TODAY,'사회조사방법론I':TODAY,'현대사회와심리학':TODAY,
    '글로벌문화':'2026-09-30','공동체활성화론':'2026-09-30','창업과공동체':'2026-10-05'};
  for(const [subject,target] of Object.entries(targets))assert.equal(s.eveTarget(subject,TODAY,timetable(EVE),HOLIDAYS),target);
  assert.equal(s.nextClassDate('사회조사방법론I',TODAY,timetable(EVE),HOLIDAYS),s.addDays(TODAY,1));
});

test('spec 4 catchup has 38 selected sessions, five per day and explicit optional holiday selection',()=>{
  const tt=timetable(),rows=s.catchupPreview(tt,SEM,TODAY,SETTINGS,HOLIDAYS),plan=s.catchupPlan(rows,tt,TODAY,SETTINGS,HOLIDAYS,5);
  assert.equal(rows.length,40);assert.equal(plan.length,38);
  assert.ok(rows.filter(row=>row.date==='2026-09-24').every(row=>!row.selected&&row.holiday==='추석'));
  const counts=Object.fromEntries(Array.from({length:8},(_,i)=>[s.addDays(TODAY,i),i===7?3:5]));
  assert.deepEqual(plan.reduce((total,row)=>(total[row.base]=(total[row.base]||0)+1,total),{}),counts);
  assert.equal(plan[0].title,'9/1(화) 창업과공동체 수업');assert.deepEqual(plan[0].offsets,[0,1,3,7,14,30]);
  assert.ok(s.catchupPlan(rows,timetable(EVE),TODAY,SETTINGS,HOLIDAYS).every(row=>row.offsets.length===1&&row.offsets[0]===0&&row.base===s.eveTarget(row.subject,TODAY,timetable(EVE),HOLIDAYS)));
  assert.ok(s.catchupPlan(rows,tt,TODAY,SETTINGS,HOLIDAYS,0).every(row=>row.base===TODAY));
  rows[0].existing=true;for(const row of rows)if(row.holiday)row.selected=true;
  assert.equal(s.catchupPlan(rows,tt,TODAY,SETTINGS,HOLIDAYS).length,39);
  const oldThrough=tt.through;tt.from=SEM;assert.equal(tt.from,SEM);assert.equal(tt.through,oldThrough);
});

test('spec 5 exact midterm/final ranges have 13/15/9 sessions and Monday-based weeks',()=>{
  const expected=[[13,'2026-09-02','2026-10-19',1,8],[15,'2026-10-26','2026-12-14',9,16],[9,'2026-09-01','2026-10-01',1,5]];
  exams().forEach((exam,index)=>{
    const rows=s.examSessions(exam,SEM,timetable(),HOLIDAYS,exams(),[],TODAY);
    assert.deepEqual([rows.length,rows[0].date,rows.at(-1).date,rows[0].week,rows.at(-1).week],expected[index]);
    assert.ok(rows.every(row=>!['2026-09-24','2026-10-05'].includes(row.date)));
  });
  assert.equal(s.weekNo('2026-09-06',SEM),1);assert.equal(s.weekNo('2026-09-07',SEM),2);
  assert.throws(()=>s.validateExamOrder({...exams()[1],date:'2026-10-21'},exams()));
});

test('spec 6 cram totals 27/24 and marks support normalized booleans as well as 0/1',()=>{
  const exam=exams()[2],rows=s.examSessions(exam,SEM,timetable(),HOLIDAYS,exams(),[],TODAY);
  let stats=s.examStats(exam,rows,TODAY);assert.deepEqual([stats.phase,stats.days_left,stats.total,stats.checkable],['cram',7,27,24]);
  exam.done={[rows[0].key]:[1,0,0],[rows[1].key]:[true,false,false]};stats=s.examStats(exam,rows,TODAY);
  assert.deepEqual([stats.remaining,stats.target,stats.per,stats.next_key],[25,4,[2,0,0],rows[2].key]);
  for(const [today,phase] of [['2026-09-28','before'],['2026-10-06','today'],['2026-10-07','over']])assert.equal(s.examStats(exam,rows,today).phase,phase);
});

test('exam source keys deduplicate timetable rows and skipped/future/manual sessions keep their meaning',()=>{
  const exam=exams()[2],rows=s.examSessions(exam,SEM,timetable(),HOLIDAYS,exams(),[],TODAY);
  const inputs=[item({source:'timetable',source_key:rows[0].key,subject:exam.subject,learned:rows[0].date,skipped:TODAY}),
    item({id:'manual',subject:exam.subject,learned:'2026-10-02'})];
  const result=s.examSessions(exam,SEM,timetable(),HOLIDAYS,exams(),inputs,TODAY);
  assert.equal(result.length,10);assert.equal(result[0].skipped,true);assert.equal(result.at(-1).future,true);
  assert.equal(s.examStats(exam,result,TODAY).total,27);
  const until={...timetable(),until:TODAY};assert.equal(s.examSessions(exam,SEM,until,HOLIDAYS,exams(),[],TODAY).length,8);
});

test('spec 7 skipped records disappear from all due projections and unskip restores their schedule',()=>{
  const original=item(),before=structuredClone(original),skipped=s.skipItem(original,TODAY);
  assert.equal(s.nextDue(skipped),null);assert.deepEqual(s.projected(skipped,TODAY),[]);assert.deepEqual(s.dueItems([skipped],TODAY),[]);
  assert.equal(s.nextDue(s.unskipItem(skipped,TODAY)),s.nextDue(original));assert.deepEqual(original,before);
  const reset=s.resetItem(item({catchup:true,moved:{stage:0,date:TODAY}}),TODAY);
  assert.deepEqual([reset.base,reset.reviews,reset.resets,reset.moved,reset.catchup],[TODAY,[],1,null,false]);
});

test('spec 8 late completion shifts remaining gaps and projection starts future steps from today',()=>{
  const row=item({reviews:['2026-09-21']});assert.equal(s.nextDue(row),'2026-09-23');
  assert.deepEqual(s.projected(row,TODAY),[{stage:1,date:'2026-09-23',overdue:true},{stage:2,date:'2026-10-03',overdue:false}]);
  assert.equal(s.nextDue(s.completeReview(row,'2026-09-25')),TODAY);
});

test('spec 9 move to future/today and backdated catchup, edit and undo preserve stages and bounds',()=>{
  const rows=s.timetablePreview(timetable(),TODAY,SETTINGS,HOLIDAYS).items;
  const moved=s.placeReview(rows[1],'2026-09-30',TODAY);assert.deepEqual(moved.moved,{stage:0,date:'2026-09-30'});
  assert.deepEqual(s.dueItems([moved],TODAY),[]);assert.deepEqual(s.projected(moved,TODAY).slice(0,2).map(row=>row.date),['2026-09-30','2026-10-01']);
  assert.equal(s.dueItems([s.placeReview(moved,TODAY,TODAY)],TODAY).length,1);
  assert.deepEqual(s.placeReview(moved,'2026-09-30',TODAY),moved);
  assert.throws(()=>s.placeReview(rows[0],'2026-09-28',TODAY));
  const caught=item({learned:SEM,base:'2026-10-02',offsets:[0,1,3,7,14,30],catchup:true});
  const done=s.placeReview(caught,'2026-09-02',TODAY);assert.deepEqual(done.reviews,['2026-09-02']);assert.equal(s.nextDue(done),'2026-09-03');
  const edited=s.editReviewDate(done,0,SEM,TODAY);assert.deepEqual(edited.history.at(-1),{date:SEM,type:'review',stage:0});
  assert.deepEqual(s.undoLastReview(edited).reviews,[]);
  assert.throws(()=>s.editReviewDate(done,0,'2026-08-31',TODAY));assert.throws(()=>s.editReviewDate(done,0,'2026-09-30',TODAY));
});

test('spec 10 subject groups order 7 plus 2 records and mutations preserve the full undo snapshot',()=>{
  const subjects=['2026-09-28','2026-09-23','2026-09-21','2026-09-16','2026-09-14','2026-09-09','2026-09-07'].map((learned,i)=>item({id:`s${i}`,learned}));
  subjects.push(...['2026-09-09','2026-09-07'].map((learned,i)=>item({id:`a${i}`,learned,subject:'행정학의이해(eng)'})));
  const before=structuredClone(subjects),groups=s.groupDueItems(subjects,TODAY);
  assert.deepEqual(groups.map(row=>[row.subject,row.items.length]),[['사회조사방법론I',7],['행정학의이해(eng)',2]]);
  assert.deepEqual(groups[0].items.slice(0,3).map(row=>row.learned),['2026-09-07','2026-09-09','2026-09-14']);
  assert.ok(groups[0].items.map(row=>s.completeReview(row,TODAY)).every(row=>row.reviews[0]===TODAY));
  assert.deepEqual(subjects,before);
});

test('spec 11 routine calendar numbers, holiday run, counts, streaks, and future-date prevention',()=>{
  let routine={name:'단어',days:[],start:'2026-09-28',end:null,count:6,numbered:true,num_start:1,done:[]};
  for(const [day,n] of [['2026-09-28',1],[TODAY,2],['2026-10-03',6],['2026-10-04',0]]){assert.equal(s.routineN(routine,day),n);if(n)assert.equal(s.routineTitle(routine,n),`단어 (${n})`);}
  routine=s.toggleRoutine(s.toggleRoutine(routine,'2026-09-28',TODAY,true),TODAY,TODAY,true);
  assert.equal(s.routineStats(routine,TODAY).total,2);assert.equal(s.routineStats(routine,TODAY).streak,2);
  const thu={...routine,days:[2,4],start:TODAY,count:0,done:[]};
  assert.deepEqual([TODAY,'2026-10-01','2026-10-05'].map(day=>s.routineN(thu,day)),[1,2,0]);
  assert.throws(()=>s.toggleRoutine(thu,'2026-10-01',TODAY,true));assert.equal(s.routineStats(routine,'2026-10-04').ended,true);
  assert.equal(s.routineStats({...routine,start:'2026-12-01',done:[]},TODAY).ended,false);
});

test('retention is explicitly an estimate using learned date for unreviewed catchup',()=>{
  assert.ok(Math.abs(s.retention(item({catchup:true,learned:SEM,base:'2026-10-02'}),TODAY)-Math.exp(-28))<1e-12);
  assert.ok(Math.abs(s.retention(item({reviews:['2026-09-28']}),TODAY)-Math.exp(-1/2.5))<1e-12);
  const row=item({source:'timetable',subject:'창업과공동체',catchup:true,learned:SEM,base:'2026-10-02'});
  const result=s.applyMode(row,timetable(EVE),SETTINGS,HOLIDAYS,TODAY);assert.equal(result.base,'2026-10-05');assert.deepEqual(result.offsets,[0]);
  const completed=item({source:'timetable',reviews:['2026-09-21','2026-09-23',TODAY]});assert.deepEqual(s.applyMode(completed,timetable(EVE),SETTINGS,HOLIDAYS,TODAY),completed);
});

test('spec 12 tolerant parser keeps Korean/English days, canonical time, and safe bounded text',()=>{
  const rows=s.cleanClasses([{subject:'글로벌문화',day:'화',start:'13:00',end:'14:15'},
    {subject:'월 수업',day:'월요일',start:'15시',end:'14:00'},{subject:'Thu',day:'Thu',start:'09:00'},
    {subject:'',day:1},{subject:'bool',day:true},{subject:'safe\x00',day:0}]);
  assert.deepEqual(rows[0],{subject:'글로벌문화',day:2,start:'13:00',end:'14:15',room:''});
  assert.deepEqual([rows[1].day,rows[1].start,rows[1].end,rows[2].day],[1,'15:00','',4]);
  assert.equal(rows.length,4);assert.equal(rows.at(-1).subject,'safe');assert.equal(s.cleanClasses(Array(70).fill(rows[0])).length,60);
  for(const invalid of ['25:00','12:61','13:00 junk','<script>','١٣:٠٠'])assert.equal(s.normTime(invalid),'');
});

test('holiday source is user supplied, May Day omitted, no-next-class fallback and ten-year limit explicit',()=>{
  assert.equal(HOLIDAYS.source,'user-provided');assert.equal(s.holidayMap(HOLIDAYS)['2026-05-01'],undefined);
  assert.deepEqual(s.offsetsFor('없는 과목',TODAY,timetable(EVE),SETTINGS,HOLIDAYS),[1]);
  assert.deepEqual(s.catchupOffsets(timetable({sameDay:false,nextDay:false,eve:false,curve:true}),{offsets:[0,1]}),[0]);
  assert.equal(s.nextClassDate('글로벌문화',TODAY,{...timetable(),until:TODAY},HOLIDAYS),null);
  assert.throws(()=>s.catchupPreview(timetable(),'2000-01-01',TODAY,SETTINGS,HOLIDAYS));
});
