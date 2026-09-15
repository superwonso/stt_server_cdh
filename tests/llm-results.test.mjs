import assert from 'node:assert/strict';
import {test} from 'node:test';
import {RESULT_WARNING,validResultDraft,validResultWarnings} from '../web/llm-results.js';
const draft=()=>({format:'draft',text:'받은 본문입니다.\n마지막 문장.',warnings:['validation_failed']});
test('received result draft keeps prose and fixed diagnostic codes without claiming sources',()=>{
  const original=draft(),checked=validResultDraft(original);assert.deepEqual(checked,original);
  checked.warnings.push('response_truncated');assert.equal(original.warnings.length,1);
  assert.equal(RESULT_WARNING,'일부 내용을 확인하지 못했습니다. 원문과 함께 확인해 주세요.');
  assert.equal(validResultWarnings(['context_unverified','content_limited']),true);
});
test('untrusted draft envelopes and excessive or control-character content are rejected',()=>{
  for(const mutate of [d=>d.format='unchecked',d=>d.text='',d=>d.text+='\u0000',d=>d.text+='\u0080',
    d=>d.text='x'.repeat(250001),d=>d.source_ids=['made-up'],d=>d.warnings=[],
    d=>d.warnings=['unknown provider text'],d=>d.warnings.push('validation_failed')]){
    const value=draft();mutate(value);assert.equal(validResultDraft(value),null);
  }
  const max=draft();max.text='😀'.repeat(250000);assert.ok(validResultDraft(max));
  assert.equal(validResultDraft(null),null);
});
