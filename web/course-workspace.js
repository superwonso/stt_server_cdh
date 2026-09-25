import { validateUnifiedStudyNoteDocument } from './study-notes.js';
import { renderUnifiedStudyNote } from './unified-note-view.js';
import { createMaterialPanel } from './study-materials.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const id = value => typeof value === 'string' && UUID.test(value);
const dateText = value => value ? new Date(value).toLocaleString('ko-KR') : '수업 일시 미지정';
const abortError = () => Object.assign(new Error('화면이 변경되었습니다.'), {name:'AbortError'});
export function localDateTime(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return '';
  const date = new Date(value); date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0,16);
}
export function validCoursePage(value) {
  return Array.isArray(value?.courses) && value.courses.length <= 200
    && Number.isSafeInteger(value.total) && value.total >= 0 && value.total <= 500
    && value.courses.every(row => id(row.id) && typeof row.name === 'string' && row.name.length <= 160
      && typeof row.semester === 'string' && Number.isSafeInteger(row.revision));
}
export function createCourseWorkspace({container,api,scopeKey,getCurrent,onSessionChanged,onSelectLecture}) {
  const document = container.ownerDocument;
  let context = null, sequence = 0, timer = null, course = null, pending = null, activeReview = null;
  let createIntent=null;
  let courseRows = [], sessionRows = [], offset = 0, nextOffset = null, selection = new Set(), urls = new Set();
  const el = (tag,text='',className='') => { const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node; };
  const button = (text,run) => { const node=el('button',text,'secondary-button');node.type='button';node.onclick=()=>void action(run);return node; };
  const input = (type,max) => { const node=el('input');node.type=type;if(max)node.maxLength=max;return node; };
  const label = (text,control) => { const node=el('label',text);node.append(control);return node; };
  const status=el('p','','panel-help');status.setAttribute('role','status');
  const refresh=button('강의 목록 새로고침',()=>open());
  const picker=el('select');picker.setAttribute('aria-label','복습할 강의');
  const newName=input('text',80),semester=input('text',40);
  const createForm=el('form','','course-form');
  const createButton=el('button','강의 만들기','secondary-button');createButton.type='submit';
  createForm.append(label('강의명',newName),label('학기 · 선택',semester),createButton);
  const assignment=el('form','','course-form'), assigned=el('select');
  const sessionName=input('text',120),sessionAt=input('datetime-local');
  const assignButton=el('button','현재 수업 정보 저장','secondary-button');assignButton.type='submit';
  const assignmentTitle=el('h3','현재 수업의 강의 · 일시');
  assignment.append(assignmentTitle,label('연결할 강의',assigned),label('회차 제목',sessionName),label('실제 수업 일시',sessionAt),assignButton);
  const sessions=el('div','','course-session-list');
  const all=input('checkbox');all.checked=true;
  const reviewControls=el('div','','study-actions');
  const createReview=button('복습 정리본 만들기',()=>requestReview());
  const refreshReview=button('복습 상태 새로고침',()=>refreshReviews());
  reviewControls.append(label('이 강의의 전체 수업',all),createReview,refreshReview);
  const scopeInfo=el('p','전체 수업을 해제하면 체크한 회차만 처리합니다. 원문과 강의자료를 NOVA로 보내며 수업별 핵심 흐름·상세 정리·전체 원문을 함께 보관합니다. 수업 수와 분량에 따라 여러 AI 요청이 발생합니다.','summary-privacy');
  const reviews=el('div','','course-reviews'),output=el('div','','course-review-output');
  const materialsHost=el('div'),materialDetails=el('details');materialDetails.append(el('summary','이 강의의 공통 PDF · PPTX 자료'),materialsHost);
  const materialPanel=createMaterialPanel({container:materialsHost,api,scopeKey,onChanged:()=>{status.textContent='강의자료가 바뀌었습니다. 기존 정리본은 갱신 후 다시 만들 수 있어요.';}});
  container.replaceChildren(refresh,status,label('강의 선택',picker),createForm,assignment,materialDetails,el('h3','강의에 연결된 수업'),sessions,scopeInfo,reviewControls,reviews,output);
  const valid = captured => context===captured && captured?.key===scopeKey() && !captured.controller.signal.aborted;
  async function request(path,options={},captured=context) {
    if(!valid(captured))throw abortError();
    const result=await api(path,{...options,signal:captured.controller.signal});
    if(!valid(captured))throw abortError();return result;
  }
  async function action(fn) { const captured=context;try { await fn(); }catch(error){if(valid(captured)&&error.name!=='AbortError')status.textContent=error.message||'요청을 확인하지 못했습니다.';} }
  function reset() {
    ++sequence;context?.controller.abort();context=null;clearTimeout(timer);timer=null;pending=null;activeReview=null;
    course=null;courseRows=[];sessionRows=[];selection.clear();materialPanel.reset();assignmentState=null;createIntent=null;
    createReview.disabled=true;assignButton.disabled=false;createButton.disabled=false;createReview.textContent='복습 정리본 만들기';
    for(const url of urls)URL.revokeObjectURL(url);urls.clear();
    picker.replaceChildren();assigned.replaceChildren();sessions.replaceChildren();reviews.replaceChildren();output.replaceChildren();
    newName.value='';semester.value='';sessionName.value='';sessionAt.value='';status.textContent='';assignment.hidden=true;
  }
  async function open() {
    reset();context={key:scopeKey(),controller:new AbortController()};const captured=context;
    await action(async()=>{
      let page=0;
      do {const result=await request(`/courses?offset=${page}&limit=200`,{},captured);
        if(!validCoursePage(result))throw new Error('강의 목록 형식을 확인하지 못했습니다.');
        courseRows.push(...result.courses);page=result.next_offset;
        if(page!==null&&(!Number.isSafeInteger(page)||page<=0||page>500))throw new Error('강의 목록 범위를 확인하지 못했습니다.');
      }while(page!==null);
      fillOptions();await loadAssignment(captured);
      if(courseRows.length){const initial=courseRows.some(row=>row.id===assignmentState?.course_id)?assignmentState.course_id:courseRows[0].id;picker.value=initial;await loadCourse(initial,0,captured);}
      else status.textContent='강의를 만든 뒤 현재 수업을 연결하면 자료와 복습을 한곳에서 볼 수 있어요.';
    });
  }
  function fillOptions(){
    picker.replaceChildren();assigned.replaceChildren();const none=el('option','연결하지 않음');none.value='';assigned.append(none);
    for(const row of courseRows){for(const select of [picker,assigned]){const option=el('option',`${row.name}${row.semester?' · '+row.semester:''}`);option.value=row.id;select.append(option);}}
  }
  let assignmentState=null;
  async function loadAssignment(captured){
    const lecture=getCurrent();assignment.hidden=!lecture?.id;assignmentState=null;if(!lecture?.id)return;
    const state=await request(`/lectures/${lecture.id}/course-session`,{},captured);
    if(state.lecture_id!==lecture.id||!Number.isSafeInteger(state.session_revision))throw new Error('수업 정보를 확인하지 못했습니다.');
    if(getCurrent()?.id!==lecture.id)return;
    assignmentState=state;assigned.value=state.course_id||'';sessionName.value=state.session_name||'';
    sessionName.placeholder=lecture.display_title||lecture.title||'회차 제목';sessionAt.value=localDateTime(state.session_at);
  }
  createForm.onsubmit=event=>{event.preventDefault();void action(async()=>{
    if(!newName.value.trim())return;createButton.disabled=true;
    const captured=context,intent={name:newName.value.trim(),semester:semester.value.trim()};
    if(!createIntent||createIntent.name!==intent.name||createIntent.semester!==intent.semester)createIntent={id:crypto.randomUUID(),...intent};
    try{const row=await request('/courses',{method:'POST',body:JSON.stringify(createIntent)},captured);createIntent=null;
      courseRows.push(row);fillOptions();picker.value=row.id;newName.value='';semester.value='';await loadCourse(row.id);
    }finally{if(valid(captured))createButton.disabled=false;}
  });};
  assignment.onsubmit=event=>{event.preventDefault();void action(async()=>{
    const captured=context,lecture=getCurrent(),state=assignmentState;
    if(!state||state.lecture_id!==lecture?.id)return;assignButton.disabled=true;
    try{const row=await request(`/lectures/${lecture.id}/course-session`,{method:'PUT',body:JSON.stringify({revision:state.session_revision,course_id:assigned.value||null,session_name:sessionName.value.trim(),session_at:sessionAt.value?new Date(sessionAt.value).toISOString():null})},captured);
      if(getCurrent()?.id!==lecture.id)return;assignmentState=row;onSessionChanged(row);status.textContent='강의명·회차 제목·수업 일시를 저장했습니다.';
      if(row.course_id){picker.value=row.course_id;await loadCourse(row.course_id,0,captured);}
    }finally{if(valid(captured))assignButton.disabled=false;}
  });};
  picker.onchange=()=>void action(async()=>{pending=null;selection.clear();createReview.textContent='복습 정리본 만들기';await loadCourse(picker.value);});
  materialDetails.ontoggle=()=>{if(materialDetails.open&&course)materialPanel.setScope({kind:'course',id:course.id});else materialPanel.reset();};
  async function loadCourse(identifier,start=0,captured=context){
    if(!id(identifier))return;const run=++sequence;clearTimeout(timer);activeReview=null;course=null;createReview.disabled=true;sessions.replaceChildren();output.replaceChildren();reviews.replaceChildren();
    materialPanel.reset();materialDetails.open=false;
    const result=await request(`/courses/${identifier}?offset=${start}&limit=50`,{},captured);
    if(run!==sequence)return;
    if(!result.course||result.course.id!==identifier||!Array.isArray(result.sessions)||result.sessions.length>50)throw new Error('강의 수업 목록을 확인하지 못했습니다.');
    course=result.course;createReview.disabled=false;sessionRows=result.sessions;offset=start;nextOffset=result.next_offset;renderSessions(result.total,result.materials||[]);await refreshReviews(captured);
  }
  function renderSessions(total,materials){
    const captured=context,run=sequence;const current=()=>valid(captured)&&run===sequence;
    sessions.replaceChildren(el('p',`총 ${total}회 · 자료 ${materials.length}개`));
    for(const row of sessionRows){
      if(!id(row.id))continue;const item=el('div','','course-session');const check=input('checkbox');check.checked=selection.has(row.id);
      check.onchange=()=>{if(!current())return;if(check.checked){selection.add(row.id);all.checked=false;}else selection.delete(row.id);};
      const name=row.session_name||row.title||'수업';item.append(label(name,check),el('p',`${row.session_at?dateText(row.session_at):'기록 생성 '+dateText(row.created_at)+' · 수업 일시 미지정'}${row.recording_finalized?'':' · 저장 중'}`),button('이 수업 열기',async()=>{if(current())await onSelectLecture(row.id);}));
      const files=materials.filter(file=>file.lecture_id===row.id);
      if(files.length)item.append(el('p',files.map(file=>file.filename).join(' · '),'summary-privacy'));
      sessions.append(item);
    }
    const nav=el('div','','study-actions');if(offset>0)nav.append(button('이전 수업',()=>{if(current())return loadCourse(course.id,Math.max(0,offset-50));}));
    if(nextOffset!==null)nav.append(button('다음 수업',()=>{if(current())return loadCourse(course.id,nextOffset);}));sessions.append(nav);
  }
  async function refreshReviews(captured=context,reviewOffset=0){
    if(!course)return;const identifier=course.id,run=sequence;
    const result=await request(`/courses/${identifier}/reviews?offset=${reviewOffset}`,{},captured);if(run!==sequence||course?.id!==identifier)return;
    if(!Array.isArray(result.reviews))throw new Error('복습 기록을 확인하지 못했습니다.');reviews.replaceChildren();
    for(const row of result.reviews){if(!id(row.id))continue;reviews.append(button(`${dateText(row.created_at)} · ${{queued:'대기',processing:'처리 중',completed:'완료',failed:'확인 필요'}[row.status]||'상태 확인'}`,()=>action(()=>showReview(identifier,row.id,captured))));}
    if(reviewOffset>0)reviews.append(button('이전 복습 기록',()=>action(()=>refreshReviews(captured,Math.max(0,reviewOffset-20)))));
    if(Number.isSafeInteger(result.next_offset)&&result.next_offset>reviewOffset)reviews.append(button('다음 복습 기록',()=>action(()=>refreshReviews(captured,result.next_offset))));
    if(result.reviews.length)await showReview(identifier,result.reviews[0].id,captured);
  }
  async function requestReview(){await action(async()=>{
    if(!course)return;const captured=context,identifier=course.id,run=sequence;
    if(!pending){const selected=all.checked?null:[...selection];if(selected&&!selected.length)throw new Error('복습할 수업을 선택해 주세요.');pending={course_id:identifier,body:{id:crypto.randomUUID(),...(selected?{lecture_ids:selected}:{})}};}
    if(pending.course_id!==identifier)return;const intent=pending;const current=()=>valid(captured)&&run===sequence&&course?.id===identifier&&pending===intent;createReview.disabled=true;
    try{const result=await request(`/courses/${identifier}/reviews`,{method:'POST',body:JSON.stringify(intent.body)},captured);
      if(!current())return;pending=null;createReview.textContent='복습 정리본 만들기';await showReview(identifier,result.id,captured);
    }catch(error){if(current()){if([400,403,404,409,413,422,429,503].includes(error.status)){pending=null;createReview.textContent='복습 정리본 만들기';}else{createReview.textContent='같은 요청 상태 확인 · 재시도';status.textContent='처리 여부가 불확실할 때는 같은 요청 ID로 확인합니다. 중복 생성하지 않습니다.';}throw error;}
    }finally{if(valid(captured)&&run===sequence)createReview.disabled=false;}
  });}
  function download(markdown){const captured=context;const url=URL.createObjectURL(new Blob(['\uFEFF',markdown],{type:'text/markdown;charset=utf-8'}));urls.add(url);const link=el('a','↓ 강의 복습 정리본.md','secondary-button');link.href=url;link.download='강의 복습 정리본.md';link.onclick=event=>{if(!valid(captured)){event.preventDefault();}};return link;}
  async function showReview(courseId,reviewId,captured=context){
    if(!id(reviewId))throw new Error('복습 기록 식별자를 확인하지 못했습니다.');clearTimeout(timer);const run=sequence;
    activeReview=reviewId;const row=await request(`/courses/${courseId}/reviews/${reviewId}`,{},captured);
    const current=()=>valid(captured)&&run===sequence&&course?.id===courseId&&activeReview===reviewId;
    if(!current())return;output.replaceChildren();
    if(['queued','processing'].includes(row.status)){status.textContent='강의 복습 정리본을 만들고 있습니다. 이 화면을 닫아도 서버 작업은 계속됩니다.';timer=setTimeout(()=>void action(()=>showReview(courseId,reviewId,captured)),4000);return;}
    if(row.status!=='completed'){status.textContent='복습을 완료하지 못했습니다. 원문은 보존되어 있으며, 자료와 연결 상태를 확인한 뒤 새로 요청할 수 있어요.';return;}
    if(row.document?.format!=='course_review'||row.document.version!==1||!Array.isArray(row.document.sessions)||row.document.sessions.length>200||typeof row.markdown!=='string'||row.markdown.length>128*1024*1024)throw new Error('복습 결과 형식을 확인하지 못했습니다.');
    status.textContent=row.stale?'수업 또는 자료가 바뀌었습니다. 아래 결과는 이전 범위의 정리본입니다.':'수업별 핵심 흐름과 상세 정리를 모두 모았습니다. 원문 대조에서 빠진 내용이 없는지 확인할 수 있어요.';
    const fragment=el('div'),overview=el('section'),overviewList=el('ul');overview.append(el('h3','전체 수업 핵심 흐름'),overviewList);const seen=new Set();
    for(const session of row.document.sessions){
      if(!id(session.lecture_id)||seen.has(session.lecture_id))throw new Error('복습 수업 범위를 확인하지 못했습니다.');seen.add(session.lecture_id);
      const lecture=await request(`/lectures/${session.lecture_id}`,{},captured);if(!current())return;
      const note=validateUnifiedStudyNoteDocument(session.document,lecture);if(!note)throw new Error('복습 원문과 저장 결과가 달라 표시하지 못했습니다.');
      const details=el('details','','course-review-session');details.append(el('summary',`${session.session_name||lecture.title||'수업'} · ${session.session_at?dateText(session.session_at):'수업 일시 미지정'}`));
      for(const point of note.overview){const item=el('li');item.append(el('strong',session.session_name||lecture.title||'수업'),el('p',point.text));const jump=button('이 회차 상세 보기',()=>{if(current()){details.open=true;details.scrollIntoView?.({block:'start',behavior:'smooth'});}});item.append(jump);overviewList.append(item);}
      const body=el('div');renderUnifiedStudyNote(note,body);details.append(body);fragment.append(details);
    }
    if(!current())return;output.append(download(row.markdown));if(overviewList.children.length)output.append(overview);output.append(fragment);
    if(row.document.warnings?.length)output.append(el('p','일부 수업의 AI 결과를 확인하지 못했습니다. 해당 수업의 원문을 함께 확인해 주세요.','ai-result-warning'));
  }
  reset();return {open,reset,destroy:reset};
}
