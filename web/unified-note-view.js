import { appendStudyNoteText, STUDY_NOTE_RESULT_WARNING } from './study-notes.js';

export const UNIFIED_NOTE_SEMANTIC_NOTICE = '원문은 모두 보존했지만 AI 설명의 의미상 완전성은 확인하지 못했습니다. 원문과 함께 확인해 주세요.';

function timeLabel(seconds) {
  const whole = Math.floor(seconds), minutes = Math.floor(whole / 60), rest = String(whole % 60).padStart(2,'0');
  return minutes < 60 ? `${minutes}:${rest}` : `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2,'0')}:${rest}`;
}

// Accept the canonical value returned by validateStudyNoteDocument, never raw
// provider JSON. Navigation is delegated to the caller so it can enforce its
// current lecture/account/request scope when the user clicks a source button.
// onSeek(startSeconds, sourceIds) receives a fresh IDs array on every click.
export function renderUnifiedStudyNote(value, target, {onSeek} = {}) {
  const document = target?.ownerDocument;
  if (!document?.createElement || typeof target?.replaceChildren !== 'function'
      || value?.format !== 'unified_study_note' || value.version !== 1
      || value.coverage?.complete !== true || value.coverage.semantic_verified !== false
      || !Array.isArray(value.sections) || !value.sections.length
      || !Array.isArray(value.overview) || !Array.isArray(value.supporting_sources)
      || !Array.isArray(value.warnings)) throw new TypeError('검증된 통합 정리본이 필요합니다.');
  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  const prose = text => {
    const result = node('p',undefined,'study-note-text');
    appendStudyNoteText(result,text,document);
    return result;
  };
  const seek = (start, sourceIds) => {
    if (typeof onSeek !== 'function' || !Number.isFinite(start) || start < 0) return null;
    const ids = [...sourceIds], button = node('button',`원문 ${timeLabel(start)} 보기`,'secondary-button');
    button.type = 'button'; button.addEventListener('click',() => onSeek(start,[...ids]));
    return button;
  };
  const root = node('div',undefined,'unified-study-note');
  const materials = new Map(), materialSection = node('section',undefined,'study-note-paragraph');
  if (value.supporting_sources.length) {
    materialSection.append(node('h4','첨부한 보조 자료'),node('p',
      '자료에서 추출한 내용입니다. 수업 발언과 구분해서 확인하세요. 이미지나 표의 추출 안내도 함께 확인하세요.','summary-privacy'));
    for (const unit of value.supporting_sources) {
      const label = `${unit.label} · ${unit.kind === 'pptx' ? '슬라이드 ' + unit.index : unit.index + '쪽'}`;
      const details = node('details',undefined,'study-note-material'); details.open = false;
      // Source text is exact and literal, including Markdown/HTML-like examples.
      details.append(node('summary',label),node('div',unit.text,'study-note-text'));
      materials.set(unit.id,{label,details}); materialSection.append(details);
    }
  }
  if (value.overview.length) {
    const overview = node('section',undefined,'study-note-overview'), list = node('ul');
    overview.append(node('h4','한눈에 보는 개요'));
    for (const item of value.overview) {
      const row = node('li'); row.append(prose(item.text));
      const button = seek(item.start,item.source_ids); if (button) row.append(button);
      list.append(row);
    }
    overview.append(list); root.append(overview);
  }
  for (const section of value.sections) {
    const part = node('section',undefined,'study-note-paragraph');
    const labels = {mapped:'원문과 연결한 AI 설명',unverified:'원문 대응을 확인하지 못한 AI 초안',source_only:'이 구간은 원문만 보관되었습니다.'};
    part.append(node('h4',section.heading),node('p',labels[section.status],'summary-privacy'));
    if (section.text) part.append(prose(section.text));
    for (const uncertain of [false,true]) {
      const edits = section.edits.filter(edit => edit.uncertain === uncertain);
      if (!edits.length) continue;
      const details = node('details',undefined,'study-note-edits'); details.open = false;
      const list = node('ul');
      details.append(node('summary',`${uncertain ? '추정 · 확인 필요' : 'AI 제안 · 원문 확인 권장'} ${edits.length}곳`));
      for (const edit of edits) list.append(node('li',`${edit.original} → ${edit.replacement}`));
      details.append(list); part.append(details);
    }
    if (section.citations.length) {
      const citations = node('div',undefined,'summary-sources'); citations.append(node('p','참고한 보조 자료 · 수업 발언과 별도'));
      for (const id of section.citations) {
        const source = materials.get(id);
        if (!source) throw new TypeError('검증된 자료 출처가 필요합니다.');
        const button = node('button',source.label,'secondary-button'); button.type = 'button';
        button.addEventListener('click',() => {
          source.details.open = true;
          source.details.scrollIntoView?.({block:'nearest'});
        });
        citations.append(button);
      }
      part.append(citations);
    }
    const originals = node('details',undefined,'study-note-originals'); originals.open = false;
    originals.append(node('summary',`해당 위치의 원문 전체 · ${section.originals.length}개 구간`));
    for (const original of section.originals) {
      const row = node('div',undefined,'study-note-original');
      row.append(node('p',`${timeLabel(original.start)}–${timeLabel(original.end)}`,'summary-sources'),
        node('div',original.text,'study-note-text'));
      const button = seek(original.start,[original.id]); if (button) row.append(button);
      originals.append(row);
    }
    part.append(originals); root.append(part);
  }
  if (value.supporting_sources.length) root.append(materialSection);
  const footer = node('section',undefined,'study-note-issues');
  footer.append(node('p',`원문 ${value.coverage.source_count}개 구간을 모두 보존했습니다.`,'summary-privacy'),
    node('p',UNIFIED_NOTE_SEMANTIC_NOTICE,'summary-privacy'));
  if (value.warnings.length) footer.append(node('p',STUDY_NOTE_RESULT_WARNING,'summary-privacy'));
  root.append(footer);
  // Build off-screen first, leaving the previous view intact on a bad input.
  target.replaceChildren(root);
  return root;
}
