import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import type { ChapterManifest, Question } from '../src/lib/types';

const seedBody = readFileSync(new URL('../data/questions.json', import.meta.url), 'utf8');
const questions = JSON.parse(seedBody) as Question[];
const chapters = JSON.parse(
  readFileSync(new URL('../data/chapters.json', import.meta.url), 'utf8'),
) as ChapterManifest;
const scope = 'a'.repeat(64);

async function home(page: Page) {
  await page.goto('/__study-test/');
  const app = page.frameLocator('iframe');
  await expect(app.getByRole('heading', { name: '암기 학습', exact: true })).toBeVisible();
  return app;
}

test('fresh home uses manifest counts; a cancelled lazy start cannot reopen learning', async ({
  page,
}) => {
  let requests = 0;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/questions.json', async (route) => {
    requests++;
    await gate;
    await route.fulfill({ contentType: 'application/json', body: seedBody });
  });
  const app = await home(page);
  await expect(app.locator('.study-stat').first()).toContainText('0 / 665');
  await expect(app.getByText('665문제 선택 · 665문제 더 익히기', { exact: true })).toBeVisible();
  expect(requests).toBe(0);
  await app.getByRole('button', { name: '선택 해제', exact: true }).click();
  await expect(app.getByRole('button', { name: '학습하기', exact: true })).toBeDisabled();
  await app.locator('.study-chapter input').first().check();
  await expect(
    app.getByText(`${chapters[0][1]}문제 선택 · ${chapters[0][1]}문제 더 익히기`, { exact: true }),
  ).toBeVisible();
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  await expect(app.getByTestId('seed-loading')).toBeVisible();
  await expect(app.getByRole('button', { name: '학습하기', exact: true })).toBeDisabled();
  await expect(app.getByRole('button', { name: '플래시카드', exact: true })).toBeDisabled();
  await app.getByRole('button', { name: '＋ 내 파일 가져오기', exact: true }).click();
  release();
  await expect(app.getByTestId('seed-loading')).toHaveCount(0);
  await expect(app.getByRole('heading', { name: '내 파일 가져오기', exact: true })).toBeVisible();
  await expect(app.getByTestId('question')).toHaveCount(0);
  await app.getByRole('button', { name: '학습 홈', exact: true }).click();
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  await expect(app.getByTestId('question')).toBeVisible();
  expect(requests).toBe(1);
});

test('failed lazy fetch leaves home usable and an explicit retry starts learning', async ({
  page,
}) => {
  let requests = 0;
  await page.route('**/questions.json', async (route) => {
    requests++;
    await route.fulfill(
      requests === 1
        ? { status: 503, body: '' }
        : { contentType: 'application/json', body: seedBody },
    );
  });
  const app = await home(page);
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  await expect(app.locator('.study-error[role="alert"]')).toContainText(
    '기본 문제집을 불러오지 못했어요.',
  );
  await expect(app.getByRole('button', { name: '학습하기', exact: true })).toBeEnabled();
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  await expect(app.getByTestId('question')).toBeVisible();
  expect(requests).toBe(2);
});

test('saved seed levels trigger one fetch and restore accurate chapter progress', async ({
  page,
}) => {
  await page.addInitScript(
    ({ scope, first, second, chapter }) => {
      localStorage.setItem(
        'hnk_v1',
        JSON.stringify({
          version: 1,
          accounts: {
            [scope]: {
              initialized: true,
              selectedChapters: [chapter],
              shuffle: false,
              levels: { [first]: 2, [second]: 1 },
            },
          },
        }),
      );
    },
    { scope, first: questions[0].id, second: questions[1].id, chapter: chapters[0][0] },
  );
  let requests = 0;
  await page.route('**/questions.json', async (route) => {
    requests++;
    await route.fulfill({ contentType: 'application/json', body: seedBody });
  });
  const app = await home(page);
  await expect(app.locator('.study-stat').first()).toContainText('1 / 665');
  await expect(app.locator('.study-chapter-count').first()).toHaveText(`1 / ${chapters[0][1]}`);
  await expect(
    app.getByText(`${chapters[0][1]}문제 선택 · ${chapters[0][1] - 1}문제 더 익히기`, {
      exact: true,
    }),
  ).toBeVisible();
  await expect(app.getByRole('button', { name: '이어서 학습', exact: true })).toBeEnabled();
  expect(requests).toBe(1);
});

test('a saved personal deck works without loading the seed questions', async ({ page }) => {
  const own = {
    ...questions[0],
    id: 'personal-one',
    chapter: '합성 개인 단원',
    question: '합성 개인 문제',
    answer: '합성 답',
  };
  await page.addInitScript(
    ({ scope, own }) => {
      localStorage.setItem(
        'hnk_v1',
        JSON.stringify({
          version: 1,
          accounts: {
            [scope]: {
              initialized: true,
              selectedChapters: [own.chapter],
              shuffle: false,
              levels: {},
              activeDeckId: 'personal-deck',
              decks: [
                {
                  id: 'personal-deck',
                  title: '합성 개인 문제집',
                  questions: [own],
                  createdAt: '2026-10-12T00:00:00.000Z',
                },
              ],
            },
          },
        }),
      );
    },
    { scope, own },
  );
  let requests = 0;
  await page.route('**/questions.json', async (route) => {
    requests++;
    await route.abort();
  });
  const app = await home(page);
  await expect(app.locator('.study-stat').first()).toContainText('0 / 1');
  await app.getByRole('button', { name: '플래시카드', exact: true }).click();
  await expect(
    app.getByRole('button', { name: '문제 카드, 눌러서 정답 보기', exact: true }),
  ).toContainText(own.question);
  expect(requests).toBe(0);
});

test('JSON export lazily loads the complete seed deck', async ({ page }) => {
  let requests = 0;
  await page.route('**/questions.json', async (route) => {
    requests++;
    await route.fulfill({ contentType: 'application/json', body: seedBody });
  });
  const app = await home(page);
  expect(requests).toBe(0);
  await app.getByText('문제집 내보내기', { exact: true }).click();
  const downloaded = page.waitForEvent('download');
  await app.getByRole('button', { name: 'JSON', exact: true }).click();
  const download = await downloaded;
  const file = await download.path();
  expect(file).toBeTruthy();
  expect(JSON.parse(readFileSync(file!, 'utf8')).questions).toHaveLength(665);
  expect(requests).toBe(1);
});

test('logging out during a lazy load cannot show an old account learning session', async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/questions.json', async (route) => {
    await gate;
    await route.fulfill({ contentType: 'application/json', body: seedBody }).catch(() => {});
  });
  const app = await home(page);
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  await expect(app.getByTestId('seed-loading')).toBeVisible();
  await page.evaluate(() =>
    document
      .querySelector('iframe')!
      .contentWindow!.postMessage(
        { channel: 'yeobaek-study-v1', type: 'context', scope: null, active: false },
        location.origin,
      ),
  );
  await expect(
    app.getByText('여백에 로그인한 뒤 상단의 암기 학습 탭을 열어 주세요.', { exact: true }),
  ).toBeVisible();
  release();
  await expect(app.getByTestId('question')).toHaveCount(0);
});
