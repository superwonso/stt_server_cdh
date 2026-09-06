import assert from 'node:assert/strict';
import {test} from 'node:test';
import {validManualState,manualSegments,validManualHistory} from '../web/manual-notes.js';
const lecture = {id:'lesson',segments:[{id:'s1',start:1,end:2,text:'원문'}, {id:'s2',start:2,end:3,text:'다음 문장'}]};
const state = {lecture_id:'lesson',raw_revision:'a'.repeat(64),revision:1,notes:[],edits:[{segment_id:'s1',text:'직접 수정'}]};
test('manual transcript changes only selected text and preserves raw IDs and times', () => {
  assert.equal(validManualState(state,lecture),true);
  assert.deepEqual(manualSegments(lecture,state),[{id:'s1',start:1,end:2,text:'직접 수정'},lecture.segments[1]]);
  assert.equal(lecture.segments[0].text,'원문');
});
test('manual state refuses foreign, duplicate, oversized and unlinked data', () => {
  for (const invalid of [{...state,lecture_id:'foreign'}, {...state,raw_revision:''},
    {...state,edits:[...state.edits,...state.edits]}, {...state,edits:[{segment_id:'foreign',text:'text'}]},
    {...state,edits:[{segment_id:'s1',text:'x'.repeat(5001)}]},
    {...state,notes:[{id:'11111111-1111-4111-8111-111111111111',text:'note',start_seconds:Infinity}]}]) {
    assert.equal(validManualState(invalid,lecture),false); assert.equal(manualSegments(lecture,invalid),null);
  }
});
test('revision history is bounded and cannot claim invalid revisions', () => {
  const item={revision:1,created_at:'2026-01-01T00:00:00Z',action:'segment_edit',segment_id:'s1',note_id:null,start_seconds:1,text:'내용'};
  const result={lecture_id:lecture.id,raw_revision:state.raw_revision,revision:1,at_revision:1,items:[item],offset:0,limit:20,has_more:false};
  assert.equal(validManualHistory(result,lecture,state),true);
  for (const invalid of [{...result,lecture_id:'foreign'}, {...result,raw_revision:'b'.repeat(64)},
    {...result,items:[item,item]}, {...result,at_revision:0}, {...result,items:[{...item,segment_id:'foreign'}]},
    {...result,items:[{...item,text:'x'.repeat(5001)}]}, {...result,items:[{...item,action:'unrecognized'}]}]) {
    assert.equal(validManualHistory(invalid,lecture,state),false);
  }
  assert.equal(validManualHistory(result,lecture,state,{segment_id:'s2'}),false);
  assert.equal(validManualHistory(result,lecture,state,{note_id:'11111111-1111-4111-8111-111111111111'}),false);
});
