import assert from 'node:assert/strict';
import {test} from 'node:test';
import {performance} from 'node:perf_hooks';
import {groupTranscriptSentences} from '../web/transcript-sentences.js';

const chunks = texts => texts.map((text, index) => ({id: `s${index}`, text, start: index * 2, end: index * 2 + 2}));

test('Korean grammatical chunk boundaries become a sentence with every original source', () => {
  const input = chunks(['오늘 찍은 사진이라', '이라면 어제 전송이 처음 멈춘 문제', '와 오늘 복구 버튼이 잠긴 문제를 구분해야 합니', '다.']);
  const original = structuredClone(input);
  input.forEach(Object.freeze); Object.freeze(input);
  const rows = groupTranscriptSentences(input);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].text, '오늘 찍은 사진이라면 어제 전송이 처음 멈춘 문제와 오늘 복구 버튼이 잠긴 문제를 구분해야 합니다.');
  assert.deepEqual(rows[0].sourceIds, ['s0', 's1', 's2', 's3']);
  assert.deepEqual([rows[0].start, rows[0].end, rows[0].complete], [0, 8, true]);
  assert.deepEqual(input, original);
});

test('multiple sentences in one chunk and the unfinished live tail have stable, unique keys', () => {
  const input = chunks(['첫 문장입니다. 두 번째 문장이', '이어집니다! 마지막']);
  const before = groupTranscriptSentences(input.slice(0, 1));
  const after = groupTranscriptSentences(input);
  assert.deepEqual(after.map(row => row.text), ['첫 문장입니다.', '두 번째 문장이 이어집니다!', '마지막']);
  assert.equal(before[0].id, after[0].id);
  assert.equal(before[1].id, after[1].id);
  assert.equal(new Set(after.map(row => row.id)).size, 3);
  assert.deepEqual(after.map(row => row.sourceIds), [['s0'], ['s0', 's1'], ['s1']]);
  assert.deepEqual(after.map(row => row.complete), [true, true, false]);
});

test('late source insertion keeps completed sentence keys stable despite shifted source indexes', () => {
  const input = chunks(['첫 문장입니다.', '마지막 구간입니다. 이 구간의 두 번째 문장입니다.']);
  const before = groupTranscriptSentences(input);
  const after = groupTranscriptSentences([
    input[0], {id: 'late-boundary', text: '나중에 도착한 중간 문장입니다.', start: 1, end: 2}, input[1],
  ]);
  assert.equal(after.length, 4);
  for (const row of before) {
    assert.equal(row.complete, true);
    assert.equal(after.find(candidate => candidate.text === row.text)?.id, row.id);
  }
});

test('duplicate source IDs and differently typed IDs still yield distinct sentence keys', () => {
  const input = [
    {id: 'repeated', text: '첫 번째 등장입니다.'},
    {id: 'repeated', text: '두 번째 등장입니다. 다음 문장입니다.'},
    {id: 1, text: '숫자 식별자입니다.'},
    {id: '1', text: '문자 식별자입니다.'},
  ];
  const rows = groupTranscriptSentences(input);
  assert.equal(rows.length, 5);
  assert.equal(new Set(rows.map(row => row.id)).size, rows.length);
  assert.deepEqual(rows.map(row => row.sourceIds), [['repeated'], ['repeated'], ['repeated'], [1], ['1']]);
  const after = groupTranscriptSentences([input[0], {id: 'inserted', text: '중간에 추가됩니다.'}, ...input.slice(1)]);
  for (const row of rows) assert.equal(after.find(candidate => candidate.text === row.text)?.id, row.id);
});

test('sentence terminators keep closing quotes and punctuation together', () => {
  assert.deepEqual(groupTranscriptSentences(chunks(['그는 “맞나요?!” 물었습니다. 다음입니다。真的嗎？是的！']))
    .map(row => row.text), ['그는 “맞나요?!”', '물었습니다.', '다음입니다。', '真的嗎？', '是的！']);
});

test('decimals, addresses, and common English abbreviations stay readable', () => {
  const input = 'Dr. Kim met Ms. Lee in the U.S. at 3.14 p.m. See e.g. https://example.com/a?q=ok!yes and foo@example.com. Done!';
  assert.deepEqual(groupTranscriptSentences(chunks([input])).map(row => row.text), [
    'Dr. Kim met Ms. Lee in the U.S. at 3.14 p.m. See e.g. https://example.com/a?q=ok!yes and foo@example.com.', 'Done!',
  ]);
});

test('only narrow Korean boundary repairs apply, preserving actual repetitions', () => {
  assert.equal(groupTranscriptSentences(chunks(['좋아', '좋아 정말', '정말 좋아요.']))[0].text, '좋아 좋아 정말 정말 좋아요.');
  assert.equal(groupTranscriptSentences(chunks(['책', '을 읽었습', '니다.']))[0].text, '책을 읽었습니다.');
  assert.equal(groupTranscriptSentences(chunks(['사', '진을 봅니다.']))[0].text, '사 진을 봅니다.');
  assert.equal(groupTranscriptSentences(chunks(['그러니까', '그러니까 다시 말합니다.']))[0].text, '그러니까 그러니까 다시 말합니다.');
});

test('blank lines separate paragraphs and internal newlines survive', () => {
  assert.deepEqual(groupTranscriptSentences(chunks(['첫 줄\n둘째 줄\n\n새 단락입니다.'])).map(row => row.text), ['첫 줄\n둘째 줄', '새 단락입니다.']);
  assert.deepEqual(groupTranscriptSentences(chunks(['첫 줄\n', '둘째 줄입니다.'])).map(row => row.text), ['첫 줄\n둘째 줄입니다.']);
});

test('large temporal gaps separate unfinished fragments, missing times never become zero', () => {
  const input = [{id: 'a', text: '앞부분', start: 0, end: 2}, {id: 'b', text: '다음 주제', start: 13, end: 15}];
  assert.deepEqual(groupTranscriptSentences(input).map(row => row.text), ['앞부분', '다음 주제']);
  const rows = groupTranscriptSentences([{id: 0, text: '시간 없이', start: null, end: ''}, {id: 1, text: '이어집니다.', start: undefined, end: null}]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].start, undefined); assert.equal(rows[0].end, undefined);
  assert.deepEqual(rows[0].sourceIds, [0, 1]);
  assert.equal(groupTranscriptSentences([{text: '숫자 시간.', start: '2.5', end: '5'}])[0].start, 2.5);
});

test('malformed rows are ignored, literal markup is returned without interpretation', () => {
  assert.deepEqual(groupTranscriptSentences(null), []);
  const rows = groupTranscriptSentences([null, {}, {text: 123}, {text: '  '}, {id: 'x', text: '<img src=x onerror=alert(1)> **문장입니다.**'}]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].text, '<img src=x onerror=alert(1)> **문장입니다.**');
  assert.deepEqual(rows[0].sourceIds, ['x']);
  const unidentified = groupTranscriptSentences([{text: '출처 식별자 없는 문장. 다음 문장.'}]);
  assert.deepEqual(unidentified.map(row => row.sourceIds), [[], []]);
  assert.equal(new Set(unidentified.map(row => row.id)).size, 2);
});

test('long punctuationless speech forms bounded paragraphs without lost words or source references', () => {
  const input = chunks(Array.from({length: 1600}, (_, index) => `내용${index} 설명하는 중`));
  const started = performance.now();
  const rows = groupTranscriptSentences(input);
  assert.ok(performance.now() - started < 2000, 'grouping 1,600 live chunks should not stall the page');
  assert.ok(rows.length > 10);
  assert.ok(rows.every(row => row.text.length <= 800 && !row.complete));
  assert.equal(rows.map(row => row.text).join(' '), input.map(row => row.text).join(' '));
  assert.deepEqual([...new Set(rows.flatMap(row => row.sourceIds))], input.map(row => row.id));
  const more = groupTranscriptSentences([...input, ...chunks(['이어지는 내용']).map(row => ({...row, id: 'last', start: 3200, end: 3202}))]);
  assert.deepEqual(more.slice(0, -1).map(row => row.id), rows.slice(0, -1).map(row => row.id));
});

test('a long word is not truncated and Unicode characters survive paragraph boundaries', () => {
  const text = '😀'.repeat(1001);
  const rows = groupTranscriptSentences([{id: 'emoji', text}]);
  assert.equal(rows.map(row => row.text).join(''), text);
  assert.ok(rows.every(row => row.text.length <= 800 && !/[\uD800-\uDBFF]$/.test(row.text)));
});
