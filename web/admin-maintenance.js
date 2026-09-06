// Read-only aggregate presentation. The authenticated caller owns fetching and
// account changes; this module neither sends requests nor retains status data.
const LEASE_STATES = Object.freeze({
  unknown: ['미확인', '자동 주소 갱신 상태를 아직 확인하지 못했습니다.'],
  disabled: ['사용 안 함', '자동 주소 갱신을 사용하지 않습니다.'],
  idle: ['첫 확인 대기', '현재 주소의 유효기간을 확인할 준비 중입니다.'],
  waiting: ['다음 갱신 대기', '확인된 기존 주소의 유효기간에 맞춰 다음 갱신을 기다립니다.'],
  renewing: ['갱신 중', '기존 주소의 새 유효기간을 게시하고 확인하고 있습니다.'],
  retrying: ['재시도 대기', '갱신을 완료하지 못해 다음 시도를 기다립니다.'],
  blocked: ['운영자 확인 필요', '안전하게 갱신할 조건을 확인하지 못했습니다. 운영자 PC에서 점검하세요.'],
  offline: ['오프라인', '공개 연결이 꺼져 있거나 갱신할 기존 주소가 없습니다.'],
  stopping: ['종료 중', '주소 갱신 작업을 종료하고 있습니다.'],
  stopped: ['작업 중지', '주소 갱신 작업이 중지되어 있습니다.'],
});

const BACKUP_ERRORS = Object.freeze({
  cancelled: '백업 작업이 취소되었습니다.',
  timeout: '백업 작업의 제한 시간을 초과했습니다.',
  unsafe_path: '백업 파일 경로의 안전성을 확인하지 못했습니다.',
  unsafe_directory: '백업 폴더의 안전성을 확인하지 못했습니다.',
  unsafe_file: '백업 파일의 안전성을 확인하지 못했습니다.',
  size_limit: '백업 크기 제한을 초과했습니다.',
  external_tool_failed: '백업에 필요한 도구를 실행하지 못했습니다.',
  account_mismatch: '백업 자료의 계정 연결을 확인하지 못했습니다.',
  configuration_changed: '작업 중 설정이 바뀌어 백업을 완료하지 않았습니다.',
  database_integrity: '데이터베이스 무결성을 확인하지 못했습니다.',
  database_foreign_keys: '데이터베이스 자료 간 연결을 확인하지 못했습니다.',
  missing_configuration: '필수 백업 설정이 없습니다.',
  incomplete_drive_credentials: 'Drive 복구에 필요한 연결 설정이 불완전합니다.',
  already_running: '다른 백업 작업이 진행 중입니다.',
  invalid_pending_bundle: '복사 대기 중인 암호화 파일을 확인하지 못했습니다.',
  pending_bundle_changed: '복사 대기 중인 암호화 파일이 바뀌었습니다.',
  destination_unavailable: '백업 목적지에 접근하지 못했습니다.',
  backup_failed: '백업을 완료하지 못했습니다. 운영자 PC에서 확인하세요.',
  copy_verification_failed: '목적지에 복사된 암호화 파일의 검증을 통과하지 못했습니다.',
  invalid_configuration: '백업 설정을 확인하지 못했습니다.',
});

const LEASE_ERRORS = Object.freeze({
  control_unavailable: '주소 갱신 제어를 준비하지 못했습니다.',
  worker_start_failed: '주소 갱신 작업을 시작하지 못했습니다.',
  offline: '현재 공개 연결이 꺼져 있습니다.',
  desired_missing: '갱신할 기존 공개 주소 정보를 확인하지 못했습니다.',
  desired_invalid: '기존 공개 주소 정보가 유효하지 않습니다.',
  unsafe_permissions: '주소 갱신에 필요한 파일의 안전성을 확인하지 못했습니다.',
  process_not_owned: '현재 서버·터널을 이 앱이 관리하는지 확인하지 못했습니다.',
  url_changed: '현재 연결과 게시된 주소가 일치하지 않습니다.',
  url_or_process_changed: '갱신 도중 연결 또는 관리 대상이 바뀌었습니다.',
  renewal_failed: '주소 갱신을 완료하지 못했습니다.',
  retry_wait: '재시도 간격이 지난 뒤 다시 갱신합니다.',
  publication_failed: '새 유효기간 게시를 확인하지 못했습니다.',
});

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function count(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function bytes(value) {
  if (count(value) === null) return '미확인';
  if (value < 1024) return `${value} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let amount = value / 1024;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(1)} ${units[unit]}`;
}

function timestamp(value) {
  // Epoch seconds only. No locale-parsed strings, arbitrary provider text, or
  // impossible dates; fractional seconds from the lease worker are allowed.
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 253402214399) return '미확인';
  const date = new Date(value * 1000);
  if (!Number.isFinite(date.getTime())) return '미확인';
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function expiry(value) {
  if (!Number.isSafeInteger(value)) return '미확인';
  // A negative lease countdown is an expired lease, not negative duration.
  if (value <= 0) return '만료됨';
  const days = Math.floor(value / 86400);
  const hours = Math.floor(value % 86400 / 3600);
  const minutes = Math.floor(value % 3600 / 60);
  if (days > 0) return `${days}일 ${hours}시간`;
  if (hours > 0) return `${hours}시간 ${minutes}분`;
  if (minutes > 0) return `${minutes}분`;
  return `${value}초`;
}

function errorText(value, errors) {
  if (value === null || value === '') return '보고된 오류 없음';
  if (value === undefined) return '미확인';
  return typeof value === 'string' && Object.hasOwn(errors, value)
    ? errors[value] : '작업 상태를 확인하지 못했습니다. 운영자 PC에서 확인하세요.';
}

/** Call with null statuses on logout/account changes to clear previous values. */
export function renderMaintenanceStatus({ document = globalThis.document, backup = null, lease = null } = {}) {
  if (!document?.getElementById) return false;
  const get = (id) => document.getElementById(`admin-${id}`);
  const text = (id, value) => {
    const node = get(id);
    if (node) node.textContent = value;
  };
  const badge = (id, state, label) => {
    const node = get(id);
    if (node) {
      node.textContent = label;
      node.dataset.state = state;
    }
  };
  const haveBackup = Boolean(get('backup-panel'));
  const haveLease = Boolean(get('lease-panel'));
  if (!haveBackup && !haveLease) return false;
  if (haveBackup) {
    const value = object(backup);
    const failures = count(value.failure_count);
    const error = value.last_error_code;
    const failed = (error !== undefined && error !== null && error !== '') || failures > 0;
    let state = 'unknown';
    let label = '미확인';
    let detail = '암호화 복구 백업 상태를 아직 확인하지 못했습니다.';
    if (value.running === true) {
      [state, label, detail] = ['running', '백업 작업 중', '암호화 파일 준비 또는 목적지 복사·검증을 진행하고 있습니다.'];
    } else if (failed) {
      [state, label, detail] = ['attention', '운영자 확인 필요', '최근 백업을 완료하지 못했습니다. 아래 상태를 확인하세요.'];
    } else if (value.configured === false) {
      [state, label, detail] = ['unconfigured', '설정 필요', '운영자 PC에서 백업 설정과 복구 키의 별도 보관을 준비해야 합니다.'];
    } else if (value.pending_copy === true) {
      [state, label, detail] = ['pending', '복사 대기', '암호화 파일이 목적지 복사·검증을 기다립니다. 아직 보관 완료가 아닙니다.'];
    } else if (value.configured === true && value.enabled === false) {
      [state, label, detail] = ['disabled', '예약 사용 안 함', '백업 설정은 있지만 예약 실행은 꺼져 있습니다.'];
    } else if (value.configured === true && value.enabled === true) {
      [state, label, detail] = ['waiting', '예약 대기', '예약 실행이 켜져 있습니다. 마지막 보관 완료 시각은 별도로 확인하세요.'];
    }
    badge('backup-state', state, label);
    text('backup-detail', detail);
    text('backup-schedule', value.enabled === true ? '사용 중' : (value.enabled === false ? '사용 안 함' : '미확인'));
    text('backup-pending', value.pending_copy === true ? '암호화 파일 대기 중'
      : (value.pending_copy === false ? '대기 없음' : '미확인'));
    text('backup-failures', failures === null ? '미확인' : `${failures}회`);
    text('backup-last-success', `마지막 검증 복사 완료: ${timestamp(value.last_success_at)} (한국 시간)`);
    text('backup-size', `마지막 완료 파일 크기: ${bytes(value.last_success_bytes)}`);
    text('backup-error', `최근 작업 안내: ${errorText(error, BACKUP_ERRORS)}`);
  }
  if (haveLease) {
    const value = object(lease);
    const state = value.enabled === false ? 'disabled'
      : (typeof value.state === 'string' && Object.hasOwn(LEASE_STATES, value.state) ? value.state : 'unknown');
    badge('lease-state', state, LEASE_STATES[state][0]);
    text('lease-detail', LEASE_STATES[state][1]);
    text('lease-enabled', value.enabled === true ? '사용 중' : (value.enabled === false ? '사용 안 함' : '미확인'));
    text('lease-expiry', expiry(value.expires_in_seconds));
    text('lease-running', value.renewing === true ? '작업 진행 중' : (value.renewing === false ? '진행 중인 작업 없음' : '미확인'));
    text('lease-last-attempt', `마지막 갱신 시도: ${timestamp(value.last_attempt_at)} (한국 시간)`);
    text('lease-last-success', `마지막 갱신 확인: ${timestamp(value.last_success_at)} (한국 시간)`);
    text('lease-error', `최근 작업 안내: ${errorText(value.error_code, LEASE_ERRORS)}`);
  }
  return true;
}
