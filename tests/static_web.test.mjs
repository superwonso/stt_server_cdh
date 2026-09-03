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
