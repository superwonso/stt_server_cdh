#!/usr/bin/env node
/** Real browser QA; every account, recording and usage row is synthetic.
 * node scripts/validate_usage_browser.mjs --sandbox /tmp/stt-browser-check.EXAMPLE
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname,join} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const index=process.argv.indexOf('--sandbox');
assert.ok(index>0&&process.argv[index+1]);
const sandbox=resolve(process.argv[index+1]);
assert.equal(dirname(sandbox),'/tmp'); assert.ok(sandbox.split('/').at(-1).startsWith('stt-browser-check.'));
const runDirectory=join(sandbox,`usage-${Date.now()}`),artifacts=join(sandbox,`usage-artifacts-${Date.now()}`);
await mkdir(artifacts,{mode:0o700});
process.env.PLAYWRIGHT_BROWSERS_PATH=join(sandbox,'browsers');
const {chromium}=await import(pathToFileURL(join(sandbox,'node_modules/playwright/index.mjs')));
const report={synthetic_only:true,checks:[],limitations:[
  'Synthetic retained metadata, no production users, external models, Drive or billing calls.',
  'Desktop headless Chromium on loopback, not external authenticated access, school Wi-Fi or a long class.',
]};
let phase='startup',browser,metadata,log='',holdPeriod=null,held=false,releaseHeld=null,failUsage=false;
const requests=[],errors=[],blocked=[];
const note=name=>{report.checks.push(name);process.stdout.write(`${name}: PASS\n`);};
async function poll(predicate,label,timeout=20000){
  const until=Date.now()+timeout;
  while(Date.now()<until){try{if(await predicate())return;}catch{} await delay(100);}
  throw new Error(label);
}
const fixture=spawn(join(root,'.venv/bin/python'),[join(root,'scripts/browser_fixture.py'),
  '--directory',runDirectory,'--admin','--usage-seed'],
  {cwd:root,env:{PATH:'/usr/bin:/bin',PYTHONUNBUFFERED:'1'},stdio:['ignore','pipe','pipe']});
fixture.stdout.on('data',data=>{log+=data.toString();});fixture.stderr.on('data',data=>{log+=data.toString();});
const running=()=>fixture.exitCode===null&&fixture.signalCode===null;
try{
  await poll(async()=>{
    if(!running())throw new Error('fixture exited');
    metadata=JSON.parse(await readFile(join(runDirectory,'fixture.json'),'utf8'));
    assert.notEqual(new URL(metadata.api_origin).port,'8765');
    return(await fetch(metadata.api_origin+'/health')).ok;
  },'fixture startup');
  browser=await chromium.launch({headless:true,env:{...process.env,
    LD_LIBRARY_PATH:join(sandbox,'libraries/usr/lib/x86_64-linux-gnu')},
    args:['--disable-background-networking','--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${metadata.fake_audio}`]});
  report.browser=browser.version();
  const context=await browser.newContext({permissions:['microphone'],viewport:{width:1365,height:1000}});
  const permitted=new Set([metadata.api_origin,metadata.site_origin]);
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(!permitted.has(url.origin)){blocked.push(url.origin);await route.abort();return;}
    requests.push({path:url.pathname,method:request.method(),period:url.searchParams.get('period')});
    if(url.pathname==='/admin/usage'&&request.method()==='GET'){
      if(failUsage){await route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Synthetic unavailable"}',
        headers:{'Access-Control-Allow-Origin':metadata.site_origin}});return;}
      if(holdPeriod===url.searchParams.get('period')){
        holdPeriod=null; held=true;
        await new Promise(resolve=>{releaseHeld=resolve;});
        try{await route.continue();}catch{} return;
      }
    }
    await route.continue();
  });
  await context.addInitScript(({site,api})=>{if(location.origin===site)localStorage.setItem('yeobaek-server',api);},
    {site:metadata.site_origin,api:metadata.api_origin});
  async function login(username){
    const page=await context.newPage();page.on('pageerror',()=>errors.push('pageerror'));
    await page.goto(metadata.site_origin);await poll(()=>page.locator('#login-button').isEnabled(),'login ready');
    await page.locator('#username').fill(username);await page.locator('#password').fill(metadata.password);
    await page.locator('#login-button').click();
    await poll(async()=>await page.locator('#current-user').textContent()===username,'login');return page;
  }
  const admin=await login(metadata.accounts[0]);
  const token=await admin.evaluate(()=>JSON.parse(sessionStorage.getItem('yeobaek-auth-session-v1')).token);
  async function getUsage(period,credential=token){return fetch(metadata.api_origin+'/admin/usage?period='+period,
    {headers:{Authorization:'Bearer '+credential,Origin:metadata.site_origin}});}
  phase='source totals, per-user reconciliation and bounded fields';
  const data=await(await getUsage('month')).json();
  assert.equal(data.period,'month');assert.equal(data.totals.lectures.total,metadata.usage_expected.month_lectures);
  assert.equal(data.totals.recording.known_seconds,metadata.usage_expected.month_seconds);
  assert.equal(data.totals.lectures.trashed,1);assert.equal(data.totals.ai.summary.completed,1);
  assert.equal(data.billing.available,false);
  assert.equal(data.accounts.reduce((n,row)=>n+row.lectures.total,0),data.totals.lectures.total);
  assert.equal(data.accounts.reduce((n,row)=>n+row.recording.known_seconds,0),data.totals.recording.known_seconds);
  for(const forbidden of ['Synthetic usage fixture','synthetic-file-','source_sha256','password_hash','summary_json'])
    assert.ok(!JSON.stringify(data).includes(forbidden));
  await admin.locator('#admin-open').click();
  const total=()=>admin.locator('#admin-usage-lectures').textContent();
  await poll(async()=>String(await total()).includes(String(metadata.usage_expected.month_lectures)),'monthly UI total');
  assert.ok((await admin.locator('#admin-usage-accounts').textContent()).includes(metadata.accounts[0]));
  assert.ok((await admin.locator('#admin-usage-accounts').textContent()).includes(metadata.accounts[1]));
  assert.ok((await admin.locator('#admin-usage-ai').textContent()).includes('1'));
  note(phase);

  phase='KST period filters, unknown duration and stale-response isolation';
  await admin.locator('#admin-usage-period').selectOption('all');
  await poll(async()=>String(await total()).includes('4'),'all UI total');
  const all=await(await getUsage('all')).json();
  assert.equal(all.totals.recording.unknown_lectures,1);
  await admin.locator('#admin-usage-period').selectOption('month');
  await poll(async()=>String(await total()).includes('3'),'month UI total');
  holdPeriod='all';held=false;
  await admin.locator('#admin-usage-period').selectOption('all');
  await poll(()=>held,'held stale period');
  await admin.locator('#admin-usage-period').selectOption('today');
  await poll(async()=>String(await total()).includes(String(metadata.usage_expected.today_lectures)),'latest today UI');
  releaseHeld();releaseHeld=null;await delay(500);
  assert.ok(String(await total()).includes(String(metadata.usage_expected.today_lectures)));
  note(phase);

  phase='recoverable read error, narrow layout and recovery-dialog coexistence';
  failUsage=true;await admin.locator('#admin-usage-refresh').click();
  await poll(()=>admin.locator('#admin-usage-error').isVisible(),'usage error');
  assert.equal(await admin.locator('#admin-dialog').isVisible(),true);
  failUsage=false;await admin.locator('#admin-usage-refresh').click();
  await poll(async()=>!(await admin.locator('#admin-usage-error').isVisible()),'usage recovery');
  await admin.setViewportSize({width:390,height:844});
  await admin.locator('#admin-usage-title').scrollIntoViewIfNeeded();
  assert.equal(await admin.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await admin.locator('#admin-dialog').evaluate(element=>element.scrollWidth>element.clientWidth+1),false);
  await admin.screenshot({path:join(artifacts,'usage-mobile.png')});
  await admin.locator('.admin-usage-account').first().scrollIntoViewIfNeeded();
  await admin.screenshot({path:join(artifacts,'usage-account-mobile.png')});
  await admin.locator('.admin-account-recovery').click();
  assert.equal(await admin.locator('#admin-recovery-dialog').isVisible(),true);
  await admin.locator('#admin-recovery-close').click();await admin.locator('#admin-close').click();
  note(phase);

  phase='usage reads preserve a separate live AudioWorklet capture';
  await admin.setViewportSize({width:1365,height:1000});
  await admin.locator('#lecture-title').fill('Synthetic usage capture');
  await admin.locator('#record-button').click();
  await poll(async()=>(await admin.locator('#record-state').textContent()).includes('듣고'),'capture');
  await admin.locator('#admin-open').click();
  await admin.locator('#admin-usage-refresh').click();await delay(2500);
  assert.ok((await admin.locator('#record-state').textContent()).includes('듣고'));
  await admin.locator('#admin-close').click();await admin.locator('#record-button').click();
  await poll(async()=>{
    const state=await fetch(metadata.api_origin+'/__validation__/state').then(r=>r.json());
    return state.lectures.length===5&&state.lectures.every(row=>row.recording_finalized);
  },'final WAV',45000);
  const state=await fetch(metadata.api_origin+'/__validation__/state').then(r=>r.json());
  assert.ok(state.worklet_loads>0);assert.equal(state.chunks.filter(row=>row.final_chunk).length,1);
  note(phase);

  phase='ordinary account cannot read usage; logout clears administrator data';
  const student=await login(metadata.accounts[1]);
  const studentToken=await student.evaluate(()=>JSON.parse(sessionStorage.getItem('yeobaek-auth-session-v1')).token);
  assert.equal((await getUsage('all',studentToken)).status,403);
  assert.equal((await fetch(metadata.api_origin+'/admin/usage')).status,401);
  assert.equal(await student.locator('#admin-open').isVisible(),false);
  await poll(()=>admin.locator('#logout').isEnabled(),'idle logout');await admin.locator('#logout').click();
  await poll(()=>admin.locator('#auth-screen').isVisible(),'logged out');
  assert.equal(await admin.locator('#admin-usage-accounts').textContent(),'');
  assert.equal(requests.filter(row=>row.path==='/admin/usage'&&row.method!=='GET'&&row.method!=='OPTIONS').length,0);
  assert.equal(errors.length,0);assert.equal(blocked.length,0);note(phase);
}catch(error){
  report.failed_phase=phase;process.exitCode=1;
  await writeFile(join(artifacts,'failure-private.log'),String(error.stack||error),{mode:0o600});
  process.stderr.write(`Usage browser validation failed during: ${phase}\n`);
}finally{
  releaseHeld?.();await browser?.close();if(running())fixture.kill('SIGTERM');
  try{await poll(()=>!running(),'fixture shutdown',10000);}catch{if(running())fixture.kill('SIGKILL');}
  await poll(()=>!running(),'fixture exit',5000);
  if(metadata)for(const origin of [metadata.api_origin,metadata.site_origin]){
    let alive=false;try{alive=(await fetch(origin,{signal:AbortSignal.timeout(1500)})).status>0;}catch{}
    assert.equal(alive,false,'temporary port must close');
  }
  report.fixture_closed=!running();await writeFile(join(artifacts,'fixture.log'),log,{mode:0o600});
  await writeFile(join(artifacts,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
  process.stdout.write(`Private artifacts: ${artifacts}\n`);
}
