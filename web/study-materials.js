// Private PDF/PPTX attachments. Every async boundary retains the selected owner/scope.
const MAX_BYTES = 32 * 1024 * 1024;
const PART_BYTES = 480 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ERRORS = {
  awaiting_upload:'파일 전송을 마쳐 주세요.', converting:'Markdown으로 변환하고 있습니다.',
  interrupted:'변환이 중단되었습니다. 원본을 확인한 뒤 다시 변환할 수 있습니다.',
  invalid_file:'자료 파일을 읽지 못했습니다. PDF 또는 PPTX 파일을 확인해 주세요.',
  limits_exceeded:'자료가 변환 크기·시간 제한을 넘었습니다. 파일을 나누어 주세요.',
  dependency_unavailable:'서버의 자료 변환 도구가 준비되지 않았습니다.',
  conversion_failed:'자료를 변환하지 못했습니다. 보관한 원본을 확인해 주세요.',
  source_changed:'자료 상태가 바뀌었습니다. 목록을 다시 확인해 주세요.',
};
class MaterialError extends Error {}
const cancelled = () => Object.assign(new Error('cancelled'), {name:'AbortError'});

export function validMaterial(row, scope) {
  return !!row && UUID.test(row.id || '') && ['pdf','pptx'].includes(row.kind)
    && typeof row.filename === 'string' && row.filename.length > 0 && row.filename.length <= 180
    && !/[\x00-\x1f\x7f/\\]/.test(row.filename)
    && row.filename.toLowerCase().endsWith('.' + row.kind)
    && row[scope.kind === 'lecture' ? 'lecture_id' : 'course_id'] === scope.id
    && row[scope.kind === 'lecture' ? 'course_id' : 'lecture_id'] === null
    && Number.isSafeInteger(row.size_bytes) && row.size_bytes > 0 && row.size_bytes <= MAX_BYTES
    && Number.isSafeInteger(row.uploaded_bytes) && row.uploaded_bytes >= 0 && row.uploaded_bytes <= row.size_bytes
    && Number.isSafeInteger(row.revision) && row.revision > 0
    && ['processing','ready','failed'].includes(row.status)
    && (row.error_code === null || Object.hasOwn(ERRORS,row.error_code))
    && (row.status !== 'ready' || (row.error_code === null && row.uploaded_bytes === row.size_bytes))
    && Number.isSafeInteger(row.unit_count) && row.unit_count >= 0 && row.unit_count <= 400
    && Number.isSafeInteger(row.warning_count) && row.warning_count >= 0 && row.warning_count <= 100;
}

function validateFile(file) {
  if (!(file instanceof Blob) || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > MAX_BYTES
      || typeof file.name !== 'string' || file.name !== file.name.trim() || file.name.length > 180
      || /[\x00-\x1f\x7f/\\]/.test(file.name) || !/\.(pdf|pptx)$/i.test(file.name)) {
    throw new MaterialError('PDF 또는 PPTX 파일을 선택해 주세요. 파일 하나는 32 MiB까지 올릴 수 있습니다.');
  }
}

async function sha256(blob, check) {
  const bytes = await blob.arrayBuffer(); check();
  const digest = await globalThis.crypto.subtle.digest('SHA-256',bytes); check();
  return Array.from(new Uint8Array(digest),byte => byte.toString(16).padStart(2,'0')).join('');
}

export function createMaterialPanel({container, api, scopeKey, onChanged = () => {}}) {
  if (!container?.ownerDocument || typeof api !== 'function' || typeof scopeKey !== 'function') throw new TypeError('Invalid material panel');
  const document = container.ownerDocument;
  let scope = null, owner = '', epoch = 0, dead = false, busy = false;
  let rows = [], chosen = null, resumeId = null, pendingReservation = null, message = '', preview = '';
  let pollTimer = null;
  const controllers = new Set(), objectUrls = new Set(), urlTimers = new Set();

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function capture() {
    return {scope:scope && {...scope}, owner, epoch};
  }
  function current(context) {
    return !dead && !!context.scope && epoch === context.epoch && owner === context.owner
      && !!owner && scopeKey() === owner && scope?.id === context.scope.id && scope?.kind === context.scope.kind;
  }
  function check(context) { if (!current(context)) throw cancelled(); }
  function endpoint(context) {
    return `/${context.scope.kind === 'lecture' ? 'lectures' : 'courses'}/${encodeURIComponent(context.scope.id)}/materials`;
  }
  async function request(context, path, options = {}) {
    check(context);
    const controller = new AbortController(); controllers.add(controller);
    try {
      const result = await api(path,{...options,signal:controller.signal},45000);
      check(context);
      return result;
    } finally { controllers.delete(controller); }
  }
  function accept(row, context, expectedId) {
    check(context);
    if (!validMaterial(row,context.scope) || (expectedId && row.id !== expectedId)) throw new MaterialError('자료 상태를 확인하지 못했습니다. 목록을 다시 불러와 주세요.');
    const index = rows.findIndex(item => item.id === row.id);
    if (index === -1) rows.push(row); else rows[index] = row;
    return row;
  }
  function schedule(context) {
    clearTimeout(pollTimer); pollTimer = null;
    if (!current(context) || !rows.some(row => row.error_code === 'converting')) return;
    pollTimer = setTimeout(() => {
      pollTimer = null;
      if (current(context) && !busy) void run(() => load(context),context);
      else if (current(context)) schedule(context);
    },1500);
  }
  async function load(context) {
    const result = await request(context,endpoint(context));
    if (!Array.isArray(result?.materials) || result.materials.length > 20
        || !result.materials.every(row => validMaterial(row,context.scope))
        || new Set(result.materials.map(row => row.id)).size !== result.materials.length) {
      throw new MaterialError('자료 목록을 확인하지 못했습니다. 다시 불러와 주세요.');
    }
    const signature = items => JSON.stringify(items.map(row => [row.id,row.revision,row.status,row.error_code,row.uploaded_bytes]));
    const updated = signature(rows) !== signature(result.materials);
    rows = result.materials;
    if (updated) changed(context);
    schedule(context);
  }
  async function run(action, context = capture()) {
    if (busy || !current(context)) return;
    busy = true; message = ''; render();
    try { await action(context); }
    catch (error) {
      if (current(context) && error?.name !== 'AbortError') {
        message = error instanceof MaterialError ? error.message
          : error?.status === 401 ? '로그인이 만료됐습니다. 다시 로그인해 주세요.'
          : '자료 요청을 마치지 못했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요.';
      }
    } finally {
      if (current(context)) { busy = false; render(); schedule(context); }
    }
  }
  function changed(context) { if (current(context)) onChanged({...context.scope}); }
  async function upload(context) {
    const file = chosen; validateFile(file);
    const previous = resumeId && rows.find(row => row.id === resumeId);
    if (resumeId && (!previous || previous.filename !== file.name || previous.size_bytes !== file.size)) {
      throw new MaterialError('전송을 이어가려면 처음 올린 것과 같은 이름·크기·내용의 파일을 선택해 주세요.');
    }
    message = '파일의 내용을 확인하고 있습니다.'; render();
    const hash = await sha256(file,() => check(context));
    const metadata = {filename:file.name,size_bytes:file.size,sha256:hash};
    const matchesPending = pendingReservation && Object.entries(metadata).every(([key,value]) => pendingReservation[key] === value);
    const id = previous?.id || (matchesPending ? pendingReservation.id : globalThis.crypto.randomUUID());
    pendingReservation = {id,...metadata};
    let state;
    try {
      state = await request(context,endpoint(context),{method:'POST',body:JSON.stringify(pendingReservation)});
    } catch (error) {
      if (previous && error?.status === 409) throw new MaterialError('처음 올린 파일과 내용이 다릅니다. 같은 원본 파일을 선택해 주세요.');
      throw error;
    }
    accept(state,context,id);
    if (state.filename !== file.name || state.size_bytes !== file.size) throw new MaterialError('파일 확인 정보가 일치하지 않습니다.');
    // Keep the reservation visible after interruption; reselection verifies its immutable hash.
    resumeId = id; pendingReservation = null; render(); changed(context);
    while (state.uploaded_bytes < file.size) {
      check(context);
      const offset = state.uploaded_bytes, end = Math.min(file.size,offset + PART_BYTES);
      const part = file.slice(offset,end,'application/octet-stream');
      const partHash = await sha256(part,() => check(context));
      state = await request(context,`/study-materials/${id}/content`,{method:'PUT',body:part,
        headers:{'Content-Type':'application/octet-stream','X-Upload-Offset':String(offset),'X-Part-SHA256':partHash}});
      accept(state,context,id);
      if (state.uploaded_bytes !== end || state.size_bytes !== file.size) throw new MaterialError('파일 전송 위치를 확인하지 못했습니다. 목록을 새로고침해 주세요.');
      message = `파일 전송 ${Math.floor(100 * end / file.size)}%`; render();
    }
    chosen = null; resumeId = null;
    message = '파일을 보관했습니다. 아래에서 Markdown으로 변환해 주세요.';
    changed(context);
  }
  async function convert(context, row) {
    accept(await request(context,`/study-materials/${row.id}/convert`,{method:'POST'}),context,row.id);
    message = '자료 변환을 요청했습니다.'; changed(context); schedule(context);
  }
  async function readDocument(context, row) {
    const result = await request(context,`/study-materials/${row.id}`);
    accept(result,context,row.id);
    if (result.status !== 'ready' || typeof result.document?.markdown !== 'string'
        || result.document.markdown.length > 1200000) throw new MaterialError('변환된 Markdown을 확인하지 못했습니다.');
    return result.document.markdown;
  }
  function saveBlob(context, blob, filename) {
    check(context);
    const url = URL.createObjectURL(blob); objectUrls.add(url);
    const link = node('a'); link.href = url;
    link.download = filename.replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g,'_').replace(/[. ]+$/,'') || 'lecture-material';
    container.append(link); link.click(); link.remove();
    const timer = setTimeout(() => { URL.revokeObjectURL(url); objectUrls.delete(url); urlTimers.delete(timer); },30000);
    timer.unref?.(); urlTimers.add(timer);
  }
  function button(text, action, name, disabled = false) {
    const result = node('button',text); result.type = 'button'; result.className = 'secondary-button'; result.dataset.action = name;
    result.disabled = busy || disabled;
    const context = capture();
    result.addEventListener('click',() => run(action,context));
    return result;
  }
  function render() {
    container.replaceChildren();
    if (!scope || dead) return;
    container.append(node('p','PDF·PPTX 자료를 보관하고 페이지·슬라이드별 Markdown으로 변환합니다. 파일 하나는 32 MiB까지 가능합니다.','muted'));
    container.append(node('p','그림·도표·스캔 내용은 추출하지 못할 수 있습니다. 원본 자료와 함께 확인해 주세요.','muted'));
    const pickerLabel = node('label','강의자료 파일 선택 '), picker = node('input'), renderContext = capture();
    picker.type = 'file'; picker.accept = '.pdf,.pptx'; picker.disabled = busy; picker.dataset.action = 'select-file';
    picker.addEventListener('change',() => {
      if (!current(renderContext) || busy) return;
      chosen = picker.files?.[0] || null; message = '';
      if (chosen) { try { validateFile(chosen); } catch (error) { chosen = null; message = error.message; } }
      render();
    });
    pickerLabel.append(picker); container.append(pickerLabel);
    if (chosen) container.append(node('p',`${resumeId ? '이어갈 파일' : '선택한 파일'}: ${chosen.name}`));
    const tools = node('div',undefined,'action-row');
    tools.append(button(resumeId ? '전송 이어가기' : '자료 업로드',upload,'upload',!chosen),
                 button('목록 새로고침',load,'refresh'));
    if (resumeId) tools.append(button('새 자료 선택',async () => { resumeId = null; chosen = null; },'new-file'));
    container.append(tools);
    const status = node('p',message); status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
    container.append(status);
    if (!rows.length) container.append(node('p',busy ? '자료를 확인하고 있습니다.' : '첨부한 자료가 없습니다.','muted'));
    for (const row of rows) {
      const card = node('article',undefined,'material-card'); card.dataset.materialId = row.id;
      card.append(node('h4',row.filename),node('p',row.status === 'ready' ? `${row.unit_count}페이지·슬라이드 변환 완료`
        : ERRORS[row.error_code] || '자료 상태를 확인해 주세요.'));
      if (row.warning_count) card.append(node('p','일부 내용의 추출을 확인하지 못했습니다. 원본 자료도 확인해 주세요.','muted'));
      const actions = node('div',undefined,'action-row');
      if (row.uploaded_bytes < row.size_bytes) {
        card.append(node('p',`파일 전송 ${Math.floor(100 * row.uploaded_bytes / row.size_bytes)}%`));
        actions.append(button('같은 파일로 전송 이어가기',async () => {
          resumeId = row.id; chosen = null;
          message = '위에서 처음 올린 것과 같은 파일을 다시 선택한 뒤 전송 이어가기를 눌러 주세요.';
        },'resume'));
      } else {
        actions.append(button('원본 다운로드',async context => {
          const blob = await request(context,`/study-materials/${row.id}/original`,{responseType:'material-file'});
          if (!(blob instanceof Blob) || blob.size !== row.size_bytes) throw new MaterialError('원본 파일의 크기를 확인하지 못했습니다.');
          saveBlob(context,blob,row.filename);
        },'original'));
        if (row.status !== 'ready') actions.append(button('Markdown으로 변환',context => convert(context,row),'convert',row.error_code === 'converting'));
      }
      if (row.status === 'ready') {
        actions.append(button('Markdown 보기',async context => { preview = await readDocument(context,row); },'preview'),
          button('Markdown 다운로드',async context => saveBlob(context,new Blob([await readDocument(context,row)],{type:'text/markdown;charset=utf-8'}),row.filename.replace(/\.(pdf|pptx)$/i,'.md')),'markdown'));
      }
      actions.append(button('자료 삭제',async context => {
        if (!document.defaultView?.confirm?.('보관한 원본 자료와 변환 결과를 삭제할까요?')) return;
        await request(context,`/study-materials/${row.id}`,{method:'DELETE'});
        rows = rows.filter(item => item.id !== row.id); preview = '';
        if (resumeId === row.id) { resumeId = null; chosen = null; }
        changed(context);
      },'delete',row.error_code === 'converting'));
      card.append(actions); container.append(card);
    }
    if (preview) {
      const details = node('details'); details.open = true;
      details.append(node('summary','변환된 Markdown'),node('pre',preview,'material-markdown'));
      container.append(details);
    }
  }
  function reset() {
    epoch++; clearTimeout(pollTimer); pollTimer = null;
    for (const controller of controllers) controller.abort(); controllers.clear();
    for (const timer of urlTimers) clearTimeout(timer); urlTimers.clear();
    for (const url of objectUrls) URL.revokeObjectURL(url); objectUrls.clear();
    scope = null; owner = ''; rows = []; chosen = null; resumeId = null; pendingReservation = null;
    busy = false; message = ''; preview = ''; render();
  }
  return {
    async setScope(next) {
      reset();
      if (dead || !next || !['lecture','course'].includes(next.kind) || !UUID.test(next.id || '')) return;
      const key = scopeKey(); if (typeof key !== 'string' || !key) return;
      scope = {kind:next.kind,id:next.id}; owner = key;
      return run(load);
    },
    refresh() { return run(load); },
    reset,
    destroy() { reset(); dead = true; },
  };
}
