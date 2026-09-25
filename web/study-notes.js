// A study note is a separate, source-complete document, never a replacement
// transcript. Treat provider/server text as data; only **emphasis** is styled.
const record = value => !!value && typeof value === 'object' && !Array.isArray(value);
const chars = value => Array.from(value).length;
const text = (value, max) => typeof value === 'string' && !!value.trim()
  && value.length <= max * 2 && chars(value) <= max;
const timestamp = value => typeof value === 'string' && value.length <= 40
  && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value));
export const STUDY_NOTE_RESULT_WARNING = '일부 내용을 확인하지 못했습니다. 원문과 함께 확인해 주세요.';
const draftWarningCodes = new Set([
  'invalid_response','response_truncated','incomplete_batches','gateway_unavailable','authentication_failed',
  'credit_exhausted','rate_limited','model_refused','interrupted','content_limited','placeholder_unresolved',
]);

export function studyNoteWarningMessage(code) {
  return draftWarningCodes.has(code) ? STUDY_NOTE_RESULT_WARNING : null;
}

export function studyNoteSourceSnapshot(lecture) {
  if (!lecture?.recording_finalized || !Array.isArray(lecture.segments)
      || !lecture.segments.length || lecture.segments.length > 50000) return null;
  const ids = new Set(), rows = [];
  let totalChars = 0, previousStart = -1;
  for (const segment of lecture.segments) {
    if (!text(segment?.id,200) || ids.has(segment.id) || !text(segment.text,24000)
        || !Number.isFinite(segment.start) || segment.start < 0 || segment.start < previousStart
        || !Number.isFinite(segment.end) || segment.end > 1e12 || segment.end < segment.start) return null;
    totalChars += chars(segment.text); previousStart = segment.start;
    if (totalChars > 250000) return null;
    ids.add(segment.id); rows.push([segment.id,segment.start,segment.end,segment.text]);
  }
  return JSON.stringify(rows);
}


const unifiedDocumentBytes = 8 * 1024 * 1024;
const unifiedMarkdownBytes = 12 * 1024 * 1024;
const exactKeys = (value, keys) => record(value) && Object.keys(value).length === keys.length
  && keys.every(key => Object.hasOwn(value,key));
const sourceControl = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u;
const invalidUnicode = /[\uD800-\uDFFF]/u;
const hiddenReasoning = /<(?:think|analysis|reasoning)(?:\s[^>]*)?>/iu;
const boundedSourceText = (value, limit) => text(value,limit) && !sourceControl.test(value) && !invalidUnicode.test(value);
const generatedText = (value, limit) => boundedSourceText(value,limit)
  && !/[\u007f-\u009f]/u.test(value) && !hiddenReasoning.test(value);
const safeWarnings = value => Array.isArray(value) && value.length <= draftWarningCodes.size
  && value.every(code => typeof code === 'string' && draftWarningCodes.has(code)) && new Set(value).size === value.length;

export function validateUnifiedStudyNoteDocument(value, lecture) {
  if (!lecture?.recording_finalized || !Array.isArray(lecture.segments) || !lecture.segments.length
      || lecture.segments.length > 2048 || !exactKeys(value,[
        'format','version','overview','sections','supporting_sources','coverage','warnings'])
      || value.format !== 'unified_study_note' || value.version !== 1
      || new TextEncoder().encode(JSON.stringify(value)).length > unifiedDocumentBytes) return null;
  const raw = [], byId = new Map();
  let previousStart=-1, rawChars=0;
  for (const row of lecture.segments) {
    if (!record(row) || !boundedSourceText(row.id,256) || /\s|[\u0000-\u001f]/u.test(row.id)
        || byId.has(row.id) || !boundedSourceText(row.text,24000)
        || !Number.isFinite(row.start) || !Number.isFinite(row.end) || row.start < 0
        || row.start < previousStart || row.end < row.start || row.end > 1e12) return null;
    rawChars+=chars(row.text); if(rawChars>250000) return null;
    const copy={id:row.id,start:row.start,end:row.end,text:row.text};
    raw.push(copy); byId.set(row.id,copy); previousStart=row.start;
  }
  if (!Array.isArray(value.supporting_sources) || value.supporting_sources.length>128) return null;
  const materials=[], materialIds=new Set();
  let materialChars=0;
  for (const unit of value.supporting_sources) {
    if (!exactKeys(unit,['id','label','text','kind','index']) || !boundedSourceText(unit.id,256)
        || /\s|[\u0000-\u001f]/u.test(unit.id) || materialIds.has(unit.id)
        || !boundedSourceText(unit.label,200) || /[\n\r\t]/u.test(unit.label)
        || !boundedSourceText(unit.text,24000) || !['pdf','pptx'].includes(unit.kind)
        || !Number.isInteger(unit.index) || unit.index<1 || unit.index>10000) return null;
    materialChars+=chars(unit.text); if(materialChars>200000) return null;
    materialIds.add(unit.id); materials.push({...unit});
  }
  if (!Array.isArray(value.sections) || !value.sections.length || value.sections.length>raw.length
      || !safeWarnings(value.warnings)) return null;
  const sections=[], counts={mapped:0,unverified:0,source_only:0};
  let offset=0,totalChars=0,editCount=0;
  for (const section of value.sections) {
    if (!exactKeys(section,['heading','text','edits','source_ids','originals','status','warnings','citations'])
        || !generatedText(section.heading,120) || /[\n\r\t]/u.test(section.heading)
        || !['mapped','unverified','source_only'].includes(section.status) || !safeWarnings(section.warnings)
        || section.warnings.some(code=>!value.warnings.includes(code))
        || !Array.isArray(section.source_ids) || !section.source_ids.length
        || !Array.isArray(section.originals) || section.originals.length!==section.source_ids.length
        || !Array.isArray(section.edits) || section.edits.length>16
        || !Array.isArray(section.citations) || section.citations.length>128
        || section.citations.some(id=>typeof id!=='string'||!materialIds.has(id))
        || new Set(section.citations).size!==section.citations.length) return null;
    const originals=[];
    for(let index=0;index<section.source_ids.length;index++) {
      const source=raw[offset++], original=section.originals[index];
      if(!source || section.source_ids[index]!==source.id || !exactKeys(original,['id','start','end','text'])
          || ['id','start','end','text'].some(key=>original[key]!==source[key])) return null;
      originals.push({...source});
    }
    counts[section.status]+=originals.length;
    const edits=[];
    if(section.status==='source_only') {
      if(section.text!=='' || section.edits.length || section.citations.length || !section.warnings.length) return null;
    } else {
      if(!generatedText(section.text,24000)) return null;
      totalChars+=chars(section.text); if(totalChars>500000) return null;
      if(section.status==='unverified') {
        if(section.edits.length || section.citations.length || !section.warnings.length) return null;
      } else {
        const originalText=originals.map(row=>row.text).join('\n');
        if(originals.length>64 || chars(section.text)>Math.max(1000,chars(originalText)*3+1000)) return null;
        const pairs=new Set();
        for(const edit of section.edits) {
          if(!exactKeys(edit,['original','replacement','uncertain']) || !generatedText(edit.original,256)
              || !generatedText(edit.replacement,256) || typeof edit.uncertain!=='boolean'
              || edit.original===edit.replacement) return null;
          const pair=JSON.stringify([edit.original,edit.replacement]);
          if(pairs.has(pair) || ++editCount>512) return null;
          pairs.add(pair); edits.push({...edit});
        }
      }
    }
    sections.push({...section,source_ids:[...section.source_ids],originals,edits,citations:[...section.citations],
      warnings:[...section.warnings],start:originals[0].start,end:Math.max(...originals.map(row=>row.end))});
  }
  const coverage={source_count:raw.length,preserved_count:raw.length,mapped_count:counts.mapped,
    unverified_count:counts.unverified,fallback_count:counts.source_only,complete:true,semantic_verified:false};
  if(offset!==raw.length || !exactKeys(value.coverage,Object.keys(coverage))
      || Object.keys(coverage).some(key=>value.coverage[key]!==coverage[key])) return null;
  if(!Array.isArray(value.overview) || value.overview.length>128) return null;
  const overview=[];
  for(const item of value.overview) {
    if(!exactKeys(item,['text','source_ids']) || !generatedText(item.text,1000)
        || !Array.isArray(item.source_ids) || !item.source_ids.length || item.source_ids.length>64
        || item.source_ids.some(id=>typeof id!=='string'||!byId.has(id))
        || new Set(item.source_ids).size!==item.source_ids.length) return null;
    const rows=item.source_ids.map(id=>byId.get(id));
    overview.push({text:item.text,source_ids:[...item.source_ids],
      start:Math.min(...rows.map(row=>row.start)),end:Math.max(...rows.map(row=>row.end))});
  }
  return {format:value.format,version:value.version,overview,sections,supporting_sources:materials,coverage,
    warnings:[...value.warnings]};
}

export function validateStudyNoteDocument(value, lecture) {
  if (record(value) && value.format === 'unified_study_note') return validateUnifiedStudyNoteDocument(value,lecture);
  if (!studyNoteSourceSnapshot(lecture) || !record(value)
      || new TextEncoder().encode(JSON.stringify(value)).length > 1024 * 1024) return null;
  if (value.format === 'draft') {
    // Only the server's explicit fallback envelope is accepted. This never
    // reinterprets a malformed legacy paragraph document as valid source maps.
    if (Object.keys(value).length !== 3 || !text(value.text,500000)
        || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value.text)
        || !Array.isArray(value.warnings) || !value.warnings.length || value.warnings.length > 16
        || value.warnings.some(code => typeof code !== 'string' || !studyNoteWarningMessage(code))
        || new Set(value.warnings).size !== value.warnings.length) return null;
    return {format:'draft',text:value.text,warnings:[...value.warnings]};
  }
  if (Object.hasOwn(value,'format') || !Array.isArray(value.paragraphs)
      || !value.paragraphs.length || value.paragraphs.length > Math.min(2048,lecture.segments.length)) return null;
  const paragraphs = [];
  const rawChars = lecture.segments.reduce((total,segment) => total + chars(segment.text),0);
  let offset = 0, totalChars = 0, editCount = 0;
  for (const paragraph of value.paragraphs) {
    if (!record(paragraph) || !text(paragraph.heading,120) || !text(paragraph.text,24000)
        || !Array.isArray(paragraph.source_ids) || !paragraph.source_ids.length || paragraph.source_ids.length > 64
        || !Array.isArray(paragraph.edits) || paragraph.edits.length > 16) return null;
    const sources = [];
    for (const id of paragraph.source_ids) {
      const source = lecture.segments[offset++];
      if (!source || id !== source.id) return null;
      sources.push(source);
    }
    const sourceText = sources.map(source => source.text).join('\n');
    if (chars(paragraph.text) > chars(sourceText) * 3 + 1000) return null;
    const edits = [], pairs = new Set();
    for (const edit of paragraph.edits) {
      // Restoration notes describe the model's contextual interpretation; their
      // wording need not be a literal substring of either source or result.
      if (!record(edit) || !text(edit.original,256) || !text(edit.replacement,256)
          || typeof edit.uncertain !== 'boolean' || edit.original === edit.replacement) return null;
      const pair = JSON.stringify([edit.original,edit.replacement]);
      if (pairs.has(pair) || ++editCount > 512) return null;
      pairs.add(pair);
      edits.push({original:edit.original,replacement:edit.replacement,uncertain:edit.uncertain});
    }
    totalChars += chars(paragraph.text);
    if (totalChars > Math.min(500000,rawChars * 3 + 10000)) return null;
    paragraphs.push({heading:paragraph.heading,source_ids:[...paragraph.source_ids],text:paragraph.text,edits,
      start:Math.min(...sources.map(source => source.start)),end:Math.max(...sources.map(source => source.end))});
  }
  return offset === lecture.segments.length ? {paragraphs} : null;
}

export function validateStudyNoteResponse(value, lecture) {
  if (!record(value) || typeof value.configured !== 'boolean' || !text(value.model,200)
      || !Object.hasOwn(value,'study_note')) return null;
  if (value.study_note === null) return {configured:value.configured,model:value.model,study_note:null};
  const row = value.study_note;
  if (!record(row) || row.lecture_id !== lecture?.id || !text(row.model,200)
      || !['queued','processing','completed','failed'].includes(row.status)
      || !timestamp(row.created_at) || !timestamp(row.updated_at)
      || (row.completed_at !== null && !timestamp(row.completed_at))
      || (row.error_code !== null && !text(row.error_code,100))
      || (row.error !== null && !text(row.error,1000))
      || (Object.hasOwn(row,'stale') && typeof row.stale !== 'boolean')
      || (Object.hasOwn(row,'format_version') && ![1,2].includes(row.format_version))) return null;
  let document = null, markdown = null;
  if (row.status === 'completed') {
    document = validateStudyNoteDocument(row.document,lecture);
    const markdownLimit=document?.format==='unified_study_note'?unifiedMarkdownBytes:4000000;
    if (!document || !text(row.markdown,markdownLimit) || new TextEncoder().encode(row.markdown).length > markdownLimit
        || !timestamp(row.completed_at)) return null;
    markdown = row.markdown;
  } else if (row.document !== null || row.markdown !== null) return null;
  return {configured:value.configured,model:value.model,study_note:{lecture_id:row.lecture_id,status:row.status,
    model:row.model,error_code:row.error_code,error:row.error,created_at:row.created_at,updated_at:row.updated_at,
    completed_at:row.completed_at,document,markdown,stale:row.stale ?? false,format_version:row.format_version ?? 1}};
}

export function appendStudyNoteText(parent, value, document) {
  // Links, images, HTML, inline code, etc. stay literal and cannot navigate or
  // fetch external resources. A simple bounded emphasis parser needs no HTML.
  const pieces = String(value).split(/(\*\*[^*\n]+\*\*)/g);
  for (const piece of pieces) {
    if (!piece) continue;
    const bold = piece.startsWith('**') && piece.endsWith('**') && piece.length > 4;
    const node = document.createElement(bold ? 'strong' : 'span');
    node.textContent = bold ? piece.slice(2,-2) : piece;
    parent.append(node);
  }
}
