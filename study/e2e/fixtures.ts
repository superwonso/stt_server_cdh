import { test as base, expect } from '@playwright/test';

const channel = 'yeobaek-study-v1';
const studyPath = '/stt_server_cdh/study/';
const parentPath = '/__study-test/';
const publicAssetMode = process.env.STUDY_PUBLIC_ASSETS === '1';

// This synthetic parent exists only in Playwright's route handler. Nothing is
// uploaded to the service, and the scope is not a real account or credential.
const parentFixture = `<!doctype html><html lang="ko"><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<link rel="icon" href="data:,">
<title>공개 학습 자산 검증</title>
<style>body{margin:0}iframe{width:100%;height:1600px;border:0}</style>
</head><body><iframe title="암기 학습" src="${studyPath}"
sandbox="allow-scripts allow-same-origin allow-downloads allow-modals"></iframe>
<script>const f=document.querySelector('iframe');
window.addEventListener('message',e=>{
if(e.origin!==location.origin||e.source!==f.contentWindow||e.data?.channel!=='${channel}')return;
if(e.data.type==='ready')f.contentWindow.postMessage({channel:'${channel}',type:'context',scope:'${'a'.repeat(64)}',active:true},location.origin);
if(e.data.type==='height'&&Number.isFinite(e.data.height))f.style.height=Math.max(600,Math.min(20000,e.data.height))+'px';
});</script></body></html>`;

export const test = base.extend({
  page: async ({ page, context, baseURL }, runPage, testInfo) => {
    if (!publicAssetMode) {
      await runPage(page);
      return;
    }
    if (!baseURL) throw new Error('Public asset checks require STUDY_TEST_URL.');
    const origin = new URL(baseURL).origin;
    const rehearsal = process.env.STUDY_PUBLIC_ASSETS_REHEARSAL === '1';
    const permitted =
      origin === 'https://superwonso.github.io' ||
      (rehearsal && /^http:\/\/127\.0\.0\.1:\d+$/.test(origin));
    if (!permitted)
      throw new Error('Only the approved public site or explicit loopback rehearsal is allowed.');
    expect(await context.cookies()).toHaveLength(0);
    const blocked: string[] = [];
    const failed: { path: string; status: number }[] = [];
    const loaded = new Set<string>();
    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const headers = request.headers();
      if (
        !['GET', 'HEAD'].includes(request.method()) ||
        url.origin !== origin ||
        headers.authorization ||
        headers.cookie
      ) {
        blocked.push('unexpected-method-origin-or-credentials');
        await route.abort();
        return;
      }
      if (url.pathname === parentPath && url.search === '') {
        await route.fulfill({
          status: 200,
          contentType: 'text/html; charset=utf-8',
          body: parentFixture,
          headers: { 'Cache-Control': 'no-store' },
        });
        return;
      }
      if (!url.pathname.startsWith(studyPath)) {
        blocked.push('unexpected-non-study-path');
        await route.abort();
        return;
      }
      await route.continue();
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin !== origin || !url.pathname.startsWith(studyPath)) return;
      if (response.ok()) loaded.add(url.pathname);
      else failed.push({ path: url.pathname, status: response.status() });
    });
    try {
      await runPage(page);
    } finally {
      const report = {
        check: rehearsal ? 'loopback-public-asset-rehearsal' : 'production-public-static-assets',
        origin,
        syntheticParent: true,
        syntheticAccountScope: true,
        realServiceAuthenticationTested: false,
        productionDatabaseAccessed: false,
        paidCalls: 0,
        loadedAssetCount: loaded.size,
        blockedRequests: blocked,
        failedAssets: failed,
      };
      await testInfo.attach('public-assets-scope', {
        body: Buffer.from(JSON.stringify(report, null, 2)),
        contentType: 'application/json',
      });
      expect(blocked).toEqual([]);
      expect(failed).toEqual([]);
      expect(loaded.has(studyPath)).toBe(true);
      expect([...loaded].some((path) => path.endsWith('.js'))).toBe(true);
      expect([...loaded].some((path) => path.endsWith('.css'))).toBe(true);
      expect(await context.cookies()).toHaveLength(0);
    }
  },
});

export { expect };
