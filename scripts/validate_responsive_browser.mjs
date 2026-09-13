#!/usr/bin/env node
/** Responsive layout QA against frozen HEAD/current HTML+CSS in an isolated browser.
 * node scripts/validate_responsive_browser.mjs --sandbox /tmp/stt-browser-check.EXAMPLE
 * Optional --baseline-only records the historical layout without a current-layout gate.
 * Expanded queue/AI/held states are layout-only synthetic DOM, not service outcomes.
 */
import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve, dirname, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const argument = process.argv.indexOf('--sandbox');
assert.ok(argument >= 0 && process.argv[argument + 1], 'Supply an isolated --sandbox');
const sandbox = resolve(process.argv[argument + 1]);
assert.equal(dirname(sandbox), '/tmp');
assert.ok(sandbox.split('/').at(-1).startsWith('stt-browser-check.'));
const baselineOnly = process.argv.includes('--baseline-only');
const widths = [320,390,520,720,768,900,1024,1280,1440];
const stamp = Date.now();
const runDirectory = join(sandbox, `responsive-${stamp}`);
const artifacts = join(sandbox, `responsive-artifacts-${stamp}`);
await mkdir(artifacts, {mode:0o700});
const snapshots = {};
for (const variant of ['baseline','after']) {
  snapshots[variant] = {};
  for (const name of ['index.html','style.css','rescue.html']) {
    snapshots[variant][name] = variant === 'baseline'
      ? execFileSync('git',['show',`HEAD:web/${name}`],{cwd:root,encoding:'utf8',maxBuffer:2*1024*1024})
      : await readFile(join(root,'web',name),'utf8');
  }
}
process.env.PLAYWRIGHT_BROWSERS_PATH = join(sandbox,'browsers');
const {chromium} = await import(pathToFileURL(join(sandbox,'node_modules/playwright/index.mjs')));
const report = {synthetic_only:true,widths,measurements:[],screenshots:[],functional:[],
  source_hashes:Object.fromEntries(Object.entries(snapshots).map(([variant,files]) => [variant,
    Object.fromEntries(Object.entries(files).map(([name,source]) => [name,createHash('sha256').update(source).digest('hex')]))])),
  limitations:[
    'Queue errors, held cards and AI/review results are layout-only synthetic DOM with main application scripts disabled.',
    'Separate functional checks use actual app login/local-audio/rescue UI with new synthetic accounts and empty temporary storage.',
    'No speech model, external LLM, production account/recording/DB, or public site is used.',
    'Desktop Chromium viewport widths are not physical tablet/Safari, zoom, keyboard or long-class endurance tests.',
  ]};
let fixtureLog = '', browser, metadata, activePage, phase = 'fixture startup';
const blockedOrigins = new Set(), pageErrors = [], requests = [], consoleErrors = [];
const fixture = spawn(join(root,'.venv/bin/python'),[join(root,'scripts/browser_fixture.py'),'--directory',runDirectory],
  {cwd:root,env:{PATH:'/usr/bin:/bin',PYTHONUNBUFFERED:'1'},stdio:['ignore','pipe','pipe']});
fixture.stdout.on('data',data => { fixtureLog += data.toString(); });
fixture.stderr.on('data',data => { fixtureLog += data.toString(); });
async function poll(predicate,label,timeout=25000) {
  const deadline = Date.now()+timeout;
  while (Date.now()<deadline) {
    if (fixture.exitCode !== null) throw new Error('Synthetic fixture stopped');
    if (await predicate().catch(() => false)) return;
    await delay(100);
  }
  throw new Error(`Timed out: ${label}`);
}
async function makeContext(variant,layoutOnly=true) {
  const context = await browser.newContext({viewport:{width:1024,height:1000},reducedMotion:'reduce'});
  // Snapshot document fulfillment lacks the browser's original socket address
  // classification. Grant only this isolated fixture origin permission to use
  // its loopback API; keep production CSP/auth and the network allowlist intact.
  if (!layoutOnly) await context.grantPermissions(['local-network-access'],{origin:metadata.site_origin});
  const allowed = new Set([metadata.site_origin,metadata.api_origin]);
  await context.route('**/*',async route => {
    const request=route.request(),url=new URL(request.url());
    if (!allowed.has(url.origin)) { blockedOrigins.add(url.origin); await route.abort('blockedbyclient'); return; }
    if (url.origin===metadata.api_origin) {
      requests.push({method:request.method(),path:url.pathname,layoutOnly});
      if (layoutOnly) { await route.abort('blockedbyclient'); return; }
      assert.ok(request.method()==='GET' || request.method()==='OPTIONS'
        || (request.method()==='POST' && ['/auth/login','auth/logout','/auth/logout','/presence'].includes(url.pathname)),
      'Responsive QA must not create/transcribe/change lectures');
      await route.continue(); return;
    }
    let filename=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if (Object.hasOwn(snapshots[variant],filename)) {
      await route.fulfill({status:200,contentType:filename.endsWith('.css')?'text/css':'text/html',body:snapshots[variant][filename]}); return;
    }
    if (filename==='config.json') {
      const publishedAt=new Date().toISOString().replace(/\.\d{3}Z$/,'Z');
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({version:1,state:'online',
        apiUrl:metadata.api_origin,publishedAt,expiresAt:new Date(Date.parse(publishedAt)+86400000).toISOString().replace(/\.\d{3}Z$/,'Z')})}); return;
    }
    if (layoutOnly && filename.endsWith('.js')) {
      await route.fulfill({status:200,contentType:'text/javascript',body:'export {};'}); return;
    }
    await route.continue();
  });
  if (!layoutOnly) await context.addInitScript(({site,api}) => {
    if (location.origin===site) localStorage.setItem('yeobaek-server',api);
  },{site:metadata.site_origin,api:metadata.api_origin});
  return context;
}
async function screenshot(page,variant,width,state,selector) {
  const name=`${variant}-${width}-${state}.png`;
  if (selector) await page.locator(selector).screenshot({path:join(artifacts,name),animations:'disabled'});
  else { await page.evaluate(()=>scrollTo(0,0)); await page.screenshot({path:join(artifacts,name),fullPage:true,animations:'disabled'}); }
  report.screenshots.push(name);
}
async function layout(page,variant,width,state,scope='body') {
  await page.evaluate(()=>document.fonts.ready);
  const result=await page.evaluate(({state,scope}) => {
    const host=document.querySelector(scope),issues=[],metrics={};
    const visible=node => node && !node.closest('[hidden]') && getComputedStyle(node).visibility!=='hidden'
      && !node.classList.contains('visually-hidden') && node.getClientRects().length && node.getBoundingClientRect().width>1;
    const box=node => { const r=node.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width,height:r.height}; };
    const label=node => node.id?`#${node.id}`:`${node.tagName.toLowerCase()}.${String(node.className).trim().replace(/\s+/g,'.')}`;
    const round=value=>Math.round(value*10)/10;
    if (document.documentElement.scrollWidth>innerWidth+1) issues.push({type:'page_horizontal_overflow',pixels:document.documentElement.scrollWidth-innerWidth});
    const nodes=[host,...host.querySelectorAll('h1,h2,h3,p,button,a[download],input,select,textarea,.note-heading,.note-actions,.lesson-fields,.queue-actions,.held-audio-card,.summary-actions,.study-actions,.record-actions,.file-import-panel')];
    for (const node of nodes.filter(visible)) {
      const rect=box(node),css=getComputedStyle(node),name=label(node);
      if (rect.left < -1 || rect.right > innerWidth+1) issues.push({type:'element_outside_viewport',element:name,left:round(rect.left),right:round(rect.right)});
      const card=node.parentElement?.closest('dialog,.auth-panel,.note-shell,.recorder,.queue-warning,.held-audio-panel,.held-audio-card,.lesson-summary,.transcript-section,.file-import-panel');
      if (card && visible(card)) {
        const edge=box(card);
        if (rect.left<edge.left-1 || rect.right>edge.right+1) issues.push({type:'element_outside_card',element:name,card:label(card),width:round(rect.width),cardWidth:round(edge.width)});
      }
      if (!['INPUT','SELECT','TEXTAREA'].includes(node.tagName) && node.scrollWidth>node.clientWidth+2 && css.textOverflow!=='ellipsis') {
        issues.push({type:'internal_horizontal_overflow',element:name,pixels:node.scrollWidth-node.clientWidth});
      }
      if (['BUTTON','H1','H2','H3','P'].includes(node.tagName) && node.textContent.trim()) {
        const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);let text;
        while ((text=walker.nextNode())) {
          if (!text.textContent.trim() || !visible(text.parentElement)) continue;
          const range=document.createRange();range.selectNodeContents(text);
          const clips=[...range.getClientRects()].filter(r=>r.width>0);
          if (clips.some(r=>r.left<rect.left-2 || r.right>rect.right+2)) {
            issues.push({type:node.tagName==='BUTTON'?'button_text_clipped':'text_outside_box',element:name,width:round(rect.width)});break;
          }
        }
      }
    }
    for (const [key,selector] of Object.entries({title:'.note-heading h1',main:'.note-shell',queue:'#queue-warning',message:'#queue-message',
      form:'.lesson-fields',recovery:'#hold-new-note',login:'.auth-panel'})) {
      const node=document.querySelector(selector);
      if (visible(node)) metrics[key]={...Object.fromEntries(Object.entries(box(node)).map(([k,v])=>[k,round(v)])),
        fontSize:getComputedStyle(node).fontSize,lineHeight:getComputedStyle(node).lineHeight};
    }
    if (state==='basic' && innerWidth>=768 && metrics.title && metrics.main && metrics.title.width<Math.min(340,metrics.main.width*.6)) {
      issues.push({type:'squeezed_title',width:metrics.title.width,available:metrics.main.width});
    }
    if (state==='expanded' && metrics.message?.width<Math.min(220,metrics.queue.width*.5)) {
      issues.push({type:'squeezed_queue_message',width:metrics.message.width,available:metrics.queue.width});
    }
    return {issues:[...new Map(issues.map(issue=>[JSON.stringify(issue),issue])).values()],metrics,
      scrollWidth:document.documentElement.scrollWidth,viewport:innerWidth};
  },{state,scope});
  report.measurements.push({variant,width,state,...result});
  return result;
}
async function showWorkspace(page,expanded=false) {
  await page.evaluate(expanded => {
    const get=id=>document.getElementById(id);
    get('auth-screen').hidden=true;get('workspace').hidden=false;
    get('current-user').textContent='합성 테스트';get('model-status').textContent='합성 서버 준비됨';
    get('server-label').textContent='서버 연결됨';get('note-date').textContent='합성 수업 · 화면 배치 검증';
    const history=document.createElement('button');history.className='lecture-item selected';
    const historyTitle=document.createElement('strong');historyTitle.textContent='합성 수업 · 화면 배치 검증';
    const sub=document.createElement('span');sub.textContent='수업 목록 · 녹음 자료 없음';history.append(historyTitle,sub);get('lecture-list').replaceChildren(history);
    if (!expanded) return;
    const warning='서버 처리 여부를 확인하지 못한 음성 조각 403개가 이 기기에 남아 있습니다. 이미 처리되었는지 알 수 없는 CLOVA 음성은 자동으로 다시 보내지 않습니다. '
      +'기기에 남은 음성을 먼저 내려받고, 기존 음성 보관 · 새 수업으로 별도 녹음을 시작할 수 있습니다. 원래 탭과 사이트 데이터는 지우지 마세요.';
    get('queue-warning').hidden=false;get('queue-message').textContent=warning;
    for (const id of ['save-failed','skip-failed','retry','local-audio-open','hold-new-note']) {get(id).hidden=false;get(id).disabled=false;}
    get('capture-input-status').textContent='마이크 입력이 중단되었습니다. 기존 음성은 이 기기에 보관되어 있으며 새 수업을 시작해도 남아 있습니다.';
    get('capture-input-status').dataset.state='unavailable';
    get('save-state').textContent='기기에 음성 조각 403개 보관 중';
    get('held-audio-panel').hidden=false;get('held-audio-state').textContent='전송을 보류한 수업 2개가 남아 있습니다. 선택한 수업만 복구할 수 있습니다.';
    for (let index=1;index<=2;index++) {
      const card=document.createElement('article');card.className='held-audio-card';
      const strong=document.createElement('strong');strong.textContent=`합성 ${index} · 문맥과 핵심 개념을 함께 살펴보는 긴 제목의 강의`;
      const detail=document.createElement('p');detail.textContent='마이크 · CLOVA · 음성 조각 403개 · 서버 처리 여부 미확인 · 자동 재전송하지 않음';
      const actions=document.createElement('div');actions.className='study-actions';
      for (const text of ['수업 보기','보관 음성 다운로드','선택한 수업 전송 복구']) {
        const button=document.createElement('button');button.className='secondary-button';button.textContent=text;actions.append(button);
      }
      card.append(strong,detail,actions);get('held-audio-list').append(card);
    }
    get('recording-file-selection').textContent='합성으로_준비한_아주_긴_파일_이름_녹음파일_화면_검증용_audio_recording_example_without_personal_information.m4a';
    for (const id of ['recording-partial-download','recording-finalize']) get(id).hidden=false;
    for (const id of ['recording-review','transcript-versions','manual-panel','correction-panel','summary-panel','translation-panel','translation-views','study-note-panel','question-panel']) get(id).hidden=false;
    for (const id of ['review-details','manual-details','study-note-details','question-details']) get(id).open=true;
    get('summary-content').textContent='핵심 개념을 서로 비교하고, 원문 시간을 눌러 설명의 근거를 확인합니다. 이 내용은 화면 배치만 확인하는 합성 문장입니다.';
    for (const [id,text] of [['translation-content','The original lecture remains unchanged. 원문과 번역을 대조하며 숫자와 의미를 확인합니다.'],
      ['study-note-content','주제별 정리와 원문 시간 범위를 함께 표시합니다. 원문과 녹음을 변경하지 않는 합성 화면입니다.'],
      ['question-list','합성 질문: 두 개념의 차이는 무엇인가요? 합성 답변: 비교 기준을 원문에서 찾아 함께 살펴봅니다.']]) {
      const p=document.createElement('p');p.textContent=text;get(id).replaceChildren(p);
    }
    get('manual-note-text').value='긴 필기와 수정 이력을 화면에 표시하는 합성 예제입니다. 서버에 저장하지 않습니다.';
    const segment=document.createElement('div');segment.className='segment';
    const time=document.createElement('time');time.textContent='00:00';
    const text=document.createElement('p');text.textContent='이 문장은 반응형 배치 확인용 합성 원문입니다. 오디오나 개인 수업 자료는 사용하지 않습니다.';
    segment.append(time,text);get('transcript').replaceChildren(segment);
  },expanded);
}

try {
  await poll(async()=>{
    metadata=JSON.parse(await readFile(join(runDirectory,'fixture.json'),'utf8'));
    for (const origin of [metadata.site_origin,metadata.api_origin]) {
      const url=new URL(origin);assert.equal(url.hostname,'127.0.0.1');assert.notEqual(url.port,'8765');
    }
    return (await fetch(`${metadata.api_origin}/health`)).ok;
  },'isolated fixture readiness');
  browser=await chromium.launch({headless:true,env:{...process.env,
    LD_LIBRARY_PATH:join(sandbox,'libraries/usr/lib/x86_64-linux-gnu')},args:['--disable-background-networking']});
  report.browser=browser.version();
  const identifiers={};
  for (const variant of baselineOnly?['baseline']:['baseline','after']) {
    const context=await makeContext(variant);
    const page=await context.newPage();page.on('pageerror',()=>pageErrors.push(`${variant}:layout`));
    for (const width of widths) {
      phase=`${variant}/${width}`;
      await page.setViewportSize({width,height:1000});
      await page.goto(metadata.site_origin);await page.evaluate(()=>document.fonts.ready);
      if (!identifiers[variant]) identifiers[variant]=await page.locator('[id]').evaluateAll(nodes=>nodes.map(n=>n.id));
      await layout(page,variant,width,'login','#auth-screen');
      if (variant==='after' && width===390) await screenshot(page,variant,width,'login');
      await showWorkspace(page);
      await layout(page,variant,width,'basic','#workspace');
      const shoot=(variant==='baseline' && width===1024) || (variant==='after' && [390,768,1024,1440].includes(width));
      if (shoot) await screenshot(page,variant,width,'basic');
      await showWorkspace(page,true);
      await layout(page,variant,width,'expanded','#workspace');
      if (shoot) await screenshot(page,variant,width,'queue-recovery','.recorder');
      if (variant==='after' && [768,1440].includes(width)) await screenshot(page,variant,width,'ai-review','.transcript-section');
      for (const id of ['connection-dialog','local-audio-dialog','library-search-dialog','manual-edit-dialog']) {
        await page.evaluate(id=>document.getElementById(id).showModal(),id);
        await layout(page,variant,width,id,`#${id}`);
        if (variant==='after' && width===390 && id==='local-audio-dialog') await screenshot(page,variant,width,id,`#${id}`);
        await page.evaluate(id=>document.getElementById(id).close(),id);
      }
      await page.goto(`${metadata.site_origin}/rescue.html`);
      await layout(page,variant,width,'rescue');
      if (variant==='after' && width===390) await screenshot(page,variant,width,'rescue');
      const rows=report.measurements.filter(row=>row.variant===variant && row.width===width);
      process.stdout.write(`${JSON.stringify({variant,width,states:rows.length,issues:rows.reduce((sum,row)=>sum+row.issues.length,0)})}\n`);
    }
    await context.close();
  }
  if (!baselineOnly) {
    assert.equal(new Set(identifiers.after).size,identifiers.after.length,'No duplicate DOM IDs');
    assert.deepEqual(identifiers.baseline.filter(id=>!identifiers.after.includes(id)),[],'All existing functional DOM IDs are preserved');
    phase='actual synthetic login and read-only recovery smoke';
    const context=await makeContext('after',false),page=await context.newPage();activePage=page;
    page.on('pageerror',()=>pageErrors.push('functional'));
    page.on('console',message=>{if(message.type()==='error')consoleErrors.push(message.text());});
    await page.goto(metadata.site_origin);
    await poll(()=>page.locator('#login-button').isEnabled(),'login ready');
    await page.locator('#username').fill(metadata.accounts[0]);await page.locator('#password').fill(metadata.password);
    await page.locator('#login-button').click();
    await poll(async()=>await page.locator('#current-user').textContent()===metadata.accounts[0],'synthetic login');
    await page.locator('#connection-open').click();await page.locator('#connection-close').click();
    await page.locator('#local-audio-open').click();
    await poll(()=>page.locator('#local-audio-refresh').isEnabled(),'read-only local audio scan');
    assert.equal(await page.locator('#local-audio-files a[download]').count(),0);
    await page.locator('#local-audio-close').click();
    assert.equal(await page.locator('#record-button').isEnabled(),true);
    const storageBefore=await page.evaluate(()=>indexedDB.databases());
    const rescue=await context.newPage();rescue.on('pageerror',()=>pageErrors.push('rescue-functional'));
    await rescue.goto(`${metadata.site_origin}/rescue.html`);
    await poll(()=>rescue.locator('#rescue-login').isEnabled(),'rescue trusted connection');
    await rescue.locator('#rescue-username').fill(metadata.accounts[0]);await rescue.locator('#rescue-password').fill(metadata.password);
    await rescue.locator('#rescue-login').click();
    await poll(async()=>await rescue.locator('#rescue-scan').isEnabled()
      && await rescue.locator('#rescue-results').isVisible(),'rescue authenticated empty scan');
    assert.equal(await rescue.locator('#rescue-files a[download]').count(),0);
    assert.deepEqual(await page.evaluate(()=>indexedDB.databases()),storageBefore,'Rescue does not create or upgrade IndexedDB');
    await rescue.locator('#rescue-lock').click();assert.equal(await rescue.locator('#rescue-results').isVisible(),false);
    const serverState=await fetch(`${metadata.api_origin}/__validation__/state`).then(r=>r.json());
    assert.equal(serverState.lectures.length,0);assert.equal(serverState.asr_calls.length,0);
    report.functional.push('existing IDs preserved','actual synthetic login','connection dialog open/close',
      'local audio read-only empty scan','rescue account login/read-only scan/lock','no lecture or ASR operation');
    await context.close();
  }
  assert.deepEqual([...blockedOrigins],[]);assert.deepEqual(pageErrors,[]);
  report.after_issues=report.measurements.filter(row=>row.variant==='after').flatMap(row=>row.issues.map(issue=>({width:row.width,state:row.state,...issue})));
  report.status=baselineOnly?'baseline-recorded':report.after_issues.length?'layout-issues':'passed';
  if (!baselineOnly && report.after_issues.length) process.exitCode=1;
} catch (error) {
  report.status='failed';report.phase=phase;report.error=error.message;process.exitCode=1;
  report.connection_ui=await activePage?.evaluate(()=>Object.fromEntries(
    ['server-label','auth-server-status','auth-error','connection-status','connection-error'].map(id=>[id,document.getElementById(id)?.textContent || ''])
  )).catch(()=>null);
} finally {
  await browser?.close().catch(()=>{});
  if (fixture.exitCode===null && fixture.signalCode===null) {
    const ended=once(fixture,'exit'),timer=new AbortController();
    fixture.kill('SIGTERM');await Promise.race([ended,delay(10000,undefined,{signal:timer.signal})]);timer.abort();
    if (fixture.exitCode===null && fixture.signalCode===null) {
      const force=new AbortController();fixture.kill('SIGKILL');await Promise.race([ended,delay(3000,undefined,{signal:force.signal})]);force.abort();
    }
  }
  report.fixture_stopped=fixture.exitCode!==null || fixture.signalCode!==null;
  report.page_errors=pageErrors;
  report.console_errors=consoleErrors;
  report.request_counts=Object.fromEntries([...new Set(requests.map(row=>`${row.method} ${row.path}`))]
    .map(key=>[key,requests.filter(row=>`${row.method} ${row.path}`===key).length]));
  await writeFile(join(artifacts,'fixture.log'),fixtureLog,{mode:0o600});
  await writeFile(join(artifacts,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
  process.stdout.write(`${JSON.stringify({status:report.status,phase:report.phase,error:report.error,
    afterIssues:report.after_issues?.length,fixture_stopped:report.fixture_stopped,report:join(artifacts,'report.json')})}\n`);
}
