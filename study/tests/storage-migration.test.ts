import { describe, expect, it } from 'vitest';
import {
  defaultProgress,
  loadProgress,
  sanitizeProgress,
  saveProgress,
  STORAGE_KEY,
} from '../src/lib/storage';
import type { StorageAdapter } from '../src/lib/types';
import { renderAnswer } from '../src/lib/answer';
import { gradeAnswer } from '../src/lib/grading';

function memory(raw: string | null = null): StorageAdapter {
  return {
    getItem: () => raw,
    setItem: (_key, value) => {
      raw = value;
    },
  };
}

const legacyQuestion = {
  id: 'unchanged-original-id',
  chapter: '조선',
  sub: '왕',
  number: 3,
  question: '훈민정음을 창제한 왕은?',
  answer: '세종',
  type: 'short',
};
const legacyProgress = () => ({
  initialized: true,
  selectedChapters: ['조선'],
  shuffle: false,
  levels: { 'unchanged-original-id': 2, another: 1, retry: 0 },
  theme: 'dark',
  decks: [
    {
      id: 'unchanged-deck-id',
      title: '기존 문제집',
      createdAt: '2026-10-12T00:00:00Z',
      questions: [{ ...legacyQuestion }],
    },
  ],
  activeDeckId: 'unchanged-deck-id',
  deckProgress: {
    seed: { selectedChapters: ['기존 단원'], shuffle: true, levels: { seedQuestion: 1 } },
  },
});

describe('structured-answer storage migration', () => {
  it('reads the legacy envelope without writing and retains identifiers, levels and preferences', () => {
    const prior = legacyProgress();
    const raw = JSON.stringify({ version: 1, accounts: { owner: prior } });
    const adapter = memory(raw);
    const result = loadProgress(adapter, 'owner');
    expect(result.loadError).toBeUndefined();
    expect(result).toMatchObject({ ...prior, dataVersion: 2, showCountry: true });
    expect(result.decks?.[0].questions[0]).toMatchObject({
      ...legacyQuestion,
      dataVersion: 2,
      answerParts: [{ kind: 'ruler', name: '세종', country: '조선' }],
    });
    expect(result.decks?.[0].questions[0].aliases).toBeInstanceOf(Array);
    expect(adapter.getItem(STORAGE_KEY)).toBe(raw);
  });

  it.each([undefined, null, 'false', 0, {}, []])(
    'defaults malformed showCountry %j to true',
    (value) => {
      const result = sanitizeProgress({ ...legacyProgress(), showCountry: value });
      expect(result?.showCountry).toBe(true);
      expect(result?.levels).toEqual(legacyProgress().levels);
    },
  );

  it('preserves an explicit false display setting and deep clones answer metadata', () => {
    const upgraded = sanitizeProgress({ ...legacyProgress(), showCountry: false })!;
    upgraded.decks![0].questions[0].aliases = ['세종대왕'];
    const copy = sanitizeProgress(upgraded)!;
    expect(copy.showCountry).toBe(false);
    expect(copy.decks![0].questions[0].aliases).toContain('세종대왕');
    copy.decks![0].questions[0].aliases.push('가짜 별칭');
    const copiedPart = copy.decks![0].questions[0].answerParts[0];
    if (copiedPart.kind !== 'ruler') throw new Error('Expected ruler metadata');
    copiedPart.name = '바뀐 글';
    expect(upgraded.decks![0].questions[0].aliases).not.toContain('가짜 별칭');
    expect(upgraded.decks![0].questions[0].answerParts[0]).toMatchObject({ name: '세종' });
  });

  it('retains an explicit original country after the pure answer is saved and loaded again', () => {
    const previous = legacyProgress();
    previous.decks[0].questions[0].chapter = 'Ⅳ-1 조선';
    previous.decks[0].questions[0].answer = '고려 성종';
    const first = sanitizeProgress(previous)!;
    expect(first.decks![0].questions[0].answerParts).toEqual([
      { kind: 'ruler', name: '성종', country: '고려' },
    ]);
    const second = sanitizeProgress(first)!;
    expect(second.decks![0].questions[0].answerParts).toEqual(
      first.decks![0].questions[0].answerParts,
    );
  });

  it('preserves a country-parenthesis pair and all accepted forms through legacy migration and saving', () => {
    const previous = legacyProgress();
    previous.decks[0].questions[0] = {
      ...legacyQuestion,
      chapter: 'Ⅳ-1 조선',
      question: '향리 제도를 정비한 국가와 왕은?',
      answer: '고려(성종)',
    };
    const adapter = memory(JSON.stringify({ version: 1, accounts: { owner: previous } }));
    const first = loadProgress(adapter, 'owner');
    expect(saveProgress(adapter, 'owner', first)).toEqual({ ok: true });
    const second = loadProgress(adapter, 'owner');
    expect(second.levels).toEqual(previous.levels);
    expect(second.selectedChapters).toEqual(previous.selectedChapters);
    expect(second.theme).toBe(previous.theme);
    expect(second.decks?.[0].id).toBe(previous.decks[0].id);
    const question = second.decks![0].questions[0];
    expect(question.id).toBe(legacyQuestion.id);
    expect(question.answer).toBe('고려(성종)');
    expect(question.answerParts).toEqual([
      { kind: 'text', value: '고려(' },
      { kind: 'ruler', name: '성종', country: '고려' },
      { kind: 'text', value: ')' },
    ]);
    expect(renderAnswer(question, { showCountry: true })).toBe('고려(성종)');
    expect(renderAnswer(question, { showCountry: false })).toBe('고려(성종)');
    for (const answer of ['고려(성종)', '고려/성종', '성종/고려', '고려 성종', '성종 고려']) {
      expect(gradeAnswer(question, answer).correct, answer).toBe(true);
    }
    expect(gradeAnswer(question, '조선 성종').correct).toBe(false);
    expect(sanitizeProgress(second)).toEqual(second);
  });

  it('keeps the migration baseline stable and rejects a stale tab after a real progress edit', () => {
    const shared = memory(JSON.stringify({ version: 1, accounts: { owner: legacyProgress() } }));
    const tabA: StorageAdapter = { getItem: shared.getItem, setItem: shared.setItem };
    const tabB: StorageAdapter = { getItem: shared.getItem, setItem: shared.setItem };
    const a = loadProgress(tabA, 'owner');
    const b = loadProgress(tabB, 'owner');
    expect(saveProgress(tabA, 'owner', a)).toEqual({ ok: true });
    expect(JSON.parse(shared.getItem(STORAGE_KEY)!).version).toBe(2);
    // A schema-only rewrite does not fabricate a conflict for an equivalent account.
    expect(saveProgress(tabB, 'owner', b)).toEqual({ ok: true });
    expect(saveProgress(tabA, 'owner', { ...a, showCountry: false })).toEqual({ ok: true });
    const latest = shared.getItem(STORAGE_KEY);
    expect(saveProgress(tabB, 'owner', { ...b, theme: 'light' })).toEqual({
      ok: false,
      error: 'conflict',
    });
    expect(shared.getItem(STORAGE_KEY)).toBe(latest);
  });

  it('retains unrelated legacy accounts when one account is upgraded and saved', () => {
    const raw = JSON.stringify({
      version: 1,
      accounts: { owner: legacyProgress(), other: legacyProgress() },
    });
    const adapter = memory(raw);
    const before = loadProgress(adapter, 'other');
    const owner = loadProgress(adapter, 'owner');
    expect(saveProgress(adapter, 'owner', { ...owner, showCountry: false }).ok).toBe(true);
    expect(loadProgress(adapter, 'other')).toEqual(before);
  });

  it('still refuses to overwrite a corrupt envelope or invalid original question', () => {
    const prior = legacyProgress();
    prior.decks[0].questions[0].id = '__proto__';
    const raw = JSON.stringify({ version: 1, accounts: { owner: prior } });
    const adapter = memory(raw);
    expect(loadProgress(adapter, 'owner').loadError).toBe('corrupt');
    expect(saveProgress(adapter, 'owner', defaultProgress()).ok).toBe(false);
    expect(adapter.getItem(STORAGE_KEY)).toBe(raw);
  });
});
