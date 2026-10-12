// The child owns local learning data. Only this parent makes authenticated API
// calls; neither the bearer token nor the API address is sent to the child.
export const STUDY_CHANNEL = 'yeobaek-study-v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const MAX_REQUESTS = 512;
const SAFE_ERRORS = new Set([
  '서버가 다시 시작되어 이전 요청을 확인할 수 없어요. 자동으로 재전송하지 않습니다.',
  '같은 요청 번호의 내용은 변경할 수 없어요. 새 요청을 시작해 주세요.',
  '이 요청을 처리하고 있어요. 잠시 후 같은 요청으로 결과를 확인해 주세요.',
  'AI 문제 만들기는 한 시간에 6회까지 사용할 수 있어요.',
  'AI 요청 보관 한도에 도달했어요. 운영자에게 문의해 주세요. 기존 결과는 확인할 수 있어요.',
]);

export async function studyScope(owner, crypto = globalThis.crypto) {
  if (typeof owner !== 'string' || !owner || owner.length > 128 || !crypto?.subtle) throw Error('Unavailable scope');
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('yeobaek/stt_server_cdh/' + owner));
  return [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, '0')).join('');
}

function safeError(error) {
  if (SAFE_ERRORS.has(error?.message)) return error.message;
  return ({
    401:'로그인이 만료됐어요. 다시 로그인해 주세요.',
    403:'AI 문제 만들기 권한을 확인해 주세요.',
    404:'이 서버에서 AI 문제 만들기를 사용할 수 없어요. 연결을 확인해 주세요.',
    409:'요청이 처리 중이거나 이전 상태가 바뀌었어요. 같은 요청으로 다시 확인해 주세요.',
    413:'원문이 너무 길어요. 24,000자 이하로 나눠 주세요.',
    422:'문제 생성 결과나 입력 형식을 확인하지 못했어요. 원문을 확인해 주세요.',
    429:'AI 문제 만들기 요청이 많아요. 잠시 후 다시 시도해 주세요.',
    502:'AI 서비스와 통신하지 못했어요. 같은 요청으로 결과를 다시 확인해 주세요.',
    503:'AI 문제 만들기를 잠시 사용할 수 없어요. 문제·답 파일은 직접 가져올 수 있어요.',
    504:'AI 응답 시간이 초과됐어요. 이 요청을 자동으로 다시 전송하지 않아요.',
  })[error?.status] || '아직 결과를 확인하지 못했어요. 같은 요청으로 다시 확인해 주세요.';
}

function validResult(value, requestId, count) {
  const text = (item, limit, empty = false) => typeof item === 'string' && item.length <= limit && (empty || !!item.trim());
  if (value?.request_id !== requestId || value?.status !== 'completed' || !Array.isArray(value.questions)
    || !value.questions.length || value.questions.length > count || !Array.isArray(value.warnings)
    || value.warnings.length > 8 || !value.warnings.every(item => text(item, 400))) return false;
  return value.questions.every(row => row && text(row.chapter,120) && text(row.sub,240,true)
    && Number.isInteger(row.number) && row.number > 0 && row.number <= 30
    && text(row.question,2000) && text(row.answer,1000) && ['short','multi','sequence'].includes(row.type)
    && (row.source_excerpt === undefined || text(row.source_excerpt,400,true))
    && (row.source_page === undefined || row.source_page === null || Number.isInteger(row.source_page) && row.source_page > 0));
}

export function createStudyWorkspace({container, api, scopeKey, getOwner, onInteraction = () => {}}) {
  const document = container.ownerDocument, win = document.defaultView || globalThis;
  const origin = win.location.origin;
  let frame = null, status = null, context = null, visible = false, destroyed = false, sequence = 0, scope = null;
  let capabilities = null;
  const requests = new Map(), controllers = new Set();
  const current = (expected = context, revision = sequence) => !destroyed && revision === sequence
    && expected !== null && expected === scopeKey() && !!getOwner();
  const send = data => {
    if (frame?.contentWindow && current()) frame.contentWindow.postMessage({channel:STUDY_CHANNEL, ...data}, origin);
  };
  const sendContext = () => send({type:'context', scope, active:visible && document.visibilityState !== 'hidden'});
  const sendError = (requestId, message) => send({type:'generated', requestId, error:message});

  async function generate(message) {
    const {text, count, requestId, consent} = message;
    if (!UUID.test(requestId)) return;
    if (!visible || document.visibilityState === 'hidden') {
      sendError(requestId, '암기 학습 탭을 연 뒤 다시 시도해 주세요.'); return;
    }
    if (consent !== true || typeof text !== 'string' || !text.trim() || text.length > 24000
      || !Number.isInteger(count) || count < 1 || count > 30) {
      sendError(requestId, '외부 AI 전송에 동의하고 원문 24,000자 이하, 문제 1~30개로 입력해 주세요.'); return;
    }
    const expected = context, revision = sequence;
    try {
      const hash = await win.crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([text,count])));
      const fingerprint = [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2,'0')).join('');
      if (!current(expected, revision)) return;
      let receipt = requests.get(requestId);
      if (receipt && receipt.fingerprint !== fingerprint) {
        sendError(requestId, '같은 요청 번호의 내용은 변경할 수 없어요. 새 요청을 시작해 주세요.'); return;
      }
      if (receipt?.result) { send(receipt.result); return; }
      if (receipt?.running) return;
      if ([...requests.values()].some(item => item.running)) {
        sendError(requestId, '다른 문제 만들기가 진행 중이에요. 결과를 받은 뒤 다시 시도해 주세요.'); return;
      }
      if (!receipt) {
        if (requests.size >= MAX_REQUESTS) { sendError(requestId, '요청 보관 한도에 도달했어요. 다시 로그인해 주세요.'); return; }
        receipt = {fingerprint, epoch:null, result:null, running:false}; requests.set(requestId, receipt);
      }
      receipt.running = true;
      const controller = new AbortController(); controllers.add(controller);
      try {
        if (!receipt.epoch) {
          if (!capabilities) {
            const data = await api('/study/capabilities', {signal:controller.signal}, 15000);
            if (!current(expected, revision)) return;
            if (!data?.configured || !UUID.test(data.generation_epoch)) throw Object.assign(Error(), {status:503});
            capabilities = {generation_epoch:data.generation_epoch};
          }
          // Once assigned, never substitute a newer server epoch for a retry.
          receipt.epoch = capabilities.generation_epoch;
        }
        if (!current(expected, revision)) return;
        const data = await api('/study/generate', {method:'POST', signal:controller.signal, body:JSON.stringify({
          request_id:requestId, generation_epoch:receipt.epoch, text, count, accepted_external_processing:true,
        })}, 180000);
        if (!current(expected, revision)) return;
        if (!validResult(data, requestId, count)) throw Object.assign(Error(), {status:422});
        const questions = data.questions.map(row => ({chapter:row.chapter, sub:row.sub, number:row.number,
          question:row.question, answer:row.answer, type:row.type, source_excerpt:row.source_excerpt || '', source_page:row.source_page ?? null}));
        receipt.result = {type:'generated', requestId, questions, warnings:[...data.warnings]};
        send(receipt.result);
      } catch (error) {
        // An explicit new request may discover the replacement server, but a
        // retry must keep its original receipt epoch and never replay a charge.
        if (current(expected, revision) && error?.status === 409
          && error?.message === '서버가 다시 시작되어 이전 요청을 확인할 수 없어요. 자동으로 재전송하지 않습니다.') capabilities = null;
        if (current(expected, revision) && !controller.signal.aborted) sendError(requestId, safeError(error));
      } finally { receipt.running = false; controllers.delete(controller); }
    } catch {
      if (current(expected, revision)) sendError(requestId, '안전한 요청을 준비하지 못했어요. 브라우저를 확인해 주세요.');
    }
  }

  const receive = event => {
    if (!frame || event.origin !== origin || event.source !== frame.contentWindow
      || event.data?.channel !== STUDY_CHANNEL || !current()) return;
    const data = event.data;
    if (data.type === 'ready') { status.hidden = true; sendContext(); }
    else if (data.type === 'height' && Number.isFinite(data.height)) {
      frame.style.height = Math.ceil(Math.max(600, Math.min(20000, data.height))) + 'px';
    } else if (data.type === 'interaction' && visible && document.visibilityState !== 'hidden') onInteraction();
    else if (data.type === 'generate') void generate(data);
  };
  const visibility = () => sendContext();
  win.addEventListener('message', receive);
  document.addEventListener('visibilitychange', visibility);

  function reset() {
    send({type:'context', scope:null, active:false});
    sequence++; context = null; scope = null; visible = false; capabilities = null;
    for (const controller of controllers) controller.abort();
    controllers.clear(); requests.clear(); frame = null; status = null; container.replaceChildren();
  }
  async function open() {
    if (destroyed) return;
    const owner = getOwner(), key = scopeKey();
    if (!owner || !key) { reset(); return; }
    if (context !== key) reset();
    visible = true;
    if (frame) { sendContext(); return; }
    context = key;
    const revision = sequence;
    try {
      const hashed = await studyScope(owner, win.crypto);
      if (!current(key, revision) || owner !== getOwner()) return;
      scope = hashed;
      status = document.createElement('p'); status.className = 'study-loading'; status.textContent = '암기 학습을 열고 있어요…';
      status.setAttribute('role','status');
      frame = document.createElement('iframe'); frame.className = 'study-frame'; frame.title = '암기 학습';
      frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-downloads allow-modals');
      frame.setAttribute('referrerpolicy','no-referrer'); frame.style.height = '600px'; frame.src = './study/';
      container.replaceChildren(status, frame);
    } catch {
      if (!current(key, revision)) return;
      status = document.createElement('p'); status.className = 'study-loading';
      status.textContent = '암기 학습을 안전하게 열지 못했어요. 최신 브라우저에서 다시 시도해 주세요.';
      status.setAttribute('role','alert'); container.replaceChildren(status);
    }
  }
  return {open, hide(){visible = false; sendContext();}, reset,
    destroy(){reset(); destroyed = true; win.removeEventListener('message', receive); document.removeEventListener('visibilitychange', visibility);}};
}
