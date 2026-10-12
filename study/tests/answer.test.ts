import { describe, expect, it } from 'vitest';
import {
  DATA_VERSION,
  enrichQuestion,
  getStructuredAliases,
  inspectRulerCandidates,
  renderAnswer,
  upgradeQuestion,
} from '../src/lib/answer';
import type { LegacyQuestionLike } from '../src/lib/answer';
import { gradeAnswer } from '../src/lib/grading';

const item = (answer: string, extra: Partial<LegacyQuestionLike> = {}) =>
  enrichQuestion({
    id: 'stable-id',
    chapter: '단원',
    sub: '',
    answer,
    type: 'short' as const,
    ...extra,
  });
const on = (answer: ReturnType<typeof item>) => renderAnswer(answer, { showCountry: true });
const off = (answer: ReturnType<typeof item>) => renderAnswer(answer, { showCountry: false });

describe('structured answers preserve source meaning and ruler boundaries', () => {
  it('adds a country only when requested and preserves question identity', () => {
    const value = item('의자왕');
    expect(value.id).toBe('stable-id');
    expect(value.dataVersion).toBe(DATA_VERSION);
    expect(value.answer).toBe('의자왕');
    expect(on(value)).toBe('백제 의자왕');
    expect(off(value)).toBe('의자왕');
    expect(value.answerParts).toEqual([{ kind: 'ruler', name: '의자왕', country: '백제' }]);
  });

  it('preserves all other words and delimiters in composite and parenthetical answers', () => {
    const composite = item('대동법/광해군', { type: 'multi' });
    expect(on(composite)).toBe('대동법/조선 광해군');
    expect(off(composite)).toBe('대동법/광해군');
    const parenthetical = item('이자겸(인종)', { chapter: 'Ⅲ-1 고려 시대' });
    expect(on(parenthetical)).toBe('이자겸(고려 인종)');
    expect(off(parenthetical)).toBe('이자겸(인종)');
    expect(on(item('동의보감 (광해군)'))).toBe('동의보감 (조선 광해군)');
    expect(on(item('선조 (김장생)'))).toBe('조선 선조 (김장생)');
  });

  it.each([
    ['신라/진흥왕', '진흥왕', '신라 진흥왕'],
    ['백제/무왕', '무왕', '백제 무왕'],
    ['고려 성종', '성종', '고려 성종'],
  ])('moves one explicit country into metadata for %s', (answer, pure, display) => {
    const value = item(answer, { type: 'multi', chapter: 'Ⅳ-1 조선 전기' });
    expect(value.answer).toBe(pure);
    expect(on(value)).toBe(display);
    expect(off(value)).toBe(pure);
    expect(value.type).toBe('short');
    expect(upgradeQuestion(value)).toEqual(value);
  });

  it.each(['성종', '숙종', '현종', '예종'])('resolves %s only with reliable context', (answer) => {
    expect(on(item(answer, { chapter: 'Ⅲ-1 고려 시대' }))).toBe(`고려 ${answer}`);
    expect(on(item(answer, { chapter: 'Ⅳ-1 조선 전기' }))).toBe(`조선 ${answer}`);
    const unknown = item(answer);
    expect(on(unknown)).toBe(answer);
    expect(unknown.answerParts).toEqual([
      { kind: 'ruler', name: answer, country: null, countryAmbiguous: true },
    ]);
    expect(inspectRulerCandidates(unknown).ambiguous).toEqual([answer]);
  });

  it('takes the longest name first without breaking Taejo Wang Geon', () => {
    const value = item('태조 왕건', { chapter: 'Ⅲ-1 고려 시대' });
    expect(value.answerParts).toHaveLength(1);
    expect(on(value)).toBe('고려 태조 왕건');
    expect(getStructuredAliases(value)).toContain('왕건');
    expect(on(item('태조', { chapter: 'Ⅳ-1 조선 전기' }))).toBe('조선 태조');
  });

  it.each([
    '문성왕',
    '선종',
    '백제 (무령왕릉)',
    '동명왕편',
    '제왕운기',
    '김대중',
    '노무현',
    '조선(후기)',
    '고구려',
    '연개소문',
  ])('never decorates non-matching answer %s', (answer) => {
    const value = item(answer, { chapter: 'Ⅳ-1 조선 전기' });
    expect(on(value)).toBe(answer);
    expect(off(value)).toBe(answer);
    expect(value.answerParts).toEqual([{ kind: 'text', value: answer }]);
  });

  it('does not rewrite numeric, date, or sequence content', () => {
    for (const answer of ['①→⑤→②', '3/1/1919', '1910', '신라→고려→조선']) {
      const value = item(answer, { type: answer.includes('→') ? 'sequence' : 'short' });
      expect(value.answer).toBe(answer);
      expect(on(value)).toBe(answer);
      expect(value.answerParts).toEqual([{ kind: 'text', value: answer }]);
    }
  });

  it('rejects dishonest imported parts, while retaining a valid ambiguous-country hint', () => {
    const fake = item('동명왕편', {
      answerParts: [{ kind: 'ruler', name: '동명왕편', country: '고구려' }],
    });
    expect(on(fake)).toBe('동명왕편');
    const wrongText = item('성종', {
      answerParts: [{ kind: 'ruler', name: '의자왕', country: '백제' }],
    });
    expect(on(wrongText)).toBe('성종');
    const legitimate = item('성종', {
      answerParts: [{ kind: 'ruler', name: '성종', country: '고려' }],
    });
    expect(on(legitimate)).toBe('고려 성종');
  });
});

describe('whole-answer alias generation', () => {
  it('uses a country directly before a ruler parenthesis without repeating it', () => {
    const value = item('고려(성종)', {
      chapter: 'Ⅳ-3·4 조선의 사회·경제·문화',
      answerParts: [
        { kind: 'text', value: '고려(' },
        { kind: 'ruler', name: '성종', country: '조선' },
        { kind: 'text', value: ')' },
      ],
    });
    expect(value.answerParts).toEqual([
      { kind: 'text', value: '고려(' },
      { kind: 'ruler', name: '성종', country: '고려' },
      { kind: 'text', value: ')' },
    ]);
    expect(value.answer).toBe('고려(성종)');
    expect(on(value)).toBe('고려(성종)');
    expect(off(value)).toBe('고려(성종)');
    expect(upgradeQuestion(value)).toEqual(value);
    for (const input of [
      '고려(성종)',
      '고려/성종',
      '성종/고려',
      '고려 성종',
      '성종 고려',
      '고려성종',
      '성종고려',
    ]) {
      expect(gradeAnswer(value, input).correct, input).toBe(true);
    }
    for (const input of ['조선 성종', '성종 조선', '고려 숙종', '숙종 고려']) {
      expect(gradeAnswer(value, input).correct, input).toBe(false);
    }
  });

  it('keeps a surrounding required answer in country-parenthetical variants', () => {
    const value = item('제위보/고려(광종)', { type: 'multi', chapter: 'Ⅳ-1 조선' });
    expect(on(value)).toBe('제위보/고려(광종)');
    for (const input of ['제위보/고려(광종)', '제위보/광종 고려', '제위보/고려 광종']) {
      expect(gradeAnswer(value, input).correct, input).toBe(true);
    }
    for (const input of ['제위보', '광종', '고려 광종', '광종 고려']) {
      expect(gradeAnswer(value, input).correct, input).toBe(false);
    }
  });

  it('rejects imported reversed country-parenthetical aliases that omit another required answer', () => {
    const value = item('고려(성종)/대동법', {
      type: 'multi',
      chapter: 'Ⅳ-1 조선',
      aliases: ['성종 고려', '성종/고려', '고려 성종', '대동법', '성종 고려/대동법'],
    });
    for (const input of ['성종 고려', '성종/고려', '고려 성종', '대동법']) {
      expect(value.aliases).not.toContain(input);
      expect(gradeAnswer(value, input).correct, input).toBe(false);
    }
    expect(gradeAnswer(value, '성종 고려/대동법').correct).toBe(true);
  });

  it('accepts plain and country ruler forms without enabling a different ruler', () => {
    const aliases = getStructuredAliases(item('의자왕'));
    expect(aliases).toContain('의자왕');
    expect(aliases).toContain('백제 의자왕');
    expect(aliases).not.toContain('무왕');
  });

  it('never emits or imports a lone component of a required composite answer', () => {
    const value = item('대동법/광해군', {
      type: 'multi',
      aliases: ['광해군', '조선 광해군', '대동법'],
    });
    expect(value.aliases).toContain('대동법/조선 광해군');
    expect(value.aliases).toContain('대동법 광해군');
    expect(value.aliases).toContain('대동법광해군');
    expect(value.aliases).not.toContain('광해군');
    expect(value.aliases).not.toContain('조선 광해군');
    expect(value.aliases).not.toContain('대동법');
  });

  it('retains parenthetical names but excludes standalone numeric annotations', () => {
    expect(item('광혜원(제중원)').aliases).toContain('제중원');
    expect(item('경순왕 (김부)').aliases).toContain('김부');
    expect(item('사건(1910년)').aliases).not.toContain('1910년');
    expect(item('사건(1910~1919)').aliases).not.toContain('1910~1919');
  });

  it('applies bounded reviewed overrides by exact ID and filters partial components', () => {
    const value = enrichQuestion(
      { id: 'reviewed', answer: '대동법/광해군', type: 'multi' as const },
      { reviewed: ['광해군', '대동법 광해군'] },
    );
    expect(value.aliases).toContain('대동법 광해군');
    expect(value.aliases).not.toContain('광해군');
    expect(item('답', { aliases: ['x'.repeat(6001), {}, '', '\u0000secret'] }).aliases).toEqual([
      '답',
    ]);
  });
});
