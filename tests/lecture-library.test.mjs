import assert from 'node:assert/strict';
import {test} from 'node:test';
import {lectureTitle,filterLibrary,libraryOptions,validMetadata,validLibrarySearch} from '../web/lecture-library.js';

test('display names never overwrite the title used by capture replay', () => {
  const lecture = {title:'처음 이름',display_title:'바꾼 이름'};
  assert.equal(lectureTitle(lecture),'바꾼 이름'); assert.equal(lecture.title,'처음 이름');
  assert.equal(lectureTitle({title:'처음 이름',display_title:''}),'처음 이름');
});
test('course and semester filters compose without modifying owner-scoped input', () => {
  const lectures = [{id:1,course:'통계',semester:'2026-2'}, {id:2,course:'통계',semester:'2026-1'}, {id:3}];
  assert.deepEqual(filterLibrary(lectures,{course:'통계',semester:'2026-2'}),[lectures[0]]);
  assert.deepEqual(libraryOptions(lectures,'course'),['통계']);
  assert.deepEqual(libraryOptions(lectures,'title'),[]); assert.equal(lectures.length,3);
});
test('unfiltered library data keeps all classes and classification suggestions without changing stored metadata', () => {
  const lectures=[{id:1,course:'통계',semester:'2026-2'},{id:2,course:'물리',semester:'2025-1'},{id:3}];
  const before=structuredClone(lectures);
  assert.deepEqual(filterLibrary(lectures),lectures);
  assert.deepEqual(filterLibrary(lectures,{course:'',semester:''}),lectures);
  assert.deepEqual(libraryOptions(lectures,'semester'),['2025-1','2026-2']);
  assert.deepEqual(lectures,before);
});
test('metadata and search responses are bounded and require matching identities', () => {
  const id = '11111111-1111-4111-8111-111111111111';
  const metadata = {lecture_id:id,display_title:'수업',course:'',semester:'',revision:0};
  assert.equal(validMetadata(metadata,id),true);
  assert.equal(validMetadata(metadata,'another'),false);
  assert.equal(validMetadata({...metadata,revision:-1},id),false);
  const item = {lecture_id:id,display_title:'수업',snippet:'<script>not HTML</script>',created_at:'2026-01-01T00:00:00Z',source:'raw',segment_id:'s',start:1,end:2};
  const result = {items:[item],has_more:false,offset:0,limit:20};
  assert.equal(validLibrarySearch(result),true);
  assert.equal(validLibrarySearch({...result,items:[{...item,start:NaN}]}),false);
  assert.equal(validLibrarySearch({...result,items:Array(51).fill(item)}),false);
});
