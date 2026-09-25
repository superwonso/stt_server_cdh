import assert from 'node:assert/strict';
import { test } from 'node:test';
import { validateStudyNoteDocument, STUDY_NOTE_RESULT_WARNING } from '../web/study-notes.js';
import { renderUnifiedStudyNote, UNIFIED_NOTE_SEMANTIC_NOTICE } from '../web/unified-note-view.js';

class FakeNode {
  constructor(tag,document) { this.tag=tag;this.ownerDocument=document;this.children=[];this.listeners=new Map();this.ownText=''; }
  set innerHTML(value) { throw new Error('HTML must never be assigned'); }
  set textContent(value) { assert.equal(typeof value,'string');this.ownText=value;this.children=[]; }
  get textContent() { return this.ownText+this.children.map(child=>child.textContent).join(''); }
  append(...nodes) { assert.ok(nodes.every(node=>node instanceof FakeNode));this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=[];this.ownText='';this.append(...nodes); }
  addEventListener(type,callback) { this.listeners.set(type,callback); }
  click() { this.listeners.get('click')?.(); }
  scrollIntoView(options) { this.scrolled=options; }
}
const all = root => [root,...root.children.flatMap(all)];
const findClass = (root,className) => all(root).filter(node=>node.className===className);
function targetFixture() {
  const allowed=new Set(['div','section','p','h4','ul','li','details','summary','button','span','strong']);
  const document={createElement(tag) { assert.ok(allowed.has(tag),`Unexpected active element: ${tag}`);return new FakeNode(tag,document); }};
  return document.createElement('div');
}
function fixture() {
  const rows=[{id:'raw-a',start:1.25,end:7,text:'  **그대로인 표시** <img src=x>\n두 번째 줄\t  '},
    {id:'raw-b',start:8,end:12,text:'확인을 못한 부분'}, {id:'raw-c',start:60,end:63,text:'AI가 쓰지 못한 원문'}];
  const document={format:'unified_study_note',version:1,
    overview:[{text:'**전체 개요**',source_ids:['raw-a','raw-b']}],
    sections:rows.map((row,index)=>({heading:['상세 설명','받은 초안','원문만 남은 구간'][index],
      text:['**핵심**을 풀어 쓴 한국어 설명','일부 대응을 확인하지 못한 AI 문장',''][index],
      source_ids:[row.id],originals:[{...row}],status:['mapped','unverified','source_only'][index],
      edits:index===0?[{original:'원래 표현',replacement:'추정한 표현',uncertain:true},
        {original:'다른 원래 표현',replacement:'다른 제안',uncertain:false}]:[],
      warnings:index===0?[]:['incomplete_batches'],citations:index===0?['m:2']:[]})),
    supporting_sources:[{id:'m:2',label:'합성.pdf',kind:'pdf',index:2,text:'  보조 자료 **그대로**\n<script>자료 예시</script>  '},
      {id:'m:4',label:'합성.pptx',kind:'pptx',index:4,text:'별도의 슬라이드 내용'}],
    coverage:{source_count:3,preserved_count:3,mapped_count:1,unverified_count:1,fallback_count:1,complete:true,semantic_verified:false},
    warnings:['incomplete_batches']};
  const checked=validateStudyNoteDocument(document,{id:'synthetic',recording_finalized:true,segments:rows});
  assert.ok(checked);return checked;
}

test('unified view displays complete detailed content and exact collapsed originals at their section locations',()=>{
  const value=fixture(),before=structuredClone(value),target=targetFixture();
  const root=renderUnifiedStudyNote(value,target);
  assert.equal(target.children[0],root);assert.deepEqual(value,before);
  assert.match(root.textContent,/한눈에 보는 개요/);assert.match(root.textContent,/핵심을 풀어 쓴 한국어 설명/);
  assert.match(root.textContent,/원문 대응을 확인하지 못한 AI 초안/);assert.match(root.textContent,/이 구간은 원문만 보관되었습니다/);
  const originals=findClass(root,'study-note-originals');assert.equal(originals.length,3);
  originals.forEach((details,index)=>{
    assert.equal(details.open,false);
    assert.equal(findClass(details,'study-note-text')[0].textContent,value.sections[index].originals[0].text);
    assert.equal(all(details).some(node=>node.tag==='strong'),false);
    assert.equal(root.children.filter(node=>node.className==='study-note-paragraph')[index].children.at(-1),details);
  });
  assert.match(root.textContent,/추정 · 확인 필요 1곳/);assert.match(root.textContent,/AI 제안 · 원문 확인 권장 1곳/);
  assert.ok(root.textContent.includes('원래 표현 → 추정한 표현'));
  assert.equal(root.textContent.split(STUDY_NOTE_RESULT_WARNING).length-1,1);
  assert.equal(root.children.at(-1).textContent.includes(UNIFIED_NOTE_SEMANTIC_NOTICE),true);
  assert.equal(root.textContent.includes('semantic_verified'),false);
  assert.equal(root.textContent.includes('incomplete_batches'),false);
});

test('source navigation uses checked ranges and fresh IDs while no navigation callbacks are invented',()=>{
  const value=fixture(),target=targetFixture(),calls=[];
  renderUnifiedStudyNote(value,target,{onSeek:(start,ids)=>{calls.push([start,[...ids]]);ids.push('caller mutation');}});
  const buttons=all(target).filter(node=>node.tag==='button'&&node.textContent.startsWith('원문 '));
  assert.equal(buttons.length,4);assert.ok(buttons.every(button=>button.type==='button'));
  buttons[0].click();buttons[0].click();buttons[3].click();
  assert.deepEqual(calls,[[1.25,['raw-a','raw-b']],[1.25,['raw-a','raw-b']],[60,['raw-c']]]);
  assert.deepEqual(value.overview[0].source_ids,['raw-a','raw-b']);
  renderUnifiedStudyNote(value,target);
  assert.equal(all(target).some(node=>node.tag==='button'&&node.textContent.startsWith('원문 ')),false);
});

test('material citations expand the exact labeled supporting source without external navigation',()=>{
  const value=fixture(),target=targetFixture();renderUnifiedStudyNote(value,target);
  const materials=findClass(target,'study-note-material');assert.equal(materials.length,2);
  assert.equal(materials[0].children[0].textContent,'합성.pdf · 2쪽');
  assert.equal(materials[1].children[0].textContent,'합성.pptx · 슬라이드 4');
  assert.equal(materials[0].children[1].textContent,value.supporting_sources[0].text);
  assert.match(target.textContent,/수업 발언과 별도/);
  const citation=all(target).find(node=>node.tag==='button'&&node.textContent==='합성.pdf · 2쪽');
  assert.equal(materials[0].open,false);citation.click();assert.equal(materials[0].open,true);
  assert.deepEqual(materials[0].scrolled,{block:'nearest'});assert.equal(materials[1].open,false);
});

test('all untrusted text remains inert and incoming markup never becomes executable DOM or resource links',()=>{
  const value=fixture(),target=targetFixture();
  const attack='<script>alert(1)</script><img src=https://invalid.example/x> [link](javascript:alert(1))';
  value.overview[0].text=attack;value.sections[0].heading=attack;value.sections[0].text=`**강조** ${attack}`;
  value.sections[0].edits[0].replacement=attack;value.supporting_sources[0].label=attack;
  renderUnifiedStudyNote(value,target);
  assert.ok(target.textContent.includes(attack));
  assert.ok(all(target).every(node=>!Object.hasOwn(node,'href')&&!Object.hasOwn(node,'src')&&!Object.hasOwn(node,'onclick')));
  assert.ok(all(target).filter(node=>node.tag==='strong').some(node=>node.textContent==='강조'));
});

test('every render replaces prior content once and clean output still states semantic limits without a warning footer',()=>{
  const target=targetFixture(),first=renderUnifiedStudyNote(fixture(),target),value=fixture();
  value.overview=[];value.warnings=[];value.supporting_sources=[];
  value.sections=value.sections.slice(0,1);value.sections[0].citations=[];
  value.coverage={source_count:1,preserved_count:1,mapped_count:1,unverified_count:0,fallback_count:0,complete:true,semantic_verified:false};
  const second=renderUnifiedStudyNote(value,target);
  assert.notEqual(first,second);assert.deepEqual(target.children,[second]);
  assert.equal(findClass(second,'study-note-overview').length,0);
  assert.equal(second.textContent.includes(STUDY_NOTE_RESULT_WARNING),false);
  assert.equal(second.textContent.includes(UNIFIED_NOTE_SEMANTIC_NOTICE),true);
  assert.match(second.textContent,/원문 1개 구간을 모두 보존/);
});

test('invalid formats or unknown material citations cannot clear the previous view',()=>{
  const target=targetFixture(),first=renderUnifiedStudyNote(fixture(),target);
  for(const change of [value=>value.format='draft',value=>value.version=9,value=>value.coverage.semantic_verified=true,
    value=>value.sections=[],value=>value.sections[0].citations=['foreign']]) {
    const value=fixture();change(value);assert.throws(()=>renderUnifiedStudyNote(value,target),TypeError);
    assert.deepEqual(target.children,[first]);
  }
});
