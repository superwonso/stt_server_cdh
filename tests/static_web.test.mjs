import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const app = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const css = await readFile(new URL('../web/style.css', import.meta.url), 'utf8');

test('static page provides every element required by the app exactly once', () => {
  const required = new Set([...app.matchAll(/\$\('([^']+)'\)/g)].map(([, id]) => id));
  const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(([, id]) => id);
  assert.deepEqual([...required].filter(id => !ids.includes(id)), []);
  assert.deepEqual([...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))], []);
});

test('page has no remote assets or inline executable content and declares privacy policy', () => {
  assert.match(html, /http-equiv="Content-Security-Policy"/);
  assert.match(html, /form-action 'none'/);
  assert.match(html, /script-src 'self'/);
  assert.match(html, /style-src 'self'/);
  assert.equal(html.match(/media-src ([^;]+)/)[1].trim(),'blob:', 'recording playback may only use bounded authenticated in-memory clips');
  const connectSources = html.match(/connect-src ([^;]+)/)[1].trim().split(/\s+/);
  assert.ok(connectSources.includes("'self'"));
  assert.ok(connectSources.includes('https://*.trycloudflare.com'));
  assert.ok(connectSources.includes('http://localhost:*'));
  assert.ok(connectSources.includes('http://127.0.0.1:*'));
  assert.ok(!connectSources.some(value => value.includes('[')),
    'Chromium ignores bracketed IPv6 CSP host-sources and emits console errors; use localhost or 127.0.0.1');
  assert.ok(!connectSources.includes('https:'), 'arbitrary HTTPS destinations must not be allowed');
  assert.ok(!connectSources.includes('http:'), 'arbitrary HTTP destinations must not be allowed');
  assert.ok(!connectSources.some(value => /google(?:apis)?\.com/i.test(value)),
    'the browser must download recordings through the authenticated API server, not Google');
  assert.match(html, /name="referrer" content="no-referrer"/);
  assert.match(html, /사이트 운영자가 관리하는 NAVER Cloud 계정/);
  assert.doesNotMatch(html, /비용이 청구|추가 과금|중복 과금|과금될/);
  assert.doesNotMatch(app, /비용이 청구|추가 과금|중복 과금|과금될/);
  assert.doesNotMatch(html, /<(?:script|style)(?![^>]*\bsrc=)[^>]*>\s*[^<\s]/i);
  assert.doesNotMatch(html, /\s(?:src|href)="https?:/i);
  assert.doesNotMatch(html, /\son[a-z]+\s*=/i);
});

test('secrets use bounded password fields with appropriate autofill hints', () => {
  const input = id => {
    const match = html.match(new RegExp(`<input\\b[^>]*\\bid="${id}"[^>]*>`, 'i'));
    assert.ok(match, `missing ${id}`);
    return match[0];
  };
  for (const id of ['password', 'password-confirm', 'setup-code']) {
    assert.match(input(id), /\btype="password"/i);
    assert.match(input(id), /\bmaxlength="128"/i);
    assert.match(input(id), /\bspellcheck="false"/i);
  }
  assert.match(input('password'), /\bautocomplete="current-password"/i);
  assert.match(input('password-confirm'), /\bautocomplete="new-password"/i);
  assert.match(input('password-confirm'), /\bminlength="4"/i);
  assert.match(input('setup-code'), /\bautocomplete="one-time-code"/i);
});

test('automatic server discovery has a locked login and an accessible manual fallback', () => {
  assert.match(html, /<button\b[^>]*\bid="connection-open"[^>]*\bdata-state="discovering"[^>]*\baria-busy="true"[^>]*\baria-controls="connection-dialog"/i);
  assert.match(html, /<p\b[^>]*\bid="auth-server-status"[^>]*\brole="status"[^>]*\baria-live="polite"/i);
  assert.match(html, /<button\b[^>]*\bid="auth-server-open"[^>]*\baria-haspopup="dialog"[^>]*\baria-controls="connection-dialog"/i);
  assert.match(html, /<button\b[^>]*\bid="login-button"[^>]*\btype="submit"[^>]*\bdisabled(?:\s|>)/i);
  assert.match(html, /<dialog\b[^>]*\bid="connection-dialog"[^>]*\baria-describedby="connection-description connection-status"/i);
  assert.match(html, /<p\b[^>]*\bid="connection-status"[^>]*\brole="status"[^>]*\baria-live="polite"/i);
  assert.match(html, /<input\b[^>]*\bid="api-url"[^>]*\baria-describedby="connection-description connection-privacy connection-error"/i);
});

test('account recovery uses private administrator contact and explicit single-use secret controls', () => {
  assert.match(html,/아이디나 비밀번호를 잊었나요\? 관리자에게 개인적으로 문의/);
  assert.match(html,/<dialog\b[^>]*id="admin-recovery-dialog"[^>]*aria-describedby="admin-recovery-description"/);
  assert.match(html,/<input\b[^>]*id="admin-recovery-password"[^>]*type="password"[^>]*maxlength="128"[^>]*autocomplete="off"/);
  assert.match(html,/<textarea\b[^>]*id="admin-recovery-link"[^>]*readonly[^>]*autocomplete="off"/);
  assert.match(html,/요청자와 개인적으로 연락해 본인임을 확인/);
  assert.match(html,/기존 복구 링크 취소/);
  assert.match(html,/이 창을 닫으면 링크는 지워지고 다시 표시할 수 없습니다/);
  assert.match(html,/자동으로 재발급하지 않습니다/);
  assert.doesNotMatch(html,/type="email"|id="reset-code"/);
});

test('retained usage declares cohort coverage unavailable billing and responsive administrator-only controls', () => {
  assert.match(html,/<select\b[^>]*id="admin-usage-period"[^>]*aria-describedby="admin-usage-definition"/);
  assert.match(html,/<option value="month" selected>이번 달/);
  assert.match(html,/<p\b[^>]*id="admin-usage-updated"[^>]*role="status"/);
  assert.match(html,/수업 생성일\(KST\) 기준 현재 보관 기록/);
  assert.match(html,/휴지통 수업은 포함하고 삭제 중·영구 삭제된 수업은 제외/);
  assert.match(html,/확인할 수 없는 시간과 과거 누락은 0으로 추정하지/);
  assert.match(html,/보관된 완료 결과 수이며 API 요청 수·토큰 수가 아닙니다/);
  assert.match(html,/실제 과금액은 기록하지 않아/);
  assert.match(css,/\.admin-usage-summary, \.admin-usage-account dl \{ grid-template-columns: minmax\(0,1fr\)/);
  assert.match(css,/\.admin-usage-account dd[^}]*overflow-wrap: anywhere/);
});

test('history, export, recording, and destructive controls are explicit and accessible', () => {
  assert.match(html, /<select\b[^>]*\bid="lecture-date"[^>]*\baria-label="수업 날짜별 보기"/i);
  assert.match(html, /<option value="">전체 날짜<\/option>/);
  assert.match(html, /<option value="markdown">Markdown \(\.md\)<\/option>/);
  assert.match(html, /<option value="text" selected>일반 텍스트 \(\.txt\)<\/option>/);
  assert.match(html, /<div\b[^>]*\bclass="note-actions"[^>]*\brole="group"[^>]*\baria-label="현재 수업 작업"/i);
  assert.match(html, /<dialog\b[^>]*\bid="delete-dialog"[^>]*\baria-labelledby="delete-title"[^>]*\baria-describedby="delete-description"/i);
  assert.match(html, /원문·AI 결과·필기·녹음은 보관되며 휴지통에서 복원/);
  assert.match(html, /id="purge-dialog"[^>]*aria-labelledby="purge-heading"/);
  assert.match(html, /앱에서 되돌릴 수 없고/);
  assert.match(html, /연결된 Drive 녹음은 Drive 휴지통으로 옮깁니다/);
  assert.match(html, /자동으로 영구 삭제하지 않습니다/);
  assert.match(html, /기존 백업과 CLOVA 별도 사본은 자동 삭제되지 않습니다/);
  assert.match(html, /<div\b[^>]*\bclass="save-row"[^>]*\brole="status"[^>]*\baria-live="polite"/i);
  assert.match(html, /<div\b[^>]*\bclass="record-actions"[^>]*\brole="group"[^>]*\baria-label="받아쓰기 녹음 제어"/i);
  assert.match(html, /<button\b[^>]*\bid="pause-button"[^>]*\baria-pressed="false"[^>]*\bdisabled[^>]*>Ⅱ 일시정지<\/button>/i);
  assert.match(html, /<div\b[^>]*\bid="live-capture-banner"[^>]*\brole="region"[^>]*\baria-labelledby="live-capture-title"[^>]*\bhidden/i);
  assert.match(html, /<button\b[^>]*\bid="return-live-capture"[^>]*>현재 녹음으로 돌아가기<\/button>/i);
});

test('lesson heading separates its title from a wrapping toolbar without enabling unavailable actions', () => {
  const heading = html.match(/<header\b[^>]*class="note-heading"[^>]*>([\s\S]*?)<\/header>/)?.[1];
  assert.ok(heading, 'the current lesson must keep a heading and a separate action group');
  const title = heading.match(/<h1\b[^>]*>([\s\S]*?)<\/h1>/);
  assert.ok(title);
  assert.doesNotMatch(title[1], /<(?:button|select)\b/i);
  assert.match(heading, /<\/h1>[\s\S]*<div\b[^>]*class="note-actions"[^>]*role="group"[^>]*aria-label="[^"]+"/);
  for (const id of ['export-format','download','recording-download','recording-partial-download',
    'recording-finalize','continue-recording','metadata-open','delete-lecture']) {
    const control = heading.match(new RegExp(`<(?:button|select)\\b[^>]*\\bid="${id}"[^>]*>`))?.[0];
    assert.ok(control, `the lesson toolbar must retain ${id}`);
    assert.match(control, /\sdisabled(?:\s|>)/);
    if (control.startsWith('<button')) assert.match(control, /\btype="button"/);
    if (['recording-partial-download','recording-finalize'].includes(id)) assert.match(control, /\shidden(?:\s|>)/);
  }
  assert.match(css, /\.note-heading\s*\{[^}]*flex-direction:\s*column\s*;/);
  assert.match(css, /\.note-actions\s*\{[^}]*flex-wrap:\s*wrap\s*;/);
});

test('audio recovery panel groups live input status with explicit preservation controls and guidance', () => {
  const panel = html.match(/<details\b[^>]*class="audio-recovery-panel"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(panel, 'input health and local recovery must remain in the recorder panel');
  assert.doesNotMatch(panel[0].split('>')[0], /\sopen(?:\s|=|$)/);
  assert.match(panel[1], /^\s*<summary\b[^>]*id="audio-recovery-title"[^>]*>[\s\S]*?<\/summary>/);
  assert.match(panel[1], /<p\b[^>]*id="capture-input-status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(panel[1], /<div\b[^>]*class="recovery-actions"[^>]*role="group"[^>]*aria-label="[^"]+"/);
  for (const id of ['local-audio-open','hold-new-note']) {
    const button = panel[1].match(new RegExp(`<button\\b[^>]*\\bid="${id}"[^>]*>`))?.[0];
    assert.ok(button, `the recovery panel must retain ${id}`);
    assert.match(button, /\btype="button"/);
    if (id === 'hold-new-note') assert.match(button, /\sdisabled(?:\s|>)/);
  }
  const help = panel[1].match(/<p\b[^>]*class="panel-help"[^>]*>([^<]+)<\/p>/)?.[1];
  assert.ok(help, 'preservation and new-capture instructions must not disappear with the layout');
  assert.match(help, /현재 녹음.*종료/);
  assert.match(help, /시작 버튼/);
  assert.match(help, /삭제하거나 자동 재전송하지 않습니다/);
  assert.match(css, /\.recovery-actions\s*\{[^}]*flex-wrap:\s*wrap\s*;/);
});

test('queue warning separates readable failure text from wrapping actions and retains safe initial visibility', () => {
  const warning = html.match(/<div\b[^>]*id="queue-warning"[^>]*>([\s\S]*?)<\/div>\s*<\/div>/);
  assert.ok(warning);
  assert.match(warning[0], /role="alert"[^>]*\shidden(?:\s|>)/);
  assert.match(warning[1], /<p\b[^>]*id="queue-message"[^>]*>[\s\S]*?<\/p>\s*<div\b[^>]*class="queue-actions"/);
  const message = warning[1].match(/<p\b[^>]*id="queue-message"[^>]*>([\s\S]*?)<\/p>/)?.[1];
  assert.doesNotMatch(message, /<button\b/i);
  for (const id of ['save-failed','skip-failed','retry']) {
    const button = warning[1].match(new RegExp(`<button\\b[^>]*\\bid="${id}"[^>]*>`))?.[0];
    assert.ok(button, `the failure actions must retain ${id}`);
    assert.match(button, /\btype="button"/);
    if (id === 'skip-failed') assert.match(button, /\shidden(?:\s|>)/);
  }
  assert.match(css, /\.queue-warning\s*\{[^}]*flex-direction:\s*column\s*;/);
  assert.match(css, /\.queue-warning\s+p\s*\{[^}]*overflow-wrap:\s*anywhere\s*;/);
  assert.match(css, /\.queue-actions\s*\{[^}]*flex-wrap:\s*wrap\s*;/);
  assert.match(css, /\.queue-warning:not\(\[hidden\]\)\s*\{[^}]*display:\s*flex\s*;/);
});

test('library search has no classification filters while metadata editing remains available', () => {
  assert.doesNotMatch(html, /\bid="library-(?:course|semester)"/);
  assert.doesNotMatch(app, /\$\(['"]library-(?:course|semester)['"]\)|libraryCourse|librarySemester|renderLibraryFilters/);
  assert.match(html, /\bid="lecture-date"/);
  assert.match(html, /\bid="library-query"/);
  assert.match(html, /\bid="library-source"/);
  for (const id of ['metadata-title','metadata-course','metadata-semester','course-options','semester-options']) {
    assert.ok(html.includes(`id="${id}"`), `stored metadata editing still needs ${id}`);
  }
  assert.match(app, /내 모든 수업 · 날짜 제한 없이 검색/);
});

test('AI correction controls keep raw and corrected transcripts explicit without exposing credentials', () => {
  assert.match(html, /<div\b[^>]*\bid="transcript-versions"[^>]*\brole="group"[^>]*\baria-label="표시할 받아쓰기 버전"[^>]*\bhidden/i);
  assert.match(html, /<button\b[^>]*\bid="transcript-raw"[^>]*\baria-pressed="true"/i);
  assert.match(html, /<button\b[^>]*\bid="transcript-corrected"[^>]*\baria-pressed="false"[^>]*\bdisabled/i);
  assert.match(html, /<p\b[^>]*\bid="correction-detail"[^>]*\brole="status"[^>]*\baria-live="polite"/i);
  assert.match(html, /<button\b[^>]*\bid="correct-transcript"[^>]*\btype="button"[^>]*\bdisabled/i);
  assert.match(html, /텍스트만 NOVA\(Mindlogic\)로 전송하고 오디오는 보내지 않습니다/);
  assert.match(html, /모든 아라비아 숫자와 형식을 인식한 일부 이메일·전화번호는 이 PC에서 먼저 가리지만/);
  assert.doesNotMatch(html, /MINDLOGIC_API_KEY|OPENAI_API_KEY|Bearer\s+[A-Za-z0-9_-]/i);
});

test('AI correction summary and translation start collapsed with native keyboard-accessible toggles', () => {
  for (const [id,title,action,content] of [
    ['correction','AI 후보정','correct-transcript','correction-detail'],
    ['summary','AI 수업 요약','summarize-lecture','summary-content'],
    ['translation','영어 수업 한국어 번역','translate-lecture','translation-content'],
  ]) {
    const details = html.match(new RegExp(`<details\\b([^>]*\\bid="${id}-details"[^>]*)>([\\s\\S]*?)<\\/details>`));
    assert.ok(details, `missing ${id} disclosure`);
    assert.doesNotMatch(details[1], /\bopen(?:\s|=|$)/);
    assert.match(details[2], new RegExp(`^\\s*<summary\\b[^>]*>${title}<\\/summary>`));
    assert.ok(details[2].includes(`id="${action}"`), 'generation stays inside the disclosure');
    assert.ok(details[2].includes(`id="${content}"`), 'details and results stay inside the disclosure');
    assert.doesNotMatch(details[2].match(/<summary\b[^>]*>([\s\S]*?)<\/summary>/)[1], /<(?:button|a|input)\b/);
  }
  assert.match(css, /\.ai-tool-details > summary:focus-visible\s*\{[^}]*outline:/);
  assert.match(css, /\.ai-tool-details > summary\s*\{[^}]*overflow-wrap:\s*anywhere/);
});

test('unified study-note details preserve originals and disclose material transmission', () => {
  assert.match(html,/<details id="study-note-details">/);
  assert.match(html,/<button[^>]*id="study-note-create"[^>]*type="button"[^>]*disabled/);
  assert.match(html,/<button[^>]*id="study-note-refresh"/);
  assert.match(html,/<a[^>]*id="study-note-download"[^>]*download[^>]*hidden/);
  assert.match(html,/상세 설명과 모든 원문 구간을 함께 보관합니다/);
  assert.match(html,/확정 원문과 연결된 강의자료 텍스트를 NOVA/);
  assert.match(html,/녹음은 보내지 않습니다/);
  for (const id of ['correct-transcript','summarize-lecture','translate-lecture']) assert.match(html,new RegExp(`<button[^>]*id="${id}"[^>]*hidden`));
  assert.match(html,/<details id="legacy-ai-results"[^>]*>/);
  assert.match(html,/id="course-open"/);
  assert.match(html,/id="study-material-details"/);
  assert.match(css,/\.lesson-study-note \.summary-actions > \* \{ width: 100%/);
});

test('admin controls are hidden by default, accessible, and contain no embedded account data', () => {
  assert.match(html, /<button\b[^>]*\bid="admin-open"[^>]*\baria-haspopup="dialog"[^>]*\baria-controls="admin-dialog"[^>]*\bhidden/i);
  assert.match(html, /<dialog\b[^>]*\bid="admin-dialog"[^>]*\baria-labelledby="admin-title"[^>]*\baria-describedby="admin-description"/i);
  assert.match(html, /<p\b[^>]*\bid="admin-updated"[^>]*>/i);
  assert.doesNotMatch(html, /<p\b[^>]*\bid="admin-updated"[^>]*\baria-live="polite"/i);
  assert.match(html, /<p\b[^>]*\bid="admin-error"[^>]*\brole="alert"[^>]*\bhidden/i);
  assert.match(html, /<button\b[^>]*\bid="admin-access-toggle"[^>]*\bdisabled/i);
  assert.match(html, /<button\b[^>]*\bid="admin-tunnel-restart"[^>]*\bdisabled/i);
  assert.match(html, /<dialog\b[^>]*\bid="admin-confirm-dialog"[^>]*\baria-labelledby="admin-confirm-title"[^>]*\baria-describedby="admin-confirm-description"/i);
  assert.match(html, /<div\b[^>]*\bid="admin-accounts"[^>]*><\/div>/i);
  assert.match(html, /관리자 현황용 상태에는 IP, 기기 정보, 수업 제목과 내용을 저장하거나 표시하지 않습니다/);
  assert.doesNotMatch(html, /data-account-id|MINDLOGIC_API_KEY|OPENAI_API_KEY/i);
});
