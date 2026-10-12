import type { Question } from './types';
import { gradeAnswer, normalizeAnswer, sequenceTokens } from './grading';

export function shuffled<T>(input: readonly T[], rng: () => number = Math.random): T[] {
  const result = [...input];
  for (let index = result.length - 1; index > 0; index--) {
    const value = rng();
    const other = Math.min(
      index,
      Math.max(0, Math.floor((Number.isFinite(value) ? value : 0) * (index + 1))),
    );
    [result[index], result[other]] = [result[other], result[index]];
  }
  return result;
}

export function sequenceDistractors(
  answer: string,
  count = 3,
  rng: () => number = Math.random,
): string[] {
  const tokens = sequenceTokens(answer);
  if (!tokens || tokens.length < 2) return [];
  const arrow = /→|⇒|->|>/.exec(answer)?.[0];
  const display = arrow
    ? answer.split(/\s*(?:→|⇒|->|>)\s*/)
    : /[①-⑳]/.test(answer)
      ? tokens.map((value) => String.fromCodePoint(0x2460 + Number(value) - 1))
      : tokens;
  const joiner = arrow || (/[①-⑳]/.test(answer) ? '' : ' → ');
  const unique = new Set<string>();
  // A representative of each distinct step avoids quadratic work for long
  // imported sequences containing repeated steps. Stop after enough swaps.
  const representatives = new Map<string, number>();
  tokens.forEach((token, index) => {
    if (!representatives.has(token)) representatives.set(token, index);
  });
  if (representatives.size < 2 || count < 1) return [];
  swaps: for (const left of shuffled([...representatives.values()], rng)) {
    for (const right of shuffled(
      display.map((_, index) => index),
      rng,
    )) {
      if (tokens[left] === tokens[right]) continue;
      const changed = [...display];
      [changed[left], changed[right]] = [changed[right], changed[left]];
      const candidate = changed.join(joiner);
      if (!gradeAnswer({ answer, type: 'sequence' }, candidate).correct) unique.add(candidate);
      if (unique.size >= count) break swaps;
    }
  }
  return shuffled([...unique], rng).slice(0, Math.max(0, count));
}

export function getChoices(
  question: Question,
  allQuestions: readonly Question[],
  rng: () => number = Math.random,
): string[] {
  const distractors: string[] = [];
  const seen = new Set([normalizeAnswer(question.answer)]);
  if (question.type === 'sequence')
    distractors.push(...sequenceDistractors(question.answer, 3, rng));
  else {
    const groups = [
      allQuestions.filter((item) => item.chapter === question.chapter && item.sub === question.sub),
      allQuestions.filter((item) => item.chapter === question.chapter),
      allQuestions,
    ];
    for (const group of groups) {
      for (const item of shuffled(group, rng)) {
        const normalized = normalizeAnswer(item.answer);
        if (
          item.id === question.id ||
          item.type === 'sequence' ||
          !normalized ||
          seen.has(normalized) ||
          gradeAnswer(question, item.answer).correct ||
          gradeAnswer(item, question.answer).correct
        )
          continue;
        seen.add(normalized);
        distractors.push(item.answer);
        if (distractors.length === 3) break;
      }
      if (distractors.length === 3) break;
    }
  }
  // Small custom decks can have fewer than four distinct answers. Never invent
  // a fake name or duplicate option merely to fill an arbitrary slot count.
  return shuffled([question.answer, ...distractors], rng);
}
