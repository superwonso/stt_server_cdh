import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const app = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');

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
  const connectSources = html.match(/connect-src ([^;]+)/)[1].trim().split(/\s+/);
  assert.ok(connectSources.includes("'self'"));
  assert.ok(connectSources.includes('https://*.trycloudflare.com'));
  assert.ok(connectSources.includes('http://localhost:*'));
  assert.ok(connectSources.includes('http://127.0.0.1:*'));
  assert.ok(!connectSources.includes('https:'), 'arbitrary HTTPS destinations must not be allowed');
  assert.ok(!connectSources.includes('http:'), 'arbitrary HTTP destinations must not be allowed');
  assert.match(html, /name="referrer" content="no-referrer"/);
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

test('history, export, recording, and destructive controls are explicit and accessible', () => {
  assert.match(html, /<select\b[^>]*\bid="lecture-date"[^>]*\baria-label="수업 날짜별 보기"/i);
  assert.match(html, /<option value="">전체 날짜<\/option>/);
  assert.match(html, /<option value="markdown">Markdown \(\.md\)<\/option>/);
  assert.match(html, /<option value="text" selected>일반 텍스트 \(\.txt\)<\/option>/);
  assert.match(html, /<div\b[^>]*\bclass="note-actions"[^>]*\brole="group"[^>]*\baria-label="현재 수업 작업"/i);
  assert.match(html, /<dialog\b[^>]*\bid="delete-dialog"[^>]*\baria-labelledby="delete-title"[^>]*\baria-describedby="delete-description"/i);
  assert.match(html, /받아쓴 원문, AI 후보정본과 저장된 녹음/);
  assert.match(html, /이 작업은 되돌릴 수 없습니다/);
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
