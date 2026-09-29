// A shared, read-only list. Loading suggestions must never create review items.
export function timetableSubjects(response) {
  if (!response || !Object.hasOwn(response, 'timetable')) throw new Error('시간표 응답을 확인하지 못했어요.');
  if (response.timetable === null) return [];
  const rows = response.timetable?.classes;
  if (!Array.isArray(rows) || rows.length > 60 || rows.some(row =>
    typeof row?.subject !== 'string' || !row.subject.trim() || row.subject.length > 40)) {
    throw new Error('시간표 과목을 확인하지 못했어요.');
  }
  return [...new Set(rows.map(row => row.subject.trim()))].sort((a,b) => a.localeCompare(b,'ko'));
}

export function createTimetableCatalog({api, scopeKey, onChange = () => {}}) {
  let key = null, subjects = [], status = 'idle', controller = null, pending = null, sequence = 0;
  const snapshot = () => key === scopeKey() ? {subjects:[...subjects],status} : {subjects:[],status:'idle'};
  function reset() {
    ++sequence; controller?.abort(); controller = null; pending = null;
    key = null; subjects = []; status = 'idle'; onChange(snapshot());
  }
  function load({force = false} = {}) {
    const captured = scopeKey();
    if (key === captured && pending) return pending;
    if (!force && key === captured && status === 'ready') return Promise.resolve([...subjects]);
    controller?.abort(); const request = new AbortController(), run = ++sequence;
    controller = request; key = captured; subjects = []; status = 'loading'; onChange(snapshot());
    const valid = () => run === sequence && key === captured && scopeKey() === captured && !request.signal.aborted;
    pending = (async () => {
      try {
        const result = await Promise.resolve().then(() => valid() ? api('/review/timetable',{signal:request.signal}) : null);
        if (!valid()) return [];
        subjects = timetableSubjects(result); status = 'ready'; onChange(snapshot());
        return [...subjects];
      } catch {
        if (valid()) { subjects = []; status = 'unavailable'; onChange(snapshot()); }
        return [];
      } finally { if (run === sequence) { pending = null; controller = null; } }
    })();
    return pending;
  }
  return {snapshot,load,reset};
}

export function fillTimetableSelect(select, document, {subjects,status}) {
  const placeholder = document.createElement('option'); placeholder.value = '';
  placeholder.textContent = status === 'loading' ? '시간표 불러오는 중…' : subjects.length
    ? '시간표에서 과목 선택' : status === 'unavailable' ? '시간표를 불러오지 못했어요' : '등록된 시간표 과목이 없어요';
  select.replaceChildren(placeholder);
  for (const subject of subjects) {
    const option = document.createElement('option'); option.value = subject; option.textContent = subject; select.append(option);
  }
  select.value = ''; select.disabled = !subjects.length;
}
