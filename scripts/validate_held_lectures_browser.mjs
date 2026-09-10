#!/usr/bin/env node
/** Real Chromium held-lecture QA: synthetic loopback fixtures only.
 * node scripts/validate_held_lectures_browser.mjs --sandbox /tmp/stt-browser-check.EXAMPLE
 * Uses the existing fixture without enabling CLOVA or reading private settings.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createHash, randomUUID} from 'node:crypto';
import {readFile, writeFile, mkdir, chmod} from 'node:fs/promises';
import {resolve, dirname, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const argument = process.argv.indexOf('--sandbox');
assert.ok(argument >= 0 && process.argv[argument + 1], 'Supply an isolated --sandbox');
const sandbox = resolve(process.argv[argument + 1]);
assert.equal(dirname(sandbox), '/tmp');
assert.ok(sandbox.split('/').at(-1).startsWith('stt-browser-check.'));
const stamp = Date.now(), runDirectory = join(sandbox, `held-lectures-${stamp}`);
const artifacts = join(sandbox, `held-lectures-artifacts-${stamp}`);
await mkdir(artifacts, {mode:0o700});
process.env.PLAYWRIGHT_BROWSERS_PATH = join(sandbox, 'browsers');
const {chromium} = await import(pathToFileURL(join(sandbox, 'node_modules/playwright/index.mjs')));
const report = {synthetic_only:true, checks:[], limitations:[
  'Desktop Chromium and fake microphone with temporary local API/DB only; no private recordings or real speech providers.',
  'CLOVA lectures and 403/unknown/ACK results are deterministic browser route fixtures; no CLOVA request reaches a real provider.',
  'The 403 retained clips are 0.1-second synthetic clips (40.3 seconds total), exercising queue count and preservation, not hour-long audio volume.',
  'New Qwen requests reach the actual local fixture/Fake ASR. This is not real CLOVA billing, tablet, school Wi-Fi, or long-class endurance validation.',
]};
let browser, context, page, metadata, fixtureLog = '', phase = 'startup';
const pageErrors = [], routeErrors = [], blockedOrigins = new Set(), tokens = new Map();
const chunkRequests = [], oldResultRequests = [], createRequests = [];
const fakeCloudLectures = new Map(), fakeCloudChunks = new Map();
const oldId = randomUUID(), oldChunkId = randomUUID();
const oldTitle = 'Synthetic held CLOVA unresolved';
// "403" in the reported UI is a count of queued clips, not HTTP status 403.
// Keep a separate opt-in permission-error variant; the default mirrors 403
// retained clips whose initial lookup says HTTP 200 / state=unknown.
const oldChunkCount = 403;
let resultMode = process.argv.includes('--old-http-forbidden') ? 'forbidden' : 'unknown';
let loseNextAck = false, heldFirstAckId = '', rejectDraft = false, allowFakeNewClova = false;
report.old_fixture_chunks = oldChunkCount;
report.old_fixture_audio_seconds = oldChunkCount / 10;
report.initial_old_result = resultMode === 'unknown' ? 'HTTP 200 / unknown' : 'HTTP 403 / permission error';
const fixture = spawn(join(root, '.venv/bin/python'),
  [join(root, 'scripts/browser_fixture.py'), '--directory', runDirectory],
  {cwd:root, env:{PATH:'/usr/bin:/bin',PYTHONUNBUFFERED:'1'}, stdio:['ignore','pipe','pipe']});
fixture.stdout.on('data', value => { fixtureLog += value.toString(); });
fixture.stderr.on('data', value => { fixtureLog += value.toString(); });
function note(name, details = {}) {
  report.checks.push({name, ...details});
  process.stdout.write(`${JSON.stringify({check:name, ...details})}\n`);
}
async function poll(test, label, timeout = 25000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    if (fixture.exitCode !== null) throw new Error('Synthetic fixture stopped');
    try { if (await test()) return; } catch {}
    await delay(100);
  }
  throw new Error(`Timed out: ${label}`);
}
async function queueState(target, owner) {
  return target.evaluate(owner => new Promise((resolve, reject) => {
    const request = indexedDB.open('yeobaek-live-audio');
    request.onerror = () => reject(new Error('Synthetic queue open failed'));
    request.onsuccess = () => {
      const db = request.result, stores = Array.from(db.objectStoreNames);
      const transaction = db.transaction(stores, 'readonly');
      const reads = Object.fromEntries(stores.map(name => [name,transaction.objectStore(name).getAll()]));
      transaction.oncomplete = async () => {
        try {
          const output = {};
          for (const name of stores) {
            output[name] = [];
            for (const row of reads[name].result.filter(row => row.owner === owner)) {
              const value = {...row};
              if (row.blob) value.blob = {size:row.blob.size,sha256:Array.from(new Uint8Array(
                await crypto.subtle.digest('SHA-256',await row.blob.arrayBuffer())), byte => byte.toString(16).padStart(2,'0')).join('')};
              output[name].push(value);
            }
            output[name].sort((a,b) => String(a.id || a.captureId).localeCompare(String(b.id || b.captureId)));
          }
          resolve(output);
        } catch { reject(new Error('Synthetic queue digest failed')); }
        finally { db.close(); }
      };
      transaction.onerror = () => { db.close(); reject(new Error('Synthetic queue read failed')); };
    };
  }), owner);
}
async function oldChunk(target = page) {
  return (await queueState(target,metadata.accounts[0])).chunks.find(row => row.id === oldChunkId);
}
async function oldChunks(target = page) {
  return (await queueState(target,metadata.accounts[0])).chunks.filter(row => row.captureId === oldId);
}
async function assertOldRetained(before) {
  assert.equal(before.length,oldChunkCount);
  const current = await oldChunks();
  assert.ok(current.every(row => row.uploadHeld === true),'Every held chunk needs the old-tab upload guard');
  const withoutGuard = rows => rows.map(row => {
    const copy = {...row}; delete copy.uploadHeld; return copy;
  });
  assert.deepEqual(withoutGuard(current),withoutGuard(before),
    'Holding/new work may add only the upload guard; every old field and byte hash stays unchanged');
}
async function assertOldAudioAndGuard(before, held) {
  const current = await oldChunks();
  assert.equal(current.length,oldChunkCount);
  assert.ok(current.every(row => held ? row.uploadHeld === true : !Object.hasOwn(row,'uploadHeld')),
    'Explicit restoration removes the old-tab guard; re-holding restores it');
  const immutable = rows => rows.map(row => Object.fromEntries([
    'id','captureId','owner','sessionCreatedAt','sequence','startSamples','durationSamples',
    'overlapSamples','final','asrProvider','byteLength','createdAt','blob',
  ].map(key => [key,row[key]])));
  assert.deepEqual(immutable(current),immutable(before),'Explicit recovery must preserve all old audio and its ordering');
}
async function sessionRow(id, target = page) {
  return (await queueState(target,metadata.accounts[0])).sessions.find(row => row.id === id);
}
async function serverState() {
  return fetch(`${metadata.api_origin}/__validation__/state`).then(response => response.json());
}
async function downloadOld(target, filename) {
  await target.locator('#local-audio-open').click();
  const link = target.locator('#local-audio-files article').filter({hasText:oldTitle}).locator('a[download]');
  await poll(() => link.count().then(count => count === 1), 'held WAV export link');
  const event = target.waitForEvent('download');
  await link.click();
  const download = await event;
  assert.equal(await download.failure(), null);
  const path = join(artifacts, filename);
  await download.saveAs(path); await chmod(path, 0o600);
  const content = await readFile(path);
  assert.equal(content.toString('ascii',0,4), 'RIFF');
  assert.equal(content.toString('ascii',8,12), 'WAVE');
  assert.equal(content.readUInt32LE(4), content.length - 8);
  assert.equal(content.readUInt32LE(40), content.length - 44);
  assert.equal(content.readUInt32LE(24), 16000);
  assert.ok(content.length > 44);
  await target.locator('#local-audio-close').click();
  return {bytes:content.length,sha256:createHash('sha256').update(content).digest('hex')};
}
async function login(target, index) {
  await target.goto(metadata.site_origin);
  if (await target.locator('#current-user').textContent() === metadata.accounts[index]) return;
  await poll(() => target.locator('#login-button').isEnabled(), 'login ready');
  await target.locator('#username').fill(metadata.accounts[index]);
  await target.locator('#password').fill(metadata.password);
  await target.locator('#login-button').click();
  await poll(async () => await target.locator('#current-user').textContent() === metadata.accounts[index], 'synthetic login');
}
async function holdAndNew(target) {
  await poll(() => target.locator('#hold-new-note').isEnabled(), 'explicit hold control enabled');
  await target.locator('#hold-new-note').click();
  await poll(async () => await target.locator('#record-button').isEnabled()
    && (await target.locator('#record-button').textContent()).includes('시작'), 'held old work and blank new lecture');
}

try {
  await poll(async () => {
    try { metadata = JSON.parse(await readFile(join(runDirectory,'fixture.json'),'utf8')); }
    catch { return false; }
    for (const value of [metadata.api_origin, metadata.site_origin]) {
      const url = new URL(value);
      assert.equal(url.hostname,'127.0.0.1'); assert.notEqual(url.port,'8765');
    }
    assert.equal(dirname(metadata.fake_audio),runDirectory);
    return (await fetch(`${metadata.api_origin}/health`)).ok;
  }, 'isolated fixture readiness');
  browser = await chromium.launch({headless:true, env:{...process.env,
    LD_LIBRARY_PATH:join(sandbox,'libraries/usr/lib/x86_64-linux-gnu')},
    args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${metadata.fake_audio}`,'--disable-background-networking']});
  report.browser = browser.version();
  context = await browser.newContext({permissions:['microphone'],acceptDownloads:true,viewport:{width:1365,height:1000}});
  const allowed = new Set([metadata.api_origin,metadata.site_origin]);
  const oldLecture = {id:oldId,title:oldTitle,display_title:oldTitle,language:'ko',asr_provider:'clova',
    created_at:new Date(Date.now()-86400000).toISOString(),recording_finalized:false,recording_available:false,
    recording_storage:'not_available',recording_duration_seconds:0,recording_bytes:0,
    course:'',semester:'',metadata_revision:0,segments:[],continuation_of:null,continuations:[]};
  await context.route('**/*', async route => {
    try {
    const request = route.request(), url = new URL(request.url());
    if (!allowed.has(url.origin)) { blockedOrigins.add(url.origin); await route.abort('blockedbyclient'); return; }
    const jsonReply = (status, value) => route.fulfill({status,contentType:'application/json',headers:{
      'Access-Control-Allow-Origin':metadata.site_origin,'Access-Control-Allow-Credentials':'true'},body:JSON.stringify(value)});
    if (url.origin === metadata.site_origin && url.pathname === '/config.json') {
      const publishedAt = new Date().toISOString().replace(/\.\d{3}Z$/,'Z');
      return jsonReply(200,{version:1,state:'online',apiUrl:metadata.api_origin,publishedAt,
        expiresAt:new Date(Date.parse(publishedAt)+86400000).toISOString().replace(/\.\d{3}Z$/,'Z')});
    }
    if (url.origin !== metadata.api_origin) { await route.continue(); return; }
    if (url.pathname === '/auth/login' && request.method() === 'POST') {
      const response = await route.fetch(); const body = await response.json();
      if (body.token && body.user?.username) tokens.set(`Bearer ${body.token}`,body.user.username);
      await route.fulfill({response}); return;
    }
    const owner = tokens.get(request.headers().authorization);
    if (url.pathname === '/status' && request.method() === 'GET' && allowFakeNewClova) {
      const response = await route.fetch(), body = await response.json();
      body.transcription_providers.clova.configured = true;
      await route.fulfill({response,json:body}); return;
    }
    if (owner === metadata.accounts[0] && request.method() === 'GET' && url.pathname === '/lectures') {
      const response = await route.fetch(); const rows = await response.json();
      await route.fulfill({response,json:[oldLecture,...fakeCloudLectures.values(),...rows]}); return;
    }
    if (owner === metadata.accounts[0] && request.method() === 'GET' && url.pathname === `/lectures/${oldId}`) {
      await jsonReply(200,oldLecture); return;
    }
    const matchingFakeCloud = fakeCloudLectures.get(url.pathname.split('/')[2]);
    if (matchingFakeCloud && owner === metadata.accounts[0] && request.method() === 'GET'
        && url.pathname === `/lectures/${matchingFakeCloud.id}`) {
      await jsonReply(200,matchingFakeCloud); return;
    }
    if (url.pathname.startsWith(`/lectures/${oldId}/chunks/`) && request.method() === 'GET') {
      oldResultRequests.push(resultMode);
      if (owner !== metadata.accounts[0]) { await jsonReply(404,{detail:'Synthetic owner isolation'}); return; }
      await jsonReply(resultMode === 'forbidden' ? 403 : 200,
        resultMode === 'forbidden' ? {detail:'Synthetic unresolved old audio'} : {state:'unknown'}); return;
    }
    if (request.method() === 'POST' && /\/chunks$/.test(url.pathname)) {
      const id = request.headers()['x-chunk-id'];
      chunkRequests.push({old:url.pathname.includes(oldId),id});
      if (url.pathname.includes(oldId)) { await jsonReply(403,{detail:'Synthetic CLOVA POST forbidden'}); return; }
      if (matchingFakeCloud) {
        assert.equal(owner,metadata.accounts[0]);
        const payload = request.postDataBuffer();
        const hash = createHash('sha256').update(payload).digest('hex');
        const previous = fakeCloudChunks.get(id);
        if (previous) assert.equal(previous.hash,hash,'Fake CLOVA retry must preserve bytes');
        const start = Number(request.headers()['x-start-seconds']);
        const final = request.headers()['x-final-chunk'] === 'true';
        const result = previous?.result || {segments:[{id:randomUUID(),start,end:start+(payload.length-44)/32000,text:'합성 새 CLOVA 전송 성공'}],
          recording_available:true,recording_finalized:final};
        fakeCloudChunks.set(id,{hash,result});
        if (final) matchingFakeCloud.recording_finalized = true;
        await jsonReply(200,result); return;
      }
      if (loseNextAck) {
        loseNextAck = false; heldFirstAckId = id;
        const response = await route.fetch(); assert.equal(response.status(),200);
        await route.abort('failed'); return;
      }
    }
    if (request.method() === 'POST' && url.pathname === '/lectures') {
      const body = request.postDataJSON(); createRequests.push({id:request.headers()['x-lecture-id'],draft:rejectDraft});
      if (body.asr_provider === 'clova') {
        assert.equal(allowFakeNewClova,true,'CLOVA may only use the explicitly enabled synthetic route');
        assert.equal(owner,metadata.accounts[0]);
        const id = request.headers()['x-lecture-id'];
        const lecture = fakeCloudLectures.get(id) || {...oldLecture,id,title:body.title,display_title:body.title,
          language:body.language,created_at:new Date().toISOString()};
        fakeCloudLectures.set(id,lecture);
        await jsonReply(201,lecture); return;
      }
      assert.equal(body.asr_provider,'qwen','No live CLOVA creation is allowed in this fixture');
      if (rejectDraft) { await jsonReply(503,{detail:'Synthetic unregistered draft'}); return; }
    }
    await route.continue();
    } catch {
      routeErrors.push('Synthetic network contract failed');
      await route.abort('failed').catch(() => {});
    }
  });
  await context.addInitScript(({site,api}) => {
    if (location.origin !== site) return;
    localStorage.setItem('yeobaek-server',api);
    window.__heldBrowser = {suppressPCM:false,streams:[],nodes:[]};
    const getUserMedia = navigator.mediaDevices.getUserMedia;
    navigator.mediaDevices.getUserMedia = async function(...args) {
      const stream = await getUserMedia.apply(this,args); window.__heldBrowser.streams.push(stream); return stream;
    };
    const Worklet = window.AudioWorkletNode;
    window.AudioWorkletNode = class extends Worklet {
      constructor(...args) {
        super(...args); window.__heldBrowser.nodes.push(this);
        this.port.addEventListener('message', event => {
          if (window.__heldBrowser.suppressPCM && event.data?.type === 'samples') event.stopImmediatePropagation();
        });
      }
    };
  },{site:metadata.site_origin,api:metadata.api_origin});
  page = await context.newPage(); page.on('pageerror', () => pageErrors.push('main'));
  page.on('dialog', dialog => dialog.accept());
  await login(page,0);
  const seeded = await page.evaluate(async ({owner,id,chunk,title,count}) => {
    const {DurableLiveQueue} = await import('./live-queue.js');
    const {encodeWav} = await import('./audio.js');
    const queue = new DurableLiveQueue();
    const chunkSamples = 1600;
    const pcm = Float32Array.from({length:count*chunkSamples},(_,index) => Math.sin(2*Math.PI*440*index/16000)*0.2);
    const blob = encodeWav(pcm);
    try {
      await queue.createSession({id,owner,title,language:'ko',source:'microphone',asrProvider:'clova',createdAt:Date.now()-86400000});
      for (let index = 0; index < count; index += 1) {
        await queue.enqueueChunk(owner,id,{id:index ? crypto.randomUUID() : chunk,startSamples:index*chunkSamples,
          durationSamples:chunkSamples,overlapSamples:0,final:false,blob:encodeWav(pcm.slice(index*chunkSamples,(index+1)*chunkSamples))});
      }
      await queue.updateSession(owner,id,{state:'stopped',lectureCreated:true});
      await queue.markChunkInflight(owner,chunk);
      return {bytes:blob.size,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer())),
        byte => byte.toString(16).padStart(2,'0')).join('')};
    } finally { queue.close(); }
  },{owner:metadata.accounts[0],id:oldId,chunk:oldChunkId,title:oldTitle,count:oldChunkCount});
  phase = 'old CLOVA uncertain result is not automatically retransmitted';
  await page.reload();
  await poll(() => oldResultRequests.length >= 1,'old CLOVA GET-only recovery');
  await poll(async () => (await oldChunk())?.state === 'blocked','old CLOVA durable blocked state');
  const allBeforeHold = await oldChunks();
  assert.equal(allBeforeHold.length,oldChunkCount);
  assert.equal(chunkRequests.filter(row => row.old).length,0);
  await holdAndNew(page);
  await poll(async () => (await sessionRow(oldId))?.uploadHeld === true,'durable per-lecture hold');
  await assertOldRetained(allBeforeHold);
  const downloaded = await downloadOld(page,'held-old-original.wav');
  assert.deepEqual(downloaded,seeded);
  note(phase,{oldChunkPosts:0,oldRetainedChunks:oldChunkCount,downloadBytes:downloaded.bytes});

  phase = 'held old audio does not block new capture, lost ACK retry, or exact server storage';
  loseNextAck = true;
  await page.locator('#asr-provider').selectOption('qwen');
  await page.locator('#lecture-title').fill('Synthetic new independent class');
  await page.locator('#record-button').click();
  await poll(() => chunkRequests.filter(row => row.id === heldFirstAckId && heldFirstAckId).length >= 2,
    'same UUID retried after a committed lost ACK',45000);
  await poll(async () => (await serverState()).chunks.some(row => row.chunk_id === heldFirstAckId && row.status === 'done'),'new server ACK');
  assert.equal(chunkRequests.filter(row => row.old).length,0);
  await assertOldRetained(allBeforeHold);
  assert.equal(await page.locator('#capture-input-status').getAttribute('data-state'),'receiving');
  await page.locator('#pause-button').click();
  await poll(() => page.locator('#pause-button').getAttribute('aria-pressed').then(value => value === 'true'),'manual pause');
  await page.locator('#record-button').click();
  await poll(async () => (await serverState()).lectures.some(row => row.recording_finalized),'new class final storage',30000);
  const successful = await serverState();
  assert.equal(successful.chunks.filter(row => row.chunk_id === heldFirstAckId).length,1);
  assert.equal(successful.asr_calls.length,successful.chunks.filter(row => row.status === 'done').length,
    'Lost ACK replay must not call Fake ASR twice');
  note(phase,{newChunkPosts:chunkRequests.filter(row => !row.old).length,storedChunks:successful.chunks.length});

  phase = 'reload keeps old hold and other-account UI cannot see its audio';
  const recoveryReads = oldResultRequests.length;
  await page.reload();
  await poll(() => page.locator('#held-audio-panel').isVisible(),'held lecture panel after reload');
  await delay(1500);
  assert.equal(oldResultRequests.length,recoveryReads,'Held CLOVA is not polled or retried on reload');
  await assertOldRetained(allBeforeHold);
  assert.deepEqual(await downloadOld(page,'held-old-after-reload.wav'),seeded);
  const other = await context.newPage(); other.on('pageerror', () => pageErrors.push('other'));
  await login(other,1);
  assert.equal(await other.locator('#held-audio-panel').isVisible(),false);
  assert.ok(!(await other.locator('body').textContent()).includes(oldTitle));
  await other.goto(`${metadata.site_origin}/rescue.html`);
  await poll(() => other.locator('#rescue-login').isEnabled(),'separate account rescue login');
  await other.locator('#rescue-username').fill(metadata.accounts[1]);
  await other.locator('#rescue-password').fill(metadata.password);
  await other.locator('#rescue-login').click();
  await poll(() => other.locator('#rescue-results').isVisible(),'other account authenticated');
  assert.equal(await other.locator('#rescue-files a[download]').count(),0);
  assert.ok(!(await other.locator('body').textContent()).includes(oldTitle));
  await assertOldRetained(allBeforeHold);
  await other.close();
  note(phase,{oldChunkPosts:0,otherAccountDownloads:0});

  phase = 'paused unregistered draft is held without deleting PCM or background creation';
  await page.locator('#new-note').click();
  rejectDraft = true;
  await page.locator('#asr-provider').selectOption('qwen');
  await page.locator('#lecture-title').fill('Synthetic unregistered paused draft');
  await page.locator('#record-button').click();
  await poll(() => createRequests.some(row => row.draft),'draft creation failure');
  await delay(1300);
  await page.locator('#pause-button').click();
  await poll(() => page.locator('#pause-button').getAttribute('aria-pressed').then(value => value === 'true'),'draft manual pause');
  const draftId = createRequests.findLast(row => row.draft).id;
  await holdAndNew(page);
  await poll(async () => (await sessionRow(draftId))?.uploadHeld === true,'unregistered draft held');
  const draftRows = await queueState(page,metadata.accounts[0]);
  assert.ok(draftRows.chunks.some(row => row.captureId === draftId), 'Paused draft PCM is retained');
  const createdBefore = createRequests.filter(row => row.id === draftId).length;
  rejectDraft = false;
  await delay(3500);
  assert.equal(createRequests.filter(row => row.id === draftId).length,createdBefore,'Held draft does not auto-create a server lecture');
  assert.equal((await serverState()).lectures.some(row => row.id === draftId),false);
  await assertOldRetained(allBeforeHold);
  note(phase,{retainedDraftChunks:draftRows.chunks.filter(row => row.captureId === draftId).length});

  phase = 'input interruption remains recoverable and explicit hold preserves its tail';
  await page.locator('#asr-provider').selectOption('qwen');
  await page.locator('#lecture-title').fill('Synthetic interrupted input');
  await page.locator('#record-button').click();
  await poll(() => page.locator('#capture-input-status').getAttribute('data-state').then(value => value === 'receiving'),'new input receiving');
  await delay(600);
  await page.evaluate(() => { window.__heldBrowser.suppressPCM = true; });
  await poll(() => page.locator('#capture-input-status').getAttribute('data-state').then(value => value === 'unavailable'),'input liveness warning',12000);
  await holdAndNew(page);
  await page.evaluate(() => { window.__heldBrowser.suppressPCM = false; });
  await assertOldRetained(allBeforeHold);
  assert.equal(chunkRequests.filter(row => row.old).length,0);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  await page.screenshot({path:join(artifacts,'held-mobile.png')});
  assert.deepEqual(pageErrors,[]);
  assert.deepEqual([...blockedOrigins],[]);
  note(phase,{mobileWidth:390,oldChunkPosts:0});

  phase = 'new CLOVA capture sends independently to a fake provider while old CLOVA stays held';
  allowFakeNewClova = true;
  await page.setViewportSize({width:1365,height:1000});
  await page.reload();
  await poll(() => page.locator('#asr-provider-clova').isEnabled(),'synthetic new CLOVA configured');
  await page.locator('#new-note').click();
  await page.locator('#asr-provider').selectOption('clova');
  await page.locator('#lecture-title').fill('Synthetic new CLOVA independent');
  await page.locator('#record-button').click();
  await poll(() => fakeCloudChunks.size >= 1,'new CLOVA independent POST',30000);
  const newCloudChunk = [...fakeCloudChunks.keys()][0];
  await poll(async () => !(await queueState(page,metadata.accounts[0])).chunks.some(row => row.id === newCloudChunk),'new CLOVA durable ACK');
  assert.equal(chunkRequests.filter(row => row.old).length,0);
  await assertOldRetained(allBeforeHold);
  assert.equal(await page.locator('#capture-input-status').getAttribute('data-state'),'receiving');
  await page.locator('#record-button').click();
  await poll(() => [...fakeCloudLectures.values()].some(lecture => lecture.recording_finalized),'new fake CLOVA finished');
  note(phase,{newFakeClovaChunkPosts:fakeCloudChunks.size,oldChunkPosts:0});

  phase = 'explicit old recovery queries unknown without automatic CLOVA POST';
  resultMode = 'unknown';
  const oldReads = oldResultRequests.length;
  const oldCard = page.locator('#held-audio-list').locator('article').filter({hasText:oldTitle});
  await oldCard.getByRole('button',{name:'선택한 수업 전송 복구',exact:true}).click();
  await poll(() => oldResultRequests.length > oldReads,'explicit old GET-only recovery');
  await poll(async () => (await oldChunk())?.state === 'blocked','unknown old result remains blocked');
  assert.equal(oldResultRequests.at(-1),'unknown');
  assert.equal(chunkRequests.filter(row => row.old).length,0);
  await assertOldAudioAndGuard(allBeforeHold,false);
  await holdAndNew(page);
  await poll(async () => (await sessionRow(oldId))?.uploadHeld === true,'old unknown restored to hold');
  await assertOldAudioAndGuard(allBeforeHold,true);
  assert.deepEqual(await downloadOld(page,'held-old-after-explicit-query.wav'),seeded);
  assert.deepEqual(pageErrors,[]);
  assert.deepEqual(routeErrors,[]);
  assert.deepEqual([...blockedOrigins],[]);
  note(phase,{oldResultQueries:oldResultRequests.length,oldChunkPosts:0});
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.phase = phase; report.error = error.message;
  report.ui = page ? await page.evaluate(() => Object.fromEntries(
    ['record-state','capture-input-status','save-message','held-audio-panel'].map(id => [id,document.getElementById(id)?.textContent || ''])
  )).catch(() => null) : null;
  process.stdout.write(`${JSON.stringify({status:'failed',phase,error:error.message,ui:report.ui})}\n`);
  process.exitCode = 1;
} finally {
  await browser?.close().catch(() => {});
  if (fixture.exitCode === null && fixture.signalCode === null) {
    const ended = once(fixture,'exit');
    const stopWait = new AbortController();
    fixture.kill('SIGTERM'); await Promise.race([ended,delay(10000,undefined,{signal:stopWait.signal})]);
    stopWait.abort();
    if (fixture.exitCode === null && fixture.signalCode === null) {
      const forceWait = new AbortController();
      fixture.kill('SIGKILL'); await Promise.race([ended,delay(3000,undefined,{signal:forceWait.signal})]);
      forceWait.abort();
    }
  }
  report.fixture_stopped = fixture.exitCode !== null || fixture.signalCode !== null;
  report.old_clova_posts = chunkRequests.filter(row => row.old).length;
  report.page_errors = pageErrors;
  report.route_errors = routeErrors;
  await writeFile(join(artifacts,'fixture.log'),fixtureLog,{mode:0o600});
  await writeFile(join(artifacts,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
  process.stdout.write(`${JSON.stringify({status:report.status,phase:report.phase,fixture_stopped:report.fixture_stopped,
    report:join(artifacts,'report.json')})}\n`);
}
