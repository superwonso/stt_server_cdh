export function validQuestionJob(job, lecture) {
  if (!job || job.lecture_id !== lecture?.id || typeof job.id !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(job.id)
      || typeof job.question !== 'string' || !job.question.trim() || Array.from(job.question).length > 1000
      || !['queued','processing','completed','failed','cancelled'].includes(job.status)
      || typeof job.created_at !== 'string' || !Number.isFinite(Date.parse(job.created_at))
      || (job.cancel_requested !== undefined && typeof job.cancel_requested !== 'boolean')) return false;
  if (job.status !== 'completed') return job.document === null || job.document === undefined;
  if (!Array.isArray(lecture.segments) || lecture.segments.length > 50000
      || !lecture.segments.every(segment => segment && typeof segment.id === 'string' && segment.id
        && Number.isFinite(segment.start) && Number.isFinite(segment.end) && segment.start >= 0 && segment.end >= segment.start)) return false;
  const doc = job.document, source = new Set(lecture.segments.map(segment => segment.id));
  if (source.size !== lecture.segments.length) return false;
  if (!['full','retrieved','none'].includes(job.scope) || !Number.isSafeInteger(job.selected_count)
      || job.selected_count < 0 || job.selected_count > Math.min(128,source.size) || job.total_segments !== source.size
      || (job.scope === 'full' && job.selected_count !== source.size)
      || ((job.scope === 'none') !== (job.selected_count === 0))
      || !doc || Object.keys(doc).sort().join(',') !== 'answerability,paragraphs'
      || !['answered','insufficient_evidence'].includes(doc.answerability) || !Array.isArray(doc.paragraphs)) return false;
  if (doc.answerability === 'insufficient_evidence') return doc.paragraphs.length === 0;
  if (!(job.selected_count > 0 && doc.paragraphs.length > 0 && doc.paragraphs.length <= 6
    && doc.paragraphs.every(paragraph => paragraph && Object.keys(paragraph).sort().join(',') === 'source_ids,text'
      && typeof paragraph.text === 'string' && paragraph.text.trim()
      && Array.from(paragraph.text).length <= 800 && Array.isArray(paragraph.source_ids)
      && paragraph.source_ids.length > 0 && paragraph.source_ids.length <= 6
      && new Set(paragraph.source_ids).size === paragraph.source_ids.length
      && paragraph.source_ids.every(id => source.has(id))))) return false;
  return new Set(doc.paragraphs.flatMap(paragraph => paragraph.source_ids)).size <= job.selected_count;
}

export function validQuestionPage(data, lecture, offset = 0) {
  return typeof data?.configured === 'boolean' && typeof data.model === 'string'
    && data.model.length > 0 && data.model.length <= 128
    && Number.isSafeInteger(offset) && offset >= 0 && offset <= 100 && data.offset === offset
    && (data.limit === undefined || data.limit === 20)
    && Number.isSafeInteger(data.total) && data.total >= 0 && data.total <= 100
    && typeof data.has_more === 'boolean' && Array.isArray(data.questions) && data.questions.length <= 20
    && data.questions.length === Math.min(20,Math.max(0,data.total-offset))
    && data.has_more === (offset+data.questions.length < data.total)
    && data.questions.every(job => validQuestionJob(job,lecture))
    && new Set(data.questions.map(job => job.id)).size === data.questions.length;
}
