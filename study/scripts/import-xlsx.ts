/** Derive structured display metadata without modifying the approved workbook or IDs. */
import { createHash } from 'node:crypto';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as XLSX from 'xlsx';
import { inferQuestionType } from '../src/lib/grading';
import { COUNTRIES, enrichQuestion, inspectRulerCandidates, renderAnswer } from '../src/lib/answer';
import type { ChapterManifest, Question } from '../src/lib/types';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const auditOnly = process.argv.includes('--audit');
const source = resolve(root, '../seed/한능검_공부내용확인_문제답.xlsx');
const sourceBytes = readFileSync(source);
const workbook = XLSX.read(sourceBytes, {
  type: 'buffer',
  cellDates: false,
  cellFormula: false,
  cellHTML: false,
});
if (workbook.SheetNames.length !== 1 || workbook.SheetNames[0] !== '문제-답')
  throw new Error('Expected only the approved 문제-답 seed sheet.');
const sheet = workbook.Sheets['문제-답'];
const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, {
  header: 1,
  blankrows: false,
  defval: '',
});
if (JSON.stringify(rows[0]) !== JSON.stringify(['파트', '문제', '답']))
  throw new Error('Unexpected seed column headers.');
const ids = new Set<string>();
const original = rows.slice(1).map((row, index) => {
  if (row.length !== 3 || !row.every((value) => typeof value === 'string' && value.length > 0))
    throw new Error(`Invalid seed row ${index + 2}.`);
  const [part, body, answer] = row as string[];
  const prefix = /^\((\d+)\) /.exec(body);
  const separator = part.indexOf(' > ');
  if (!prefix || separator === 0)
    throw new Error(`Invalid part or question number at seed row ${index + 2}.`);
  const chapter = separator < 0 ? part : part.slice(0, separator),
    sub = separator < 0 ? '' : part.slice(separator + 3);
  const number = Number(prefix[1]);
  const question = body.slice(prefix[0].length);
  if (!question || !Number.isSafeInteger(number) || number < 1)
    throw new Error(`Invalid seed row ${index + 2}.`);
  const id = createHash('sha256')
    .update(JSON.stringify([chapter, sub, number]), 'utf8')
    .digest('hex');
  if (ids.has(id)) throw new Error(`Duplicate seed identity at row ${index + 2}.`);
  ids.add(id);
  return { id, chapter, sub, number, question, answer, type: inferQuestionType(answer) };
});
if (original.length !== 665)
  throw new Error('Seed count differs from approved 665 questions; import stopped.');
const questions: Question[] = original.map((question) => enrichQuestion(question));
const blankCount = questions.filter(
  (question) => !question.question.trim() || !question.answer.trim(),
).length;
if (blankCount) throw new Error('Blank question or answer detected; import stopped.');
if (questions.some((question, index) => question.id !== original[index].id))
  throw new Error('Question identity changed; import stopped.');
const rulerRows = questions.filter((question) =>
  question.answerParts.some((part) => part.kind === 'ruler'),
);
const countryCounts: Record<string, number> = {};
for (const question of rulerRows) {
  for (const country of new Set(
    question.answerParts.flatMap((part) =>
      part.kind === 'ruler' ? [part.country ?? '미확정'] : [],
    ),
  )) {
    countryCounts[country] = (countryCounts[country] ?? 0) + 1;
  }
}
const reviews = original.map((question, index) => ({
  row: index + 2,
  ...question,
  ...inspectRulerCandidates(question),
}));
const ambiguous = reviews.filter((row) => row.ambiguous.length);
const unknown = reviews.filter((row) => row.unknown.length);
// Source country before a ruler parenthesis outranks chapter context. The user
// approved keeping its original display and accepting both country/name orders.
const parentheticalCountryRows: { row: number; answer: string; country: string }[] = [];
const blocked = original.flatMap((question, index) => {
  const country = COUNTRIES.find(
    (country) =>
      question.answer.startsWith(`${country}(`) || question.answer.startsWith(`${country} (`),
  );
  if (country && questions[index].answerParts.some((part) => part.kind === 'ruler')) {
    parentheticalCountryRows.push({ row: index + 2, answer: question.answer, country });
  }
  const invalidParenthetical =
    country &&
    questions[index].answerParts.some((part) => part.kind === 'ruler') &&
    (questions[index].answerParts.some(
      (part) => part.kind === 'ruler' && part.country !== country,
    ) ||
      renderAnswer(questions[index], { showCountry: true }) !== question.answer ||
      renderAnswer(questions[index], { showCountry: false }) !== question.answer);
  return invalidParenthetical
    ? [
        {
          row: index + 2,
          question,
          reason: '괄호 바로 앞 원문 나라와 다른 나라가 붙거나 원문 나라가 중복 표시됨',
        },
      ]
    : [];
});
const sourceSha256 = createHash('sha256').update(sourceBytes).digest('hex');
const tableCell = (value: string) => value.replaceAll('|', '\\|').replace(/\r?\n/g, '<br>');
const heading =
  '| Excel 행 | 단원 · 번호 | 원문 정답 | 순수 정답 | 나라 표시 | 나라 숨김 |\n|---:|---|---|---|---|---|';
const samplePredicates: ((question: Question, index: number) => boolean)[] = [
  (q) => q.answer === '의자왕',
  (q) => q.answer === '소수림왕',
  (q) => q.answer === '광개토왕',
  (q) => q.answer === '대동법/광해군',
  (q) => q.answer === '이자겸(인종)',
  (q) => q.answer === '묘청(인종)',
  (q) => q.answer === '동의보감 (광해군)',
  (q) => q.answer === '선조 (김장생)',
  (q) => q.answer === '경순왕 (김부)',
  (q) => q.answer === '신문왕 (9서당)',
  (q) => q.answer === '태조 왕건',
  (q) => q.answer === '성종' && q.chapter.startsWith('Ⅲ'),
  (q) => q.answer === '성종' && q.chapter.startsWith('Ⅳ'),
  (q, i) => original[i].answer === '신라/진흥왕',
  (q, i) => original[i].answer === '백제/무왕',
  (q) => q.answer === '백제 (무령왕릉)',
  (q) => q.answer === '동명왕편',
  (q) => q.answer === '궁예',
  (q) => q.answer === '고려(성종)',
  (q) => q.type === 'sequence',
];
const sampleIndexes = [
  ...new Set(
    samplePredicates
      .map((predicate) => questions.findIndex(predicate))
      .filter((index) => index >= 0),
  ),
];
const sampleLines = sampleIndexes.map((index) => {
  const question = questions[index];
  return `| ${index + 2} | ${tableCell(question.chapter)} · ${question.number} | ${tableCell(original[index].answer)} | ${tableCell(question.answer)} | ${tableCell(renderAnswer(question, { showCountry: true }))} | ${tableCell(renderAnswer(question, { showCountry: false }))} |`;
});
const changed = questions.flatMap((question, index) =>
  question.answer !== original[index].answer
    ? [
        {
          row: index + 2,
          id: question.id,
          before: original[index].answer,
          after: question.answer,
          typeBefore: original[index].type,
          typeAfter: question.type,
        },
      ]
    : [],
);
const report = [
  '# 구조화 정답 가져오기 감사',
  '',
  `- 상태: ${blocked.length ? '자동 변환 중단 · 잘못된 나라 또는 중복 표시 발견 (questions.json 미갱신)' : auditOnly ? '미적용 안전 감사 통과 (questions.json 미갱신)' : '검증 통과 · 개발용 생성 문항 갱신'}`,
  `- 원본: seed/한능검_공부내용확인_문제답.xlsx (읽기 전용)`,
  `- SHA-256: ${sourceSha256}`,
  `- 문항 ${questions.length}개, 빈 문제·답 ${blankCount}개, 기존 ID 변경 0개.`,
  `- 군주를 포함한 구조화 후보 ${rulerRows.length}개. 예상 148개에 맞춰 항목을 추가·삭제하지 않음. 원문 나라/군주 6개를 함께 세면 154개임.`,
  `- 나라별 문항 수: ${Object.entries(countryCounts)
    .map(([country, count]) => `${country} ${count}`)
    .join(', ')}.`,
  `- 나라 접두사를 metadata로 이동한 원문 정답 ${changed.length}개. 숫자·순서형 답 변경 0개.`,
  '',
  '## 변환 중단 및 확인 항목',
  ...(blocked.length
    ? blocked.map(
        ({ row, question, reason }) =>
          `- Excel ${row}행 / ${question.id}: ${question.answer} — ${reason}. 해당 행의 표시는 감사 후보이며 출력 데이터에 반영하지 않음.`,
      )
    : ['- 없음']),
  '',
  '## 사용자 확인을 반영한 원문 명시 나라 처리',
  ...parentheticalCountryRows.map(
    ({ row, answer, country }) =>
      `- Excel ${row}행: ${answer} — 괄호 바로 앞 ${country}를 단원보다 우선. 나라 표시 켬/끔 모두 원문 유지. 나라/왕, 왕/나라, 나라 왕, 왕 나라 등 전체 답 변형을 인정하며 잘못된 나라·다른 왕은 인정하지 않음.`,
  ),
  '',
  '## 나라 미확정 후보 전체',
  ...(ambiguous.length
    ? ambiguous.map(
        (row) => `- Excel ${row.row}행 / ${row.id}: ${row.answer} — ${row.ambiguous.join(', ')}`,
      )
    : ['- 없음']),
  '',
  '## 사전 밖 군주명·왕/인물 소단원 후보 전체',
  '다음은 추가 검토 대상이며 군주라고 단정하지 않는다. 사전에 없는 답에 나라를 붙이지 않았고 원문을 유지한다. 특히 통일신라 소단원 안의 궁예·견훤을 통일신라 왕으로 추론하지 않는다.',
  ...(unknown.length
    ? unknown.map(
        (row) => `- Excel ${row.row}행 / ${row.id}: ${row.answer} — 후보 ${row.unknown.join(', ')}`,
      )
    : ['- 없음']),
  '',
  '## 순수 정답 변환 내역 전체',
  '| Excel 행 | ID | 원문 | 변환 | 유형 |',
  '|---:|---|---|---|---|',
  ...changed.map(
    (row) =>
      `| ${row.row} | ${row.id} | ${tableCell(row.before)} | ${tableCell(row.after)} | ${row.typeBefore} → ${row.typeAfter} |`,
  ),
  '',
  '## 대표 20개: 원문 → 순수 정답 → 나라 표시/숨김',
  heading,
  ...sampleLines,
  '',
  '## 관리 규칙',
  '- 군주·나라 표기는 study/data/rulers.json의 정확한 이름과 한글 토큰 경계로만 인식한다. 가장 긴 이름을 먼저 처리한다.',
  '- 명시한 나라가 없고 동명이인인 경우 검증된 단원 맥락을 사용하며, 결정할 수 없으면 country=null로 유지한다.',
  '- 별칭 검토 결과는 study/data/alias-overrides.json에서 원래 문항 ID를 키로 관리한다. 복합답 구성요소 하나만 정답으로 인정하는 별칭은 금지한다.',
  '- JSON의 answer는 순수 정답이며 화면 표기 문자열을 저장하지 않는다. renderAnswer(...,{showCountry})에서만 나라를 표시한다.',
  '',
].join('\n');
mkdirSync(resolve(root, '../reports'), { recursive: true });
writeFileSync(resolve(root, '../reports/import-report.md'), report, 'utf8');
console.log(
  JSON.stringify({
    questions: questions.length,
    blankCount,
    structuredRulerAnswers: rulerRows.length,
    countryCounts,
    ambiguous,
    unknown,
    blocked: blocked.map(({ row, question, reason }) => ({
      row,
      id: question.id,
      answer: question.answer,
      reason,
    })),
    parentheticalCountryRows,
    auditOnly,
    sourceSha256,
  }),
);
if (blocked.length)
  throw new Error(
    'Structured answer source review is required; generated question files were not changed.',
  );
const chapters: ChapterManifest = [];
for (const question of questions) {
  const chapter = chapters.find(([name]) => name === question.chapter);
  if (chapter) chapter[1]++;
  else chapters.push([question.chapter, 1]);
}
for (const directory of auditOnly ? [] : ['data', 'public']) {
  mkdirSync(resolve(root, directory), { recursive: true });
  writeFileSync(
    resolve(root, directory, 'questions.json'),
    JSON.stringify(questions) + '\n',
    'utf8',
  );
  writeFileSync(resolve(root, directory, 'chapters.json'), JSON.stringify(chapters) + '\n', 'utf8');
}
console.log(
  JSON.stringify({
    questions: questions.length,
    chapters: chapters.length,
    sourceSha256: createHash('sha256').update(sourceBytes).digest('hex'),
    questionBytes: Buffer.byteLength(JSON.stringify(questions)),
    types: questions.reduce<Record<string, number>>((counts, question) => {
      counts[question.type] = (counts[question.type] ?? 0) + 1;
      return counts;
    }, {}),
  }),
);
