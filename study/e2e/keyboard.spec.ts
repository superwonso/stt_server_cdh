import type { CDPSession, FrameLocator, Page } from '@playwright/test';
import { test, expect } from './fixtures';

const scope = 'a'.repeat(64);
const questions = Array.from({ length: 8 }, (_, index) => ({
  id: `keyboard-synthetic-${index + 1}`,
  chapter: '키보드 합성 단원',
  sub: '',
  number: index + 1,
  question: `키보드 합성 문제 ${index + 1}`,
  answer: index === 0 ? '1234' : `검증답${index + 1}`,
  type: 'short',
}));

async function start(page: Page, levels: Record<string, number> = {}) {
  await page.addInitScript(
    ({ scope, questions, levels }) => {
      localStorage.setItem(
        'hnk_v1',
        JSON.stringify({
          version: 1,
          accounts: {
            [scope]: {
              initialized: true,
              selectedChapters: [questions[0].chapter],
              shuffle: false,
              levels,
              activeDeckId: 'keyboard-synthetic',
              decks: [
                {
                  id: 'keyboard-synthetic',
                  title: '키보드 합성 문제집',
                  questions,
                  createdAt: '2026-10-13T00:00:00Z',
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
  const name = Object.values(levels).some((level) => level > 0) ? '이어서 학습' : '학습하기';
  await app.getByRole('button', { name, exact: true }).click();
  await expect(app.getByTestId('question')).toHaveText(questions[0].question);
  return app;
}

async function currentQuestion(app: FrameLocator) {
  const text = await app.getByTestId('question').innerText();
  const question = questions.find((item) => item.question === text);
  expect(question).toBeTruthy();
  return question!;
}

async function choiceNumber(app: FrameLocator, correct: boolean) {
  const question = await currentQuestion(app);
  const values = await app.locator('.study-choice > span:last-child').allTextContents();
  const index = values.findIndex((value) => (value === question.answer) === correct);
  expect(index).toBeGreaterThanOrEqual(0);
  return String(index + 1);
}

async function storedLevels(page: Page) {
  return page.evaluate(
    (scope) => JSON.parse(localStorage.getItem('hnk_v1')!).accounts[scope].levels,
    scope,
  );
}

// Playwright's US layout maps Numpad1 to End without NumLock. Send a trusted
// Chromium keypad event with its numeric value instead of simulating Shift+End.
async function keypad(
  session: CDPSession,
  digit: string,
  type: 'keyDown' | 'keyUp',
  autoRepeat = false,
) {
  await session.send('Input.dispatchKeyEvent', {
    type,
    key: digit,
    code: `Numpad${digit}`,
    windowsVirtualKeyCode: 96 + Number(digit),
    isKeypad: true,
    ...(type === 'keyDown' ? { text: digit, autoRepeat } : {}),
  });
}

test('number and keypad choices and feedback work; modifiers, IME and consumed events do nothing', async ({
  page,
}) => {
  const app = await start(page);
  await app.getByTestId('question').evaluate((element) => {
    const variants: KeyboardEventInit[] = [
      { ctrlKey: true },
      { altKey: true },
      { metaKey: true },
      { shiftKey: true },
      { isComposing: true },
      { keyCode: 229 },
    ];
    for (const extra of variants) {
      element.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: '1',
          code: 'Digit1',
          bubbles: true,
          cancelable: true,
          ...extra,
        }),
      );
      element.dispatchEvent(
        new KeyboardEvent('keyup', { key: '1', code: 'Digit1', bubbles: true }),
      );
    }
    const consumed = new KeyboardEvent('keydown', {
      key: '1',
      code: 'Digit1',
      bubbles: true,
      cancelable: true,
    });
    consumed.preventDefault();
    element.dispatchEvent(consumed);
    element.dispatchEvent(new KeyboardEvent('keyup', { key: '1', code: 'Digit1', bubbles: true }));
  });
  await expect(app.locator('.study-feedback')).toHaveCount(0);
  await expect(app.getByTestId('question')).toHaveText(questions[0].question);
  expect(await storedLevels(page)).toEqual({});

  for (const digit of [1, 2, 3, 4]) {
    const question = await currentQuestion(app);
    const selected = app.locator('.study-choice').nth(digit - 1);
    const value = await selected.locator('span:last-child').innerText();
    await page.keyboard.press(String(digit));
    await expect(app.locator('.study-feedback')).toHaveAttribute(
      'data-correct',
      String(value === question.answer),
    );
    await expect(selected).toHaveAttribute(
      'data-result',
      value === question.answer ? 'correct' : 'wrong',
    );
    await page.keyboard.press(String((digit % 4) + 1));
    await expect(app.locator('.study-feedback')).toHaveCount(0);
    await expect(app.getByTestId('question')).not.toHaveText(question.question);
  }

  const session = await page.context().newCDPSession(page);
  try {
    for (const correct of [false, true]) {
      const question = await currentQuestion(app);
      const digit = await choiceNumber(app, correct);
      await keypad(session, digit, 'keyDown');
      await keypad(session, digit, 'keyUp');
      await expect(app.locator('.study-feedback')).toHaveAttribute('data-correct', String(correct));
      await keypad(session, '4', 'keyDown');
      await keypad(session, '4', 'keyUp');
      await expect(app.locator('.study-feedback')).toHaveCount(0);
      await expect(app.getByTestId('question')).not.toHaveText(question.question);
    }
  } finally {
    await session.detach();
  }
});

test('holding a correct choice across automatic feedback cannot answer or type into the next question', async ({
  page,
}) => {
  const levels = Object.fromEntries(
    questions.map((question, index) => [question.id, index ? 1 : 0]),
  );
  const app = await start(page, levels);
  const digit = await choiceNumber(app, true);
  await page.keyboard.down(digit);
  await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
  await page.keyboard.down(digit); // A second down without up is a real repeat.
  await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
  // Waiting for the next question observes the real 850ms automatic transition.
  await expect(app.getByTestId('question')).toHaveText(questions[1].question);
  const input = app.getByLabel('정답 입력', { exact: true });
  await expect(input).toBeFocused();
  await page.keyboard.down(digit);
  await expect(input).toHaveValue('');
  await expect(app.locator('.study-feedback')).toHaveCount(0);
  await expect.poll(() => storedLevels(page)).toEqual({ ...levels, [questions[0].id]: 1 });
  await page.keyboard.up(digit);
  await page.keyboard.type('1234');
  await expect(input).toHaveValue('1234');
  await expect(app.locator('.study-feedback')).toHaveCount(0);
});

test('written digits and Enter work; one continued key cannot cross the round boundary or answer the next round', async ({
  page,
}) => {
  const levels = Object.fromEntries(
    questions.map((question, index) => [question.id, index < 7 ? 1 : 0]),
  );
  const app = await start(page, levels);
  for (let index = 0; index < 7; index++) {
    await expect(app.getByTestId('question')).toHaveText(questions[index].question);
    const input = app.getByLabel('정답 입력', { exact: true });
    await expect(input).toBeFocused();
    await page.keyboard.type(questions[index].answer);
    await expect(input).toHaveValue(questions[index].answer);
    await expect(app.locator('.study-feedback')).toHaveCount(0);
    await page.keyboard.press('Enter');
    await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
    if (index < 6) await page.keyboard.press('2');
    else await page.keyboard.down('2');
  }
  const completed = app.getByRole('heading', { name: '1라운드 완료', exact: true });
  await expect(completed).toBeVisible();
  await page.keyboard.down('2');
  await expect(completed).toBeVisible();
  await expect(app.getByTestId('question')).toHaveCount(0);
  await page.keyboard.up('2');

  const session = await page.context().newCDPSession(page);
  try {
    await keypad(session, '1', 'keyDown');
    await expect(app.getByTestId('question')).toHaveText(questions[7].question);
    await keypad(session, '1', 'keyDown', true);
    await expect(app.locator('.study-feedback')).toHaveCount(0);
    expect((await storedLevels(page))[questions[7].id]).toBe(0);
    await keypad(session, '1', 'keyUp');
  } finally {
    await session.detach();
  }
  await page.keyboard.press(await choiceNumber(app, true));
  await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
  await page.keyboard.press('3');
  const finalInput = app.getByLabel('정답 입력', { exact: true });
  await expect(finalInput).toBeFocused();
  await page.keyboard.type(questions[7].answer);
  await page.keyboard.press('Enter');
  await expect(app.getByText('✓ 정답이에요', { exact: true })).toBeVisible();
  await page.keyboard.press('4');
  await expect(app.getByRole('heading', { name: '모두 외웠어요!', exact: true })).toBeVisible();
  await page.keyboard.press('1');
  await expect(app.getByRole('heading', { name: '모두 외웠어요!', exact: true })).toBeVisible();
  await expect(app.getByTestId('question')).toHaveCount(0);
});
