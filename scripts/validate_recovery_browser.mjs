#!/usr/bin/env node
/** Administrator recovery QA against two synthetic accounts, never deployment settings.
 * node scripts/validate_recovery_browser.mjs --sandbox /tmp/stt-browser-check.EXAMPLE
 * Requires Playwright/Chromium installed in that dedicated sandbox.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {resolve, dirname, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const argument = process.argv.indexOf('--sandbox');
assert.ok(argument > 0 && process.argv[argument + 1], 'An explicit sandbox is required');
const sandbox = resolve(process.argv[argument + 1]);
assert.equal(dirname(sandbox), '/tmp');
assert.ok(sandbox.split('/').at(-1).startsWith('stt-browser-check.'));
const runDirectory = join(sandbox, `recovery-${Date.now()}`);
const artifacts = join(sandbox, `recovery-artifacts-${Date.now()}`);
await mkdir(artifacts, {mode:0o700});
process.env.PLAYWRIGHT_BROWSERS_PATH = join(sandbox, 'browsers');
const {chromium} = await import(pathToFileURL(join(sandbox, 'node_modules/playwright/index.mjs')));
const report = {synthetic_only:true, checks:[], limitations:[
  'Loopback Chromium, synthetic users and audio only; no production credentials, ASR, LLM or deployment.',
  'The generated public-site link is checked, then its fragment is exercised on the isolated local website.',
  'Not a physical device, external delivery, school network or long-class test.',
]};
let phase = 'startup', metadata, browser, fixtureLog = '';
const failures = [], blocked = [], requests = [];
const note = name => { report.checks.push(name); process.stdout.write(`${name}: PASS\n`); };
async function poll(predicate, label, timeout = 20000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    try { if (await predicate()) return; } catch {}
    await delay(100);
  }
  throw new Error(label);
}
const fixture = spawn(join(root, '.venv/bin/python'),
  [join(root, 'scripts/browser_fixture.py'), '--directory', runDirectory, '--admin'],
  {cwd:root, env:{PATH:'/usr/bin:/bin',PYTHONUNBUFFERED:'1'}, stdio:['ignore','pipe','pipe']});
fixture.stdout.on('data', data => { fixtureLog += data.toString(); });
fixture.stderr.on('data', data => { fixtureLog += data.toString(); });
const stillRunning = () => fixture.exitCode === null && fixture.signalCode === null;
async function api(path, token, body) {
  const headers = {Origin:metadata.site_origin};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body) headers['Content-Type'] = 'application/json';
  return fetch(`${metadata.api_origin}${path}`, {method:body ? 'POST' : 'GET', headers,
    ...(body ? {body:JSON.stringify(body)} : {})});
}
async function storageHas(page, value) {
  return page.evaluate(value => JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)]).includes(value), value);
}
try {
  await poll(async () => {
    if (!stillRunning()) throw new Error('fixture startup failed');
    try { metadata = JSON.parse(await readFile(join(runDirectory,'fixture.json'),'utf8')); }
    catch { return false; }
    assert.notEqual(new URL(metadata.api_origin).port, '8765');
    return (await fetch(`${metadata.api_origin}/health`)).ok;
  }, 'fixture startup');
  browser = await chromium.launch({headless:true, env:{...process.env,
    LD_LIBRARY_PATH:join(sandbox,'libraries/usr/lib/x86_64-linux-gnu'),
  },args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${metadata.fake_audio}`, '--disable-background-networking']});
  report.browser = browser.version();
  const context = await browser.newContext({permissions:['microphone'],viewport:{width:1365,height:1000}});
  const permitted = new Set([metadata.api_origin,metadata.site_origin]);
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (!permitted.has(url.origin)) { blocked.push(url.origin); await route.abort(); return; }
    requests.push({method:request.method(),path:url.pathname});
    await route.continue();
  });
  await context.addInitScript(({site,api}) => {
    if (location.origin === site) localStorage.setItem('yeobaek-server', api);
  }, {site:metadata.site_origin,api:metadata.api_origin});
  async function newPage() {
    const page = await context.newPage();
    page.on('pageerror', () => failures.push('pageerror'));
    return page;
  }
  async function login(page, username, password) {
    await page.goto(metadata.site_origin);
    await poll(() => page.locator('#login-button').isEnabled(), 'login ready');
    await page.locator('#username').fill(username); await page.locator('#password').fill(password);
    await page.locator('#login-button').click();
    await poll(async () => await page.locator('#current-user').textContent() === username, 'login');
  }
  const admin = await newPage(), student = await newPage();
  phase = 'admin authorization and current password';
  await login(admin, metadata.accounts[0], metadata.password);
  await login(student, metadata.accounts[1], metadata.password);
  const studentToken = await student.evaluate(() => JSON.parse(sessionStorage.getItem('yeobaek-auth-session-v1')).token);
  assert.equal(await student.locator('#admin-open').isVisible(), false);
  await admin.locator('#lecture-title').fill('Synthetic account recovery capture');
  await admin.locator('#record-button').click();
  await poll(async () => (await admin.locator('#record-state').textContent()).includes('듣고'), 'capture');
  await admin.locator('#admin-open').click();
  await poll(async () => await admin.locator('.admin-account-recovery').count() === 1, 'other activated account');
  await admin.locator('.admin-account-recovery').click();
  await admin.locator('#admin-recovery-password').fill('incorrect-synthetic-password');
  await admin.locator('#admin-recovery-issue').click();
  await poll(() => admin.locator('#admin-recovery-error').isVisible(), 'reauth rejection');
  assert.equal(await admin.locator('#current-user').textContent(), metadata.accounts[0]);
  assert.equal(await admin.locator('#admin-recovery-password').inputValue(), '');
  note(phase);

  phase = 'issue, rotate, revoke and no browser persistence';
  async function issue() {
    await admin.locator('#admin-recovery-password').fill(metadata.password);
    await admin.locator('#admin-recovery-issue').click();
    await poll(() => admin.locator('#admin-recovery-result').isVisible(), 'link result');
    const link = new URL(await admin.locator('#admin-recovery-link').inputValue());
    assert.equal(link.origin + link.pathname, 'https://superwonso.github.io/stt_server_cdh/');
    const code = new URLSearchParams(link.hash.slice(1)).get('reset_code');
    assert.ok(code && code.length === 43);
    assert.equal(await storageHas(admin, code), false);
    assert.equal(await storageHas(admin, metadata.password), false);
    assert.equal(await admin.locator('#admin-recovery-password').inputValue(), '');
    return {link,code};
  }
  const first = await issue(), second = await issue();
  assert.notEqual(first.code, second.code);
  const resetBody = code => ({username:metadata.accounts[1],reset_code:code,password:'New-synthetic-42!',password_confirm:'New-synthetic-42!'});
  assert.equal((await api('/auth/reset-password', null, resetBody(first.code))).status, 400);
  assert.equal((await api('/auth/me', studentToken)).status, 200);
  await admin.locator('#admin-recovery-password').fill(metadata.password);
  await admin.locator('#admin-recovery-revoke').click();
  await poll(async () => (await admin.locator('#admin-recovery-status').textContent()).includes('취소했어요'), 'revoke');
  assert.equal((await api('/auth/reset-password', null, resetBody(second.code))).status, 400);
  const third = await issue();
  await admin.setViewportSize({width:390,height:844});
  assert.equal(await admin.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await admin.screenshot({path:join(artifacts,'admin-recovery-mobile.png')});
  await admin.locator('#admin-recovery-close').click();
  assert.equal(await admin.locator('#admin-recovery-link').inputValue(), '');
  await admin.locator('#admin-close').click();
  note(phase);

  phase = 'fragment removal, saved-login suppression and one-use reset';
  const authMeBefore = requests.filter(row => row.path === '/auth/me').length;
  await student.goto(metadata.site_origin + '/' + third.link.hash);
  await poll(() => student.locator('#login-button').isEnabled(), 'reset ready');
  assert.equal(new URL(student.url()).hash, '');
  assert.equal(await student.locator('#username').inputValue(), metadata.accounts[1]);
  assert.equal(await student.locator('#username').getAttribute('readonly'), '');
  assert.equal(await student.locator('#workspace').isVisible(), false);
  assert.equal(await storageHas(student, third.code), false);
  assert.equal(requests.filter(row => row.path === '/auth/me').length, authMeBefore);
  await student.locator('#password').fill('New-synthetic-42!');
  await student.locator('#password-confirm').fill('different-synthetic-password');
  const resetPosts = () => requests.filter(row => row.path === '/auth/reset-password' && row.method === 'POST').length;
  const count = resetPosts();
  await student.locator('#login-button').click();
  await poll(() => student.locator('#auth-error').isVisible(), 'mismatch');
  assert.equal(resetPosts(), count);
  assert.equal(await student.locator('#password').inputValue(), '');
  await student.locator('#password').fill('New-synthetic-42!');
  await student.locator('#password-confirm').fill('New-synthetic-42!');
  await student.locator('#login-button').click();
  await poll(async () => (await student.locator('#login-button').textContent()).includes('로그인'), 'reset complete');
  assert.equal(await student.locator('#workspace').isVisible(), false);
  assert.equal(await student.locator('#password').inputValue(), '');
  assert.equal(await student.evaluate(() => sessionStorage.getItem('yeobaek-auth-session-v1')), null);
  assert.equal((await api('/auth/me', studentToken)).status, 401);
  assert.equal((await api('/auth/reset-password', null, resetBody(third.code))).status, 400);
  assert.equal((await api('/auth/login', null, {username:metadata.accounts[1],password:metadata.password})).status, 401);
  await student.locator('#password').fill('New-synthetic-42!');
  await student.locator('#login-button').click();
  await poll(() => student.locator('#workspace').isVisible(), 'new password login');
  note(phase);

  phase = 'independent live AudioWorklet recording survives recovery';
  assert.ok((await admin.locator('#record-state').textContent()).includes('듣고'));
  await admin.locator('#record-button').click();
  await poll(async () => (await fetch(`${metadata.api_origin}/__validation__/state`).then(r=>r.json()))
    .lectures.some(row=>row.recording_finalized), 'final capture', 45000);
  const state = await fetch(`${metadata.api_origin}/__validation__/state`).then(r=>r.json());
  assert.equal(state.lectures.length, 1);
  assert.ok(state.lectures[0].recording_seconds > 0);
  assert.equal(state.chunks.filter(row=>row.final_chunk).length, 1);
  assert.ok(state.worklet_loads > 0);
  assert.equal(new Set(state.chunks.map(row=>row.chunk_id)).size, state.chunks.length);
  note(phase);
  assert.equal(failures.length, 0); assert.equal(blocked.length, 0);
  note('no script errors or external-origin requests');
} catch (error) {
  // Do not print assertions or browser traces that could contain synthetic
  // credentials/reset links. The phase is sufficient to locate the check.
  report.failed_phase = phase;
  await writeFile(join(artifacts,'failure-private.log'), String(error.stack || error), {mode:0o600});
  process.stderr.write(`Recovery browser validation failed during: ${phase}\n`);
  process.exitCode = 1;
} finally {
  await browser?.close();
  if (stillRunning()) fixture.kill('SIGTERM');
  try { await poll(() => !stillRunning(), 'fixture shutdown', 10000); }
  catch { if (stillRunning()) fixture.kill('SIGKILL'); }
  await poll(() => !stillRunning(), 'fixture exit', 5000);
  if (metadata) {
    for (const origin of [metadata.api_origin,metadata.site_origin]) {
      let alive = false;
      try { alive = (await fetch(origin, {signal:AbortSignal.timeout(1500)})).status > 0; } catch {}
      assert.equal(alive, false, 'isolated fixture port must close');
    }
  }
  report.fixture_closed = !stillRunning();
  // Fixture log may include synthetic identifiers and stays in its private directory.
  await writeFile(join(artifacts,'fixture.log'), fixtureLog, {mode:0o600});
  await writeFile(join(artifacts,'report.json'), JSON.stringify(report,null,2), {mode:0o600});
  process.stdout.write(`Private artifacts: ${artifacts}\n`);
}
