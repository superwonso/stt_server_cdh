import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { renderMaintenanceStatus } from '../web/admin-maintenance.js';

const BACKUP_IDS = ['panel', 'state', 'detail', 'schedule', 'pending', 'failures', 'last-success', 'size', 'error'];
const LEASE_IDS = ['panel', 'state', 'detail', 'enabled', 'expiry', 'running', 'last-attempt', 'last-success', 'error'];
const ALL_IDS = [...BACKUP_IDS.map((id) => `backup-${id}`), ...LEASE_IDS.map((id) => `lease-${id}`)];
const EPOCH = Date.UTC(2026, 8, 6, 0, 0, 0) / 1000;

function fixture() {
  const nodes = new Map(ALL_IDS.map((id) => [`admin-${id}`, {
    textContent: '', dataset: {},
    set innerHTML(_value) { throw new Error('HTML insertion is forbidden'); },
    addEventListener() { throw new Error('Maintenance is status-only'); },
  }]));
  return {
    document: { getElementById: (id) => nodes.get(id) ?? null },
    get: (id) => nodes.get(`admin-${id}`),
    remove: (id) => nodes.delete(`admin-${id}`),
    text: () => [...nodes.values()].map((node) => node.textContent).join('\n'),
  };
}

function backup(changes = {}) {
  return { configured: true, enabled: true, running: false, pending_copy: false, failure_count: 0,
    last_success_at: EPOCH, last_success_bytes: 4096, last_error_code: null, ...changes };
}

function lease(changes = {}) {
  return { enabled: true, state: 'waiting', expires_in_seconds: 90061, last_attempt_at: EPOCH - 60,
    last_success_at: EPOCH + 0.125, renewing: false, error_code: '', ...changes };
}

test('backup schedule is separate from completed copy history and file size', () => {
  const f = fixture();
  assert.equal(renderMaintenanceStatus({ document: f.document, backup: backup(), lease: lease() }), true);
  assert.equal(f.get('backup-state').textContent, '예약 대기');
  assert.equal(f.get('backup-schedule').textContent, '사용 중');
  assert.equal(f.get('backup-pending').textContent, '대기 없음');
  assert.equal(f.get('backup-failures').textContent, '0회');
  assert.match(f.get('backup-last-success').textContent, /09:00.*한국 시간/);
  assert.equal(f.get('backup-size').textContent, '마지막 완료 파일 크기: 4.0 KiB');
  assert.match(f.get('backup-detail').textContent, /마지막 보관 완료 시각은 별도로/);
  assert.match(f.get('backup-error').textContent, /보고된 오류 없음/);
});

test('new or disabled backup configuration does not imply a completed backup', () => {
  const f = fixture();
  renderMaintenanceStatus({ document: f.document, backup: { configured: false, enabled: false, running: false } });
  assert.equal(f.get('backup-state').textContent, '설정 필요');
  assert.match(f.get('backup-last-success').textContent, /미확인/);
  assert.match(f.get('backup-failures').textContent, /미확인/);
  renderMaintenanceStatus({ document: f.document, backup: backup({ enabled: false }) });
  assert.equal(f.get('backup-state').textContent, '예약 사용 안 함');
  assert.match(f.get('backup-last-success').textContent, /09:00/);
});

test('running, pending copy and failures remain distinguishable from prior success', () => {
  const f = fixture();
  renderMaintenanceStatus({ document: f.document, backup: backup({ pending_copy: true, enabled: false }) });
  assert.equal(f.get('backup-state').textContent, '복사 대기');
  assert.equal(f.get('backup-schedule').textContent, '사용 안 함');
  assert.match(f.get('backup-detail').textContent, /아직 보관 완료가 아닙니다/);
  renderMaintenanceStatus({ document: f.document, backup: backup({ pending_copy: true,
    failure_count: 2, last_error_code: 'destination_unavailable' }) });
  assert.equal(f.get('backup-state').textContent, '운영자 확인 필요');
  assert.equal(f.get('backup-failures').textContent, '2회');
  assert.match(f.get('backup-error').textContent, /목적지에 접근하지 못했습니다/);
  assert.match(f.get('backup-last-success').textContent, /09:00/);
  renderMaintenanceStatus({ document: f.document, backup: backup({ running: true,
    failure_count: 2, last_error_code: 'timeout' }) });
  assert.equal(f.get('backup-state').textContent, '백업 작업 중');
  assert.equal(f.get('backup-failures').textContent, '2회');
  assert.match(f.get('backup-error').textContent, /제한 시간을 초과/);
});

test('configuration errors take precedence over setup-needed or disabled states', () => {
  const f = fixture();
  renderMaintenanceStatus({ document: f.document, backup: {
    configured: false, enabled: false, running: false, last_error_code: 'invalid_configuration',
  } });
  assert.equal(f.get('backup-state').textContent, '운영자 확인 필요');
  assert.match(f.get('backup-error').textContent, /백업 설정을 확인하지 못했습니다/);
});

test('lease countdown is a snapshot and epoch timestamps allow fractional seconds', () => {
  const f = fixture();
  renderMaintenanceStatus({ document: f.document, lease: lease() });
  assert.equal(f.get('lease-state').textContent, '다음 갱신 대기');
  assert.equal(f.get('lease-expiry').textContent, '1일 1시간');
  assert.equal(f.get('lease-running').textContent, '진행 중인 작업 없음');
  assert.match(f.get('lease-last-attempt').textContent, /08:59/);
  assert.match(f.get('lease-last-success').textContent, /09:00/);
  assert.match(f.get('lease-error').textContent, /보고된 오류 없음/);
  for (const [seconds, label] of [[3660, '1시간 1분'], [61, '1분'], [1, '1초'], [0, '만료됨'], [-1, '만료됨']]) {
    renderMaintenanceStatus({ document: f.document, lease: lease({ expires_in_seconds: seconds }) });
    assert.equal(f.get('lease-expiry').textContent, label);
  }
});

test('all backend lease states are allowlisted and state descriptions are not provider text', () => {
  const f = fixture();
  for (const state of ['idle', 'waiting', 'renewing', 'retrying', 'blocked', 'offline', 'stopping', 'stopped', 'disabled']) {
    renderMaintenanceStatus({ document: f.document, lease: lease({ state, renewing: state === 'renewing' }) });
    assert.equal(f.get('lease-state').dataset.state, state);
    assert.notEqual(f.get('lease-state').textContent, '미확인');
    if (state === 'renewing') assert.equal(f.get('lease-running').textContent, '작업 진행 중');
  }
  renderMaintenanceStatus({ document: f.document, lease: lease({ state: 'renewing', enabled: false }) });
  assert.equal(f.get('lease-state').dataset.state, 'disabled');
});

test('known lease errors are translated without displaying code or private context', () => {
  const f = fixture();
  for (const code of ['control_unavailable', 'worker_start_failed', 'offline', 'desired_missing', 'desired_invalid',
    'unsafe_permissions', 'process_not_owned', 'url_changed', 'url_or_process_changed', 'renewal_failed',
    'retry_wait', 'publication_failed']) {
    renderMaintenanceStatus({ document: f.document, lease: lease({ state: 'blocked', error_code: code }) });
    assert.doesNotMatch(f.get('lease-error').textContent, /미확인|작업 상태를 확인하지/);
    assert.ok(!f.get('lease-error').textContent.includes(code));
  }
});

test('known backup errors never reflect raw machine codes or false success', () => {
  const f = fixture();
  const source = readFileSync(new URL('../server/recovery_backup.py', import.meta.url), 'utf8');
  const definition = source.match(/PUBLIC_ERROR_CODES = frozenset\(\{([\s\S]+?)\}\)/)?.[1];
  assert.ok(definition);
  const codes = [...definition.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]);
  assert.ok(codes.length > 15);
  for (const code of codes) {
    renderMaintenanceStatus({ document: f.document, backup: backup({ last_error_code: code }) });
    assert.equal(f.get('backup-state').dataset.state, 'attention');
    assert.doesNotMatch(f.get('backup-error').textContent, /미확인|작업 상태를 확인하지/);
    assert.ok(!f.get('backup-error').textContent.includes(code));
  }
});

test('invalid counts, bytes and countdowns cannot display unsafe or coerced numbers', () => {
  for (const invalid of [undefined, null, true, '1', {}, [], Infinity, NaN, 2 ** 53, 1.5]) {
    const f = fixture();
    renderMaintenanceStatus({ document: f.document,
      backup: backup({ failure_count: invalid, last_success_bytes: invalid }),
      lease: lease({ expires_in_seconds: invalid }) });
    assert.equal(f.get('backup-failures').textContent, '미확인');
    assert.match(f.get('backup-size').textContent, /미확인/);
    assert.equal(f.get('lease-expiry').textContent, '미확인');
    assert.doesNotMatch(f.text(), /NaN|Infinity|\[object/);
  }
  const f = fixture();
  renderMaintenanceStatus({ document: f.document, backup: backup({ failure_count: -1, last_success_bytes: -1 }) });
  assert.equal(f.get('backup-failures').textContent, '미확인');
  assert.match(f.get('backup-size').textContent, /미확인/);
});

test('only bounded numeric epoch seconds are formatted as dates', () => {
  for (const invalid of [undefined, null, true, '2026-09-06T00:00:00Z', {}, [], -1, Infinity, NaN, 253402214400, 2 ** 53]) {
    const f = fixture();
    renderMaintenanceStatus({ document: f.document, backup: backup({ last_success_at: invalid }),
      lease: lease({ last_attempt_at: invalid, last_success_at: invalid }) });
    for (const id of ['backup-last-success', 'lease-last-success', 'lease-last-attempt']) {
      assert.match(f.get(id).textContent, /미확인/);
    }
    assert.doesNotMatch(f.text(), /Invalid Date|NaN|Infinity/);
  }
});

test('reset clears all earlier success, failure and working state across account changes', () => {
  const f = fixture();
  renderMaintenanceStatus({ document: f.document, backup: backup({ running: true, failure_count: 3 }),
    lease: lease({ state: 'renewing', renewing: true }) });
  renderMaintenanceStatus({ document: f.document, backup: null, lease: null });
  assert.equal(f.get('backup-state').dataset.state, 'unknown');
  assert.equal(f.get('lease-state').dataset.state, 'unknown');
  assert.doesNotMatch(f.text(), /2026|09:00|4\.0 KiB|3회|1일|작업 진행 중/);
  for (const prefix of ['backup', 'lease']) assert.match(f.get(`${prefix}-last-success`).textContent, /미확인/);
});

test('private metadata, unknown codes, prototype names and hostile text never enter the DOM', () => {
  const f = fixture();
  const privateText = '<img src=x onerror=alert(1)>PRIVATE https://internal.invalid private-user /private/path SECRET';
  for (const bad of [privateText, '__proto__', 'constructor', { toString() { throw new Error('No coercion'); } }]) {
    renderMaintenanceStatus({ document: f.document,
      backup: backup({ configured: bad, enabled: bad, running: bad, pending_copy: bad,
        last_error_code: bad, failure_count: bad, last_success_at: bad, last_success_bytes: bad,
        recipient: privateText, path: privateText, account: privateText }),
      lease: lease({ state: bad, error_code: bad, enabled: bad, renewing: bad, expires_in_seconds: bad,
        last_attempt_at: bad, last_success_at: bad, api_url: privateText, token: privateText }) });
    assert.doesNotMatch(f.text(), /PRIVATE|onerror|SECRET|internal|private-user|private\/path|__proto__|constructor/);
    assert.equal(f.get('lease-state').dataset.state, 'unknown');
  }
});

test('missing documents or panels are safe and malformed status objects reset independently', () => {
  assert.equal(renderMaintenanceStatus({ document: null }), false);
  assert.equal(renderMaintenanceStatus({ document: { getElementById() { return null; } } }), false);
  const f = fixture();
  f.remove('backup-panel');
  assert.equal(renderMaintenanceStatus({ document: f.document, lease: lease() }), true);
  assert.equal(f.get('lease-state').dataset.state, 'waiting');
  for (const value of [undefined, null, 'unknown', true, []]) {
    renderMaintenanceStatus({ document: f.document, backup: value, lease: value });
    assert.equal(f.get('lease-state').dataset.state, 'unknown');
    assert.match(f.get('lease-last-success').textContent, /미확인/);
  }
});

test('maintenance targets exist once inside admin dialog with no secret or recovery controls', () => {
  const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  const admin = html.slice(html.indexOf('<dialog id="admin-dialog"'), html.indexOf('<dialog id="admin-confirm-dialog"'));
  for (const id of ALL_IDS) {
    const attribute = `id="admin-${id}"`;
    assert.equal(html.split(attribute).length - 1, 1, attribute);
    assert.ok(admin.includes(attribute), attribute);
  }
  for (const prefix of ['backup', 'lease']) {
    const section = admin.match(new RegExp(`<section id="admin-${prefix}-panel"[\\s\\S]+?</section>`))?.[0];
    assert.ok(section);
    assert.doesNotMatch(section, /<(?:button|input|a|form)\b/);
  }
  assert.match(admin, /Drive의 녹음 파일 보관과는 별개/);
  assert.match(admin, /복구 키는 암호화 백업과 분리/);
  assert.match(admin, /조회 시점 만료까지/);
  assert.match(admin, /꺼진 서버·터널을 다시 켜거나/);
});
