import type { Level, Levels, Question, RoundState } from './types';
import { shuffled } from './choices';

export function createRound(
  questions: readonly Question[],
  levels: Levels,
  options: { size?: number; shuffle?: boolean; rng?: () => number } = {},
): RoundState {
  const size = Math.max(1, Math.min(7, Math.floor(options.size ?? 7)));
  const unique = [...new Map(questions.map((question) => [question.id, question])).values()];
  const remaining = unique.filter((question) => levels[question.id] !== 2);
  const ordered = options.shuffle === false ? remaining : shuffled(remaining, options.rng);
  const initialIds = ordered.slice(0, size).map((question) => question.id);
  return {
    queue: [...initialIds],
    initialIds,
    levels: { ...levels },
    correctCount: 0,
    wrongCount: 0,
    wrongIds: [],
    wrongCounts: {},
    lastAnswer: null,
  };
}

export function submitAnswer(
  round: RoundState,
  question: Pick<Question, 'id'>,
  correct: boolean,
  options: { manual?: boolean; written?: boolean } = {},
): RoundState {
  const id = question.id;
  const levels = { ...round.levels },
    wrongCounts = { ...round.wrongCounts };
  if (options.manual) {
    const previous = round.lastAnswer;
    if (
      !correct ||
      !previous ||
      previous.questionId !== id ||
      previous.correct ||
      !previous.written
    ) {
      throw new Error('Only the most recent written-answer error can be manually corrected.');
    }
    levels[id] = 2;
    wrongCounts[id] = Math.max(0, (wrongCounts[id] ?? 0) - 1);
    if (!wrongCounts[id]) delete wrongCounts[id];
    return {
      ...round,
      queue: round.queue.filter((value) => value !== id),
      levels,
      wrongCounts,
      correctCount: round.correctCount + 1,
      wrongCount: Math.max(0, round.wrongCount - 1),
      wrongIds: round.wrongIds.filter((value) => value !== id || !!wrongCounts[id]),
      lastAnswer: { ...previous, correct: true, manual: true },
    };
  }
  if (round.queue[0] !== id) throw new Error('The answer does not belong to the current question.');
  const previousLevel: Level = levels[id] === 1 ? 1 : 0;
  const written = options.written ?? previousLevel === 1;
  levels[id] = correct ? (written ? 2 : 1) : 0;
  const queue = round.queue.slice(1);
  if (levels[id] < 2) queue.push(id);
  if (!correct) wrongCounts[id] = (wrongCounts[id] ?? 0) + 1;
  return {
    ...round,
    queue,
    levels,
    wrongCounts,
    correctCount: round.correctCount + (correct ? 1 : 0),
    wrongCount: round.wrongCount + (correct ? 0 : 1),
    wrongIds:
      !correct && !round.wrongIds.includes(id) ? [...round.wrongIds, id] : [...round.wrongIds],
    lastAnswer: { questionId: id, correct, previousLevel, written, manual: false },
  };
}
