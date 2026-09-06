// Aggregate-only presentation. Authentication, account scoping, and the refresh
// POST belong to the caller; this module never fetches or retains credentials.
const STATES = Object.freeze({
  disabled: ['사용 안 함', '이 서버에서 Google Drive 녹음 보관을 사용하지 않습니다.'],
  unconfigured: ['설정 필요', '운영자 PC에서 Google Drive 연결 설정을 확인하세요.'],
  unchecked: ['아직 확인 전', 'Google 연결 상태를 아직 확인하지 못했습니다.'],
  ready: ['연결 확인됨', 'Google 연결과 용량을 확인했습니다. 개별 녹음의 보관 완료는 아래 집계를 확인하세요.'],
  temporary_error: ['일시 확인 실패', '일시적인 연결 문제입니다. 이전 값이 있으면 참고용으로 남겨 둡니다.'],
  reauth_required: ['다시 인증 필요', '운영자 PC에서 기존 Google 계정으로 다시 인증해야 합니다.'],
  attention: ['운영자 확인 필요', '연결 설정·계정 일치 여부 등을 운영자 PC에서 확인하세요.'],
});

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
  // No arbitrary provider strings or locale-parsed dates in the DOM.
  if (typeof value !== 'string' || value.length > 40
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return '미확인';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '미확인';
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

/** Reset with null on logout/account changes; repeatedly rendering adds no listeners. */
export function renderDriveStatus(status, { document = globalThis.document, busy = false, onRefresh } = {}) {
  if (!document?.getElementById) return false;
  const get = (suffix) => document.getElementById(`admin-drive-${suffix}`);
  if (!get('panel')) return false;
  const text = (suffix, value) => {
    const node = get(suffix);
    if (node) node.textContent = value;
  };
  const value = object(status);
  const state = value.enabled === false ? 'disabled' : (value.reauth_required === true ? 'reauth_required'
    : (typeof value.state === 'string' && Object.hasOwn(STATES, value.state) ? value.state : 'unchecked'));
  const refreshing = value.refreshing === true;
  const stale = value.stale !== false;
  const badge = get('state');
  if (badge) {
    badge.textContent = refreshing ? '확인 중' : (state === 'ready' && stale ? '이전 확인값' : STATES[state][0]);
    badge.dataset.state = refreshing ? 'starting' : (state === 'ready' && stale ? 'stale' : state);
  }
  text('detail', value.reauth_required === true ? STATES.reauth_required[1] : STATES[state][1]);
  const validRemoteState = ['ready', 'temporary_error'].includes(state);
  const rawQuota = object(value.quota);
  const usage = count(rawQuota.usage_bytes);
  const limit = count(rawQuota.limit_bytes);
  const haveQuota = validRemoteState && usage !== null
    && (rawQuota.limit_bytes == null || limit !== null);
  const old = stale || state !== 'ready';
  text('cache-note', haveQuota && old
    ? '이전 조회 값입니다. 현재 용량과 다를 수 있어요.'
    : (refreshing ? 'Google 상태를 별도로 확인하고 있습니다. 녹음은 계속할 수 있어요.'
      : '이 화면 조회만으로 녹음을 옮기거나 삭제하지 않습니다.'));
  text('quota', haveQuota ? `${bytes(usage)} 사용 / ${limit === null ? '한도 미확인' : bytes(limit)}` : '용량 미확인');
  text('quota-detail', haveQuota
    ? `남은 용량 ${limit === null ? '미확인' : bytes(Math.max(0, limit - usage))} · Drive 사용 ${bytes(rawQuota.drive_bytes)} · 휴지통 ${bytes(rawQuota.trash_bytes)}`
    : '조회 실패나 미제공 값을 0으로 표시하지 않습니다.');
  const progress = get('progress');
  if (progress) {
    progress.hidden = !haveQuota || limit === null || limit === 0;
    progress.max = 100;
    progress.value = progress.hidden ? 0 : Math.min(100, usage / limit * 100);
  }
  const archive = object(value.archive);
  const unknownBytes = count(archive.pending_bytes_unknown_count);
  const pendingBytes = count(archive.pending_bytes);
  text('pending-bytes', pendingBytes === null ? '대기 크기 미확인'
    : (unknownBytes === 0 ? bytes(pendingBytes) : `확인된 ${bytes(pendingBytes)} + 미확인 크기`));
  text('pending-detail', unknownBytes === null ? '일부 녹음의 크기가 확인되지 않았을 수 있습니다.'
    : (unknownBytes > 0 ? `${unknownBytes}건의 크기를 확인하지 못했습니다. 위 값은 전체 대기량이 아닙니다.`
      : '전송·검증·확인 대기 WAV 전체 크기이며, 실제 남은 전송 바이트와는 다릅니다.'));
  const countText = (key) => count(archive[key]) === null ? '미확인' : `${archive[key]}건`;
  text('counts', `대기 ${countText('pending_count')} · 전송 중 ${countText('uploading_count')} · 확인 필요 ${countText('attention_count')} · Drive 보관 ${countText('ready_count')}`);
  text('cleanup', `서버 사본 정리 대기 ${countText('cleanup_pending_count')} · 수업 삭제 대기 ${countText('deleting_count')}`);
  const pendingCounts = ['pending_count', 'uploading_count', 'attention_count'].map((key) => count(archive[key]));
  const noPending = pendingCounts.every((number) => number === 0);
  const unknownTimes = count(archive.oldest_pending_unknown_count);
  const oldest = noPending ? '대기 없음' : timestamp(archive.oldest_pending_at);
  text('oldest', `가장 오래된 대기 시작: ${oldest}${unknownTimes > 0 ? ` · ${unknownTimes}건의 시작 시각 미확인` : ''}`);
  text('last-upload', `마지막 녹음 검증 완료: ${timestamp(archive.last_verified_upload_at)}`);
  text('checked', `마지막 조회: ${timestamp(value.checked_at)} · 마지막 정상 조회: ${timestamp(value.last_successful_check_at)} (한국 시간)`);
  const button = get('refresh');
  if (button) {
    button.disabled = busy === true || refreshing || value.enabled !== true || value.configured !== true
      || typeof onRefresh !== 'function';
    button.textContent = busy === true || refreshing ? '상태 확인 중…' : 'Drive 상태 확인';
    button.onclick = typeof onRefresh === 'function' ? () => {
      if (!button.disabled) return onRefresh();
      return undefined;
    } : null;
  }
  return true;
}
