// Personal notes and manual transcripts are independent of raw/AI versions.
export function validManualState(value, lecture) {
  if (value?.lecture_id !== lecture?.id || !/^[a-f0-9]{64}$/.test(value?.raw_revision || '')
      || !Number.isSafeInteger(value.revision) || value.revision < 0
      || !Array.isArray(value.notes) || value.notes.length > 100
      || !Array.isArray(value.edits) || value.edits.length > 1000) return false;
  const source = new Map((lecture.segments || []).map(segment => [segment.id,segment]));
  const notes = new Set(), edits = new Set();
  for (const note of value.notes) {
    if (typeof note?.id !== 'string' || !/^[a-f0-9-]{36}$/i.test(note.id) || notes.has(note.id)
        || typeof note.text !== 'string' || !note.text.trim() || Array.from(note.text).length > 5000
        || !Number.isFinite(note.start_seconds) || note.start_seconds < 0 || note.start_seconds > 14400
        || (note.segment_id !== null && note.segment_id !== undefined && !source.has(note.segment_id))) return false;
    notes.add(note.id);
  }
  for (const edit of value.edits) {
    if (!source.has(edit?.segment_id) || edits.has(edit.segment_id) || typeof edit.text !== 'string'
        || !edit.text.trim() || Array.from(edit.text).length > 5000) return false;
    edits.add(edit.segment_id);
  }
  return true;
}

export function manualSegments(lecture, state) {
  if (!validManualState(state,lecture)) return null;
  const edits = new Map(state.edits.map(edit => [edit.segment_id,edit.text]));
  return (lecture.segments || []).map(segment => ({...segment,text:edits.get(segment.id) ?? segment.text}));
}

export function validManualHistory(data, lecture, state, query = {}) {
  if (!validManualState(state,lecture) || data?.lecture_id !== lecture.id || data.raw_revision !== state.raw_revision
      || !Number.isSafeInteger(data.revision) || data.revision < state.revision
      || !Number.isSafeInteger(data.at_revision) || data.at_revision < 0 || data.at_revision > data.revision
      || (query.at_revision !== undefined && Number(query.at_revision) !== data.at_revision)
      || data.offset !== (query.offset || 0) || !Number.isSafeInteger(data.limit) || data.limit < 1 || data.limit > 20
      || !Array.isArray(data.items) || data.items.length > data.limit || typeof data.has_more !== 'boolean') return false;
  const source = new Map((lecture.segments || []).map(segment => [segment.id,segment]));
  let previous = data.at_revision + 1;
  for (const item of data.items) {
    if (!Number.isSafeInteger(item.revision) || item.revision <= 0 || item.revision >= previous
        || !Number.isFinite(Date.parse(item.created_at)) || !['note_upsert','note_delete','segment_edit'].includes(item.action)
        || !(item.text === null || (typeof item.text === 'string' && item.text.trim() && Array.from(item.text).length <= 5000))
        || !Number.isFinite(item.start_seconds) || item.start_seconds < 0 || item.start_seconds > 14400) return false;
    previous = item.revision;
    if (item.segment_id !== null && (!source.has(item.segment_id) || source.get(item.segment_id).start !== item.start_seconds)) return false;
    if (item.action === 'segment_edit') {
      if (!source.has(item.segment_id) || item.note_id !== null) return false;
    } else if (typeof item.note_id !== 'string' || !/^[a-f0-9-]{36}$/i.test(item.note_id)
        || (item.action === 'note_upsert' ? typeof item.text !== 'string' : item.text !== null)) return false;
    if (query.segment_id && (item.action !== 'segment_edit' || item.segment_id !== query.segment_id)) return false;
    if (query.note_id && item.note_id !== query.note_id) return false;
  }
  return true;
}
