import { writeFile, mkdir } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
import path from 'node:path';
import net from 'node:net';
import lighthouse from 'lighthouse';
import { chromium } from '@playwright/test';
const url = process.env.STUDY_AUDIT_URL || 'http://127.0.0.1:3218/__study-test/';
const output = path.resolve(process.env.STUDY_AUDIT_OUTPUT || 'test-results/audit');
await mkdir(output, { recursive: true });
const executable =
  process.env.STUDY_BROWSER_EXECUTABLE ||
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const port = await new Promise((resolve, reject) => {
  const socket = net.createServer();
  socket.on('error', reject);
  socket.listen(0, '127.0.0.1', () => {
    const address = socket.address();
    socket.close(() => resolve(address.port));
  });
});
const chrome = await chromium.launch({
  executablePath: executable,
  headless: true,
  args: [`--remote-debugging-port=${port}`],
});
try {
  const result = await lighthouse(url, {
    port,
    output: ['html', 'json'],
    onlyCategories: ['performance', 'accessibility', 'best-practices'],
    logLevel: 'error',
  });
  await writeFile(path.join(output, 'lighthouse.html'), result.report[0]);
  await writeFile(path.join(output, 'lighthouse.json'), result.report[1]);
  if (
    result.lhr.runtimeError ||
    Object.values(result.lhr.audits).some((audit) => audit.scoreDisplayMode === 'error')
  )
    throw new Error('Lighthouse could not complete a valid measurement. Inspect lighthouse.json.');
  const scores = Object.fromEntries(
    Object.entries(result.lhr.categories).map(([key, value]) => [
      key,
      Math.round(value.score * 100),
    ]),
  );
  const failures = Object.values(result.lhr.audits)
    .filter((a) => a.score !== null && a.score < 1)
    .map((a) => ({ id: a.id, title: a.title, score: a.score }));
  console.log(JSON.stringify({ scores, failures }));
  if (Object.values(scores).some((score) => score < 90)) process.exitCode = 1;
} finally {
  await chrome.close();
}
const browser = await chromium.launch({ executablePath: executable, headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const loaded = new Map();
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(error.message));
  page.on('response', (response) => {
    if (response.ok() && response.url().startsWith(new URL(url).origin))
      loaded.set(response.url(), response);
  });
  await page.goto(url);
  const frame = page.frameLocator('iframe');
  await frame.locator('#deck-select').waitFor();
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: path.join(output, 'mobile-home.png'), fullPage: true });
  const assets = [];
  for (const [address, response] of loaded) {
    const body = await response.body();
    assets.push({
      path: new URL(address).pathname,
      bytes: body.length,
      gzip: gzipSync(body).length,
    });
  }
  const studyAssets = assets.filter((item) => item.path.includes('/study/'));
  const report = {
    totalGzip: studyAssets.reduce((sum, item) => sum + item.gzip, 0),
    assets: studyAssets,
    consoleErrors,
  };
  await writeFile(path.join(output, 'initial-load.json'), JSON.stringify(report, null, 2));
  if (report.totalGzip >= 200000 || consoleErrors.length) process.exitCode = 1;
  console.log(
    JSON.stringify({
      initialStudyGzipBytes: report.totalGzip,
      assets: studyAssets.length,
      consoleErrors,
    }),
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: path.join(output, 'desktop-home.png'), fullPage: true });
} finally {
  await browser.close();
}
