import type { DraftQuestion } from './imports';

function filename(title: string): string {
  return (
    title
      .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
      .trim()
      .slice(0, 100) || '암기_학습'
  );
}

function download(data: BlobPart, type: string, name: string): void {
  const blob = new Blob([data], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  // Keep the URL alive long enough for Safari and slower downloads.
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

export function downloadStudyJson(questions: DraftQuestion[], title: string): void {
  download(
    JSON.stringify({ version: 1, title, questions }, null, 2),
    'application/json;charset=utf-8',
    `${filename(title)}.json`,
  );
}

export async function downloadStudyExcel(questions: DraftQuestion[], title: string): Promise<void> {
  const xlsx = await import('xlsx');
  const rows = [
    ['파트', '소제목', '번호', '문제', '답'],
    ...questions.map((item) => [item.chapter, item.sub, item.number, item.question, item.answer]),
  ];
  const sheet = xlsx.utils.aoa_to_sheet(rows);
  // XLSX stores strings with an explicit cell type. Keep formula-like answers as
  // literal text (without adding a visible apostrophe or creating a formula).
  for (const [address, cell] of Object.entries(sheet)) {
    if (
      !address.startsWith('!') &&
      cell &&
      typeof cell === 'object' &&
      typeof cell.v === 'string'
    ) {
      cell.t = 's';
      delete cell.f;
    }
  }
  sheet['!cols'] = [{ wch: 24 }, { wch: 30 }, { wch: 8 }, { wch: 80 }, { wch: 45 }];
  const workbook = xlsx.utils.book_new();
  xlsx.utils.book_append_sheet(workbook, sheet, '문제-답');
  const bytes = xlsx.write(workbook, { type: 'array', bookType: 'xlsx', compression: true });
  download(
    bytes,
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    `${filename(title)}.xlsx`,
  );
}

export interface StudyPdfOptions {
  fontUrl?: string;
}

/** Produces a local, printable question booklet with a separate answer section. */
export async function downloadStudySheet(
  questions: DraftQuestion[],
  title: string,
  options: StudyPdfOptions = {},
): Promise<void> {
  if (!questions.length) throw new Error('PDF로 만들 문제가 없어요.');
  const [{ PDFDocument, rgb }, fontkit] = await Promise.all([
    import('pdf-lib'),
    import('@pdf-lib/fontkit'),
  ]);
  const fontResponse = await fetch(
    options.fontUrl ??
      `${process.env.NEXT_PUBLIC_BASE_PATH ?? '/stt_server_cdh/study'}/fonts/NotoSansKR-Regular.ttf`,
  );
  if (!fontResponse.ok)
    throw new Error('PDF용 한글 글꼴을 읽지 못했어요. 연결을 확인한 뒤 다시 시도해 주세요.');
  const doc = await PDFDocument.create();
  doc.registerFontkit(fontkit.default);
  // Use the checked-in TrueType font. The earlier CFF/OpenType file produced
  // invalid embedded subsets in PDF readers despite successful text extraction.
  const font = await doc.embedFont(await fontResponse.arrayBuffer(), {
    subset: false,
    features: { calt: false },
  });
  const supported = new Set(font.getCharacterSet());
  const allText = [
    title,
    ...questions.flatMap((item) => [item.chapter, item.sub, item.question, item.answer]),
  ].join('\n');
  for (const character of allText) {
    if (!/[\s\u0000-\u001f]/.test(character) && !supported.has(character.codePointAt(0)!)) {
      throw new Error(
        'PDF 글꼴에 없는 문자나 이모지가 포함돼 있어요. 해당 문자를 일반 글자로 바꾸거나 엑셀·JSON으로 내보내 주세요.',
      );
    }
  }
  doc.setTitle(title);
  doc.setSubject('암기 학습 문항과 정답');
  doc.setCreator('여백 암기 학습');
  doc.setProducer('여백');
  const width = 595.28;
  const height = 841.89;
  const margin = 46;
  const bodyWidth = width - margin * 2;
  const ink = rgb(0.12, 0.18, 0.16);
  const muted = rgb(0.36, 0.41, 0.39);
  const pages: ReturnType<typeof doc.addPage>[] = [];
  let current = doc.addPage([width, height]);
  pages.push(current);
  let y = height - margin;

  // Preserve newlines and break by Unicode characters, including Korean words without spaces.
  const lines = (text: string, size: number, maxWidth = bodyWidth): string[] =>
    text
      .replace(/\r\n?/g, '\n')
      .split('\n')
      .flatMap((paragraph) => {
        if (!paragraph) return [''];
        const result: string[] = [];
        let line = '';
        for (const character of paragraph) {
          const safe = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(character) ? '' : character;
          if (line && font.widthOfTextAtSize(line + safe, size) > maxWidth) {
            result.push(line);
            line = safe;
          } else line += safe;
        }
        if (line) result.push(line);
        return result;
      });
  const newPage = () => {
    current = doc.addPage([width, height]);
    pages.push(current);
    y = height - margin;
  };
  const ensure = (needed: number) => {
    if (y - needed < margin + 20) newPage();
  };
  const write = (text: string, size = 10.5, color = ink, leading = size * 1.6) => {
    for (const line of lines(text, size)) {
      ensure(leading);
      if (line) current.drawText(line, { x: margin, y: y - size, size, font, color });
      y -= leading;
    }
  };

  write(title || '암기 학습', 22);
  y -= 8;
  write(`시험에는 이렇게 나온다 · ${questions.length}문제`, 11, muted);
  write('빈칸에 답을 쓰세요. 정답은 문제 다음 페이지부터 따로 실려 있어요.', 9, muted);
  y -= 20;
  let lastPart = '';
  questions.forEach((item, index) => {
    const part = [item.chapter, item.sub].filter(Boolean).join(' > ');
    const estimated = Math.min(
      180,
      lines(item.question, 10.5).length * 17 + 62 + (part === lastPart ? 0 : 34),
    );
    ensure(estimated);
    if (part !== lastPart) {
      write(part, 11, muted);
      y -= 8;
      lastPart = part;
    }
    write(`${index + 1}. ${item.question}`);
    ensure(40);
    y -= 18;
    current.drawLine({
      start: { x: margin, y },
      end: { x: width - margin, y },
      thickness: 0.5,
      color: rgb(0.76, 0.79, 0.77),
    });
    y -= 20;
  });
  newPage();
  write('정답 확인', 22);
  y -= 16;
  write('가져온 원문과 직접 확인한 정답을 바탕으로 복습하세요.', 9, muted);
  y -= 14;
  questions.forEach((item, index) => {
    ensure(Math.min(100, lines(`${index + 1}. ${item.answer}`, 10.5).length * 17 + 14));
    write(`${index + 1}. ${item.answer}`);
    y -= 10;
  });
  pages.forEach((page, index) => {
    const footer = `여백 암기 학습 | ${index + 1} / ${pages.length}`;
    page.drawText(footer, {
      x: (width - font.widthOfTextAtSize(footer, 8)) / 2,
      y: 24,
      size: 8,
      font,
      color: muted,
    });
  });
  const result = await doc.save();
  download(new Uint8Array(result).buffer, 'application/pdf', `${filename(title)}_문제와_정답.pdf`);
}
