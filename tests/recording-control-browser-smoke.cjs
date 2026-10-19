/* Actual Chromium, synthetic loopback API/audio only. No production accounts. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('../study/node_modules/@playwright/test');

async function main() {
  const stateFile = process.argv[process.argv.indexOf('--state-file') + 1];
  const output = process.argv[process.argv.indexOf('--output') + 1];
  assert.ok(stateFile && output, 'Supply --state-file and --output');
  const metadata = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  const origin = metadata.origin;
  assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.RECORDING_BROWSER_EXECUTABLE,
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${metadata.fake_audio}`],
  });
  let phase = 'login';
  const errors = [], blocked = [], checks = [], created = [];
  try {
    const context = await browser.newContext({permissions: ['microphone'], viewport: {width: 1280, height: 1000}});
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      blocked.push(new URL(route.request().url()).origin);
      return route.abort();
    });
    await context.addInitScript(origin => {
      localStorage.setItem('yeobaek-server', origin);
      window.__syntheticTracks = [];
      const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (...args) => {
        const stream = await original(...args);
        window.__syntheticTracks.push(...stream.getTracks());
        return stream;
      };
    }, origin);
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', async response => {
      if (response.request().method() === 'POST' && new URL(response.url()).pathname === '/lectures' && response.status() === 201) {
        created.push((await response.json()).id);
      }
    });
    const fixture = async (route, method = 'GET') => {
      const response = await context.request.fetch(`${origin}/_fixture/${route}`, {
        method, headers: {'x-fixture': 'synthetic-recording-control-only'},
      });
      assert.equal(response.ok(), true, `fixture ${route}`);
      return response.json();
    };
    const wait = async (check, name, timeout = 35000) => {
      const end = Date.now() + timeout;
      while (Date.now() < end) {
        if (await check()) return;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw Error(`Timed out: ${name}`);
    };
    const noLiveTracks = () => page.evaluate(() => window.__syntheticTracks.every(track => track.readyState === 'ended'));
    const localState = () => page.evaluate(() => new Promise((resolve, reject) => {
      const request = indexedDB.open('yeobaek-live-audio');
      request.onerror = () => reject(Error('Synthetic queue unavailable'));
      request.onsuccess = () => {
        const db = request.result;
        const names = Array.from(db.objectStoreNames);
        const tx = db.transaction(names, 'readonly');
        const reads = Object.fromEntries(names.map(name => [name, tx.objectStore(name).getAll()]));
        tx.oncomplete = () => { resolve(Object.fromEntries(names.map(name => [name, reads[name].result]))); db.close(); };
        tx.onerror = () => reject(Error('Synthetic queue read failed'));
      };
    }));
    const begin = async title => {
      await page.locator('#lecture-title').fill(title);
      await page.locator('#language').selectOption('en');
      await page.locator('#record-button').click();
      await wait(() => page.locator('#recording-stop').isEnabled(), 'explicit stop available');
    };
    await page.goto(`${origin}/app/`);
    await page.locator('#username').fill('user-alpha');
    await page.locator('#password').fill('synthetic-test-password');
    await page.locator('#login-button').click();
    await page.locator('#workspace').waitFor({state: 'visible'});

    phase = 'manual stop while ASR blocked';
    await begin('합성 수동 중단');
    await wait(async () => (await fixture('state')).calls >= 1, 'blocked synthetic ASR');
    await page.screenshot({path: path.join(output, 'manual-before.png')});
    await page.locator('#recording-stop').click();
    await wait(noLiveTracks, 'microphone ended');
    await wait(async () => (await fixture('state')).cancelled === 1, 'server cancelled only request');
    await wait(() => page.locator('#new-note').isEnabled(), 'new lesson available after stop');
    assert.equal(await page.locator('#recording-finalize').isEnabled(), false, 'stopped ASR cannot be silently finalized again');
    assert.equal((await fixture('state')).segments, 0, 'late result after cancellation must not commit');
    let stored = await localState();
    assert.ok(stored.sessions.length >= 1, 'stopped recording metadata retained');
    assert.ok(stored.chunks.length >= 1 || stored.tails?.length >= 1, 'untranscribed original retained');
    assert.equal((await page.locator('#processing-text').textContent()).includes('녹음은 기다리지 않고 계속'), false);
    checks.push('manual stop during blocked ASR ends tracks, cancels request, preserves audio, discards late result, enables new lesson');
    await page.screenshot({path: path.join(output, 'manual-after.png')});

    phase = 'server timeout synchronization';
    await page.locator('#new-note').click();
    await begin('합성 자동 중단');
    await wait(async () => (await fixture('state')).calls >= 2, 'second blocked request');
    await fixture('expire', 'POST');
    await wait(noLiveTracks, 'server timeout synchronized to browser');
    await wait(() => page.locator('#new-note').isEnabled(), 'new lesson after server timeout');
    assert.equal((await fixture('state')).segments, 0);
    stored = await localState();
    assert.ok(stored.sessions.length >= 2, 'both stopped lessons retained independently');
    checks.push('server 180-second deadline stops browser capture and preserves separate recordings');

    phase = 'new lesson still works';
    await fixture('unblock', 'POST');
    await page.locator('#new-note').click();
    await begin('합성 새 수업');
    await wait(async () => (await fixture('state')).segments > 0, 'new lesson transcript');
    await page.locator('#record-button').click();
    await wait(noLiveTracks, 'normal recording stop');
    assert.equal(new Set(created).size, 3, 'new lesson IDs are independent');
    checks.push('new independent lesson transcribes after both stop paths; normal stop still ends input');

    phase = 'stop while another tab owns native uploader lock';
    await wait(() => page.locator('#new-note').isEnabled(), 'normal stop drained');
    await page.evaluate(async () => {
      const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('user-alpha'));
      const owner = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
      window.__testUploaderLockName = `yeobaek-live-v1:uploader:${owner}`;
      await new Promise(resolve => {
        window.__testUploaderLock = navigator.locks.request(window.__testUploaderLockName, async () => {
          resolve();
          await new Promise(release => {window.__releaseTestUploader = release;});
        });
      });
    });
    const callsBeforeLock = (await fixture('state')).calls;
    await page.locator('#new-note').click();
    await begin('합성 잠금 대기 중단');
    await wait(() => page.evaluate(async () => {
      const locks = await navigator.locks.query();
      return locks.pending.some(lock => lock.name === window.__testUploaderLockName);
    }), 'native uploader lock queued');
    await page.locator('#recording-stop').click();
    await wait(noLiveTracks, 'stop while waiting for native lock');
    await wait(() => page.locator('#new-note').isEnabled(), 'lock wait does not block new lesson');
    await page.evaluate(async () => {window.__releaseTestUploader(); await window.__testUploaderLock;});
    await wait(() => page.evaluate(async () => !(await navigator.locks.query()).pending
      .some(lock => lock.name === window.__testUploaderLockName)), 'cancelled lock removed');
    assert.equal((await fixture('state')).calls, callsBeforeLock, 'cancelled waiter must not submit audio after lock release');
    stored = await localState();
    assert.ok(stored.sessions.length >= 3, 'stopped lock-wait recording retained');
    checks.push('native Web Locks wait can be stopped immediately without a late ASR upload after release');
    phase = 'responsive';
    for (const width of [1280, 390, 320]) {
      await page.setViewportSize({width, height: 1000});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow ${width}`);
      await page.screenshot({path: path.join(output, `stopped-${width}.png`)});
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(blocked, []);
    const report = {passed: true, checks, pageErrors: errors, externalRequests: blocked, syntheticOnly: true, paidCalls: 0,
      limitations: ['Synthetic audio and ASR; no production recordings, paid calls, real microphones, or GPU preemption tested.', 'Server clock advanced by 181 seconds; this is not a long-duration endurance test.']};
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } catch (error) {
    console.error(JSON.stringify({passed: false, phase, error: error.message, pageErrors: errors, externalRequests: blocked}));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error.message); process.exitCode = 1;});
