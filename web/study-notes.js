// A study note is a separate, source-complete document, never a replacement
// transcript. Treat provider/server text as data; only **emphasis** is styled.
const record = value => !!value && typeof value === 'object' && !Array.isArray(value);
const chars = value => Array.from(value).length;
const text = (value, max) => typeof value === 'string' && !!value.trim()
  && value.length <= max * 2 && chars(value) <= max;
const timestamp = value => typeof value === 'string' && value.length <= 40
  && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value));

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

export function validateStudyNoteDocument(value, lecture) {
  if (!studyNoteSourceSnapshot(lecture) || !record(value) || !Array.isArray(value.paragraphs)
      || !value.paragraphs.length || value.paragraphs.length > Math.min(2048,lecture.segments.length)
      || new TextEncoder().encode(JSON.stringify(value)).length > 1024 * 1024) return null;
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
      if (!record(edit) || !text(edit.original,256) || !text(edit.replacement,256)
          || typeof edit.uncertain !== 'boolean' || edit.original === edit.replacement
          || !sourceText.includes(edit.original) || !paragraph.text.includes(edit.replacement)) return null;
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
      || (row.error !== null && !text(row.error,1000))) return null;
  let document = null, markdown = null;
  if (row.status === 'completed') {
    document = validateStudyNoteDocument(row.document,lecture);
    if (!document || !text(row.markdown,4000000) || new TextEncoder().encode(row.markdown).length > 4000000
        || !timestamp(row.completed_at)) return null;
    markdown = row.markdown;
  } else if (row.document !== null || row.markdown !== null) return null;
  return {configured:value.configured,model:value.model,study_note:{lecture_id:row.lecture_id,status:row.status,
    model:row.model,error_code:row.error_code,error:row.error,created_at:row.created_at,updated_at:row.updated_at,
    completed_at:row.completed_at,document,markdown}};
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
