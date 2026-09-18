import {IMPORT_PART_BYTES,MAX_RECORDING_FILE_BYTES,recordingFileFingerprint} from './file-import.js';
import {mergeLocalAudioExportParts} from './local-audio-export.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const BRIDGE_NAME = /^보관음성_([0-9a-f-]{36})_(0|[1-9][0-9]*)-(0|[1-9][0-9]*)\.wav$/;
const MAX_CAPTURE_SAMPLES = 16000*4*60*60;
const MAX_HINTS = 1000;
const MAX_CAPTURE_HINTS = 128;
const MAX_STORED_CHARACTERS = 512*1024;
const STORAGE_KEY = 'yeobaek-held-import-ids-v1';
const REQUEST_TIMEOUT_MS = 15000;
const RECEIPT_KEYS = ['id','filename','total_bytes','file_fingerprint','lecture_id'];

function fail() { throw new Error('완료한 파일 변환과 현재 보관 음성이 같은지 확인하지 못했습니다. 원본과 보류 상태는 유지합니다.'); }
function cancelled(signal) {
  if(signal?.aborted) {
    const error=new Error('보관 음성의 파일 변환 확인을 취소했습니다.');error.name='AbortError';throw error;
  }
}
function uuid(value) { return typeof value==='string' && UUID.test(value); }
function scope(owner,server) {
  if(typeof owner!=='string' || !owner || owner!==owner.trim() || Array.from(owner).length>32
      || /[\u0000-\u001f\u007f]/.test(owner) || typeof server!=='string' || server.length>2048)return null;
  try {
    const url=new URL(server);
    if(!['https:','http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash
        || url.pathname!=='/')return null;
    return {owner,server:url.origin};
  }catch{return null;}
}
function bridgeFilename(value) {
  if(typeof value!=='string' || value.length>160)return null;
  const match=BRIDGE_NAME.exec(value);if(!match || !uuid(match[1]))return null;
  const start=Number(match[2]),end=Number(match[3]);
  if(!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start<0 || end<=start || end>MAX_CAPTURE_SAMPLES)return null;
  return {captureId:match[1],startSamples:start,endSamples:end};
}
function filename(captureId,part) { return `보관음성_${captureId}_${part.startSamples}-${part.endSamples}.wav`; }

/** Optional hints only. Every completion is re-proven against authenticated GETs. */
export class HeldImportReceiptStore {
  constructor({storage}={}) {
    if(storage===undefined){try{storage=globalThis.localStorage;}catch{storage=null;}}
    this.storage=storage;
  }
  _read() {
    try {
      const raw=this.storage?.getItem(STORAGE_KEY);
      if(typeof raw!=='string' || raw.length>MAX_STORED_CHARACTERS)return [];
      const value=JSON.parse(raw);
      if(!value || value.version!==1 || !Array.isArray(value.ids) || value.ids.length>MAX_HINTS)return [];
      return value.ids.filter(row=>{
        if(!row || typeof row!=='object' || Object.keys(row).length!==4
            || !['owner','server','captureId','importId'].every(key=>Object.hasOwn(row,key)))return false;
        const normalized=scope(row.owner,row.server);
        return normalized && normalized.server===row.server && uuid(row.captureId) && uuid(row.importId);
      });
    }catch{return [];}
  }
  remember(owner,server,state) {
    const normalized=scope(owner,server), parsed=bridgeFilename(state?.filename);
    if(!normalized || !parsed || !uuid(state?.id))return false;
    try {
      const row={...normalized,captureId:parsed.captureId,importId:state.id};
      const sameScope=old=>old.owner===row.owner && old.server===row.server && old.captureId===row.captureId;
      const previous=this._read().filter(old=>!(sameScope(old) && old.importId===row.importId));
      const scoped=previous.filter(sameScope);
      const discard=new Set(scoped.slice(0,Math.max(0,scoped.length-MAX_CAPTURE_HINTS+1)));
      const rows=previous.filter(old=>!discard.has(old));
      rows.push(row);
      if(rows.length>MAX_HINTS)rows.splice(0,rows.length-MAX_HINTS);
      let raw=JSON.stringify({version:1,ids:rows});
      while(raw.length>MAX_STORED_CHARACTERS && rows.length>1){rows.shift();raw=JSON.stringify({version:1,ids:rows});}
      if(raw.length>MAX_STORED_CHARACTERS || typeof this.storage?.setItem!=='function')return false;
      this.storage.setItem(STORAGE_KEY,raw);return true;
    }catch{return false;}
  }
  ids(owner,server,captureId) {
    const normalized=scope(owner,server);if(!normalized || !uuid(captureId))return [];
    return [...new Set(this._read().filter(row=>row.owner===normalized.owner && row.server===normalized.server
      && row.captureId===captureId).map(row=>row.importId))];
  }
}

function receipt(state) {
  if(!state || !uuid(state.id) || !uuid(state.lecture_id) || state.status!=='completed'
      || !bridgeFilename(state.filename) || !Number.isSafeInteger(state.total_bytes)
      || state.total_bytes<46 || state.total_bytes>MAX_RECORDING_FILE_BYTES
      || state.uploaded_bytes!==state.total_bytes || state.next_offset!==state.total_bytes
      || state.part_bytes!==IMPORT_PART_BYTES || typeof state.raw_deleted!=='boolean'
      || typeof state.cancel_requested!=='boolean' || ![null,''].includes(state.error)
      || typeof state.file_fingerprint!=='string' || !SHA256.test(state.file_fingerprint))return null;
  return Object.fromEntries(RECEIPT_KEYS.map(key=>[key,state[key]]));
}
function sameReceipt(left,right) { return RECEIPT_KEYS.every(key=>left[key]===right[key]); }
function validSavedReceipt(value) {
  return value && typeof value==='object' && Object.keys(value).length===RECEIPT_KEYS.length
    && RECEIPT_KEYS.every(key=>Object.hasOwn(value,key)) && uuid(value.id) && uuid(value.lecture_id)
    && !!bridgeFilename(value.filename) && Number.isSafeInteger(value.total_bytes)
    && value.total_bytes>=46 && value.total_bytes<=MAX_RECORDING_FILE_BYTES
    && typeof value.file_fingerprint==='string' && SHA256.test(value.file_fingerprint);
}
async function getState(id,request,signal,{allowMissing=false}={}) {
  cancelled(signal);
  try {
    const state=await request(`/imports/${id}`,{signal},REQUEST_TIMEOUT_MS);
    cancelled(signal);
    if(state?.id!==id)fail();
    return receipt(state);
  }catch(error){
    cancelled(signal);
    if(allowMissing && error?.status===404)return null;
    throw error;
  }
}
async function fingerprint(blob,name,signal) {
  cancelled(signal);
  // recordingFileFingerprint reads at most one 480 KiB part. Check cancellation
  // around each read, including when the crypto digest yielded to another task.
  const file={name,size:blob.size,slice(start,end){
    const slice=blob.slice(start,end);
    return {async arrayBuffer(){cancelled(signal);const bytes=await slice.arrayBuffer();cancelled(signal);return bytes;}};
  }};
  const result=await recordingFileFingerprint(file);cancelled(signal);return result;
}

/**
 * Caller supplies every readable part from exactly one owned, durable capture.
 * This proves file completion only; the caller separately rechecks the current
 * held manifest and performs the user's explicit, non-destructive closure.
 */
export async function verifyHeldImportCompletion({captureId,parts,request,knownIds=[],signal}={}) {
  cancelled(signal);
  if(!uuid(captureId) || typeof request!=='function' || !Array.isArray(knownIds) || knownIds.length>MAX_HINTS
      || !Array.isArray(parts))fail();
  const pinned=parts.map(part=>part && ({blob:part.blob,startSamples:part.startSamples,
    endSamples:part.endSamples,durationSamples:part.durationSamples})).sort((a,b)=>(a?.startSamples || 0)-(b?.startSamples || 0));
  const merged=await mergeLocalAudioExportParts(pinned,{signal});
  cancelled(signal);
  const expected=new Map(pinned.map((part,index)=>[filename(captureId,part),{blob:part.blob,indexes:[index]}]));
  if(pinned.length>1)expected.set(filename(captureId,merged),{blob:merged.blob,indexes:pinned.map((_,index)=>index)});
  const list=await request('/imports',{signal},REQUEST_TIMEOUT_MS);cancelled(signal);
  const rows=Array.isArray(list) ? list : list?.imports;
  if(!Array.isArray(rows) || rows.length>20)fail();
  // Prefer the recent whole-file candidate, then recent individual parts.
  // Only consult older optional hints while there is still uncovered audio.
  const recent=[];
  for(const row of rows) {
    const candidate=receipt(row), target=candidate && expected.get(candidate.filename);
    if(target && target.blob.size===candidate.total_bytes)recent.push({id:candidate.id,coverage:target.indexes.length});
  }
  recent.sort((a,b)=>b.coverage-a.coverage);
  const ids=new Set([...recent.map(value=>value.id),...knownIds.filter(uuid).slice(-MAX_CAPTURE_HINTS).reverse()]);
  const matches=new Map();
  for(const id of ids) {
    const candidate=await getState(id,request,signal,{allowMissing:true});
    const target=candidate && expected.get(candidate.filename);
    if(!target || target.blob.size!==candidate.total_bytes)continue;
    if(!target.fingerprint)target.fingerprint=await fingerprint(target.blob,candidate.filename,signal);
    if(target.fingerprint!==candidate.file_fingerprint)continue;
    if(target.indexes.length===pinned.length) {
      cancelled(signal);
      return {completedParts:pinned.length,partCount:pinned.length,imports:[candidate],complete:true};
    }
    for(const index of target.indexes)if(!matches.has(index))matches.set(index,candidate);
    if(matches.size===pinned.length)break;
  }
  cancelled(signal);
  return {completedParts:matches.size,partCount:pinned.length,imports:[...matches.entries()].sort(([a],[b])=>a-b).map(([,value])=>value),
    complete:matches.size===pinned.length};
}

/** Freshly verify every selected receipt immediately before a manual closure. */
export async function recheckHeldImportCompletion(imports,request,{signal}={}) {
  cancelled(signal);
  if(!Array.isArray(imports) || !imports.length || imports.length>MAX_HINTS+20 || typeof request!=='function')fail();
  const pinned=imports.map(value=>{
    if(!validSavedReceipt(value))fail();
    return Object.fromEntries(RECEIPT_KEYS.map(key=>[key,value[key]]));
  });
  if(new Set(pinned.map(value=>value.id)).size!==pinned.length)fail();
  for(const previous of pinned){
    const latest=await getState(previous.id,request,signal);
    if(!latest || !sameReceipt(previous,latest))fail();
  }
  cancelled(signal);return true;
}
