// Only canonical server fallback envelopes can become readable, uncited results.
export const RESULT_WARNING = '일부 내용을 확인하지 못했습니다. 원문과 함께 확인해 주세요.';
const warningCodes = new Set([
  'validation_failed','invalid_response','unsupported_claim','response_truncated','model_refused',
  'placeholder_unresolved','content_limited','incomplete_batches','context_unverified',
]);
export function validResultWarnings(value) {
  return Array.isArray(value) && value.length > 0 && value.length <= 16
    && value.every(code => typeof code === 'string' && warningCodes.has(code))
    && new Set(value).size === value.length;
}
export function validResultDraft(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).sort().join(',') !== 'format,text,warnings' || value.format !== 'draft'
      || typeof value.text !== 'string' || !value.text.trim() || value.text.length > 500000
      || Array.from(value.text).length > 250000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/u.test(value.text)
      || !validResultWarnings(value.warnings)) return null;
  try { if (new TextEncoder().encode(JSON.stringify(value)).length > 2000000) return null; }
  catch { return null; }
  return {format:'draft',text:value.text,warnings:[...value.warnings]};
}
