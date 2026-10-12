import type { GradeResult, Question, QuestionType } from './types';

/** Only comparison text is normalized. The question and answer remain exact. */
export function normalizeAnswer(value: string): string {
  return value
    .normalize('NFC')
    .toLocaleLowerCase('ko-KR')
    .replace(/[\s\p{P}\p{S}]+/gu, '');
}

/** Split lists without splitting a comma inside a parenthetical alias. */
export function splitAnswers(value: string): string[] {
  const parts: string[] = [];
  let depth = 0,
    current = '';
  for (const ch of value) {
    if (ch === '(' || ch === '（') depth++;
    if (ch === ')' || ch === '）') depth = Math.max(0, depth - 1);
    if (!depth && /[,，;；/\n]/.test(ch)) {
      if (current.trim()) parts.push(current.trim());
      current = '';
    } else current += ch;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

export function answerAliases(answer: string): string[] {
  const full = normalizeAnswer(answer);
  const variants = new Set<string>(full ? [full] : []);
  const main = answer.replace(/[（(][^()（）]*[)）]/g, '').trim();
  if (main && normalizeAnswer(main)) variants.add(normalizeAnswer(main));
  for (const match of answer.matchAll(/[（(]([^()（）]*)[)）]/g)) {
    const text = match[1].trim().replace(/^(?:본명|별칭|호|자)\s*[:：]\s*/, '');
    // Dates, counts and numeric annotations are never standalone answers.
    if (text.length >= 2 && /\p{L}/u.test(text) && !/^[\d\s.,~〜\-–—년월일세기]+$/.test(text)) {
      const alias = normalizeAnswer(text);
      if (alias) variants.add(alias);
    }
  }
  return [...variants];
}

export function sequenceTokens(answer: string): string[] | null {
  const compact = answer.replace(/[（(][^()（）]*[)）]/g, '').trim();
  if (!compact) return null;
  if (/→|⇒|->|>/.test(compact)) {
    const tokens = compact.split(/\s*(?:→|⇒|->|>)\s*/).map((token) => {
      if (/^[①-⑳]$/.test(token)) return String(token.codePointAt(0)! - 0x2460 + 1);
      if (/^\d+$/.test(token)) return token.replace(/^0+(?=\d)/, '');
      return normalizeAnswer(token);
    });
    return tokens.length > 1 && tokens.every(Boolean) ? tokens : null;
  }
  if (/^[①-⑳\s→⇒>,，\-]+$/.test(compact) && /[①-⑳]/.test(compact)) {
    return [...compact]
      .filter((ch) => /[①-⑳]/.test(ch))
      .map((ch) => String(ch.codePointAt(0)! - 0x2460 + 1));
  }
  if (/^\d+(?:\s*(?:→|⇒|>|,|，|\s|-)\s*\d+)+$/.test(compact)) {
    return compact
      .split(/[\s→⇒>,，-]+/)
      .filter(Boolean)
      .map((token) => token.replace(/^0+(?=\d)/, ''));
  }
  return null;
}

export function inferQuestionType(answer: string): QuestionType {
  if (sequenceTokens(answer)) return 'sequence';
  // A slash in a numeric date is not a multiple-answer list.
  if (splitAnswers(answer).length > 1 && /\p{L}/u.test(answer)) return 'multi';
  return 'short';
}

export function gradeAnswer(
  question: Pick<Question, 'answer' | 'type'>,
  input: string,
): GradeResult {
  const normalizedInput = normalizeAnswer(input);
  const result = (correct: boolean, reason: GradeResult['reason']): GradeResult => ({
    correct,
    normalizedInput,
    reason,
  });
  if (!input.trim() || !normalizedInput) return result(false, 'empty');
  if (question.type === 'sequence') {
    const expected = sequenceTokens(question.answer);
    // Named steps may be typed as a comma/slash list, but their order still
    // matters. This input-only fallback must not reclassify multi-answer lists.
    const namedInput = expected?.some((token) => !/^\d+$/.test(token))
      ? splitAnswers(input).map((token) =>
          normalizeAnswer(token.replace(/[（(][^()（）]*[)）]/g, '')),
        )
      : null;
    const actual =
      sequenceTokens(input) ??
      (namedInput && namedInput.length > 1 ? namedInput : null) ??
      (expected?.every((token) => /^[0-9]$/.test(token)) && /^\d+$/.test(input.trim())
        ? [...input.trim()]
        : null);
    return result(
      !!expected &&
        !!actual &&
        expected.length === actual.length &&
        expected.every((value, index) => value === actual[index]),
      'sequence',
    );
  }
  if (question.type === 'multi') {
    const expected = splitAnswers(question.answer).map(answerAliases);
    const actual = splitAnswers(input).map(normalizeAnswer);
    if (expected.length !== actual.length || actual.some((value) => !value))
      return result(false, 'multiple');
    if (expected.length > 128) return result(false, 'multiple');
    // Bipartite matching handles overlapping aliases without factorial work.
    const owners = new Map<number, number>();
    const match = (index: number, visited: Set<number>): boolean => {
      for (let target = 0; target < expected.length; target++) {
        if (!visited.has(target) && expected[target].includes(actual[index])) {
          visited.add(target);
          if (!owners.has(target) || match(owners.get(target)!, visited)) {
            owners.set(target, index);
            return true;
          }
        }
      }
      return false;
    };
    return result(
      actual.every((_, index) => match(index, new Set())),
      'multiple',
    );
  }
  if (normalizedInput === normalizeAnswer(question.answer)) return result(true, 'exact');
  return result(
    answerAliases(question.answer).includes(normalizedInput),
    answerAliases(question.answer).includes(normalizedInput) ? 'alias' : 'mismatch',
  );
}
