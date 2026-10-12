import { describe, expect, it } from 'vitest';
import { enrichQuestion, renderAnswer } from '../src/lib/answer';
import { clean, gradeAnswer, inferQuestionType } from '../src/lib/grading';
import { getChoices, sequenceDistractors } from '../src/lib/choices';
import type { Question } from '../src/lib/types';

const q = (answer: string, chapter = 'Ⅱ-1 삼국 시대', sub = '백제: 왕', id = answer): Question =>
  enrichQuestion({
    id,
    chapter,
    sub,
    number: 1,
    question: '합성 문제',
    answer,
    type: inferQuestionType(answer),
  });

describe('structured answers are graded independently of display', () => {
  it.each(['의자왕', '백제의자왕', '백제 의자왕', '백제/의자왕'])('accepts %s', (input) => {
    expect(gradeAnswer(q('의자왕'), input).correct).toBe(true);
  });
  it('rejects a different king or country', () => {
    expect(gradeAnswer(q('의자왕'), '무령왕').correct).toBe(false);
    expect(gradeAnswer(q('의자왕'), '신라 의자왕').correct).toBe(false);
    expect(gradeAnswer(q('숙종', 'Ⅲ-1 고려 시대'), '조선 숙종').correct).toBe(false);
  });
  it.each(['고려(성종)', '고려/성종', '성종/고려', '고려 성종', '성종 고려', '성종고려'])(
    'accepts the reviewed country/ruler form %s even inside a Joseon chapter',
    (input) => {
      const item = q('고려(성종)', 'Ⅳ-3·4 조선의 사회·경제·문화', '국가 찾기');
      expect(item.answer).toBe('고려(성종)');
      expect(gradeAnswer(item, input).correct).toBe(true);
      expect(
        gradeAnswer({ answer: item.answer, type: 'short', chapter: item.chapter }, input).correct,
      ).toBe(true);
    },
  );
  it('rejects wrong or duplicated countries and rulers in that pair', () => {
    const item = q('고려(성종)', 'Ⅳ-3·4 조선의 사회·경제·문화', '국가 찾기');
    for (const input of ['조선/성종', '성종 조선', '고려/숙종', '고려고려성종'])
      expect(gradeAnswer(item, input).correct, input).toBe(false);
    expect(renderAnswer(item, { showCountry: true })).toBe('고려(성종)');
    expect(renderAnswer(item, { showCountry: false })).toBe('고려(성종)');
  });
  it('requires the full composite while accepting country and separator variants', () => {
    const item = q('대동법/광해군', 'Ⅳ-2 조선 후기', '');
    for (const input of [
      '대동법/광해군',
      '대동법 광해군',
      '대동법광해군',
      '대동법/조선 광해군',
      '조선 광해군/대동법',
    ])
      expect(gradeAnswer(item, input).correct, input).toBe(true);
    for (const input of ['광해군', '조선 광해군', '대동법'])
      expect(gradeAnswer(item, input).correct, input).toBe(false);
  });
  it('keeps parenthetical aliases and rejects numeric annotations', () => {
    expect(gradeAnswer(q('광혜원(제중원)'), '제중원').correct).toBe(true);
    expect(gradeAnswer(q('사건(1910년)'), '1910년').correct).toBe(false);
    expect(gradeAnswer(q('이자겸(인종)', 'Ⅲ-1 고려 시대'), '이자겸(고려 인종)').correct).toBe(true);
  });
  it('does not promote an explicit override containing only one required component', () => {
    const item = enrichQuestion(q('대동법/광해군', 'Ⅳ-2 조선 후기', '', 'composite'), {
      composite: ['광해군'],
    });
    expect(gradeAnswer(item, '광해군').correct).toBe(false);
  });
  it('accepts reviewed whole-answer aliases', () => {
    const item = enrichQuestion(q('광개토왕', 'Ⅱ-1 삼국 시대', '고구려:', 'king'), {
      king: ['광개토대왕'],
    });
    expect(gradeAnswer(item, '광개토대왕').correct).toBe(true);
  });
  it('does not duplicate required items when a manual alias reorders them', () => {
    const item = enrichQuestion(q('알파/베타', '', '', 'order'), { order: ['베타/알파'] });
    expect(gradeAnswer(item, '베타/알파').correct).toBe(true);
    expect(gradeAnswer(item, '알파/알파').correct).toBe(false);
    expect(gradeAnswer(item, '베타/베타').correct).toBe(false);
  });
  it('ignores the required punctuation and case without changing stored text', () => {
    expect(clean('A · , . / ( ) [ ] 「 」 “ ” \' " ~ - B')).toBe('ab');
  });
});

describe('choices use the same country display setting', () => {
  const items = [
    q('의자왕'),
    q('무령왕'),
    q('근초고왕'),
    q('성왕'),
    q('백제/무령왕', 'Ⅱ-1 삼국 시대', '백제: 왕', 'duplicate'),
  ];
  it.each([true, false])('four unique choices with showCountry=%s', (showCountry) => {
    const choices = getChoices(items[0], items, () => 0.5, { showCountry });
    expect(choices).toHaveLength(4);
    expect(new Set(choices.map(clean)).size).toBe(4);
    expect(choices).toContain(renderAnswer(items[0], { showCountry }));
    expect(choices.filter((answer) => gradeAnswer(items[0], answer).correct)).toHaveLength(1);
    expect(choices.every((answer) => answer.includes('백제') === showCountry)).toBe(true);
  });
  it('does not alter sequence distractors or their grading', () => {
    const item = q('④→②→⑧');
    const choices = getChoices(item, [item], () => 0.5, { showCountry: true });
    expect(choices).toHaveLength(4);
    expect(choices).toContain(item.answer);
    expect(sequenceDistractors(item.answer, 3, () => 0.5)).toHaveLength(3);
    expect(gradeAnswer(item, '428').correct).toBe(true);
    expect(gradeAnswer(item, '482').correct).toBe(false);
  });
});
