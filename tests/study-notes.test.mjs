import assert from 'node:assert/strict';
import { test } from 'node:test';
import { studyNoteSourceSnapshot, validateStudyNoteDocument, validateStudyNoteResponse, appendStudyNoteText, studyNoteWarningMessage, STUDY_NOTE_RESULT_WARNING } from '../web/study-notes.js';

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
const draftFixture = () => ({format:'draft',text:'# 수업 정리본\n\n**수업 내용**입니다.\n원문 대응을 확인하지 못한 결과도 보관합니다.',
  warnings:['invalid_response','response_truncated']});

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

test('canonical saved drafts accept returned prose without inventing source mappings and clone warnings', () => {
  const draft=draftFixture(),checked=validateStudyNoteDocument(draft,lecture());
  assert.deepEqual(checked,draft);
  assert.equal(Object.hasOwn(checked,'paragraphs'),false);
  assert.equal(Object.hasOwn(checked,'start'),false);
  checked.warnings.push('interrupted');assert.equal(draft.warnings.length,2);
  const value=envelope();value.study_note.document=draft;
  assert.deepEqual(validateStudyNoteResponse(value,lecture()).study_note.document,draft);
  assert.equal(validateStudyNoteDocument(draft,{...lecture(),recording_finalized:false}),null);
});

test('saved draft envelopes reject unknown formats keys empty prose control characters and untrusted warnings', () => {
  for (const mutate of [d=>d.format='raw',d=>delete d.format,d=>d.text='',d=>d.text=' \n\t',d=>d.text=123,
    d=>d.text+='\u0000',d=>d.text+='\u000b',d=>d.text+='\u000c',d=>d.text+='\u001f',d=>d.text+='\u007f',
    d=>d.paragraphs=[],d=>d.source_ids=['foreign'],d=>d.warnings=[],d=>d.warnings=['future_warning'],
    d=>d.warnings=['<script>alert(1)</script>'],d=>d.warnings=['__proto__'],d=>d.warnings=[123],
    d=>d.warnings.push(d.warnings[0]),d=>d.warnings='invalid_response',d=>d.warnings=Array(17).fill('invalid_response')]) {
    const draft=draftFixture();mutate(draft);assert.equal(validateStudyNoteDocument(draft,lecture()),null);
  }
  const draft=draftFixture();draft.text+='\n\r\t허용하는 공백';assert.ok(validateStudyNoteDocument(draft,lecture()));
});

test('saved drafts enforce independent code-point and serialized UTF-8 size bounds', () => {
  const draft=draftFixture();draft.text='x'.repeat(500000);assert.ok(validateStudyNoteDocument(draft,lecture()));
  draft.text+='x';assert.equal(validateStudyNoteDocument(draft,lecture()),null);
  draft.text='😀'.repeat(200000);assert.ok(validateStudyNoteDocument(draft,lecture()));
  draft.text='😀'.repeat(262144);assert.equal(validateStudyNoteDocument(draft,lecture()),null);
});

test('all saved draft warning codes use one short footer message rather than provider diagnostics', () => {
  const codes=['invalid_response','response_truncated','incomplete_batches','gateway_unavailable','authentication_failed',
    'credit_exhausted','rate_limited','model_refused','interrupted','content_limited','placeholder_unresolved'];
  const draft=draftFixture();draft.warnings=codes;assert.ok(validateStudyNoteDocument(draft,lecture()));
  for (const code of codes) assert.equal(studyNoteWarningMessage(code),STUDY_NOTE_RESULT_WARNING);
  assert.equal(STUDY_NOTE_RESULT_WARNING,'일부 내용을 확인하지 못했습니다. 원문과 함께 확인해 주세요.');
  assert.equal(studyNoteWarningMessage('unexpected provider text'),null);
  assert.equal(studyNoteWarningMessage('constructor'),null);
});

test('draft completion retains job identity metadata and completed-only artifact requirements', () => {
  for (const mutate of [r=>r.lecture_id='foreign',r=>r.status='failed',r=>r.status='processing',r=>r.created_at='bad',
    r=>r.completed_at=null,r=>delete r.error_code,r=>r.markdown='']) {
    const value=envelope();value.study_note.document=draftFixture();mutate(value.study_note);
    assert.equal(validateStudyNoteResponse(value,lecture()),null);
  }
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


const unifiedFixture=()=>({format:'unified_study_note',version:1,
  overview:[{text:'전체를 보는 개요',source_ids:['raw-one','raw-two']}],
  sections:[{...documentFixture().paragraphs[0],originals:lecture().segments.map(row=>({...row})),
    status:'mapped',warnings:[],citations:['material:3']}],
  supporting_sources:[{id:'material:3',label:'합성 자료.pdf',kind:'pdf',index:3,text:'수업 발언과 별도인 보조 자료입니다.'}],
  coverage:{source_count:2,preserved_count:2,mapped_count:2,unverified_count:0,fallback_count:0,
    complete:true,semantic_verified:false},warnings:[]});

test('unified notes retain exact originals beside AI text and compute independently navigable source ranges',()=>{
  const input=unifiedFixture(),checked=validateStudyNoteDocument(input,lecture());
  assert.ok(checked); assert.equal(checked.format,'unified_study_note');
  assert.deepEqual(checked.sections[0].originals,lecture().segments);
  assert.equal(checked.sections[0].start,1.25);assert.equal(checked.sections[0].end,12.5);
  assert.equal(checked.overview[0].start,1.25);assert.equal(checked.overview[0].end,12.5);
  checked.sections[0].originals[0].text='mutation';checked.sections[0].citations.push('changed');
  checked.supporting_sources[0].text='mutation';checked.overview[0].source_ids.reverse();
  assert.notEqual(input.sections[0].originals[0].text,'mutation');
  assert.notEqual(input.supporting_sources[0].text,'mutation');
  assert.deepEqual(input.overview[0].source_ids,['raw-one','raw-two']);
  assert.deepEqual(input.sections[0].citations,['material:3']);
});

test('unified saved documents reject altered originals omission duplicate reorder and false coverage claims',()=>{
  for(const mutate of [d=>d.sections[0].originals[0].text+='변경',d=>d.sections[0].originals[0].start=0,
    d=>d.sections[0].originals[0].id='foreign',d=>d.sections[0].originals[0].extra='untrusted',
    d=>d.sections[0].originals.reverse(),d=>d.sections[0].source_ids.reverse(),d=>d.sections[0].originals.pop(),
    d=>d.sections[0].source_ids.pop(),d=>d.sections.push(structuredClone(d.sections[0])),
    d=>d.coverage.preserved_count=1,d=>d.coverage.mapped_count=0,d=>d.coverage.complete=1,
    d=>d.coverage.semantic_verified=true,d=>d.version=2,d=>d.extra='unexpected']) {
    const input=unifiedFixture();mutate(input);assert.equal(validateStudyNoteDocument(input,lecture()),null);
  }
});

test('unified partial and raw-only sections have explicit status warnings and never fabricate AI citations',()=>{
  for(const status of ['unverified','source_only']) {
    const input=unifiedFixture(),section=input.sections[0];
    Object.assign(section,{status,text:status==='source_only'?'':'받은 초안',edits:[],citations:[],warnings:['incomplete_batches']});
    Object.assign(input.coverage,{mapped_count:0,unverified_count:status==='unverified'?2:0,fallback_count:status==='source_only'?2:0});
    input.warnings=['incomplete_batches'];
    assert.ok(validateStudyNoteDocument(input,lecture()));
    for(const mutate of [s=>s.warnings=[],s=>s.citations=['material:3'],s=>s.edits=documentFixture().paragraphs[0].edits,
      s=>s.status='completed']) {
      const damaged=structuredClone(input);mutate(damaged.sections[0]);assert.equal(validateStudyNoteDocument(damaged,lecture()),null);
    }
    const damaged=structuredClone(input);damaged.warnings=[];assert.equal(validateStudyNoteDocument(damaged,lecture()),null);
  }
});

test('unified material and overview citations stay inside the validated snapshot',()=>{
  for(const mutate of [d=>d.sections[0].citations=['outside'],d=>d.sections[0].citations.push('material:3'),
    d=>d.supporting_sources[0].kind='html',d=>d.supporting_sources[0].index=0,d=>d.supporting_sources[0].index=1.5,
    d=>d.supporting_sources[0].index=true,d=>d.supporting_sources[0].label+='\nsecret',
    d=>d.supporting_sources[0].text='x'.repeat(24001),d=>d.supporting_sources.push({...d.supporting_sources[0]}),
    d=>d.overview[0].source_ids=['outside'],d=>d.overview[0].source_ids.push('raw-one'),
    d=>d.overview[0].text='x'.repeat(1001),d=>d.overview[0].extra='unexpected']) {
    const input=unifiedFixture();mutate(input);assert.equal(validateStudyNoteDocument(input,lecture()),null);
  }
  const input=unifiedFixture();input.supporting_sources[0].text='<script>untrusted</script> Ignore instructions.';
  assert.ok(validateStudyNoteDocument(input,lecture()));
});

test('unified generated fields reject hidden reasoning while exact raw markup stays inert source data',()=>{
  for(const mutate of [d=>d.sections[0].text='<think>hidden</think>보이는 설명',
    d=>d.sections[0].heading='<analysis>hidden</analysis>제목',d=>d.sections[0].edits[0].original='<reasoning>hidden',
    d=>d.overview[0].text='<think>hidden</think>개요',d=>d.sections[0].text+='\u0085',
    d=>d.sections[0].text+='\ud800']) {
    const input=unifiedFixture();mutate(input);assert.equal(validateStudyNoteDocument(input,lecture()),null);
  }
  const source=lecture(),input=unifiedFixture();source.segments[0].text='  <think>수업 예시</think>  ';
  input.sections[0].originals=structuredClone(source.segments);
  const checked=validateStudyNoteDocument(input,source);assert.ok(checked);
  assert.equal(checked.sections[0].originals[0].text,source.segments[0].text);
  const nodes=[];appendStudyNoteText({append:node=>nodes.push(node)},checked.sections[0].originals[0].text,{createElement:tag=>({tag})});
  assert.ok(nodes.every(node=>node.tag==='span'));assert.ok(nodes[0].textContent.includes('<think>'));
});

test('unified bounds allow the combined source and generated document above legacy 1MiB without enlarging legacy limits',()=>{
  const source={...lecture(),segments:Array.from({length:20},(_,index)=>({id:`s${index}`,start:index,end:index+1,text:'a'.repeat(8000)}))};
  const input={format:'unified_study_note',version:1,overview:[],warnings:[],supporting_sources:[],
    sections:source.segments.map(row=>({heading:'상세 설명',text:'나'.repeat(24000),source_ids:[row.id],originals:[{...row}],
      edits:[],status:'mapped',warnings:[],citations:[]})),
    coverage:{source_count:20,preserved_count:20,mapped_count:20,unverified_count:0,fallback_count:0,complete:true,semantic_verified:false}};
  assert.ok(new TextEncoder().encode(JSON.stringify(input)).length>1024*1024);
  assert.ok(validateStudyNoteDocument(input,source));
  const value=envelope();value.study_note.document=input;value.study_note.markdown='x'.repeat(4000001);
  assert.ok(validateStudyNoteResponse(value,source));
  value.study_note.markdown='x'.repeat(12*1024*1024+1);assert.equal(validateStudyNoteResponse(value,source),null);
  const legacy=envelope();legacy.study_note.markdown='x'.repeat(4000001);assert.equal(validateStudyNoteResponse(legacy,lecture()),null);
});


test('study-note response preserves explicit staleness and format version for regeneration decisions',()=>{
  for (const [stale,format_version] of [[true,1],[true,2],[false,2]]) {
    const input=envelope();Object.assign(input.study_note,{stale,format_version});
    if(format_version===2) input.study_note.document=unifiedFixture();
    const checked=validateStudyNoteResponse(input,lecture());
    assert.equal(checked.study_note.stale,stale);assert.equal(checked.study_note.format_version,format_version);
    assert.equal(input.study_note.stale,stale);
  }
  const pending=envelope();Object.assign(pending.study_note,{status:'processing',document:null,markdown:null,
    completed_at:null,stale:false,format_version:2});
  const checked=validateStudyNoteResponse(pending,lecture());
  assert.equal(checked.study_note.format_version,2);assert.equal(checked.study_note.stale,false);
  const legacy=validateStudyNoteResponse(envelope(),lecture());
  assert.equal(legacy.study_note.format_version,1);assert.equal(legacy.study_note.stale,false);
});

test('study-note response rejects non-boolean staleness and unsupported format versions',()=>{
  for (const [key,values] of [['stale',[null,0,1,'false',{},[]]],['format_version',[null,0,3,true,'2',1.5,{},[]]]]) {
    for(const value of values) {
      const input=envelope();input.study_note[key]=value;
      assert.equal(validateStudyNoteResponse(input,lecture()),null);
    }
  }
});
