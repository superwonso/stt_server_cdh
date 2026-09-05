#!/usr/bin/env node
/** Real Chromium + HTTP + IndexedDB/AudioWorklet checks using synthetic local services only.
 * Install Playwright under a fresh /tmp/stt-browser-check.* directory, then run:
 * node scripts/validate_browser.mjs --sandbox /tmp/stt-browser-check.EXAMPLE
 * The companion fixture never uses deployment settings, accounts, audio, or APIs.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {resolve, dirname, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const sandbox = resolve(process.argv[process.argv.indexOf('--sandbox') + 1] || '');
assert.equal(dirname(sandbox), '/tmp', 'Use an explicit /tmp/stt-browser-check.* directory');
assert.ok(sandbox.split('/').at(-1).startsWith('stt-browser-check.'), 'Use a dedicated validation directory');
const runDirectory = join(sandbox, `run-${Date.now()}`);
const artifacts = join(sandbox, `artifacts-${Date.now()}`);
await mkdir(artifacts, {mode:0o700});
process.env.PLAYWRIGHT_BROWSERS_PATH = join(sandbox, 'browsers');
const {chromium} = await import(pathToFileURL(join(sandbox, 'node_modules/playwright/index.mjs')));
const report = {synthetic_only:true, results:[], limitations:[
  'No live ASR model or external LLM was called; text is from injected local fakes.',
  'Desktop headless Chromium on loopback is not a school Wi-Fi, tablet, or long-class test.',
]};
const note = (name, details = {}) => {
  report.results.push({name, ...details});
  process.stdout.write(`${JSON.stringify({check:name,...details})}\n`);
};
async function poll(predicate, label, timeout = 20000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try { if (await predicate()) return; } catch (error) { last = error; }
    await delay(100);
  }
  throw new Error(`Timed out: ${label}${last ? ` (${last.message})` : ''}`);
}
const fixture = spawn(join(root, '.venv/bin/python'), [join(root,'scripts/browser_fixture.py'), '--directory',runDirectory], {
  cwd:root, env:{PATH:'/usr/bin:/bin',PYTHONUNBUFFERED:'1'}, stdio:['ignore','pipe','pipe'],
});
let fixtureLog = '', browser, page;
fixture.stdout.on('data', data => { fixtureLog += data.toString(); });
fixture.stderr.on('data', data => { fixtureLog += data.toString(); });
const pageErrors = [], blockedOrigins = new Set();
let metadata, failedUploads = 0, holdUploads = false;
const requests = [], chunkRequests = [];

async function queueSnapshot() {
  return page.evaluate(() => new Promise((resolve, reject) => {
    const opening = indexedDB.open('yeobaek-live-audio');
    opening.onerror = () => reject(opening.error);
    opening.onsuccess = () => {
      const db = opening.result;
      const transaction = db.transaction(['chunks','sessions'], 'readonly');
      const chunks = transaction.objectStore('chunks').getAll();
      const sessions = transaction.objectStore('sessions').getAll();
      transaction.oncomplete = () => {
        resolve({chunks:chunks.result.map(row => ({state:row.state,bytes:row.blob?.size || 0})),
          sessionCount:sessions.result.length});
        db.close();
      };
      transaction.onerror = () => { reject(transaction.error); db.close(); };
    };
  }));
}

try {
  await poll(async () => {
    if (fixture.exitCode !== null) throw new Error('Synthetic fixture exited before startup');
    try { metadata = JSON.parse(await readFile(join(runDirectory,'fixture.json'),'utf8')); }
    catch { return false; }
    return (await fetch(`${metadata.api_origin}/health`)).ok;
  }, 'isolated HTTP fixture startup');
  const permitted = new Set([metadata.api_origin,metadata.site_origin]);
  browser = await chromium.launch({headless:true,env:{...process.env,
    LD_LIBRARY_PATH:join(sandbox,'libraries/usr/lib/x86_64-linux-gnu'),
  },args:[
    '--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${metadata.fake_audio}`,
    '--disable-background-networking',
  ]});
  report.browser = browser.version();
  const context = await browser.newContext({permissions:['microphone'],acceptDownloads:true,viewport:{width:1365,height:1000}});
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (!permitted.has(url.origin)) {
      blockedOrigins.add(url.origin);
      await route.abort('blockedbyclient');
      return;
    }
    if (url.origin === metadata.api_origin) requests.push({method:request.method(),path:url.pathname});
    if (url.origin === metadata.api_origin && request.method() === 'POST' && /\/chunks$/.test(url.pathname)) {
      const headers = request.headers();
      chunkRequests.push({start:Number(headers['x-start-seconds']),overlap:Number(headers['x-overlap-seconds']),
        final:headers['x-final-chunk'] === 'true',id:headers['x-chunk-id'],blocked:holdUploads});
      if (holdUploads) { failedUploads += 1; await route.abort('internetdisconnected'); return; }
    }
    await route.continue();
  });
  await context.addInitScript(({siteOrigin,apiOrigin}) => {
    if (location.origin === siteOrigin) localStorage.setItem('yeobaek-server',apiOrigin);
  }, {siteOrigin:metadata.site_origin,apiOrigin:metadata.api_origin});
  page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(metadata.site_origin);
  await poll(() => page.locator('#login-button').isEnabled(), 'verified local API connection');
  await page.locator('#username').fill(metadata.accounts[0]);
  await page.locator('#password').fill(metadata.password);
  await page.locator('#login-button').click();
  await poll(async () => await page.locator('#current-user').textContent() === metadata.accounts[0], 'actual login');
  const beforeReload = await page.evaluate(() => {
    const raw = sessionStorage.getItem('yeobaek-auth-session-v1');
    return {record:JSON.parse(raw),local:Object.fromEntries(Object.entries(localStorage))};
  });
  assert.ok(beforeReload.record.token);
  assert.ok(!JSON.stringify(beforeReload.local).includes(beforeReload.record.token));
  assert.ok(!JSON.stringify(beforeReload).includes(metadata.password));
  await page.reload();
  await poll(async () => await page.locator('#current-user').textContent() === metadata.accounts[0], 'tab session restore after reload');
  const afterReload = await page.evaluate(() => JSON.parse(sessionStorage.getItem('yeobaek-auth-session-v1')));
  assert.equal(afterReload.token,beforeReload.record.token);
  assert.equal(requests.filter(row => row.path === '/auth/login').length,1);
  assert.ok(requests.some(row => row.path === '/auth/me'));
  note('real-login-and-tab-session-refresh');

  holdUploads = true;
  await page.locator('#lecture-title').fill('Synthetic browser validation');
  await page.locator('#language').selectOption('en');
  await page.locator('#record-button').click();
  await poll(async () => (await page.locator('#record-state').textContent()).includes('듣고'), 'real microphone/AudioWorklet start');
  const startedAt = Date.now();
  await poll(async () => (await queueSnapshot()).chunks.some(row => row.bytes > 44), 'real IndexedDB queued audio Blob', 20000);
  await poll(() => Date.now() - startedAt >= 13500, 'silence exceeds former auto-pause threshold', 16000);
  assert.equal(await page.locator('#pause-button').textContent(),'Ⅱ 일시정지');
  assert.equal(await page.locator('#record-button').isEnabled(),true);
  assert.equal(await page.locator('#auto-silence-pause').count(),0);
  assert.ok((await fetch(`${metadata.api_origin}/__validation__/state`).then(r=>r.json())).worklet_loads > 0,
    'the real worklet module was served; Chromium does not surface worklet fetches as Page responses');
  assert.ok(failedUploads > 0);
  const stored = await queueSnapshot();
  assert.ok(stored.chunks.some(row => row.bytes > 44));
  assert.equal(chunkRequests.some(row => row.final),false);
  note('actual-audioworklet-quiet-capture-and-indexeddb-offline-queue',{quietWallSeconds:(Date.now()-startedAt)/1000,queuedBlobs:stored.chunks.length});

  await page.locator('#pause-button').click();
  await poll(async () => await page.locator('#pause-button').textContent() === '▶ 받아쓰기 재개', 'manual pause acknowledgement');
  const pausedClock = await page.locator('#elapsed').textContent();
  await delay(2200);
  assert.equal(await page.locator('#elapsed').textContent(),pausedClock);
  assert.equal(await page.locator('#record-button').isEnabled(),true);
  holdUploads = false;
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await page.locator('#pause-button').click();
  await poll(async () => await page.locator('#pause-button').textContent() === 'Ⅱ 일시정지', 'manual resume acknowledgement');
  await delay(2500);
  assert.notEqual(await page.locator('#elapsed').textContent(),pausedClock);
  await page.locator('#record-button').click();
  await poll(async () => (await fetch(`${metadata.api_origin}/__validation__/state`).then(r=>r.json())).lectures.some(row=>row.recording_finalized), 'real final WAV upload and recording finalization', 45000);
  await poll(async () => (await queueSnapshot()).chunks.length === 0, 'IndexedDB queue drained after reconnect', 20000);
  const state = await fetch(`${metadata.api_origin}/__validation__/state`).then(r=>r.json());
  assert.equal(state.lectures.length,1);
  assert.equal(state.chunks.filter(row=>row.final_chunk).length,1);
  assert.ok(state.chunks.every(row=>row.status === 'done'));
  assert.equal(new Set(state.chunks.map(row=>row.chunk_id)).size,state.chunks.length);
  assert.ok(state.asr_calls.every(row=>row.peak === 0),'fake input is actual digital silence');
  assert.ok(state.lectures[0].recording_seconds >= 15,'long quiet input remains in the recording');
  const lastChunk = state.chunks.at(-1);
  const lastCall = state.asr_calls.at(-1);
  assert.ok(Math.abs(state.lectures[0].recording_seconds - lastChunk.start_seconds - lastCall.samples/16000) < 0.001);
  note('manual-pause-resume-final-wav-and-queue-recovery',{recordingSeconds:state.lectures[0].recording_seconds,chunks:state.chunks.length});

  await poll(() => page.locator('#summarize-lecture').isEnabled(), 'summary button after finalized transcript');
  await page.locator('#summarize-lecture').click();
  await poll(async () => (await page.locator('#summary-content').textContent()).includes('식물은 빛'), 'actual HTTP fake summary worker/UI');
  await poll(() => page.locator('#translate-lecture').isEnabled(), 'translation button');
  await page.locator('#translate-lecture').click();
  await poll(async () => (await page.locator('#translation-content').textContent()).includes('식물은 빛'), 'actual HTTP fake translation worker/UI');
  assert.ok((await page.locator('#translation-content').textContent()).includes('Plants use light'));
  await page.locator('#translation-full').click();
  assert.ok(!(await page.locator('#translation-content').textContent()).includes('Plants use light'));
  assert.ok((await page.locator('#transcript').textContent()).includes('Plants use light'),'translation does not replace the raw transcript');
  for (const [selector,name,expected] of [['#summary-download','summary.md','식물은'],['#translation-download','translation.md','식물은']]) {
    const pending = page.waitForEvent('download');
    await page.locator(selector).click();
    const download = await pending;
    await download.saveAs(join(artifacts,name));
    assert.ok((await readFile(join(artifacts,name),'utf8')).includes(expected));
  }
  const final = await fetch(`${metadata.api_origin}/__validation__/state`).then(r=>r.json());
  assert.equal(final.summary_calls,1);
  assert.equal(final.translation_calls,1);
  await page.screenshot({path:join(artifacts,'synthetic-summary-translation.png'),fullPage:true});
  note('real-summary-translation-jobs-ui-and-markdown-downloads');
  await page.locator('#logout').click();
  await poll(() => page.locator('#auth-screen').isVisible(), 'logout');
  assert.equal(await page.evaluate(() => sessionStorage.getItem('yeobaek-auth-session-v1')),null);
  await page.locator('#username').fill(metadata.accounts[1]);
  await page.locator('#password').fill(metadata.password);
  await page.locator('#login-button').click();
  await poll(async () => await page.locator('#current-user').textContent() === metadata.accounts[1], 'second synthetic account login');
  assert.equal(await page.locator('#lecture-list button').count(),0);
  note('logout-clears-tab-session-and-second-account-has-no-first-account-lecture');
  assert.deepEqual(pageErrors,[]);
  assert.deepEqual([...blockedOrigins],[],'browser never even attempts an out-of-fixture network request');
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = error.message;
  report.pageErrors = pageErrors;
  if (page) {
    await page.screenshot({path:join(artifacts,'synthetic-failure.png'),fullPage:true}).catch(()=>{});
    report.ui = await page.locator('body').innerText().catch(()=>'');
  }
  process.exitCode = 1;
} finally {
  await browser?.close().catch(()=>{});
  if (fixture.exitCode === null) {
    fixture.kill('SIGTERM');
    await Promise.race([once(fixture,'exit'),delay(15000,undefined,{ref:false})]);
    if (fixture.exitCode === null) fixture.kill('SIGKILL');
  }
  await writeFile(join(artifacts,'fixture.log'),fixtureLog,{mode:0o600});
  await writeFile(join(artifacts,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
  process.stdout.write(`${JSON.stringify({status:report.status,error:report.error,report:join(artifacts,'report.json')})}\n`);
}
