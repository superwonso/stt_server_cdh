import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { renderDriveStatus } from '../web/admin-storage.js';

const SUFFIXES = ['panel', 'state', 'detail', 'cache-note', 'quota', 'quota-detail', 'progress',
  'pending-bytes', 'pending-detail', 'counts', 'cleanup', 'oldest', 'last-upload', 'checked', 'refresh'];

function fixture() {
  const nodes = new Map(SUFFIXES.map((suffix) => [`admin-drive-${suffix}`, {
    textContent: '', dataset: {}, hidden: false, disabled: false, onclick: null,
    set innerHTML(_value) { throw new Error('HTML insertion is forbidden'); },
  }]));
  const document = { getElementById: (id) => nodes.get(id) ?? null };
  return { document, get: (suffix) => nodes.get(`admin-drive-${suffix}`),
    text: () => [...nodes.values()].map((node) => node.textContent).join('\n') };
}

function status(changes = {}) {
  return {
    enabled: true, configured: true, state: 'ready', refreshing: false, stale: false,
    reauth_required: false, checked_at: '2026-09-06T00:01:00.000Z',
    last_successful_check_at: '2026-09-06T00:01:00.000Z',
    quota: { limit_bytes: 2048, usage_bytes: 1024, drive_bytes: 256, trash_bytes: 32, remaining_bytes: 1024 },
    archive: { pending_count: 1, uploading_count: 2, attention_count: 0, ready_count: 4,
      cleanup_pending_count: 1, deleting_count: 0, pending_bytes: 4096, pending_bytes_unknown_count: 0,
      oldest_pending_at: '2026-09-05T23:00:00Z', oldest_pending_unknown_count: 0,
      last_verified_upload_at: '2026-09-06T00:00:00Z' },
    ...changes,
  };
}

test('Drive panel shows Google-wide usage separately from Drive usage and pending WAV bytes', () => {
  const f = fixture();
  assert.equal(renderDriveStatus(status(), { document: f.document, onRefresh() {} }), true);
  assert.equal(f.get('state').textContent, '연결 확인됨');
  assert.equal(f.get('quota').textContent, '1.0 KiB 사용 / 2.0 KiB');
  assert.match(f.get('quota-detail').textContent, /남은 용량 1\.0 KiB · Drive 사용 256 B/);
  assert.equal(f.get('progress').value, 50);
  assert.equal(f.get('progress').hidden, false);
  assert.equal(f.get('pending-bytes').textContent, '4.0 KiB');
  assert.match(f.get('pending-detail').textContent, /실제 남은 전송 바이트와는 다릅니다/);
  assert.match(f.get('counts').textContent, /대기 1건 · 전송 중 2건 · 확인 필요 0건 · Drive 보관 4건/);
  assert.match(f.get('cleanup').textContent, /서버 사본 정리 대기 1건 · 수업 삭제 대기 0건/);
  assert.match(f.get('oldest').textContent, /08:00/);
  assert.match(f.get('last-upload').textContent, /09:00/);
  assert.match(f.get('checked').textContent, /한국 시간/);
});

test('absent quota limit is unknown rather than zero and never creates a percentage', () => {
  const f = fixture();
  renderDriveStatus(status({ quota: { usage_bytes: 100, limit_bytes: null } }), { document: f.document });
  assert.equal(f.get('quota').textContent, '100 B 사용 / 한도 미확인');
  assert.match(f.get('quota-detail').textContent, /남은 용량 미확인/);
  assert.equal(f.get('progress').hidden, true);
  assert.equal(f.get('progress').value, 0);
});

test('capacity zero and exceeded capacity remain finite and supplied remaining bytes are not trusted', () => {
  const f = fixture();
  for (const limit of [0, 50]) {
    renderDriveStatus(status({ quota: { usage_bytes: 100, limit_bytes: limit, remaining_bytes: 999999 } }), { document: f.document });
    assert.match(f.get('quota-detail').textContent, /남은 용량 0 B/);
    assert.ok(Number.isFinite(f.get('progress').value));
    assert.ok(f.get('progress').value <= 100);
  }
});

test('malformed byte fields do not display NaN, Infinity or fictitious zero capacity', () => {
  for (const usage of [undefined, null, '500', true, -1, Infinity, NaN, 2 ** 53]) {
    const f = fixture();
    renderDriveStatus(status({ quota: { usage_bytes: usage, limit_bytes: 1000 } }), { document: f.document });
    assert.equal(f.get('quota').textContent, '용량 미확인');
    assert.equal(f.get('progress').hidden, true);
    assert.doesNotMatch(f.text(), /NaN|Infinity/);
  }
});

test('stale success and temporary failures label cached quota as historical', () => {
  const f = fixture();
  for (const state of ['ready', 'temporary_error']) {
    renderDriveStatus(status({ state, stale: true }), { document: f.document });
    assert.match(f.get('cache-note').textContent, /이전 조회 값.*현재 용량과 다를/);
    assert.notEqual(f.get('state').dataset.state, 'ready');
    assert.match(f.get('checked').textContent, /마지막 정상 조회/);
  }
});

test('reauthorization and binding attention do not show a previous account quota', () => {
  const f = fixture();
  for (const state of ['reauth_required', 'attention']) {
    renderDriveStatus(status({ state, reauth_required: state === 'reauth_required' }), { document: f.document });
    assert.equal(f.get('quota').textContent, '용량 미확인');
    assert.equal(f.get('progress').hidden, true);
  }
  renderDriveStatus(status({ state: 'ready', reauth_required: true }), { document: f.document });
  assert.equal(f.get('state').textContent, '다시 인증 필요');
  assert.match(f.get('detail').textContent, /기존 Google 계정으로 다시 인증/);
});

test('unknown pending byte and time counts never imply a complete total', () => {
  const f = fixture();
  const value = status();
  value.archive.pending_bytes_unknown_count = 2;
  value.archive.oldest_pending_unknown_count = 1;
  renderDriveStatus(value, { document: f.document });
  assert.equal(f.get('pending-bytes').textContent, '확인된 4.0 KiB + 미확인 크기');
  assert.match(f.get('pending-detail').textContent, /2건.*전체 대기량이 아닙니다/);
  assert.match(f.get('oldest').textContent, /1건의 시작 시각 미확인/);
});

test('an empty queue is different from unknown history and retained last success', () => {
  const f = fixture();
  const value = status();
  Object.assign(value.archive, { pending_count: 0, uploading_count: 0, attention_count: 0,
    pending_bytes: 0, oldest_pending_at: null });
  renderDriveStatus(value, { document: f.document });
  assert.match(f.get('oldest').textContent, /대기 없음/);
  assert.match(f.get('last-upload').textContent, /09:00/);
});

test('reset clears all earlier aggregate data and removes the refresh callback', () => {
  const f = fixture();
  renderDriveStatus(status(), { document: f.document, onRefresh() {} });
  renderDriveStatus(null, { document: f.document });
  assert.equal(f.get('quota').textContent, '용량 미확인');
  assert.equal(f.get('pending-bytes').textContent, '대기 크기 미확인');
  assert.match(f.get('last-upload').textContent, /미확인/);
  assert.doesNotMatch(f.text(), /2026|4\.0 KiB|Drive 보관 4건/);
  assert.equal(f.get('refresh').disabled, true);
  assert.equal(f.get('refresh').onclick, null);
});

test('refresh is callback-only and repeated renders never accumulate handlers', () => {
  const f = fixture();
  const called = [];
  renderDriveStatus(status(), { document: f.document, onRefresh: () => called.push('old') });
  renderDriveStatus(status(), { document: f.document, onRefresh: () => called.push('new') });
  f.get('refresh').onclick();
  assert.deepEqual(called, ['new']);
  for (const options of [{ busy: true }, { value: { refreshing: true } }, { value: { enabled: false } }, { value: { configured: false } }]) {
    renderDriveStatus(status(options.value), { document: f.document, busy: options.busy, onRefresh: () => called.push('blocked') });
    assert.equal(f.get('refresh').disabled, true);
    f.get('refresh').onclick();
  }
  assert.deepEqual(called, ['new']);
});

test('untrusted text, private fields and malformed timestamps never reach text or markup', () => {
  const f = fixture();
  const marker = '<img src=x onerror=alert(1)>PRIVATE';
  const value = status({ state: marker, checked_at: marker, last_successful_check_at: marker,
    email: marker, folder_id: marker, message: marker });
  Object.assign(value.archive, { last_verified_upload_at: marker, oldest_pending_at: marker, ready_count: marker });
  renderDriveStatus(value, { document: f.document });
  assert.doesNotMatch(f.text(), /PRIVATE|onerror|img src/);
  assert.equal(f.get('state').dataset.state, 'unchecked');
  renderDriveStatus(status({ state: '__proto__' }), { document: f.document });
  assert.equal(f.get('state').dataset.state, 'unchecked');
});

test('missing documents or panels are a harmless no-op', () => {
  assert.equal(renderDriveStatus(null, { document: null }), false);
  assert.equal(renderDriveStatus(null, { document: { getElementById() { return null; } } }), false);
});

test('all rendering targets exist once and are inside the existing administrator dialog', () => {
  const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  const dialog = html.slice(html.indexOf('<dialog id="admin-dialog"'), html.indexOf('<dialog id="admin-confirm-dialog"'));
  for (const suffix of SUFFIXES) {
    const id = `id="admin-drive-${suffix}"`;
    assert.equal(html.split(id).length - 1, 1, id);
    assert.ok(dialog.includes(id), id);
  }
  assert.match(dialog, /Google 계정 전체 공유 용량/);
  assert.match(dialog, /Gmail 등 Google 서비스/);
  assert.match(dialog, /서버 WAV 사본을 정리/);
});
