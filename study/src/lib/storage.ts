import type {
  DeckProgress,
  Level,
  Progress,
  Question,
  StorageAdapter,
  StorageError,
  StorageResult,
  StudyDeck,
} from './types';

export const STORAGE_KEY = 'hnk_v1';
export const MAX_DECKS = 20;
export const MAX_DECK_QUESTIONS = 5000;
export const MAX_STORAGE_CHARACTERS = 2_000_000;
const RESERVED = new Set(['__proto__', 'prototype', 'constructor']);
type AccountStore = { version: 1; accounts: Record<string, Progress> };
// Each browser context remembers the exact account state it last read/wrote.
// Comparing only this account allows unrelated accounts to save independently.
const baselines = new WeakMap<StorageAdapter, Map<string, string | null>>();
const accountSnapshot = (store: AccountStore, scope: string): string | null =>
  Object.hasOwn(store.accounts, scope) ? JSON.stringify(store.accounts[scope]) : null;
function remember(adapter: StorageAdapter, scope: string, snapshot: string | null): void {
  let accounts = baselines.get(adapter);
  if (!accounts) {
    accounts = new Map();
    baselines.set(adapter, accounts);
  }
  accounts.set(scope, snapshot);
}
const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const key = (value: unknown): value is string =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= 200 &&
  !RESERVED.has(value) &&
  !/[\x00-\x1f\x7f]/.test(value);
const text = (value: unknown, maximum: number, empty = false): value is string =>
  typeof value === 'string' && (empty || value.length > 0) && value.length <= maximum;

export function defaultProgress(): Progress {
  return {
    initialized: false,
    selectedChapters: [],
    shuffle: true,
    levels: {},
    decks: [],
    activeDeckId: 'seed',
    theme: 'system',
    deckProgress: {},
  };
}

export function validQuestion(value: unknown): value is Question {
  return (
    record(value) &&
    key(value.id) &&
    text(value.chapter, 500) &&
    text(value.sub, 1000, true) &&
    Number.isSafeInteger(value.number) &&
    Number(value.number) > 0 &&
    Number(value.number) <= 1_000_000 &&
    text(value.question, 20_000) &&
    text(value.answer, 4000) &&
    ['short', 'multi', 'sequence'].includes(String(value.type))
  );
}

function deckState(value: unknown): DeckProgress | null {
  if (
    !record(value) ||
    !Array.isArray(value.selectedChapters) ||
    value.selectedChapters.length > 1000 ||
    !value.selectedChapters.every((chapter) => text(chapter, 500)) ||
    typeof value.shuffle !== 'boolean' ||
    !record(value.levels) ||
    Object.keys(value.levels).length > 100_000
  )
    return null;
  const levels: Record<string, Level> = {};
  for (const [id, level] of Object.entries(value.levels)) {
    if (!key(id) || ![0, 1, 2].includes(level as number)) return null;
    levels[id] = level as Level;
  }
  return {
    selectedChapters: [...new Set(value.selectedChapters as string[])],
    shuffle: value.shuffle,
    levels,
  };
}

/** Whitelist fields; input objects and unknown keys never become trusted state. */
export function sanitizeProgress(value: unknown): Progress | null {
  const common = deckState(value);
  if (!record(value) || !common || typeof value.initialized !== 'boolean') return null;
  const result: Progress = { ...common, initialized: value.initialized };
  if (value.theme !== undefined) {
    if (!['system', 'light', 'dark'].includes(String(value.theme))) return null;
    result.theme = value.theme as Progress['theme'];
  }
  if (value.activeDeckId !== undefined) {
    if (!key(value.activeDeckId)) return null;
    result.activeDeckId = value.activeDeckId;
  }
  if (value.decks !== undefined) {
    if (!Array.isArray(value.decks) || value.decks.length > MAX_DECKS) return null;
    const ids = new Set<string>();
    const decks: StudyDeck[] = [];
    for (const deck of value.decks) {
      if (
        !record(deck) ||
        !key(deck.id) ||
        deck.id === 'seed' ||
        ids.has(deck.id) ||
        !text(deck.title, 200) ||
        !text(deck.createdAt, 40) ||
        !Number.isFinite(Date.parse(deck.createdAt)) ||
        !Array.isArray(deck.questions) ||
        !deck.questions.length ||
        deck.questions.length > MAX_DECK_QUESTIONS ||
        !deck.questions.every(validQuestion)
      )
        return null;
      if (new Set(deck.questions.map((question) => question.id)).size !== deck.questions.length)
        return null;
      ids.add(deck.id);
      decks.push({
        id: deck.id,
        title: deck.title,
        createdAt: deck.createdAt,
        questions: deck.questions.map((question) => ({
          id: question.id,
          chapter: question.chapter,
          sub: question.sub,
          number: question.number,
          question: question.question,
          answer: question.answer,
          type: question.type,
        })),
      });
    }
    result.decks = decks;
  }
  if (value.deckProgress !== undefined) {
    if (!record(value.deckProgress) || Object.keys(value.deckProgress).length > MAX_DECKS + 1)
      return null;
    result.deckProgress = {};
    for (const [id, progress] of Object.entries(value.deckProgress)) {
      if (!key(id)) return null;
      const clean = deckState(progress);
      if (!clean) return null;
      result.deckProgress[id] = clean;
    }
  }
  return result;
}

function readStore(adapter: StorageAdapter): { store?: AccountStore; error?: StorageError } {
  let raw: string | null;
  try {
    raw = adapter.getItem(STORAGE_KEY);
  } catch {
    return { error: 'unavailable' };
  }
  if (raw === null) return { store: { version: 1, accounts: {} } };
  if (raw.length > MAX_STORAGE_CHARACTERS) return { error: 'corrupt' };
  try {
    const value: unknown = JSON.parse(raw);
    if (
      !record(value) ||
      value.version !== 1 ||
      !record(value.accounts) ||
      Object.keys(value.accounts).length > 100
    )
      return { error: 'corrupt' };
    const accounts: Record<string, Progress> = {};
    for (const [scope, progress] of Object.entries(value.accounts)) {
      if (!key(scope)) return { error: 'corrupt' };
      const clean = sanitizeProgress(progress);
      if (!clean) return { error: 'corrupt' };
      accounts[scope] = clean;
    }
    return { store: { version: 1, accounts } };
  } catch {
    return { error: 'corrupt' };
  }
}

export function loadProgress(adapter: StorageAdapter, scope: string): Progress {
  if (!key(scope)) return { ...defaultProgress(), loadError: 'invalid' };
  const { store, error } = readStore(adapter);
  if (error || !store) return { ...defaultProgress(), loadError: error ?? 'corrupt' };
  remember(adapter, scope, accountSnapshot(store, scope));
  return Object.hasOwn(store.accounts, scope) ? store.accounts[scope] : defaultProgress();
}

export function saveProgress(
  adapter: StorageAdapter,
  scope: string,
  progress: Progress,
): StorageResult {
  if (!key(scope)) return { ok: false, error: 'invalid' };
  const clean = sanitizeProgress(progress);
  if (!clean) return { ok: false, error: 'invalid' };
  const { store, error } = readStore(adapter);
  // Never replace unreadable/corrupt storage with an empty account map.
  if (error || !store) return { ok: false, error: error ?? 'corrupt' };
  const current = accountSnapshot(store, scope);
  const expected = baselines.get(adapter)?.get(scope);
  // Existing accounts must have been read first. A conflict never adopts the
  // newer baseline silently: the caller must explicitly reload before saving.
  if (current !== (expected === undefined ? null : expected))
    return { ok: false, error: 'conflict' };
  if (!Object.hasOwn(store.accounts, scope) && Object.keys(store.accounts).length >= 100)
    return { ok: false, error: 'quota' };
  store.accounts[scope] = clean;
  const serialized = JSON.stringify(store);
  if (serialized.length > MAX_STORAGE_CHARACTERS) return { ok: false, error: 'quota' };
  try {
    adapter.setItem(STORAGE_KEY, serialized);
    remember(adapter, scope, JSON.stringify(clean));
    return { ok: true };
  } catch (error) {
    const name = error && typeof error === 'object' && 'name' in error ? String(error.name) : '';
    return { ok: false, error: /quota/i.test(name) ? 'quota' : 'unavailable' };
  }
}

export interface StorageLockManager {
  request<T>(name: string, callback: () => T | Promise<T>): Promise<T>;
}

/** Serialize the whole hnk_v1 read/check/write across same-origin tabs. */
export async function saveProgressLocked(
  adapter: StorageAdapter,
  scope: string,
  progress: Progress,
  locks: StorageLockManager | undefined,
  isCurrent: () => boolean = () => true,
): Promise<StorageResult> {
  // A browser without an atomic cross-tab lock may still study/export in
  // memory, but must not silently risk another tab's only copy of a deck.
  if (!locks) return { ok: false, error: 'unavailable' };
  try {
    return await locks.request(STORAGE_KEY, () =>
      isCurrent() ? saveProgress(adapter, scope, progress) : { ok: false, error: 'unavailable' },
    );
  } catch {
    return { ok: false, error: 'unavailable' };
  }
}

/** Reset only this account; a failed write leaves every previous account intact. */
export function resetProgress(adapter: StorageAdapter, scope: string): StorageResult {
  return saveProgress(adapter, scope, { ...defaultProgress(), initialized: true });
}
