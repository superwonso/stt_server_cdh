#!/usr/bin/env node
/** Chromium/IndexedDB crash and quota recovery checks. Synthetic loopback only.
 * Reuses the isolated browser_fixture.py; no production account, audio, or API.
 * node scripts/validate_durability.mjs --sandbox /tmp/stt-browser-check.EXAMPLE
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {resolve, dirname, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const argumentIndex = process.argv.indexOf('--sandbox');
assert.ok(argumentIndex >= 0 && process.argv[argumentIndex + 1], 'Supply --sandbox explicitly');
const sandbox = resolve(process.argv[argumentIndex + 1]);
assert.equal(dirname(sandbox), '/tmp');
assert.ok(sandbox.split('/').at(-1).startsWith('stt-browser-check.'));
const runDirectory = join(sandbox, `durability-${Date.now()}`);
const artifacts = join(sandbox, `durability-artifacts-${Date.now()}`);
await mkdir(artifacts, {mode:0o700});
process.env.PLAYWRIGHT_BROWSERS_PATH = join(sandbox, 'browsers');
const {chromium} = await import(pathToFileURL(join(sandbox, 'node_modules/playwright/index.mjs')));
const report = {synthetic_only:true, results:[], limitations:[
  'Injected IndexedDB write failures are not a real full-disk or storage-eviction test.',
  'A crashed desktop Chromium renderer is not an OS power-loss, tablet, or school Wi-Fi test.',
  'Digital silence and fake ASR cannot establish transcription accuracy.',
]};
const note = (name, details = {}) => {
  report.results.push({name, ...details});
  process.stdout.write(`${JSON.stringify({check:name,...details})}\n`);
};
async function poll(predicate, label, timeout = 30000) {
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
let fixtureLog = '', browser, page, metadata;
fixture.stdout.on('data', data => { fixtureLog += data.toString(); });
fixture.stderr.on('data', data => { fixtureLog += data.toString(); });
const pageErrors = [], blockedOrigins = new Set(), chunkRequests = [];
async function queueSnapshot(target = page) {
  return target.evaluate(() => new Promise((resolve, reject) => {
    const opening = indexedDB.open('yeobaek-live-audio');
    opening.onerror = () => reject(opening.error);
    opening.onsuccess = () => {
      const db = opening.result;
      const names = Array.from(db.objectStoreNames);
      const transaction = db.transaction(names, 'readonly');
      const rows = Object.fromEntries(names.map(name => [name,transaction.objectStore(name).getAll()]));
      transaction.oncomplete = () => {
        const summary = Object.fromEntries(names.map(name => [name,rows[name].result.map(row => ({
          id:row.id, captureId:row.captureId, sequence:row.sequence, state:row.state,
          startSamples:row.startSamples, durationSamples:row.durationSamples,
          overlapSamples:row.overlapSamples, revision:row.revision, bytes:row.blob?.size || 0,
        }))]));
        resolve(summary); db.close();
      };
      transaction.onerror = () => { reject(transaction.error); db.close(); };
    };
  }));
}
const state = () => fetch(`${metadata.api_origin}/__validation__/state`).then(response=>response.json());
async function login(target, account = 0) {
  await target.goto(metadata.site_origin);
  await poll(() => target.locator('#login-button').isEnabled(), 'local server verification');
  await target.locator('#username').fill(metadata.accounts[account]);
  await target.locator('#password').fill(metadata.password);
  await target.locator('#login-button').click();
  await poll(async () => await target.locator('#current-user').textContent() === metadata.accounts[account], 'synthetic login');
}
try {
  await poll(async () => {
    if (fixture.exitCode !== null) throw new Error('Synthetic fixture stopped');
    try { metadata = JSON.parse(await readFile(join(runDirectory,'fixture.json'),'utf8')); }
    catch { return false; }
    return (await fetch(`${metadata.api_origin}/health`)).ok;
  }, 'fixture startup');
  const permitted = new Set([metadata.api_origin,metadata.site_origin]);
  browser = await chromium.launch({headless:true,env:{...process.env,
    LD_LIBRARY_PATH:join(sandbox,'libraries/usr/lib/x86_64-linux-gnu'),
  },args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${metadata.fake_audio}`,'--disable-background-networking']});
  report.browser = browser.version();
  const context = await browser.newContext({permissions:['microphone'],acceptDownloads:true});
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (!permitted.has(url.origin)) {
      blockedOrigins.add(url.origin); await route.abort('blockedbyclient'); return;
    }
    if (url.origin === metadata.api_origin && request.method() === 'POST' && /\/chunks$/.test(url.pathname)) {
      const headers = request.headers();
      chunkRequests.push({id:headers['x-chunk-id'],lectureId:url.pathname.split('/')[2],
        final:headers['x-final-chunk'] === 'true'});
    }
    await route.continue();
  });
  await context.addInitScript(({siteOrigin,apiOrigin}) => {
    if (location.origin !== siteOrigin) return;
    localStorage.setItem('yeobaek-server',apiOrigin);
    window.__durabilityFault = {enabled:false,failures:0};
    for (const method of ['add','put']) {
      const original = IDBObjectStore.prototype[method];
      IDBObjectStore.prototype[method] = function(...args) {
        if (window.__durabilityFault.enabled && this.name === 'chunks') {
          window.__durabilityFault.failures += 1;
          throw new DOMException('Synthetic temporary quota failure','QuotaExceededError');
        }
        return original.apply(this,args);
      };
    }
  }, {siteOrigin:metadata.site_origin,apiOrigin:metadata.api_origin});
  page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await login(page);
  await page.locator('#lecture-title').fill('Synthetic temporary storage failure');
  await page.evaluate(() => { window.__durabilityFault.enabled = true; });
  await page.locator('#record-button').click();
  await poll(async () => (await page.evaluate(() => window.__durabilityFault.failures)) >= 4, 'four failed chunk writes', 25000);
  assert.equal(chunkRequests.length,0,'No ASR POST before durable persistence');
  assert.ok((await page.locator('#record-state').textContent()).includes('듣고'),'Storage fault must not silently end capture');
  await page.evaluate(() => { window.__durabilityFault.enabled = false; window.dispatchEvent(new Event('online')); });
  await poll(() => chunkRequests.length > 0, 'upload resumes after temporary storage failure', 45000);
  await page.locator('#record-button').click();
  await poll(async () => (await state()).lectures.some(row => row.recording_finalized), 'recovered recording finalized');
  await poll(async () => (await queueSnapshot()).chunks.length === 0, 'recovered queue acknowledged');
  const recovered = await state();
  assert.equal(new Set(chunkRequests.map(row=>row.id)).size,chunkRequests.length,'No duplicate ASR POST');
  assert.equal(recovered.chunks.filter(row=>row.final_chunk).length,1);
  note('four-write-failures-capture-continues-and-same-session-recovers',{
    failures:await page.evaluate(() => window.__durabilityFault.failures),
    recordingSeconds:recovered.lectures[0].recording_seconds,chunks:recovered.chunks.length,
  });

  // Crash a second renderer before the first 8-second ASR chunk exists. Unlike
  // page.close()/reload(), Page.crash cannot rely on pagehide checkpoint events.
  await page.locator('#logout').click();
  await login(page,1);
  await page.locator('#lecture-title').fill('Synthetic unfinished PCM crash');
  const beforeCrashRequests = chunkRequests.length;
  await page.locator('#record-button').click();
  let snapshot;
  await poll(async () => {
    const data = await queueSnapshot();
    const snapshots = Object.entries(data).filter(([name]) => !['chunks','sessions'].includes(name))
      .flatMap(([,rows]) => rows);
    snapshot = snapshots.find(row => row.bytes > 44 && row.durationSamples >= 32000);
    return !!snapshot;
  }, 'durable unfinished PCM before first ASR chunk', 6500);
  assert.equal(chunkRequests.length,beforeCrashRequests);
  const crash = await context.newCDPSession(page);
  const crashed = page.waitForEvent('crash',{timeout:10000});
  void crash.send('Page.crash').catch(()=>{});
  await crashed;
  const crashedPage = page;
  page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await login(page,1);
  const dataAfterCrash = await queueSnapshot();
  const retained = Object.entries(dataAfterCrash).filter(([name]) => !['chunks','sessions'].includes(name))
    .flatMap(([,rows])=>rows).find(row=>row.bytes > 44);
  assert.ok(retained,'Committed unfinished PCM survives renderer crash');
  const expectedSamples = retained.startSamples + retained.durationSamples;
  await poll(() => page.locator('#lecture-list button').count().then(count=>count > 0), 'crashed lesson history');
  await page.locator('#lecture-list button').first().click();
  await poll(() => page.locator('#recording-download').isEnabled(), 'explicit recovery finalization control');
  await page.locator('#recording-download').click();
  await poll(async () => (await state()).lectures.filter(row=>row.recording_finalized).length === 2, 'partial PCM promoted and finalized');
  const final = await state();
  const crashedLecture = final.lectures.find(row=>row.id === retained.captureId);
  assert.ok(crashedLecture);
  assert.equal(Math.round(crashedLecture.recording_seconds * 16000),expectedSamples,'Recovered WAV sample count matches the persisted PCM timeline');
  assert.equal(chunkRequests.filter(row=>row.lectureId === retained.captureId && row.final).length,1);
  await crashedPage.close().catch(()=>{});
  note('renderer-crash-before-asr-chunk-preserves-pcm-and-explicit-finalization',{
    persistedSamples:expectedSamples,recordingSeconds:crashedLecture.recording_seconds,
  });
  const queueResult = await page.evaluate(async () => {
    const {DurableLiveQueue}=await import('./live-queue.js');
    const {encodeWav}=await import('./audio.js');
    const queue=new DurableLiveQueue(), owner='synthetic-queue-check', captureId=crypto.randomUUID(), id=crypto.randomUUID();
    const pcm=Float32Array.from({length:32000},(_,index)=>(index % 251 - 125)/128);
    const input={id,startSamples:0,durationSamples:32000,overlapSamples:0,final:false,blob:encodeWav(pcm)};
    const same=async(left,right)=>{
      const a=new Uint8Array(await left.arrayBuffer()),b=new Uint8Array(await right.arrayBuffer());
      return a.length === b.length && a.every((value,index)=>value === b[index]);
    };
    try {
      await queue.createSession({id:captureId,owner,title:'Synthetic IDB transaction',language:'ko',source:'microphone',asrProvider:'qwen'});
      await queue.saveSnapshot(owner,captureId,{sequence:0,startSamples:0,durationSamples:16000,overlapSamples:0,blob:encodeWav(pcm.slice(0,16000))});
      await queue.enqueueChunk(owner,captureId,input);
      const duplicate=await queue.enqueueChunk(owner,captureId,{...input,blob:encodeWav(pcm)});
      let changedRejected=false;
      try { await queue.enqueueChunk(owner,captureId,{...input,blob:encodeWav(new Float32Array(32000))}); }
      catch(error){ changedRejected=error.code === 'live_queue_conflict'; }
      const guard=await queue.getSnapshot(owner,captureId), stored=await queue.getChunk(owner,id);
      const exactPcm=await same(stored.blob,input.blob), exactGuard=await same(guard.blob,input.blob);
      await queue.ackChunk(owner,id);
      const final=await queue.promoteSnapshot(owner,captureId,{id:crypto.randomUUID(),expectedRevision:guard.revision});
      await queue.ackChunk(owner,final.id);
      return {duplicateSequence:duplicate.sequence,changedRejected,exactPcm,exactGuard,
        finalFreshSamples:final.durationSamples-final.overlapSamples,
        sessionCleared:(await queue.getSession(owner,captureId)) === null};
    } finally { await queue.deleteSession(owner,captureId); queue.close(); }
  });
  assert.deepEqual(queueResult,{duplicateSequence:0,changedRejected:true,exactPcm:true,exactGuard:true,finalFreshSamples:0,sessionCleared:true});
  note('real-indexeddb-idempotent-pcm-promotion-and-patterned-byte-preservation',queueResult);
  assert.deepEqual(pageErrors,[]);
  assert.deepEqual([...blockedOrigins],[]);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.error = error.message; report.pageErrors = pageErrors;
  report.ui = await page?.locator('body').innerText().catch(()=>'');
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
