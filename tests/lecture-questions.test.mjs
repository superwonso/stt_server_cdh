import assert from 'node:assert/strict';
import {test} from 'node:test';
import {validQuestionJob,validQuestionPage} from '../web/lecture-questions.js';
const lecture={id:'lesson',segments:[{id:'source',start:1,end:2,text:'근거'}]};
const job={id:'11111111-1111-4111-8111-111111111111',lecture_id:'lesson',question:'설명해 줘',status:'completed',
  created_at:'2026-01-01T00:00:00Z',scope:'full',selected_count:1,total_segments:1,
  document:{answerability:'answered',paragraphs:[{text:'근거에 따른 설명',source_ids:['source']}]}};
test('question answers require the same lecture and real timestamp-linked source IDs',()=>{
  assert.equal(validQuestionJob(job,lecture),true);
  for(const change of [{lecture_id:'foreign'}, {scope:'full',selected_count:0},
    {document:{answerability:'answered',paragraphs:[{text:'invented',source_ids:['foreign']}]}}]) {
    assert.equal(validQuestionJob({...job,...change},lecture),false);
  }
});
test('insufficient evidence is explicit and cannot smuggle an uncited answer',()=>{
  const insufficient={...job,scope:'none',selected_count:0,document:{answerability:'insufficient_evidence',paragraphs:[]}};
  assert.equal(validQuestionJob(insufficient,lecture),true);
  assert.equal(validQuestionJob({...insufficient,document:{...insufficient.document,paragraphs:[{text:'guess'}]}},lecture),false);
});
test('question history bounds duplicates and pagination scope',()=>{
  const page={configured:true,model:'test-model',offset:0,total:1,has_more:false,questions:[job]};
  assert.equal(validQuestionPage(page,lecture),true);
  assert.equal(validQuestionPage(page,lecture,20),false);
  assert.equal(validQuestionPage({...page,questions:[job,job]},lecture),false);
});
test('retrieved evidence counts and timestamps cannot claim missing or duplicate raw sources',()=>{
  const insufficient={answerability:'insufficient_evidence',paragraphs:[]};
  for(const changes of [{scope:'retrieved',selected_count:0},{scope:'retrieved',selected_count:2},
    {scope:'none',selected_count:1},{id:'a'.repeat(36)},{cancel_requested:'yes'}]) {
    assert.equal(validQuestionJob({...job,document:insufficient,...changes},lecture),false);
  }
  for(const segments of [[{id:'source',start:NaN,end:2}], [{id:'source',start:2,end:1}],
    [{id:'source',start:-1,end:2}], [lecture.segments[0],lecture.segments[0]], [null]]) {
    assert.equal(validQuestionJob(job,{...lecture,segments}),false);
  }
  assert.equal(validQuestionJob({...job,document:insufficient},lecture),true);
  assert.equal(validQuestionJob({...job,scope:'retrieved',document:insufficient},lecture),true);
});
test('answer schema and aggregate citation count match the selected evidence contract',()=>{
  for(const document of [{...job.document,extra:'unvalidated'},
    {...job.document,paragraphs:[{...job.document.paragraphs[0],start:1}]},
    {...job.document,paragraphs:[{text:'가'.repeat(801),source_ids:['source']}]},
    {...job.document,paragraphs:[{text:'text',source_ids:['source','source']}]}]) {
    assert.equal(validQuestionJob({...job,document},lecture),false);
  }
  const two={...lecture,segments:[...lecture.segments,{id:'other',start:2,end:3,text:'다른 근거'}]};
  const mixed={...job,scope:'retrieved',total_segments:2,selected_count:1,document:{answerability:'answered',paragraphs:[
    {text:'first',source_ids:['source']},{text:'second',source_ids:['other']} ]}};
  assert.equal(validQuestionJob(mixed,two),false);
});
test('question pages reject null rows and inconsistent totals without throwing',()=>{
  const page={configured:true,model:'test-model',offset:0,limit:20,total:1,has_more:false,questions:[job]};
  for(const change of [{questions:[null]}, {questions:[undefined]}, {total:0}, {total:2},
    {has_more:true}, {offset:-1}, {limit:19}, {model:''}, {total:101}]) {
    assert.equal(validQuestionPage({...page,...change},lecture),false);
  }
  const second={...page,offset:20,total:21};
  assert.equal(validQuestionPage(second,lecture,20),true);
  assert.equal(validQuestionPage(second,lecture,0),false);
  assert.equal(validQuestionPage({...second,has_more:true},lecture,20),false);
});
test('empty-source abstention and terminal jobs remain displayable without fabricated prose',()=>{
  const empty={...job,scope:'none',selected_count:0,total_segments:0,
    document:{answerability:'insufficient_evidence',paragraphs:[]}};
  assert.equal(validQuestionJob(empty,{...lecture,segments:[]}),true);
  assert.equal(validQuestionJob({...empty,scope:'full'},{...lecture,segments:[]}),false);
  for(const status of ['queued','processing','failed','cancelled']) {
    assert.equal(validQuestionJob({...job,status,document:null,cancel_requested:true},lecture),true);
    assert.equal(validQuestionJob({...job,status},lecture),false);
  }
});
