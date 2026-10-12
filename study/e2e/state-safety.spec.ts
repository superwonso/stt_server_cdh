import { test, expect, type Page } from '@playwright/test';

const scope = 'a'.repeat(64);
const one = {
  id: 'synthetic-one',
  chapter: '합성 단원',
  sub: '',
  number: 1,
  question: '합성 문제',
  answer: '합성 정답',
  type: 'short',
};

async function home(page: Page) {
  await page.goto('/__study-test/');
  const app = page.frameLocator('iframe');
  await expect(app.getByRole('heading', { name: '암기 학습', exact: true })).toBeVisible();
  return app;
}

test('a stale same-account tab cannot delete another tab newly imported deck', async ({
  page,
  context,
}) => {
  const first = await home(page);
  const secondPage = await context.newPage();
  const second = await home(secondPage);
  await first.getByRole('button', { name: '＋ 내 파일 가져오기', exact: true }).click();
  await first.locator('input[type=file]').setInputFiles({
    name: '합성문제집.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify([one])),
  });
  await first.getByRole('checkbox', { name: '문항과 정답을 확인했어요.', exact: true }).check();
  await first.getByRole('button', { name: '확인한 1문제 저장', exact: true }).click();
  await expect(first.getByText('1문제를 이 기기에 가져왔어요.', { exact: true })).toBeVisible();
  const original = await page.evaluate(() => localStorage.getItem('hnk_v1'));
  await second.getByLabel('화면 색상').selectOption('dark');
  await expect(second.locator('.study-error[role="alert"]')).toContainText(
    '다른 탭에서 학습 기록이 바뀌어 저장하지 않았어요.',
  );
  expect(await page.evaluate(() => localStorage.getItem('hnk_v1'))).toBe(original);
  await secondPage.reload();
  await expect(second.locator('#deck-select')).toContainText('합성문제집');
  await secondPage.close();
});

test('home navigation preserves AI text, complete drafts and request UUID; account change discards them', async ({
  page,
}) => {
  const app = await home(page);
  await page.evaluate(() => {
    const requests: { requestId: string }[] = [];
    Object.assign(window, { studySyntheticRequests: requests });
    window.addEventListener('message', (event) => {
      if (
        event.source !== document.querySelector('iframe')?.contentWindow ||
        event.origin !== location.origin ||
        event.data?.channel !== 'yeobaek-study-v1' ||
        event.data.type !== 'generate'
      )
        return;
      requests.push({ requestId: event.data.requestId });
    });
  });
  await app.getByRole('button', { name: '＋ 내 파일 가져오기', exact: true }).click();
  await app.getByText('글에서 문항 만들기', { exact: true }).click();
  const text = app.getByPlaceholder(
    '교재나 내 노트에서 허가된 글을 붙여 넣으세요. 필요한 범위만 남겨 주세요.',
  );
  await text.fill('합성 자료의 정답은 합성 정답이다.');
  await app.getByRole('checkbox', { name: /유료 AI 문항 생성/ }).check();
  await app.getByRole('button', { name: 'AI 문항 초안 만들기', exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { studySyntheticRequests: unknown[] }).studySyntheticRequests
            .length,
      ),
    )
    .toBe(1);
  await app.getByRole('button', { name: '학습 홈', exact: true }).click();
  await page.evaluate((one) => {
    const requests = (window as unknown as { studySyntheticRequests: { requestId: string }[] })
      .studySyntheticRequests;
    document.querySelector('iframe')!.contentWindow!.postMessage(
      {
        channel: 'yeobaek-study-v1',
        type: 'generated',
        requestId: requests[0].requestId,
        questions: [one],
        warnings: ['합성 검증용 초안'],
      },
      location.origin,
    );
  }, one);
  await app.getByRole('button', { name: '＋ 내 파일 가져오기', exact: true }).click();
  await expect(text).toHaveValue('합성 자료의 정답은 합성 정답이다.');
  await expect(
    app.getByRole('heading', { name: '저장 전 확인 · 1문제', exact: true }),
  ).toBeVisible();
  await app.getByRole('button', { name: 'AI 문항 초안 만들기', exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { studySyntheticRequests: unknown[] }).studySyntheticRequests
            .length,
      ),
    )
    .toBe(2);
  expect(
    await page.evaluate(() => {
      const requests = (window as unknown as { studySyntheticRequests: { requestId: string }[] })
        .studySyntheticRequests;
      return requests[0].requestId === requests[1].requestId;
    }),
  ).toBe(true);
  await page.evaluate(() =>
    document
      .querySelector('iframe')!
      .contentWindow!.postMessage(
        { channel: 'yeobaek-study-v1', type: 'context', scope: 'b'.repeat(64), active: true },
        location.origin,
      ),
  );
  await expect(app.getByRole('heading', { name: '암기 학습', exact: true })).toBeVisible();
  await app.getByRole('button', { name: '＋ 내 파일 가져오기', exact: true }).click();
  await expect(app.getByRole('heading', { name: '저장 전 확인 · 1문제', exact: true })).toHaveCount(
    0,
  );
  await app.getByText('글에서 문항 만들기', { exact: true }).click();
  await expect(text).toHaveValue('');
});

test('single-option written retry keeps manual correction; click and Enter work in production sandbox', async ({
  page,
}) => {
  await page.addInitScript(
    ({ scope, one }) =>
      localStorage.setItem(
        'hnk_v1',
        JSON.stringify({
          version: 1,
          accounts: {
            [scope]: {
              initialized: true,
              selectedChapters: [one.chapter],
              shuffle: false,
              levels: {},
              activeDeckId: 'synthetic-deck',
              decks: [
                {
                  id: 'synthetic-deck',
                  title: '합성 문제집',
                  questions: [one],
                  createdAt: '2026-10-12T00:00:00Z',
                },
              ],
            },
          },
        }),
      ),
    { scope, one },
  );
  const app = await home(page);
  await app.getByRole('button', { name: '학습하기', exact: true }).click();
  const input = app.getByLabel('정답 입력', { exact: true });
  await input.fill('합성 오답');
  const imeDefaultPrevented = await input.evaluate((element) => {
    const composing = new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
      isComposing: true,
    });
    const legacyComposition = new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
      keyCode: 229,
    });
    element.dispatchEvent(composing);
    element.dispatchEvent(legacyComposition);
    element.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, repeat: true }),
    );
    return [composing.defaultPrevented, legacyComposition.defaultPrevented];
  });
  expect(imeDefaultPrevented).toEqual([false, false]);
  await expect(app.getByText('다시 익혀 볼까요?', { exact: true })).toHaveCount(0);
  await app.getByRole('button', { name: '정답 확인', exact: true }).click();
  await expect(app.getByRole('button', { name: '내 답도 맞아요', exact: true })).toBeVisible();
  await app.getByRole('button', { name: '계속하기', exact: true }).click();
  await input.fill('합성 다른 오답');
  await input.press('Enter');
  await expect(app.getByRole('button', { name: '내 답도 맞아요', exact: true })).toBeVisible();
  await app.getByRole('button', { name: '내 답도 맞아요', exact: true }).click();
  await app.getByRole('button', { name: '바로 계속', exact: true }).click();
  await expect(app.getByRole('heading', { name: '모두 외웠어요!', exact: true })).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        (scope) =>
          JSON.parse(localStorage.getItem('hnk_v1')!).accounts[scope].levels['synthetic-one'],
        scope,
      ),
    )
    .toBe(2);
});
