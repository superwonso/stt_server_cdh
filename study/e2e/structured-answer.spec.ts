import type { Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { test, expect } from './fixtures';

const scope = 'a'.repeat(64);
const chapter = 'Ⅱ-1 삼국 시대';
const kings = ['의자왕', '무왕', '근초고왕', '무령왕'];
// Deliberately use the original schema. Loading a personal deck created before
// structured answers must preserve its IDs and learning levels.
const legacyQuestions = kings.map((answer, index) => ({
  id: `legacy-king-${index}`,
  chapter,
  sub: '백제: 설명에 해당하는 왕(또는 인물)을 쓰세요',
  number: index + 1,
  question: `합성 검증 문제 ${index + 1}`,
  answer,
  type: 'short',
}));

async function legacyHome(
  page: Page,
  firstLevel: 0 | 1,
  questions = legacyQuestions,
  remainingCompleted = true,
) {
  const levels = Object.fromEntries(
    questions.map((question, index) => [
      question.id,
      index === 0 || !remainingCompleted ? firstLevel : 2,
    ]),
  );
  await page.addInitScript(
    ({ scope, questions, levels }) => {
      // Never overwrite changes when the document is reloaded or its frame opens.
      if (localStorage.getItem('hnk_v1') !== null) return;
      localStorage.setItem(
        'hnk_v1',
        JSON.stringify({
          version: 1,
          accounts: {
            [scope]: {
              initialized: true,
              selectedChapters: [...new Set(questions.map((question) => question.chapter))],
              shuffle: false,
              levels,
              activeDeckId: 'legacy-kings',
              decks: [
                {
                  id: 'legacy-kings',
                  title: '구버전 왕 이름 연습',
                  questions,
                  createdAt: '2026-10-12T00:00:00Z',
                },
              ],
            },
          },
        }),
      );
    },
    { scope, questions, levels },
  );
  await page.goto('/__study-test/');
  const app = page.frameLocator('iframe');
  await expect(app.getByRole('heading', { name: '암기 학습', exact: true })).toBeVisible();
  await expect(app.locator('#deck-select')).toHaveValue('legacy-kings');
  return { app, levels };
}

async function accountProgress(page: Page) {
  return page.evaluate(
    (scope) => JSON.parse(localStorage.getItem('hnk_v1')!).accounts[scope],
    scope,
  );
}

test('country toggle persists without changing legacy IDs or levels; bare king name is correct', async ({
  page,
}) => {
  const { app, levels } = await legacyHome(page, 1);
  const toggle = app.getByRole('checkbox', { name: '왕 이름 앞에 나라 표시', exact: true });
  await expect(toggle).toBeChecked();
  await app.getByRole('button', { name: '플래시카드', exact: true }).click();
  await app.getByRole('button', { name: '문제 카드, 눌러서 정답 보기', exact: true }).click();
  await expect(app.locator('.study-flash .study-question')).toHaveText('백제 의자왕');
  await app.getByRole('button', { name: '← 학습 홈', exact: true }).click();
  await toggle.uncheck();
  await expect.poll(async () => (await accountProgress(page)).showCountry).toBe(false);
  await page.reload();
  await expect(toggle).not.toBeChecked();
  const progress = await accountProgress(page);
  expect(progress.levels).toEqual(levels);
  expect(progress.decks[0].questions.map((q: { id: string }) => q.id)).toEqual(
    legacyQuestions.map((q) => q.id),
  );
  expect(progress.decks[0].questions[0]).toMatchObject({
    answer: '의자왕',
    dataVersion: 2,
    answerParts: [{ kind: 'ruler', name: '의자왕', country: '백제' }],
  });
  await app.getByRole('button', { name: '플래시카드', exact: true }).click();
  await app.getByRole('button', { name: '문제 카드, 눌러서 정답 보기', exact: true }).click();
  await expect(app.locator('.study-flash .study-question')).toHaveText('의자왕');
  await app.getByRole('button', { name: '← 학습 홈', exact: true }).click();
  await toggle.check();
  await app.getByRole('button', { name: '이어서 학습', exact: true }).click();
  await app.getByLabel('정답 입력', { exact: true }).fill('의자왕');
  await app.getByRole('button', { name: '정답 확인', exact: true }).click();
  await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
  await expect.poll(async () => (await accountProgress(page)).levels['legacy-king-0']).toBe(2);
  expect((await accountProgress(page)).levels).toEqual({ ...levels, 'legacy-king-0': 2 });
});

for (const showCountry of [true, false]) {
  test(`choices, wrong feedback and result list share country display (${showCountry})`, async ({
    page,
  }) => {
    const { app } = await legacyHome(page, 0);
    if (!showCountry)
      await app.getByRole('checkbox', { name: '왕 이름 앞에 나라 표시', exact: true }).uncheck();
    const displayed = (king: string) => (showCountry ? `백제 ${king}` : king);
    await app.getByRole('button', { name: '이어서 학습', exact: true }).click();
    const choices = app.locator('.study-choice > span:last-child');
    await expect(choices).toHaveCount(4);
    expect((await choices.allTextContents()).sort()).toEqual(kings.map(displayed).sort());
    await app.getByRole('button', { name: displayed('무왕'), exact: true }).click();
    await expect(app.locator('.study-feedback')).toContainText(`정답: ${displayed('의자왕')}`);
    await app.getByRole('button', { name: '계속하기', exact: true }).click();
    await app.getByRole('button', { name: displayed('의자왕'), exact: true }).click();
    await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
    await app.getByRole('button', { name: '바로 계속', exact: true }).click();
    await app.getByLabel('정답 입력', { exact: true }).fill('의자왕');
    await app.getByRole('button', { name: '정답 확인', exact: true }).click();
    await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
    await app.getByRole('button', { name: '바로 계속', exact: true }).click();
    await app.getByText('학습 중 틀렸던 문제 1개', { exact: true }).click();
    await expect(app.locator('.study-wrong-list')).toContainText(`정답: ${displayed('의자왕')}`);
  });
}

test('JSON and Excel exports preserve pure answers and structured metadata independently of display', async ({
  page,
}) => {
  const { app } = await legacyHome(page, 1);
  await app.getByText('문제집 내보내기', { exact: true }).click();
  const firstDownload = page.waitForEvent('download');
  await app.getByRole('button', { name: 'JSON', exact: true }).click();
  const firstFile = await (await firstDownload).path();
  const countryOn = JSON.parse(readFileSync(firstFile!, 'utf8'));
  await app.getByRole('checkbox', { name: '왕 이름 앞에 나라 표시', exact: true }).uncheck();
  const secondDownload = page.waitForEvent('download');
  await app.getByRole('button', { name: 'JSON', exact: true }).click();
  const secondFile = await (await secondDownload).path();
  expect(JSON.parse(readFileSync(secondFile!, 'utf8'))).toEqual(countryOn);
  expect(countryOn.version).toBe(2);
  expect(countryOn.questions[0]).toMatchObject({
    answer: '의자왕',
    dataVersion: 2,
    answerParts: [{ kind: 'ruler', name: '의자왕', country: '백제' }],
  });
  expect(countryOn.questions[0].aliases).toEqual(expect.any(Array));
  const excelDownload = page.waitForEvent('download');
  await app.getByRole('button', { name: '엑셀', exact: true }).click();
  const excelFile = await (await excelDownload).path();
  const xlsx = await import('xlsx');
  const workbook = xlsx.read(readFileSync(excelFile!), { type: 'buffer' });
  const rows = xlsx.utils.sheet_to_json<Record<string, unknown>>(workbook.Sheets['문제-답']);
  expect(rows[0]['답']).toBe('의자왕');
  expect(JSON.parse(String(rows[0].answerParts))).toEqual(countryOn.questions[0].answerParts);
  expect(JSON.parse(String(rows[0].aliases))).toEqual(countryOn.questions[0].aliases);
  expect(rows[0].dataVersion).toBe(2);
});

test('explicit country-parenthesis source survives both display settings and accepts all five approved forms', async ({
  page,
}) => {
  const forms = ['고려(성종)', '고려/성종', '성종/고려', '고려 성종', '성종 고려'];
  const questions = forms.map((_, index) => ({
    id: `legacy-country-pair-${index}`,
    chapter: 'Ⅳ-1 조선 전기',
    sub: '합성 검증: 원문에 명시한 나라와 왕',
    number: index + 1,
    question: `나라와 왕의 합성 검증 문제 ${index + 1}`,
    answer: '고려(성종)',
    type: 'short',
  }));
  const { app, levels } = await legacyHome(page, 1, questions, false);
  const toggle = app.getByRole('checkbox', { name: '왕 이름 앞에 나라 표시', exact: true });
  for (const showCountry of [true, false]) {
    await toggle.setChecked(showCountry);
    await app.getByRole('button', { name: '플래시카드', exact: true }).click();
    await app.getByRole('button', { name: '문제 카드, 눌러서 정답 보기', exact: true }).click();
    await expect(app.locator('.study-flash .study-question')).toHaveText('고려(성종)');
    await app.getByRole('button', { name: '← 학습 홈', exact: true }).click();
  }
  const saved = await accountProgress(page);
  expect(saved.levels).toEqual(levels);
  expect(saved.decks[0].questions[0].answer).toBe('고려(성종)');
  expect(saved.decks[0].questions[0].answerParts).toContainEqual({
    kind: 'ruler',
    name: '성종',
    country: '고려',
  });
  await toggle.check();
  await app.getByRole('button', { name: '이어서 학습', exact: true }).click();
  for (const form of forms) {
    await app.getByLabel('정답 입력', { exact: true }).fill(form);
    await app.getByRole('button', { name: '정답 확인', exact: true }).click();
    await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
    await app.getByRole('button', { name: '바로 계속', exact: true }).click();
  }
  await expect
    .poll(async () => Object.values((await accountProgress(page)).levels))
    .toEqual([2, 2, 2, 2, 2]);
});
