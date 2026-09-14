import assert from 'node:assert/strict';
import { test } from 'node:test';
import { studyNoteSourceSnapshot, validateStudyNoteDocument, validateStudyNoteResponse, appendStudyNoteText } from '../web/study-notes.js';

const lecture = () => ({id:'lesson-fixture',recording_finalized:true,segments:[
  {id:'raw-one',start:1.25,end:7,text:'개념을 살펴봅시다.'},
  {id:'raw-two',start:7,end:12.5,text:'조건이 흐릿해요. 사례를 비교합니다.'},
]});
const documentFixture = () => ({paragraphs:[{heading:'개념과 조건',source_ids:['raw-one','raw-two'],
  text:'**개념**을 살펴봅니다. 조건이 불명확해요. 사례를 비교합니다.',
  edits:[{original:'살펴봅시다',replacement:'살펴봅니다',uncertain:false},
    {original:'흐릿해요',replacement:'불명확해요',uncertain:true}]}]});
const envelope = () => ({configured:true,model:'synthetic-model',study_note:{lecture_id:'lesson-fixture',
  status:'completed',model:'synthetic-model',error_code:null,error:null,created_at:'2026-01-01T00:00:00Z',
  updated_at:'2026-01-01T00:00:01Z',completed_at:'2026-01-01T00:00:01Z',document:documentFixture(),markdown:'# 수업 정리본\n\n본문'}});

test('study-note source snapshot binds every raw ID time text and finalization', () => {
  const source = lecture(), snapshot = studyNoteSourceSnapshot(source);
  for (const mutate of [s=>s.segments[0].text+='다름',s=>s.segments[0].start=0,s=>s.segments[0].id='different',s=>s.segments.reverse()]) {
    const other=lecture(); mutate(other); assert.notEqual(studyNoteSourceSnapshot(other),snapshot);
  }
  assert.equal(studyNoteSourceSnapshot({...source,recording_finalized:false}),null);
  for (const mutate of [s=>s.segments[1].id='raw-one',s=>s.segments[0].end=NaN,s=>s.segments[0].start=-1,s=>s.segments[0].text='']) {
    const other=lecture(); mutate(other); assert.equal(studyNoteSourceSnapshot(other),null);
  }
});

test('validated study-note paragraphs preserve contiguous complete raw coverage and source ranges', () => {
  const original=documentFixture(), checked=validateStudyNoteDocument(original,lecture());
  assert.ok(checked); assert.equal(checked.paragraphs[0].start,1.25); assert.equal(checked.paragraphs[0].end,12.5);
  checked.paragraphs[0].source_ids[0]='changed'; checked.paragraphs[0].edits[0].original='changed';
  assert.equal(original.paragraphs[0].source_ids[0],'raw-one'); assert.equal(original.paragraphs[0].edits[0].original,'살펴봅시다');
  for (const ids of [['raw-one'],['raw-two','raw-one'],['raw-one','raw-one'],['raw-one','foreign'],['raw-one','raw-two','raw-two']]) {
    const doc=documentFixture(); doc.paragraphs[0].source_ids=ids;
    assert.equal(validateStudyNoteDocument(doc,lecture()),null);
  }
});

test('study notes accept restored numbers contacts and terms absent from the raw transcript', () => {
  const doc=documentFixture();
  Object.assign(doc.paragraphs[0],{
    text:'**개념과 조건**을 살펴봅니다. 표본 250개를 비교합니다. 연락처는 010-0000-1234입니다. 라그랑주 승수법을 설명합니다.',
    edits:[
      {original:'이백오십',replacement:'250',uncertain:true},
      {original:'연락 가능한 번호',replacement:'010-0000-1234',uncertain:true},
      {original:'라그랑쥐안',replacement:'라그랑주 승수법',uncertain:true},
    ],
  });
  const checked=validateStudyNoteDocument(doc,lecture());
  assert.ok(checked);
  assert.equal(checked.paragraphs[0].text,doc.paragraphs[0].text);
  assert.deepEqual(checked.paragraphs[0].edits,doc.paragraphs[0].edits);
  const response=envelope(); response.study_note.document=doc;
  assert.ok(validateStudyNoteResponse(response,lecture()));
});

test('restoration annotations do not require literal original or replacement substrings', () => {
  const doc=documentFixture();
  doc.paragraphs[0].edits=[{original:'원문에서 나뉘어 인식된 표현',replacement:'문맥에 맞게 복원한 개념',uncertain:false}];
  const checked=validateStudyNoteDocument(doc,lecture());
  assert.ok(checked);
  assert.deepEqual(checked.paragraphs[0].edits,doc.paragraphs[0].edits);
});

test('edit markers keep bounded nonempty strings explicit boolean uncertainty and no duplicates', () => {
  for (const mutate of [p=>p.edits[0].uncertain=1,p=>p.edits[0].original=p.edits[0].replacement,
    p=>p.edits[0].original='',p=>p.edits[0].replacement=' ',p=>p.edits[0]=null,
    p=>p.edits.push({...p.edits[0]}),p=>p.edits[0].original='x'.repeat(257),p=>p.edits[0].replacement='x'.repeat(257),p=>p.heading='x'.repeat(121),
    p=>p.text='x'.repeat(24001),p=>p.edits=Array(17).fill(p.edits[0])]) {
    const doc=documentFixture(); mutate(doc.paragraphs[0]); assert.equal(validateStudyNoteDocument(doc,lecture()),null);
  }
});

test('paragraph size counts the same source-group newline separators as the server', () => {
  const source={id:'long-fixture',recording_finalized:true,segments:Array.from({length:64},(_,i)=>({id:`s-${i}`,start:i,end:i+1,text:'가'}))};
  const doc={paragraphs:[{heading:'개념',source_ids:source.segments.map(s=>s.id),text:'나'.repeat(1300),edits:[]}]};
  assert.ok(validateStudyNoteDocument(doc,source));
  doc.paragraphs[0].text='나'.repeat(1382);assert.equal(validateStudyNoteDocument(doc,source),null);
});

test('Unicode astral symbols count as server code points rather than UTF-16 units', () => {
  const source={id:'unicode-fixture',recording_finalized:true,segments:[{id:'unicode-raw',start:0,end:1,text:'😀'.repeat(24000)}]};
  const doc={paragraphs:[{heading:'😀'.repeat(120),source_ids:['unicode-raw'],text:source.segments[0].text,edits:[]}]};
  assert.ok(studyNoteSourceSnapshot(source));assert.ok(validateStudyNoteDocument(doc,source));
  doc.paragraphs[0].heading+='😀';assert.equal(validateStudyNoteDocument(doc,source),null);
});

test('study-note envelope rejects wrong lecture missing metadata malformed jobs and unexpected artifacts', () => {
  assert.ok(validateStudyNoteResponse(envelope(),lecture()));
  for (const mutate of [e=>e.configured='true',e=>delete e.study_note,e=>e.study_note.lecture_id='foreign',
    e=>e.study_note.status='unknown',e=>e.study_note.created_at='invalid',e=>e.study_note.markdown='',
    e=>e.study_note.completed_at=null,e=>e.study_note.document.paragraphs[0].source_ids.reverse(),
    e=>e.study_note.status='processing',e=>delete e.study_note.error_code]) {
    const value=envelope(); mutate(value); assert.equal(validateStudyNoteResponse(value,lecture()),null);
  }
  const pending=envelope(); Object.assign(pending.study_note,{status:'queued',document:null,markdown:null,completed_at:null});
  assert.ok(validateStudyNoteResponse(pending,lecture()));
  assert.deepEqual(validateStudyNoteResponse({configured:false,model:'synthetic-model',study_note:null},lecture()),
    {configured:false,model:'synthetic-model',study_note:null});
});

test('only bold markers are styled and HTML images links are inert text without remote elements', () => {
  const nodes=[], parent={append(node){nodes.push(node);}}, document={createElement(tag){return {tag};}};
  appendStudyNoteText(parent,'**핵심 개념** <img src=x> [링크](https://invalid.example) ![그림](https://invalid.example/x) **미완성',document);
  assert.equal(nodes[0].tag,'strong'); assert.equal(nodes[0].textContent,'핵심 개념');
  assert.ok(nodes.every(node=>['span','strong'].includes(node.tag)));
  assert.match(nodes[1].textContent,/<img src=x>/); assert.match(nodes[1].textContent,/\*\*미완성$/);
});
