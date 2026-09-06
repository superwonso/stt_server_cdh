// Public code contains no account data. Classification lives on the private API.
export function lectureTitle(lecture) {
  return typeof lecture?.display_title === 'string' && lecture.display_title.trim()
    ? lecture.display_title : lecture?.title || '수업';
}

export function filterLibrary(lectures, {course = '', semester = ''} = {}) {
  return lectures.filter(lecture => (!course || lecture.course === course)
    && (!semester || lecture.semester === semester));
}

export function libraryOptions(lectures, field) {
  if (!['course','semester'].includes(field)) return [];
  return [...new Set(lectures.map(lecture => lecture[field]).filter(value => typeof value === 'string' && value))]
    .sort((a,b) => a.localeCompare(b,'ko'));
}

export function validMetadata(value, lectureId) {
  return value?.lecture_id === lectureId && typeof value.display_title === 'string'
    && value.display_title.trim().length > 0 && Array.from(value.display_title).length <= 120
    && typeof value.course === 'string' && Array.from(value.course).length <= 80
    && typeof value.semester === 'string' && Array.from(value.semester).length <= 40
    && Number.isSafeInteger(value.revision) && value.revision >= 0;
}

export function validLibrarySearch(value) {
  return Array.isArray(value?.items) && value.items.length <= 50 && typeof value.has_more === 'boolean'
    && Number.isSafeInteger(value.offset) && value.offset >= 0
    && Number.isSafeInteger(value.limit) && value.limit > 0 && value.limit <= 50
    && value.items.every(item => typeof item.lecture_id === 'string' && /^[0-9a-f-]{36}$/i.test(item.lecture_id)
      && typeof item.display_title === 'string' && typeof item.snippet === 'string'
      && item.snippet.length <= 2000 && Number.isFinite(Date.parse(item.created_at))
      && ['title','raw','corrected'].includes(item.source)
      && (item.source === 'title' || (typeof item.segment_id === 'string'
        && Number.isFinite(item.start) && item.start >= 0 && Number.isFinite(item.end) && item.end >= item.start)));
}
