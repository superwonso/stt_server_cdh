import { describe, expect, it } from 'vitest';
import { gradeAnswer, inferQuestionType, answerAliases } from '../src/lib/grading';
import { getChoices, sequenceDistractors } from '../src/lib/choices';
import { createRound, submitAnswer } from '../src/lib/round';
import {
  defaultProgress,
  loadProgress,
  saveProgress,
  saveProgressLocked,
  resetProgress,
  sanitizeProgress,
  STORAGE_KEY,
} from '../src/lib/storage';
import type { StorageLockManager } from '../src/lib/storage';
import type { Question, StorageAdapter } from '../src/lib/types';
import seedData from '../data/questions.json';

function question(id: string, answer: string, extra: Partial<Question> = {}): Question {
  return {
    id,
    chapter: '단원',
    sub: '소단원',
    number: 1,
    question: '합성 문제',
    answer,
    type: inferQuestionType(answer),
    ...extra,
  };
}
function memory(): StorageAdapter & { values: Map<string, string> } {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => {
      values.set(key, value);
    },
    removeItem: (key) => {
      values.delete(key);
    },
  };
}

describe('written answer grading preserves all required information', () => {
  it('treats slash-separated answers as all required items, not alternatives', () => {
    const item = question('a', '김구(김창수) / 안중근');
    expect(item.type).toBe('multi');
    expect(gradeAnswer(item, '안중근 / 김창수').correct).toBe(true);
    expect(gradeAnswer(item, '김구').correct).toBe(false);
    expect(gradeAnswer(item, '김창수').correct).toBe(false);
  });
  it('ignores spacing, case and punctuation without fuzzy name matching', () => {
    expect(gradeAnswer(question('a', '5·18 민주화 운동'), '5.18민주화운동').correct).toBe(true);
    expect(gradeAnswer(question('a', 'UN'), 'un').correct).toBe(true);
    expect(gradeAnswer(question('a', '정약용'), '정약전').correct).toBe(false);
  });
  it('accepts parenthetical names and the main answer, but not dates or counts', () => {
    const item = question('a', '김구(김창수)');
    expect(gradeAnswer(item, '김구').correct).toBe(true);
    expect(gradeAnswer(item, '김창수').correct).toBe(true);
    expect(answerAliases('정약용(호: 다산)')).toContain('다산');
    expect(gradeAnswer(question('a', '사건(1910년)'), '1910년').correct).toBe(false);
    expect(gradeAnswer(question('a', '사건(1910~1919)'), '1910~1919').correct).toBe(false);
  });
  it('requires all multiple answers exactly once, in any order', () => {
    const item = question('a', '김구(김창수), 안중근, 유관순');
    expect(gradeAnswer(item, '유관순, 김창수, 안중근').correct).toBe(true);
    expect(gradeAnswer(item, '김구, 안중근').correct).toBe(false);
    expect(gradeAnswer(item, '김구, 김구, 안중근').correct).toBe(false);
    expect(gradeAnswer(item, '김구, 안중근, 유관순, 이순신').correct).toBe(false);
  });
  it('retains overlapping required aliases using one-to-one matching', () => {
    expect(gradeAnswer(question('a', '가나다(라마바), 가나다'), '가나다, 라마바').correct).toBe(
      true,
    );
    expect(gradeAnswer(question('a', '가나다(라마바), 사아자'), '라마바, 가나다').correct).toBe(
      false,
    );
  });
  it('compares chronological order and supports circled, separated and compact digits', () => {
    const item = question('a', '④→①→③→②');
    for (const answer of ['④①③②', '4,1,3,2', '4 1 3 2', '4132'])
      expect(gradeAnswer(item, answer).correct).toBe(true);
    expect(gradeAnswer(item, '①④③②').correct).toBe(false);
    expect(gradeAnswer(question('a', '①→⑩→②'), '1,10,2').correct).toBe(true);
    expect(gradeAnswer(question('a', '①→⑩→②'), '1102').correct).toBe(false);
    expect(inferQuestionType('⑬(1866)→⑤(사건 직후)→①(1885)')).toBe('sequence');
    expect(gradeAnswer(question('a', '⑬(1866)→⑤(사건 직후)→①(1885)'), '13,5,1').correct).toBe(true);
    expect(
      sequenceDistractors('①(1900)→②(1901)→③(1902)', 3, () => 0).every((value) =>
        ['(1900)', '(1901)', '(1902)'].every((annotation) => value.includes(annotation)),
      ),
    ).toBe(true);
  });
  it('does not accept blanks or punctuation-only input', () => {
    expect(gradeAnswer(question('a', '정답'), '...').correct).toBe(false);
    expect(gradeAnswer(question('a', '정답'), ' ').reason).toBe('empty');
  });
  it('grades named arrow steps in order without turning comma lists or dates into sequences', () => {
    const item = question('named', '신라(고대) → 고려 → 조선');
    expect(item.type).toBe('sequence');
    for (const input of [
      '신라→고려→조선',
      '신라, 고려, 조선',
      '신라 / 고려 / 조선',
      '신라 -> 고려 -> 조선',
    ]) {
      expect(gradeAnswer(item, input).correct).toBe(true);
    }
    for (const input of ['고려→신라→조선', '신라, 조선, 고려', '신라, 고려', '고대, 고려, 조선']) {
      expect(gradeAnswer(item, input).correct).toBe(false);
    }
    expect(gradeAnswer(question('mixed', '①→고려→3'), '1→고려→3').correct).toBe(true);
    expect(gradeAnswer(question('letters', 'Alpha → beta'), 'alpha -> BETA').correct).toBe(true);
    expect(inferQuestionType('신라, 고려, 조선')).toBe('multi');
    expect(inferQuestionType('1919/3/1')).toBe('short');
    expect(gradeAnswer(question('date', '1919/3/1'), '3/1/1919').correct).toBe(false);
  });
});

describe('multiple choice distractors', () => {
  it('has exactly one grading-correct option for every approved seed question', () => {
    const seed = seedData as Question[];
    expect(seed).toHaveLength(665);
    for (const item of seed) {
      const choices = getChoices(item, seed, () => 0.4);
      expect(
        choices.filter((choice) => gradeAnswer(item, choice).correct),
        item.id,
      ).toEqual([item.answer]);
    }
  });
  it('uses distinct answers and avoids a valid alias as an incorrect option', () => {
    const target = question('a', '김구(김창수)');
    const pool = [
      target,
      question('b', '김창수'),
      question('c', '안중근'),
      question('d', '유관순'),
      question('e', '정약용'),
    ];
    const choices = getChoices(target, pool, () => 0.4);
    expect(choices).toHaveLength(4);
    expect(choices).toContain(target.answer);
    expect(choices).not.toContain('김창수');
    expect(new Set(choices).size).toBe(4);
  });
  it('prioritizes the same subsection before other topics', () => {
    const target = question('a', '정답');
    const pool = [
      target,
      ...['나', '다', '라'].map((answer, index) => question(String(index), answer)),
      question('outside', '밖', { sub: '다른 소단원' }),
    ];
    expect(getChoices(target, pool, () => 0)).not.toContain('밖');
  });
  it('creates valid sequence swaps without infinite retries and does not fabricate missing answers', () => {
    const alternatives = sequenceDistractors('①→②→③→④', 3, () => 0);
    expect(alternatives).toHaveLength(3);
    for (const value of alternatives) {
      expect(gradeAnswer({ answer: '①→②→③→④', type: 'sequence' }, value).correct).toBe(false);
      expect([...value].filter((ch) => /[①-④]/.test(ch)).sort()).toEqual(['①', '②', '③', '④']);
    }
    expect(getChoices(question('a', '정답'), [question('a', '정답')])).toEqual(['정답']);
    expect(sequenceDistractors('①②')).toEqual(['②①']);
  });
  it('swaps two named steps and preserves their original annotation text', () => {
    expect(sequenceDistractors('신라(고대) → 고려', 3, () => 0)).toEqual(['고려→신라(고대)']);
    const item = question('named', '신라→고려→조선');
    const choices = getChoices(item, [item], () => 0);
    expect(choices).toHaveLength(4);
    expect(choices.filter((choice) => gradeAnswer(item, choice).correct)).toEqual([item.answer]);
    expect(sequenceDistractors('Alpha⇒beta', 3, () => 0)).toEqual(['beta⇒Alpha']);
    const repeated = `${Array(400).fill('단계A').join('→')}→단계B`;
    expect(sequenceDistractors(repeated, 3, () => 0)).toHaveLength(3);
    expect(sequenceDistractors(Array(400).fill('단계A').join('→'))).toEqual([]);
  });
});

describe('seven-question learning rounds', () => {
  it('remembers older real errors when a later written attempt is manually corrected', () => {
    const item = question('a', '답');
    const start = createRound([item], {});
    const firstWrong = submitAnswer(start, item, false);
    const multipleChoice = submitAnswer(firstWrong, item, true);
    const writtenWrong = submitAnswer(multipleChoice, item, false);
    const fixed = submitAnswer(writtenWrong, item, true, { manual: true });
    expect(fixed.wrongCount).toBe(1);
    expect(fixed.wrongIds).toEqual(['a']);
    expect(fixed.wrongCounts.a).toBe(1);
    expect(fixed.queue).toEqual([]);
  });
  it('selects at most seven distinct unmastered questions', () => {
    const items = Array.from({ length: 10 }, (_, index) => question(String(index), '답'));
    const round = createRound([...items, items[0]], { '0': 2 }, { shuffle: false });
    expect(round.initialIds).toEqual(['1', '2', '3', '4', '5', '6', '7']);
    expect(round.queue).toEqual(round.initialIds);
  });
  it('advances 0→1→2, requeues wrong answers and never changes the input state', () => {
    const item = question('a', '답');
    const start = createRound([item], {}, { shuffle: false });
    const mc = submitAnswer(start, item, true);
    expect(start.levels).toEqual({});
    expect(mc.levels.a).toBe(1);
    expect(mc.queue).toEqual(['a']);
    const wrong = submitAnswer(mc, item, false);
    expect(wrong.levels.a).toBe(0);
    expect(wrong.wrongIds).toEqual(['a']);
    const retry = submitAnswer(wrong, item, true);
    const complete = submitAnswer(retry, item, true);
    expect(complete.queue).toEqual([]);
    expect(complete.levels.a).toBe(2);
    expect(complete.correctCount).toBe(3);
    expect(complete.wrongCount).toBe(1);
  });
  it('manual correction undoes only the latest written error and counts it once', () => {
    const item = question('a', '답');
    const start = createRound([item], { a: 1 });
    const wrong = submitAnswer(start, item, false);
    const fixed = submitAnswer(wrong, item, true, { manual: true });
    expect(fixed.levels.a).toBe(2);
    expect(fixed.queue).toEqual([]);
    expect(fixed.wrongCount).toBe(0);
    expect(fixed.correctCount).toBe(1);
    expect(fixed.wrongIds).toEqual([]);
    expect(() => submitAnswer(fixed, item, true, { manual: true })).toThrow();
    expect(() => submitAnswer(start, question('other', '답'), true)).toThrow();
  });
  it('uses the actual written mode on a single-option retry after its level resets', () => {
    const item = question('single', '합성 정답');
    const start = createRound([item], { single: 1 });
    const wrong = submitAnswer(start, item, false, { written: true });
    expect(wrong.levels.single).toBe(0);
    const secondWrong = submitAnswer(wrong, item, false, { written: true });
    expect(secondWrong.lastAnswer).toMatchObject({ previousLevel: 0, written: true });
    const manual = submitAnswer(secondWrong, item, true, { manual: true });
    expect(manual.levels.single).toBe(2);
    expect(manual.queue).toEqual([]);
    expect(manual.wrongCount).toBe(1);
    expect(submitAnswer(wrong, item, true, { written: true }).levels.single).toBe(2);
    const choiceWrong = submitAnswer(start, item, false, { written: false });
    expect(() => submitAnswer(choiceWrong, item, true, { manual: true })).toThrow();
  });
});

describe('account-scoped bounded local storage', () => {
  it('rejects a stale same-account tab without deleting a newly imported deck or adopting its baseline', () => {
    const shared = memory();
    const tabA: StorageAdapter = { getItem: shared.getItem, setItem: shared.setItem };
    const tabB: StorageAdapter = { getItem: shared.getItem, setItem: shared.setItem };
    const first = loadProgress(tabA, 'owner');
    const stale = loadProgress(tabB, 'owner');
    const imported = {
      ...first,
      initialized: true,
      decks: [
        {
          id: 'synthetic-deck',
          title: '합성 문제집',
          questions: [question('synthetic-question', '합성 답')],
          createdAt: '2026-10-12T00:00:00Z',
        },
      ],
    };
    expect(saveProgress(tabA, 'owner', imported).ok).toBe(true);
    const original = shared.getItem(STORAGE_KEY);
    for (let attempt = 0; attempt < 2; attempt++) {
      expect(saveProgress(tabB, 'owner', { ...stale, theme: 'dark' })).toEqual({
        ok: false,
        error: 'conflict',
      });
      expect(shared.getItem(STORAGE_KEY)).toBe(original);
    }
    const refreshed = loadProgress(tabB, 'owner');
    expect(refreshed.decks).toHaveLength(1);
    expect(saveProgress(tabB, 'owner', { ...refreshed, theme: 'dark' }).ok).toBe(true);
    expect(loadProgress(tabA, 'owner').decks).toHaveLength(1);
  });
  it('serializes whole-store writes for different accounts and rejects concurrent stale same-account writes', async () => {
    const shared = memory();
    const tabA: StorageAdapter = { getItem: shared.getItem, setItem: shared.setItem };
    const tabB: StorageAdapter = { getItem: shared.getItem, setItem: shared.setItem };
    let tail = Promise.resolve();
    const lockNames: string[] = [];
    const locks: StorageLockManager = {
      request<T>(name: string, callback: () => T | Promise<T>): Promise<T> {
        lockNames.push(name);
        const result = tail.then(callback);
        tail = result.then(
          () => undefined,
          () => undefined,
        );
        return result;
      },
    };
    const a = loadProgress(tabA, 'owner-a'),
      b = loadProgress(tabB, 'owner-b');
    expect(
      await Promise.all([
        saveProgressLocked(tabA, 'owner-a', { ...a, initialized: true, levels: { a: 1 } }, locks),
        saveProgressLocked(tabB, 'owner-b', { ...b, initialized: true, levels: { b: 2 } }, locks),
      ]),
    ).toEqual([{ ok: true }, { ok: true }]);
    expect(loadProgress(tabA, 'owner-a').levels).toEqual({ a: 1 });
    expect(loadProgress(tabB, 'owner-b').levels).toEqual({ b: 2 });
    const sameA = loadProgress(tabA, 'owner-a'),
      sameB = loadProgress(tabB, 'owner-a');
    const writes = await Promise.all([
      saveProgressLocked(tabA, 'owner-a', { ...sameA, levels: { a: 2 } }, locks),
      saveProgressLocked(tabB, 'owner-a', { ...sameB, levels: {} }, locks),
    ]);
    expect(writes).toEqual([{ ok: true }, { ok: false, error: 'conflict' }]);
    expect(loadProgress(tabA, 'owner-a').levels).toEqual({ a: 2 });
    expect(loadProgress(tabB, 'owner-b').levels).toEqual({ b: 2 });
    expect(lockNames.every((name) => name === STORAGE_KEY)).toBe(true);
  });
  it('does not write without a browser lock or after the account workspace has closed', async () => {
    const adapter = memory();
    const next = { ...loadProgress(adapter, 'owner'), initialized: true };
    expect(await saveProgressLocked(adapter, 'owner', next, undefined)).toEqual({
      ok: false,
      error: 'unavailable',
    });
    const locks: StorageLockManager = { request: async (_name, callback) => callback() };
    expect(await saveProgressLocked(adapter, 'owner', next, locks, () => false)).toEqual({
      ok: false,
      error: 'unavailable',
    });
    expect(adapter.getItem(STORAGE_KEY)).toBeNull();
  });
  it('a rejected quota write leaves both prior accounts and imported source text intact', () => {
    const adapter = memory();
    saveProgress(adapter, 'owner-a', { ...defaultProgress(), initialized: true, levels: { a: 2 } });
    saveProgress(adapter, 'owner-b', { ...defaultProgress(), initialized: true, levels: { b: 1 } });
    const original = adapter.getItem(STORAGE_KEY);
    adapter.setItem = () => {
      throw Object.assign(new Error(), { name: 'QuotaExceededError' });
    };
    expect(
      saveProgress(adapter, 'owner-a', { ...defaultProgress(), initialized: true, levels: {} }),
    ).toEqual({ ok: false, error: 'quota' });
    expect(adapter.getItem(STORAGE_KEY)).toBe(original);
    expect(loadProgress(adapter, 'owner-a').levels).toEqual({ a: 2 });
    expect(loadProgress(adapter, 'owner-b').levels).toEqual({ b: 1 });
  });
  it('distinguishes first visit from an intentionally empty chapter selection', () => {
    const adapter = memory();
    expect(loadProgress(adapter, 'owner-a').initialized).toBe(false);
    expect(saveProgress(adapter, 'owner-a', { ...defaultProgress(), initialized: true })).toEqual({
      ok: true,
    });
    expect(loadProgress(adapter, 'owner-a').initialized).toBe(true);
    expect(loadProgress(adapter, 'owner-a').selectedChapters).toEqual([]);
  });
  it('isolates accounts and preserves each deck progress, user text and theme', () => {
    const adapter = memory();
    const first = {
      ...defaultProgress(),
      initialized: true,
      levels: { a: 2 as const },
      theme: 'dark' as const,
      decks: [
        {
          id: 'custom-1',
          title: '<script>text</script>',
          createdAt: '2026-10-12T00:00:00Z',
          questions: [question('custom-1:a', '<img>')],
        },
      ],
      activeDeckId: 'custom-1',
      deckProgress: {
        seed: { levels: { a: 1 as const }, selectedChapters: ['원문 단원'], shuffle: false },
      },
    };
    expect(saveProgress(adapter, 'opaque-owner-a', first).ok).toBe(true);
    expect(
      saveProgress(adapter, 'opaque-owner-b', {
        ...defaultProgress(),
        initialized: true,
        levels: { b: 1 },
      }).ok,
    ).toBe(true);
    expect(loadProgress(adapter, 'opaque-owner-a')).toEqual(first);
    expect(loadProgress(adapter, 'opaque-owner-b').levels).toEqual({ b: 1 });
    expect([...adapter.values.keys()]).toEqual([STORAGE_KEY]);
    expect(resetProgress(adapter, 'opaque-owner-b').ok).toBe(true);
    expect(loadProgress(adapter, 'opaque-owner-a')).toEqual(first);
  });
  it('ignores unknown fields, rejects invalid levels and prevents prototype keys', () => {
    const clean = sanitizeProgress({ ...defaultProgress(), unknown: 'discard', levels: { a: 1 } });
    expect(clean).not.toHaveProperty('unknown');
    expect(sanitizeProgress({ ...defaultProgress(), levels: { a: 9 } })).toBeNull();
    expect(saveProgress(memory(), '__proto__', defaultProgress()).ok).toBe(false);
    expect(
      sanitizeProgress(
        JSON.parse(
          '{"initialized":true,"selectedChapters":[],"shuffle":true,"levels":{"__proto__":1}}',
        ),
      ),
    ).toBeNull();
  });
  it('never overwrites corrupt prior storage', () => {
    const adapter = memory();
    adapter.setItem(STORAGE_KEY, '{broken');
    expect(loadProgress(adapter, 'owner-a').loadError).toBe('corrupt');
    expect(saveProgress(adapter, 'owner-a', defaultProgress())).toEqual({
      ok: false,
      error: 'corrupt',
    });
    expect(adapter.getItem(STORAGE_KEY)).toBe('{broken');
  });
  it('reports quota/private-mode errors without claiming the change was saved', () => {
    const quota: StorageAdapter = {
      getItem: () => null,
      setItem: () => {
        throw Object.assign(new Error(), { name: 'QuotaExceededError' });
      },
    };
    const blocked: StorageAdapter = {
      getItem: () => {
        throw new Error();
      },
      setItem: () => {
        throw new Error();
      },
    };
    expect(saveProgress(quota, 'owner-a', defaultProgress())).toEqual({
      ok: false,
      error: 'quota',
    });
    expect(loadProgress(blocked, 'owner-a').loadError).toBe('unavailable');
    expect(saveProgress(blocked, 'owner-a', defaultProgress())).toEqual({
      ok: false,
      error: 'unavailable',
    });
  });
});
