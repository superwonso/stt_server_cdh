import { describe, expect, it } from 'vitest';
import {
  decodeTextBytes,
  detectColumns,
  detectHeaderRow,
  IMPORT_LIMITS,
  parseCsv,
  parseJsonQuestions,
  questionType,
  readWorkbook,
  safeSpreadsheetCell,
  tableToQuestions,
  validateFile,
  validateSourceText,
  validateExternalSourceText,
  validateXlsxArchive,
  type ImportTable,
} from '../src/lib/imports';

const table = (rows: string[][], formulaRows: number[] = []): ImportTable => ({
  name: '문제-답',
  rows,
  formulaRows,
});
const mapping = { chapter: 0, question: 1, answer: 2 };

describe('question table imports', () => {
  it('round trips literal formula-like and negative answers through XLSX without running formulas', async () => {
    const xlsx = await import('xlsx');
    const book = xlsx.utils.book_new();
    const sheet = xlsx.utils.aoa_to_sheet([
      ['파트', '문제', '답'],
      ['수학', '값은?', '-2'],
      ['코드', '예시는?', '=SUM(A1)'],
    ]);
    expect(sheet.C3.t).toBe('s');
    expect(sheet.C3.f).toBeUndefined();
    xlsx.utils.book_append_sheet(book, sheet, '문제-답');
    const bytes = xlsx.write(book, { type: 'array', bookType: 'xlsx' });
    const workbook = await readWorkbook(new File([bytes], '문제표.xlsx'));
    const questions = tableToQuestions(workbook[0], mapping);
    expect(questions.map((question) => question.answer)).toEqual(['-2', '=SUM(A1)']);
  });
  it('detects Korean and English aliases despite spaces and case', () => {
    expect(detectColumns(['Part', ' QUESTION ', 'Answer', 'sub', 'number'])).toEqual({
      chapter: 0,
      question: 1,
      answer: 2,
      sub: 3,
      number: 4,
    });
    expect(detectColumns(['단원명', '질문', '정답']).answer).toBe(2);
  });

  it('finds a header after a title and preserves multiple line questions', () => {
    const rows = [
      ['한국사 노트'],
      [],
      ['파트', '문제', '답'],
      ['삼국 > 고구려', '(3) 순서를 쓰세요\n① 건국\n② 전쟁', '①→②'],
    ];
    const header = detectHeaderRow(rows);
    expect(header).toBe(2);
    const questions = tableToQuestions(table(rows), mapping, header);
    expect(questions).toEqual([
      {
        chapter: '삼국',
        sub: '고구려',
        number: 3,
        question: '순서를 쓰세요\n① 건국\n② 전쟁',
        answer: '①→②',
        type: 'sequence',
      },
    ]);
  });

  it('only ignores entirely empty rows and rejects blank answers', () => {
    const data = table([
      ['파트', '문제', '답'],
      ['', '', ''],
      ['역사', '왕 이름?', ''],
    ]);
    expect(() => tableToQuestions(data, mapping)).toThrow('3행의 정답');
    expect(() =>
      tableToQuestions(
        table([
          ['문제', '답'],
          ['', '정답'],
        ]),
        { chapter: -1, question: 0, answer: 1 },
      ),
    ).toThrow('문제가 비어');
  });

  it('rejects duplicate chapter, sub and number without renumbering explicit records', () => {
    expect(() =>
      tableToQuestions(
        table([
          ['파트', '문제', '답'],
          ['역사 > 왕', '(1) 첫 문제', '가'],
          ['역사 > 왕', '(1) 둘째 문제', '나'],
        ]),
        mapping,
      ),
    ).toThrow('중복');
    const valid = tableToQuestions(
      table([
        ['파트', '문제', '답'],
        ['역사 > 왕', '(1) 첫 문제', '가'],
        ['역사 > 전쟁', '(1) 둘째 문제', '나'],
      ]),
      mapping,
    );
    expect(valid).toHaveLength(2);
  });

  it('numbers unnumbered questions in each part and handles no header', () => {
    const questions = tableToQuestions(
      table([
        ['왕 이름?', '태조'],
        ['수도는?', '개경'],
      ]),
      { chapter: -1, question: 0, answer: 1 },
      -1,
    );
    expect(questions.map((question) => question.number)).toEqual([1, 2]);
    expect(questions[0].chapter).toBe('가져온 문제');
  });

  it('reads an explicit sub and number column for JSON and exported Excel round trips', () => {
    const questions = tableToQuestions(
      table([
        ['파트', '소제목', '번호', '문제', '답'],
        ['조선', '왕', '17', '이름?', '세종'],
      ]),
      { chapter: 0, sub: 1, number: 2, question: 3, answer: 4 },
    );
    expect(questions[0]).toMatchObject({ chapter: '조선', sub: '왕', number: 17 });
    expect(parseJsonQuestions(JSON.stringify({ questions }))).toEqual(questions);
  });

  it('rejects formula-bearing rows rather than using a possibly stale cached result', () => {
    expect(() =>
      tableToQuestions(
        table(
          [
            ['파트', '문제', '답'],
            ['역사', '질문', '캐시 값'],
          ],
          [1],
        ),
        mapping,
      ),
    ).toThrow('수식');
  });

  it('does not interpret HTML or formula-like imported text', () => {
    const questions = tableToQuestions(
      table([
        ['파트', '문제', '답'],
        ['역사', '<script>alert(1)</script>', '=WEBSERVICE("https://example.invalid")'],
      ]),
      mapping,
    );
    expect(questions[0].question).toBe('<script>alert(1)</script>');
    expect(questions[0].answer).toBe('=WEBSERVICE("https://example.invalid")');
    expect(safeSpreadsheetCell(questions[0].answer)).toMatch(/^'=/);
  });

  it('rejects a duplicate selected column, invalid number and number-only question', () => {
    expect(() =>
      tableToQuestions(table([['질문'], ['내용']]), { chapter: -1, question: 0, answer: 0 }),
    ).toThrow('서로 다른');
    expect(() => parseJsonQuestions('[{"number":-1,"question":"질문","answer":"답"}]')).toThrow(
      '정수',
    );
    expect(() =>
      tableToQuestions(
        table([
          ['파트', '문제', '답'],
          ['왕', '(1)', '세종'],
        ]),
        mapping,
      ),
    ).toThrow('번호만');
  });

  it('enforces limits without silently truncating rows or cells', () => {
    const rows = Array.from({ length: IMPORT_LIMITS.questions + 1 }, (_, index) => [
      `문제 ${index}`,
      '답',
    ]);
    expect(() =>
      tableToQuestions(table(rows), { chapter: -1, question: 0, answer: 1 }, -1),
    ).toThrow('파일을 나눠');
    expect(() =>
      parseJsonQuestions(
        JSON.stringify([{ question: '가'.repeat(IMPORT_LIMITS.cellCharacters + 1), answer: '답' }]),
      ),
    ).toThrow('한 칸');
  });
});

describe('CSV and JSON inputs', () => {
  it('decodes UTF-8, UTF-16 and Korean Windows text without replacement characters', () => {
    expect(decodeTextBytes(new TextEncoder().encode('문제,답').buffer).text).toBe('문제,답');
    expect(decodeTextBytes(Uint8Array.from([0xff, 0xfe, 0x38, 0xbb]).buffer).text).toBe('문');
    const koreanWindows = decodeTextBytes(Uint8Array.from([0xb9, 0xae, 0xc1, 0xa6]).buffer);
    expect(koreanWindows.text).toBe('문제');
    expect(koreanWindows.warning).toContain('CP949');
    expect(() => decodeTextBytes(Uint8Array.from([0xff]).buffer)).toThrow('인코딩');
  });
  it('handles quoted commas, escaped quotes, BOM and newlines', () => {
    expect(parseCsv('\ufeff문제,답\r\n"첫줄\n둘째줄, 계속","말 ""인용"""\r\n')).toEqual([
      ['문제', '답'],
      ['첫줄\n둘째줄, 계속', '말 "인용"'],
    ]);
  });
  it('rejects malformed quoting and too many columns', () => {
    expect(() => parseCsv('문제,답\n"닫히지 않음,답')).toThrow('따옴표');
    expect(() => parseCsv(Array(41).fill('열').join(','))).toThrow('40열');
  });
  it('supports array and questions-object JSON but rejects malformed or empty data', () => {
    expect(parseJsonQuestions('[{"문제":"질문","답":"정답"}]')[0].answer).toBe('정답');
    expect(() => parseJsonQuestions('[]')).toThrow('찾지 못');
    expect(() => parseJsonQuestions('{oops}')).toThrow('JSON 문법');
    expect(() => parseJsonQuestions('[{"question":{},"answer":"답"}]')).toThrow('글자나 숫자');
  });
  it('preserves bounded source evidence for human review and rejects malformed evidence', () => {
    const question = {
      question: '왕은?',
      answer: '세종',
      source_excerpt: '세종이 훈민정음을 창제했다.',
      source_page: 3,
    };
    expect(parseJsonQuestions(JSON.stringify([question]))[0]).toMatchObject(question);
    expect(() => parseJsonQuestions(JSON.stringify([{ ...question, source_page: -1 }]))).toThrow(
      '쪽 번호',
    );
    expect(() =>
      parseJsonQuestions(JSON.stringify([{ ...question, source_excerpt: '가'.repeat(401) }])),
    ).toThrow('원문 근거');
  });
});

describe('limits and export safety', () => {
  it('rejects missing and oversized ZIP directories before XLSX decompression', () => {
    expect(() => validateXlsxArchive(new ArrayBuffer(12))).toThrow('올바른 XLSX');
    const bytes = new ArrayBuffer(168);
    const view = new DataView(bytes);
    view.setUint32(100, 0x02014b50, true);
    view.setUint32(124, 100 * 1024 * 1024, true);
    view.setUint32(146, 0x06054b50, true);
    view.setUint16(156, 1, true);
    view.setUint32(162, 100, true);
    expect(() => validateXlsxArchive(bytes)).toThrow('압축을 푼');
    view.setUint32(124, 1024, true);
    expect(() => validateXlsxArchive(bytes)).not.toThrow();
  });
  it('classifies supported answer types', () => {
    expect(questionType('①②③')).toBe('sequence');
    expect(questionType('②→①')).toBe('sequence');
    expect(questionType('흥선 대원군/고종')).toBe('multi');
    expect(questionType('광혜원(제중원)')).toBe('short');
    expect(questionType('1919/3/1')).toBe('short');
  });
  it('rejects oversized and unsupported files before decoding', () => {
    expect(() => validateFile({ name: '자료.xlsx', size: IMPORT_LIMITS.fileBytes + 1 })).toThrow(
      '20MB',
    );
    expect(() => validateFile({ name: '자료.pdf', size: 39083353 })).not.toThrow();
    expect(() => validateFile({ name: '자료.pdf', size: IMPORT_LIMITS.pdfBytes + 1 })).toThrow(
      '64MB',
    );
    expect(() => validateFile({ name: '실행.html', size: 10 })).toThrow('지원');
    expect(() => validateFile({ name: '자료.XLSX', size: 10 })).not.toThrow();
  });
  it('retains source text exactly and refuses oversized source instead of truncating', () => {
    const text = '[원본 3쪽]\n사용자가 선택한 글';
    expect(validateSourceText(text)).toBe(text);
    expect(() => validateSourceText(' ')).toThrow('글이 없어요');
    expect(() => validateSourceText('a'.repeat(IMPORT_LIMITS.sourceCharacters + 1))).toThrow(
      '자동으로 잘리지',
    );
  });
  it('applies the smaller AI upload limit before any paid request', () => {
    const text = '가'.repeat(24001);
    expect(() => validateSourceText(text)).not.toThrow();
    expect(() => validateExternalSourceText(text)).toThrow('24,000자');
    expect(validateExternalSourceText('선택한 글')).toBe('선택한 글');
  });
  it.each(['=1+1', '+cmd', '-cmd', '@SUM(A1)', ' \t=HYPERLINK("url")'])(
    'escapes spreadsheet formula prefix %s',
    (value) => {
      expect(safeSpreadsheetCell(value)).toBe(`'${value}`);
    },
  );
  it('keeps ordinary Korean answers unchanged on export', () => {
    expect(safeSpreadsheetCell('광혜원(제중원)')).toBe('광혜원(제중원)');
  });
});
