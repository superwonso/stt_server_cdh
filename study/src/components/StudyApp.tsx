'use client';
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  ChapterManifest,
  Progress,
  Question,
  RoundState,
  StorageAdapter,
  StudyDeck,
} from '@/lib/types';
import { defaultProgress, loadProgress, saveProgressLocked, validQuestion } from '@/lib/storage';
import { gradeAnswer } from '@/lib/grading';
import { getChoices } from '@/lib/choices';
import { renderAnswer, upgradeQuestion } from '@/lib/answer';
import { createRound, submitAnswer } from '@/lib/round';
import { useStudyBridge } from '@/lib/bridge';
import type { DraftQuestion, GenerateRequest, GenerateResult } from '@/lib/imports';
const ImportPanel = lazy(() => import('./ImportPanel'));
const base = process.env.NEXT_PUBLIC_BASE_PATH || '/stt_server_cdh/study';
const adapter: StorageAdapter = {
  getItem: (key) => window.localStorage.getItem(key),
  setItem: (key, value) => window.localStorage.setItem(key, value),
};
type View = 'home' | 'learn' | 'round' | 'result' | 'flash' | 'flash-result' | 'import';
type Feedback = {
  question: Question;
  correct: boolean;
  written: boolean;
  chosen: string;
  choices: string[];
};
type Stats = { correct: number; wrong: number; rounds: number; wrongIds: string[] };
const emptyStats: Stats = { correct: 0, wrong: 0, rounds: 0, wrongIds: [] };
const seedError = '기본 문제집을 불러오지 못했어요. 연결을 확인한 뒤 다시 시도해 주세요.';
const exportErrors = new Set([
  'PDF로 만들 문제가 없어요.',
  'PDF용 한글 글꼴을 읽지 못했어요. 연결을 확인한 뒤 다시 시도해 주세요.',
  'PDF 글꼴에 없는 문자나 이모지가 포함돼 있어요. 해당 문자를 일반 글자로 바꾸거나 엑셀·JSON으로 내보내 주세요.',
]);
const shuffleItems = <T,>(items: T[]) => {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
};
async function digest(value: string) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))]
    .map((v) => v.toString(16).padStart(2, '0'))
    .join('');
}
export default function StudyApp() {
  const bridge = useStudyBridge();
  if (!bridge.scope)
    return (
      <main className="study-app study-empty">
        <h1>암기 학습</h1>
        <p>여백에 로그인한 뒤 상단의 암기 학습 탭을 열어 주세요.</p>
        <a className="study-btn" href="../" target="_top">
          여백으로 이동
        </a>
      </main>
    );
  return (
    <Workspace
      key={bridge.scope}
      scope={bridge.scope}
      active={bridge.active}
      generate={bridge.generate}
    />
  );
}
function Workspace({
  scope,
  active,
  generate,
}: {
  scope: string;
  active: boolean;
  generate: (request: GenerateRequest) => Promise<GenerateResult>;
}) {
  const [progress, setProgress] = useState<Progress>(defaultProgress);
  const [seed, setSeed] = useState<Question[]>([]);
  const [manifest, setManifest] = useState<ChapterManifest>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [view, setView] = useState<View>('home');
  const [importOpened, setImportOpened] = useState(false);
  const [round, setRound] = useState<RoundState | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [written, setWritten] = useState('');
  const [stats, setStats] = useState<Stats>(emptyStats);
  const [flash, setFlash] = useState<Question[]>([]);
  const [flashIndex, setFlashIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [unknown, setUnknown] = useState<Question[]>([]);
  const [clearStep, setClearStep] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [seedLoading, setSeedLoading] = useState(false);
  const [seedFailed, setSeedFailed] = useState(false);
  const [actionPending, setActionPending] = useState(false);
  const mounted = useRef(false);
  const activeRef = useRef(active);
  const actionVersion = useRef(0);
  const actionBusy = useRef(false);
  const seedCache = useRef<Question[]>([]);
  const seedRequest = useRef<{ controller: AbortController; promise: Promise<Question[]> } | null>(
    null,
  );
  const dialog = useRef<HTMLDialogElement>(null);
  const answerInput = useRef<HTMLInputElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const heldShortcuts = useRef(new Set<string>());
  const cancelAction = useCallback(() => {
    actionVersion.current++;
    actionBusy.current = false;
    setActionPending(false);
    setExporting(false);
  }, []);
  const ensureSeed = useCallback((): Promise<Question[]> => {
    if (seedCache.current.length) return Promise.resolve(seedCache.current);
    if (seedRequest.current) return seedRequest.current.promise;
    const controller = new AbortController();
    setSeedLoading(true);
    setSeedFailed(false);
    const promise = (async () => {
      try {
        const response = await fetch(`${base}/questions.json`, { signal: controller.signal });
        if (!response.ok) throw new Error(seedError);
        const data: unknown = await response.json();
        if (
          !Array.isArray(data) ||
          data.length !== 665 ||
          !data.every(validQuestion) ||
          new Set(data.map((q) => q.id)).size !== data.length
        )
          throw new Error(seedError);
        if (controller.signal.aborted) throw new Error(seedError);
        const questions = data.map((question) => upgradeQuestion(question));
        if (mounted.current) {
          seedCache.current = questions;
          setSeed(questions);
        }
        return questions;
      } catch (error) {
        if (mounted.current && !controller.signal.aborted) setSeedFailed(true);
        throw error;
      } finally {
        if (seedRequest.current?.controller === controller) {
          seedRequest.current = null;
          if (mounted.current) setSeedLoading(false);
        }
      }
    })();
    seedRequest.current = { controller, promise };
    return promise;
  }, []);
  useEffect(() => {
    let alive = true;
    mounted.current = true;
    const controller = new AbortController();
    const p = loadProgress(adapter, scope);
    setProgress(p);
    if (p.decks?.some((item) => item.id === p.activeDeckId)) setLoaded(true);
    if (p.loadError)
      setError(
        '저장된 학습 기록을 읽지 못했어요. 이 화면에서는 학습할 수 있지만, 기록을 내보내거나 브라우저 저장 설정을 확인해 주세요.',
      );
    fetch(`${base}/chapters.json`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error();
        const data: unknown = await response.json();
        if (
          !Array.isArray(data) ||
          !data.length ||
          data.some(
            (row) =>
              !Array.isArray(row) ||
              row.length !== 2 ||
              typeof row[0] !== 'string' ||
              !row[0] ||
              !Number.isSafeInteger(row[1]) ||
              row[1] < 1,
          ) ||
          new Set(data.map((row) => row[0])).size !== data.length ||
          data.reduce((sum, row) => sum + row[1], 0) !== 665
        )
          throw new Error();
        return data as ChapterManifest;
      })
      .then((chapters) => {
        if (!alive) return;
        setManifest(chapters);
        setProgress((current) =>
          current.initialized
            ? current
            : {
                ...current,
                selectedChapters: chapters.map(([chapter]) => chapter),
                initialized: true,
              },
        );
        setLoaded(true);
      })
      .catch(() => {
        if (alive)
          setError(
            '기본 문제집의 단원 목록을 불러오지 못했어요. 연결을 확인한 뒤 다시 열어 주세요.',
          );
      });
    return () => {
      alive = false;
      mounted.current = false;
      controller.abort();
      seedRequest.current?.controller.abort();
      seedRequest.current = null;
    };
  }, [scope]);
  useEffect(() => {
    activeRef.current = active;
    if (!active) cancelAction();
  }, [active, cancelAction]);
  useEffect(() => {
    const held = heldShortcuts.current;
    const release = (event: KeyboardEvent) => held.delete(event.code || event.key);
    const clear = () => held.clear();
    clear();
    window.addEventListener('keyup', release);
    window.addEventListener('blur', clear);
    document.addEventListener('visibilitychange', clear);
    return () => {
      clear();
      window.removeEventListener('keyup', release);
      window.removeEventListener('blur', clear);
      document.removeEventListener('visibilitychange', clear);
    };
  }, [active]);
  useEffect(() => {
    const theme = progress.theme || 'system';
    if (theme === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }, [progress.theme]);
  useEffect(() => {
    if (clearStep) {
      dialog.current?.show();
      dialog.current?.scrollIntoView({ block: 'center' });
      dialog.current?.querySelector('button')?.focus();
    } else dialog.current?.close();
  }, [clearStep]);
  useEffect(() => {
    heading.current?.focus();
  }, [view]);
  const deck = progress.decks?.find((d) => d.id === progress.activeDeckId);
  const questions = deck?.questions || seed;
  const chapters = useMemo<ChapterManifest>(
    () =>
      deck
        ? [...new Set(questions.map((q) => q.chapter))].map((chapter) => [
            chapter,
            questions.filter((q) => q.chapter === chapter).length,
          ])
        : manifest,
    [deck, questions, manifest],
  );
  const selected = useMemo(
    () => questions.filter((q) => progress.selectedChapters.includes(q.chapter)),
    [questions, progress.selectedChapters],
  );
  const seedTotal = manifest.reduce((sum, [, count]) => sum + count, 0);
  const questionCount = deck ? questions.length : seedTotal;
  const selectedCount =
    deck || seed.length
      ? selected.length
      : chapters.reduce(
          (sum, [chapter, count]) =>
            sum + (progress.selectedChapters.includes(chapter) ? count : 0),
          0,
        );
  const mastered = questions.filter((q) => progress.levels[q.id] === 2).length;
  const remaining = selectedCount - selected.filter((q) => progress.levels[q.id] === 2).length;
  const pendingSeedProgress =
    !deck && !seed.length && Object.values(progress.levels).some((level) => level > 0);
  useEffect(() => {
    if (!loaded || !pendingSeedProgress) return;
    let current = true;
    void ensureSeed().catch(() => {
      if (current && mounted.current) setError(seedError);
    });
    return () => {
      current = false;
    };
  }, [loaded, pendingSeedProgress, ensureSeed]);
  const title = deck?.title || '한능검 · 공부내용 확인';
  const showCountry = progress.showCountry ?? true;
  const commit = useCallback(
    async (next: Progress) => {
      if (!mounted.current) return false;
      setProgress(next);
      const result = await saveProgressLocked(
        adapter,
        scope,
        next,
        navigator.locks,
        () => mounted.current,
      );
      if (!result.ok && mounted.current)
        setError(
          result.error === 'conflict'
            ? '다른 탭에서 학습 기록이 바뀌어 저장하지 않았어요. 현재 화면의 문제집을 내보낸 뒤 새로고침해 최신 기록을 불러와 주세요.'
            : '기기에 기록을 저장하지 못했어요. 지금 학습은 계속할 수 있지만 새로고침하면 사라질 수 있어요. 저장 공간과 브라우저 설정을 확인하고 문제집을 내보내 주세요.',
        );
      return result.ok;
    },
    [scope],
  );
  const persistRound = useCallback(
    (next: RoundState) => {
      setRound(next);
      commit({ ...progress, levels: { ...progress.levels, ...next.levels } });
    },
    [commit, progress],
  );
  const activeQuestion = feedback?.question || questions.find((q) => q.id === round?.queue[0]);
  const generatedChoices = useMemo(() => {
    if (!activeQuestion) return [];
    try {
      return getChoices(activeQuestion, questions, Math.random, { showCountry });
    } catch {
      return [];
    }
  }, [activeQuestion, questions, showCountry]);
  const choices = feedback?.choices || generatedChoices;
  const isWritten =
    feedback?.written ??
    (activeQuestion ? (round?.levels[activeQuestion.id] || 0) > 0 || choices.length < 2 : false);
  const finishFeedback = useCallback(() => {
    if (!feedback || !round) return;
    setFeedback(null);
    setWritten('');
    if (!round.queue.length) {
      const nextStats = {
        correct: stats.correct + round.correctCount,
        wrong: stats.wrong + round.wrongCount,
        rounds: stats.rounds + 1,
        wrongIds: [...new Set([...stats.wrongIds, ...round.wrongIds])],
      };
      setStats(nextStats);
      setView(
        selected.every((q) => round.levels[q.id] === 2 || progress.levels[q.id] === 2)
          ? 'result'
          : 'round',
      );
    }
  }, [feedback, round, stats, selected, progress.levels]);
  useEffect(() => {
    if (feedback?.correct && active) {
      const timer = setTimeout(finishFeedback, 850);
      return () => clearTimeout(timer);
    }
  }, [feedback, active, finishFeedback]);
  useEffect(() => {
    if (view === 'learn' && !feedback && isWritten && active) answerInput.current?.focus();
  }, [view, feedback, isWritten, active, activeQuestion?.id]);
  const answer = useCallback(
    (input: string, skip = false) => {
      if (!activeQuestion || !round || feedback) return;
      const correct = !skip && gradeAnswer(activeQuestion, input).correct;
      setFeedback({
        question: activeQuestion,
        correct,
        written: isWritten,
        chosen: input,
        choices,
      });
      persistRound(submitAnswer(round, activeQuestion, correct, { written: isWritten }));
    },
    [activeQuestion, round, feedback, isWritten, choices, persistRound],
  );
  const flashAnswer = useCallback(
    (known: boolean) => {
      if (!flash[flashIndex]) return;
      if (!known) setUnknown((old) => [...old, flash[flashIndex]]);
      if (flashIndex + 1 >= flash.length) setView('flash-result');
      else setFlashIndex((i) => i + 1);
      setFlipped(false);
    },
    [flash, flashIndex],
  );
  const beginAction = useCallback(() => {
    if (actionBusy.current || !activeRef.current || !mounted.current) return null;
    actionBusy.current = true;
    setActionPending(true);
    return ++actionVersion.current;
  }, []);
  const currentAction = useCallback(
    (version: number) => mounted.current && activeRef.current && actionVersion.current === version,
    [],
  );
  const finishAction = useCallback((version: number) => {
    if (actionVersion.current !== version) return;
    actionBusy.current = false;
    if (mounted.current) {
      setActionPending(false);
      setExporting(false);
    }
  }, []);
  const startLearn = useCallback(
    async (newSession = true) => {
      if (!selectedCount) return;
      const version = beginAction();
      if (version === null) return;
      try {
        const all = deck?.questions || (await ensureSeed());
        if (!currentAction(version)) return;
        const chosen = all.filter((q) => progress.selectedChapters.includes(q.chapter));
        if (!chosen.length) return;
        if (newSession) setStats(emptyStats);
        setFeedback(null);
        setWritten('');
        const next = createRound(chosen, progress.levels, { size: 7, shuffle: progress.shuffle });
        for (const id of next.initialIds) {
          const q = all.find((item) => item.id === id);
          if (q && (next.levels[id] || 0) === 0) {
            try {
              if (getChoices(q, all, Math.random, { showCountry }).length < 2) next.levels[id] = 1;
            } catch {
              next.levels[id] = 1;
            }
          }
        }
        setRound(next);
        setView(next.queue.length ? 'learn' : 'result');
      } catch {
        if (currentAction(version)) setError(seedError);
      } finally {
        finishAction(version);
      }
    },
    [
      selectedCount,
      beginAction,
      deck,
      ensureSeed,
      currentAction,
      progress.selectedChapters,
      progress.levels,
      progress.shuffle,
      showCountry,
      finishAction,
    ],
  );
  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if (
        !active ||
        event.defaultPrevented ||
        event.isComposing ||
        event.keyCode === 229 ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey ||
        event.shiftKey ||
        dialog.current?.open
      )
        return;
      const code = event.code || event.key;
      if (heldShortcuts.current.has(code)) {
        event.preventDefault();
        return;
      }
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        (target.isContentEditable ||
          (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) &&
            !(target instanceof HTMLInputElement && target.disabled)))
      )
        return;
      if (event.repeat) return;
      const numberKey = /^[1-4]$/.test(event.key);
      const takeKey = () => {
        event.preventDefault();
        heldShortcuts.current.add(code);
      };
      if (view === 'learn') {
        // Consume a continuation separately: it must never answer the next question.
        if (feedback && (numberKey || event.key === 'Enter')) {
          takeKey();
          finishFeedback();
          return;
        }
        if (!feedback && !isWritten && numberKey && choices[Number(event.key) - 1]) {
          takeKey();
          answer(choices[Number(event.key) - 1]);
        }
        return;
      }
      if (view === 'round' && (numberKey || event.key === 'Enter')) {
        takeKey();
        void startLearn(false);
        return;
      }
      if (view === 'flash') {
        if (event.key === ' ') {
          takeKey();
          setFlipped((v) => !v);
        }
        if (event.key === 'ArrowLeft') {
          takeKey();
          flashAnswer(false);
        }
        if (event.key === 'ArrowRight') {
          takeKey();
          flashAnswer(true);
        }
      }
    };
    window.addEventListener('keydown', keyboard);
    return () => window.removeEventListener('keydown', keyboard);
  }, [active, view, feedback, finishFeedback, isWritten, answer, choices, flashAnswer, startLearn]);
  const startFlash = async (list?: Question[]) => {
    const version = beginAction();
    if (version === null) return;
    try {
      const all = list || deck?.questions || (await ensureSeed());
      if (!currentAction(version)) return;
      const chosen = list || all.filter((q) => progress.selectedChapters.includes(q.chapter));
      if (!chosen.length) return;
      setFlash(progress.shuffle ? shuffleItems(chosen) : [...chosen]);
      setFlashIndex(0);
      setFlipped(false);
      setUnknown([]);
      setView('flash');
    } catch {
      if (currentAction(version)) setError(seedError);
    } finally {
      finishAction(version);
    }
  };
  const switchDeck = async (id: string, newDecks = progress.decks || []) => {
    cancelAction();
    const oldId = progress.activeDeckId || 'seed';
    if (id === oldId) {
      setView('home');
      setFeedback(null);
      setRound(null);
      return true;
    }
    const oldState = {
      selectedChapters: progress.selectedChapters,
      shuffle: progress.shuffle,
      levels: progress.levels,
    };
    const newQuestions = id === 'seed' ? seed : newDecks.find((d) => d.id === id)?.questions || [];
    const state = progress.deckProgress?.[id] || {
      selectedChapters:
        id === 'seed'
          ? manifest.map(([chapter]) => chapter)
          : [...new Set(newQuestions.map((q) => q.chapter))],
      shuffle: true,
      levels: {},
    };
    const saved = commit({
      ...progress,
      ...state,
      decks: newDecks,
      activeDeckId: id,
      deckProgress: { ...progress.deckProgress, [oldId]: oldState },
    });
    setView('home');
    setFeedback(null);
    setRound(null);
    return await saved;
  };
  const importDeck = async ({
    title: deckTitle,
    questions: drafts,
  }: {
    title: string;
    questions: DraftQuestion[];
  }) => {
    const version = beginAction();
    if (version === null) throw new Error('다른 작업이 진행 중이에요. 잠시 후 다시 시도해 주세요.');
    try {
      const canonical = drafts
        .map((q) => JSON.stringify([q.chapter, q.sub, q.number, q.question, q.answer]))
        .sort()
        .join('\n');
      const id = await digest(canonical);
      if (!currentAction(version))
        throw new Error('화면이 바뀌어 가져오기를 멈췄어요. 초안을 확인한 뒤 다시 저장해 주세요.');
      if (progress.decks?.some((d) => d.id === id)) {
        await switchDeck(id);
        if (mounted.current) setNotice('이미 가져온 문제집이에요. 기존 학습 기록을 이어갑니다.');
        return;
      }
      if ((progress.decks?.length || 0) >= 20)
        throw new Error(
          '문제집은 최대 20개까지 보관할 수 있어요. 필요한 문제집을 먼저 내보내 주세요.',
        );
      const imported = await Promise.all(
        drafts.map(async (q, index) => ({
          ...q,
          id: `${id.slice(0, 16)}-${await digest(JSON.stringify([q.chapter, q.sub, q.number || index + 1, q.question]))}`,
        })),
      );
      if (!currentAction(version))
        throw new Error('화면이 바뀌어 가져오기를 멈췄어요. 초안을 확인한 뒤 다시 저장해 주세요.');
      const newDeck: StudyDeck = {
        id,
        title: deckTitle.trim() || '내 문제집',
        questions: imported,
        createdAt: new Date().toISOString(),
      };
      const saved = await switchDeck(id, [...(progress.decks || []), newDeck]);
      if (mounted.current)
        setNotice(
          saved
            ? `${imported.length}문제를 이 기기에 가져왔어요.`
            : `${imported.length}문제를 이 화면에서 열었어요. 기기 저장은 실패했으니 새로고침 전에 문제집을 내보내 주세요.`,
        );
    } finally {
      finishAction(version);
    }
  };
  const exportDeck = async (format: 'json' | 'excel' | 'pdf') => {
    const version = beginAction();
    if (version === null) return;
    setExporting(true);
    setNotice('');
    try {
      const all = deck?.questions || (await ensureSeed());
      if (!currentAction(version)) return;
      const exporter = await import('@/lib/export');
      if (!currentAction(version)) return;
      if (format === 'json') await exporter.downloadStudyJson(all, title);
      if (format === 'excel') await exporter.downloadStudyExcel(all, title);
      if (format === 'pdf') await exporter.downloadStudySheet(all, title, { showCountry });
      if (currentAction(version)) setNotice('다운로드 파일을 확인해 주세요.');
    } catch (caught) {
      if (currentAction(version))
        setError(
          caught instanceof Error &&
            (exportErrors.has(caught.message) || caught.message === seedError)
            ? caught.message
            : '파일을 만들지 못했어요. 잠시 뒤 다시 시도해 주세요.',
        );
    } finally {
      finishAction(version);
    }
  };
  const back = () => {
    cancelAction();
    setView('home');
    setFeedback(null);
    setWritten('');
  };
  return (
    <main className="study-app" data-active={active}>
      <div className="study-topline">
        <p className="study-eyebrow">나만의 학습 노트</p>
        <label className="study-muted">
          화면{' '}
          <select
            aria-label="화면 색상"
            className="study-btn"
            value={progress.theme || 'system'}
            disabled={actionPending}
            onChange={(e) => commit({ ...progress, theme: e.target.value as Progress['theme'] })}
          >
            <option value="system">기기 설정</option>
            <option value="light">밝게</option>
            <option value="dark">어둡게</option>
          </select>
        </label>
      </div>
      {error && (
        <div className="study-error" role="alert">
          {error}
          <button className="study-btn" onClick={() => setError('')}>
            닫기
          </button>
        </div>
      )}
      {notice && (
        <p className="study-note" role="status">
          {notice}
        </p>
      )}
      {seedLoading && !deck && (
        <div className="study-note" role="status" data-testid="seed-loading">
          문제집을 불러오고 있어요.{' '}
          {actionPending && (
            <button className="study-btn" onClick={back}>
              학습 홈에 머무르기
            </button>
          )}
        </div>
      )}
      {seedFailed && !seedLoading && !deck && (
        <button
          className="study-btn"
          onClick={() => {
            const version = actionVersion.current;
            void ensureSeed().catch(() => {
              if (mounted.current && version === actionVersion.current) setError(seedError);
            });
          }}
        >
          문제집 다시 불러오기
        </button>
      )}
      {!loaded ? (
        <div className="study-empty" role="status">
          문제집을 준비하고 있어요.
        </div>
      ) : (
        <>
          {view === 'home' && (
            <>
              <section className="study-hero">
                <div>
                  <h1 ref={heading} tabIndex={-1}>
                    암기 학습
                  </h1>
                  <p className="study-muted">7문제씩 고르고 쓰며 학습합니다.</p>
                </div>
                <div className="study-stats">
                  <div className="study-stat">
                    <strong>
                      {pendingSeedProgress ? '…' : mastered}
                      <span> / {questionCount}</span>
                    </strong>
                    <span>외운 문제</span>
                  </div>
                  <div className="study-stat">
                    <strong>
                      {pendingSeedProgress
                        ? '…'
                        : Math.round((mastered / Math.max(1, questionCount)) * 100)}
                      <span>%</span>
                    </strong>
                    <span>전체 진행률</span>
                  </div>
                </div>
              </section>
              <progress
                className="study-progress"
                value={mastered}
                max={questionCount || 1}
                aria-label="전체 학습 진행률"
                aria-busy={pendingSeedProgress}
                style={pendingSeedProgress ? { visibility: 'hidden' } : undefined}
              />
              <section className="study-card study-section">
                <div className="study-row">
                  <label className="study-muted" htmlFor="deck-select">
                    학습할 문제집
                  </label>
                  <button
                    className="study-btn"
                    onClick={() => {
                      cancelAction();
                      setImportOpened(true);
                      setView('import');
                    }}
                  >
                    ＋ 내 파일 가져오기
                  </button>
                </div>
                <select
                  id="deck-select"
                  className="study-input"
                  value={progress.activeDeckId || 'seed'}
                  onChange={(e) => switchDeck(e.target.value)}
                >
                  <option value="seed" disabled={!manifest.length}>
                    한능검 · 공부내용 확인{seedTotal ? ` (${seedTotal}문제)` : ''}
                  </option>
                  {progress.decks?.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.title} ({d.questions.length}문제)
                    </option>
                  ))}
                </select>
                <details className="study-export-menu">
                  <summary>문제집 내보내기</summary>
                  <div className="study-controls">
                    <button
                      className="study-btn"
                      disabled={
                        exporting ||
                        actionPending ||
                        (!deck && seedLoading) ||
                        !questionCount ||
                        !active
                      }
                      onClick={() => void exportDeck('excel')}
                    >
                      엑셀
                    </button>
                    <button
                      className="study-btn"
                      disabled={
                        exporting ||
                        actionPending ||
                        (!deck && seedLoading) ||
                        !questionCount ||
                        !active
                      }
                      onClick={() => void exportDeck('json')}
                    >
                      JSON
                    </button>
                    <button
                      className="study-btn"
                      disabled={
                        exporting ||
                        actionPending ||
                        (!deck && seedLoading) ||
                        !questionCount ||
                        !active
                      }
                      onClick={() => void exportDeck('pdf')}
                    >
                      문제·정답 PDF
                    </button>
                  </div>
                  <p className="study-muted study-small">
                    내보내는 파일에는 문제와 답만 담깁니다. 학습 진도는 이 브라우저에 남습니다.
                  </p>
                </details>
              </section>
              <section className="study-section" aria-labelledby="chapter-title">
                <div className="study-row">
                  <h2 id="chapter-title">어디부터 공부할까요?</h2>
                  <div className="study-controls">
                    <button
                      className="study-btn"
                      disabled={actionPending}
                      onClick={() =>
                        commit({ ...progress, selectedChapters: chapters.map(([name]) => name) })
                      }
                    >
                      전체 선택
                    </button>
                    <button
                      className="study-btn"
                      disabled={actionPending}
                      onClick={() => commit({ ...progress, selectedChapters: [] })}
                    >
                      선택 해제
                    </button>
                  </div>
                </div>
                <div className="study-chapters">
                  {chapters.map(([chapter, count]) => (
                    <label className="study-chapter" key={chapter}>
                      <input
                        type="checkbox"
                        disabled={actionPending}
                        checked={progress.selectedChapters.includes(chapter)}
                        onChange={(e) =>
                          commit({
                            ...progress,
                            selectedChapters: e.target.checked
                              ? [...progress.selectedChapters, chapter]
                              : progress.selectedChapters.filter((c) => c !== chapter),
                          })
                        }
                      />
                      <span className="study-chapter-name">{chapter}</span>
                      <span className="study-chapter-count">
                        {pendingSeedProgress
                          ? '…'
                          : questions.filter(
                              (q) => q.chapter === chapter && progress.levels[q.id] === 2,
                            ).length}{' '}
                        / {count}
                      </span>
                    </label>
                  ))}
                </div>
              </section>
              <div className="study-row study-section">
                <label className="study-check">
                  <input
                    type="checkbox"
                    disabled={actionPending}
                    checked={progress.shuffle}
                    onChange={(e) => commit({ ...progress, shuffle: e.target.checked })}
                  />
                  문제 순서 섞기
                </label>
                <span className="study-muted">
                  {selectedCount}문제 선택 ·{' '}
                  {pendingSeedProgress ? '진도 확인 중' : `${remaining}문제 더 익히기`}
                </span>
              </div>
              <label className="study-check study-section">
                <input
                  type="checkbox"
                  disabled={actionPending}
                  checked={showCountry}
                  onChange={(e) => commit({ ...progress, showCountry: e.target.checked })}
                />
                왕 이름 앞에 나라 표시
              </label>
              <div className="study-launch">
                <button
                  className="study-btn study-btn-primary"
                  disabled={!selectedCount || actionPending || (!deck && seedLoading) || !active}
                  onClick={() => startLearn()}
                >
                  {pendingSeedProgress || selected.some((q) => (progress.levels[q.id] || 0) > 0)
                    ? '이어서 학습'
                    : '학습하기'}{' '}
                  <span aria-hidden>→</span>
                </button>
                <button
                  className="study-btn"
                  disabled={!selectedCount || actionPending || (!deck && seedLoading) || !active}
                  onClick={() => startFlash()}
                >
                  플래시카드
                </button>
              </div>
              <div className="study-row study-section">
                <p className="study-muted study-small">
                  문제집과 진도는 이 기기의 브라우저에만 저장돼요.
                  <br />
                  다른 기기와 자동 동기화되지 않습니다.
                </p>
                <button
                  className="study-btn study-btn-danger"
                  disabled={actionPending}
                  onClick={() => setClearStep(1)}
                >
                  기록 지우기
                </button>
              </div>
            </>
          )}
          {importOpened && (
            <section hidden={view !== 'import'} aria-label="자료 가져오기 작업">
              <div className="study-row">
                <h1 ref={view === 'import' ? heading : undefined} tabIndex={-1}>
                  내 파일 가져오기
                </h1>
                <button className="study-btn" onClick={back}>
                  학습 홈
                </button>
              </div>
              <p className="study-muted">
                엑셀의 문제와 답을 가져오거나, 문서에서 학습 문제를 만들어요.
              </p>
              <div className="study-import">
                <Suspense fallback={<p role="status">파일 도구를 불러오고 있어요.</p>}>
                  <ImportPanel onImport={importDeck} generate={generate} />
                </Suspense>
              </div>
            </section>
          )}
          {view === 'learn' && round && activeQuestion && (
            <section className="study-learning">
              <div className="study-row">
                <button className="study-btn" onClick={back}>
                  ← 학습 홈
                </button>
                <span className="study-muted">
                  {stats.rounds + 1}라운드 ·{' '}
                  {round.initialIds.filter((id) => round.levels[id] === 2).length} /{' '}
                  {round.initialIds.length} 외움
                </span>
              </div>
              <div className="study-question-card study-card">
                <div className="study-row">
                  <span className="study-badge">{isWritten ? '직접 쓰기' : '답 고르기'}</span>
                  <span className="study-muted study-small">
                    {activeQuestion.chapter} · {activeQuestion.number}번
                  </span>
                </div>
                {activeQuestion.sub && (
                  <p className="study-question-context">{activeQuestion.sub}</p>
                )}
                <h1 ref={heading} tabIndex={-1} className="study-question" data-testid="question">
                  {activeQuestion.question}
                </h1>
                {isWritten ? (
                  <div role="group" aria-label="직접 쓰기">
                    <label htmlFor="written-answer" className="study-sr-only">
                      정답 입력
                    </label>
                    <input
                      ref={answerInput}
                      id="written-answer"
                      className="study-input study-input-answer"
                      autoComplete="off"
                      value={written}
                      onChange={(e) => setWritten(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key !== 'Enter') return;
                        if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
                        e.preventDefault();
                        e.stopPropagation();
                        if (e.repeat) return;
                        if (!feedback && written.trim()) answer(written);
                      }}
                      disabled={!!feedback}
                      placeholder="떠오르는 답을 적어 주세요"
                    />
                    <div className="study-row study-section">
                      <button
                        type="button"
                        className="study-btn"
                        disabled={!!feedback}
                        onClick={() => answer('', true)}
                      >
                        모르겠어요
                      </button>
                      <button
                        type="button"
                        className="study-btn study-btn-primary"
                        disabled={!!feedback || !written.trim()}
                        onClick={() => answer(written)}
                      >
                        정답 확인
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="study-choices">
                    {choices.map((choice, index) => (
                      <button
                        key={choice}
                        className="study-btn study-choice"
                        disabled={!!feedback}
                        onClick={() => answer(choice)}
                        data-result={
                          feedback
                            ? gradeAnswer(activeQuestion, choice).correct
                              ? 'correct'
                              : choice === feedback.chosen
                                ? 'wrong'
                                : undefined
                            : undefined
                        }
                      >
                        <span className="study-key" aria-hidden>
                          {index + 1}
                        </span>
                        <span>{choice}</span>
                      </button>
                    ))}
                  </div>
                )}
                {feedback && (
                  <div className="study-feedback" data-correct={feedback.correct} role="status">
                    <strong>{feedback.correct ? '✓ 정답이에요' : '다시 익혀 볼까요?'}</strong>
                    <p>
                      {feedback.correct
                        ? '잘 기억했어요. 다음 문제로 넘어갑니다.'
                        : `정답: ${renderAnswer(feedback.question, { showCountry })}`}
                    </p>
                    <div className="study-controls">
                      {!feedback.correct && feedback.written && round.lastAnswer?.written && (
                        <button
                          className="study-btn"
                          onClick={() => {
                            const next = submitAnswer(round, feedback.question, true, {
                              manual: true,
                            });
                            persistRound(next);
                            setFeedback({ ...feedback, correct: true });
                          }}
                        >
                          내 답도 맞아요
                        </button>
                      )}
                      <button className="study-btn" onClick={finishFeedback}>
                        {feedback.correct ? '바로 계속' : '계속하기'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
              <p className="study-footer-note">
                {isWritten ? 'Enter 정답 확인' : '숫자 1–4 보기 선택'} · 결과를 본 뒤 숫자 1–4 또는
                Enter로 계속하기
                <br />
                중간에 나가도 지금까지 익힌 단계는 저장돼요.
              </p>
            </section>
          )}
          {(view === 'round' || view === 'result') && (
            <section className="study-learning study-card study-empty">
              <p className="study-eyebrow">
                {view === 'result' ? '선택한 범위 학습 완료' : '한 걸음 더 익혔어요'}
              </p>
              <h1 ref={heading} tabIndex={-1}>
                {view === 'result' ? '모두 외웠어요!' : `${stats.rounds}라운드 완료`}
              </h1>
              <p className="study-muted">
                {view === 'result'
                  ? '다른 단원을 고르거나 플래시카드로 다시 살펴보세요.'
                  : `다음 일곱 문제로 이어가요. ${remaining}문제가 남았어요.`}
              </p>
              <div className="study-score">
                <div>
                  <strong>{stats.correct}</strong>
                  <span>정답</span>
                </div>
                <div>
                  <strong>{stats.wrong}</strong>
                  <span>오답</span>
                </div>
                <div>
                  <strong>{stats.rounds}</strong>
                  <span>라운드</span>
                </div>
              </div>
              <div className="study-controls justify-center">
                <button className="study-btn" onClick={back}>
                  학습 홈
                </button>
                {view === 'round' && (
                  <button className="study-btn study-btn-primary" onClick={() => startLearn(false)}>
                    다음 라운드 →
                  </button>
                )}
              </div>
              {view === 'round' && (
                <p className="study-footer-note">숫자 1–4 또는 Enter로 다음 라운드</p>
              )}
              {stats.wrongIds.length > 0 && (
                <details className="study-section text-left">
                  <summary className="study-btn">
                    학습 중 틀렸던 문제 {stats.wrongIds.length}개
                  </summary>
                  <ul className="study-wrong-list">
                    {stats.wrongIds.map((id) => {
                      const q = questions.find((item) => item.id === id);
                      return q ? (
                        <li key={id}>
                          <strong>{q.question}</strong>
                          <p className="study-muted">정답: {renderAnswer(q, { showCountry })}</p>
                        </li>
                      ) : null;
                    })}
                  </ul>
                </details>
              )}
            </section>
          )}
          {view === 'flash' && flash[flashIndex] && (
            <section className="study-learning">
              <div className="study-row">
                <button className="study-btn" onClick={back}>
                  ← 학습 홈
                </button>
                <span className="study-muted">
                  {flashIndex + 1} / {flash.length}
                </span>
                <button
                  className="study-btn"
                  onClick={() => {
                    setFlash(shuffleItems(flash));
                    setFlashIndex(0);
                    setUnknown([]);
                    setFlipped(false);
                  }}
                >
                  다시 섞기
                </button>
              </div>
              <button
                className="study-flash"
                data-flipped={flipped}
                onClick={() => setFlipped((v) => !v)}
                aria-label={flipped ? '정답 카드, 눌러서 문제 보기' : '문제 카드, 눌러서 정답 보기'}
              >
                <span className="study-badge">{flipped ? '정답' : '문제'}</span>
                <span className="study-question">
                  {flipped
                    ? renderAnswer(flash[flashIndex], { showCountry })
                    : flash[flashIndex].question}
                </span>
                <span className="study-muted study-small">눌러서 뒤집기 · Space</span>
              </button>
              <div className="study-launch">
                <button className="study-btn" onClick={() => flashAnswer(false)}>
                  ← 몰라요
                </button>
                <button className="study-btn study-btn-primary" onClick={() => flashAnswer(true)}>
                  알아요 →
                </button>
              </div>
              <p className="study-footer-note">
                플래시카드는 익숙함을 확인하는 연습이에요. 학습하기의 진도는 바꾸지 않습니다.
              </p>
            </section>
          )}
          {view === 'flash-result' && (
            <section className="study-learning study-card study-empty">
              <h1 ref={heading} tabIndex={-1}>
                한 바퀴 마쳤어요
              </h1>
              <p className="study-muted">
                {flash.length - unknown.length}개는 알아요 · {unknown.length}개는 한 번 더
              </p>
              <div className="study-controls justify-center">
                <button className="study-btn" onClick={back}>
                  학습 홈
                </button>
                {unknown.length > 0 && (
                  <button
                    className="study-btn study-btn-primary"
                    onClick={() => startFlash(unknown)}
                  >
                    몰라요만 다시 ({unknown.length})
                  </button>
                )}
                <button className="study-btn" onClick={() => startFlash()}>
                  전체 다시
                </button>
              </div>
            </section>
          )}
        </>
      )}
      <dialog
        ref={dialog}
        className="study-dialog"
        aria-labelledby="clear-title"
        onCancel={() => setClearStep(0)}
        onClose={() => setClearStep(0)}
      >
        <h2 id="clear-title">
          {clearStep === 1 ? '학습 기록을 지울까요?' : '한 번 더 확인해 주세요'}
        </h2>
        <p className="study-muted">
          현재 문제집의 모든 학습 단계를 처음으로 되돌립니다. 문제와 답, 다른 문제집의 기록은
          남습니다.
        </p>
        <div className="study-controls">
          <button className="study-btn" autoFocus onClick={() => setClearStep(0)}>
            취소
          </button>
          <button
            className="study-btn study-btn-danger"
            onClick={async () => {
              if (clearStep === 1) setClearStep(2);
              else {
                const saved = await commit({ ...progress, levels: {} });
                if (!mounted.current) return;
                setClearStep(0);
                setNotice(
                  saved
                    ? '이 문제집의 학습 기록을 지웠어요.'
                    : '이 화면의 학습 기록만 초기화했어요. 기기에 저장된 기록은 바뀌지 않았습니다.',
                );
              }
            }}
          >
            {clearStep === 1 ? '다음 단계' : '학습 기록 지우기'}
          </button>
        </div>
      </dialog>
    </main>
  );
}
