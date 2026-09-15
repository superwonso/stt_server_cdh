const PARAGRAPH_MIN = 500;
const PARAGRAPH_MAX = 800;
const MAX_GAP_SECONDS = 10;
const TERMINATOR = /[.!?。！？…]/;
const CLOSER = /["'”’）)\]}»」』】》〉*_`]/;
const ABBREVIATION = /^(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|e\.g|i\.e|a\.m|p\.m|fig|no|inc|ltd|ph\.d)\.$/i;
const PARTICLE = /^(?:은|는|을|를|의|에|에서|에게|께|와|과|도|만|으로|로|부터|까지|보다|처럼|마다|조차|마저|이나|이랑)(?=$|\s|[.!?。！？,，:;])/;

function time(value) {
  if ((typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') return undefined;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : undefined;
}

// These are deliberately grammatical repairs, not general overlap removal. For
// ambiguous splits (including arbitrary Korean nouns), retaining a space is
// safer than changing words or removing genuinely repeated speech.
function boundary(left, right, whitespace) {
  if (whitespace.includes('\n') || whitespace.includes('\r')) {
    return {separator: whitespace.replace(/[^\r\n]/g, ''), overlap: 0};
  }
  const tail = left.slice(-60);
  if (/[가-힣]이라$/.test(tail)) {
    if (/^이라(?:면|서|고|도|는)/.test(right)) return {separator: '', overlap: 2};
    if (/^라(?:면|서|고|도|는)/.test(right)) return {separator: '', overlap: 1};
    if (/^면(?=$|\s|[.!?,。！？])/.test(right)) return {separator: '', overlap: 0};
  }
  if (/[가-힣]$/.test(tail) && PARTICLE.test(right)) return {separator: '', overlap: 0};
  if (/(?:합니|됩니|입니|습니|립니|갑니|옵니|빕니)$/.test(tail)
      && /^다(?=$|\s|[.!?,。！？])/.test(right)) return {separator: '', overlap: 0};
  if (/(?:습|입|합|됩|립|갑|옵|빕)$/.test(tail)
      && /^니다(?=$|\s|[.!?,。！？])/.test(right)) return {separator: '', overlap: 0};
  if (/^[.!?,。！？:;，]/.test(right)) return {separator: '', overlap: 0};
  if (TERMINATOR.test(tail.at(-1) || '') && /^["'”’）)\]}»」』】》〉*_`]+(?:\s|$)/.test(right)) {
    return {separator: '', overlap: 0};
  }
  return {separator: ' ', overlap: 0};
}

function sentenceEnd(text, position) {
  const mark = text[position];
  if (!TERMINATOR.test(mark)) return false;
  const before = text[position - 1] || '', after = text[position + 1] || '';
  if (mark === '.' && /[0-9]/.test(before) && /[0-9]/.test(after)) return false;
  if (mark === '.' && /[A-Za-z0-9]/.test(before) && /[A-Za-z0-9]/.test(after)) return false;
  let tokenStart = position;
  while (tokenStart > 0 && position - tokenStart < 200 && !/\s/.test(text[tokenStart - 1])) tokenStart -= 1;
  const token = text.slice(tokenStart, position + 1).replace(/^["'“‘([{]+/, '');
  // A question/exclamation mark inside an address is not sentence punctuation.
  if (/(?:https?:\/\/|www\.|@)/i.test(token) && after && !/\s/.test(after)
      && !CLOSER.test(after) && !TERMINATOR.test(after)) return false;
  if (mark === '.' && (ABBREVIATION.test(token) || /^(?:[A-Za-z]\.)+$/.test(token))) return false;
  return true;
}

function ranges(text) {
  const result = [];
  let start = 0, position = 0, lastSpace = -1;
  const emit = (end, complete) => {
    let from = start, to = end;
    while (from < to && /\s/.test(text[from])) from += 1;
    while (to > from && /\s/.test(text[to - 1])) to -= 1;
    if (from < to) result.push({from, to, complete});
    start = end; lastSpace = -1;
  };
  while (position < text.length) {
    if (start === position && /\s/.test(text[position])) { start += 1; position += 1; continue; }
    if (sentenceEnd(text, position)) {
      let end = position + 1;
      while (end < text.length && (TERMINATOR.test(text[end]) || CLOSER.test(text[end]))) end += 1;
      emit(end, true); position = end; continue;
    }
    if (text[position] === '\n' && /^\n[\t \r]*\n/.test(text.slice(position, position + 80))) {
      emit(position, false); start = position + 1;
    }
    if (/\s/.test(text[position]) && position - start >= PARAGRAPH_MIN) lastSpace = position;
    if (position - start >= PARAGRAPH_MAX) {
      let end = lastSpace >= start + PARAGRAPH_MIN ? lastSpace : position;
      // A paragraph boundary must not split a Unicode surrogate pair.
      if (end > start && /[\uD800-\uDBFF]/.test(text[end - 1])) end -= 1;
      emit(end, false); position = end; continue;
    }
    position += 1;
  }
  emit(text.length, false);
  return result;
}

/**
 * Derive readable sentences without changing ASR segments or interpreting HTML.
 * Source times stay approximate: a sentence split within a source retains that
 * source's time range. The last fragment is always returned immediately.
 */
export function groupTranscriptSentences(segments) {
  if (!Array.isArray(segments)) return [];
  const rows = [], occurrences = new Map();
  let run = '', sources = [], previousEnd, trailingWhitespace = '';
  const flush = () => {
    let cursor = 0;
    for (const range of ranges(run)) {
      while (cursor < sources.length && sources[cursor].to <= range.from) cursor += 1;
      const first = sources[cursor];
      if (!first) continue;
      const sourceIds = [], seen = new Set();
      let start, end;
      for (let index = cursor; index < sources.length && sources[index].from < range.to; index += 1) {
        const source = sources[index];
        if (source.id !== undefined && !seen.has(source.id)) { sourceIds.push(source.id); seen.add(source.id); }
        if (source.start !== undefined) start = start === undefined ? source.start : Math.min(start, source.start);
        if (source.end !== undefined) end = end === undefined ? source.end : Math.max(end, source.end);
      }
      const offset = first.offset + Math.max(0, range.from - first.from);
      const row = {
        id: `sentence:${first.identity}:${offset}`,
        text: run.slice(range.from, range.to), sourceIds, complete: range.complete,
      };
      if (start !== undefined) row.start = start;
      if (end !== undefined) row.end = end;
      rows.push(row);
    }
    run = ''; sources = []; previousEnd = undefined; trailingWhitespace = '';
  };
  segments.forEach((segment, index) => {
    if (!segment || typeof segment.text !== 'string' || !segment.text.trim()) return;
    const text = segment.text.trim();
    const offset = segment.text.length - segment.text.trimStart().length;
    const start = time(segment.start), candidateEnd = time(segment.end);
    const end = candidateEnd !== undefined && (start === undefined || candidateEnd >= start) ? candidateEnd : undefined;
    if (run && start !== undefined && previousEnd !== undefined && start - previousEnd > MAX_GAP_SECONDS) flush();
    const join = run ? boundary(run, text, trailingWhitespace + segment.text.slice(0, offset)) : {separator: '', overlap: 0};
    const from = run.length - join.overlap + join.separator.length;
    run += join.separator + text.slice(join.overlap);
    const rawId = segment.id ?? segment.segment_id;
    const id = (typeof rawId === 'string' && rawId) || (typeof rawId === 'number' && Number.isFinite(rawId))
      ? rawId : undefined;
    const occurrence = occurrences.get(id) || 0;
    if (id !== undefined) occurrences.set(id, occurrence + 1);
    const identity = id === undefined ? `index:${index}` : `id:${JSON.stringify([typeof id, id, occurrence])}`;
    sources.push({from, to: run.length, id, identity, offset, start, end});
    previousEnd = end ?? start;
    trailingWhitespace = segment.text.slice(segment.text.trimEnd().length);
  });
  flush();
  return rows;
}
