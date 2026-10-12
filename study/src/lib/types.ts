export type QuestionType = 'short' | 'multi' | 'sequence';
export interface Question {
  id: string;
  chapter: string;
  sub: string;
  number: number;
  question: string;
  answer: string;
  type: QuestionType;
}
export type Level = 0 | 1 | 2;
export type Levels = Record<string, Level>;
export type ChapterManifest = [chapter: string, count: number][];
export interface DeckProgress {
  selectedChapters: string[];
  shuffle: boolean;
  levels: Levels;
}
export interface StudyDeck {
  id: string;
  title: string;
  questions: Question[];
  createdAt: string;
}
export type StorageError = 'unavailable' | 'quota' | 'invalid' | 'corrupt' | 'conflict';
export interface Progress extends DeckProgress {
  initialized: boolean;
  decks?: StudyDeck[];
  activeDeckId?: string;
  theme?: 'system' | 'light' | 'dark';
  deckProgress?: Record<string, DeckProgress>;
  /** A read failure, never persisted as study data. */
  loadError?: StorageError;
}
export interface StorageAdapter {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}
export interface StorageResult {
  ok: boolean;
  error?: StorageError;
}
export interface GradeResult {
  correct: boolean;
  normalizedInput: string;
  reason: 'exact' | 'alias' | 'multiple' | 'sequence' | 'mismatch' | 'empty';
}
export interface RoundState {
  queue: string[];
  initialIds: string[];
  levels: Levels;
  correctCount: number;
  wrongCount: number;
  wrongIds: string[];
  wrongCounts: Record<string, number>;
  lastAnswer: {
    questionId: string;
    correct: boolean;
    previousLevel: Level;
    written: boolean;
    manual: boolean;
  } | null;
}
