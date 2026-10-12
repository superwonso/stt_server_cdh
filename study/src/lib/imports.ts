import { inferQuestionType } from './grading';

export type QuestionKind = 'short' | 'multi' | 'sequence';

export interface DraftQuestion {
  chapter: string;
  sub: string;
  number: number;
  question: string;
  answer: string;
  type: QuestionKind;
  source_excerpt?: string;
  source_page?: number | null;
}

export interface GenerateRequest {
  text: string;
  count: number;
  requestId: string;
  consent: true;
}

export interface GenerateResult {
  questions: DraftQuestion[];
  warnings: string[];
}

export interface ColumnMapping {
  chapter: number;
  question: number;
  answer: number;
  sub?: number;
  number?: number;
}

export interface ImportTable {
  name: string;
  rows: string[][];
  formulaRows: number[];
}

export const IMPORT_LIMITS = {
  fileBytes: 20 * 1024 * 1024,
  pdfBytes: 64 * 1024 * 1024,
  questions: 2000,
  columns: 40,
  cellCharacters: 6000,
  sourceCharacters: 120000,
  externalSourceCharacters: 24000,
  pdfPagesPerSelection: 40,
} as const;

function cleanCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
    throw new Error('셀에는 글자나 숫자만 사용할 수 있어요.');
  }
  const result = String(value).replace(/\r\n?/g, '\n').trim();
  if (result.length > IMPORT_LIMITS.cellCharacters) {
    throw new Error(
      `한 칸은 ${IMPORT_LIMITS.cellCharacters.toLocaleString()}자까지 가져올 수 있어요. 긴 내용을 나눠 주세요.`,
    );
  }
  return result;
}

export function questionType(answer: string): QuestionKind {
  return inferQuestionType(answer);
}

export function validateSourceText(text: string): string {
  if (!text.trim())
    throw new Error('문항을 만들 글이 없어요. 글을 입력하거나 텍스트가 있는 파일을 선택해 주세요.');
  if (text.length > IMPORT_LIMITS.sourceCharacters) {
    throw new Error(
      `한 번에 ${IMPORT_LIMITS.sourceCharacters.toLocaleString()}자까지 사용할 수 있어요. 필요한 범위를 직접 나눠 주세요. 내용은 자동으로 잘리지 않아요.`,
    );
  }
  return text;
}

export function validateExternalSourceText(text: string): string {
  validateSourceText(text);
  if (text.length > IMPORT_LIMITS.externalSourceCharacters)
    throw new Error(
      'AI로 한 번에 보낼 수 있는 글은 24,000자까지예요. 아래 글에서 사용할 부분만 직접 남겨 주세요. 원문은 자동으로 잘리지 않아요.',
    );
  return text;
}

export function validateFile(file: Pick<File, 'size' | 'name'>): void {
  const pdf = /\.pdf$/i.test(file.name);
  if (file.size > (pdf ? IMPORT_LIMITS.pdfBytes : IMPORT_LIMITS.fileBytes))
    throw new Error(
      pdf
        ? 'PDF는 64MB 이하로 나눠서 가져와 주세요.'
        : '엑셀·텍스트 파일은 20MB 이하로 나눠서 가져와 주세요.',
    );
  if (!/\.(xlsx|xls|csv|json|txt|md|pdf)$/i.test(file.name)) {
    throw new Error(
      '엑셀(.xlsx, .xls), CSV, JSON, TXT, Markdown, PDF를 지원해요. 다른 파일은 필요한 글을 복사해 넣어 주세요.',
    );
  }
}

export function decodeTextBytes(buffer: ArrayBuffer): { text: string; warning: string } {
  const bytes = new Uint8Array(buffer);
  if (bytes[0] === 0xff && bytes[1] === 0xfe)
    return { text: new TextDecoder('utf-16le', { fatal: true }).decode(bytes), warning: '' };
  if (bytes[0] === 0xfe && bytes[1] === 0xff)
    return { text: new TextDecoder('utf-16be', { fatal: true }).decode(bytes), warning: '' };
  try {
    return { text: new TextDecoder('utf-8', { fatal: true }).decode(bytes), warning: '' };
  } catch {
    try {
      return {
        text: new TextDecoder('euc-kr', { fatal: true }).decode(bytes),
        warning:
          '이 파일을 한국어 Windows(CP949) 인코딩으로 읽었어요. 미리보기에서 글자가 정확한지 확인해 주세요.',
      };
    } catch {
      throw new Error('파일의 글자 인코딩을 읽지 못했어요. UTF-8로 저장한 사본을 선택해 주세요.');
    }
  }
}

/** Inspect ZIP directory sizes before SheetJS inflates an XLSX archive on the main thread. */
export function validateXlsxArchive(buffer: ArrayBuffer): void {
  const view = new DataView(buffer);
  let end = -1;
  for (
    let offset = view.byteLength - 22;
    offset >= Math.max(0, view.byteLength - 65557);
    offset -= 1
  ) {
    if (view.getUint32(offset, true) === 0x06054b50) {
      end = offset;
      break;
    }
  }
  if (end < 0)
    throw new Error('올바른 XLSX 파일을 읽지 못했어요. 엑셀에서 다시 저장한 파일을 선택해 주세요.');
  const entries = view.getUint16(end + 10, true);
  let position = view.getUint32(end + 16, true);
  if (entries > 2000 || position === 0xffffffff || position >= end)
    throw new Error(
      '이 엑셀 파일은 내부 데이터가 너무 커요. 필요한 시트만 새 파일로 저장해 주세요.',
    );
  let total = 0;
  for (let index = 0; index < entries; index += 1) {
    if (position + 46 > end || view.getUint32(position, true) !== 0x02014b50)
      throw new Error('엑셀 파일의 내부 구조를 읽지 못했어요. 다시 저장한 파일을 선택해 주세요.');
    const uncompressed = view.getUint32(position + 24, true);
    total += uncompressed;
    if (uncompressed > 32 * 1024 * 1024 || total > 64 * 1024 * 1024)
      throw new Error('압축을 푼 엑셀 내용이 너무 커요. 필요한 범위만 새 파일로 저장해 주세요.');
    position +=
      46 +
      view.getUint16(position + 28, true) +
      view.getUint16(position + 30, true) +
      view.getUint16(position + 32, true);
    if (position > end)
      throw new Error('엑셀 파일의 내부 구조가 손상됐어요. 다시 저장한 파일을 선택해 주세요.');
  }
}

const aliases: Record<keyof ColumnMapping, string[]> = {
  chapter: ['파트', '단원', '단원명', '분류', '과목', 'chapter', 'part', 'category'],
  sub: ['소제목', '소단원', 'sub', 'subtitle'],
  number: ['번호', '문항번호', 'number', 'no'],
  question: ['문제', '질문', '문항', '앞면', 'question', 'front', 'prompt'],
  answer: ['답', '정답', '답변', '뒷면', 'answer', 'back', 'response'],
};

export function detectColumns(headers: string[]): ColumnMapping {
  const normalized = headers.map((cell) => cell.replace(/[\s_\-]/g, '').toLowerCase());
  const find = (key: keyof ColumnMapping) =>
    normalized.findIndex((cell) => aliases[key].includes(cell));
  return {
    chapter: find('chapter'),
    question: find('question'),
    answer: find('answer'),
    sub: find('sub'),
    number: find('number'),
  };
}

export function detectHeaderRow(rows: string[][]): number {
  const found = rows.slice(0, 20).findIndex((row) => {
    const mapping = detectColumns(row);
    return mapping.question >= 0 && mapping.answer >= 0;
  });
  return found < 0 ? 0 : found;
}

export function tableToQuestions(
  table: ImportTable,
  mapping: ColumnMapping,
  headerRow = 0,
): DraftQuestion[] {
  if (mapping.question < 0 || mapping.answer < 0 || mapping.question === mapping.answer) {
    throw new Error('서로 다른 문제 열과 정답 열을 선택해 주세요.');
  }
  if (!Number.isInteger(headerRow) || headerRow < -1 || headerRow >= table.rows.length)
    throw new Error('제목 행을 확인해 주세요.');
  const result: DraftQuestion[] = [];
  const used = new Set<string>();
  const counters = new Map<string, number>();
  for (let index = headerRow + 1; index < table.rows.length; index += 1) {
    const row = table.rows[index];
    if (row.every((cell) => !String(cell ?? '').trim())) continue;
    if (table.formulaRows.includes(index))
      throw new Error(
        `${index + 1}행에 수식이 있어요. 계산된 값을 복사해 ‘값으로 붙여넣기’ 한 파일을 사용해 주세요.`,
      );
    const rawQuestion = cleanCell(row[mapping.question]);
    const answer = cleanCell(row[mapping.answer]);
    if (!rawQuestion || !answer)
      throw new Error(
        `${index + 1}행의 ${rawQuestion ? '정답' : '문제'}가 비어 있어요. 빈 항목은 자동으로 건너뛰지 않아요.`,
      );
    const part =
      cleanCell(mapping.chapter >= 0 ? row[mapping.chapter] : '가져온 문제') || '가져온 문제';
    const [chapter, ...subParts] = part.split(/\s*>\s*/);
    const sub =
      mapping.sub !== undefined && mapping.sub >= 0
        ? cleanCell(row[mapping.sub])
        : subParts.join(' > ');
    const prefix = rawQuestion.match(/^\s*\((\d+)\)\s*/);
    const explicitNumber =
      mapping.number !== undefined && mapping.number >= 0
        ? cleanCell(row[mapping.number])
        : prefix?.[1];
    const group = `${chapter}\u0000${sub}`;
    const number = explicitNumber ? Number(explicitNumber) : (counters.get(group) ?? 0) + 1;
    if (!Number.isSafeInteger(number) || number < 1)
      throw new Error(`${index + 1}행의 문항 번호는 1 이상의 정수여야 해요.`);
    counters.set(group, Math.max(counters.get(group) ?? 0, number));
    const key = `${group}\u0000${number}`;
    if (used.has(key))
      throw new Error(
        `${index + 1}행에서 같은 단원·소제목의 ${number}번이 중복돼요. 번호를 다르게 바꿔 주세요.`,
      );
    used.add(key);
    const question = rawQuestion.replace(/^\s*\(\d+\)\s*/, '').trim();
    if (!question) throw new Error(`${index + 1}행에는 번호만 있고 문제가 없어요.`);
    result.push({ chapter, sub, number, question, answer, type: questionType(answer) });
    if (result.length > IMPORT_LIMITS.questions)
      throw new Error(
        `한 번에 ${IMPORT_LIMITS.questions}문제까지 가져올 수 있어요. 파일을 나눠 주세요.`,
      );
  }
  if (!result.length)
    throw new Error('선택한 시트에서 문제를 찾지 못했어요. 제목 행과 열을 확인해 주세요.');
  return result;
}

export function parseCsv(text: string): string[][] {
  const result: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;
  const input = text.replace(/^\uFEFF/, '');
  for (let i = 0; i < input.length; i += 1) {
    const char = input[i];
    if (char === '"') {
      if (quoted && input[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (quoted || cell.length === 0) quoted = !quoted;
      else cell += char;
    } else if (char === ',' && !quoted) {
      row.push(cleanCell(cell));
      cell = '';
    } else if ((char === '\n' || char === '\r') && !quoted) {
      if (char === '\r' && input[i + 1] === '\n') i += 1;
      row.push(cleanCell(cell));
      result.push(row);
      row = [];
      cell = '';
    } else cell += char;
    if (
      cell.length > IMPORT_LIMITS.cellCharacters ||
      row.length > IMPORT_LIMITS.columns ||
      result.length > IMPORT_LIMITS.questions + 20
    ) {
      throw new Error('CSV가 가져오기 한도를 넘었어요. 2,000문제·40열 이하로 나눠 주세요.');
    }
  }
  if (quoted) throw new Error('CSV의 따옴표가 닫히지 않았어요. 파일 형식을 확인해 주세요.');
  if (cell.length || row.length) {
    row.push(cleanCell(cell));
    result.push(row);
  }
  if (result.some((entry) => entry.length > IMPORT_LIMITS.columns))
    throw new Error('40열 이하의 파일을 선택해 주세요.');
  return result;
}

export function parseJsonQuestions(text: string): DraftQuestion[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error('JSON 문법을 확인해 주세요.');
  }
  const raw = Array.isArray(parsed)
    ? parsed
    : parsed && typeof parsed === 'object' && 'questions' in parsed
      ? parsed.questions
      : null;
  if (!Array.isArray(raw) || raw.length > IMPORT_LIMITS.questions)
    throw new Error('JSON은 최대 2,000개의 문제 배열 또는 { questions: [...] } 형식이어야 해요.');
  const rows = raw.map((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item))
      throw new Error(`${index + 1}번째 문제의 형식을 확인해 주세요.`);
    return [
      cleanCell(item.chapter ?? item.파트 ?? '가져온 문제'),
      cleanCell(item.sub),
      cleanCell(item.number),
      cleanCell(item.question ?? item.문제),
      cleanCell(item.answer ?? item.답),
    ];
  });
  const questions = tableToQuestions(
    { name: 'JSON', rows, formulaRows: [] },
    { chapter: 0, sub: 1, number: 2, question: 3, answer: 4 },
    -1,
  );
  return questions.map((question, index) => {
    const source = raw[index];
    if (source.source_excerpt !== undefined) {
      if (typeof source.source_excerpt !== 'string' || source.source_excerpt.length > 400)
        throw new Error(`${index + 1}번째 문제의 원문 근거 형식을 확인해 주세요.`);
      question.source_excerpt = source.source_excerpt;
    }
    if (source.source_page !== undefined) {
      if (
        source.source_page !== null &&
        (!Number.isSafeInteger(source.source_page) || source.source_page < 1)
      )
        throw new Error(`${index + 1}번째 문제의 원문 쪽 번호를 확인해 주세요.`);
      question.source_page = source.source_page;
    }
    return question;
  });
}

export async function readWorkbook(file: File): Promise<ImportTable[]> {
  validateFile(file);
  const bytes = await file.arrayBuffer();
  const signature = new Uint8Array(bytes, 0, Math.min(bytes.byteLength, 2));
  if (/\.xlsx$/i.test(file.name) || (signature[0] === 0x50 && signature[1] === 0x4b))
    validateXlsxArchive(bytes);
  const xlsx = await import('xlsx');
  const workbook = xlsx.read(bytes, {
    type: 'array',
    cellFormula: true,
    cellHTML: false,
    cellText: true,
    sheetRows: IMPORT_LIMITS.questions + 21,
  });
  if (workbook.SheetNames.length > 50)
    throw new Error('시트가 50개를 넘어요. 사용할 시트만 남긴 사본을 선택해 주세요.');
  return workbook.SheetNames.map((name) => {
    const sheet = workbook.Sheets[name];
    const range = sheet['!fullref'] ?? sheet['!ref'];
    if (range) {
      const extent = xlsx.utils.decode_range(range);
      if (extent.e.c >= IMPORT_LIMITS.columns || extent.e.r > IMPORT_LIMITS.questions + 20)
        throw new Error(
          `‘${name}’ 시트가 2,000문제·40열 한도를 넘어요. 사용할 범위만 새 파일로 저장해 주세요.`,
        );
    }
    const rows = xlsx.utils
      .sheet_to_json<unknown[]>(sheet, {
        header: 1,
        defval: '',
        raw: false,
        blankrows: true,
        range: 0,
      })
      .map((row) => row.map(cleanCell));
    const formulaRows = new Set<number>();
    Object.entries(sheet).forEach(([address, cell]) => {
      if (!address.startsWith('!') && cell && typeof cell === 'object' && 'f' in cell && cell.f)
        formulaRows.add(xlsx.utils.decode_cell(address).r);
    });
    return { name, rows, formulaRows: [...formulaRows] };
  });
}

export interface PdfTextResult {
  text: string;
  emptyPages: number[];
  pageCount: number;
}

async function loadPdf(file: File) {
  validateFile(file);
  const pdfjs = await import('pdfjs-dist');
  const base = process.env.NEXT_PUBLIC_BASE_PATH ?? '/stt_server_cdh/study';
  pdfjs.GlobalWorkerOptions.workerSrc = `${base}/pdf.worker.min.mjs`;
  // Text extraction only: no annotation actions, attachments, forms, or document JavaScript are executed.
  return pdfjs.getDocument({
    data: new Uint8Array(await file.arrayBuffer()),
    useSystemFonts: true,
    enableXfa: false,
  });
}

export async function pdfPageCount(file: File): Promise<number> {
  const task = await loadPdf(file);
  try {
    const doc = await task.promise;
    return doc.numPages;
  } finally {
    await task.destroy();
  }
}

export async function extractPdfText(
  file: File,
  first: number,
  last: number,
): Promise<PdfTextResult> {
  if (
    !Number.isInteger(first) ||
    !Number.isInteger(last) ||
    first < 1 ||
    last < first ||
    last - first + 1 > IMPORT_LIMITS.pdfPagesPerSelection
  ) {
    throw new Error('한 번에 최대 40쪽까지, 올바른 시작·끝 쪽을 선택해 주세요.');
  }
  const task = await loadPdf(file);
  try {
    const doc = await task.promise;
    if (last > doc.numPages) throw new Error(`이 PDF는 ${doc.numPages}쪽까지 있어요.`);
    const parts: string[] = [];
    const emptyPages: number[] = [];
    for (let pageNo = first; pageNo <= last; pageNo += 1) {
      const page = await doc.getPage(pageNo);
      const content = await page.getTextContent();
      const text = content.items
        .map((item) => ('str' in item ? `${item.str}${item.hasEOL ? '\n' : ' '}` : ''))
        .join('')
        .trim();
      if (!text) emptyPages.push(pageNo);
      parts.push(
        `[원본 ${pageNo}쪽]\n${text || '(추출 가능한 글자 없음 — 이미지·스캔은 OCR이 필요해요.)'}`,
      );
      if (parts.join('\n\n').length > IMPORT_LIMITS.sourceCharacters)
        throw new Error(
          '선택한 PDF 범위의 글이 120,000자를 넘어요. 쪽 범위를 줄여 주세요. 글은 자동으로 잘리지 않아요.',
        );
      page.cleanup();
    }
    return { text: parts.join('\n\n'), emptyPages, pageCount: doc.numPages };
  } finally {
    await task.destroy();
  }
}

/** Spreadsheet applications treat strings starting with these characters as formulas on some imports. */
export function safeSpreadsheetCell(value: string): string {
  return /^[\t\r\n ]*[=+@-]/.test(value) ? `'${value}` : value;
}
