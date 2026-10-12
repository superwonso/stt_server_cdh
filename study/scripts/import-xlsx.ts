/** Import the approved 665-row seed without correcting or rewriting its content. */
import { createHash } from 'node:crypto';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as XLSX from 'xlsx';
import { inferQuestionType } from '../src/lib/grading';
import type { ChapterManifest, Question } from '../src/lib/types';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
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
const questions: Question[] = rows.slice(1).map((row, index) => {
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
if (questions.length !== 665)
  throw new Error('Seed count differs from approved 665 questions; import stopped.');
const chapters: ChapterManifest = [];
for (const question of questions) {
  const chapter = chapters.find(([name]) => name === question.chapter);
  if (chapter) chapter[1]++;
  else chapters.push([question.chapter, 1]);
}
for (const directory of ['data', 'public']) {
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
