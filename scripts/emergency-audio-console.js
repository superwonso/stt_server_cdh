/* Paste this entire file in the ORIGINAL affected tab's DevTools Console.
 * Local read-only rescue: no imports, network, authentication, ASR, or storage writes.
 * Do not close/reload that tab or clear site data before checking the downloaded ZIP.
 */
void (async () => {
  'use strict';
  const ID = 'yeobaek-emergency-audio';
  const existing = document.getElementById(ID);
  if (existing) {
    existing.querySelector('input:not([hidden]), a[download], button')?.focus();
    return;
  }
  const suggested = document.getElementById('current-user')?.textContent?.trim() || '';
  let owner = null, finishOwner = null;
  const LIMIT = 2 * 1024 * 1024 * 1024, MAX_FILES = 49999, BLOCK = 65536;
  const dialog = document.createElement('dialog'); dialog.id = ID;
  const heading = document.createElement('h2'); heading.textContent = '기기에 남은 음성 긴급 저장';
  const warning = document.createElement('p');
  warning.textContent = '원래 탭을 유지하세요. ZIP은 조각별 원본 WAV와 시간 설명서이며, 전체 수업 파일이 아닐 수 있습니다. 중첩은 제거하지 않습니다. 저장소나 전송 상태는 바꾸지 않습니다.';
  const progress = document.createElement('p'); progress.setAttribute('role','status');
  const form = document.createElement('form');
  const label = document.createElement('label'); label.htmlFor = `${ID}-owner`;
  label.textContent = '이 탭에서 녹음한 본인 아이디';
  const input = document.createElement('input'); input.id = `${ID}-owner`; input.type = 'text';
  input.maxLength = 32; input.autocomplete = 'off'; input.setAttribute('autocapitalize','none'); input.spellcheck = false;
  input.value = suggested;
  const start = document.createElement('button'); start.type = 'submit'; start.textContent = '내 음성 읽기 시작';
  form.append(label,input,start);
  const result = document.createElement('p');
  const close = document.createElement('button'); close.type = 'button'; close.textContent = '이 저장 창만 닫기';
  dialog.append(heading,warning,form,progress,result,close); document.body.append(dialog);
  let cancelled = false, objectUrl = null, transaction = null;
  const clean = () => {
    cancelled = true;
    finishOwner?.(null); finishOwner = null;
    try { transaction?.abort(); } catch {}
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null; dialog.remove();
  };
  close.onclick = () => dialog.close(); dialog.addEventListener('close',clean,{once:true});
  // Nonmodal: even a large CRC pass must leave the recording controls usable.
  dialog.show();
  progress.textContent = '본인 아이디를 확인하고 읽기 시작을 눌러 주세요. 기다리는 동안 녹음 화면은 계속 사용할 수 있습니다.';
  owner = await new Promise(resolve => {
    finishOwner = resolve;
    form.onsubmit = event => {
      event.preventDefault();
      const value = input.value.trim();
      if (!/^[a-z0-9](?:[a-z0-9._-]{0,30}[a-z0-9])?$/.test(value)) {
        progress.textContent = '아이디 형식을 확인해 주세요. 저장된 음성은 변경하지 않았습니다.'; input.focus(); return;
      }
      form.hidden = true; form.onsubmit = null; finishOwner = null; resolve(value);
    };
  });
  if (owner === null || cancelled) return;
  const stopIfClosed = () => { if (cancelled) throw new Error('cancelled'); };
  const waitTurn = () => new Promise(resolve => setTimeout(resolve,0));
  const warnings = [];
  const rows = [], sessions = [];
  let declaredBytes = 0;
  const integer = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const identifier = value => typeof value === 'string' && value.length <= 128 ? value : null;
  function pin(value, source) {
    if (!value || value.owner !== owner) return;
    if (rows.length >= MAX_FILES) throw new Error('limit');
    const blob = value.blob;
    if (blob instanceof Blob) {
      declaredBytes += blob.size;
      if (!Number.isSafeInteger(declaredBytes) || declaredBytes > LIMIT) throw new Error('limit');
    }
    rows.push({source,blob:blob instanceof Blob ? blob : null,capture_id:identifier(value.captureId),
      chunk_id:identifier(value.id),sequence:integer(value.sequence),
      start_samples:integer(value.startSamples),duration_samples:integer(value.durationSamples),
      overlap_samples:integer(value.overlapSamples),final:value.final === true,
      ...(Number.isFinite(value.startSeconds) ? {start_seconds:value.startSeconds} : {}),
      ...(Number.isFinite(value.durationSeconds) ? {duration_seconds:value.durationSeconds} : {}),
      ...(Number.isFinite(value.overlapSeconds) ? {overlap_seconds:value.overlapSeconds} : {})});
  }
  function wav(samples) {
    const buffer = new ArrayBuffer(44+samples.length*2), view = new DataView(buffer);
    const text = (at,value) => { for (let index=0;index<value.length;index++) view.setUint8(at+index,value.charCodeAt(index)); };
    text(0,'RIFF'); view.setUint32(4,buffer.byteLength-8,true); text(8,'WAVE'); text(12,'fmt ');
    view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
    view.setUint32(24,16000,true); view.setUint32(28,32000,true); view.setUint16(32,2,true);
    view.setUint16(34,16,true); text(36,'data'); view.setUint32(40,samples.length*2,true);
    for (let index=0;index<samples.length;index++) {
      const sample = Number.isFinite(samples[index]) ? Math.max(-1,Math.min(1,samples[index])) : 0;
      view.setInt16(44+index*2,Math.round(sample*(sample<0 ? 32768 : 32767)),true);
    }
    return new Blob([buffer],{type:'audio/wav'});
  }
  async function readStored() {
    let database;
    try {
      database = await new Promise((resolve,reject) => {
        let done = false;
        const request = indexedDB.open('yeobaek-live-audio');
        const timer = setTimeout(() => { if (!done) { done=true; reject(new Error('idb-timeout')); } },10000);
        request.onupgradeneeded = () => { try { request.transaction.abort(); } catch {} };
        request.onerror = () => { if (!done) { done=true; clearTimeout(timer); reject(new Error('idb-unavailable')); } };
        request.onsuccess = () => {
          clearTimeout(timer);
          if (done || cancelled) { request.result.close(); if (!done) reject(new Error('cancelled')); return; }
          done=true; resolve(request.result);
        };
      });
      stopIfClosed();
      const names = ['sessions','chunks','pcmSnapshots'].filter(name => database.objectStoreNames.contains(name));
      if (!names.length) { warnings.push('읽을 수 있는 기존 음성 저장소가 없습니다.'); return; }
      await new Promise((resolve,reject) => {
        transaction = database.transaction(names,'readonly');
        const timer = setTimeout(() => { try { transaction.abort(); } catch {} },15000);
        transaction.oncomplete = () => { clearTimeout(timer); transaction=null; resolve(); };
        transaction.onabort = transaction.onerror = () => { clearTimeout(timer); transaction=null; reject(new Error('idb-read-failed')); };
        for (const name of names) {
          const store = transaction.objectStore(name);
          const indexName = {sessions:'ownerCreated',chunks:'ownerOrder',pcmSnapshots:'owner'}[name];
          if (!store.indexNames.contains(indexName)) {
            warnings.push(`${name}의 계정별 읽기 인덱스가 없어 해당 영역을 읽지 않았습니다.`); continue;
          }
          const scopedIndex=store.index(indexName), keyPath=scopedIndex.keyPath;
          if (name === 'pcmSnapshots' ? keyPath !== 'owner' : !Array.isArray(keyPath) || keyPath[0] !== 'owner') {
            warnings.push(`${name}의 계정별 읽기 범위를 확인하지 못해 해당 영역을 읽지 않았습니다.`); continue;
          }
          const request=scopedIndex.openCursor(name === 'pcmSnapshots' ? IDBKeyRange.only(owner) : IDBKeyRange.bound([owner],[owner,[]]));
          request.onsuccess = () => {
            const cursor = request.result;
            if (!cursor) return;
            const value = cursor.value;
            try {
              stopIfClosed();
              if (value?.owner === owner) {
                if (name === 'sessions') {
                  if (sessions.length >= MAX_FILES) throw new Error('limit');
                  sessions.push({capture_id:identifier(value.id),state:identifier(value.state),
                    captured_samples:integer(value.capturedSamples),next_sequence:integer(value.nextSequence)});
                } else pin(value,name === 'chunks' ? 'indexeddb_chunk' : 'indexeddb_snapshot');
              }
              cursor.continue();
            } catch (error) {
              if (error.message === 'limit') warnings.push('limit');
              try { transaction.abort(); } catch {}
            }
          };
        }
      });
    } finally { database?.close(); }
  }
  const table = new Uint32Array(256);
  for (let index=0;index<256;index++) {
    let crc=index;
    for (let bit=0;bit<8;bit++) crc=(crc>>>1)^((crc&1) ? 0xedb88320 : 0);
    table[index]=crc>>>0;
  }
  async function inspect(blob) {
    if (!(blob instanceof Blob)) throw new Error('unreadable');
    let crc=0xffffffff, canonical=blob.size >= 46 && (blob.size-44)%2 === 0;
    for (let offset=0;offset<blob.size;offset+=BLOCK) {
      stopIfClosed();
      const bytes=new Uint8Array(await blob.slice(offset,Math.min(offset+BLOCK,blob.size)).arrayBuffer());
      if (bytes.length !== Math.min(BLOCK,blob.size-offset)) throw new Error('unreadable');
      if (offset === 0 && canonical) {
        const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
        const text=(at,length)=>String.fromCharCode(...bytes.subarray(at,at+length));
        canonical=text(0,4)==='RIFF' && text(8,4)==='WAVE' && text(12,4)==='fmt '
          && view.getUint32(4,true)===blob.size-8 && view.getUint32(16,true)===16
          && view.getUint16(20,true)===1 && view.getUint16(22,true)===1
          && view.getUint32(24,true)===16000 && view.getUint32(28,true)===32000
          && view.getUint16(32,true)===2 && view.getUint16(34,true)===16
          && text(36,4)==='data' && view.getUint32(40,true)===blob.size-44;
      }
      for (const byte of bytes) crc=(crc>>>8)^table[(crc^byte)&255];
      await waitTurn();
    }
    return {crc:(crc^0xffffffff)>>>0,canonical};
  }
  const encoder = new TextEncoder();
  const files = [], manifestRows = [];
  function zip(entries) {
    const parts=[], central=[]; let offset=0, centralBytes=0;
    for (const entry of entries) {
      const name=encoder.encode(entry.name), local=new Uint8Array(30+name.length), v=new DataView(local.buffer);
      v.setUint32(0,0x04034b50,true); v.setUint16(4,20,true); v.setUint16(6,0x0800,true);
      v.setUint16(12,33,true); v.setUint32(14,entry.crc,true); v.setUint32(18,entry.blob.size,true);
      v.setUint32(22,entry.blob.size,true); v.setUint16(26,name.length,true); local.set(name,30);
      const header=new Uint8Array(46+name.length), c=new DataView(header.buffer);
      c.setUint32(0,0x02014b50,true); c.setUint16(4,20,true); c.setUint16(6,20,true);
      c.setUint16(8,0x0800,true); c.setUint16(14,33,true); c.setUint32(16,entry.crc,true);
      c.setUint32(20,entry.blob.size,true); c.setUint32(24,entry.blob.size,true);
      c.setUint16(28,name.length,true); c.setUint32(42,offset,true); header.set(name,46);
      parts.push(local,entry.blob); central.push(header); centralBytes+=header.length; offset+=local.length+entry.blob.size;
      if (offset+centralBytes+22 > 0xffffffff) throw new Error('limit');
    }
    const end=new Uint8Array(22), e=new DataView(end.buffer);
    e.setUint32(0,0x06054b50,true); e.setUint16(8,entries.length,true); e.setUint16(10,entries.length,true);
    e.setUint32(12,centralBytes,true); e.setUint32(16,offset,true);
    return new Blob([...parts,...central,end],{type:'application/zip'});
  }
  try {
    progress.textContent='이 계정의 기기 음성을 읽는 중입니다. 녹음 탭은 그대로 유지해 주세요.';
    const ram=globalThis.__yeobaekEmergencyRam;
    let ramIncluded=false;
    if (ram && ram.owner === owner) {
      for (const [key,source] of [['chunks','ram_chunk'],['snapshots','ram_snapshot']]) {
        if (Array.isArray(ram[key])) for (const value of ram[key]) pin(value,source);
      }
      const tail=ram.tail;
      if (tail?.owner === owner && tail.samples instanceof Float32Array && tail.samples.length <= 240000) {
        const samples=tail.samples.slice(); pin({...tail,blob:wav(samples)},'ram_unfinished_tail');
      } else if (tail) warnings.push('RAM의 마지막 미분할 음성 형식을 확인하지 못했습니다. 원래 탭을 유지하세요.');
      ramIncluded=true;
    } else warnings.push('브라우저 저장소(IDB)만 읽습니다. 원래 탭의 RAM에만 남은 음성은 포함되지 않습니다.');
    try { await readStored(); }
    catch { if (warnings.includes('limit')) throw new Error('limit'); warnings.push('브라우저 저장소를 완전히 읽지 못했습니다. 읽은 항목만 제공하므로 원래 탭을 유지하세요.'); }
    stopIfClosed();
    if (warnings.includes('limit')) throw new Error('limit');
    const groups=new Map(); let invalid=0, unreadable=0;
    for (let index=0;index<rows.length;index++) {
      stopIfClosed();
      const row=rows[index], {blob,...metadata}=row;
      const key=row.capture_id || 'unknown';
      if (!groups.has(key)) groups.set(key,groups.size+1);
      const filename=`capture-${String(groups.get(key)).padStart(4,'0')}/${String(index+1).padStart(5,'0')}-${row.source}.wav`;
      const entry={...metadata,filename,bytes:blob?.size ?? null,status:'unreadable'};
      progress.textContent=`음성 조각 검사 중: ${index+1} / ${rows.length}. 원본을 변경하지 않습니다.`;
      try {
        const checked=await inspect(blob);
        entry.status=checked.canonical ? 'original_wav' : blob.size === 0 ? 'empty_original_preserved' : 'noncanonical_original_preserved';
        if (!checked.canonical) invalid++;
        files.push({name:filename,blob,crc:checked.crc});
      } catch { stopIfClosed(); unreadable++; }
      manifestRows.push(entry);
    }
    const manifest={format:'yeobaek-emergency-original-audio-v1',created_at:new Date().toISOString(),sample_rate:16000,
      explanation:'Original individual WAV chunks/snapshots, NOT a merged or guaranteed complete class recording. Overlap is NOT removed. A chunk can appear in both RAM and IndexedDB. Use sample positions before combining. No source data or queue state was changed.',
      ram_marker_used:ramIncluded,warnings,noncanonical_or_empty_files:invalid,unreadable_files:unreadable,
      stored_sessions:sessions,files:manifestRows};
    const manifestBlob=new Blob([JSON.stringify(manifest,null,2)],{type:'application/json'});
    const checked=await inspect(manifestBlob);
    files.push({name:'manifest.json',blob:manifestBlob,crc:checked.crc});
    stopIfClosed();
    const archive=zip(files); objectUrl=URL.createObjectURL(archive);
    const link=document.createElement('a'); link.href=objectUrl; link.download='yeobaek-emergency-audio.zip';
    link.textContent=`ZIP 내려받기 (${files.length-1}개 원본 파일 · ${(archive.size/1024/1024).toFixed(2)} MiB)`;
    link.onclick=() => { progress.textContent='브라우저 다운로드 목록에서 저장 완료와 ZIP 내용을 확인하세요. 저장 창을 닫기 전에는 링크가 유지됩니다. 원래 녹음 탭은 닫지 마세요.'; };
    result.append(link);
    progress.textContent=[`${files.length-1}개 원본 파일을 준비했습니다. ZIP 안 manifest.json에 시간·중첩·누락 정보를 기록했습니다.`,
      `${invalid}개 비어 있거나 비표준인 파일도 읽을 수 있는 원본 그대로 포함했습니다.`,
      `${unreadable}개 파일은 읽지 못해 음성을 포함하지 못했고 설명서에 표시했습니다.`,...warnings].join(' ');
  } catch (error) {
    if (!cancelled) progress.textContent=error.message === 'limit'
      ? '안전 한도(원본 2GiB 또는 약 5만 파일)를 초과해 ZIP을 만들지 않았습니다. 원래 탭과 저장소를 유지하고 도움을 요청하세요.'
      : '음성을 안전하게 읽거나 ZIP을 만들지 못했습니다. 원본을 변경하지 않았습니다. 원래 탭과 사이트 데이터를 유지해 주세요.';
  }
})();
