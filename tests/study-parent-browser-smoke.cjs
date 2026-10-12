// Real parent/Next iframe integration, synthetic fixture and fake providers only.
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.STUDY_PLAYWRIGHT_PATH || 'playwright');
(async()=>{
  const origin=process.env.STUDY_PARENT_ORIGIN;
  if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin||''))throw Error('Use the isolated study-parent-fixture.py server.');
  const output=path.resolve(process.env.STUDY_PARENT_OUTPUT||'.data/study-parent-smoke');fs.mkdirSync(output,{recursive:true});
  let browser,phase='start'; const errors=[],external=[],checks=[];
  try{
    browser=await chromium.launch({executablePath:process.env.STUDY_BROWSER_EXECUTABLE||undefined,headless:true,
      args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
    const context=await browser.newContext({viewport:{width:1440,height:1100},locale:'ko-KR',timezoneId:'Asia/Seoul',permissions:['microphone']});
    await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin!==origin){external.push(url.origin);return route.abort();}return route.continue();});
    await context.addInitScript(value=>localStorage.setItem('yeobaek-server',value),origin);
    const page=await context.newPage();page.setDefaultTimeout(15000);page.on('pageerror',error=>errors.push(error.message));
    await page.goto(origin+'/stt_server_cdh/');
    const login=async owner=>{await page.locator('#username').fill(owner);await page.locator('#password').fill('synthetic-test-password');await page.locator('#login-button').click();await page.locator('#workspace').waitFor({state:'visible'});};
    await login('user-alpha');
    phase='handshake';await page.getByRole('tab',{name:'암기 학습',exact:true}).click();
    const iframe=page.locator('.study-frame');await iframe.waitFor();
    const child=page.frameLocator('.study-frame');await child.locator('#deck-select').waitFor();
    assert.equal(await child.locator('.study-chapter').count(),16);
    assert.match(await child.locator('#deck-select').textContent(),/665/);
    assert.equal(await child.locator('.study-app').getAttribute('data-active'),'true');
    assert.equal(await page.locator('.study-loading').isVisible(),false);
    await child.getByRole('button',{name:'선택 해제',exact:true}).click();
    await child.locator('.study-chapter input').first().check();
    checks.push('same-origin parent/child handshake loads 665 questions and 16 chapters without credentials in child messages');
    const geometry=()=>page.evaluate(()=>Object.fromEntries(['.site-header','#service-tabs','.brand','#connection-open'].map(selector=>{const b=document.querySelector(selector).getBoundingClientRect();return [selector,{x:b.x,y:b.y,width:b.width,height:b.height}];})));
    const before=await geometry();
    phase='switch-tabs';await page.getByRole('tab',{name:'리마인더',exact:true}).click();await page.locator('.rv-today').waitFor();
    assert.equal(await child.locator('.study-app').getAttribute('data-active'),'false');assert.deepEqual(await geometry(),before);
    await page.getByRole('tab',{name:'여백',exact:true}).click();assert.deepEqual(await geometry(),before);
    await page.getByRole('tab',{name:'암기 학습',exact:true}).click();assert.equal(await child.locator('.study-chapter input:checked').count(),1);
    checks.push('all three tabs retain identical header geometry; hidden study timers pause and selection survives tab switches');
    phase='layout';for(const width of [1440,1024,390,320]){
      await page.setViewportSize({width,height:1100});await page.evaluate(()=>scrollTo(0,0));
      await page.screenshot({path:path.join(output,`study-parent-${width}.png`)});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      const childFrame=page.frames().find(frame=>frame.url().endsWith('/study/'));
      assert.equal(await childFrame.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    }
    checks.push('parent and iframe have no horizontal overflow at 1440/1024/390/320 pixels');
    phase='fake-ai';await page.setViewportSize({width:1440,height:1100});
    await child.getByRole('button',{name:'＋ 내 파일 가져오기',exact:true}).click();
    await child.getByText('글에서 문항 만들기',{exact:true}).click();
    await child.getByRole('textbox',{name:/사용할 글/}).fill('합성 자료의 답은 합성 답이다.');
    await child.getByRole('spinbutton',{name:'만들 문항 수',exact:true}).fill('1');
    await child.getByRole('checkbox',{name:/위 글을 여백 서버/}).check();
    await child.getByRole('button',{name:'AI 문항 초안 만들기',exact:true}).click();
    await child.getByRole('heading',{name:'저장 전 확인 · 1문제',exact:true}).waitFor();
    checks.push('explicit text consent crosses the parent bridge to the synthetic authenticated API and returns one reviewed draft');
    phase='logout';await page.locator('#reminder-logout').click();await page.locator('#auth-screen').waitFor({state:'visible'});
    assert.equal(await page.locator('.study-frame').count(),0);
    await login('user-beta');await page.getByRole('tab',{name:'암기 학습',exact:true}).click();await child.locator('#deck-select').waitFor();
    assert.equal(await child.locator('.study-chapter input:checked').count(),16);
    assert.equal(await child.getByRole('heading',{name:'저장 전 확인 · 1문제',exact:true}).count(),0);
    checks.push('logout removes the iframe; the second account cannot see the prior selection or AI draft');
    phase='recording-preserved';await page.getByRole('tab',{name:'여백',exact:true}).click();
    await page.locator('#lecture-title').fill('합성 브라우저 녹음 유지 확인');
    await page.locator('#record-button').click();
    await page.locator('#service-recording').waitFor({state:'visible'});
    assert.match(await page.locator('#service-recording').textContent(),/녹음 중/);
    await page.getByRole('tab',{name:'암기 학습',exact:true}).click();
    assert.equal(await page.locator('#service-recording').isVisible(),true);
    await page.getByRole('tab',{name:'여백',exact:false}).click();
    assert.match(await page.locator('#record-button').textContent(),/받아쓰기 종료/);
    await page.locator('#record-button').click();
    await page.locator('#service-recording').waitFor({state:'hidden'});
    checks.push('real browser microphone capture using a synthetic device remains active while switching to study and stops only explicitly');
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    const report={passed:true,syntheticOnly:true,paidCalls:0,errors,externalRequests:external,checks};
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
  }catch(error){console.error(JSON.stringify({phase,error:error.message,errors,external}));process.exitCode=1;}
  finally{await browser?.close();}
})();
