import { MAX_RECORDING_FILE_BYTES } from './file-import.js';

const EXTENSIONS = /\.(?:wav|mp3|m4a|aac|flac|ogg|oga|opus|webm|mp4|mkv|mov)$/i;
const VIDEO_TYPES = new Set(['video/mp4','video/webm','video/quicktime','video/x-matroska']);

/** Picker and drop share these preflight checks; the server still decodes audio. */
export function validateRecordingSelection(file) {
  if (!file || typeof file.slice !== 'function' || !Number.isSafeInteger(file.size) || file.size <= 0) {
    throw new Error('내용이 있는 녹음 파일을 선택해 주세요. 폴더는 가져올 수 없어요.');
  }
  if (file.size > MAX_RECORDING_FILE_BYTES) throw new Error('녹음 파일은 1 GiB 이하여야 합니다. 더 긴 파일은 나눠서 올려 주세요.');
  if (typeof file.name !== 'string' || !file.name.trim()) throw new Error('파일 이름을 확인할 수 없습니다.');
  if (file.webkitRelativePath) throw new Error('폴더 대신 녹음 파일 한 개를 선택해 주세요.');
  const type = String(file.type || '').toLowerCase();
  if (!EXTENSIONS.test(file.name) && !type.startsWith('audio/') && !VIDEO_TYPES.has(type)) {
    throw new Error('지원하는 녹음·영상 파일 한 개를 선택해 주세요. WAV, MP3, M4A, FLAC, OGG, WebM, MP4 등을 사용할 수 있어요.');
  }
  return file;
}

export function isFileDrag(transfer) {
  return !!transfer && (Array.from(transfer.types || []).includes('Files')
    || Array.from(transfer.items || []).some(item => item.kind === 'file')
    || !!transfer.files?.length);
}

/** Pin File and optional handles synchronously, before DataTransfer is protected again. */
export async function recordingFileFromDrop(transfer) {
  const items = Array.from(transfer?.items || []), files = Array.from(transfer?.files || []);
  if (!isFileDrag(transfer)) throw new Error('텍스트나 링크 대신 녹음 파일 한 개를 놓아 주세요.');
  if (items.some(item => item.kind !== 'file') || items.length > 1 || files.length > 1) {
    throw new Error('녹음 파일은 한 번에 한 개만 가져올 수 있어요. 폴더나 여러 파일은 나누어 선택해 주세요.');
  }
  const item = items[0];
  let entry = null, handleRequest = null;
  try {
    if (typeof item?.webkitGetAsEntry === 'function') entry = item.webkitGetAsEntry();
    else if (typeof item?.getAsFileSystemHandle === 'function') handleRequest = item.getAsFileSystemHandle();
  } catch { throw new Error('끌어 놓은 항목을 확인하지 못했어요. 파일 선택 버튼으로 다시 골라 주세요.'); }
  if (entry?.isDirectory || (entry && !entry.isFile)) throw new Error('폴더 대신 녹음 파일 한 개를 놓아 주세요.');
  const file = files[0] || item?.getAsFile?.();
  if (handleRequest) {
    let timer;
    try {
      const handle = await Promise.race([handleRequest,new Promise((_,reject) => {
        timer = setTimeout(() => reject(new Error('파일 확인 시간이 초과됐어요. 파일 선택 버튼으로 다시 골라 주세요.')),2000);
      })]);
      if (!handle || handle.kind !== 'file') throw new Error('폴더 대신 녹음 파일 한 개를 놓아 주세요.');
    } finally { clearTimeout(timer); }
  }
  return validateRecordingSelection(file);
}
