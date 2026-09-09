#!/usr/bin/env node
/** Real Chromium local-audio rescue QA using synthetic loopback fixtures only.
 * node scripts/validate_local_audio_browser.mjs --sandbox /tmp/stt-browser-check.EXAMPLE
 * Requires the existing sandbox's Playwright, Chromium and extracted libraries.
 * Never opens production storage, credentials, audio, ASR, or cloud endpoints.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createHash} from 'node:crypto';
import {readFile, writeFile, mkdir, chmod} from 'node:fs/promises';
import {resolve, dirname, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const index = process.argv.indexOf('--sandbox');
assert.ok(index >= 0 && process.argv[index + 1], 'Supply an explicit isolated --sandbox');
const sandbox = resolve(process.argv[index + 1]);
assert.equal(dirname(sandbox), '/tmp');
assert.ok(sandbox.split('/').at(-1).startsWith('stt-browser-check.'));
const stamp = Date.now();
const runDirectory = join(sandbox, `local-audio-${stamp}`);
const artifacts = join(sandbox, `local-audio-artifacts-${stamp}`);
await mkdir(artifacts, {mode:0o700});
process.env.PLAYWRIGHT_BROWSERS_PATH = join(sandbox, 'browsers');
const {chromium} = await import(pathToFileURL(join(sandbox, 'node_modules/playwright/index.mjs')));
const report = {synthetic_only:true, checks:[], limitations:[
  'Desktop Chromium, fake microphone and loopback fake ASR only; no production account or audio.',
  'An injected HTTP 422 and withheld PCM messages are deterministic faults, not a real school network or OS device fault.',
  'Actual downloaded WAV bytes are checked; this is not a Safari, tablet, full-disk or long-class endurance test.',
]};
let browser, metadata, mainPage, rescuePage, phase = 'startup', fixtureLog = '';
const pageErrors = [], blockedOrigins = new Set(), requests = [], chunkRequests = [];
const pageNames = new Map();
const note = (name, details = {}) => {
  report.checks.push({name, ...details});
  process.stdout.write(`${JSON.stringify({check:name, ...details})}\n`);
};
async function poll(predicate, label, timeout = 25000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try { if (await predicate()) return; } catch {}
    await delay(100);
  }
  throw new Error(`Timed out: ${label}`);
}
const fixture = spawn(join(root, '.venv/bin/python'),
  [join(root, 'scripts/browser_fixture.py'), '--directory', runDirectory],
  {cwd:root, env:{PATH:'/usr/bin:/bin',PYTHONUNBUFFERED:'1'}, stdio:['ignore','pipe','pipe']});
fixture.stdout.on('data', data => { fixtureLog += data.toString(); });
fixture.stderr.on('data', data => { fixtureLog += data.toString(); });

async function queueState(page, owner) {
  return page.evaluate(owner => new Promise((resolve, reject) => {
    const request = indexedDB.open('yeobaek-live-audio');
    request.onerror = () => reject(new Error('Synthetic queue read failed'));
    request.onsuccess = () => {
      const db = request.result, names = Array.from(db.objectStoreNames);
      const transaction = db.transaction(names, 'readonly');
      const reads = Object.fromEntries(names.map(name => [name,transaction.objectStore(name).getAll()]));
      transaction.oncomplete = async () => {
        try {
          const result = {};
          for (const name of names) {
            result[name] = [];
            for (const row of reads[name].result.filter(row => row.owner === owner)) {
              const value = {...row};
              if (row.blob) {
                value.blob = {size:row.blob.size,sha256:Array.from(new Uint8Array(
                  await crypto.subtle.digest('SHA-256',await row.blob.arrayBuffer())), value => value.toString(16).padStart(2,'0')).join('')};
              }
              result[name].push(value);
            }
            result[name].sort((a,b) => String(a.id || a.captureId).localeCompare(String(b.id || b.captureId)));
          }
          resolve(result);
        } catch { reject(new Error('Synthetic queue digest failed')); }
        finally { db.close(); }
      };
      transaction.onerror = () => { db.close(); reject(new Error('Synthetic queue transaction failed')); };
    };
  }), owner);
}
function assertOldChunksRetained(before, after) {
  for (const old of before.chunks) {
    assert.deepEqual(after.chunks.find(row => row.id === old.id), old, 'Export must not mutate, ACK, mark skipped, or remove a queued WAV');
  }
}
async function downloadWav(page, link, filename) {
  const event = page.waitForEvent('download', {timeout:15000});
  await link.click();
  const download = await event;
  assert.equal(await download.failure(), null, 'Browser accepted the actual download');
  assert.match(download.suggestedFilename(), /\.wav$/i);
  const path = join(artifacts, filename);
  await download.saveAs(path);
  await chmod(path, 0o600);
  const bytes = await readFile(path);
  assert.ok(bytes.length >= 46, 'Actual saved file contains PCM, not zero bytes or a header alone');
  assert.equal(bytes.toString('ascii',0,4), 'RIFF');
  assert.equal(bytes.toString('ascii',8,12), 'WAVE');
  assert.equal(bytes.toString('ascii',36,40), 'data');
  assert.equal(bytes.readUInt32LE(4), bytes.length - 8);
  assert.equal(bytes.readUInt32LE(40), bytes.length - 44);
  assert.equal(bytes.readUInt16LE(20), 1);
  assert.equal(bytes.readUInt16LE(22), 1);
  assert.equal(bytes.readUInt32LE(24), 16000);
  assert.equal(bytes.readUInt16LE(34), 16);
  let peak = 0;
  for (let offset = 44; offset < bytes.length; offset += 2) peak = Math.max(peak,Math.abs(bytes.readInt16LE(offset)));
  return {bytes:bytes.length,samples:(bytes.length-44)/2,seconds:(bytes.length-44)/32000,peak,
    sha256:createHash('sha256').update(bytes).digest('hex'),buffer:bytes};
}

try {
  await poll(async () => {
    if (fixture.exitCode !== null) throw new Error('Synthetic fixture stopped');
    try { metadata = JSON.parse(await readFile(join(runDirectory,'fixture.json'),'utf8')); }
    catch { return false; }
    for (const value of [metadata.api_origin, metadata.site_origin]) {
      const url = new URL(value);
      assert.equal(url.hostname, '127.0.0.1');
      assert.notEqual(url.port, '8765', 'Never use the production API port');
    }
    assert.equal(dirname(metadata.fake_audio), runDirectory);
    return (await fetch(`${metadata.api_origin}/health`)).ok;
  }, 'isolated fixture startup');
  const permitted = new Set([metadata.api_origin, metadata.site_origin]);
  browser = await chromium.launch({headless:true, env:{...process.env,
    LD_LIBRARY_PATH:join(sandbox,'libraries/usr/lib/x86_64-linux-gnu')},
    args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${metadata.fake_audio}`,'--disable-background-networking']});
  report.browser = browser.version();
  const context = await browser.newContext({permissions:['microphone'],acceptDownloads:true,viewport:{width:1365,height:1000}});
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (!permitted.has(url.origin)) { blockedOrigins.add(url.origin); await route.abort('blockedbyclient'); return; }
    let name = 'unknown';
    try { name = pageNames.get(request.frame().page()) || name; } catch {}
    requests.push({page:name,method:request.method(),path:url.pathname,api:url.origin === metadata.api_origin});
    if (url.origin === metadata.site_origin && url.pathname === '/config.json') {
      const publishedAt = new Date().toISOString().replace(/\.\d{3}Z$/,'Z');
      const expiresAt = new Date(Date.parse(publishedAt)+86400000).toISOString().replace(/\.\d{3}Z$/,'Z');
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({version:1,state:'online',apiUrl:metadata.api_origin,publishedAt,expiresAt})});
      return;
    }
    if (url.origin === metadata.api_origin && request.method() === 'POST' && /\/chunks$/.test(url.pathname)) {
      chunkRequests.push({page:name,id:request.headers()['x-chunk-id']});
      await route.fulfill({status:422,contentType:'application/json',headers:{
        'Access-Control-Allow-Origin':metadata.site_origin,'Access-Control-Allow-Credentials':'true',
      },body:JSON.stringify({detail:'Synthetic rejected first audio; retained for local rescue'})});
      return;
    }
    await route.continue();
  });
  await context.addInitScript(({site,api}) => {
    if (location.origin !== site) return;
    localStorage.setItem('yeobaek-server',api);
    window.__localAudioBrowser = {suppressPCM:false,nodes:[],streams:[],pcm:[]};
    const getUserMedia = navigator.mediaDevices?.getUserMedia;
    if (getUserMedia) navigator.mediaDevices.getUserMedia = async function(...args) {
      const stream = await getUserMedia.apply(this,args);
      window.__localAudioBrowser.streams.push(stream);
      return stream;
    };
    const Worklet = window.AudioWorkletNode;
    if (Worklet) window.AudioWorkletNode = class extends Worklet {
      constructor(...args) {
        super(...args);
        window.__localAudioBrowser.nodes.push(this);
        const pcm = {blocks:0,lastAt:null}; window.__localAudioBrowser.pcm.push(pcm);
        this.port.addEventListener('message', event => {
          if (window.__localAudioBrowser.suppressPCM && event.data?.type === 'samples') event.stopImmediatePropagation();
          else if (event.data?.type === 'samples') { pcm.blocks += 1; pcm.lastAt = Date.now(); }
        });
      }
    };
  }, {site:metadata.site_origin,api:metadata.api_origin});
  async function pageNamed(name) {
    const page = await context.newPage(); pageNames.set(page,name);
    page.on('pageerror', () => pageErrors.push(name)); return page;
  }
  async function loginMain(page, account) {
    await page.goto(metadata.site_origin);
    await poll(() => page.locator('#login-button').isEnabled(), 'main login ready');
    await page.locator('#username').fill(metadata.accounts[account]);
    await page.locator('#password').fill(metadata.password);
    await page.locator('#login-button').click();
    await poll(async () => await page.locator('#current-user').textContent() === metadata.accounts[account], 'main login');
  }
  async function loginRescue(page, account) {
    await page.goto(`${metadata.site_origin}/rescue.html`);
    await poll(() => page.locator('#rescue-login').isEnabled(), 'rescue trusted connection');
    await page.locator('#rescue-username').fill(metadata.accounts[account]);
    await page.locator('#rescue-password').fill(metadata.password);
    await page.locator('#rescue-login').click();
    await poll(() => page.locator('#rescue-results').isVisible(), 'rescue authenticated results');
    await poll(() => page.locator('#rescue-files a[download]').count().then(count => count > 0), 'rescue WAV links');
  }
  const main = await pageNamed('main');
  mainPage = main;
  await loginMain(main,0);
  await main.locator('#asr-provider').selectOption('qwen');
  await main.locator('#lecture-title').fill('Synthetic blocked live rescue');
  await main.locator('#record-button').click();
  phase = 'HTTP 422 capture continues across two audio boundaries';
  await poll(async () => (await queueState(main,metadata.accounts[0])).chunks.length >= 2,
    'two durable WAV chunks after rejected upload',35000);
  assert.equal(chunkRequests.length,1,'Rejected chunk is not automatically billed or retried');
  assert.ok((await main.locator('#record-state').textContent()).includes('듣고'));
  assert.equal(await main.locator('#capture-input-status').getAttribute('data-state'),'receiving');
  assert.equal(await main.locator('#save-failed').isVisible(),true);
  assert.equal(await main.locator('#pause-button').isEnabled(),true);
  note(phase,{queued:(await queueState(main,metadata.accounts[0])).chunks.length,chunkPosts:chunkRequests.length});

  phase = 'live local export downloads actual WAV without changing blocked queue or pausing';
  const beforeLive = await queueState(main,metadata.accounts[0]);
  await main.locator('#local-audio-open').click();
  await poll(() => main.locator('#local-audio-files a[download]').count().then(count => count === 1),'live combined WAV');
  const live = await downloadWav(main,main.locator('#local-audio-files a[download]').first(),'live-remaining.wav');
  assert.ok(live.seconds >= 16,'Both blocked chunks and the latest accepted tail are included');
  assertOldChunksRetained(beforeLive,await queueState(main,metadata.accounts[0]));
  assert.equal(chunkRequests.length,1);
  assert.ok((await main.locator('#record-state').textContent()).includes('듣고'));
  assert.equal(await main.locator('#pause-button').getAttribute('aria-pressed'),'false');
  await main.locator('#local-audio-close').click();
  note(phase,{bytes:live.bytes,seconds:live.seconds,peak:live.peak});

  phase = 'same-origin rescue tab reads while original microphone remains active';
  const beforeRescue = await queueState(main,metadata.accounts[0]);
  const rescue = await pageNamed('rescue');
  rescuePage = rescue;
  await loginRescue(rescue,0);
  const rescued = await downloadWav(rescue,rescue.locator('#rescue-files a[download]').first(),'separate-tab-remaining.wav');
  assert.ok(rescued.seconds >= 16);
  assertOldChunksRetained(beforeRescue,await queueState(main,metadata.accounts[0]));
  assert.equal(chunkRequests.length,1);
  assert.ok((await main.locator('#record-state').textContent()).includes('듣고'));
  assert.equal(await rescue.evaluate(() => window.__localAudioBrowser.nodes.length),0,'Rescue cannot acquire audio');
  assert.ok(!requests.some(row => row.page === 'rescue' && row.path === '/app.js'),'Rescue does not import the main uploader');
  assert.ok(requests.filter(row => row.page === 'rescue' && row.api).every(row =>
    (row.method === 'GET' && ['/health','/auth/me'].includes(row.path)) || (row.method === 'POST' && row.path === '/auth/login')));
  await rescue.setViewportSize({width:390,height:844});
  assert.equal(await rescue.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  assert.equal(await rescue.locator('.rescue-limits').isVisible(),true,'Critical RAM-only recovery limitations must remain visible on mobile');
  assert.match(await rescue.locator('.rescue-limits').textContent(),/RAM|메모리/);
  assert.match(await rescue.locator('.rescue-limits').textContent(),/전체 수업/);
  await rescue.screenshot({path:join(artifacts,'rescue-mobile.png')});
  await rescue.locator('#rescue-lock').click();
  assert.equal(await rescue.locator('#rescue-files a[download]').count(),0);
  assert.equal(await main.locator('#current-user').textContent(),metadata.accounts[0]);
  note(phase,{bytes:rescued.bytes,seconds:rescued.seconds});

  phase = 'missing PCM is visible and manual recovery remains usable during blocked upload';
  await main.evaluate(() => { window.__localAudioBrowser.suppressPCM = true; });
  await poll(async () => await main.locator('#capture-input-status').getAttribute('data-state') === 'unavailable','PCM liveness warning',10000);
  assert.equal(await main.locator('#save-failed').isVisible(),true);
  assert.equal(await main.locator('#pause-button').isEnabled(),true);
  await main.locator('#pause-button').click();
  await poll(async () => (await main.locator('#pause-button').textContent()).includes('오디오 다시 연결'),'manual reconnect ready');
  await main.evaluate(() => { window.__localAudioBrowser.suppressPCM = false; });
  await main.locator('#pause-button').click();
  await poll(async () => await main.locator('#capture-input-status').getAttribute('data-state') === 'receiving','real PCM received after manual reconnect');
  assert.equal(await main.evaluate(() => window.__localAudioBrowser.nodes.length),2);
  assert.equal(chunkRequests.length,1);
  assertOldChunksRetained(beforeLive,await queueState(main,metadata.accounts[0]));
  note(phase);

  phase = 'synthetic silence then tone combines overlap and snapshot exactly without any queue write';
  const seeded = await rescue.evaluate(async owner => {
    const {DurableLiveQueue} = await import('./live-queue.js');
    const {encodeWav} = await import('./audio.js');
    const queue = new DurableLiveQueue(), captureId = crypto.randomUUID();
    const pcm = Float32Array.from({length:22*16000},(_,index) => index < 8*16000 ? 0 : Math.sin(2*Math.PI*440*index/16000)*0.2);
    try {
      await queue.createSession({id:captureId,owner,title:'Synthetic silence then tone',language:'ko',source:'microphone',asrProvider:'qwen'});
      await queue.enqueueChunk(owner,captureId,{id:crypto.randomUUID(),startSamples:0,durationSamples:8*16000,overlapSamples:0,final:false,blob:encodeWav(pcm.slice(0,8*16000))});
      await queue.enqueueChunk(owner,captureId,{id:crypto.randomUUID(),startSamples:5*16000,durationSamples:15*16000,overlapSamples:3*16000,final:false,blob:encodeWav(pcm.slice(5*16000,20*16000))});
      await queue.saveSnapshot(owner,captureId,{sequence:2,startSamples:17*16000,durationSamples:5*16000,overlapSamples:3*16000,blob:encodeWav(pcm.slice(17*16000))});
      const hash = await crypto.subtle.digest('SHA-256',await encodeWav(pcm).arrayBuffer());
      return {samples:pcm.length,sha256:Array.from(new Uint8Array(hash),value => value.toString(16).padStart(2,'0')).join('')};
    } finally { queue.close(); }
  },metadata.accounts[1]);
  const beforePattern = await queueState(rescue,metadata.accounts[1]);
  await loginRescue(rescue,1);
  assert.equal(await rescue.locator('#rescue-files a[download]').count(),1,'Other account live recording is not exposed');
  const patterned = await downloadWav(rescue,rescue.locator('#rescue-files a[download]').first(),'patterned-remaining.wav');
  assert.equal(patterned.samples,seeded.samples);
  assert.equal(patterned.sha256,seeded.sha256,'Every real PCM sample occurs once, with no overlap duplication or omitted tail');
  assert.equal(patterned.buffer.subarray(44,44+8*16000*2).every(value => value === 0),true);
  assert.ok(patterned.peak > 5000,'Later audible content is included after the first silent WAV');
  assert.deepEqual(await queueState(rescue,metadata.accounts[1]),beforePattern,'Rescue is entirely readonly for idle stored audio');
  await rescue.locator('#rescue-scan').click();
  await poll(() => rescue.locator('#rescue-files a[download]').count().then(count => count === 1),'rescan');
  assert.deepEqual(await queueState(rescue,metadata.accounts[1]),beforePattern);
  assert.equal(chunkRequests.length,1);
  assert.ok((await main.locator('#record-state').textContent()).includes('듣고'));
  note(phase,{bytes:patterned.bytes,seconds:patterned.seconds,peak:patterned.peak});

  phase = 'final isolation and cleanup';
  const server = await fetch(`${metadata.api_origin}/__validation__/state`).then(response => response.json());
  assert.equal(server.asr_calls.length,0,'The blocked synthetic upload and local rescue never invoke ASR');
  assert.equal(server.chunks.length,0);
  assert.deepEqual(pageErrors,[]);
  assert.deepEqual([...blockedOrigins],[]);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.phase = phase;
  report.error = error.message; report.pageErrors = pageErrors;
  report.ui = {
    rescueError:await rescuePage?.locator('#rescue-error').textContent().catch(() => ''),
    rescueStatus:await rescuePage?.locator('#rescue-status').textContent().catch(() => ''),
    localExportStatus:await mainPage?.locator('#local-audio-status').textContent().catch(() => ''),
    captureStatus:await mainPage?.locator('#capture-input-status').textContent().catch(() => ''),
    recordState:await mainPage?.locator('#record-state').textContent().catch(() => ''),
    microphone:await mainPage?.evaluate(() => ({visible:document.visibilityState,
      suppressed:window.__localAudioBrowser.suppressPCM,
      contexts:window.__localAudioBrowser.nodes.map(node => node.context.state),
      tracks:window.__localAudioBrowser.streams.map(stream => stream.getAudioTracks().map(track => ({state:track.readyState,muted:track.muted}))),
      pcm:window.__localAudioBrowser.pcm.map(row => ({blocks:row.blocks,ageMs:row.lastAt === null ? null : Date.now()-row.lastAt})),
    })).catch(() => null),
  };
  process.stdout.write(`${JSON.stringify({status:report.status,phase:report.phase,error:report.error,ui:report.ui,pageErrors})}\n`);
  process.exitCode = 1;
} finally {
  await browser?.close().catch(() => {});
  if (fixture.exitCode === null && fixture.signalCode === null) {
    const stopWait = new AbortController();
    const exited = once(fixture,'exit');
    fixture.kill('SIGTERM');
    await Promise.race([exited,delay(10000,undefined,{signal:stopWait.signal})]);
    stopWait.abort();
    if (fixture.exitCode === null && fixture.signalCode === null) {
      const forceWait = new AbortController();
      fixture.kill('SIGKILL');
      await Promise.race([exited,delay(3000,undefined,{signal:forceWait.signal})]);
      forceWait.abort();
    }
  }
  report.fixture_stopped = fixture.exitCode !== null || fixture.signalCode !== null;
  await writeFile(join(artifacts,'fixture.log'),fixtureLog,{mode:0o600});
  await writeFile(join(artifacts,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
  process.stdout.write(`${JSON.stringify({status:report.status,phase:report.phase,error:report.error,
    fixture_stopped:report.fixture_stopped,report:join(artifacts,'report.json')})}\n`);
}
