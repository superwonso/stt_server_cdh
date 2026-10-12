'use client';

import { useRef, useState } from 'react';
import {
  decodeTextBytes,
  detectColumns,
  detectHeaderRow,
  extractPdfText,
  IMPORT_LIMITS,
  parseCsv,
  parseJsonQuestions,
  pdfPageCount,
  questionType,
  readWorkbook,
  tableToQuestions,
  validateFile,
  validateSourceText,
  validateExternalSourceText,
  type ColumnMapping,
  type DraftQuestion,
  type GenerateRequest,
  type GenerateResult,
  type ImportTable,
} from '../lib/imports';

export interface ImportPanelProps {
  onImport: (value: { title: string; questions: DraftQuestion[] }) => void | Promise<void>;
  generate?: (request: GenerateRequest) => Promise<GenerateResult>;
}

const PAGE_SIZE = 10;
const errorMessage = (error: unknown) => {
  if (error instanceof Error && /[가-힣]/.test(error.message)) return error.message;
  if (error instanceof Error && /password/i.test(error.message))
    return '암호로 보호된 PDF는 읽을 수 없어요. 열람 권한이 있다면 암호를 해제한 사본을 선택해 주세요.';
  return '파일을 처리하지 못했어요. 파일 형식과 크기를 확인하고, 정상적으로 열리는 사본으로 다시 시도해 주세요.';
};

export default function ImportPanel({ onImport, generate }: ImportPanelProps) {
  const [title, setTitle] = useState('내 암기 노트');
  const [sourceName, setSourceName] = useState('');
  const [tables, setTables] = useState<ImportTable[]>([]);
  const [sheetIndex, setSheetIndex] = useState(0);
  const [headerRow, setHeaderRow] = useState(0);
  const [mapping, setMapping] = useState<ColumnMapping>({ chapter: -1, question: -1, answer: -1 });
  const [drafts, setDrafts] = useState<DraftQuestion[]>([]);
  const [sourceText, setSourceText] = useState('');
  const [pdf, setPdf] = useState<File | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [firstPage, setFirstPage] = useState(1);
  const [lastPage, setLastPage] = useState(1);
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [consent, setConsent] = useState(false);
  const [count, setCount] = useState(20);
  const [reviewed, setReviewed] = useState(false);
  const [failedRequest, setFailedRequest] = useState(false);
  const [confirmNewRequest, setConfirmNewRequest] = useState(false);
  const request = useRef<{ text: string; count: number; id: string } | null>(null);
  const preview = useRef<HTMLHeadingElement>(null);
  const table = tables[sheetIndex];

  function prepareTable(nextTables: ImportTable[], index = 0) {
    setTables(nextTables);
    setSheetIndex(index);
    setDrafts([]);
    setReviewed(false);
    const row = detectHeaderRow(nextTables[index]?.rows ?? []);
    setHeaderRow(row);
    setMapping(detectColumns(nextTables[index]?.rows[row] ?? []));
  }

  function showDrafts(items: DraftQuestion[]) {
    setDrafts(items);
    setPage(0);
    setReviewed(false);
    setNotice(`${items.length}문제를 불러왔어요. 문제와 정답을 확인한 뒤 저장해 주세요.`);
    setTimeout(() => preview.current?.focus(), 0);
  }

  async function openFile(file: File) {
    setBusy(true);
    setError('');
    setNotice('');
    setWarnings([]);
    setDrafts([]);
    setTables([]);
    setPdf(null);
    setSourceText('');
    setSourceName('');
    setConsent(false);
    setReviewed(false);
    setFailedRequest(false);
    setConfirmNewRequest(false);
    request.current = null;
    try {
      validateFile(file);
      setSourceName(file.name);
      setTitle(file.name.replace(/\.[^.]+$/, '').slice(0, 100));
      const extension = file.name.split('.').pop()?.toLowerCase();
      if (extension === 'xlsx' || extension === 'xls') prepareTable(await readWorkbook(file));
      else if (extension === 'csv' || extension === 'json') {
        const decoded = decodeTextBytes(await file.arrayBuffer());
        if (decoded.warning) setWarnings([decoded.warning]);
        if (extension === 'csv')
          prepareTable([{ name: file.name, rows: parseCsv(decoded.text), formulaRows: [] }]);
        else showDrafts(parseJsonQuestions(decoded.text));
      } else if (extension === 'pdf') {
        const total = await pdfPageCount(file);
        setPdf(file);
        setPageCount(total);
        setFirstPage(1);
        setLastPage(Math.min(total, 10));
        setNotice(`총 ${total}쪽이에요. 사용할 범위를 선택해 글을 추출해 주세요.`);
      } else {
        const decoded = decodeTextBytes(await file.arrayBuffer());
        const text = decoded.text;
        if (decoded.warning) setWarnings([decoded.warning]);
        validateSourceText(text);
        setSourceText(text);
        setNotice(
          '글은 이 기기에서 읽었어요. 필요한 부분을 확인하고 AI 문항 만들기를 선택할 수 있어요.',
        );
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  async function extractPages() {
    if (!pdf) return;
    setBusy(true);
    setError('');
    setConsent(false);
    try {
      const result = await extractPdfText(pdf, firstPage, lastPage);
      setSourceText(result.text);
      const missing = result.emptyPages.length
        ? ` ${result.emptyPages.join(', ')}쪽에는 읽을 수 있는 글자가 없어요. 스캔·이미지의 글은 포함되지 않았습니다.`
        : '';
      setNotice(
        `${firstPage}–${lastPage}쪽을 추출했어요. 다단 편집·표는 순서가 달라질 수 있으니 원문과 비교해 주세요.${missing}`,
      );
      if (result.emptyPages.length === lastPage - firstPage + 1) {
        setSourceText('');
        setError(
          '선택한 쪽이 스캔·이미지여서 글을 추출하지 못했어요. OCR로 추출한 글을 붙여 넣거나 텍스트가 있는 PDF를 사용해 주세요.',
        );
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  async function generateQuestions() {
    if (!generate || !consent) return;
    setBusy(true);
    setError('');
    setConfirmNewRequest(false);
    try {
      validateExternalSourceText(sourceText);
      if (!Number.isInteger(count) || count < 1 || count > 30)
        throw new Error('문항 수는 1–30개로 선택해 주세요.');
      if (
        !request.current ||
        request.current.text !== sourceText ||
        request.current.count !== count
      ) {
        request.current = { text: sourceText, count, id: crypto.randomUUID() };
      }
      const result = await generate({
        text: sourceText,
        count,
        requestId: request.current.id,
        consent: true,
      });
      // Validate AI output with the same rules as local JSON; do not trust provider shape.
      showDrafts(parseJsonQuestions(JSON.stringify(result.questions)));
      setFailedRequest(false);
      setWarnings(
        Array.isArray(result.warnings)
          ? result.warnings
              .filter((entry): entry is string => typeof entry === 'string')
              .slice(0, 10)
          : [],
      );
      setNotice(
        'AI 초안을 만들었어요. 원문 근거와 정답을 직접 확인해 주세요. AI는 틀린 답이나 빠진 내용을 만들 수 있어요.',
      );
    } catch (caught) {
      setError(errorMessage(caught));
      setFailedRequest(request.current !== null);
    } finally {
      setBusy(false);
    }
  }

  function editDraft(index: number, key: 'chapter' | 'sub' | 'question' | 'answer', value: string) {
    setReviewed(false);
    setDrafts((items) =>
      items.map((item, position) =>
        position === index
          ? { ...item, [key]: value, ...(key === 'answer' ? { type: questionType(value) } : {}) }
          : item,
      ),
    );
  }

  async function saveDrafts() {
    setError('');
    setBusy(true);
    try {
      if (!title.trim()) throw new Error('노트 이름을 입력해 주세요.');
      const validated = parseJsonQuestions(JSON.stringify(drafts));
      await onImport({ title: title.trim(), questions: validated });
      setNotice(`${validated.length}문제를 저장했어요. 이 기기의 학습 자료에서 선택할 수 있어요.`);
      setDrafts([]);
      setReviewed(false);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="study-card space-y-5" aria-labelledby="import-heading">
      <div>
        <h2 id="import-heading" className="text-xl font-bold">
          내 자료 가져오기
        </h2>
        <p className="mt-2 text-sm">
          엑셀·CSV·JSON 문제표는 이 기기에서 읽어요. PDF·글은 범위를 확인한 뒤 AI로 문제 초안을 만들
          수 있어요.
        </p>
      </div>
      <label className="block space-y-2">
        <span className="font-medium">파일 선택 (PDF 64MB · 그 외 20MB 이하)</span>
        <input
          className="study-input w-full min-h-11"
          type="file"
          accept=".xlsx,.xls,.csv,.json,.txt,.md,.pdf"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void openFile(file);
            event.target.value = '';
          }}
        />
      </label>
      {sourceName && <p className="break-all text-sm">선택한 파일: {sourceName}</p>}
      <p className="text-sm">
        문제표는 ‘파트 / 문제 / 답’ 열을 권장해요. 다른 열 이름도 직접 연결할 수 있어요. 파일은 이
        기기에서 읽고, AI 버튼을 누르면 선택한 글만 전송해요. 학습 기록은 서버로 보내지 않아요.
      </p>

      {table && (
        <div className="space-y-4 rounded-xl border p-4">
          <h3 className="font-bold">문제표 열 연결</h3>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1">
              <span className="block">시트</span>
              <select
                className="study-input w-full"
                value={sheetIndex}
                onChange={(e) => prepareTable(tables, Number(e.target.value))}
                disabled={busy}
              >
                {tables.map((entry, index) => (
                  <option key={entry.name} value={index}>
                    {entry.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="block">제목 행</span>
              <select
                className="study-input w-full"
                value={headerRow}
                onChange={(e) => {
                  const index = Number(e.target.value);
                  setHeaderRow(index);
                  setMapping(detectColumns(table.rows[Math.max(index, 0)] ?? []));
                  setDrafts([]);
                }}
              >
                <option value={-1}>제목 행 없음 (첫 행부터 문제)</option>
                {table.rows.slice(0, 20).map((_, index) => (
                  <option key={index} value={index}>
                    {index + 1}행
                  </option>
                ))}
              </select>
            </label>
            {(['chapter', 'sub', 'number', 'question', 'answer'] as const).map((key) => (
              <label key={key} className="space-y-1">
                <span className="block">
                  {
                    {
                      chapter: '단원 열 (선택)',
                      sub: '소제목 열 (선택)',
                      number: '번호 열 (선택)',
                      question: '문제 열',
                      answer: '정답 열',
                    }[key]
                  }
                </span>
                <select
                  className="study-input w-full"
                  value={mapping[key] ?? -1}
                  onChange={(e) => {
                    setMapping({ ...mapping, [key]: Number(e.target.value) });
                    setDrafts([]);
                  }}
                >
                  <option value={-1}>선택 안 함</option>
                  {Array.from(
                    { length: Math.max(...table.rows.slice(0, 20).map((row) => row.length), 0) },
                    (_, index) => (
                      <option key={index} value={index}>
                        {index + 1}열:{' '}
                        {(table.rows[Math.max(headerRow, 0)]?.[index] || '(비어 있음)').slice(
                          0,
                          60,
                        )}
                      </option>
                    ),
                  )}
                </select>
              </label>
            ))}
          </div>
          <p className="text-sm">
            번호가 없으면 같은 단원 안에서 행 순서대로 번호를 붙여요. 나중에 같은 자료를 갱신하려면
            고유한 번호를 유지해 주세요.
          </p>
          <button
            className="study-btn"
            disabled={busy}
            onClick={() => {
              setError('');
              try {
                showDrafts(tableToQuestions(table, mapping, headerRow));
              } catch (caught) {
                setError(errorMessage(caught));
              }
            }}
          >
            문제 미리보기
          </button>
        </div>
      )}

      {pdf && (
        <div className="space-y-3 rounded-xl border p-4">
          <h3 className="font-bold">PDF 범위 선택 · 전체 {pageCount}쪽</h3>
          <div className="flex flex-wrap items-end gap-3">
            <label>
              <span className="mb-1 block">시작 쪽</span>
              <input
                className="study-input w-24"
                type="number"
                min={1}
                max={pageCount}
                value={firstPage}
                onChange={(e) => setFirstPage(Number(e.target.value))}
              />
            </label>
            <label>
              <span className="mb-1 block">끝 쪽</span>
              <input
                className="study-input w-24"
                type="number"
                min={firstPage}
                max={pageCount}
                value={lastPage}
                onChange={(e) => setLastPage(Number(e.target.value))}
              />
            </label>
            <button className="study-btn" disabled={busy} onClick={() => void extractPages()}>
              선택한 쪽에서 글 추출
            </button>
          </div>
          <p className="text-sm">
            한 번에 최대 40쪽. 스캔·그림 속 글의 OCR은 포함되지 않아요. 추출된 글과 원문 쪽 번호를
            아래에서 확인해 주세요.
          </p>
        </div>
      )}

      <details className="rounded-xl border p-4" open={Boolean(sourceText) || undefined}>
        <summary className="min-h-11 cursor-pointer font-bold">글에서 문항 만들기</summary>
        <div className="mt-3 space-y-4">
          <label className="block space-y-2">
            <span>
              사용할 글 · {sourceText.length.toLocaleString()} /{' '}
              {IMPORT_LIMITS.sourceCharacters.toLocaleString()}자
            </span>
            <textarea
              className="study-input min-h-56 w-full"
              value={sourceText}
              onChange={(e) => {
                setSourceText(e.target.value);
                setConsent(false);
              }}
              placeholder="교재나 내 노트에서 허가된 글을 붙여 넣으세요. 필요한 범위만 남겨 주세요."
            />
          </label>
          <label className="flex items-center gap-3">
            <span>만들 문항 수</span>
            <input
              className="study-input w-24"
              type="number"
              min={1}
              max={30}
              value={count}
              onChange={(e) => {
                setCount(Number(e.target.value));
                setConsent(false);
              }}
            />
          </label>
          <p className="text-sm">
            AI는 한 번에 24,000자·30문제까지 처리해요. 더 긴 자료는 위 글에서 필요한 부분만 직접
            남겨 주세요. 자동으로 자르지 않아요.
          </p>
          <label className="flex min-h-11 items-start gap-3">
            <input
              className="mt-1 h-5 w-5 shrink-0"
              type="checkbox"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
            />
            <span className="text-sm">
              위 글을 여백 서버와 연결된 GPT-6 Luna 서비스에 보내 유료 AI 문항 생성을 요청하는 데
              동의합니다. 개인정보·비밀 정보가 없고 사용할 권한이 있는 자료인지 확인했어요. 생성
              결과는 검토한 뒤 저장합니다.
            </span>
          </label>
          <button
            className="study-btn"
            disabled={busy || !generate || !consent || !sourceText.trim()}
            onClick={() => void generateQuestions()}
          >
            AI 문항 초안 만들기
          </button>
          {failedRequest && (
            <div className="space-y-3 rounded-xl border p-3">
              <p className="text-sm">
                다시 누르면 같은 요청의 결과를 확인해요. 새로 생성하면 추가 요금이 발생할 수 있으며,
                먼저 보낸 요청은 취소되지 않아요.
              </p>
              <button
                className="study-btn"
                disabled={busy}
                onClick={() => setConfirmNewRequest(true)}
              >
                새 요청 시작 (추가 요금 가능)
              </button>
              {confirmNewRequest && (
                <div className="space-y-3">
                  <p>
                    같은 글로 새 문항 생성을 준비할까요? 아래 확인 후 유료 전송 동의도 다시 체크해야
                    해요.
                  </p>
                  <div className="flex flex-wrap gap-3">
                    <button className="study-btn" onClick={() => setConfirmNewRequest(false)}>
                      취소
                    </button>
                    <button
                      className="study-btn"
                      onClick={() => {
                        request.current = null;
                        setFailedRequest(false);
                        setConfirmNewRequest(false);
                        setConsent(false);
                        setError('');
                        setNotice(
                          '새 요청을 준비했어요. 글과 문항 수를 확인하고 유료 전송에 다시 동의한 뒤 AI 버튼을 눌러 주세요.',
                        );
                      }}
                    >
                      새 요청 준비 확인
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
          {!generate && (
            <p className="text-sm">AI 가져오기는 여백에 로그인한 화면에서 사용할 수 있어요.</p>
          )}
        </div>
      </details>

      {busy && (
        <p role="status" aria-live="polite">
          자료를 처리하고 있어요…
        </p>
      )}
      {error && (
        <p role="alert" className="rounded-xl border border-red-400 p-3">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="rounded-xl border p-3">
          {notice}
        </p>
      )}
      {warnings.length > 0 && (
        <div className="rounded-xl border p-3" role="status">
          <p className="font-bold">가져오기 확인 사항</p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            {warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {drafts.length > 0 && (
        <div className="space-y-4">
          <h3 ref={preview} tabIndex={-1} className="text-lg font-bold">
            저장 전 확인 · {drafts.length}문제
          </h3>
          <label className="block space-y-2">
            <span>노트 이름</span>
            <input
              className="study-input w-full"
              value={title}
              maxLength={100}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <p className="text-sm">
            문제와 정답을 수정하거나 제외할 수 있어요. 페이지를 넘겨 전체 문항을 확인해 주세요.
          </p>
          {drafts.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((draft, local) => {
            const index = page * PAGE_SIZE + local;
            return (
              <fieldset key={index} className="space-y-3 rounded-xl border p-4">
                <legend className="px-2 font-bold">{index + 1}번째 문제</legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label>
                    <span className="mb-1 block">단원</span>
                    <input
                      className="study-input w-full"
                      value={draft.chapter}
                      onChange={(e) => editDraft(index, 'chapter', e.target.value)}
                    />
                  </label>
                  <label>
                    <span className="mb-1 block">소제목</span>
                    <input
                      className="study-input w-full"
                      value={draft.sub}
                      onChange={(e) => editDraft(index, 'sub', e.target.value)}
                    />
                  </label>
                </div>
                <label className="block">
                  <span className="mb-1 block">고유 문항 번호</span>
                  <input
                    className="study-input w-28"
                    type="number"
                    min={1}
                    value={draft.number}
                    onChange={(e) => {
                      setReviewed(false);
                      setDrafts((items) =>
                        items.map((item, position) =>
                          position === index ? { ...item, number: Number(e.target.value) } : item,
                        ),
                      );
                    }}
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block">문제</span>
                  <textarea
                    className="study-input min-h-24 w-full"
                    aria-label="문제"
                    value={draft.question}
                    onChange={(e) => editDraft(index, 'question', e.target.value)}
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block">정답</span>
                  <textarea
                    className="study-input min-h-20 w-full"
                    aria-label="정답"
                    value={draft.answer}
                    onChange={(e) => editDraft(index, 'answer', e.target.value)}
                  />
                </label>
                {(draft.source_excerpt || draft.source_page) && (
                  <div className="rounded-lg border p-3 text-sm">
                    <p className="font-medium">
                      원문 근거{draft.source_page ? ` · ${draft.source_page}쪽` : ''}
                    </p>
                    {draft.source_excerpt && (
                      <blockquote className="mt-2 whitespace-pre-wrap">
                        {draft.source_excerpt}
                      </blockquote>
                    )}
                    <p className="mt-2">원문과 정답이 일치하는지 확인해 주세요.</p>
                  </div>
                )}
                <button
                  className="study-btn"
                  onClick={() => {
                    setDrafts((items) => items.filter((_, position) => position !== index));
                    setReviewed(false);
                    setPage(
                      Math.min(page, Math.max(0, Math.ceil((drafts.length - 1) / PAGE_SIZE) - 1)),
                    );
                  }}
                >
                  {index + 1}번째 문제 제외
                </button>
              </fieldset>
            );
          })}
          <nav aria-label="가져온 문제 미리보기 쪽" className="flex flex-wrap items-center gap-3">
            <button
              className="study-btn"
              disabled={page === 0}
              onClick={() => {
                setPage(page - 1);
                preview.current?.focus();
              }}
            >
              이전 문제들
            </button>
            <span>
              {page + 1} / {Math.ceil(drafts.length / PAGE_SIZE)}
            </span>
            <button
              className="study-btn"
              disabled={(page + 1) * PAGE_SIZE >= drafts.length}
              onClick={() => {
                setPage(page + 1);
                preview.current?.focus();
              }}
            >
              다음 문제들
            </button>
          </nav>
          <label className="flex min-h-11 items-center gap-3">
            <input
              className="h-5 w-5"
              type="checkbox"
              checked={reviewed}
              onChange={(e) => setReviewed(e.target.checked)}
            />
            <span>문항과 정답을 확인했어요.</span>
          </label>
          <button
            className="study-btn"
            disabled={busy || !reviewed}
            onClick={() => void saveDrafts()}
          >
            확인한 {drafts.length}문제 저장
          </button>
        </div>
      )}
    </section>
  );
}
