import { type FrameLocator } from '@playwright/test';
import { test, expect } from './fixtures';
import { readFileSync } from 'node:fs';
import type { Question } from '../src/lib/types';
const questions = JSON.parse(
  readFileSync(new URL('../data/questions.json', import.meta.url), 'utf8'),
) as Question[];
const scope = 'a'.repeat(64);
async function home(page: import('@playwright/test').Page) {
  await page.goto('/__study-test/');
  const app = page.frameLocator('iframe');
  await expect(app.getByRole('heading', { name: '암기 학습', exact: true })).toBeVisible();
  return app;
}
async function firstChapter(app: FrameLocator) {
  await app.getByRole('button', { name: '선택 해제', exact: true }).click();
  await app.locator('.study-chapter input').first().check();
  await app.getByRole('checkbox', { name: '문제 순서 섞기' }).uncheck();
}
test('seven-question Learn round, written button and Enter, wrong feedback, reload recovery', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const app = await home(page);
  await firstChapter(app);
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  let answered = 0;
  let checkedComposingEnter = false;
  while (answered < 14) {
    await expect(app.getByTestId('question')).toBeVisible();
    const text = await app.getByTestId('question').innerText();
    const q = questions.find(
      (item) => item.chapter === questions[0].chapter && item.question === text,
    );
    expect(q).toBeTruthy();
    if (await app.getByLabel('정답 입력', { exact: true }).isVisible()) {
      const input = app.getByLabel('정답 입력', { exact: true });
      await input.fill(q!.answer);
      if (!checkedComposingEnter) {
        await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
        await expect(input).toBeEnabled();
        await expect(app.locator('.study-feedback')).toHaveCount(0);
        checkedComposingEnter = true;
      }
      if (answered % 2 === 0) await input.press('Enter');
      else await app.getByRole('button', { name: '정답 확인', exact: true }).click();
    } else await app.getByRole('button', { name: q!.answer, exact: true }).click();
    await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
    await app.getByRole('button', { name: '바로 계속', exact: true }).click();
    await expect(app.locator('.study-feedback')).toHaveCount(0);
    answered++;
    expect(answered).toBeLessThan(20);
  }
  expect(answered).toBe(14);
  await expect(app.getByRole('heading', { name: '1라운드 완료' })).toBeVisible();
  await page.reload();
  await expect(app.getByRole('button', { name: '이어서 학습', exact: true })).toBeVisible();
  const stored = await page.evaluate(
    (s) => JSON.parse(localStorage.getItem('hnk_v1')!).accounts[s],
    scope,
  );
  expect(Object.values(stored.levels).filter((value) => value === 2)).toHaveLength(7);
  await app.getByRole('button', { name: '이어서 학습', exact: true }).click();
  await app.locator('.study-choice').filter({ hasNotText: questions[7].answer }).first().click();
  await expect(app.getByText('다시 익혀 볼까요?', { exact: true })).toBeVisible();
  await app.getByRole('button', { name: '계속하기', exact: true }).click();
  expect(errors).toEqual([]);
});
test('one full flashcard pass and unknown-only pass', async ({ page }) => {
  const app = await home(page);
  await firstChapter(app);
  await app.getByRole('button', { name: '플래시카드', exact: true }).click();
  const count = questions.filter((q) => q.chapter === questions[0].chapter).length;
  await app.getByRole('button', { name: '문제 카드, 눌러서 정답 보기', exact: true }).click();
  await expect(
    app.getByRole('button', { name: '정답 카드, 눌러서 문제 보기', exact: true }),
  ).toBeVisible();
  await app.getByRole('button', { name: '← 몰라요', exact: true }).click();
  for (let i = 1; i < count; i++)
    await app.getByRole('button', { name: '알아요 →', exact: true }).click();
  await expect(app.getByRole('heading', { name: '한 바퀴 마쳤어요' })).toBeVisible();
  await app.getByRole('button', { name: '몰라요만 다시 (1)', exact: true }).click();
  await app.getByRole('button', { name: '알아요 →', exact: true }).click();
  await expect(app.getByRole('heading', { name: '한 바퀴 마쳤어요' })).toBeVisible();
});
test('mobile focus, dark theme, keyboard, two-stage clear and account reset', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const app = await home(page);
  await firstChapter(app);
  await app.getByLabel('화면 색상').selectOption('dark');
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  const first = questions[0];
  await app.getByRole('button', { name: first.answer, exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
  await page.keyboard.press('Enter');
  await app.getByRole('button', { name: '← 학습 홈', exact: true }).click();
  await app.getByRole('button', { name: '기록 지우기', exact: true }).click();
  await expect(app.getByRole('dialog')).toBeVisible();
  await app.getByRole('button', { name: '다음 단계', exact: true }).click();
  await app.getByRole('button', { name: '취소', exact: true }).click();
  expect(
    await page.evaluate(
      (s) => Object.keys(JSON.parse(localStorage.getItem('hnk_v1')!).accounts[s].levels).length,
      scope,
    ),
  ).toBe(1);
  await app.getByRole('button', { name: '기록 지우기', exact: true }).click();
  await app.getByRole('button', { name: '다음 단계', exact: true }).click();
  await app.getByRole('button', { name: '학습 기록 지우기', exact: true }).click();
  expect(
    await page.evaluate(
      (s) => Object.keys(JSON.parse(localStorage.getItem('hnk_v1')!).accounts[s].levels).length,
      scope,
    ),
  ).toBe(0);
  const overflow = await app.locator('body').evaluate((el) => el.scrollWidth > innerWidth);
  expect(overflow).toBe(false);
  await page.evaluate(() =>
    document
      .querySelector('iframe')!
      .contentWindow!.postMessage(
        { channel: 'yeobaek-study-v1', type: 'context', scope: 'b'.repeat(64), active: true },
        location.origin,
      ),
  );
  await expect(app.getByRole('button', { name: '학습하기', exact: true })).toBeVisible();
});
test('damaged local storage does not prevent learning or overwrite its raw value', async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem('hnk_v1', '{broken json');
  });
  const app = await home(page);
  await expect(app.locator('.study-error[role="alert"]')).toBeVisible();
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  await expect(app.getByTestId('question')).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('hnk_v1'))).toBe('{broken json');
});
