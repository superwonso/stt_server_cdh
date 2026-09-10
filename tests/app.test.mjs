import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { performance as hostPerformance } from 'node:perf_hooks';
import { setTimeout as hostDelay } from 'node:timers/promises';
import { encodeWav } from '../web/audio.js';
import * as TestLocalAudioExport from '../web/local-audio-export.js';
import * as TestRecordingFileSelection from '../web/recording-file-selection.js';
import { renderDriveStatus } from '../web/admin-storage.js';
import { renderMaintenanceStatus } from '../web/admin-maintenance.js';
import { readRecordingClip, RecordingClipPlayer, filterTranscript } from '../web/recording-review.js';
import * as TestLectureLibrary from '../web/lecture-library.js';
import * as TestManualNotes from '../web/manual-notes.js';
import * as TestLectureQuestions from '../web/lecture-questions.js';
import * as TestStudyNotes from '../web/study-notes.js';
import { AUTH_SESSION_STORAGE_KEY, TabAuthSessionStore } from '../web/auth-session.js';

const source = (await readFile(new URL('../web/app.js', import.meta.url), 'utf8'))
  .replace("import { MicrophoneCapture } from './audio.js';", 'const MicrophoneCapture = TestCapture;')
  .replace("import { validateRecordingSelection, isFileDrag, recordingFileFromDrop } from './recording-file-selection.js';", 'const { validateRecordingSelection, isFileDrag, recordingFileFromDrop } = TestRecordingFileSelection;')
  .replace("import { buildRecoverableLocalAudioExports, validateLocalWav } from './local-audio-export.js';", 'const { buildRecoverableLocalAudioExports, validateLocalWav } = TestLocalAudioExport;')
  .replace("import { renderDriveStatus } from './admin-storage.js';", 'const renderDriveStatus = TestRenderDriveStatus;')
  .replace("import { renderMaintenanceStatus } from './admin-maintenance.js';", 'const renderMaintenanceStatus = TestRenderMaintenanceStatus;')
  .replace("import { readRecordingClip, RecordingClipPlayer, filterTranscript } from './recording-review.js';", 'const { readRecordingClip, RecordingClipPlayer, filterTranscript } = TestRecordingReview;')
  .replace("import { lectureTitle, libraryOptions, validMetadata, validLibrarySearch } from './lecture-library.js';", 'const { lectureTitle, libraryOptions, validMetadata, validLibrarySearch } = TestLectureLibrary;')
  .replace("import { validManualState, manualSegments, validManualHistory } from './manual-notes.js';", 'const { validManualState, manualSegments, validManualHistory } = TestManualNotes;')
  .replace("import { validQuestionJob, validQuestionPage } from './lecture-questions.js';", 'const { validQuestionJob, validQuestionPage } = TestLectureQuestions;')
  .replace("import { studyNoteSourceSnapshot, validateStudyNoteResponse, appendStudyNoteText } from './study-notes.js';", 'const { studyNoteSourceSnapshot, validateStudyNoteResponse, appendStudyNoteText } = TestStudyNotes;')
  .replace("import { FileImportCancelledError, RecordingFileUploader, isTerminalImportState } from './file-import.js';", `
    const FileImportCancelledError = class extends Error {};
    const RecordingFileUploader = TestFileUploader;
    const isTerminalImportState = state => ['completed','failed','cancelled'].includes(state?.status);
  `)
  .replace("import { liveCoordination } from './live-coordination.js';", 'const liveCoordination = TestLiveCoordination;')
  .replace("import { TabAuthSessionStore } from './auth-session.js';", 'const TabAuthSessionStore = TestAuthSessionStore;')
  .replace(`import {
  DurableLiveQueue,
  estimateStorage,
  isLiveQueueUnavailableError,
  requestPersistentStorage,
} from './live-queue.js';`, `
    const DurableLiveQueue = TestDurableLiveQueue;
    const estimateStorage = async () => ({supported:true,usage:0,quota:1024 ** 3,remaining:1024 ** 3});
    const isLiveQueueUnavailableError = () => false;
    const requestPersistentStorage = async () => ({supported:true,persisted:true,requested:false});
  `)
  .replace('void init();', '');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function until(predicate, label = 'asynchronous operation', {timeoutMs = 5000} = {}) {
  // WebCrypto/Blob work may finish on another thread. Two hundred immediate
  // turns can elapse in under 10 ms on CI without that work completing. Use a
  // host monotonic deadline, separate from the intentionally frozen VM clock.
  const deadline = hostPerformance.now() + timeoutMs;
  while (hostPerformance.now() < deadline) {
    if (predicate()) return;
    await hostDelay(1);
  }
  assert.ok(predicate(),`${label} did not settle`);
}
const response = (value, status = 200, headers = {}) => ({
  status, ok:status < 400, json:async () => value,
  headers:{get(name){ return headers[name] ?? headers[name.toLowerCase()] ?? null; }},
});
const chunk = (startSeconds, durationSeconds = 8, overlapSeconds = 0, final = false) => ({
  blob:encodeWav(new Float32Array(Math.max(1, Math.round(durationSeconds * 16000))).fill(0.25)),
  startSeconds,durationSeconds,overlapSeconds,final,
});
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const requireUuid = (value, label) => {
  if (typeof value !== 'string' || !UUID.test(value)) throw new TypeError(`${label} must be a UUID`);
  return value.toLowerCase();
};
const requireOwner = value => {
  if (typeof value !== 'string' || value !== value.trim() || !value) throw new TypeError('owner is invalid');
  return value;
};
const isoSeconds = milliseconds => new Date(Math.floor(milliseconds / 1000) * 1000).toISOString().replace('.000Z','Z');
function runtimeConfig({state = 'online', apiUrl = 'https://fresh-tunnel.trycloudflare.com', publishedMs = Date.now() - 60000, expiresMs = publishedMs + 86400000} = {}) {
  return {version:1,state,apiUrl,publishedAt:isoSeconds(publishedMs),expiresAt:isoSeconds(expiresMs)};
}
function deferred() { let resolve, reject; const promise = new Promise((yes,no) => { resolve = yes; reject = no; }); return {promise,resolve,reject}; }
function setup(fetch, { FileUploader = class { detach() {} }, storedServer = '', sessionItems = new Map(), translationApi = false, cryptoImplementation = webcrypto } = {}) {
  const elements = new Map(), createdElements = new Map(), intervals = new Map(), timeouts = new Map(), objectUrls = new Map();
  const documentListeners = new Map();
  const location = {hash:'',hostname:'student.github.io',pathname:'/classroom/',search:''};
  const historyCalls = [], clipboardWrites = [], storageWrites = [];
  let storedServerValue = storedServer;
  const tabStorage = {getItem:key => sessionItems.get(key) ?? null,
    setItem:(key,value) => { storageWrites.push([key,String(value)]); sessionItems.set(key,String(value)); },removeItem:key => sessionItems.delete(key)};
  let id = 0, mic, lastLiveQueue;
  const makeElement = (name, value = '') => {
    const node = {
    name,tagName:name.toUpperCase(),value,style:{},dataset:{},children:[],open:false,
    classList:{values:new Set(),toggle(className,force){
      const enabled = force === undefined ? !this.values.has(className) : !!force;
      if (enabled) this.values.add(className); else this.values.delete(className);
      return enabled;
    }},
    replaceChildren(...children){
      for (const child of this.children) child.parentNode = null;
      this.children = [];
      this.append(...children);
    },
    append(...children){ for (const child of children) this.insertBefore(child,null); },
    insertBefore(child,before){
      if (child === before) return child;
      if (before !== null && !this.children.includes(before)) throw new Error('reference node is not a child');
      child.parentNode?.removeChild(child);
      const index = before === null ? this.children.length : this.children.indexOf(before);
      this.children.splice(index,0,child);
      child.parentNode = this;
      return child;
    },
    removeChild(child){
      const index = this.children.indexOf(child);
      if (index < 0) throw new Error('node is not a child');
      this.children.splice(index,1); child.parentNode = null;
      return child;
    },
    querySelectorAll(selector){
      const matches = [];
      const visit = node => {
        if (!node || typeof node !== 'object') return;
        if (selector === 'button' && node.tagName === 'BUTTON') matches.push(node);
        for (const child of node.children || []) visit(child);
      };
      for (const child of this.children) visit(child);
      return matches;
    },
    focus(){ this.focused = true; },
    pause(){ this.paused = true; },
    async play(){ this.paused = false; },
    load(){ this.loadCalls = (this.loadCalls || 0) + 1; },
    showModal(){ this.open = true; },
    close(){ this.open = false; },
    removeAttribute(attribute){ delete this[attribute]; },
    setAttribute(attribute,value){ this[attribute] = String(value); },
    click(){ this.clicked = true; },
    };
    let textValue, textNode = null;
    Object.defineProperties(node,{
      parentNode:{value:null,writable:true},
      firstChild:{get(){ return this.children[0] || textNode; }},
      textContent:{enumerable:true,get(){ return textValue; },set(value){
        textValue = value;
        textNode = {nodeValue:String(value)};
      }},
    });
    if (name === 'recording-file') {
      let fileValue = '', selectedFiles = [];
      Object.defineProperties(node,{
        value:{configurable:true,get(){ return fileValue; },set(next){
          fileValue = String(next ?? '');
          if (!fileValue) selectedFiles = [];
        }},
        files:{configurable:true,get(){ return selectedFiles; },set(next){
          selectedFiles = Array.from(next || []);
          fileValue = selectedFiles.length ? 'selected-file' : '';
        }},
      });
    }
    return node;
  };
  const element = name => {
    const initialValue = name === 'language' ? 'ko' : name === 'asr-provider' ? 'qwen' : name === 'audio-source' ? 'microphone' : name === 'export-format' ? 'text' : '';
    if (!elements.has(name)) {
      const node = makeElement(name,initialValue);
      elements.set(name,node);
    }
    return elements.get(name);
  };
  const TestURL = class extends URL {};
  TestURL.createObjectURL = blob => { const value = `blob:test/${++id}`; objectUrls.set(value,blob); return value; };
  TestURL.revokeObjectURL = value => objectUrls.delete(value);
  const createElement = tag => {
    const node = makeElement(tag);
    const values = createdElements.get(tag) || [];
    values.push(node); createdElements.set(tag,values);
    return node;
  };
  const coordination = {
    captureOwners:[], uploaderOwners:[], destructiveCalls:[], releasedCaptureLeases:0,
    async acquireLiveCapture(owner) {
      requireOwner(owner);
      this.captureOwners.push(owner);
      let released = false;
      return {
        supported:true,acquired:true,reason:null,
        get released() { return released; },
        release:async () => {
          if (released) return;
          released = true;
          coordination.releasedCaptureLeases += 1;
        },
      };
    },
    async runUploader(owner,work) {
      requireOwner(owner);
      if (typeof work !== 'function') throw new TypeError('uploader work is invalid');
      this.uploaderOwners.push(owner);
      return {supported:true,value:await work()};
    },
    async runDestructiveLectureAction(owner,lectureId,work,{hasActiveSession} = {}) {
      requireOwner(owner); requireUuid(lectureId,'lectureId');
      if (typeof work !== 'function') throw new TypeError('destructive work is invalid');
      this.destructiveCalls.push({owner,lectureId});
      if (hasActiveSession?.()) return {supported:true,executed:false,reason:'capture-active'};
      return {supported:true,executed:true,value:await work()};
    },
  };
  const document = {
    hidden:false,getElementById:element,querySelector:element,createElement,
    addEventListener(name,callback) {
      const callbacks = documentListeners.get(name) || [];
      callbacks.push(callback); documentListeners.set(name,callbacks);
    },
  };
  const context = vm.createContext({
    Blob, Headers, URL:TestURL, URLSearchParams, AbortController, console, crypto:cryptoImplementation,
    document,
    window:{addEventListener(){}}, performance:{now:() => 0},
    location,history:{replaceState(...args){historyCalls.push(args);}},
    navigator:{clipboard:{writeText:async value => { clipboardWrites.push(value); }}},
    localStorage:{
      getItem(key){ return key === 'yeobaek-server' ? storedServerValue : ''; },
      setItem(key,value){ storageWrites.push([key,String(value)]); if (key === 'yeobaek-server') storedServerValue = String(value); },
    },
    sessionStorage:tabStorage,
    TestAuthSessionStore:class extends TabAuthSessionStore {
      constructor() { super({getStorage:() => tabStorage}); }
    },
    TestRenderDriveStatus:renderDriveStatus,
    TestRenderMaintenanceStatus:renderMaintenanceStatus,
    TestRecordingReview:{readRecordingClip,RecordingClipPlayer,filterTranscript},
    TestLectureLibrary,
    TestManualNotes,
    TestLectureQuestions, TestStudyNotes,
    TestLocalAudioExport, TestRecordingFileSelection, TestEncodeWav:encodeWav,
    setTimeout:(callback,delay = 0) => { const value = ++id; timeouts.set(value,{callback,delay}); return value; },
    clearTimeout:value => timeouts.delete(value),
    setInterval:(callback,delay = 0) => { const value = ++id; intervals.set(value,{callback,delay}); return value; },clearInterval:value => intervals.delete(value),
    fetch:(url,options) => !translationApi && url.endsWith('/translation')
      ? Promise.resolve(response({configured:false,model:'solar-pro4',translation:null})) : fetch(url,options),
    TestCapture:class {
      constructor(callbacks) {
        mic = this; this.callbacks = callbacks; this.recording = false; this.paused = false;
      }
      async start() { this.recording = true; this.paused = false; }
      async pause() {
        if (!this.recording) return;
        this.recording = false; this.paused = true; this.pauseCalls = (this.pauseCalls || 0) + 1;
        if (this.pauseTail) this.callbacks.onChunk(this.pauseTail);
        if (this.pauseError) throw this.pauseError;
      }
      async resume() {
        if (!this.paused) return;
        this.resumeCalls = (this.resumeCalls || 0) + 1;
        if (this.resumeError) throw this.resumeError;
        this.paused = false; this.recording = true;
      }
      async resumeInput() {
        this.resumeInputCalls = (this.resumeInputCalls || 0) + 1;
        if (this.resumeInputError) throw this.resumeInputError;
        return this.inputStillUnavailable ? false : true;
      }
      async reconnect() {
        this.reconnectCalls = (this.reconnectCalls || 0) + 1;
        if (this.reconnectError) throw this.reconnectError;
        this.reconnectNeeded = false; this.paused = false; this.recording = true;
      }
      async stop() {
        if (!this.recording && !this.paused) return;
        this.recording = false; this.paused = false; this.stopCalls = (this.stopCalls || 0) + 1;
        if (this.tail) this.callbacks.onChunk(this.tail);
        if (this.stopError) throw this.stopError;
      }
    },
    TestDurableLiveQueue:class {
      constructor(options = {}) {
        if (options.readOnlyExisting) return {
          readExportSnapshot:owner=>lastLiveQueue ? lastLiveQueue.readExportSnapshot(owner)
            : Promise.reject(new Error('no existing synthetic database')),
          close:()=>{},
        };
        this.sessions = new Map(); this.chunks = new Map(); this.snapshots = new Map();lastLiveQueue=this;
      }
      async open() { return this; }
      async createSession(value) {
        requireUuid(value.id,'captureId'); requireOwner(value.owner);
        if (!['qwen','clova'].includes(value.asrProvider)) throw new TypeError('provider is invalid');
        const existing = this.sessions.get(value.id);
        if (existing) {
          if (existing.owner !== value.owner || existing.asrProvider !== value.asrProvider) throw new Error('session conflict');
          return {...existing};
        }
        const stored = {...value,state:'recording',lectureCreated:false,finalQueued:false,nextSequence:0,capturedSamples:0};
        this.sessions.set(value.id,stored); return {...stored};
      }
      async updateSession(owner,id,updates) {
        requireOwner(owner); requireUuid(id,'captureId');
        const stored = this.sessions.get(id); if (!stored || stored.owner !== owner) throw new Error('missing session');
        Object.assign(stored,updates); return {...stored};
      }
      async getSession(owner,id) {
        requireOwner(owner); requireUuid(id,'captureId');
        const stored = this.sessions.get(id);
        if (stored && stored.owner !== owner) throw new Error('session ownership mismatch');
        return stored ? {...stored} : null;
      }
      async setSessionUploadHeld(owner,id,held) {
        requireOwner(owner);requireUuid(id,'captureId');assert.equal(typeof held,'boolean');
        const stored=this.sessions.get(id);if(!stored || stored.owner!==owner)throw new Error('session ownership mismatch');
        if(held)stored.uploadHeld=true;else delete stored.uploadHeld;
        for(const chunk of this.chunks.values())if(chunk.owner===owner&&chunk.captureId===id){
          if(held)chunk.uploadHeld=true;else delete chunk.uploadHeld;
        }
        return {...stored};
      }
      async saveSnapshot(owner,captureId,value) {
        const session = await this.getSession(owner,captureId);
        if (!session) throw new Error('missing session');
        if (session.finalQueued || value.sequence < session.nextSequence) return {saved:false,reason:'settled'};
        assert.equal(value.sequence,session.nextSequence);
        assert.equal(value.startSamples + value.overlapSamples,session.capturedSamples);
        const previous = this.snapshots.get(captureId);
        const snapshot = {...value,owner,captureId,byteLength:value.blob.size,revision:(previous?.revision || 0) + 1};
        this.snapshots.set(captureId,snapshot);
        return {saved:true,snapshot:{...snapshot,blob:undefined}};
      }
      async getSnapshot(owner,captureId) {
        const session = await this.getSession(owner,captureId);
        return session && this.snapshots.has(captureId) ? {...this.snapshots.get(captureId)} : null;
      }
      async promoteSnapshot(owner,captureId,{id,expectedRevision,final}) {
        const snapshot = await this.getSnapshot(owner,captureId);
        assert.equal(snapshot?.revision,expectedRevision); assert.equal(final,true);
        return this.enqueueChunk(owner,captureId,{id,startSamples:snapshot.startSamples,
          durationSamples:snapshot.durationSamples,overlapSamples:snapshot.overlapSamples,blob:snapshot.blob,final:true});
      }
      async enqueueChunk(owner,captureId,value) {
        requireOwner(owner); requireUuid(captureId,'captureId'); requireUuid(value.id,'chunkId');
        const session = this.sessions.get(captureId); if (!session || session.owner !== owner) throw new Error('missing session');
        const previous = this.chunks.get(value.id);
        if (previous) {
          assert.equal(previous.owner,owner); assert.equal(previous.captureId,captureId);
          for (const key of ['startSamples','durationSamples','overlapSamples','final']) assert.equal(previous[key],value[key]);
          assert.deepEqual(new Uint8Array(await previous.blob.arrayBuffer()),new Uint8Array(await value.blob.arrayBuffer()));
          return {...previous};
        }
        if (session.finalQueued || session.state === 'completed') throw new Error('session already finalized');
        if (value.startSamples + value.overlapSamples !== session.capturedSamples) throw new Error('chunk timeline conflict');
        const stored = {...value,owner,captureId,lectureId:captureId,asrProvider:session.asrProvider,
          sessionCreatedAt:session.createdAt,sequence:session.nextSequence++,byteLength:value.blob.size,
          state:'queued',attempts:0,errorKind:'',downloadRequested:false,inflightAt:null,
          ...(session.uploadHeld===true?{uploadHeld:true}:{})};
        session.capturedSamples = value.startSamples + value.durationSamples;
        if (value.final) { session.finalQueued = true; session.state = 'stopped'; this.snapshots.delete(captureId); }
        this.chunks.set(value.id,stored); return {...stored};
      }
      async advanceSettledChunk(owner,captureId,value,evidence) {
        requireOwner(owner); requireUuid(captureId,'captureId'); requireUuid(value.id,'chunkId');
        assert.ok(evidence.serverConfirmed || evidence.downloadRequested);
        const session = this.sessions.get(captureId); assert.equal(session?.owner,owner);
        if (value.sequence < session.nextSequence) return {...session};
        assert.equal(value.sequence,session.nextSequence);
        assert.equal(value.startSamples + value.overlapSamples,session.capturedSamples);
        session.nextSequence += 1; session.capturedSamples = value.startSamples + value.durationSamples;
        if (value.final) { session.finalQueued = true; session.state = 'completed'; }
        return {...session};
      }
      async getChunk(owner,id) {
        requireOwner(owner); requireUuid(id,'chunkId');
        const item = this.chunks.get(id); return item?.owner === owner ? {...item} : null;
      }
      async ackChunk(owner,id,{serverConfirmed = false} = {}) {
        requireOwner(owner); requireUuid(id,'chunkId');
        const item = this.chunks.get(id); if (!item || item.owner !== owner) return null;
        if (item.asrProvider === 'clova' && item.state !== 'inflight' && !item.downloadRequested && !serverConfirmed) throw new Error('unsafe CLOVA ack');
        if (item.final && [...this.chunks.values()].filter(chunk => chunk.owner === owner && chunk.captureId === item.captureId).length !== 1) {
          throw new Error('final chunk is not last');
        }
        this.chunks.delete(id);
        if (item.final) this.sessions.delete(item.captureId);
        return {chunk:{...item}};
      }
      async markChunkInflight(owner,id) {
        requireOwner(owner); requireUuid(id,'chunkId');
        const item = this.chunks.get(id); if (!item || item.owner !== owner) throw new Error('missing chunk');
        if(this.sessions.get(item.captureId)?.uploadHeld)throw new Error('upload held');
        if (item.asrProvider !== 'clova' || item.state !== 'queued') throw new Error('invalid inflight transition');
        item.state = 'inflight'; item.inflightAt = new Date().toISOString();
        item.attempts += 1;
        this.markInflightCalls = (this.markInflightCalls || 0) + 1;
        return {...item};
      }
      async markChunkBlocked(owner,id,errorKind) {
        requireOwner(owner); requireUuid(id,'chunkId');
        const item = this.chunks.get(id); if (!item || item.owner !== owner) throw new Error('missing chunk');
        if (item.state !== 'inflight') item.attempts += 1;
        Object.assign(item,{state:'blocked',errorKind});
      }
      async markChunkQueued(owner,id) {
        requireOwner(owner); requireUuid(id,'chunkId');
        const item = this.chunks.get(id); if (!item || item.owner !== owner) throw new Error('missing chunk');
        if(this.sessions.get(item.captureId)?.uploadHeld)throw new Error('upload held');
        Object.assign(item,{state:'queued',errorKind:'',inflightAt:null});
      }
      async setDownloadRequested(owner,id,requested) {
        requireOwner(owner); requireUuid(id,'chunkId');
        const item = this.chunks.get(id); if (!item || item.owner !== owner) throw new Error('missing chunk');
        item.downloadRequested = requested;
      }
      async getStats(owner) { const items = [...this.chunks.values()].filter(item => item.owner === owner); return {count:items.length,bytes:items.reduce((sum,item) => sum + item.byteLength,0),queued:items.filter(item => item.state === 'queued').length,inflight:items.filter(item => item.state === 'inflight').length,blocked:items.filter(item => item.state === 'blocked').length}; }
      async recoverOwner() { return {sessions:[],chunks:[],inflightChunks:[],stats:{count:0,bytes:0,queued:0,inflight:0,blocked:0}}; }
      async readExportSnapshot(owner) {
        return {owner,sessions:[...this.sessions.values()].filter(row=>row.owner===owner).map(row=>({...row})),
          chunks:[...this.chunks.values()].filter(row=>row.owner===owner).map(row=>({...row})),
          snapshots:[...this.snapshots.values()].filter(row=>row.owner===owner).map(row=>({...row}))};
      }
      async hasWorkForOtherOwner(owner) {
        requireOwner(owner);
        return [...this.chunks.values()].some(item => item.owner !== owner)
          || [...this.sessions.values()].some(item => item.owner !== owner && item.state !== 'completed');
      }
      async hasPendingChunks(owner,captureId) {
        requireOwner(owner); requireUuid(captureId,'captureId');
        return [...this.chunks.values()].some(item => item.owner === owner && item.captureId === captureId)
          || this.snapshots.get(captureId)?.owner === owner;
      }
      async deleteSession(owner,id) {
        requireOwner(owner); requireUuid(id,'captureId');
        const session = this.sessions.get(id);
        if (!session) return {deletedChunks:0,deletedBytes:0};
        if (session.owner !== owner) throw new Error('session ownership mismatch');
        let deletedChunks = 0, deletedBytes = 0;
        for (const [chunkId,item] of this.chunks) {
          if (item.owner === owner && item.captureId === id) {
            deletedChunks += 1; deletedBytes += item.byteLength; this.chunks.delete(chunkId);
          }
        }
        this.sessions.delete(id); this.snapshots.delete(id); return {deletedChunks,deletedBytes};
      }
    },
    TestLiveCoordination:coordination,
    TestFileUploader:FileUploader,
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run("apiUrl='https://classroom.example'; verifiedApiUrl=apiUrl; verifiedApiExpiresAt=0; connectionState='connected'; token='old-token'; user='user-alpha'; setConnectionState('connected');");
  const runTimeout = async delay => {
    const entry = [...timeouts].find(([, timer]) => timer.delay === delay);
    assert.ok(entry, `missing ${delay} ms timer`);
    timeouts.delete(entry[0]); entry[1].callback(); await tick(); await tick();
  };
  const runInterval = async delay => {
    const entry = [...intervals].find(([, timer]) => timer.delay === delay);
    assert.ok(entry, `missing ${delay} ms interval`);
    entry[1].callback(); await tick(); await tick();
  };
  const dispatchDocument = async (name,event) => {
    for (const callback of documentListeners.get(name) || []) callback(event);
    await tick(); await tick();
  };
  const created = tag => createdElements.get(tag)?.at(-1);
  const createdAll = tag => createdElements.get(tag) || [];
  const objectUrlBlob = value => objectUrls.get(value);
  const seedDurableFailedFinal = async ({lectureId,error = '마지막 조각 실패'} = {}) => {
    const chunkId = webcrypto.randomUUID();
    context.TestSeed = {lectureId,chunkId,audio:chunk(0,0.05,0,true),error};
    try {
      await run(`(async () => {
        const fixture=TestSeed, lecture=current?.id === fixture.lectureId
          ? current : lectures.find(item => item.id === fixture.lectureId);
        const session={id:fixture.lectureId,owner:user,lecture,buffered:[],title:lecture?.title || '수업',
          language:lecture?.language || 'ko',source:'microphone',asrProvider:'qwen',cancelled:true,
          creating:null,assignmentTimer:null,assignmentAttempt:0,persistChain:Promise.resolve(),
          storeReady:Promise.resolve(),captureLease:null,discardAudio:false,createdAt:Date.now(),
          nextRuntimeSequence:1,durable:true,recovered:false};
        liveSessions.set(session.id,session); captureSession=session;
        await openLiveQueue();
        await liveQueue.createSession({id:session.id,owner:session.owner,title:session.title,
          language:session.language,source:session.source,asrProvider:session.asrProvider,createdAt:session.createdAt});
        await liveQueue.updateSession(session.owner,session.id,{lectureCreated:true});
        const stored=await liveQueue.enqueueChunk(session.owner,session.id,{id:fixture.chunkId,
          startSamples:0,durationSamples:800,overlapSamples:0,final:true,blob:fixture.audio.blob});
        await liveQueue.markChunkBlocked(session.owner,fixture.chunkId,'upload_error');
        await liveQueue.setDownloadRequested(session.owner,fixture.chunkId,true);
        pending=[{...fixture.audio,id:fixture.chunkId,captureId:session.id,lectureId:session.id,
          owner:session.owner,asrProvider:session.asrProvider,sessionCreatedAt:stored.sessionCreatedAt,
          sequence:stored.sequence,lectureReady:true,durable:true,byteLength:stored.byteLength,
          persistPromise:Promise.resolve(),downloadRequested:true,blocked:true,inflight:false}];
        sendError=fixture.error; updateControls();
      })()`);
    } finally {
      delete context.TestSeed;
    }
    return chunkId;
  };
  return {run,element,created,createdAll,objectUrlBlob,intervals,timeouts,runTimeout,runInterval,dispatchDocument,document,location,historyCalls,clipboardWrites,storageWrites,storedServer:() => storedServerValue,microphone:() => mic,coordination,seedDurableFailedFinal};
}

function reviewApp(fetch) {
  return setup((url,options) => url.endsWith('/summary') ? response({configured:false,summary:null}) : fetch(url,options));
}
function openReviewFixture(app) {
  app.run(`current={id:'review-lesson',title:'Synthetic review',language:'ko',created_at:'2026-01-01T00:00:00Z',
    recording_available:true,recording_finalized:true,recording_seconds:10,
    segments:[{id:'s1',start:0,end:5,text:'첫 문장 Alpha'},{id:'s2',start:5,end:10,text:'둘째 문장 Beta'}]}; renderCurrent();`);
}
test('library metadata stays editable while history filters only by date across classifications',async () => {
  const posted = [];
  const app = reviewApp((url,options) => {
    if (url.endsWith('/metadata')) {
      if (options.method === 'PATCH') { posted.push(JSON.parse(options.body)); return response({lecture_id:'review-lesson',display_title:'정리한 수업',course:'통계',semester:'2026-1',revision:1}); }
      return response({lecture_id:'review-lesson',display_title:'Synthetic review',course:'',semester:'',revision:0});
    }
    return response({});
  });
  openReviewFixture(app); app.run(`lectures=[current,
    {id:'other-course',title:'다른 과목',course:'물리',semester:'2025-2',created_at:current.created_at},
    {id:'another-day',title:'다른 날짜',created_at:'2026-01-02T00:00:00Z'}]; renderHistory();`);
  await app.element('metadata-open').onclick();
  app.element('metadata-title').value='정리한 수업'; app.element('metadata-course').value='통계'; app.element('metadata-semester').value='2026-1';
  await app.element('metadata-form').onsubmit({preventDefault(){}});
  assert.equal(posted[0].revision,0); assert.equal(app.run('current.title'),'Synthetic review');
  assert.equal(app.run('current.display_title'),'정리한 수업');
  assert.match(app.run("exportText(current,'markdown')"),/정리한 수업/);
  assert.equal(app.run('current.course'),'통계'); assert.equal(app.run('current.semester'),'2026-1');
  assert.deepEqual(posted[0],{revision:0,display_title:'정리한 수업',course:'통계',semester:'2026-1'});
  assert.equal(app.element('lecture-count').textContent,3);
  app.element('lecture-date').value=app.run('dateKey(current.created_at)'); app.element('lecture-date').onchange();
  assert.equal(app.element('lecture-count').textContent,'2/3');
  const visible=app.element('lecture-list').children.flatMap(group=>group.children[1].children);
  assert.deepEqual(visible.map(button=>button.children[0].textContent),['정리한 수업','다른 과목']);
  assert.ok(app.element('course-options').children.some(option=>option.value==='통계'));
  assert.ok(app.element('semester-options').children.some(option=>option.value==='2026-1'));
});
test('late metadata and global search results are discarded after an account change',async () => {
  for (const action of ['metadata','search']) {
    const gate = deferred(); const app = reviewApp(() => gate.promise); openReviewFixture(app);
    const task = action === 'metadata' ? app.element('metadata-open').onclick()
      : (app.element('library-search-open').onclick(),app.run('searchLibrary()'));
    app.run("user='user-beta'; token='new-token'; resetLibraryWorkspace();");
    gate.resolve(response(action === 'metadata'
      ? {lecture_id:'review-lesson',display_title:'PRIVATE',course:'PRIVATE',semester:'',revision:1}
      : {items:[],offset:0,limit:20,has_more:false}));
    await task;
    assert.equal(app.element('metadata-title').value,'');
    assert.equal(app.element('library-search-results').children.length,0);
    assert.equal(app.run('metadataRevision'),null);
    assert.equal(app.element('course-options').children.length,0);
    assert.equal(app.element('semester-options').children.length,0);
  }
});
test('global search and its paged retries never send hidden course semester or date filters',async () => {
  for (const source of ['all','raw','corrected']) {
    const requests=[];
    const app=reviewApp((url,options)=>{
      requests.push(new URL(url));
      return response({items:[],offset:Number(new URL(url).searchParams.get('offset')),limit:20,has_more:false});
    });
    openReviewFixture(app);
    app.run("current.course='PRIVATE COURSE'; current.semester='PRIVATE SEMESTER'; lectures=[current]; lectureDateFilter=dateKey(current.created_at); renderHistory();");
    app.element('library-search-open').onclick();
    assert.equal(app.element('library-search-filter').textContent,'내 모든 수업 · 날짜 제한 없이 검색');
    app.element('library-query').value='  50%_ 조건  '; app.element('library-source').value=source;
    await app.run('searchLibrary()');
    // A cached/legacy object cannot silently add removed filter keys to a retry.
    app.run("Object.assign(librarySearchQuery,{course:'HIDDEN',semester:'HIDDEN',date:'2026-01-01'});");
    app.element('library-query').value='화면에서 바뀐 검색어'; app.element('library-source').value='title';
    await app.run('searchLibrary(20,true)');
    assert.equal(requests.length,2);
    for (const [index,url] of requests.entries()) {
      assert.deepEqual([...url.searchParams.keys()].sort(),['limit','offset','q','source']);
      assert.equal(url.searchParams.get('q'),'50%_ 조건'); assert.equal(url.searchParams.get('source'),source);
      assert.equal(url.searchParams.get('offset'),String(index*20));
      assert.equal(url.searchParams.get('limit'),'20');
    }
  }
});
test('library search uses safe text nodes and does not stop an ongoing capture',async () => {
  const id = '11111111-1111-4111-8111-111111111111'; let request;
  const app = reviewApp((url,options) => {
    request = {url,options}; return response({items:[{lecture_id:id,display_title:'<img src=x>',snippet:'<script>private</script>',created_at:'2026-01-01T00:00:00Z',source:'raw',segment_id:'s1',start:0,end:1}],offset:0,limit:20,has_more:false});
  });
  openReviewFixture(app);
  app.run("capture={capturedSeconds:12}; captureSession={id:'live-lesson',lecture:{id:'live-lesson'}}; recording=true;");
  app.element('library-search-open').onclick(); app.element('library-query').value='50%_'; await app.run('searchLibrary()');
  assert.equal(new URL(request.url).searchParams.get('q'),'50%_');
  assert.equal(app.run('recording'),true);
  const row = app.element('library-search-results').children[0];
  assert.equal(row.children[0].textContent,'<img src=x>'); assert.equal(row.children[2].textContent,'<script>private</script>');
});
test('unclassified global search results retain the exact raw timestamp and account guard when opening',async () => {
  const id='11111111-1111-4111-8111-111111111111';
  const note={id,title:'결과 수업',created_at:'2026-01-02T00:00:00Z',recording_finalized:true,
    recording_available:true,recording_seconds:10,segments:[{id:'result-segment',start:5.125,end:8,text:'원문 결과'}]};
  const requests=[];
  const app=reviewApp((url)=>{
    requests.push(url);
    return response(url.endsWith(`/lectures/${id}`) ? note : {configured:false,correction:null});
  });
  openReviewFixture(app);
  app.run("globalThis.libraryClipStart=null; playRecordingClip=async start=>{globalThis.libraryClipStart=start;};");
  const item={lecture_id:id,segment_id:'result-segment',source:'raw',start:5.125};
  const scope=app.run('libraryAuthScope()');
  await app.run(`openLibraryResult(${JSON.stringify(item)},${JSON.stringify(scope)})`);
  assert.equal(app.run('current.id'),id); assert.equal(app.run('libraryClipStart'),5.125);
  assert.equal(app.run('correctionView'),'raw');
  const count=requests.length;
  app.run("user='user-beta'; token='new-token'; resetLibraryWorkspace(); globalThis.libraryClipStart=null;");
  await app.run(`openLibraryResult(${JSON.stringify(item)},${JSON.stringify(scope)})`);
  assert.equal(requests.length,count); assert.equal(app.run('libraryClipStart'),null);
});
test('review search filters the display only and bookmark loading is explicit',async () => {
  const requests=[];
  const app=reviewApp((url,options)=>{ requests.push([url,options]); return response({bookmarks:[]}); });
  openReviewFixture(app);
  assert.equal(requests.length,0);
  app.element('transcript-search').value='beta'; app.element('transcript-search').oninput();
  assert.equal(app.element('transcript').children.length,1);
  assert.equal(app.element('segment-count').textContent,'1 / 2');
  assert.ok(app.run("exportText(current,'text')").includes('Alpha'));
  await app.run('loadBookmarks()');
  assert.equal(requests.length,1); assert.match(requests[0][0],/\/bookmarks$/);
});
test('trash can restore a class without changing the current recording or its original metadata',async () => {
  const id = '11111111-1111-4111-8111-111111111111'; const requests = [];
  const item = {lecture_id:id,display_title:'복원 수업',course:'통계',semester:'2026-1',created_at:'2026-01-01T00:00:00Z',trashed_at:'2026-01-02T00:00:00Z',deleting:false};
  const app = reviewApp((url,options) => {
    requests.push({url,method:options.method});
    if (url.endsWith('/library/trash')) return response([item]);
    if (url.endsWith('/restore')) return response({status:'restored',lecture_id:id});
    if (url.endsWith('/lectures')) return response([{...item,id,title:'생성 이름'}]);
    return response({});
  });
  openReviewFixture(app); app.run("capture={capturedSeconds:12}; captureSession={id:'live-lesson',lecture:{id:'live-lesson'}}; recording=true;");
  app.element('trash-open').onclick(); await until(()=>app.run('trashRows.length') === 1);
  await app.run("changeTrash(trashRows[0],'restore')");
  assert.equal(app.run('recording'),true); assert.equal(app.run('captureSession.id'),'live-lesson');
  assert.equal(app.run('lectures[0].course'),'통계'); assert.equal(app.run('trashRows.length'),0);
  assert.ok(requests.some(item=>item.method === 'POST' && item.url.endsWith('/restore')));
});
test('permanent deletion needs a second confirmation and deleting rows cannot restore',async () => {
  const id = '11111111-1111-4111-8111-111111111111'; let deleted=0;
  const app = reviewApp((url,options) => {
    if (url.endsWith('/permanent')) { deleted++; return response({status:'deleted'}); }
    if (url.endsWith('/lectures')) return response([]);
    return response([{lecture_id:id,display_title:'영구 삭제 대기',created_at:'2026-01-01T00:00:00Z',trashed_at:'2026-01-02T00:00:00Z',deleting:true}]);
  });
  app.element('trash-open').onclick(); await until(()=>app.run('trashRows.length') === 1);
  const actions = app.element('trash-list').children[0].children[2];
  assert.equal(actions.children[0].disabled,true);
  actions.children[1].onclick(); assert.equal(deleted,0); assert.equal(app.element('purge-dialog').open,true);
  app.element('purge-cancel').onclick(); assert.equal(deleted,0);
  actions.children[1].onclick(); app.element('purge-confirm').onclick(); await until(()=>!app.run('trashBusy'));
  assert.equal(deleted,1); assert.equal(app.run('trashRows.length'),0);
});
test('old trash responses cannot restore personal titles into a new account',async () => {
  const gate=deferred(); const app=reviewApp(()=>gate.promise);
  app.element('trash-open').onclick();
  app.run("showLogin(); user='user-beta'; token='beta-token';");
  gate.resolve(response([{lecture_id:'11111111-1111-4111-8111-111111111111',display_title:'PRIVATE',created_at:'2026-01-01T00:00:00Z',deleting:false}]));
  await tick(); await tick();
  assert.equal(app.run('trashRows.length'),0); assert.equal(app.element('trash-list').children.length,0);
  assert.equal(app.element('purge-title').textContent,'');
});
function manualFixture({loseFirst = false} = {}) {
  const state={lecture_id:'review-lesson',raw_revision:'a'.repeat(64),revision:0,notes:[],edits:[]};
  const history=[],requests=[],acks=new Map(); let lost=false,app;
  const copy=value=>JSON.parse(JSON.stringify(value));
  app=reviewApp((url,options)=>{
    if (url.includes('/manual/history')) return response({lecture_id:state.lecture_id,raw_revision:state.raw_revision,revision:state.revision,at_revision:state.revision,items:copy(history).reverse(),offset:0,limit:20,has_more:false});
    if (url.endsWith('/manual') && options.method === 'POST') {
      const body=JSON.parse(options.body); requests.push(body);
      if (!acks.has(body.id)) {
        if (body.revision !== state.revision) return response({detail:'revision conflict'},409);
        state.revision++;
        if (body.action === 'segment_edit') { state.edits=state.edits.filter(item=>item.segment_id !== body.segment_id); if (body.text !== null) state.edits.push({segment_id:body.segment_id,text:body.text}); }
        else { state.notes=state.notes.filter(item=>item.id !== body.note_id); if (body.action === 'note_upsert') state.notes.push({id:body.note_id,segment_id:body.segment_id || null,start_seconds:body.start_seconds,text:body.text}); }
        history.push({...body,note_id:body.note_id || null,segment_id:body.segment_id || null,text:body.text ?? null,
          start_seconds:body.start_seconds ?? (body.segment_id === 's2' || body.action === 'note_delete' ? 5 : 0),
          revision:state.revision,created_at:'2026-01-01T00:00:00Z'});
        acks.set(body.id,{id:body.id,revision:state.revision,action:body.action});
      }
      if (loseFirst && !lost) { lost=true; throw app.run("new TypeError('response lost')"); }
      return response(copy(acks.get(body.id)));
    }
    if (url.endsWith('/manual')) return response(copy(state));
    return response({});
  });
  openReviewFixture(app); return {app,state,history,requests};
}
test('manual editing, note revisions and original restoration preserve the raw transcript and timestamps',async()=>{
  const {app,state,history}=manualFixture(); const raw=app.run('JSON.stringify(current.segments)');
  await app.run("openManualEditor('s1')");
  assert.equal(app.element('manual-edit-raw').textContent,'첫 문장 Alpha');
  await app.run("writeManual({action:'segment_edit',segment_id:'s1',text:'직접 바로잡은 내용'})");
  assert.equal(app.run('JSON.stringify(current.segments)'),raw);
  assert.equal(app.element('transcript-title').textContent,'직접 수정본');
  assert.equal(app.run('displayedTranscriptSegments()[0].start'),0);
  assert.match(app.run("exportText(selectedTranscriptLecture(),'markdown')"),/직접 수정본/);
  await app.run("writeManual({action:'segment_edit',segment_id:'s1',text:null})");
  assert.equal(app.run('displayedTranscriptSegments()[0].text'),'첫 문장 Alpha');
  const noteId='11111111-1111-4111-8111-111111111111';
  await app.run(`writeManual({action:'note_upsert',note_id:'${noteId}',start_seconds:5,segment_id:'s2',text:${JSON.stringify('필기\n두 번째 줄')}})`);
  assert.equal(state.notes.length,1); assert.equal(app.element('manual-note-list').children.length,1);
  await app.run(`writeManual({action:'note_delete',note_id:'${noteId}'})`);
  assert.equal(state.notes.length,0); assert.equal(history.length,4);
  await app.run('openManualHistory()'); assert.equal(app.element('manual-history-list').children.length,4);
  assert.equal(app.run('JSON.stringify(current.segments)'),raw);
});
test('an ambiguous manual save retries the same UUID, while original/other-version restoration cannot replay it',async()=>{
  const {app,state,history,requests}=manualFixture({loseFirst:true});
  await app.run("openManualEditor('s1')");
  await app.run("writeManual({action:'segment_edit',segment_id:'s1',text:'정정한 표현'})");
  assert.equal(history.length,1); assert.equal(app.run('manualView.pending !== null'),true);
  assert.equal(app.element('manual-edit-original').disabled,true);
  await app.run("writeManual({action:'segment_edit',segment_id:'s1',text:null})");
  assert.equal(requests.length,1,'a different intent must not silently resend the earlier save');
  app.element('manual-edit-form').onsubmit({preventDefault(){}}); await until(()=>!app.run('manualView.busy'));
  assert.equal(requests.length,2); assert.deepEqual(requests[0],requests[1]);
  assert.equal(history.length,1); assert.equal(state.revision,1); assert.equal(app.run('manualView.pending'),null);
});
test('manual reads are scrubbed on owner change and editing another class does not stop capture',async()=>{
  const {app}=manualFixture();
  app.run("capture={capturedSeconds:12}; captureSession={id:'live-lesson',lecture:{id:'live-lesson'}}; recording=true;");
  await app.run('loadManual()'); await app.run("writeManual({action:'segment_edit',segment_id:'s2',text:'복습하며 수정'})");
  assert.equal(app.run('recording'),true); assert.equal(app.run('captureSession.id'),'live-lesson');
  const gate=deferred(); const late=reviewApp(()=>gate.promise); openReviewFixture(late);
  const task=late.run('loadManual()'); late.run("showLogin(); user='user-beta'; token='beta-token';");
  gate.resolve(response({lecture_id:'review-lesson',raw_revision:'a'.repeat(64),revision:1,notes:[],edits:[{segment_id:'s1',text:'PRIVATE'}]}));
  await task; assert.equal(late.run('manualView.row'),null); assert.equal(late.element('manual-edit-raw').textContent,'');
});
function questionFixture({loseFirst=false,notStored=false,invalid=false}={}) {
  const jobs=[],requests=[];
  const app=reviewApp((url,options)=>{
    if (!url.includes('/questions')) return response({});
    if (options.method === 'POST') {
      const body=JSON.parse(options.body); requests.push(body);
      const job={...body,lecture_id:'review-lesson',status:'completed',created_at:'2026-01-01T00:00:00Z',scope:'full',total_segments:2,selected_count:2,
        document:{answerability:'answered',paragraphs:[{text:'수업에서 설명한 합성 답변',source_ids:[invalid ? 'foreign-source' : 's1']}]}};
      if (!(notStored && requests.length===1)) jobs.unshift(job);
      if (loseFirst && requests.length===1) throw new TypeError('lost acknowledgment');
      return response({question:job});
    }
    // A real JSON response is a snapshot, not an alias of mutable server rows.
    if (url.includes('/questions?')) return response({configured:true,model:'fixture',offset:0,limit:20,total:jobs.length,has_more:false,questions:JSON.parse(JSON.stringify(jobs))});
    const job=jobs.find(value=>url.endsWith(value.id));
    if (!job) return response({detail:'missing'},404);
    return response({question:job});
  });
  openReviewFixture(app); return {app,jobs,requests};
}
test('lecture questions use explicit POST and render validated raw citations without stopping another capture',async()=>{
  const {app,requests}=questionFixture();
  app.run("capture={capturedSeconds:12}; captureSession={id:'live-lesson',lecture:{id:'live-lesson'}}; recording=true;");
  await app.run('loadQuestions()'); assert.equal(requests.length,0);
  app.element('question-text').value='첫 문장의 뜻은?'; await app.run('submitQuestion()');
  assert.equal(requests.length,1); assert.equal(app.element('question-list').children.length,1);
  assert.equal(app.run('recording'),true); assert.equal(app.run('captureSession.id'),'live-lesson');
  assert.equal(app.run('questionView.pending'),null);
});
test('lost question acknowledgment is recovered by GET without another POST',async()=>{
  const {app,requests}=questionFixture({loseFirst:true}); await app.run('loadQuestions()');
  app.element('question-text').value='첫 문장의 뜻은?'; await app.run('submitQuestion()');
  assert.equal(app.element('question-submit').textContent,'요청 상태 확인');
  await app.run('submitQuestion()');
  assert.equal(requests.length,1); assert.equal(app.run('questionView.pending'),null);
  assert.equal(app.element('question-list').children.length,1);
});
test('a question not found after an ambiguous POST needs an explicit identical UUID retry',async()=>{
  const {app,requests}=questionFixture({loseFirst:true,notStored:true}); await app.run('loadQuestions()');
  app.element('question-text').value='첫 문장의 뜻은?'; await app.run('submitQuestion()');
  await app.run('submitQuestion()'); assert.equal(requests.length,1);
  assert.equal(app.element('question-submit').textContent,'같은 질문 요청 다시 전송');
  app.element('question-text').value='몰래 바뀐 질문'; await app.run('submitQuestion()');
  assert.equal(requests.length,2); assert.deepEqual(requests[0],requests[1]);
  assert.equal(app.run('questionView.pending'),null);
});
test('questions reject foreign citations and scrub a delayed history after logout',async()=>{
  const {app}=questionFixture({invalid:true}); await app.run('loadQuestions()');
  app.element('question-text').value='질문'; await app.run('submitQuestion()');
  assert.equal(app.element('question-list').children.length,0); assert.notEqual(app.run('questionView.pending'),null);
  const gate=deferred(), late=reviewApp(()=>gate.promise); openReviewFixture(late);
  const task=late.run('loadQuestions()'); late.run("showLogin(); user='user-beta'; token='new-token';");
  gate.resolve(response({configured:true,model:'fixture',offset:0,total:0,has_more:false,questions:[]})); await task;
  assert.equal(late.run('questionView.page'),null); assert.equal(late.element('question-text').value,'');
});
function questionTestJob(index=1, updates={}) {
  return {id:`00000000-0000-4000-8000-${String(index).padStart(12,'0')}`,lecture_id:'review-lesson',
    question:`합성 질문 ${index}`,status:'completed',created_at:'2026-01-01T00:00:00Z',cancel_requested:false,
    scope:'full',total_segments:2,selected_count:2,
    document:{answerability:'answered',paragraphs:[{text:'원문에 근거한 합성 답변',source_ids:['s1']}]},...updates};
}
function questionWorkflowFixture({initial=[],onPost,onDelete,onGet,onList}={}) {
  const state={jobs:JSON.parse(JSON.stringify(initial)),calls:[]};
  const snapshot=value=>response(JSON.parse(JSON.stringify(value)));
  const app=reviewApp(async(url,options)=>{
    if (!url.includes('/questions')) return response({});
    const method=options.method || 'GET', body=options.body ? JSON.parse(options.body) : null;
    state.calls.push({url,method,body,signal:options.signal});
    if (method==='POST') {
      if(onPost) return onPost(body,state,snapshot);
      const job=questionTestJob(state.jobs.length+1,{...body,status:'queued',document:null});
      state.jobs.unshift(job); return snapshot({question:job});
    }
    if (method==='DELETE') {
      const job=state.jobs.find(item=>url.endsWith(item.id));
      if(onDelete) return onDelete(job,state,snapshot);
      job.cancel_requested=true;
      if(job.status==='queued') job.status='cancelled';
      return snapshot({question:job});
    }
    if (url.includes('/questions?')) {
      const offset=Number(new URL(url).searchParams.get('offset'));
      if(onList) return onList(offset,state,snapshot);
      return snapshot({configured:true,model:'fixture',offset,limit:20,total:state.jobs.length,
        has_more:offset+20<state.jobs.length,questions:state.jobs.slice(offset,offset+20)});
    }
    const id=url.split('/').at(-1);
    if(onGet) return onGet(id,state,snapshot);
    const job=state.jobs.find(item=>item.id===id);
    return job ? snapshot({question:job}) : response({detail:'missing'},404);
  });
  openReviewFixture(app);
  return {app,state};
}
const questionTextTree=node=>[node.textContent || '',...(node.children || []).map(questionTextTree)].join('\n');
test('processing question cancellation remains pending until confirmed and never stops another microphone',async()=>{
  const {app,state}=questionWorkflowFixture({initial:[questionTestJob(1,{status:'processing',document:null})]});
  app.run("capture={capturedSeconds:12,stopCalls:0,pauseCalls:0,async stop(){this.stopCalls++},async pause(){this.pauseCalls++}}; captureSession={id:'live-lesson',lecture:{id:'live-lesson'}}; recording=true;");
  app.element('question-details').open=true;
  await app.run('loadQuestions()');
  assert.equal([...app.timeouts.values()].filter(timer=>timer.delay===3000).length,1);
  await app.run(`cancelQuestion('${state.jobs[0].id}')`);
  assert.equal(state.calls.filter(call=>call.method==='DELETE').length,1);
  assert.match(questionTextTree(app.element('question-list')),/취소 요청됨 · 처리 정리 중/);
  const cancel=app.element('question-list').querySelectorAll('button').find(button=>button.textContent==='요청 취소');
  assert.equal(cancel.disabled,true);
  state.jobs[0].status='cancelled';
  await app.runTimeout(3000); await until(()=>!app.run('questionView.busy'));
  assert.match(questionTextTree(app.element('question-list')),/취소됨/);
  assert.equal(app.element('question-list').querySelectorAll('button').length,0);
  assert.equal(state.calls.filter(call=>call.method==='POST').length,0);
  assert.equal(app.run('recording'),true); assert.equal(app.run('captureSession.id'),'live-lesson');
  assert.equal(app.run('capture.stopCalls + capture.pauseCalls'),0);
  assert.equal([...app.timeouts.values()].filter(timer=>timer.delay===3000).length,0);
});
test('failed and cancelled questions do not retry automatically but a new explicit question gets a new UUID',async()=>{
  const {app,state}=questionWorkflowFixture({initial:[questionTestJob(1,{status:'failed',document:null,error:'확인하지 못해 저장하지 않았습니다.'}),
    questionTestJob(2,{status:'cancelled',document:null})]});
  app.element('question-details').open=true;
  await app.run('loadQuestions()'); await app.run('loadQuestions()');
  assert.match(questionTextTree(app.element('question-list')),/답변 생성 실패 · 자동 재시도하지 않음/);
  assert.equal([...app.timeouts.values()].filter(timer=>timer.delay===3000).length,0);
  assert.equal(app.element('question-submit').disabled,false);
  app.element('question-text').value='새로운 합성 질문';
  await app.run('submitQuestion()');
  const posts=state.calls.filter(call=>call.method==='POST');
  assert.equal(posts.length,1); assert.ok(UUID.test(posts[0].body.id));
  assert.notEqual(posts[0].body.id,questionTestJob(1).id); assert.notEqual(posts[0].body.id,questionTestJob(2).id);
  assert.equal(posts[0].body.question,'새로운 합성 질문');
  assert.equal(app.run('questionView.pending'),null);
});
test('question history reaches entries after twenty and returns without a model POST',async()=>{
  const {app,state}=questionWorkflowFixture({initial:Array.from({length:45},(_,index)=>questionTestJob(index+1))});
  await app.run('loadQuestions()');
  assert.equal(app.element('question-list').children.length,20);
  assert.equal(app.element('question-prev').disabled,true); assert.equal(app.element('question-next').disabled,false);
  app.element('question-next').onclick(); await until(()=>!app.run('questionView.busy'));
  assert.equal(app.run('questionView.page.offset'),20);
  assert.equal(app.element('question-list').children[0].children[0].textContent,'합성 질문 21');
  app.element('question-next').onclick(); await until(()=>!app.run('questionView.busy'));
  assert.equal(app.run('questionView.page.offset'),40); assert.equal(app.element('question-list').children.length,5);
  assert.equal(app.element('question-next').disabled,true);
  app.element('question-prev').onclick(); await until(()=>!app.run('questionView.busy'));
  assert.equal(app.run('questionView.page.offset'),20); assert.equal(app.element('question-list').children.length,20);
  assert.equal(state.calls.filter(call=>call.method!=='GET').length,0);
  assert.deepEqual(state.calls.map(call=>new URL(call.url).searchParams.get('offset')),['0','20','40','20']);
});
test('an ambiguous question POST freezes paging and repeated GET 404 never sends a new request automatically',async()=>{
  const {app,state}=questionWorkflowFixture({initial:Array.from({length:25},(_,index)=>questionTestJob(index+1)),
    onPost(body,state,snapshot){
      if(state.calls.filter(call=>call.method==='POST').length===1) throw new TypeError('synthetic lost response');
      const job=questionTestJob(30,{...body,status:'queued',document:null}); state.jobs.unshift(job);
      return snapshot({question:job});
    }});
  await app.run('loadQuestions()'); app.element('question-details').open=true;
  app.element('question-text').value='확인이 필요한 합성 질문'; await app.run('submitQuestion()');
  assert.equal(app.element('question-text').disabled,true);
  assert.equal(app.element('question-next').disabled,true);
  assert.equal([...app.timeouts.values()].filter(timer=>timer.delay===3000).length,0);
  await app.run('loadQuestions()'); await app.run('loadQuestions()');
  assert.equal(state.calls.filter(call=>call.method==='POST').length,1);
  assert.equal(app.run('questionView.pending.notFound'),true);
  assert.equal(app.element('question-submit').textContent,'같은 질문 요청 다시 전송');
  app.element('question-text').value='입력을 몰래 바꿔도 사용하지 않음';
  await app.run('submitQuestion()');
  const posts=state.calls.filter(call=>call.method==='POST');
  assert.equal(posts.length,2); assert.deepEqual(posts[0].body,posts[1].body);
  assert.equal(app.run('questionView.pending'),null); assert.equal(app.element('question-text').value,'');
});
test('normal none and full insufficient answers show local evidence guidance without creating another question',async()=>{
  const insufficient={answerability:'insufficient_evidence',paragraphs:[]};
  const {app,state}=questionWorkflowFixture({initial:[questionTestJob(1,{scope:'none',selected_count:0,document:insufficient}),
    questionTestJob(2,{scope:'full',document:insufficient}),questionTestJob(3,{scope:'retrieved',selected_count:1,document:insufficient})]});
  await app.run('loadQuestions()');
  const rows=app.element('question-list').children;
  assert.equal(rows.length,3); assert.match(questionTextTree(rows[0]),/AI에 요청하지 않았어요/);
  assert.match(questionTextTree(rows[1]),/전체 원문 2개 구간 참고/);
  assert.match(questionTextTree(rows[2]),/일부 원문만 참고/);
  for(const row of rows) {
    assert.match(questionTextTree(row),/답할 근거가 부족해요/);
    assert.equal(row.querySelectorAll('button').length,0);
  }
  assert.equal(app.run('questionView.error'),'');
  assert.equal(state.calls.filter(call=>call.method==='POST').length,0);
});
test('late question pages cannot cross owner token origin or selected lecture boundaries',async()=>{
  for(const boundary of ["user='user-beta'", "token='new-token'", "apiUrl='https://different.example'", "current={...current,id:'different-lesson'}"]) {
    const gate=deferred();
    const {app,state}=questionWorkflowFixture({onList:()=>gate.promise});
    const pending=app.run('loadQuestions()'); await until(()=>state.calls.length===1);
    app.run(`${boundary}; renderQuestions();`);
    assert.equal(state.calls[0].signal.aborted,true);
    gate.resolve(response({configured:true,model:'fixture',offset:0,limit:20,total:1,has_more:false,questions:[questionTestJob()]}));
    await pending;
    assert.equal(app.run('questionView.page'),null);
    assert.equal(app.element('question-list').children.length,0);
    assert.equal(app.element('question-text').value,'');
  }
});
test('late question POST and cancellation ACK cannot restore old question data after a lecture switch',async()=>{
  for(const method of ['POST','DELETE']) {
    const gate=deferred();
    const {app,state}=questionWorkflowFixture({initial:[questionTestJob(1,{status:'processing',document:null})],
      onPost:()=>gate.promise,onDelete:()=>gate.promise});
    await app.run('loadQuestions()');
    app.element('question-text').value='이전 수업의 합성 질문';
    const pending=app.run(method==='POST' ? 'submitQuestion()' : `cancelQuestion('${state.jobs[0].id}')`);
    await until(()=>state.calls.some(call=>call.method===method));
    const call=state.calls.find(call=>call.method===method);
    app.run("current={...current,id:'different-lesson'}; renderQuestions();");
    assert.equal(call.signal.aborted,true);
    gate.resolve(response({question:questionTestJob(1,{...(call.body || {}),status:'queued',document:null})}));
    await pending;
    assert.equal(app.run('questionView.page'),null); assert.equal(app.run('questionView.pending'),null);
    assert.equal(app.element('question-list').children.length,0);
    assert.equal(state.calls.filter(item=>item.method===method).length,1);
  }
});
test('question citations return to immutable raw text and stale citation buttons cannot affect another lecture',async()=>{
  const {app,state}=questionWorkflowFixture({initial:[questionTestJob(1,{document:{answerability:'answered',paragraphs:[
    {text:'<img src=x onerror=alert(1)> 합성 답변',source_ids:['s2']}]}})]});
  app.run("current.recording_available=false; capture={capturedSeconds:12}; captureSession={id:'live-lesson',lecture:{id:'live-lesson'}}; recording=true;");
  await app.run('loadQuestions()');
  const link=app.element('question-list').querySelectorAll('button')[0];
  const row=app.run("transcriptRenderState.rows.get(JSON.stringify(['id:s2',0])).row");
  let scrolled=0; row.scrollIntoView=()=>scrolled++;
  app.run("correctionView='manual'; reviewView.query='hidden';");
  link.onclick();
  assert.equal(app.run('correctionView'),'raw'); assert.equal(app.run('reviewView.query'),''); assert.equal(scrolled,1);
  assert.equal(app.run('recording'),true); assert.equal(app.run('captureSession.id'),'live-lesson');
  assert.match(questionTextTree(app.element('question-list')),/<img src=x onerror=alert\(1\)>/);
  assert.equal(app.createdAll('img').length,0);
  app.run("current={...current,id:'different-lesson'}; renderQuestions(); correctionView='manual'; reviewView.query='keep';");
  link.onclick();
  assert.equal(app.run('correctionView'),'manual'); assert.equal(app.run('reviewView.query'),'keep'); assert.equal(scrolled,1);
  assert.equal(state.calls.filter(call=>call.method!=='GET').length,0);
});
test('lost cancellation acknowledgment requires a status read and never repeats a provider request',async()=>{
  const {app,state}=questionWorkflowFixture({initial:[questionTestJob(1,{status:'processing',document:null})],
    onDelete(job){job.cancel_requested=true;job.status='cancelled';throw new TypeError('synthetic lost cancellation response');}});
  app.element('question-details').open=true; await app.run('loadQuestions()');
  await app.run(`cancelQuestion('${state.jobs[0].id}')`);
  assert.match(app.element('question-state').textContent,/요청 상태 확인으로 취소 여부/);
  await app.run('loadQuestions()');
  assert.match(questionTextTree(app.element('question-list')),/취소됨/);
  assert.equal(app.run('questionView.error'),'');
  assert.equal(state.calls.filter(call=>call.method==='DELETE').length,1);
  assert.equal(state.calls.filter(call=>call.method==='POST').length,0);
  assert.equal([...app.timeouts.values()].filter(timer=>timer.delay===3000).length,0);
});
test('pending question lookup rejects a foreign lecture or different question without clearing its frozen UUID',async()=>{
  for(const change of [{lecture_id:'foreign-lesson'},{question:'다른 질문'}]) {
    const {app,state}=questionWorkflowFixture({
      onPost(){throw new TypeError('synthetic ambiguous POST');},
      onGet(id,state,snapshot){
        const submitted=state.calls.find(call=>call.method==='POST').body;
        return snapshot({question:questionTestJob(1,{...submitted,id,...change,status:'queued',document:null})});
      }});
    await app.run('loadQuestions()'); app.element('question-text').value='원래의 합성 질문';
    await app.run('submitQuestion()');
    const originalId=app.run('questionView.pending.id');
    await app.run('submitQuestion()');
    assert.equal(state.calls.filter(call=>call.method==='POST').length,1);
    assert.equal(app.run('questionView.pending.id'),originalId);
    assert.equal(app.run('questionView.pending.question'),'원래의 합성 질문');
    assert.equal(app.run('questionView.pending.notFound'),false);
    assert.equal(app.element('question-submit').textContent,'요청 상태 확인');
    assert.equal(app.element('question-list').children.length,0);
    assert.match(app.element('question-state').textContent,/같은 질문의 저장 상태/);
  }
});
test('an ambiguous bookmark POST retry keeps its UUID, label and original live audio position',async () => {
  const submitted=[];
  const app=reviewApp((_url,options)=>{
    const body=JSON.parse(options.body); submitted.push(body);
    if(submitted.length === 1) throw new TypeError('lost response');
    return response({bookmark:{...body,created_at:'2026-01-01T00:00:00Z'}});
  });
  openReviewFixture(app);
  app.run("capture={capturedSeconds:12}; captureSession={id:'review-lesson',lecture:current}; recording=true;");
  app.element('bookmark-label').value='original';
  await app.run('addBookmark()');
  app.run('capture.capturedSeconds=40'); app.element('bookmark-label').value='changed';
  await app.run('addBookmark()');
  assert.equal(submitted.length,2); assert.deepEqual(submitted[0],submitted[1]);
  assert.equal(submitted[0].start_seconds,12); assert.equal(submitted[0].label,'original');
  assert.equal(app.run('recording'),true); assert.equal(app.run('reviewView.bookmarks.length'),1);
});
test('late bookmark reads and writes never cross owner, token, origin or lecture selection',async () => {
  for(const change of ["user='other'","token='new'","setServer('https://other-tunnel.trycloudflare.com')","current={...current,id:'other-lesson'}"]){
    for(const action of ['loadBookmarks()','addBookmark()']){
      const gate=deferred();
      const app=reviewApp(()=>gate.promise); openReviewFixture(app);
      const task=app.run(action);
      const id=app.run('reviewView.pending?.id') || webcrypto.randomUUID();
      app.run(`${change}; renderRecordingReview();`);
      gate.resolve(response({bookmarks:[{id,start_seconds:0,label:'PRIVATE'}],bookmark:{id,start_seconds:0,label:'PRIVATE'}}));
      await task;
      assert.equal(app.run('reviewView.bookmarks.length'),0);
      assert.equal(app.element('bookmark-list').children.length,0);
    }
  }
});
test('clip playback uses authenticated bounded audio and clears the object URL on logout',async () => {
  let clipOptions;
  const wav=encodeWav(new Float32Array(16000));
  const app=reviewApp((_url,options)=>{
    clipOptions=options;
    return new Response(wav,{headers:{'Content-Type':'audio/wav','Content-Length':String(wav.size),
      'X-Clip-Start-Seconds':'5','X-Clip-Duration-Seconds':'1'}});
  });
  openReviewFixture(app);
  await app.run('playRecordingClip(5)');
  assert.equal(clipOptions.headers.get('Authorization'),'Bearer old-token');
  assert.equal(clipOptions.responseType,undefined);
  const url=app.element('recording-player').src; assert.ok(app.objectUrlBlob(url));
  assert.equal(app.element('recording-player').paused,false);
  app.run("token=''; showLogin(false)");
  assert.equal(app.element('recording-player').src,undefined); assert.equal(app.objectUrlBlob(url),undefined);
});
test('pending clip requests are aborted when switching lessons and cannot install late audio',async () => {
  const gate=deferred(); let options;
  const app=reviewApp((_url,request)=>{options=request; return gate.promise;}); openReviewFixture(app);
  const task=app.run('playRecordingClip(0)'); await tick();
  app.run("current={...current,id:'other'}; renderRecordingReview()");
  assert.equal(options.signal.aborted,true);
  const wav=encodeWav(new Float32Array(16000));
  gate.resolve(new Response(wav,{headers:{'Content-Type':'audio/wav','Content-Length':String(wav.size),
    'X-Clip-Start-Seconds':'0','X-Clip-Duration-Seconds':'1'}}));
  await task; assert.equal(app.element('recording-player').src,undefined);
});

test('lecture status refresh updates timestamp playback and discards audio if no longer available',async () => {
  let available=true;
  const wav=encodeWav(new Float32Array(16000));
  const app=reviewApp(url => url.endsWith('/lectures')
    ? response({lectures:[{id:'review-lesson',created_at:'2026-01-01T00:00:00Z',recording_available:available,recording_finalized:available}]})
    : new Response(wav,{headers:{'Content-Type':'audio/wav','Content-Length':String(wav.size),
      'X-Clip-Start-Seconds':'0','X-Clip-Duration-Seconds':'1'}}));
  openReviewFixture(app);
  app.run('current.recording_available=false; current.recording_finalized=false; renderCurrent()');
  assert.equal(app.element('playback-start').disabled,true);
  await app.run('refreshLectures()');
  assert.equal(app.element('playback-start').disabled,false);
  assert.equal(app.element('transcript').children[0].children[0].role,'button');
  await app.run('playRecordingClip(0)'); const url=app.element('recording-player').src;
  available=false; await app.run('refreshLectures()');
  assert.equal(app.element('playback-start').disabled,true);
  assert.equal(app.element('transcript').children[0].children[0].role,'text');
  assert.equal(app.element('recording-player').src,undefined);
  assert.equal(app.objectUrlBlob(url),undefined);
});

function translationFixture(status = 'completed') {
  return {configured:true,model:'solar-pro4',translation:{lecture_id:'english-lesson',status,segments:status === 'completed' ? [
    {id:'english-one',start:0,end:5,text:'강둑이 불안정해졌습니다.'},
    {id:'english-two',start:5,end:10,text:'강의 흐름이 토양을 침식합니다.'},
  ] : []}};
}
function translationApp(fetch) {
  return setup((url,options) => url.endsWith('/summary')
    ? response({configured:false,summary:null}) : fetch(url,options),{translationApi:true});
}
function openTranslationFixture(app, finalized = true) {
  app.run(`current={id:'english-lesson',title:'River lecture',language:'en',created_at:'2026-01-01T00:00:00Z',
    recording_finalized:${finalized},segments:[
      {id:'english-one',start:0,end:5,text:'The bank became unstable.'},
      {id:'english-two',start:5,end:10,text:'The river flow erodes the soil.'}]}; renderCurrent();`);
}
test('translation waits for final saving, is opt-in and keeps a different live capture open', async () => {
  const methods = []; let posted = false;
  const app = translationApp(async (_url,options) => {
    methods.push(options.method);
    if (options.method === 'POST') { posted = true; return response(translationFixture('queued'),202); }
    return response(posted ? translationFixture() : {configured:true,translation:null});
  });
  openTranslationFixture(app,false);
  app.element('translate-lecture').onclick(); await tick(); assert.deepEqual(methods,[]);
  app.run(`recording=true; capture={recording:true,paused:false,stopCalls:0,stop(){this.stopCalls++;}};
    captureSession={id:'other-live',lecture:{id:'other-live'},source:'microphone',asrProvider:'qwen'};
    current.recording_finalized=true; renderCurrent();`);
  await until(() => app.run('translationView.loaded'));
  assert.deepEqual(methods,['GET']);
  app.element('translate-lecture').onclick(); app.element('translate-lecture').onclick();
  await until(() => app.run('translationView.row?.status') === 'queued');
  await app.runTimeout(3000);
  assert.deepEqual(methods,['GET','POST','GET']);
  assert.equal(app.run('recording'),true); assert.equal(app.run('capture.stopCalls'),0);
  assert.equal(app.run('current.segments[0].text'),'The bank became unstable.');
  assert.equal(app.element('translation-content').children.length,2);
  assert.equal(app.element('translation-content').children[0].children[1].textContent,'The bank became unstable.');
});
test('paired and full-context translation views use the same complete result without another model request', async () => {
  let calls = 0;
  const app = translationApp(async () => { calls++; return response(translationFixture()); });
  openTranslationFixture(app); await until(() => app.run('translationView.row?.status') === 'completed');
  app.element('translation-full').onclick();
  assert.equal(app.element('translation-full')['aria-pressed'],'true');
  assert.equal(app.element('translation-content').children.length,2);
  assert.equal(app.element('translation-content').children[0].children.length,2);
  assert.equal(app.element('translation-content').children[0].children[1].textContent,'강둑이 불안정해졌습니다.');
  app.element('translation-paired').onclick();
  assert.equal(app.element('translation-content').children[0].children.length,3);
  assert.equal(calls,1);
});
test('translation rejects missing, reordered, foreign and retimed sentence mappings', async () => {
  for (const mutate of [r => r.translation.segments.pop(), r => r.translation.segments.reverse(),
    r => {r.translation.segments[0].id='foreign';}, r => {r.translation.segments[0].start=.2;},
    r => {r.translation.lecture_id='foreign';}]) {
    const result = translationFixture(); mutate(result);
    const app = translationApp(async () => response(result));
    openTranslationFixture(app); await until(() => !!app.run('translationView.error'));
    assert.equal(app.element('translation-content').children.length,0);
    assert.equal(app.element('translation-download').disabled,true);
  }
});
test('late translation responses cannot cross account, token, origin, or selected lecture', async () => {
  for (const change of ["user='user-beta'","token='next-token'","apiUrl='https://other.example'","current={id:'another',segments:[]}"]) {
    const pendingTranslation = deferred();
    const app = translationApp(async () => pendingTranslation.promise);
    openTranslationFixture(app); await tick();
    app.run(change);
    pendingTranslation.resolve(response(translationFixture())); await tick(); await tick();
    assert.equal(app.run('translationView.row'),null);
    assert.equal(app.element('translation-content').children.length,0);
  }
});
test('ambiguous translation submission is followed by status GET and not automatic POST retry', async () => {
  const methods = [];
  const app = translationApp(async (_url,options) => {
    methods.push(options.method);
    if (options.method === 'POST') throw new Error('connection lost');
    return response(methods.length === 1 ? {configured:true,translation:null} : translationFixture());
  });
  openTranslationFixture(app); await until(() => app.run('translationView.loaded'));
  app.element('translate-lecture').onclick(); await until(() => !!app.run('translationView.error'));
  assert.equal(app.element('translate-lecture').textContent,'번역 상태 확인');
  app.element('translate-lecture').onclick(); await until(() => app.run('translationView.row?.status') === 'completed');
  assert.deepEqual(methods,['GET','POST','GET']);
});
test('translation polling is bounded and a manual refresh only restarts GET polling', async () => {
  const methods = [];
  const app = translationApp(async (_url,options) => {methods.push(options.method); return response(translationFixture('processing'));});
  openTranslationFixture(app); await until(() => app.run('translationView.loaded'));
  app.run('translationView.polls=200'); await app.runTimeout(3000);
  assert.ok(![...app.timeouts.values()].some(t => t.delay === 3000));
  app.element('translate-lecture').onclick(); await until(() => app.run('translationView.polls') === 1);
  assert.deepEqual(methods,['GET','GET','GET']);
});
test('translation Markdown escapes model HTML, preserves both views and cannot export after logout', async () => {
  const fixture = translationFixture(); fixture.translation.segments[0].text='<script>bad()</script> 번역';
  const app = translationApp(async () => response(fixture));
  openTranslationFixture(app); await until(() => app.run('translationView.row?.status') === 'completed');
  assert.equal(app.createdAll('script').length,0);
  app.element('translation-download').onclick();
  let link = app.created('a'); let text = await app.objectUrlBlob(link.href).text();
  assert.match(link.download,/_문장대조\.md$/); assert.match(text,/원문:/); assert.match(text,/&lt;script&gt;/);
  assert.doesNotMatch(text,/<script>/);
  app.element('translation-full').onclick(); app.element('translation-download').onclick();
  link = app.created('a'); text = await app.objectUrlBlob(link.href).text();
  assert.doesNotMatch(text,/원문:|The bank/); assert.match(text,/강의 흐름/);
  const exports = app.createdAll('a').length;
  app.run('token=""; current=null; renderCurrent()'); app.element('translation-download').onclick();
  assert.equal(app.createdAll('a').length,exports); assert.equal(app.element('translation-panel').hidden,true);
});

function completedSummaryFixture(lectureId = 'summary-lesson', text = '핵심 내용입니다.') {
  return {lecture_id:lectureId,status:'completed',model:'solar-pro4',document:{
    overview:text,overview_source_ids:['summary-source'],
    sections:[{heading:'핵심 주제',bullets:[{text,source_ids:['summary-source']}]}],
    review_questions:[{question:'핵심 내용을 설명할 수 있나요?',source_ids:['summary-source']}],
  }};
}
function openSummaryFixture(app, finalized = true) {
  app.run(`current={id:'summary-lesson',title:'요약 수업',created_at:'2026-01-01T00:00:00Z',
    recording_finalized:${finalized},segments:[{id:'summary-source',start:15,end:20,text:'핵심 내용입니다.'}]};
    renderCurrent();`);
}

test('summary creation is opt-in, waits for final saving, and does not stop a different live lecture', async () => {
  const methods = [];
  let posted = false;
  const app = setup(async (url, options = {}) => {
    assert.ok(url.endsWith('/summary'));
    methods.push(options.method);
    if (options.method === 'POST') {
      posted = true;
      return response({summary:{lecture_id:'summary-lesson',status:'queued'}},202);
    }
    return response({configured:true,model:'solar-pro4',summary:posted ? completedSummaryFixture() : null});
  });
  openSummaryFixture(app,false);
  app.element('summarize-lecture').onclick();
  await tick();
  assert.deepEqual(methods,[]);
  app.run(`current.recording_finalized=true; recording=true;
    capture={recording:true,paused:false,stopCalls:0,stop(){this.stopCalls+=1;}};
    captureSession={id:'live-summary-test',lecture:{id:'live-summary-test'},source:'microphone',asrProvider:'qwen'};
    renderCurrent();`);
  await until(() => app.run('summaryView.loaded'));
  assert.deepEqual(methods,['GET']);
  app.element('summarize-lecture').onclick(); app.element('summarize-lecture').onclick();
  await until(() => app.run('summaryView.row?.status') === 'queued');
  assert.deepEqual(methods,['GET','POST']);
  await app.runTimeout(3000);
  assert.equal(app.run('summaryView.row.status'),'completed');
  assert.equal(app.run('recording'),true);
  assert.equal(app.run('capture.stopCalls'),0);
  assert.equal(app.run('current.segments[0].text'),'핵심 내용입니다.');
  assert.equal(app.element('summary-download').disabled,false);
  assert.match(app.element('summary-content').children[0].children[0].textContent,/00:15/);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 3000));
});

test('late summary responses are discarded on every account, token, server, and lecture transition', async () => {
  for (const change of ["user='user-beta'", "token='next-token'", "apiUrl='https://changed.example'", "current={id:'another',segments:[]}"] ) {
    const pendingSummary = deferred();
    const app = setup(async () => pendingSummary.promise);
    openSummaryFixture(app);
    await tick();
    app.run(`${change}; resetSummaryView();`);
    pendingSummary.resolve(response({configured:true,summary:completedSummaryFixture()}));
    await tick(); await tick();
    assert.equal(app.run('summaryView.row'),null);
    assert.equal(app.element('summary-content').children.length,0);
  }
});

test('a lost summary POST response requires a GET status check, never an automatic repeated POST', async () => {
  const methods = [];
  const app = setup(async (_url, options = {}) => {
    methods.push(options.method);
    if (options.method === 'POST') throw new Error('simulated connection loss');
    return response({configured:true,summary:methods.length === 1 ? null : completedSummaryFixture()});
  });
  openSummaryFixture(app);
  await until(() => app.run('summaryView.loaded'));
  app.element('summarize-lecture').onclick();
  await until(() => !!app.run('summaryView.error'));
  assert.equal(app.element('summarize-lecture').textContent,'요약 상태 확인');
  app.element('summarize-lecture').onclick();
  await until(() => app.run('summaryView.row?.status') === 'completed');
  assert.deepEqual(methods,['GET','POST','GET']);
});

test('summary rendering rejects foreign source IDs and untrusted incomplete documents', async () => {
  for (const mutate of [row => { row.document.overview_source_ids=['foreign-source']; },
    row => { delete row.document.review_questions; }, row => { row.lecture_id='foreign-lecture'; }]) {
    const row = completedSummaryFixture(); mutate(row);
    const app = setup(async () => response({configured:true,summary:row}));
    openSummaryFixture(app);
    await until(() => !!app.run('summaryView.error'));
    assert.equal(app.element('summary-content').children.length,0);
    assert.equal(app.element('summary-download').disabled,true);
  }
});

test('summary text is rendered literally and the separate Markdown export escapes active content', async () => {
  const text = '<script>alert(1)</script> [외부 링크](https://invalid.example)';
  const app = setup(async () => response({configured:true,summary:completedSummaryFixture('summary-lesson',text)}));
  openSummaryFixture(app);
  await until(() => app.run('summaryView.row?.status') === 'completed');
  assert.equal(app.element('summary-content').children[0].textContent,text);
  assert.equal(app.createdAll('script').length,0);
  app.element('summary-download').onclick();
  const link = app.created('a');
  assert.match(link.download,/_수업요약\.md$/);
  const exported = await app.objectUrlBlob(link.href).text();
  assert.doesNotMatch(exported,/<script>|\[외부 링크\]\(https/);
  assert.match(exported,/&lt;script&gt;/);
  assert.match(exported,/원문 00:15/);
});

test('summary polling has a finite bound and manual status refresh restarts GET polling only', async () => {
  const methods = [];
  const app = setup(async (_url, options = {}) => {
    methods.push(options.method);
    return response({configured:true,summary:{lecture_id:'summary-lesson',status:'processing'}});
  });
  openSummaryFixture(app);
  await until(() => app.run('summaryView.loaded'));
  app.run('summaryView.polls=200');
  await app.runTimeout(3000);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 3000));
  app.element('summarize-lecture').onclick();
  await until(() => app.run('summaryView.polls') === 1);
  assert.deepEqual(methods,['GET','GET','GET']);
});

test('disabled summary API and logout keep creation and stale exports unavailable', async () => {
  let calls = 0;
  const app = setup(async () => { calls += 1; return response({configured:false,summary:null}); });
  openSummaryFixture(app);
  await until(() => app.run('summaryView.loaded'));
  assert.equal(app.element('summarize-lecture').disabled,true);
  app.element('summarize-lecture').onclick();
  await tick(); assert.equal(calls,1);
  app.run('token=""; current=null; renderCurrent()');
  assert.equal(app.element('summary-panel').hidden,true);
  assert.equal(app.element('summary-content').children.length,0);
  app.element('summary-download').onclick();
  assert.equal(app.createdAll('a').length,0);
});

test('activation presents and enforces the four-character minimum in the browser', () => {
  const app = setup(async () => response({}));
  app.run('setActivation(true)');
  assert.equal(app.element('password').minLength, 4);
  assert.equal(app.element('password-confirm').minLength, 4);
  assert.match(app.element('password-label').textContent, /4자 이상/);
});

test('temporary input loss during lecture creation keeps capture open until an explicit stop', async () => {
  const creation = deferred(), uploads = [];
  let lectureId = '';
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      lectureId = options.headers.get('X-Lecture-Id');
      return creation.promise;
    }
    uploads.push(options); return response({segments:[]});
  });
  await app.run('startRecording()');
  await tick();
  assert.equal(app.run('recording'), true);
  assert.equal(app.element('record-button').disabled, false);
  app.microphone().callbacks.onInputUnavailable(new Error('입력이 잠시 중단됨'),{
    reason:'track-muted',reconnectNeeded:false,source:'microphone',
  });
  await tick();
  assert.equal(app.run('recording'), true);
  assert.equal(app.run('inputUnavailable'), true);
  assert.equal(app.microphone().stopCalls, undefined);
  assert.equal(uploads.length, 0);
  assert.equal(app.intervals.size, 0, 'elapsed time pauses while no input is arriving');

  creation.resolve(response({id:lectureId,title:'수업',created_at:new Date().toISOString(),segments:[]}));
  await tick(); await tick();
  app.microphone().callbacks.onInputRecovered({reason:'track-unmuted',source:'microphone'});
  await tick();
  assert.equal(app.run('inputUnavailable'), false);
  assert.equal(app.run('recording'), true);
  assert.equal(app.intervals.size, 1);

  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.run('recording || starting || stopping'), false);
  assert.equal(app.microphone().stopCalls, 1);
  assert.equal(uploads.length, 0, 'stopping without any accepted PCM must not synthesize audio');
  assert.equal(app.run('pending.length'), 0);
});

test('the selected computer-audio source is passed to capture without a preceding network request', async () => {
  const requests = [];
  const app = setup(async (url, options) => {
    requests.push({url,method:options.method || 'GET',body:options.body});
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'온라인 강의',language:'ko',asr_provider:'qwen',created_at:new Date().toISOString(),segments:[]},201);
    return response({segments:[]});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  app.element('asr-provider').onchange();
  app.element('audio-source').value = 'system';
  app.element('audio-source').onchange();
  assert.equal(app.element('source-privacy').hidden, false);
  assert.equal(app.element('asr-provider').value, 'qwen');
  assert.equal(app.element('asr-provider').disabled, true);
  assert.match(app.element('provider-guidance').textContent, /항상 이 PC의 Qwen/);
  app.element('audio-source').value = 'microphone';
  app.element('audio-source').onchange();
  assert.equal(app.element('asr-provider').value, 'clova',
    'the explicit microphone preference survives a temporary system-audio selection');
  app.element('audio-source').value = 'system';
  app.element('audio-source').onchange();
  const starting = app.run('startRecording()');
  assert.equal(app.microphone().callbacks.source, 'system');
  assert.equal(requests.length, 0, 'capture is opened synchronously before lecture creation');
  await starting;
  assert.equal(JSON.parse(requests.find(request => request.url.endsWith('/lectures')).body).asr_provider,'qwen');
  await app.run('stopRecording()');
  await tick();
  assert.deepEqual(app.coordination.captureOwners,['user-alpha']);
  assert.deepEqual(app.coordination.uploaderOwners,[],
    'a capture with no PCM has no durable chunk to serialize through the uploader lock');
  assert.equal(app.coordination.releasedCaptureLeases,1);
});

test('authenticated status enables only the advertised CLOVA choice and ignores provider-supplied display or secret fields', async () => {
  const app = setup(async url => url.endsWith('/status') ? response({
    model_state:'ready',
    transcription_providers:{
      qwen:{configured:true,label:'untrusted local label'},
      clova:{configured:true,label:'untrusted cloud label',secret_key:'must-not-render'},
    },
  }) : response({}));
  assert.equal(app.element('asr-provider').value,'qwen');
  app.element('language').value = 'auto';
  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider').value,'clova');
  assert.equal(app.element('language').value,'ko');
  assert.equal(app.element('asr-provider-clova').disabled,false);
  assert.match(app.element('asr-provider-clova').textContent,/기본/);
  assert.match(app.element('asr-provider-clova').textContent,/NAVER CLOVA Speech/);
  assert.doesNotMatch(app.element('asr-provider-clova').textContent,/untrusted|must-not-render/);
  assert.doesNotMatch(app.run('JSON.stringify(transcriptionProviders)'),/secret|label|must-not-render/);

  app.element('language').value = 'auto';
  app.element('asr-provider').value = 'clova';
  app.element('asr-provider').onchange();
  assert.equal(app.element('language').value,'ko');
  assert.equal(app.element('provider-privacy').hidden,false);
  assert.match(app.element('provider-privacy').textContent,/NAVER Cloud/);
  assert.match(app.element('provider-privacy').textContent,/사이트 운영자가 관리하는.*계정/);
  assert.match(app.element('provider-privacy').textContent,/전송/);
  assert.doesNotMatch(app.element('provider-privacy').textContent,/비용|과금/);
  assert.match(app.element('provider-privacy').textContent,/Object Storage/);
  assert.match(app.element('provider-privacy').textContent,/자동 저장/);
  assert.match(app.element('provider-privacy').textContent,/삭제해도 그 클라우드 사본은 삭제되지/);
});

test('an unavailable CLOVA provider keeps an automatic new microphone lesson on Qwen', async () => {
  const app = setup(async url => url.endsWith('/status') ? response({
    model_state:'ready',transcription_providers:{qwen:{configured:true},clova:{configured:false}},
  }) : response({}));
  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider-clova').disabled,true);
  assert.equal(app.element('asr-provider').value,'qwen');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  assert.equal(app.microphone(),undefined);
  assert.equal(app.run('draft'),null);
  assert.match(app.element('notice').textContent,/설정되지 않았/);
  assert.equal(app.element('asr-provider').value,'clova','start does not silently switch engines');
});

test('the automatic microphone provider falls back on status failure and returns to CLOVA after recovery', async () => {
  let state = 'ready';
  const app = setup(async url => {
    if (!url.endsWith('/status')) return response({});
    if (state === 'failed') throw new TypeError('status unavailable');
    return response({model_state:'ready',transcription_providers:{
      qwen:{configured:true},clova:{configured:true},
    }});
  });

  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider').value,'clova');
  state = 'failed';
  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider').value,'qwen');
  assert.equal(app.run('micProviderPreference'),null);
  state = 'ready';
  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider').value,'clova');
});

test('an older status response cannot overwrite a newer provider decision', async () => {
  const first = deferred();
  const second = deferred();
  let calls = 0;
  const app = setup(async url => {
    if (!url.endsWith('/status')) return response({});
    calls += 1;
    return calls === 1 ? first.promise : second.promise;
  });
  const older = app.run('updateStatus()');
  const newer = app.run('updateStatus()');
  second.resolve(response({model_state:'ready',transcription_providers:{
    qwen:{configured:true},clova:{configured:false},
  }}));
  await newer;
  assert.equal(app.element('asr-provider').value,'qwen');
  first.resolve(response({model_state:'ready',transcription_providers:{
    qwen:{configured:true},clova:{configured:true},
  }}));
  await older;
  assert.equal(app.element('asr-provider').value,'qwen');
  assert.equal(app.run('transcriptionProviders.clova.configured'),false);
});

test('an explicit Qwen microphone preference survives polling, source changes, and a new note', async () => {
  const app = setup(async url => url.endsWith('/status') ? response({
    model_state:'ready',transcription_providers:{qwen:{configured:true},clova:{configured:true}},
  }) : response({}));
  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider').value,'clova');
  app.element('asr-provider').value = 'qwen';
  app.element('asr-provider').onchange();
  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider').value,'qwen');
  app.element('audio-source').value = 'system';
  app.element('audio-source').onchange();
  app.element('audio-source').value = 'microphone';
  app.element('audio-source').onchange();
  assert.equal(app.element('asr-provider').value,'qwen');
  app.run('resetNewNote()');
  assert.equal(app.element('asr-provider').value,'qwen');
});

test('a CLOVA microphone lecture snapshots its provider in creation and every queued chunk', async () => {
  const requests = [];
  const app = setup(async (url,options = {}) => {
    requests.push({url,options});
    if (url.endsWith('/lectures')) return response({
      id:options.headers.get('X-Lecture-Id'),title:'클로바 수업',language:'ko',asr_provider:'clova',created_at:new Date().toISOString(),segments:[],
    },201);
    return response({segments:[]});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  app.element('language').value = 'auto';
  app.element('asr-provider').onchange();
  await app.run('startRecording()');
  const creation = requests.find(request => request.url.endsWith('/lectures'));
  assert.deepEqual(JSON.parse(creation.options.body),{
    title:app.run('captureSession.title'),language:'ko',asr_provider:'clova',
  });
  assert.equal(app.run('captureSession.asrProvider'),'clova');
  assert.equal(app.element('asr-provider').disabled,true);
  assert.equal(app.element('asr-provider').value,'clova');

  app.element('asr-provider').value = 'qwen';
  app.microphone().callbacks.onChunk(chunk(0));
  assert.equal(app.run("pending[0]?.asrProvider || 'sent'"),'clova');
  await tick(); await tick();
  app.microphone().tail = chunk(5,3,3,true);
  await app.run('stopRecording()');
  await tick(); await tick();
  assert.ok(requests.filter(request => request.url.includes('/chunks')).length >= 1);
});

test('a status failure never changes an in-progress CLOVA microphone session', async () => {
  let statusFails = false;
  const app = setup(async url => {
    if (url.endsWith('/status')) {
      if (statusFails) throw new TypeError('status unavailable');
      return response({model_state:'ready',transcription_providers:{
        qwen:{configured:true},clova:{configured:true},
      }});
    }
    return response({segments:[]});
  });
  await app.run('updateStatus()');
  assert.equal(app.element('asr-provider').value,'clova');
  await app.run('startRecording()');
  assert.equal(app.run('captureSession.asrProvider'),'clova');
  statusFails = true;
  await app.run('updateStatus()');
  assert.equal(app.run('captureSession.asrProvider'),'clova');
  assert.equal(app.element('asr-provider').value,'clova');
  app.microphone().tail = null;
  await app.run('stopRecording()');
});

test('a mismatched server lecture provider blocks upload without ending capture', async () => {
  const requests = [];
  const app = setup(async (url,options = {}) => {
    requests.push({url,options});
    if (url.endsWith('/lectures')) return response({
      id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',asr_provider:'clova',created_at:new Date().toISOString(),segments:[],
    },201);
    return response({segments:[]});
  });
  await app.run('startRecording()');
  await tick(); await tick();

  assert.equal(requests.filter(request => request.url.includes('/chunks')).length,0);
  assert.equal(app.run('recording'),true);
  assert.equal(app.microphone().stopCalls,undefined);
  assert.equal(app.run('draft.asrProvider'),'qwen');
  assert.equal(app.run('draft.lecture'),null);
  assert.match(app.run('sendError'),/다른 수업을 반환/);

  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  await app.run('saveFailedChunk()');
  await app.run('skipFailedChunk()');
  await tick(); await tick();
  assert.equal(app.run('pending.length'),0);
  assert.equal(app.run('recording'),true);
  assert.ok(app.run('sendError'),'the assignment gate remains visible while capture continues');
  assert.notEqual(app.run('draft'),null);

  app.microphone().tail = chunk(5,3,3,true);
  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.microphone().stopCalls,1);
  assert.equal(app.run('pending.length'),1);
  await app.run('saveFailedChunk()');
  await app.run('skipFailedChunk()');
  await tick(); await tick();
  assert.equal(app.run('pending.length'),0);
  assert.equal(app.run('draft'),null);
  assert.equal(app.run('captureSession'),null);
  assert.equal(app.run('isBusy()'),false,'explicitly discarding the saved orphan must allow a new class');
});

test('pause flushes a non-final boundary and resume continues the same lecture and timeline', async () => {
  let lectureCreates = 0;
  let lectureId = '';
  const uploads = [];
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) {
      lectureCreates += 1;
      lectureId = options.headers.get('X-Lecture-Id');
      return response({id:lectureId,title:'수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (lectureId && url.includes(`/lectures/${lectureId}/chunks`)) {
      uploads.push({
        url,
        start:options.headers.get('X-Start-Seconds'),
        overlap:options.headers.get('X-Overlap-Seconds'),
        final:options.headers.get('X-Final-Chunk'),
      });
      return response({segments:[],recording_available:true,recording_finalized:options.headers.get('X-Final-Chunk') === 'true'});
    }
    return response({});
  });
  await app.run('startRecording()');
  app.microphone().pauseTail = chunk(0,4,0,false);
  await app.run('pauseRecording()');
  await tick(); await tick();
  assert.equal(app.run('recording'),false);
  assert.equal(app.run('paused'),true);
  assert.equal(app.microphone().pauseCalls,1);
  assert.equal(app.element('pause-button')['aria-pressed'],'true');
  assert.match(app.element('pause-button').textContent,/재개/);
  assert.match(app.element('record-state').textContent,/일시정지/);
  assert.match(app.element('record-hint').textContent,/녹음하거나 전송하지 않/);
  let historyButton = app.element('lecture-list').querySelectorAll('button')[0];
  assert.match(historyButton.children[2].textContent,/일시정지/);
  assert.match(historyButton['aria-label'],/일시정지/);
  assert.equal(uploads[0].final,'false');

  app.microphone().tail = chunk(1,5,3,true);
  await app.run('resumeRecording()');
  assert.equal(app.run('recording'),true);
  assert.equal(app.run('paused'),false);
  assert.equal(app.microphone().resumeCalls,1);
  historyButton = app.element('lecture-list').querySelectorAll('button')[0];
  assert.match(historyButton.children[2].textContent,/현재 녹음/);
  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(lectureCreates,1,'resume must not create a second lecture');
  assert.deepEqual(uploads.map(item => item.final),['false','true']);
  assert.deepEqual(uploads.map(item => item.start),['0','1']);
  assert.deepEqual(uploads.map(item => item.overlap),['0','3']);
  assert.ok(uploads.every(item => item.url.includes(`/lectures/${lectureId}/chunks`)));
  assert.equal(app.run('current.recording_finalized'),true);
  assert.equal(app.run('recording || paused || starting || pausing || resuming || stopping'),false);
  historyButton = app.element('lecture-list').querySelectorAll('button')[0];
  assert.equal(historyButton.children.length,2,'a stopped lecture must not retain a live badge');
  assert.equal(historyButton['aria-label'],undefined);
});

test('a defensive short non-final upload is never zero-padded beyond its real timeline', async () => {
  const app = setup(async () => response({}));
  const result = await app.run(`(() => {
    const blob=new Blob([new Uint8Array(364)],{type:'audio/wav'});
    return uploadBlob({blob,durationSeconds:0.01,final:false})
      .then(uploaded=>({same:uploaded===blob,size:uploaded.size}));
  })()`);
  assert.equal(result.same,true);
  assert.equal(result.size,364);
});

test('auth expiry keeps a paused capture open and offers an explicit local stop', async () => {
  const app = setup(async (url,options = {}) => url.endsWith('/lectures')
    ? response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201)
    : response({segments:[],recording_available:true,recording_finalized:true}));
  await app.run('startRecording()');
  app.microphone().pauseTail = chunk(0,2,0,false);
  app.microphone().tail = chunk(0,2,2,true);
  await app.run('pauseRecording()');
  assert.equal(app.run('isBusy()'),true,'beforeunload and navigation must remain guarded while paused');
  assert.equal(app.run('presenceActivity()'),'recording');
  app.run('showLogin()');
  await tick(); await tick();
  assert.equal(app.microphone().stopCalls,undefined);
  assert.equal(app.run('paused'),true);
  assert.equal(app.element('auth-capture-stop').hidden,false);
  assert.equal(app.element('workspace').hidden,true);

  app.element('auth-capture-stop').onclick();
  await tick(); await tick();
  assert.equal(app.microphone().stopCalls,1);
  assert.equal(app.run('recording || paused || stopping'),false);
  assert.equal(app.element('auth-capture-stop').hidden,true);
});

test('an explicit caller abort reaches fetch and is not mislabeled as a timeout', async () => {
  const app = setup((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      const error = new Error('caller stopped'); error.name = 'AbortError'; reject(error);
    }, {once:true});
  }));
  const request = app.run(`(() => {
    const controller = new AbortController();
    const pendingRequest = api('/imports/job', {signal:controller.signal});
    controller.abort();
    return pendingRequest;
  })()`);
  await assert.rejects(request, error => {
    assert.equal(error.name, 'AbortError');
    assert.notEqual(error.transient, true);
    return true;
  });
});

test('a late imported-lecture response cannot replace a note selected afterward', async () => {
  const late = deferred();
  const app = setup(url => url.endsWith('/lectures/import-lecture') ? late.promise : response([]));
  const loading = app.run(`(() => {
    importJob={id:'job',lecture_id:'import-lecture',status:'processing'};
    current={id:'import-lecture',title:'가져오는 중',created_at:'2026-01-01T00:00:00Z',segments:[]};
    selectImportLecture=true;
    return refreshImportLecture(importJob,true,importGeneration);
  })()`);
  await tick();
  app.run(`++requestGeneration; current={id:'chosen-lecture',title:'직접 고른 기록',created_at:'2026-01-02T00:00:00Z',segments:[]};`);
  late.resolve(response({id:'import-lecture',title:'늦은 응답',created_at:'2026-01-01T00:00:00Z',segments:[]}));
  await loading;
  assert.equal(app.run('current.id'), 'chosen-lecture');
});

test('navigating to a new note clears the import auto-selection latch', async () => {
  let importedLectureRequests = 0;
  const app = setup(url => {
    if (url.endsWith('/lectures/import-lecture')) importedLectureRequests += 1;
    return response({id:'import-lecture',title:'완료된 가져오기',created_at:'2026-01-01T00:00:00Z',segments:[]});
  });
  await app.run(`(() => {
    importJob={id:'job',lecture_id:'import-lecture',status:'processing'};
    current={id:'import-lecture',title:'가져오는 중',created_at:'2026-01-01T00:00:00Z',segments:[]};
    selectImportLecture=true;
    resetNewNote();
    return refreshImportLecture({...importJob,status:'completed'},true,importGeneration);
  })()`);
  assert.equal(app.run('current'), null);
  assert.equal(app.run('selectImportLecture'), false);
  assert.equal(importedLectureRequests, 0, 'completion must not pull the user back after explicit navigation');
});

test('a recovered import response from the previous account cannot overwrite the next account', async () => {
  const late = deferred();
  const app = setup(url => url.endsWith('/imports') ? late.promise : response([]));
  const recovering = app.run('recoverFileImport()');
  await tick();
  app.run(`
    showLogin();
    token='beta-token'; user='user-beta';
    importJob={id:'beta-job',lecture_id:'beta-lecture',filename:'beta.m4a',status:'processing'};
    fileUploader={marker:'beta-uploader',detach(){}};
  `);
  late.resolve(response({imports:[{
    id:'alpha-job',lecture_id:'alpha-lecture',filename:'alpha-private.m4a',status:'processing',
  }]}));
  await recovering;
  assert.equal(app.run('importJob.id'), 'beta-job');
  assert.equal(app.run('fileUploader.marker'), 'beta-uploader');
  assert.notEqual(app.run('importJob.filename'), 'alpha-private.m4a');
});

test('navigation during import recovery keeps the watcher without auto-selecting its lecture', async () => {
  const late = deferred();
  class RecoveringUploader {
    detach() {}
    recover() { this.recovered = true; return new Promise(() => {}); }
  }
  const app = setup(url => url.endsWith('/imports') ? late.promise : response([]), {FileUploader:RecoveringUploader});
  const recovering = app.run('recoverFileImport()');
  await tick();
  app.run('resetNewNote()');
  late.resolve(response({imports:[{
    id:'active-job',lecture_id:'active-lecture',filename:'ongoing.m4a',status:'processing',
  }]}));
  await recovering;
  assert.equal(app.run('importJob.id'), 'active-job');
  assert.equal(app.run('fileUploader.recovered'), true);
  assert.equal(app.run('selectImportLecture'), false);
  assert.equal(app.run('current'), null);
});

test('a late cancel response from the previous account cannot alter the next account state', async () => {
  const app = setup(async () => response([]));
  const cancelling = app.run(`(() => {
    const pendingCancel=new Promise(resolve => { globalThis.resolveOldCancel=resolve; });
    importJob={id:'alpha-job',lecture_id:'alpha-lecture',filename:'private.wav',status:'processing'};
    fileUploader={file:{},detach(){},cancel:()=>pendingCancel};
    return cancelFileImport();
  })()`);
  await tick();
  app.run(`
    showLogin();
    token='beta-token'; user='user-beta';
    importJob={id:'beta-job',lecture_id:'beta-lecture',filename:'beta.wav',status:'processing'};
    fileUploader={marker:'beta-uploader',detach(){}};
    importCancelling=true;
    globalThis.resolveOldCancel({id:'alpha-job',lecture_id:null,filename:'private.wav',status:'cancelled',raw_deleted:true});
  `);
  await cancelling;
  assert.equal(app.run('importJob.id'), 'beta-job');
  assert.equal(app.run('fileUploader.marker'), 'beta-uploader');
  assert.equal(app.run('importCancelling'), true, 'old finally must not clear the new session flag');
});

test('only the newest lecture-list refresh can update history', async () => {
  const first = deferred(), second = deferred();
  let calls = 0;
  const app = setup(url => {
    if (url.endsWith('/lectures')) return ++calls === 1 ? first.promise : second.promise;
    return response({});
  });
  const older = app.run('refreshLectures()');
  const newer = app.run('refreshLectures()');
  second.resolve(response([{id:'new',title:'최신',created_at:'2026-01-02T00:00:00Z'}]));
  await newer;
  first.resolve(response([{id:'old',title:'오래된 응답',created_at:'2026-01-01T00:00:00Z'}]));
  await older;
  assert.equal(app.run('lectures[0].id'), 'new');
});

test('lecture history groups and filters by the fixed Korean calendar date', () => {
  const app = setup(async () => response({}));
  assert.equal(app.run("dateKey('2026-01-01T16:00:00Z')"), '2026-01-02');
  assert.match(app.run("dateLabel('2026-01-01T16:00:00Z')"), /1월 2일/);
  app.run(`
    lectures=[
      {id:'late',title:'둘째 날 오후',created_at:'2026-01-02T02:00:00Z'},
      {id:'early',title:'둘째 날 새벽',created_at:'2026-01-01T16:00:00Z'},
      {id:'previous',title:'첫째 날',created_at:'2026-01-01T14:00:00Z'},
    ];
    current=lectures[1];
    renderHistory();
  `);
  assert.deepEqual(app.element('lecture-date').children.map(option => option.value), ['', '2026-01-02', '2026-01-01']);
  assert.equal(app.element('lecture-list').children.length, 2);
  assert.match(app.element('lecture-list').children[0].children[0].textContent, /1월 2일/);
  assert.equal(app.element('lecture-list').children[0].children[1].children.length, 2);
  assert.equal(app.element('lecture-list').children[0].children[1].children[1]['aria-current'], 'page');

  app.element('lecture-date').value = '2026-01-01';
  app.element('lecture-date').onchange();
  assert.equal(app.run('lectureDateFilter'), '2026-01-01');
  assert.equal(app.element('lecture-count').textContent, '1/3');
  assert.equal(app.element('lecture-list').children.length, 1);
  assert.equal(app.element('lecture-list').children[0].children[1].children[0].children[0].textContent, '첫째 날');
});

test('starting a new recording clears an older date filter before the new lecture arrives', async () => {
  const creation = deferred();
  let lectureId = '';
  const app = setup((url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') {
      lectureId = options.headers.get('X-Lecture-Id');
      return creation.promise;
    }
    return response({segments:[],recording_available:true,recording_finalized:true});
  });
  app.run(`
    lectures=[{id:'old',title:'지난 수업',created_at:'2026-01-01T00:00:00Z'}];
    lectureDateFilter='2026-01-01';
    renderHistory();
  `);
  const starting = app.run('startRecording()');
  assert.equal(app.run('lectureDateFilter'), '');
  assert.equal(app.element('lecture-date').value, '');
  await starting;
  await tick();
  creation.resolve(response({id:lectureId,title:'새 수업',created_at:'2026-02-01T00:00:00Z',segments:[]},201));
  await tick(); await tick();
  assert.deepEqual(Array.from(app.run('lectures.map(lecture => lecture.id)')), [lectureId,'old']);
  assert.equal(app.element('lecture-list').children.length, 2, 'the new date must not stay hidden by the old filter');
  await app.run('stopRecording()');
  await tick(); await tick();
});

test('starting a new file import clears an older date filter without changing resume behavior', async () => {
  class PendingUploader {
    detach() {}
    start() { this.running = true; return new Promise(() => {}); }
  }
  const app = setup(async () => response({}), {FileUploader:PendingUploader});
  const file = new Blob([new Uint8Array(10)]);
  Object.defineProperty(file, 'name', {value:'새 수업.wav'});
  app.element('recording-file').files = [file];
  app.run(`
    lectures=[{id:'old',title:'지난 수업',created_at:'2026-01-01T00:00:00Z'}];
    lectureDateFilter='2026-01-01';
    renderHistory();
  `);
  await app.run('startOrResumeFileImport()');
  assert.equal(app.run('lectureDateFilter'), '');
  assert.equal(app.element('lecture-date').value, '');
});

test('Markdown and plain-text exports preserve content safely independently of audio work', async () => {
  const app = setup(async () => response({}));
  app.run(`
    current={id:'lesson',title:'# 역사 [1]',language:'ko',created_at:'2026-01-01T16:00:00Z',
      recording_available:true,recording_finalized:true,
      segments:[{id:'s1',start:2,text:'<script> *강조* \\\\경로 $공식$ ~~삭제~~ [링크](https://example.test?a=1&b=2)'}]};
    renderCurrent();
  `);
  assert.equal(app.element('download').disabled, false);
  app.element('export-format').value = 'markdown';
  app.element('download').onclick();
  const markdownLink = app.created('a');
  const markdown = await app.objectUrlBlob(markdownLink.href).text();
  assert.equal(markdownLink.download, '# 역사 [1].md');
  assert.ok(markdown.startsWith('# \\# 역사 \\[1\\]'));
  assert.ok(markdown.includes('**\\[00:02\\]** \\<script\\> \\*강조\\* \\\\경로'));
  assert.ok(markdown.includes(String.raw`\$공식\$ \~\~삭제\~\~ \[링크\]\(https\:\/\/example\.test\?a\=1\&b\=2\)`));

  app.element('export-format').value = 'text';
  app.element('download').onclick();
  const textLink = app.created('a');
  const plain = await app.objectUrlBlob(textLink.href).text();
  assert.equal(textLink.download, '# 역사 [1].txt');
  assert.ok(plain.includes('[00:02] <script> *강조* \\경로 $공식$ ~~삭제~~'));

  app.run('recording=true; updateControls()');
  for (const id of ['export-format','download','recording-download']) assert.equal(app.element(id).disabled,false,`${id} is read-only during recording`);
  assert.equal(app.element('delete-lecture').disabled,true);
});

test('live transcript updates reuse unchanged rows and text nodes without changing the reading position', () => {
  const app = setup(async () => response({}));
  app.run(`
    current={id:'long-lecture',title:'긴 수업',created_at:'2026-01-01T00:00:00Z',asr_provider:'qwen',
      segments:Array.from({length:1000},(_,index) => ({id:'s'+index,start:index * 8,end:index * 8+4,text:'문장 '+index}))};
    renderCurrent();
  `);
  const transcript = app.element('transcript');
  const originalRows = [...transcript.children];
  const selectedParagraph = originalRows[420].children[1];
  const selectionAnchor = selectedParagraph.firstChild;
  transcript.scrollTop = 4321;
  app.run(`mergeChunkSegments(current,{segments:[{id:'s1000',start:8000,end:8004,text:'새 문장'}]}); renderCurrent()`);
  assert.equal(transcript.children.length,1001);
  for (let index = 0; index < originalRows.length; index += 1) {
    assert.equal(transcript.children[index],originalRows[index]);
  }
  assert.equal(selectedParagraph.firstChild,selectionAnchor,'unchanged selected text must not be replaced');
  assert.equal(transcript.scrollTop,4321);
  app.run(`current.segments[999].text='변경된 마지막 문장'; renderCurrent()`);
  assert.equal(transcript.children[999],originalRows[999],'changing text must reuse its row');
  assert.equal(transcript.children[999].children[1].textContent,'변경된 마지막 문장');
  assert.equal(selectedParagraph.firstChild,selectionAnchor);
  assert.equal(transcript.scrollTop,4321);

  app.run(`mergeChunkSegments(current,{segments:[{id:'late-boundary',start:7990,end:7991,text:'뒤늦게 확정된 경계'}]}); renderCurrent()`);
  assert.equal(transcript.children[998],originalRows[998],'a late boundary leaves preceding rows untouched');
  assert.equal(transcript.children[1000],originalRows[999],'a late boundary retains the following row');
  assert.equal(transcript.children[420],originalRows[420]);
  assert.equal(selectedParagraph.firstChild,selectionAnchor);
});

test('transcript keys are isolated by lecture, account, provider, origin, and raw versus corrected view', () => {
  const app = setup(async () => response({}));
  app.run(`
    current={id:'lesson',title:'수업',created_at:'2026-01-01T00:00:00Z',asr_provider:'qwen',
      recording_finalized:true,segments:[{id:'shared-id',start:0,end:1,text:'받아쓴 원문'}]}; renderCurrent();
    correction={status:'completed',corrected_segments:[{id:'shared-id',start:null,end:null,text:'후보정본'}]};
  `);
  let previous = app.element('transcript').children[0];
  app.run(`correctionView='corrected'; renderCurrent()`);
  let next = app.element('transcript').children[0];
  assert.notEqual(next,previous);
  assert.equal(next.children.length,2); // Text plus the explicit manual-edit/note actions.
  assert.equal(next.children[1].className,'segment-tools');
  assert.equal(next.children[0].textContent,'후보정본');
  previous = next;
  app.run(`correctionView='raw'; renderCurrent()`);
  next = app.element('transcript').children[0];
  assert.notEqual(next,previous);
  assert.equal(next.children[1].textContent,'받아쓴 원문');
  for (const change of [
    "current.id='another-lesson'",
    "user='user-beta'",
    "current.asr_provider='clova'",
    "apiUrl='https://another-tunnel.trycloudflare.com'",
  ]) {
    previous = app.element('transcript').children[0];
    app.run(`${change}; renderCurrent()`);
    assert.notEqual(app.element('transcript').children[0],previous,change);
  }
  app.run('scrubAccountWorkspace()');
  assert.equal(app.run('transcriptRenderState.rows.size'),0);
  assert.doesNotMatch(JSON.stringify(app.element('transcript').children),/받아쓴 원문|후보정본/);
});

test('chronological segment merges skip sorting while boundary insertion retains a stable fallback', () => {
  const app = setup(async () => response({}));
  app.run(`
    current={segments:[{id:'one',start:1,end:2,text:'첫 문장'}]};
    current.segments.sort=() => { throw new Error('chronological append must not sort'); };
    mergeChunkSegments(current,{segments:[{id:'one',start:1,end:2,text:'첫 문장'},{id:'three',start:3,end:4,text:'셋째 문장'}]});
  `);
  assert.equal(app.run('current.segments.length'),2);
  app.run(`
    delete current.segments.sort;
    mergeChunkSegments(current,{segments:[{id:'two',start:2,end:3,text:'경계 문장'},{id:'three',start:3,end:4,text:'셋째 문장'}]});
  `);
  assert.equal(app.run('current.segments.map(segment => segment.id).join(",")'),'one,two,three');
});

test('confirmed text is visible before slow local acknowledgement and remains visible after cleanup retries fail', async () => {
  let uploads = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      asr_provider:'qwen',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    uploads += 1;
    return response({segments:[{id:'confirmed',start:0,end:7,text:'이미 서버에 저장된 문장'}],
      recording_available:true,recording_finalized:false});
  });
  await app.run('startRecording()');
  const localAck = deferred(), queue = app.run('liveQueue');
  let cleanupAttempts = 0;
  queue.ackChunk = async () => { cleanupAttempts += 1; await localAck.promise; throw new Error('temporary storage failure'); };
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  assert.equal(cleanupAttempts,1);
  assert.equal(app.element('transcript').children[0].children[1].textContent,'이미 서버에 저장된 문장');
  assert.equal(app.run('pending.length'),1,'cleanup must retain the durable chunk until its ACK path completes');
  assert.equal(queue.chunks.size,1);
  localAck.resolve();
  await tick(); await tick();
  for (const delay of [500,1000,2000]) await app.runTimeout(delay);
  assert.equal(cleanupAttempts,4);
  assert.equal(app.run('pending.length'),0,'an acknowledged server result must not be uploaded again for a local cleanup failure');
  assert.equal(queue.chunks.size,1,'failed local deletion retains the recoverable WAV');
  assert.equal(uploads,1);
  assert.equal(app.run('recording'),true);
  assert.equal(app.element('transcript').children[0].children[1].textContent,'이미 서버에 저장된 문장');
});

for (const scenario of ['account','token','origin','provider']) {
  test(`a late successful chunk response cannot cross a changed ${scenario} boundary`, async () => {
    const late = deferred();
    const app = setup(async (url,options = {}) => url.endsWith('/lectures')
      ? response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',asr_provider:'qwen',
        created_at:'2026-01-01T00:00:00Z',segments:[]},201) : late.promise);
    await app.run('startRecording()');
    app.microphone().callbacks.onChunk(chunk(0));
    await tick(); await tick();
    const queue = app.run('liveQueue');
    if (scenario === 'account') app.run("user='user-beta'; token='beta-token'");
    if (scenario === 'token') app.run("token='replacement-token'");
    if (scenario === 'origin') app.run("apiUrl='https://replacement-tunnel.trycloudflare.com'");
    if (scenario === 'provider') app.run("current.asr_provider='clova'");
    late.resolve(response({segments:[{id:'late-private',start:0,end:2,text:'늦은 이전 응답'}],
      recording_available:true,recording_finalized:true}));
    await tick(); await tick();
    assert.equal(app.run('current.segments.length'),0);
    assert.doesNotMatch(JSON.stringify(app.element('transcript').children),/늦은 이전 응답/);
    assert.equal(queue.chunks.size,1);
    assert.equal(app.run('pending[0].blocked'),true);
    assert.notEqual(app.run('current.recording_finalized'),true);
  });
}

test('malformed success payloads preserve the WAV and cannot finalize or partially display a lecture', async () => {
  const app = setup(async (url,options = {}) => url.endsWith('/lectures')
    ? response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',asr_provider:'qwen',
      created_at:'2026-01-01T00:00:00Z',segments:[]},201)
    : response({segments:[{id:'valid',start:0,end:1,text:'부분 저장 금지'},{id:'invalid',start:2,end:1,text:'잘못된 범위'}],
      recording_available:true,recording_finalized:true}));
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  assert.equal(app.run('current.segments.length'),0);
  assert.equal(app.run('pending[0].blocked'),true);
  assert.equal(app.run('liveQueue.chunks.size'),1);
  assert.equal(app.run('recording'),true);
  assert.notEqual(app.run('current.recording_finalized'),true);
  assert.doesNotMatch(JSON.stringify(app.element('transcript').children),/부분 저장 금지/);
});

test('AI correction stays raw by default, polls to completion, and exports only the selected version', async () => {
  const requests = [];
  let reads = 0;
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/summary')) {
      assert.equal(options.method || 'GET','GET');
      return response({configured:false,model:'solar-pro4',summary:null});
    }
    assert.ok(url.endsWith('/correction'),'only the correction endpoint advances this fixture');
    requests.push({url,method:options.method || 'GET'});
    if (options.method === 'POST') {
      return response({lecture_id:'lesson',status:'queued',raw_revision:'a'.repeat(64)});
    }
    reads += 1;
    if (reads === 1) return response({lecture_id:'lesson',status:'processing',raw_revision:'a'.repeat(64)});
    return response({
      lecture_id:'lesson',status:'completed',raw_revision:'a'.repeat(64),corrected_text:'교정된 전체 문장',
      corrected_segments:[{id:'s1',start:0,end:4,text:'교정된 첫 문장입니다.'},{id:'s2',start:4,end:8,text:'교정된 둘째 문장입니다.'}],
      uncertain_terms:['전문용어'],
    });
  });
  app.run(`
    current={id:'lesson',title:'한국어 수업',language:'ko',created_at:'2026-01-01T16:00:00Z',
      recording_available:true,recording_finalized:true,
      segments:[{id:'s1',start:0,end:4,text:'원문 첫 문장'},{id:'s2',start:4,end:8,text:'원문 둘째 문장'}]};
    renderCurrent();
  `);
  assert.equal(app.element('correction-panel').hidden, false);
  assert.equal(app.element('correct-transcript').disabled, false);
  assert.equal(app.element('transcript-raw')['aria-pressed'], 'true');
  assert.equal(app.element('transcript-corrected').disabled, true);

  app.element('correct-transcript').onclick();
  await tick(); await tick();
  assert.deepEqual(requests.map(item => item.method), ['POST']);
  assert.equal(app.run('correction.status'), 'queued');
  assert.equal(app.run('correctionView'), 'raw');
  assert.match(app.element('correction-state').textContent, /대기/);
  assert.equal(app.element('transcript').children[0].children[1].textContent, '원문 첫 문장');

  await app.runTimeout(2500);
  assert.equal(app.run('correction.status'), 'processing');
  assert.equal(app.run('correctionView'), 'raw');
  await app.runTimeout(2500);
  assert.equal(app.run('correction.status'), 'completed');
  assert.equal(app.run('correctionView'), 'raw', 'a model result must never replace the raw view automatically');
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 2500));
  assert.match(app.element('notice').textContent, /후보정본을 만들었어요/);
  assert.equal(app.element('transcript-corrected').disabled, false);
  assert.match(app.element('correction-detail').textContent, /확인이 필요한 표현: 전문용어/);
  assert.equal(app.element('transcript').children[0].children[1].textContent, '원문 첫 문장');

  app.element('transcript-corrected').onclick();
  assert.equal(app.run('correctionView'), 'corrected');
  assert.equal(app.element('transcript-corrected')['aria-pressed'], 'true');
  assert.equal(app.element('transcript').children[0].children[1].textContent, '교정된 첫 문장입니다.');
  assert.equal(app.element('segment-count').textContent, 2);

  app.element('export-format').value = 'markdown';
  app.element('download').onclick();
  const correctedLink = app.created('a');
  const correctedMarkdown = await app.objectUrlBlob(correctedLink.href).text();
  assert.equal(correctedLink.download, '한국어 수업_AI후보정.md');
  assert.match(correctedMarkdown, /버전: AI 후보정본/);
  assert.match(correctedMarkdown, /교정된 첫 문장입니다/);
  assert.doesNotMatch(correctedMarkdown, /원문 첫 문장/);

  app.element('transcript-raw').onclick();
  app.element('export-format').value = 'text';
  app.element('download').onclick();
  const rawLink = app.created('a');
  const rawText = await app.objectUrlBlob(rawLink.href).text();
  assert.equal(rawLink.download, '한국어 수업.txt');
  assert.match(rawText, /원문 첫 문장/);
  assert.doesNotMatch(rawText, /교정된 첫 문장입니다/);
});

test('AI correction is blocked until the lecture is finalized and reports exhausted credits without hiding raw text', async () => {
  let requests = 0;
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/summary')) {
      assert.equal(options.method || 'GET','GET');
      return response({configured:false,model:'solar-pro4',summary:null});
    }
    assert.ok(url.endsWith('/correction'),'only the correction endpoint consumes this fixture credit');
    requests += 1;
    return response({detail:'사용 가능한 크레딧이 부족합니다.',error_code:'credit_exhausted'},402);
  });
  app.run(`
    current={id:'unfinished',title:'진행 중',created_at:'2026-01-01T00:00:00Z',recording_finalized:false,
      segments:[{id:'raw',start:0,text:'보존할 원문'}]}; renderCurrent();
  `);
  assert.equal(app.element('correct-transcript').disabled, true);
  assert.equal(app.element('correction-panel')['data-state'], 'unfinished');
  app.element('correct-transcript').onclick();
  await tick();
  assert.equal(requests, 0);

  app.run('current.recording_finalized=true; renderCurrent()');
  assert.equal(app.element('correct-transcript').disabled, false);
  app.element('correct-transcript').onclick();
  await tick(); await tick();
  assert.equal(requests, 1);
  assert.equal(app.element('correction-panel')['data-state'], 'credit-exhausted');
  assert.match(app.element('correction-state').textContent, /크레딧/);
  assert.equal(app.run('correctionView'), 'raw');
  assert.equal(app.element('transcript').children[0].children[1].textContent, '보존할 원문');
});

test('a live lecture schedules exactly one correction after its final chunk without stopping capture', async () => {
  const events = [];
  let correctionPosts = 0;
  let liveLectureId = '';
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') {
      liveLectureId = options.headers.get('X-Lecture-Id');
      return response({id:liveLectureId,title:'현재 수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (liveLectureId && url.endsWith(`/lectures/${liveLectureId}/chunks`)) {
      const final = options.headers.get('X-Final-Chunk') === 'true';
      const start = Number(options.headers.get('X-Start-Seconds'));
      events.push(final ? 'final-chunk' : 'chunk');
      return response({
        segments:[{id:`live-${start}`,start,end:start + 1,text:`문장 ${start}`}],
        recording_available:true,recording_finalized:final,
      });
    }
    if (liveLectureId && url.endsWith(`/lectures/${liveLectureId}/correction`) && options.method === 'POST') {
      correctionPosts += 1; events.push('correction');
      return response({lecture_id:liveLectureId,status:'queued',raw_revision:'a'.repeat(64)});
    }
    return response({});
  });

  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  assert.equal(app.run('current.segments.length'),1);
  assert.equal(app.element('correct-transcript').disabled,false);

  app.element('correct-transcript').onclick();
  await tick(); await tick();
  assert.equal(correctionPosts,0,'a changing transcript must not be sent before finalization');
  assert.equal(app.microphone().stopCalls,undefined);
  assert.equal(app.run('recording'),true);
  assert.equal(app.run(`scheduledCorrections.get(${JSON.stringify(liveLectureId)}).status`),'scheduled');
  assert.equal(app.element('correction-panel')['data-state'],'scheduled');
  assert.match(app.element('correction-detail').textContent,/계속됩니다/);

  app.microphone().tail = chunk(6,3,2,true);
  await app.run('stopRecording()');
  await tick(); await tick(); await tick();
  assert.equal(app.microphone().stopCalls,1);
  assert.equal(correctionPosts,1);
  assert.ok(events.indexOf('final-chunk') < events.indexOf('correction'));
  assert.equal(app.run('scheduledCorrections.size'),0);
  assert.equal(app.run('correction.status'),'queued');
  assert.equal(app.run('current.recording_finalized'),true);
  assert.equal(app.run('current.segments.length'),2,'the selected finalized transcript stays available');
  assert.equal(app.run('captureSession'),null,'a finalized capture must not retain a second long transcript');
  assert.equal(app.run("Object.hasOwn(lectures[0],'segments')"),false,
    'the history summary must release finalized live segment arrays');
});

test('an auth-expired scheduled correction is rebound only to the same owner and resumes once', async () => {
  let oldCorrectionPosts = 0, newCorrectionPosts = 0, loginPosts = 0;
  const finalized = {
    id:'reauth-correction',title:'인증 복구 수업',language:'ko',created_at:'2026-01-01T00:00:00Z',
    segments:[{id:'raw',start:0,end:2,text:'보존할 원문'}],recording_available:true,recording_finalized:true,
  };
  const app = setup((url, options = {}) => {
    const authorization = options.headers?.get?.('Authorization');
    if (url.endsWith('/lectures/reauth-correction/correction') && options.method === 'POST') {
      if (authorization === 'Bearer old-token') {
        oldCorrectionPosts += 1;
        return response({detail:'expired'},401);
      }
      newCorrectionPosts += 1;
      return response({lecture_id:'reauth-correction',status:'queued'});
    }
    if (url.endsWith('/auth/login')) {
      loginPosts += 1;
      return response({token:'renewed-token',user:{username:'user-alpha',is_admin:false}});
    }
    if (url.endsWith('/lectures')) return response([finalized]);
    if (url.endsWith('/imports')) return response([]);
    if (url.endsWith('/status')) return response({model_state:'ready'});
    return response({});
  });
  app.run(`
    current=${JSON.stringify(finalized)}; lectures=[current]; captureSession={id:'reauth-capture',lecture:current};
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,status:'scheduled'});
    submitScheduledCorrection(current.id);
  `);
  await tick(); await tick();
  assert.equal(oldCorrectionPosts,1);
  assert.equal(app.run("scheduledCorrections.get('reauth-correction').status"),'scheduled');
  assert.equal(app.run("scheduledCorrections.get('reauth-correction').sessionToken"),'');
  assert.equal(app.run("scheduledCorrections.get('reauth-correction').server"),'');
  assert.equal(app.run('user'),'user-alpha');

  app.element('username').value = 'user-beta';
  app.element('password').value = 'different-account';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(loginPosts,0,'another account must not claim a retained correction reservation');
  assert.match(app.element('auth-error').textContent,/이전 계정/);

  app.element('username').value = 'user-alpha';
  app.element('password').value = 'same-account';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  for (let index = 0; index < 5; index += 1) await tick();
  assert.equal(loginPosts,1);
  assert.equal(newCorrectionPosts,1);
  assert.equal(app.run('scheduledCorrections.size'),0);
  assert.equal(app.run('token'),'renewed-token');
});

test('a tunnel replacement between live chunks retains the capture owner and final correction', async () => {
  const oldUrl = 'https://old-live.trycloudflare.com';
  const newUrl = 'https://new-live.trycloudflare.com';
  const events = [];
  let loginPosts = 0, correctionPosts = 0, movingLiveId = '';
  const app = setup((url, options = {}) => {
    if (url === `${oldUrl}/lectures` && options.method === 'POST') {
      movingLiveId = options.headers.get('X-Lecture-Id');
      return response({id:movingLiveId,title:'현재 수업',language:'ko',asr_provider:'qwen',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    }
    if (movingLiveId && url === `${oldUrl}/lectures/${movingLiveId}/chunks`) {
      return response({segments:[{id:'first',start:0,end:2,text:'첫 문장'}],recording_available:true,recording_finalized:false});
    }
    if (url === `${newUrl}/auth/login`) {
      loginPosts += 1;
      return response({token:'new-live-token',user:{username:'user-alpha',is_admin:false}});
    }
    if (url === `${newUrl}/lectures` && !options.method) {
      return response([{id:movingLiveId,title:'현재 수업',language:'ko',asr_provider:'qwen',created_at:'2026-01-01T00:00:00Z',
        segments:[{id:'first',start:0,end:2,text:'첫 문장'}],recording_available:true,recording_finalized:false}]);
    }
    if (url === `${newUrl}/imports`) return response([]);
    if (url === `${newUrl}/status`) return response({model_state:'ready'});
    if (movingLiveId && url === `${newUrl}/lectures/${movingLiveId}/chunks`) {
      assert.equal(options.headers.get('Authorization'),'Bearer new-live-token');
      assert.equal(options.headers.get('X-Final-Chunk'),'true');
      events.push('final');
      return response({segments:[{id:'tail',start:6,end:9,text:'마지막 문장'}],recording_available:true,recording_finalized:true});
    }
    if (movingLiveId && url === `${newUrl}/lectures/${movingLiveId}/correction` && options.method === 'POST') {
      correctionPosts += 1; events.push('correction');
      return response({lecture_id:movingLiveId,status:'queued'});
    }
    throw new Error(`unexpected request: ${url}`);
  });
  app.run(`apiUrl=${JSON.stringify(oldUrl)}; verifiedApiUrl=apiUrl; setServer(apiUrl);`);
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  assert.equal(app.run('pending.length'),0,'the tunnel changes between ordinary chunks');
  app.element('correct-transcript').onclick();
  app.microphone().tail = chunk(6,3,2,true);

  app.run(`installVerifiedServer(${JSON.stringify(newUrl)},'새 연결 확인',{expiresAt:Date.now()+86400000})`);
  await tick(); await tick();
  assert.equal(app.run('apiUrl'),newUrl);
  assert.equal(app.run('token'),'');
  assert.equal(app.run('user'),'user-alpha');
  assert.equal(app.run('pending.length'),0,'changing the tunnel does not stop capture or synthesize its final tail');
  assert.equal(app.run('recording'),true);
  assert.equal(app.microphone().stopCalls,undefined);
  assert.equal(app.run(`scheduledCorrections.get(${JSON.stringify(movingLiveId)}).sessionToken`),'');
  assert.equal(app.run(`scheduledCorrections.get(${JSON.stringify(movingLiveId)}).server`),'');
  assert.equal(app.element('username').value,'user-alpha');

  app.element('username').value = 'user-beta';
  app.element('password').value = 'different-account';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(loginPosts,0);
  assert.match(app.element('auth-error').textContent,/이전 계정/);

  app.element('username').value = 'user-alpha';
  app.element('password').value = 'same-account';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  await app.run('stopRecording()');
  for (let index = 0; index < 6; index += 1) await tick();
  assert.equal(loginPosts,1);
  assert.equal(app.run('pending.length'),0);
  assert.equal(correctionPosts,1);
  assert.deepEqual(events,['final','correction']);
  assert.equal(app.run('scheduledCorrections.size'),0);
});

test('a finalized past lecture can be corrected while live chunks stay attached to the active lecture', async () => {
  const chunkUrls = [];
  let pastCorrectionPosts = 0, activeLiveId = '';
  const past = {
    id:'past-finalized',title:'지난 수업',language:'ko',created_at:'2026-01-01T00:00:00Z',
    recording_available:true,recording_finalized:true,
    segments:[{id:'past-raw',start:0,end:2,text:'지난 수업 원문'}],
  };
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') {
      activeLiveId = options.headers.get('X-Lecture-Id');
      return response({id:activeLiveId,title:'현재 수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (activeLiveId && url.endsWith(`/lectures/${activeLiveId}/chunks`)) {
      chunkUrls.push(url);
      const start = Number(options.headers.get('X-Start-Seconds'));
      return response({
        segments:[{id:`active-${start}`,start,end:start + 1,text:`현재 문장 ${start}`}],
        recording_available:true,recording_finalized:options.headers.get('X-Final-Chunk') === 'true',
      });
    }
    if (url.endsWith('/lectures/past-finalized/correction')) {
      if (options.method === 'POST') {
        pastCorrectionPosts += 1;
        return response({lecture_id:'past-finalized',status:'queued',raw_revision:'b'.repeat(64)});
      }
      return response({detail:'이 수업에는 후보정 작업이 없습니다.'},404);
    }
    if (url.endsWith('/lectures/past-finalized')) return response(past);
    return response({});
  });

  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  app.run(`lectures.push(${JSON.stringify(past)}); renderHistory(); updateControls()`);
  const buttons = app.element('lecture-list').querySelectorAll('button');
  const activeButton = buttons.find(button => button.children[0]?.textContent === '현재 수업');
  const pastButton = buttons.find(button => button.children[0]?.textContent === '지난 수업');
  assert.equal(app.element('lecture-date').disabled,false);
  assert.equal(activeButton.disabled,false);
  assert.equal(pastButton.disabled,false);
  assert.match(activeButton.children[2].textContent,/현재 녹음/);

  await pastButton.onclick();
  await tick(); await tick();
  assert.equal(app.run('current.id'),'past-finalized');
  assert.equal(app.element('live-capture-banner').hidden,false);
  assert.equal(app.element('return-live-capture').disabled,false);
  for (const id of ['new-note','logout','recording-file','delete-lecture']) {
    assert.equal(app.element(id).disabled,true,`${id} must remain locked during background capture`);
  }
  assert.equal(app.element('download').disabled,false);assert.equal(app.element('recording-download').disabled,false);
  assert.equal(app.element('correct-transcript').disabled,false);
  app.element('correct-transcript').onclick();
  await tick(); await tick();
  assert.equal(pastCorrectionPosts,1);
  assert.equal(app.run('recording'),true);
  assert.equal(app.microphone().stopCalls,undefined);
  const pastTranscriptNode = app.element('transcript').children[0];

  app.microphone().callbacks.onChunk(chunk(6,10,2));
  await tick(); await tick();
  assert.ok(chunkUrls.length >= 2);
  assert.ok(chunkUrls.every(url => url.endsWith(`/lectures/${activeLiveId}/chunks`)));
  assert.equal(app.run('current.id'),'past-finalized');
  assert.equal(app.run("captureSession.lecture.segments.length"),2);
  assert.equal(app.element('transcript').children[0],pastTranscriptNode,
    'background live chunks must not rebuild the past transcript being read');

  app.element('return-live-capture').onclick();
  assert.equal(app.run('current.id'),activeLiveId);
  assert.equal(app.run('current.segments.length'),2,'returning must retain chunks received while viewing history');
  assert.equal(app.element('live-capture-banner').hidden,true);
  assert.equal(app.element('main-content').focused,true);
  app.microphone().tail = chunk(13,3,3,true);
  await app.run('stopRecording()');
  await tick(); await tick();
});

test('paused capture supports both current-lecture scheduling and past-lecture correction', async () => {
  let activeCorrectionPosts = 0, pastCorrectionPosts = 0, pausedLiveId = '';
  const past = {
    id:'paused-past',title:'지난 기록',language:'ko',created_at:'2026-01-01T00:00:00Z',recording_finalized:true,
    segments:[{id:'old',start:0,end:1,text:'지난 원문'}],
  };
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') {
      pausedLiveId = options.headers.get('X-Lecture-Id');
      return response({id:pausedLiveId,title:'일시정지 수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (pausedLiveId && url.endsWith(`/lectures/${pausedLiveId}/chunks`)) {
      const start = Number(options.headers.get('X-Start-Seconds'));
      return response({segments:[{id:`p-${start}`,start,end:start + 1,text:'현재 원문'}],recording_available:true,
        recording_finalized:options.headers.get('X-Final-Chunk') === 'true'});
    }
    if (pausedLiveId && url.endsWith(`/lectures/${pausedLiveId}/correction`) && options.method === 'POST') {
      activeCorrectionPosts += 1; return response({lecture_id:pausedLiveId,status:'queued'});
    }
    if (url.endsWith('/lectures/paused-past/correction')) {
      if (options.method === 'POST') {
        pastCorrectionPosts += 1; return response({lecture_id:'paused-past',status:'queued'});
      }
      return response({detail:'not found'},404);
    }
    if (url.endsWith('/lectures/paused-past')) return response(past);
    return response({});
  });

  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  app.microphone().pauseTail = chunk(6,2,2,false);
  await app.run('pauseRecording()');
  await tick(); await tick();
  assert.equal(app.run('paused'),true);
  assert.equal(app.element('correct-transcript').disabled,false);
  app.element('correct-transcript').onclick();
  assert.equal(activeCorrectionPosts,0);
  assert.equal(app.run(`scheduledCorrections.has(${JSON.stringify(pausedLiveId)})`),true);
  assert.equal(app.microphone().stopCalls,undefined);

  app.run(`lectures.push(${JSON.stringify(past)}); renderHistory(); updateControls()`);
  const pastButton = app.element('lecture-list').querySelectorAll('button')
    .find(button => button.children[0]?.textContent === '지난 기록');
  assert.equal(pastButton.disabled,false);
  await pastButton.onclick(); await tick(); await tick();
  assert.equal(app.run('paused'),true);
  assert.match(app.element('live-capture-title').textContent,/일시정지/);
  app.element('correct-transcript').onclick();
  await tick(); await tick();
  assert.equal(pastCorrectionPosts,1);
  assert.equal(activeCorrectionPosts,0);
  assert.equal(app.microphone().stopCalls,undefined);

  app.element('return-live-capture').onclick();
  assert.equal(app.run('current.id'),pausedLiveId);
  assert.equal(app.element('correction-panel')['data-state'],'scheduled');
  app.microphone().tail = chunk(6,2,2,true);
  await app.run('stopRecording()');
  await tick(); await tick(); await tick();
  assert.equal(activeCorrectionPosts,1);
  assert.equal(app.microphone().stopCalls,1);
});

test('late AI correction responses and polling timers cannot cross lecture boundaries', async () => {
  const late = deferred();
  const app = setup(() => late.promise);
  app.run(`
    current={id:'old',title:'이전 수업',created_at:'2026-01-01T00:00:00Z',recording_finalized:true,
      segments:[{id:'old-raw',start:0,text:'이전 원문'}]}; renderCurrent();
  `);
  const loading = app.run("loadCorrection('old')");
  await tick();
  app.run(`
    current={id:'new',title:'새 수업',created_at:'2026-01-02T00:00:00Z',recording_finalized:true,
      segments:[{id:'new-raw',start:0,text:'새 원문'}]}; renderCurrent();
  `);
  late.resolve(response({status:'completed',corrected_text:'노출되면 안 되는 이전 후보정'}));
  await loading;
  assert.equal(app.run('current.id'), 'new');
  assert.equal(app.run('correction'), null);
  assert.equal(app.run('correctionView'), 'raw');
  assert.equal(app.element('transcript').children[0].children[1].textContent, '새 원문');

  app.run(`
    correction={status:'processing',corrected_text:'',corrected_segments:[]};
    scheduleCorrectionPoll('new');
  `);
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 2500));
  app.run('resetNewNote()');
  assert.equal(app.run('correctionPollTimer'), null);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 2500));
});

test('a late automatically scheduled correction cannot alter another account', async () => {
  const late = deferred();
  const app = setup((url, options = {}) => url.endsWith('/lectures/old-auto/correction') && options.method === 'POST'
    ? late.promise : response({}));
  app.run(`
    current={id:'old-auto',title:'이전 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'old',start:0,end:1,text:'이전 원문'}],recording_available:true,recording_finalized:false};
    lectures=[current]; captureSession={id:'old-capture',lecture:current};
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,status:'scheduled'});
    applyRecordingFlags(current.id,{recording_available:true,recording_finalized:true});
  `);
  await tick();
  assert.equal(app.run("scheduledCorrections.get('old-auto').status"),'starting');
  app.run(`
    showLogin(); const previousUser=user; token='next-token'; user='next-user';
    if (previousUser !== user) { scheduledCorrections.clear(); scrubAccountWorkspace(); }
    current={id:'next-lesson',title:'다음 수업',created_at:'2026-01-02T00:00:00Z',recording_finalized:true,
      segments:[{id:'next',start:0,end:1,text:'다음 원문'}]}; lectures=[current]; renderCurrent();
  `);
  late.resolve(response({lecture_id:'old-auto',status:'completed',corrected_text:'노출되면 안 되는 이전 결과',
    corrected_segments:[{id:'old',start:0,end:1,text:'노출되면 안 됨'}]}));
  await tick(); await tick();
  assert.equal(app.run('scheduledCorrections.size'),0);
  assert.equal(app.run('current.id'),'next-lesson');
  assert.equal(app.run('correction'),null);
  assert.equal(app.element('transcript').children[0].children[1].textContent,'다음 원문');
});

test('an in-flight reserved correction keeps logout and navigation protected until acknowledged', async () => {
  const correctionRequest = deferred();
  const app = setup((url, options = {}) =>
    url.endsWith('/lectures/protected-auto/correction') && options.method === 'POST'
      ? correctionRequest.promise : response({}));
  app.run(`
    current={id:'protected-auto',title:'보호할 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'raw',start:0,end:1,text:'원문'}],recording_available:true,recording_finalized:false};
    lectures=[current]; captureSession={id:'protected-capture',lecture:current};
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,finalized:false,status:'scheduled'});
    applyRecordingFlags(current.id,{recording_available:true,recording_finalized:true});
    updateControls();
  `);
  await tick();
  assert.equal(app.run('captureSession'),null);
  assert.equal(app.run("scheduledCorrections.get('protected-auto').status"),'starting');
  assert.equal(app.run('isBusy()'),true);
  assert.equal(app.element('logout').disabled,true);

  correctionRequest.resolve(response({lecture_id:'protected-auto',status:'queued'}));
  await tick(); await tick();
  assert.equal(app.run('scheduledCorrections.size'),0);
  assert.equal(app.run('isBusy()'),false);
  assert.equal(app.element('logout').disabled,false);
});

function adminOverviewFixture(overrides = {}) {
  return {
    generated_at:'2026-09-04T03:00:00Z',
    access:{enabled:true,updated_at:'2026-09-04T02:59:00Z'},
    server:{uptime_seconds:3720,model_state:'ready',engine:'qwen3-asr',model:'Qwen3-ASR-1.7B',device:'cuda'},
    resources:{
      memory:{total_bytes:32 * 1024 ** 3,used_bytes:12 * 1024 ** 3,available_bytes:20 * 1024 ** 3,process_rss_bytes:2 * 1024 ** 3},
      load:{one:0.5,five:0.4,fifteen:0.3,cpu_count:8},
      disk:{total_bytes:500 * 1024 ** 3,used_bytes:125 * 1024 ** 3,free_bytes:375 * 1024 ** 3},
      gpu:{available:true,total_bytes:16 * 1024 ** 3,used_bytes:5 * 1024 ** 3,free_bytes:11 * 1024 ** 3,
        process_allocated_bytes:3 * 1024 ** 3,process_reserved_bytes:4 * 1024 ** 3},
    },
    queues:{transcription:1,imports:2,corrections:3},
    tunnel:{state:'online',restart_available:true},
    accounts:[
      {account_id:'opaque-self',label:'user-alpha',is_self:true,activated:true,online:true,activity:'recording',last_activity_at:'2026-09-04T02:59:58Z',session_count:1,jobs:{transcription:1,imports:0,corrections:0}},
      {account_id:'opaque-peer',label:'member-beta',is_self:false,activated:false,online:false,activity:'offline',last_activity_at:null,session_count:2,jobs:{transcription:0,imports:0,corrections:0}},
    ],
    recent_audit:[
      {timestamp:'2026-09-04T02:58:00Z',action:'access_changed',result:'success',target:'service'},
      {timestamp:'2026-09-04T02:57:00Z',action:'sessions_revoked',result:'success',target:'member-beta'},
    ],
    ...overrides,
  };
}

test('admin discovery stays hidden after 403 and ignores an overview from an old session', async () => {
  const denied = setup(async () => response({detail:'관리자 권한이 필요합니다.'},403));
  await denied.run('loadAdminOverview({probe:true})');
  assert.equal(denied.run('adminAuthorized'), false);
  assert.equal(denied.element('admin-open').hidden, true);
  assert.equal(denied.element('admin-dialog').open, false);

  const late = deferred();
  const stale = setup(() => late.promise);
  const loading = stale.run('loadAdminOverview({probe:true})');
  await tick();
  stale.run('showLogin()');
  late.resolve(response(adminOverviewFixture()));
  await loading;
  assert.equal(stale.run('adminOverview'), null);
  assert.equal(stale.run('adminAuthorized'), false);
  assert.equal(stale.element('admin-open').hidden, true);
});

test('a transient initial admin probe retries without exposing the control early', async () => {
  let attempts = 0;
  const app = setup(async url => {
    if (url.endsWith('/admin/overview') && ++attempts === 1) return response({detail:'잠시 실패'},503);
    return response(adminOverviewFixture());
  });
  await app.run('loadAdminOverview({probe:true})');
  assert.equal(app.element('admin-open').hidden,true);
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 10000));

  await app.runTimeout(10000);
  assert.equal(attempts,2);
  assert.equal(app.element('admin-open').hidden,false);
});

test('admin response data and account action closures are scrubbed when the identity resets', async () => {
  const app = setup(async () => response(adminOverviewFixture()));
  await app.run('loadAdminOverview({probe:true})');
  app.element('admin-accounts').children[1].children[2].onclick();
  assert.match(app.element('admin-confirm-description').textContent,/member-beta/);
  assert.match(app.element('admin-server-detail').textContent,/Qwen3-ASR-1\.7B/);

  app.run('resetAdminState()');

  assert.equal(app.element('admin-open').hidden,true);
  assert.equal(app.element('admin-accounts').children.length,0);
  assert.equal(app.element('admin-audit').children.length,0);
  assert.doesNotMatch(app.element('admin-confirm-description').textContent,/member-beta/);
  assert.doesNotMatch(app.element('admin-server-detail').textContent,/Qwen3-ASR-1\.7B/);
  assert.equal(app.element('admin-tunnel-restart').disabled,true);
  assert.equal(app.run('adminOverview'),null);
  assert.equal(app.run('adminConfirmation'),null);
});

test('admin work totals include summary translation and study notes without revealing lesson content', async () => {
  const overview = adminOverviewFixture();
  overview.queues.summaries = 2;
  overview.queues.translations = 3;
  overview.queues.study_notes = 4;
  overview.accounts[1].jobs = {transcription:0,imports:0,corrections:0,summaries:1,translations:1,study_notes:1};
  const app = setup(async () => response(overview));
  await app.run('loadAdminOverview({probe:true})');
  assert.equal(app.element('admin-summary-queue').textContent, '2');
  assert.equal(app.element('admin-translation-queue').textContent, '3');
  assert.equal(app.element('admin-study-note-queue').textContent, '4');
  assert.match(app.element('admin-accounts').children[1].children[1].children[1].textContent, /진행 작업 3개/);
  app.run('adminOverview={...adminOverview,queues:{}}; renderAdminOverview()');
  assert.equal(app.element('admin-summary-queue').textContent, '0 · 0');
  assert.equal(app.element('admin-translation-queue').textContent, '0 · 0');
  assert.equal(app.element('admin-study-note-queue').textContent, '서버 미지원');
});

test('admin overview renders safe operational metadata, refreshes while open, and never offers self-revocation', async () => {
  const overview = adminOverviewFixture();
  const app = setup(async () => response(overview));
  await app.run('loadAdminOverview({probe:true})');

  assert.equal(app.element('admin-open').hidden, false);
  assert.equal(app.element('admin-server-state').textContent, '음성 모델 준비됨');
  assert.match(app.element('admin-server-detail').textContent, /Qwen3-ASR-1\.7B/);
  assert.match(app.element('admin-server-detail').textContent, /시스템 부하 0\.50/);
  assert.match(app.element('admin-gpu-detail').textContent, /서버 프로세스 할당 3\.00 GiB/);
  assert.match(app.element('admin-ram-detail').textContent, /서버 프로세스 2\.00 GiB/);
  assert.equal(app.element('admin-transcription-queue').textContent, '1');
  assert.equal(app.element('admin-import-queue').textContent, '2');
  assert.equal(app.element('admin-correction-queue').textContent, '3');
  assert.equal(app.element('admin-tunnel-state').textContent, '프로세스 실행 중');
  assert.match(app.element('admin-tunnel-detail').textContent, /외부 HTTPS 접속 가능 여부는 별도로 확인/);

  const accountRows = app.element('admin-accounts').children;
  assert.equal(accountRows.length, 2);
  assert.equal(accountRows[0].children[0].children[0].textContent, 'user-alpha');
  assert.equal(accountRows[0].children[1].children[0].textContent, '녹음 중');
  assert.equal(accountRows[0].children[2].tagName, 'SPAN');
  assert.equal(accountRows[0].children[2].textContent, '현재 계정');
  assert.equal(accountRows[1].children[0].children[1].textContent, '초대·비밀번호 설정 대기');
  assert.equal(accountRows[1].children[2].tagName, 'BUTTON');
  assert.doesNotMatch(accountRows[1].children[2].textContent, /opaque-peer/);
  assert.match(app.element('admin-audit').children[0].children[0].children[1].textContent, /운영 접속/);

  app.element('admin-open').onclick();
  await tick(); await tick();
  assert.equal(app.element('admin-dialog').open, true);
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 10000));
  app.element('admin-close').onclick();
  assert.equal(app.element('admin-dialog').open, false);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 10000));

  app.run("adminOverview={...adminOverview,tunnel:{state:'starting',restart_available:true}}; renderAdminOverview() ");
  assert.equal(app.element('admin-tunnel-state').textContent, '재연결 중');
  assert.equal(app.element('admin-tunnel-restart').disabled, true);

  app.run("adminOverview={...adminOverview,resources:{...adminOverview.resources,memory:null,disk:null}}; renderAdminOverview() ");
  assert.equal(app.element('admin-ram-value').textContent, '사용할 수 없음');
  assert.equal(app.element('admin-disk-value').textContent, '사용할 수 없음');
});

test('admin mutations require confirmation and send only the opaque account reference', async () => {
  const calls = [];
  const overview = adminOverviewFixture();
  const app = setup(async (url, options = {}) => {
    calls.push({url,method:options.method || 'GET',body:options.body});
    return response(url.endsWith('/admin/overview') ? overview : {status:'ok'});
  });
  await app.run('loadAdminOverview({probe:true})');
  const peerRevoke = app.element('admin-accounts').children[1].children[2];
  peerRevoke.onclick();
  assert.equal(app.element('admin-confirm-dialog').open, true);
  assert.match(app.element('admin-confirm-description').textContent, /member-beta/);
  assert.doesNotMatch(app.element('admin-confirm-description').textContent, /opaque-peer/);
  assert.equal(calls.filter(call => call.method === 'POST').length, 0);
  app.element('admin-confirm-accept').onclick();
  await tick(); await tick(); await tick();
  const revoke = calls.find(call => call.url.endsWith('/admin/sessions/revoke'));
  assert.ok(revoke);
  assert.deepEqual(JSON.parse(revoke.body),{account_id:'opaque-peer'});

  app.element('admin-access-toggle').onclick();
  assert.equal(app.element('admin-confirm-dialog').open, true);
  assert.match(app.element('admin-confirm-description').textContent, /모든 수업 데이터 요청이 일시 중지/);
  assert.equal(calls.filter(call => call.url.endsWith('/admin/access')).length, 0);
  app.element('admin-confirm-cancel').onclick();

  app.element('admin-tunnel-restart').onclick();
  assert.equal(app.element('admin-confirm-dialog').open, true);
  assert.match(app.element('admin-confirm-description').textContent, /외부 주소가 바뀌며/);
  assert.equal(calls.filter(call => call.url.endsWith('/admin/tunnel/restart')).length, 0);
});

test('presence heartbeat reports only activity and follows recording, away, and logout state', async () => {
  const bodies = [];
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/presence')) bodies.push(JSON.parse(options.body));
    return response({status:'ok'});
  });
  app.run('startPresence()');
  await app.runTimeout(0);
  assert.deepEqual(bodies.at(-1),{activity:'viewing'});

  app.run('recording=true; notePresenceStateChange()');
  await tick(); await tick();
  assert.deepEqual(bodies.at(-1),{activity:'recording'});
  app.document.hidden = true;
  await app.dispatchDocument('visibilitychange');
  assert.deepEqual(bodies.at(-1),{activity:'away'});
  await app.runInterval(15000);
  assert.deepEqual(bodies.at(-1),{activity:'away'});
  assert.ok(bodies.every(body => Object.keys(body).length === 1 && typeof body.activity === 'string'));

  app.run('recording=false; showLogin()');
  assert.equal(app.intervals.size, 0);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 5 * 60 * 1000));
});

test('tunnel recovery retries published config after five seconds and is cancelled by logout', async () => {
  const oldServer = 'https://old-tunnel.trycloudflare.com';
  const config = runtimeConfig({apiUrl:oldServer});
  const requests = [];
  const app = setup(async url => {
    requests.push(String(url));
    if (String(url).startsWith('./config.json?')) return response(config);
    if (String(url).endsWith('/health')) return response({status:'ok'});
    return response({});
  });
  app.run(`apiUrl='${oldServer}'; verifiedApiUrl=apiUrl; connectionState='connected';
    startTunnelRecovery({owner:user,sessionToken:token,server:apiUrl,requestGeneration,connectionGeneration});`);
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 5000));
  await app.runTimeout(5000);
  assert.ok(requests.some(url => url.startsWith('./config.json?')));
  assert.ok(requests.some(url => url.endsWith('/health')));
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 8000));
  app.run('showLogin()');
  assert.equal(app.run('tunnelRecoveryTimer'), null);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 8000));

  const exhausted = setup(async url => {
    if (String(url).startsWith('./config.json?')) return response(config);
    if (String(url).endsWith('/health')) return response({status:'ok'});
    return response({});
  });
  exhausted.run(`apiUrl='${oldServer}'; verifiedApiUrl=apiUrl; connectionState='connected';
    startTunnelRecovery({owner:user,sessionToken:token,server:apiUrl,requestGeneration,connectionGeneration});
    tunnelRecoveryDeadline=Date.now()-1;`);
  await exhausted.runTimeout(5000);
  assert.equal(exhausted.run('tunnelRecoveryTimer'), null);
  assert.match(exhausted.element('notice').textContent, /페이지를 새로고침하거나 연결 설정/);
});

test('recording download exchanges bearer auth for a same-origin native ticket link', async () => {
  const ticket = deferred();
  const ticketPath = `/recording-downloads/${'a'.repeat(43)}`;
  let request;
  const app = setup((url, options) => {
    request = {url,options}; return ticket.promise;
  });
  app.run(`current={id:'lesson-id',title:'한국사: 1교시',created_at:'2026-01-01T16:00:00Z',segments:[],recording_available:true,recording_finalized:true}; renderCurrent()`);
  assert.equal(app.element('recording-download').disabled, false);
  app.run('current.recording_finalized=false; updateControls()');
  assert.equal(app.element('recording-download').disabled, false, 'saved unfinished audio must be recoverable');
  assert.match(app.element('recording-download').textContent, /기기에 남은/);
  assert.equal(app.element('recording-finalize').disabled,false);
  app.run('current.recording_finalized=true; updateControls()');
  const downloading = app.run('downloadRecording()');
  assert.equal(app.run('recordingDownloadPending'), true);
  assert.equal(app.element('recording-download').disabled, true);
  assert.match(request.url, /\/lectures\/lesson-id\/recording-download-ticket$/);
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers.get('Authorization'), 'Bearer old-token');
  ticket.resolve(response({path:ticketPath}));
  await downloading;
  const link = app.created('a');
  assert.equal(link.href, `https://classroom.example${ticketPath}`);
  assert.equal(link.download, '한국사_ 1교시.wav');
  assert.equal(link.rel, 'noreferrer');
  assert.equal(link.referrerPolicy, 'no-referrer');
  assert.doesNotMatch(link.href, /old-token/);
  assert.equal(app.run('recordingDownloadPending'), false);
  for (const unsafe of [
    'https://attacker.example/private',
    `${ticketPath}?session=old-token`,
    `/recording-downloads/${'a'.repeat(31)}`,
    `/lectures/${'a'.repeat(43)}`,
  ]) {
    assert.throws(() => app.run(`nativeDownloadUrl(${JSON.stringify(unsafe)})`), /안전하지 않은/);
  }
});

test('recording archive states explain Drive progress without exposing a Drive locator', () => {
  const app = setup(async () => response({}));
  const lectureId = webcrypto.randomUUID();
  app.run(`current={id:${JSON.stringify(lectureId)},title:'수업',created_at:'2026-01-01T00:00:00Z',
    segments:[],recording_available:true,recording_finalized:true,recording_storage_state:'local_recording'};
    lectures=[{...current}]; updateControls()`);
  const labels = {
    local_recording:'이 서버에 녹음 저장됨',
    upload_queued:'Google Drive 저장 대기 · 이 서버에 임시 보관',
    uploading:'Google Drive로 녹음을 옮기는 중',
    retrying:'Google Drive 연결 대기 · 이 서버에 임시 보관',
    drive_cleanup_pending:'Google Drive 저장 확인 완료 · 서버 임시본 정리 중',
    drive_ready:'Google Drive에 녹음 저장됨',
    attention_required:'Google Drive 저장 확인 필요 · 서버에서 확인해 주세요',
  };
  for (const [state,label] of Object.entries(labels)) {
    app.run(`current.recording_storage_state=${JSON.stringify(state)}; updateControls()`);
    assert.equal(app.element('save-state').textContent,label,state);
  }
  app.run(`applyRecordingFlags(${JSON.stringify(lectureId)}, {
    recording_available:true,recording_finalized:true,recording_storage_state:'drive_ready',
    drive_file_id:'must-not-be-copied'
  }); updateControls()`);
  assert.equal(app.run('current.recording_storage_state'),'drive_ready');
  assert.equal(app.run('lectures[0].recording_storage_state'),'drive_ready');
  assert.equal(app.run("Object.hasOwn(current,'drive_file_id')"),false);
  assert.equal(app.element('save-state').textContent,'Google Drive에 녹음 저장됨');
});

test('unfinished WAV download never finalizes implicitly and separate explicit finalization precedes a server ticket', async () => {
  const lectureId = webcrypto.randomUUID();
  const ticketPath = `/recording-downloads/${'b'.repeat(43)}`;
  const requests = [];
  const app = setup(async (url, options = {}) => {
    requests.push({url,method:options.method,authorization:options.headers.get('Authorization')});
    if (url.endsWith('/recording-finalize')) {
      return response({recording_available:true,recording_finalized:true});
    }
    if (url.endsWith('/recording-download-ticket')) return response({path:ticketPath});
    return response({});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'복구 수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}]; renderCurrent(); renderHistory();
  `);
  assert.equal(app.element('recording-download').disabled, false);
  assert.match(app.element('recording-download').textContent, /기기에 남은/);
  await app.run('downloadRecording()');
  assert.deepEqual(requests,[],'local rescue never finalizes or requests server tickets');
  app.run('clearLocalAudioExports()');
  await app.run('downloadRecording({finalize:true})');
  assert.equal(requests.length,1);assert.equal(app.created('a'),undefined);
  await app.run('downloadRecording()');
  assert.deepEqual(requests.map(request => request.url.replace('https://classroom.example','')), [
    `/lectures/${lectureId}/recording-finalize`,
    `/lectures/${lectureId}/recording-download-ticket`,
  ]);
  assert.ok(requests.every(request => request.method === 'POST' && request.authorization === 'Bearer old-token'));
  assert.equal(app.run('current.recording_finalized'), true);
  assert.equal(app.run('lectures[0].recording_finalized'), true);
  assert.equal(app.created('a').href, `https://classroom.example${ticketPath}`);
  assert.equal(app.created('a').clicked, true);
});

test('deletion requires confirmation and invalidates an older lecture response', async () => {
  const targetId = webcrypto.randomUUID(), remainingId = webcrypto.randomUUID();
  const lateLecture = deferred();
  const lateList = deferred();
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,method:options.method || 'GET'});
    if (url.endsWith('/lectures') && !options.method) return lateList.promise;
    if (url.endsWith(`/lectures/${remainingId}`) && !options.method) return lateLecture.promise;
    if (url.endsWith(`/lectures/${targetId}`) && options.method === 'DELETE') return response(null,204);
    return response({});
  });
  app.run(`
    lectures=[
      {id:${JSON.stringify(targetId)},title:'지울 수업',created_at:'2026-01-02T00:00:00Z'},
      {id:${JSON.stringify(remainingId)},title:'늦은 수업',created_at:'2026-01-01T00:00:00Z'},
    ];
    current={id:${JSON.stringify(targetId)},title:'지울 수업',created_at:'2026-01-02T00:00:00Z',segments:[],recording_finalized:true};
    renderCurrent(); renderHistory();
  `);
  const groups = app.element('lecture-list').children;
  const secondButton = groups[1].children[1].children[0];
  const selecting = secondButton.onclick();
  const refreshing = app.run('refreshLectures()');
  await tick();

  app.element('delete-lecture').onclick();
  assert.equal(app.element('delete-dialog').open, true);
  assert.equal(app.element('delete-lecture-title').textContent, '지울 수업');
  await app.element('delete-confirm').onclick();
  assert.equal(app.element('delete-dialog').open, false);
  assert.equal(app.run('current'), null);
  assert.deepEqual(Array.from(app.run('lectures.map(lecture => lecture.id)')), [remainingId]);
  assert.ok(requests.some(item => item.method === 'DELETE' && item.url.endsWith(`/lectures/${targetId}`)));

  lateLecture.resolve(response({id:remainingId,title:'늦게 온 내용',created_at:'2026-01-01T00:00:00Z',segments:[]}));
  lateList.resolve(response([
    {id:targetId,title:'삭제 전 목록',created_at:'2026-01-02T00:00:00Z'},
    {id:remainingId,title:'남은 수업',created_at:'2026-01-01T00:00:00Z'},
  ]));
  await selecting;
  await refreshing;
  assert.equal(app.run('current'), null, 'the response started before deletion must not restore a note');
  assert.deepEqual(Array.from(app.run('lectures.map(lecture => lecture.id)')), [remainingId], 'a stale list must not restore the deleted note');
});

test('deletion safely retries once after a lost response', async () => {
  const lectureId = webcrypto.randomUUID();
  let deleteCalls = 0;
  let app;
  app = setup(async (_url, options = {}) => {
    if (options.method !== 'DELETE') return response({});
    deleteCalls += 1;
    if (deleteCalls === 1) throw app.run("new TypeError('response lost')");
    return response({status:'deleted'});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'응답이 끊긴 수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_finalized:true};
    lectures=[current]; renderCurrent(); renderHistory();
  `);
  app.element('delete-lecture').onclick();
  await app.element('delete-confirm').onclick();
  assert.equal(deleteCalls, 2);
  assert.equal(app.run('current'), null);
  assert.equal(app.run('lectures.length'), 0);
  assert.match(app.element('notice').textContent, /휴지통으로 옮겼어요/);
});

test('unfinished recovered lessons cannot be trashed and keep their recovery reservation', async () => {
  const lectureId = webcrypto.randomUUID();
  const app = setup((url, options = {}) => options.method === 'DELETE'
    ? response(null,204) : response({}));
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'지울 미완료 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'saved',start:0,end:1,text:'저장됨'}],recording_available:true,recording_finalized:false};
    lectures=[current]; captureSession={id:current.id,lecture:current};
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,finalized:false,status:'scheduled'});
    updateControls();
  `);
  assert.equal(app.run('isBusy()'),false);
  assert.equal(app.element('delete-lecture').disabled,true);
  app.element('delete-lecture').onclick();
  await app.element('delete-confirm').onclick();
  assert.equal(app.run('scheduledCorrections.size'),1);
  assert.equal(app.run('captureSession.id'),lectureId);
  assert.equal(app.run('hasOwnerLockedWork()'),true);
});

test('an explicit deletion failure keeps the lecture available for a later retry', async () => {
  const lectureId = webcrypto.randomUUID();
  let deleteCalls = 0;
  const app = setup(async (_url, options = {}) => {
    if (options.method === 'DELETE') {
      deleteCalls += 1;
      return response({detail:'녹음 파일을 지우지 못했습니다.'},503);
    }
    return response({});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'남겨 둘 수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_finalized:true};
    lectures=[current]; renderCurrent(); renderHistory();
  `);
  app.element('delete-lecture').onclick();
  await app.element('delete-confirm').onclick();
  assert.equal(deleteCalls, 1, 'an explicit server failure must not be blindly retried');
  assert.equal(app.run('current.id'), lectureId);
  assert.equal(app.run('lectures[0].id'), lectureId);
  assert.equal(app.element('delete-dialog').open, false);
  assert.match(app.element('notice').textContent, /삭제하지 못했습니다/);
});

test('closing deletion confirmation makes no request and an old account response cannot clear the next account', async () => {
  const oldLectureId = webcrypto.randomUUID(), nextLectureId = webcrypto.randomUUID();
  const deletion = deferred();
  let deleteCalls = 0;
  const app = setup((url, options = {}) => {
    if (options.method === 'DELETE') { deleteCalls += 1; return deletion.promise; }
    return response({});
  });
  app.run(`current={id:${JSON.stringify(oldLectureId)},title:'기존 수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_finalized:true}; lectures=[current]; renderCurrent(); renderHistory()`);
  app.element('delete-lecture').onclick();
  app.element('delete-cancel').onclick();
  assert.equal(app.element('delete-dialog').open, false);
  assert.equal(deleteCalls, 0);

  app.element('delete-lecture').onclick();
  const removing = app.element('delete-confirm').onclick();
  await tick();
  assert.equal(deleteCalls, 1);
  app.run(`
    showLogin(); token='next-token'; user='user-beta';
    current={id:${JSON.stringify(nextLectureId)},title:'다음 계정 수업',created_at:'2026-01-02T00:00:00Z',segments:[]};
    lectures=[current];
  `);
  deletion.resolve(response(null,204));
  await removing;
  assert.equal(app.run('current.id'), nextLectureId);
  assert.equal(app.run('lectures[0].id'), nextLectureId);
  assert.equal(app.run('deletingLecture'), false);
});

test('cancel racing with completion reports completion rather than claiming cancellation', async () => {
  const completed = {
    id:'import-job',lecture_id:'import-lecture',title:'업로드 수업',language:'ko',filename:'lesson.wav',
    status:'completed',total_bytes:10,uploaded_bytes:10,next_offset:10,part_bytes:491520,
    processed_seconds:20,duration_seconds:20,error:null,cancel_requested:false,raw_deleted:true,
  };
  const app = setup(url => url.endsWith('/lectures')
    ? response([{id:'import-lecture',title:'업로드 수업',created_at:'2026-01-01T00:00:00Z'}])
    : response({}));
  app.run(`
    importJob={...${JSON.stringify(completed)},status:'processing'};
    fileUploader={file:{},cancel:async()=>(${JSON.stringify(completed)})};
    current={id:'import-lecture',title:'업로드 수업',created_at:'2026-01-01T00:00:00Z',segments:[]};
  `);
  await app.run('cancelFileImport()');
  assert.equal(app.run('importJob.status'), 'completed');
  assert.match(app.element('notice').textContent, /먼저 변환이 완료/);
  assert.doesNotMatch(app.element('notice').textContent, /변환을 취소하고/);
});

test('a lost init response is reconciled by its stable import id and completed normally', async () => {
  const completed = {
    id:'12345678-1234-4123-8123-123456789abc',lecture_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    title:'복구 수업',language:'ko',filename:'lesson.wav',file_fingerprint:'ab'.repeat(32),status:'completed',
    total_bytes:10,uploaded_bytes:10,next_offset:10,part_bytes:491520,processed_seconds:10,
    duration_seconds:10,error:null,cancel_requested:false,raw_deleted:true,
  };
  const app = setup(url => {
    if (url.endsWith(`/imports/${completed.id}`)) return response(completed);
    if (url.endsWith(`/lectures/${completed.lecture_id}`)) {
      return response({id:completed.lecture_id,title:'복구 수업',created_at:'2026-01-01T00:00:00Z',segments:[]});
    }
    if (url.endsWith('/lectures')) return response([{id:completed.lecture_id,title:'복구 수업',created_at:'2026-01-01T00:00:00Z'}]);
    return response({});
  });
  const running = app.run(`(() => {
    importGeneration=7; selectImportLecture=true;
    fileUploader={importId:${JSON.stringify(completed.id)},file:{name:'lesson.wav'},running:false,
      recover:state=>Promise.resolve(state)};
    const error=new TypeError('response lost'); error.importId=${JSON.stringify(completed.id)};
    return runFileImport(Promise.reject(error),7);
  })()`);
  await running;
  assert.equal(app.run('importJob.status'), 'completed');
  assert.equal(app.run('importError'), '');
  assert.equal(app.run('current.id'), completed.lecture_id);
});

test('logging out detaches only the tab watcher while a server import keeps processing', () => {
  const app = setup(async () => response({}));
  app.run(`
    globalThis.detachCalls=0; globalThis.cancelCalls=0;
    importJob={id:'job',lecture_id:'lecture',status:'processing'};
    fileUploader={detach(){globalThis.detachCalls+=1},cancel(){globalThis.cancelCalls+=1}};
    showLogin();
  `);
  assert.equal(app.run('globalThis.detachCalls'), 1);
  assert.equal(app.run('globalThis.cancelCalls'), 0);
  assert.equal(app.run('importJob'), null);
});

test('explicit logout scrubs the previous account profile, notes, draft, and selected file', async () => {
  const app = setup(async () => response({}));
  const selected = new Blob([new Uint8Array(32)],{type:'audio/wav'});
  Object.defineProperty(selected,'name',{value:'private-lesson.wav'});
  app.element('recording-file').files = [selected];
  app.run(`
    transcriptionProviders.clova.configured=true;
    current={id:'private-note',title:'이전 계정 비공개 수업',language:'en',asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'secret',start:0,end:1,text:'이전 계정만 볼 문장'}]};
    lectures=[current]; captureSession={id:'completed-capture',lecture:current};
    document.getElementById('username').value='user-alpha';
    document.getElementById('password').value='plaintext-password';
    document.getElementById('password-confirm').value='plaintext-password';
    document.getElementById('setup-code').value='one-time-code';
    document.getElementById('current-user').textContent='user-alpha';
    document.querySelector('.user-avatar').textContent='U';
    document.getElementById('delete-lecture-title').textContent=current.title;
    document.getElementById('import-detail').textContent='private-lesson.wav 업로드 중';
    document.getElementById('notice').textContent='이전 계정 알림'; document.getElementById('notice').hidden=false;
    renderCurrent(); renderHistory();
  `);

  await app.element('logout').onclick();
  assert.equal(app.run('user'),'');
  assert.equal(app.run('current'),null);
  assert.equal(app.run('lectures.length'),0);
  assert.equal(app.run('captureSession'),null);
  assert.equal(app.element('username').value,'');
  assert.equal(app.element('password').value,'');
  assert.equal(app.element('password-confirm').value,'');
  assert.equal(app.element('setup-code').value,'');
  assert.equal(app.element('recording-file').value,'');
  assert.equal(app.element('recording-file').files.length,0);
  assert.equal(app.element('lecture-title').value,'');
  assert.equal(app.element('language').value,'ko');
  assert.equal(app.element('audio-source').value,'microphone');
  assert.equal(app.element('asr-provider').value,'qwen');
  assert.equal(app.run('transcriptionProviders.clova.configured'),false);
  assert.equal(app.element('provider-privacy').hidden,true);
  assert.equal(app.element('current-user').textContent,'내 계정');
  assert.equal(app.element('.user-avatar').textContent,'–');
  assert.equal(app.element('delete-lecture-title').textContent,'선택한 수업');
  assert.equal(app.element('import-detail').textContent,'');
  assert.equal(app.element('notice').hidden,true);
  assert.doesNotMatch(JSON.stringify(app.element('lecture-list').children),/비공개 수업/);
  assert.doesNotMatch(JSON.stringify(app.element('transcript').children),/이전 계정만 볼 문장/);
});

test('switching accounts scrubs stale workspace data even when the new history request fails', async () => {
  const app = setup((url) => {
    if (url.endsWith('/auth/login')) return response({token:'beta-token',user:{username:'user-beta',is_admin:false}});
    if (url.endsWith('/lectures')) throw new TypeError('history unavailable');
    if (url.endsWith('/status')) return response({model_state:'ready'});
    return response([]);
  });
  const selected = new Blob([new Uint8Array(32)],{type:'audio/wav'});
  Object.defineProperty(selected,'name',{value:'alpha-private.wav'});
  app.element('recording-file').files = [selected];
  app.run(`
    current={id:'alpha-note',title:'알파 비공개 제목',language:'en',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'alpha-text',start:0,end:1,text:'알파 비공개 문장'}]};
    lectures=[current]; renderCurrent(); renderHistory(); token=''; showLogin(false);
  `);
  app.element('username').value = 'user-beta';
  app.element('password').value = 'beta-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});

  assert.equal(app.run('user'),'user-beta');
  assert.equal(app.run('current'),null);
  assert.equal(app.run('lectures.length'),0);
  assert.equal(app.element('workspace').hidden,false);
  assert.equal(app.element('recording-file').value,'');
  assert.equal(app.element('recording-file').files.length,0);
  assert.equal(app.element('lecture-title').value,'');
  assert.equal(app.element('language').value,'ko');
  assert.equal(app.element('import-button').disabled,true);
  assert.doesNotMatch(JSON.stringify(app.element('lecture-list').children),/알파 비공개 제목/);
  assert.doesNotMatch(JSON.stringify(app.element('transcript').children),/알파 비공개 문장/);
});

test('a failed login clears typed passwords while retaining an activation code for retry', async () => {
  const app = setup(async url => url.endsWith('/auth/activate')
    ? response({detail:'비밀번호를 확인해 주세요.'},401) : response({}));
  app.run("token=''; user=''; setActivation(true)");
  app.element('workspace').hidden = true;
  app.element('username').value = 'new-user';
  app.element('password').value = 'wrong-password';
  app.element('password-confirm').value = 'wrong-password';
  app.element('setup-code').value = 'retryable-one-time-code';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(app.element('password').value,'');
  assert.equal(app.element('password-confirm').value,'');
  assert.equal(app.element('setup-code').value,'retryable-one-time-code');
  assert.equal(app.element('workspace').hidden,true);
});

test('a pending logout locks recording and file import before yielding to the network', async () => {
  const logout = deferred();
  const app = setup(url => url.endsWith('/auth/logout') ? logout.promise : response({}));
  const leaving = app.element('logout').onclick();
  assert.equal(app.run('loggingOut'), true);
  assert.equal(app.element('record-button').disabled, true);
  assert.equal(app.element('recording-file').disabled, true);
  await app.run('startRecording()');
  await app.run('startOrResumeFileImport()');
  assert.equal(app.run('draft'), null);
  assert.equal(app.run('pending.length'), 0);
  assert.equal(app.microphone(), undefined);
  logout.resolve(response(null, 204));
  await leaving;
  assert.equal(app.run('loggingOut'), false);
  assert.equal(app.run('token'), '');
  assert.equal(app.run('user'), '');
});

test('selecting an import while viewing an English note opens an editable Korean draft', () => {
  const app = setup(async () => response({}));
  const file = new Blob([new Uint8Array(10)]);
  Object.defineProperty(file, 'name', {value:'한국사 녹음.m4a'});
  app.run(`current={id:'english-note',title:'English',language:'en',created_at:'2026-01-01T00:00:00Z',segments:[]}; renderCurrent()`);
  app.element('recording-file').files = [file];
  app.element('recording-file').onchange();
  assert.equal(app.run('current'), null);
  assert.equal(app.element('language').value, 'ko');
  assert.equal(app.element('lecture-title').value, '한국사 녹음');
  assert.equal(app.element('language').disabled, false);
});

test('a microphone flush failure remains visible and is not replaced by a generic stop message', async () => {
  const app = setup(async (url,options = {}) => url.endsWith('/lectures')
    ? response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201)
    : response({segments:[]}));
  await app.run('startRecording()');
  app.microphone().stopError = new Error('마지막 오디오 조각을 확인하지 못했습니다.');
  await app.run("stopRecording('기기에서 녹음이 중단됐어요.')");
  await tick(); await tick();
  assert.match(app.element('notice').textContent, /기기에서 녹음이 중단됐어요/);
  assert.match(app.element('notice').textContent, /마지막 오디오 조각을 확인하지 못했습니다/);
  assert.match(app.run('captureWarning'), /마지막 오디오/);
  assert.match(app.element('save-state').textContent, /누락 가능/);
});

test('failed lecture creation retains captured audio for retry', async () => {
  let attempts = 0, uploads = 0;
  const lectureIds = [];
  let app;
  app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      const lectureId = options.headers.get('X-Lecture-Id');
      lectureIds.push(lectureId);
      attempts += 1;
      if (attempts === 1) throw app.run("new TypeError('offline')");
      return response({id:lectureId,title:'수업',asr_provider:'qwen',created_at:new Date().toISOString(),segments:[]});
    }
    if (url.includes('/chunks')) uploads += 1;
    return response({segments:[]});
  });
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  for (let index = 0; index < 20
      && ![...app.timeouts.values()].some(timer => timer.delay === 1000); index += 1) await tick();
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 1000),
    'the transient lecture-creation failure schedules its deterministic retry');
  assert.equal(app.run('pending.length'), 1);
  assert.equal(app.run('isBusy()'), true);
  assert.equal(app.run('recording'), true);
  assert.match(app.run('retryMessage'), /기기에 보관/);
  await app.run('retryPending()'); await tick(); await tick();
  assert.equal(uploads, 1);
  assert.equal(lectureIds.length, 2);
  assert.equal(lectureIds[0], lectureIds[1]);
  assert.equal(app.run('draft'), null);
  assert.equal(app.run('pending.length'), 0);
  assert.equal(app.run('recording'), true);
  app.microphone().tail = chunk(5,3,3,true);
  await app.run('stopRecording()');
});

test('a late unauthorized response cannot log out a newer login', async () => {
  const oldResponse = deferred();
  const app = setup(() => oldResponse.promise);
  const request = app.run("api('/status')");
  app.run("token='new-token'");
  oldResponse.resolve(response({detail:'expired'},401));
  await assert.rejects(request);
  assert.equal(app.run('token'), 'new-token');
});

function storedAuthFixture(sessionItems, {origin = 'https://saved-login.trycloudflare.com', username = 'user-alpha',
  token = 'saved-opaque-token', expiresAt = Date.now() + 3600000} = {}) {
  const record = {version:1,token,username,apiOrigin:origin,expiresAt};
  sessionItems.set(AUTH_SESSION_STORAGE_KEY,JSON.stringify(record));
  return record;
}
function blankAuthentication(app) {
  app.run("apiUrl=''; verifiedApiUrl=''; verifiedApiExpiresAt=0; token=''; user=''; connectionState='unverified'");
  app.element('workspace').hidden = true;
  app.element('auth-screen').hidden = false;
}

test('login saves a tab-only session and a reload verifies health and identity before unlocking the workspace', async () => {
  const sessionItems = new Map(), origin = 'https://saved-login.trycloudflare.com';
  const expiresAt = Math.floor(Date.now() / 1000) + 3600;
  const first = setup(async url => {
    if (url.endsWith('/auth/login')) return response({token:'saved-opaque-token',user:{username:'user-alpha',is_admin:false},session_expires_at:expiresAt});
    if (url.endsWith('/lectures') || url.endsWith('/imports')) return response([]);
    return response({model_state:'ready'});
  },{sessionItems});
  first.run(`apiUrl=${JSON.stringify(origin)}; verifiedApiUrl=apiUrl`);
  first.element('username').value = 'user-alpha';
  first.element('password').value = 'never-store-this-password';
  await first.element('auth-form').onsubmit({preventDefault(){}});
  const saved = JSON.parse(sessionItems.get(AUTH_SESSION_STORAGE_KEY));
  assert.equal(saved.expiresAt,expiresAt * 1000);
  assert.equal(saved.apiOrigin,origin);
  assert.doesNotMatch(sessionItems.get(AUTH_SESSION_STORAGE_KEY),/password|is_admin|never-store/);

  const me = deferred(), requests = [];
  const reloaded = setup(async (url,options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json')) return response(runtimeConfig({apiUrl:origin}));
    if (url.endsWith('/health')) return response({status:'ok'});
    if (url.endsWith('/auth/me')) return me.promise;
    if (url.endsWith('/lectures') || url.endsWith('/imports')) return response([]);
    return response({model_state:'ready'});
  },{sessionItems});
  blankAuthentication(reloaded);
  const boot = reloaded.run('init()');
  await until(() => requests.some(item => item.url.endsWith('/auth/me')),'stored identity request');
  assert.equal(reloaded.run('token'),'');
  assert.equal(reloaded.element('workspace').hidden,true);
  assert.equal(requests.length,3);
  assert.equal(requests[0].options.headers,undefined);
  assert.equal(requests[1].options.headers.get('Authorization'),null);
  assert.equal(requests[2].options.headers.get('Authorization'),'Bearer saved-opaque-token');
  assert.ok(requests[2].url.startsWith(origin));
  me.resolve(response({username:'user-alpha',is_admin:false,session_expires_at:expiresAt}));
  await boot;
  assert.equal(reloaded.run('token'),'saved-opaque-token');
  assert.equal(reloaded.element('workspace').hidden,false);
  assert.equal(reloaded.element('current-user').textContent,'user-alpha');
  assert.equal(requests.some(item => item.url.endsWith('/auth/login')),false);
  assert.equal(JSON.parse(sessionItems.get(AUTH_SESSION_STORAGE_KEY)).expiresAt,saved.expiresAt);
});

test('a changed tunnel cannot receive a saved bearer token and clears the old-origin record', async () => {
  const sessionItems = new Map(), saved = storedAuthFixture(sessionItems);
  const nextOrigin = 'https://new-login.trycloudflare.com', requests = [];
  const app = setup(async (url,options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json')) return response(runtimeConfig({apiUrl:nextOrigin}));
    if (url.endsWith('/health')) return response({status:'ok'});
    throw new Error('no authenticated request expected');
  },{sessionItems});
  blankAuthentication(app);
  await app.run('init()');
  assert.equal(sessionItems.size,0);
  assert.equal(app.run('token'),'');
  assert.equal(app.element('workspace').hidden,true);
  assert.equal(requests.some(item => item.url.startsWith(saved.apiOrigin)),false);
  assert.ok(requests.every(item => !item.options.headers?.get('Authorization')));
});

test('expired tab sessions are removed before any identity probe or private-data request', async () => {
  const sessionItems = new Map(), saved = storedAuthFixture(sessionItems,{expiresAt:Date.now() - 1});
  const requests = [];
  const app = setup(async url => {
    requests.push(url);
    if (url.startsWith('./config.json')) return response(runtimeConfig({apiUrl:saved.apiOrigin}));
    return response({status:'ok'});
  },{sessionItems});
  blankAuthentication(app);
  await app.run('init()');
  assert.equal(sessionItems.size,0);
  assert.equal(requests.length,2);
  assert.equal(app.run('token'),'');
});

test('revoked, expired, mismatched, and legacy identity responses cannot restore a saved login', async () => {
  for (const scenario of ['revoked','forbidden','expired','different-user','missing-expiry']) {
    const sessionItems = new Map(), saved = storedAuthFixture(sessionItems);
    let privateReads = 0;
    const app = setup(async url => {
      if (url.startsWith('./config.json')) return response(runtimeConfig({apiUrl:saved.apiOrigin}));
      if (url.endsWith('/health')) return response({status:'ok'});
      if (url.endsWith('/auth/me')) {
        if (scenario === 'revoked') return response({detail:'revoked'},401);
        if (scenario === 'forbidden') return response({detail:'forbidden'},403);
        if (scenario === 'expired') return response({username:saved.username,session_expires_at:Date.now() / 1000 - 1});
        if (scenario === 'different-user') return response({username:'user-beta',session_expires_at:saved.expiresAt / 1000});
        return response({username:saved.username});
      }
      privateReads += 1;
      return response([]);
    },{sessionItems});
    blankAuthentication(app);
    await app.run('init()');
    assert.equal(app.run('token'),'',scenario);
    assert.equal(app.element('workspace').hidden,true,scenario);
    assert.equal(sessionItems.size,0,scenario);
    assert.equal(privateReads,0,scenario);
  }
});

test('transient discovery or identity failures retain the tab record without installing its token', async () => {
  for (const phase of ['config','identity']) {
    const sessionItems = new Map(), saved = storedAuthFixture(sessionItems);
    const requests = [];
    let recovered = false;
    const app = setup(async (url,options = {}) => {
      requests.push({url,options});
      if (url.startsWith('./config.json')) {
        if (phase === 'config' && !recovered) throw new TypeError('offline');
        return response(runtimeConfig({apiUrl:saved.apiOrigin}));
      }
      if (url.endsWith('/health')) return response({status:'ok'});
      if (url.endsWith('/auth/me')) return recovered
        ? response({username:saved.username,is_admin:false,session_expires_at:saved.expiresAt / 1000})
        : response({detail:'temporarily unavailable'},503);
      if (url.endsWith('/lectures') || url.endsWith('/imports')) return response([]);
      return response({model_state:'ready'});
    },{sessionItems});
    blankAuthentication(app);
    await app.run('init()');
    assert.equal(app.run('token'),'',phase);
    assert.equal(app.element('workspace').hidden,true,phase);
    assert.equal(sessionItems.size,1,phase);
    assert.equal(requests.some(item => item.url.endsWith('/lectures') || item.url.endsWith('/imports')),false,phase);
    if (phase === 'config') assert.ok(requests.every(item => !item.options.headers?.get('Authorization')));
    recovered = true;
    await app.run('discoverServer();');
    assert.equal(await app.run('restoreStoredSession()'),true);
    assert.equal(app.run('token'),saved.token);
  }
});

test('a late saved-session response cannot replace a newer explicitly entered account', async () => {
  const sessionItems = new Map(), saved = storedAuthFixture(sessionItems), me = deferred();
  let probed = false;
  const app = setup(async url => {
    if (url.startsWith('./config.json')) return response(runtimeConfig({apiUrl:saved.apiOrigin}));
    if (url.endsWith('/health')) return response({status:'ok'});
    if (url.endsWith('/auth/me')) { probed = true; return me.promise; }
    if (url.endsWith('/auth/login')) return response({token:'new-user-token',user:{username:'user-beta',is_admin:false},
      session_expires_at:saved.expiresAt / 1000});
    if (url.endsWith('/lectures') || url.endsWith('/imports')) return response([]);
    return response({model_state:'ready'});
  },{sessionItems});
  blankAuthentication(app);
  const boot = app.run('init()');
  await until(() => probed,'saved identity request');
  app.element('username').value = 'user-beta';
  app.element('password').value = 'new-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  me.resolve(response({username:saved.username,session_expires_at:saved.expiresAt / 1000}));
  await boot;
  assert.equal(app.run('user'),'user-beta');
  assert.equal(app.run('token'),'new-user-token');
  assert.equal(JSON.parse(sessionItems.get(AUTH_SESSION_STORAGE_KEY)).username,'user-beta');
});

test('logout and current-session 401 revoke browser persistence while late 401 responses preserve a newer session', async () => {
  const sessionItems = new Map(), origin = 'https://saved-login.trycloudflare.com';
  const unauthorized = deferred();
  let statusRequests = 0;
  const app = setup(async url => {
    if (url.endsWith('/old-request')) return unauthorized.promise;
    if (url.endsWith('/status')) { statusRequests += 1; return response({detail:'revoked'},401); }
    if (url.endsWith('/auth/logout')) throw new TypeError('logout response lost');
    return response({});
  },{sessionItems});
  app.run(`apiUrl=${JSON.stringify(origin)}; verifiedApiUrl=apiUrl; token='old-token';
    rememberAuthenticatedSession({token,user:{username:user},session_expires_at:Date.now()/1000+3600},apiUrl)`);
  const old = app.run("api('/old-request')");
  app.run("token='new-token'; rememberAuthenticatedSession({token,user:{username:user},session_expires_at:Date.now()/1000+3600},apiUrl)");
  unauthorized.resolve(response({detail:'old session revoked'},401));
  await assert.rejects(old);
  assert.equal(JSON.parse(sessionItems.get(AUTH_SESSION_STORAGE_KEY)).token,'new-token');
  await app.run('updateStatus()');
  assert.equal(statusRequests,1);
  assert.equal(sessionItems.size,0);
  assert.equal(app.run('token'),'');
  app.run("token='logout-token'; rememberAuthenticatedSession({token,user:{username:user},session_expires_at:Date.now()/1000+3600},apiUrl)");
  await app.element('logout').onclick();
  assert.equal(sessionItems.size,0);
  assert.equal(app.run('token'),'');
});

test('active session expiry blocks credentials before fetch while microphone capture remains recoverable', async () => {
  let requests = 0;
  const sessionItems = new Map();
  const app = setup(async () => { requests += 1; return response({}); },{sessionItems});
  app.run(`
    rememberAuthenticatedSession({token,user:{username:user},session_expires_at:Date.now()/1000+3600},apiUrl);
    recording=true; capture={recording:true}; captureSession={id:'active-capture',owner:user,asrProvider:'qwen'};
    authSessionExpiresAt=Date.now()-1;
  `);
  await assert.rejects(app.run("api('/lectures')"),/만료/);
  assert.equal(requests,0);
  assert.equal(app.run('token'),'');
  assert.equal(app.run('user'),'user-alpha');
  assert.equal(app.run('recording'),true);
  assert.equal(app.element('workspace').hidden,true);
  assert.equal(sessionItems.size,0);
});

test('an invitation flow and another owner pending audio prevent automatic saved-session login', async () => {
  for (const reason of ['invitation','pending-owner']) {
    const sessionItems = new Map(), saved = storedAuthFixture(sessionItems);
    let probes = 0;
    const app = setup(async url => {
      if (url.startsWith('./config.json')) return response(runtimeConfig({apiUrl:saved.apiOrigin}));
      if (url.endsWith('/auth/me')) probes += 1;
      return response({status:'ok'});
    },{sessionItems});
    blankAuthentication(app);
    if (reason === 'invitation') {
      app.location.hash = '#username=invited-user&setup_code=opaque-invitation';
      await app.run('init()');
    } else {
      app.run(`apiUrl=${JSON.stringify(saved.apiOrigin)}; verifiedApiUrl=apiUrl; connectionState='connected';
        user='pending-owner'; pending=[{owner:user}];`);
      assert.equal(await app.run('restoreStoredSession()'),false);
    }
    assert.equal(probes,0,reason);
    assert.equal(app.run('token'),'',reason);
  }
});

test('the workspace stays hidden until login history and import recovery finish', async () => {
  const imports = deferred();
  const status = deferred();
  const app = setup((url) => {
    if (url.endsWith('/auth/login')) return response({token:'fresh-token',user:{username:'user-alpha'}});
    if (url.endsWith('/lectures')) return response([]);
    if (url.endsWith('/imports')) return imports.promise;
    if (url.endsWith('/status')) return status.promise;
    return response({});
  });
  app.element('auth-screen').hidden = false;
  app.element('workspace').hidden = true;
  app.element('username').value = 'user-alpha';
  app.element('password').value = 'valid-password';
  const submitting = app.element('auth-form').onsubmit({preventDefault(){}});
  await tick(); await tick();
  assert.equal(app.run('authenticating'), true);
  assert.equal(app.element('auth-screen').hidden, false);
  assert.equal(app.element('workspace').hidden, true);
  imports.resolve(response([]));
  await tick(); await tick();
  assert.equal(app.run('authenticating'), true);
  assert.equal(app.element('workspace').hidden, true,
    'the first provider status must settle before recording can be enabled');
  status.resolve(response({model_state:'ready',transcription_providers:{
    qwen:{configured:true},clova:{configured:true},
  }}));
  await submitting;
  assert.equal(app.element('auth-screen').hidden, true);
  assert.equal(app.element('workspace').hidden, false);
  assert.equal(app.element('asr-provider').value,'clova');
  assert.equal(app.element('record-button').disabled, false);
  assert.equal(app.element('recording-file').disabled, false);
});

test('a rejected first provider status cannot expose an authenticated workspace', async () => {
  const app = setup((url) => {
    if (url.endsWith('/auth/login')) return response({token:'fresh-token',user:{username:'user-alpha'}});
    if (url.endsWith('/lectures') || url.endsWith('/imports')) return response([]);
    if (url.endsWith('/status')) return response({detail:'expired'},401);
    return response({});
  });
  app.element('auth-screen').hidden = false;
  app.element('workspace').hidden = true;
  app.element('username').value = 'user-alpha';
  app.element('password').value = 'valid-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('token'),'');
  assert.equal(app.element('auth-screen').hidden,false);
  assert.equal(app.element('workspace').hidden,true);
});

test('a different account may log in after expiry when no unsent audio is owner-locked', async () => {
  const app = setup(url => {
    if (url.endsWith('/auth/login')) return response({token:'beta-token',user:{username:'user-beta'}});
    if (url.endsWith('/lectures') || url.endsWith('/imports')) return response([]);
    return response({});
  });
  app.run("token=''; user='user-alpha'; showLogin(false)");
  app.element('username').value = 'user-beta';
  app.element('password').value = 'valid-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('user'), 'user-beta');
  assert.equal(app.run('token'), 'beta-token');
  assert.equal(app.element('auth-error').hidden, true);
});

test('a stale successful login response cannot install a token after the server origin changes', async () => {
  const login = deferred();
  const app = setup(() => login.promise);
  app.element('username').value = 'user-alpha';
  app.element('password').value = 'valid-password';
  const submitting = app.element('auth-form').onsubmit({preventDefault(){}});
  await tick();
  assert.equal(app.run('authenticating'), true);

  app.element('api-url').value = 'https://replacement.trycloudflare.com';
  await app.element('connection-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('apiUrl'), 'https://classroom.example', 'UI server changes are blocked during authentication');

  app.run("apiUrl='https://replacement.trycloudflare.com'; ++requestGeneration; token=''");
  login.resolve(response({token:'stale-token',user:{username:'user-alpha'}}));
  await submitting;
  assert.equal(app.run('token'), '');
  assert.match(app.element('auth-error').textContent, /응답을 적용하지 않았어요/);
  assert.equal(app.run('authenticating'), false);
});

test('a verified server change keeps recording active and checks the candidate without the token', async () => {
  const candidateHealth = deferred();
  const requests = [];
  const app = setup(async (url,options = {}) => {
    requests.push({url,authorization:options.headers.get('Authorization')});
    if (url === 'https://replacement.trycloudflare.com/health') return candidateHealth.promise;
    if (url === 'https://classroom.example/status') return response({model_state:'ready'});
    throw new Error(`unexpected request: ${url}`);
  });
  app.run('recording=true');
  app.element('api-url').value = 'https://replacement.trycloudflare.com';
  const saving = app.element('connection-form').onsubmit({preventDefault(){}});
  await tick();
  assert.equal(app.run('connectionState'),'checking');
  await app.run("api('/status')");
  assert.deepEqual(requests,[
    {url:'https://replacement.trycloudflare.com/health',authorization:null},
    {url:'https://classroom.example/status',authorization:'Bearer old-token'},
  ]);
  assert.equal(app.run('apiUrl'), 'https://classroom.example');
  assert.equal(app.run('token'), 'old-token');
  assert.equal(app.run('recording'), true);

  candidateHealth.resolve(response({status:'ok'}));
  await saving;
  assert.equal(app.run('apiUrl'), 'https://replacement.trycloudflare.com');
  assert.equal(app.run('token'), '');
  assert.equal(app.run('user'), 'user-alpha');
  assert.equal(app.run('recording'), true);
});

test('checking a new server never exposes the active token before the origin changes', async () => {
  const health = deferred();
  let healthAuthorization = 'not-called';
  const app = setup(async (url, options) => {
    if (url === 'https://new-server.trycloudflare.com/health') {
      healthAuthorization = options.headers.get('Authorization');
      return health.promise;
    }
    return response({});
  });
  app.element('api-url').value = 'https://new-server.trycloudflare.com';
  const saving = app.element('connection-form').onsubmit({preventDefault(){}});
  await tick();
  assert.equal(app.run('apiUrl'), 'https://classroom.example');
  assert.equal(app.run('token'), 'old-token');
  assert.equal(healthAuthorization, null);
  health.resolve(response({status:'ok'}));
  await saving;
  assert.equal(app.run('apiUrl'), 'https://new-server.trycloudflare.com');
  assert.equal(app.run('token'), '');
});

test('a new tunnel can replace a dead one without losing a queued chunk or its owner lock', async () => {
  const requests = [];
  const app = setup(async (url, options) => {
    requests.push({url,authorization:options.headers.get('Authorization')});
    return response({status:'ok'});
  });
  app.run("pending=[{blob:new Blob([new Uint8Array(1644)],{type:'audio/wav'}),startSeconds:0,durationSeconds:0.05,overlapSeconds:0,final:false,id:'stable-id',lectureId:'lesson',owner:'user-alpha'}]; sendError='old tunnel failed'");
  app.element('api-url').value = 'https://replacement.trycloudflare.com';
  await app.element('connection-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('apiUrl'), 'https://replacement.trycloudflare.com');
  assert.equal(app.run('token'), '');
  assert.equal(app.run('user'), 'user-alpha');
  assert.equal(app.run('pending[0].id'), 'stable-id');
  assert.equal(app.element('username').value, 'user-alpha');
  assert.equal(requests[0].authorization, null);

  app.element('username').value = 'user-beta';
  app.element('password').value = 'different-user-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(requests.length, 1, 'a different account cannot claim the retained audio queue');
  assert.match(app.element('auth-error').textContent, /이전 계정/);
});

test('server URL accepts only Cloudflare Quick Tunnels and local development', () => {
  const app = setup(async () => response({}));
  assert.equal(
    app.run("normalizeUrl('https://gentle-classroom-voice.trycloudflare.com/')"),
    'https://gentle-classroom-voice.trycloudflare.com',
  );
  assert.throws(() => app.run("normalizeUrl('http://localhost:8765')"), /로컬 개발 페이지/);
  app.location.hostname = 'localhost';
  assert.equal(app.run("normalizeUrl('http://localhost:8765')"), 'http://localhost:8765');
  assert.equal(app.run("normalizeUrl('https://127.0.0.1:8765')"), 'https://127.0.0.1:8765');
  assert.equal(app.run("normalizeUrl('http://[::1]:8765')"), 'http://[::1]:8765');

  for (const value of [
    'https://attacker.example',
    'https://trycloudflare.com',
    'https://nested.evil.trycloudflare.com',
    'https://safe.trycloudflare.com.evil.example',
    'http://safe.trycloudflare.com',
    'https://safe.trycloudflare.com:8443',
    'https://safe.trycloudflare.com/path',
    'https://safe.trycloudflare.com/?redirect=evil',
    'https://user:password@safe.trycloudflare.com',
  ]) {
    assert.throws(() => app.run(`normalizeUrl(${JSON.stringify(value)})`));
  }
});

test('a fresh Pages config overrides a stale saved tunnel only after anonymous health verification', async () => {
  const freshUrl = 'https://fresh-classroom.trycloudflare.com';
  const staleUrl = 'https://stale-classroom.trycloudflare.com';
  const health = deferred(), requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json?')) return response(runtimeConfig({apiUrl:freshUrl}));
    if (url === `${freshUrl}/health`) return health.promise;
    throw new Error(`unexpected request: ${url}`);
  }, {storedServer:staleUrl});
  app.run("apiUrl=''; verifiedApiUrl=''; connectionState='unverified'; token=''; user=''; setConnectionState('unverified')");

  const initializing = app.run('init()');
  await tick(); await tick();
  assert.equal(app.run('apiUrl'), '', 'an unverified config must not become the active API origin');
  assert.equal(app.storedServer(), staleUrl, 'the stored fallback is not overwritten before health succeeds');
  assert.equal(app.element('login-button').disabled, true);
  assert.equal(app.element('connection-open')['data-state'], 'discovering');
  assert.equal(app.element('connection-open')['aria-busy'], 'true');
  assert.match(requests[0].url, /^\.\/config\.json\?v=\d+$/);
  assert.notEqual(requests[0].url, './config.json', 'the runtime lookup must bypass an old Pages/CDN object');
  assert.equal(requests[0].options.cache, 'no-store');
  assert.equal(requests[0].options.credentials, 'omit');
  assert.equal(requests[0].options.referrerPolicy, 'no-referrer');
  assert.equal(requests[0].options.body, undefined);
  assert.equal(requests[1].options.credentials, 'omit');
  assert.equal(requests[1].options.headers.get('Authorization'), null);
  assert.equal(requests[1].options.body, undefined);

  health.resolve(response({status:'ok'}));
  await initializing;
  assert.equal(app.run('apiUrl'), freshUrl);
  assert.equal(app.run('verifiedApiUrl'), freshUrl);
  assert.equal(app.storedServer(), freshUrl);
  assert.equal(app.element('login-button').disabled, false);
  assert.equal(app.element('connection-open')['data-state'], 'connected');
  assert.equal(app.element('connection-open')['aria-busy'], undefined);
});

test('offline, expired, and malformed Pages configs fail closed without trying a saved tunnel', async () => {
  const now = Date.now();
  const cases = [
    ['offline',runtimeConfig({state:'offline',apiUrl:'',publishedMs:now - 60000,expiresMs:now - 60000}),/꺼져/],
    ['expired',runtimeConfig({publishedMs:now - 25 * 60 * 60 * 1000,expiresMs:now - 60 * 60 * 1000}),/만료/],
    ['malformed',{apiUrl:''},/확인하지 못했/],
  ];
  for (const [name,config,message] of cases) {
    const requests = [];
    const app = setup((url, options = {}) => {
      requests.push({url,options});
      return response(config);
    }, {storedServer:'https://saved-fallback.trycloudflare.com'});
    app.run("apiUrl='https://unverified-stale.trycloudflare.com'; verifiedApiUrl=''; connectionState='unverified'; token=''; user=''; setConnectionState('unverified')");
    await app.run('init()');
    assert.equal(requests.length, 1, `${name} must not fall back to a stale saved origin`);
    assert.match(requests[0].url, /^\.\/config\.json\?v=/);
    assert.equal(app.run('connectionState'), 'manual-needed');
    assert.equal(app.run('verifiedApiUrl'), '');
    assert.equal(app.element('login-button').disabled, true);
    assert.match(app.element('auth-server-status').textContent, message);
  }
});

test('a transient Pages config failure stays locked and leaves the saved tunnel as manual prefill only', async () => {
  const savedUrl = 'https://saved-fallback.trycloudflare.com';
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json?')) throw new TypeError('temporary Pages failure');
    throw new Error(`unexpected request: ${url}`);
  }, {storedServer:savedUrl});
  app.run("apiUrl=''; verifiedApiUrl=''; connectionState='unverified'; token=''; user=''; setConnectionState('unverified')");
  await app.run('init()');
  assert.equal(requests.length, 1, 'a stored Quick Tunnel must not bypass the same-origin runtime lease');
  assert.match(requests[0].url, /^\.\/config\.json\?v=/);
  assert.equal(app.run('apiUrl'), '');
  assert.equal(app.run('verifiedApiUrl'), '');
  assert.equal(app.run('connectionState'), 'manual-needed');
  assert.equal(app.element('login-button').disabled, true);
  assert.equal(app.element('api-url').value, savedUrl);
});

test('an authoritative config health failure never falls back to the stale saved origin', async () => {
  const freshUrl = 'https://published-but-dead.trycloudflare.com';
  const savedUrl = 'https://saved-but-stale.trycloudflare.com';
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json?')) return response(runtimeConfig({apiUrl:freshUrl}));
    if (url === `${freshUrl}/health`) throw new TypeError('tunnel stopped');
    if (url === `${savedUrl}/health`) return response({status:'ok'});
    throw new Error(`unexpected request: ${url}`);
  }, {storedServer:savedUrl});
  app.run("apiUrl=''; verifiedApiUrl=''; connectionState='unverified'; token=''; user=''; setConnectionState('unverified')");
  await app.run('init()');
  assert.deepEqual(requests.map(request => request.url), [requests[0].url,`${freshUrl}/health`]);
  assert.equal(app.run('connectionState'), 'manual-needed');
  assert.equal(app.run('apiUrl'), '');
  assert.equal(app.element('login-button').disabled, true);
});

test('an expired automatic lease blocks bearer, password, and audio bodies before any old-origin request', async () => {
  const oldUrl = 'https://expired-origin.trycloudflare.com';
  const publishedMs = Date.now() - 60000;
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json?')) {
      return response(runtimeConfig({state:'offline',apiUrl:'',publishedMs,expiresMs:publishedMs}));
    }
    throw new Error(`unexpected request: ${url}`);
  });
  app.run(`
    apiUrl=${JSON.stringify(oldUrl)}; verifiedApiUrl=apiUrl; verifiedApiExpiresAt=Date.now()-1;
    connectionState='connected'; token='secret-bearer'; user='user-alpha'; setConnectionState('connected');
  `);
  const credentials = app.run(`api('/auth/login', {
    method:'POST',body:JSON.stringify({username:'private-user',password:'private-password'})
  })`);
  const audio = app.run(`api('/lectures/lesson/chunks', {
    method:'POST',body:new Blob([new Uint8Array(1644)],{type:'audio/wav'})
  })`);
  const results = await Promise.allSettled([credentials,audio]);

  assert.ok(results.every(result => result.status === 'rejected'));
  assert.ok(results.every(result => result.reason.connectionLeaseExpired));
  assert.equal(requests.length, 1, 'only same-origin config may be requested after lease expiry');
  assert.match(requests[0].url, /^\.\/config\.json\?v=/);
  assert.equal(requests[0].options.body, undefined);
  assert.equal(requests[0].options.credentials, 'omit');
  assert.equal(requests.some(request => request.url.startsWith(oldUrl)), false);
  assert.equal(app.run('token'), '');
  assert.equal(app.run('verifiedApiUrl'), '');
  assert.equal(app.run('connectionState'), 'manual-needed');
  assert.equal(app.element('login-button').disabled, true);
});

test('an expired lease renews from Pages and sends queued audio only after anonymous health succeeds', async () => {
  const server = 'https://renewed-origin.trycloudflare.com';
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json?')) return response(runtimeConfig({apiUrl:server}));
    if (url === `${server}/health`) return response({status:'ok'});
    if (url === `${server}/lectures/lesson/chunks`) return response({segments:[]});
    throw new Error(`unexpected request: ${url}`);
  });
  app.run(`
    apiUrl=${JSON.stringify(server)}; verifiedApiUrl=apiUrl; verifiedApiExpiresAt=Date.now()-1;
    connectionState='connected'; token='secret-bearer'; user='user-alpha'; setConnectionState('connected');
  `);
  await app.run(`api('/lectures/lesson/chunks', {
    method:'POST',body:new Blob([new Uint8Array(1644)],{type:'audio/wav'})
  })`);

  assert.deepEqual(requests.map(request => request.url), [
    requests[0].url,`${server}/health`,`${server}/lectures/lesson/chunks`,
  ]);
  assert.match(requests[0].url, /^\.\/config\.json\?v=/);
  assert.equal(requests[0].options.body, undefined);
  assert.equal(requests[1].options.headers.get('Authorization'), null);
  assert.equal(requests[1].options.body, undefined);
  assert.equal(requests[2].options.headers.get('Authorization'), 'Bearer secret-bearer');
  assert.ok(requests[2].options.body instanceof Blob);
  assert.ok(app.run('verifiedApiExpiresAt > Date.now()'));
  assert.equal(app.run('connectionState'), 'connected');
  assert.equal(app.run('token'), 'secret-bearer');
});

test('a renewed lease with a new tunnel preserves queued-audio ownership until same-account login', async () => {
  const oldUrl = 'https://old-lease.trycloudflare.com';
  const newUrl = 'https://new-lease.trycloudflare.com';
  const durableChunkId = webcrypto.randomUUID();
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json?')) return response(runtimeConfig({apiUrl:newUrl}));
    if (url === `${newUrl}/health`) return response({status:'ok'});
    if (url === `${newUrl}/auth/login`) return response({token:'new-token',user:{username:'user-alpha'}});
    if (url === `${newUrl}/lectures` || url === `${newUrl}/imports`) return response([]);
    if (url === `${newUrl}/status`) return response({model_state:'ready'});
    if (url === `${newUrl}/lectures/lesson/chunks`) return response({segments:[],recording_available:true,recording_finalized:false});
    throw new Error(`unexpected request: ${url}`);
  });
  app.run(`
    apiUrl=${JSON.stringify(oldUrl)}; verifiedApiUrl=apiUrl; verifiedApiExpiresAt=Date.now()-1;
    connectionState='connected'; token='old-token'; user='user-alpha';
    current={id:'lesson',title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],asr_provider:'qwen'}; lectures=[current];
    liveSessions.set('lesson',{id:'lesson',owner:'user-alpha',asrProvider:'qwen',lecture:current});
    pending=[{blob:new Blob([new Uint8Array(1644)],{type:'audio/wav'}),startSeconds:0,durationSeconds:0.05,overlapSeconds:0,final:false,id:${JSON.stringify(durableChunkId)},captureId:'lesson',lectureId:'lesson',owner:'user-alpha',asrProvider:'qwen',lectureReady:true,durable:true}];
    setConnectionState('connected'); renderCurrent();
  `);
  await app.run("openLiveQueue()");
  app.run("liveQueue.getSession=async(owner,id)=>owner==='user-alpha'&&id==='lesson'?{id,owner,asrProvider:'qwen'}:null");
  await app.run('drain()');

  assert.equal(requests.some(request => request.url.startsWith(oldUrl)), false);
  assert.deepEqual(requests.map(request => request.url), [requests[0].url,`${newUrl}/health`]);
  assert.equal(app.run('apiUrl'), newUrl);
  assert.equal(app.run('token'), '');
  assert.equal(app.run('user'), 'user-alpha');
  assert.equal(app.run('pending[0].id'), durableChunkId);
  assert.equal(app.element('username').value, 'user-alpha');

  app.element('username').value = 'user-alpha';
  app.element('password').value = 'same-account-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  for (let index = 0; index < 4; index += 1) await tick();
  const loginRequest = requests.find(request => request.url === `${newUrl}/auth/login`);
  const audioRequest = requests.find(request => request.url === `${newUrl}/lectures/lesson/chunks`);
  assert.ok(loginRequest);
  assert.match(String(loginRequest.options.body), /same-account-password/);
  assert.ok(audioRequest);
  assert.equal(audioRequest.options.headers.get('Authorization'), 'Bearer new-token');
  assert.ok(audioRequest.options.body instanceof Blob);
  assert.equal(app.run('pending.length'), 0);
  assert.equal(requests.some(request => request.url.startsWith(oldUrl)), false);
});

test('manual connection cancels a late automatic discovery before it can replace the chosen origin', async () => {
  const lateConfig = deferred();
  const automaticUrl = 'https://late-automatic.trycloudflare.com';
  const manualUrl = 'https://chosen-manually.trycloudflare.com';
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,options});
    if (url.startsWith('./config.json?')) return lateConfig.promise;
    if (url === `${manualUrl}/health`) return response({status:'ok'});
    if (url === `${automaticUrl}/health`) return response({status:'ok'});
    throw new Error(`unexpected request: ${url}`);
  });
  app.run("apiUrl=''; verifiedApiUrl=''; connectionState='unverified'; token=''; user=''; setConnectionState('unverified')");
  const initializing = app.run('init()');
  await tick();

  app.element('auth-server-open').onclick();
  assert.equal(app.element('connection-dialog').open, true);
  assert.equal(app.element('api-url').focused, true);
  app.element('api-url').value = manualUrl;
  await app.element('connection-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('apiUrl'), manualUrl);
  assert.equal(app.run('connectionState'), 'connected');

  lateConfig.resolve(response(runtimeConfig({apiUrl:automaticUrl})));
  await initializing;
  assert.equal(app.run('apiUrl'), manualUrl);
  assert.equal(app.run('verifiedApiUrl'), manualUrl);
  assert.equal(requests.filter(request => request.url === `${automaticUrl}/health`).length, 0);
});

test('a manual health error stays accessible and cannot unlock login with an unverified URL', async () => {
  const app = setup((url) => url.endsWith('/health')
    ? response({detail:'서버 준비 중'},503) : response({}));
  app.run("apiUrl=''; verifiedApiUrl=''; connectionState='manual-needed'; token=''; user=''; setConnectionState('manual-needed')");
  app.element('api-url').value = 'https://not-ready.trycloudflare.com';
  await app.element('connection-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('connectionState'), 'manual-needed');
  assert.equal(app.run('verifiedApiUrl'), '');
  assert.equal(app.element('login-button').disabled, true);
  assert.equal(app.element('api-url')['aria-invalid'], 'true');
  assert.equal(app.element('api-url').focused, true);
  assert.equal(app.element('connection-error').hidden, false);
  assert.match(app.element('connection-error').textContent, /서버 준비 중/);
});

test('a programmatic login submit cannot send credentials to an unverified origin', async () => {
  let requests = 0;
  const app = setup(async () => { requests += 1; return response({}); });
  app.run("apiUrl='https://unverified.trycloudflare.com'; verifiedApiUrl=''; connectionState='connected'; token=''; user=''; updateAuthControls() ");
  app.element('username').value = 'private-user';
  app.element('password').value = 'private-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(requests, 0);
  assert.equal(app.element('login-button').disabled, true);
  assert.equal(app.element('auth-error').hidden, false);
  assert.match(app.element('auth-error').textContent, /연결을 먼저 확인/);
  assert.equal(app.element('auth-server-open').focused, true);
});

test('a forged invitation cannot choose even an otherwise-valid Quick Tunnel', async () => {
  let requests = 0;
  const app = setup(async () => { requests += 1; return response({apiUrl:''}); });
  app.run("apiUrl=''");
  app.location.hash = '#username=user-alpha&setup_code=opaque-invitation-code-1234&api=https%3A%2F%2Fevil-capture.trycloudflare.com';
  await app.run('init()');
  assert.equal(app.run('apiUrl'), '');
  assert.equal(app.element('setup-code').value, 'opaque-invitation-code-1234');
  assert.equal(app.run('activation'), true);
  assert.equal(requests, 1, 'only the same-origin static config is fetched');
});

test('a Pages invitation cannot redirect activation credentials to loopback', async () => {
  const app = setup(async () => response({apiUrl:''}));
  app.run("apiUrl=''");
  app.location.hash = '#username=user-alpha&setup_code=opaque-invitation-code-1234&api=http%3A%2F%2Flocalhost%3A8765';
  await app.run('init()');
  assert.equal(app.run('apiUrl'), '');
  assert.equal(app.run('activation'), true);
});

test('invitation username is restored without a client-side account allow-list', async () => {
  const app = setup(async () => response({apiUrl:''}));
  app.location.hash = '#username=server-owned-account&setup_code=opaque-invitation-code-1234';
  await app.run('init()');
  assert.equal(app.element('username').value, 'server-owned-account');
  assert.equal(app.element('setup-code').value, 'opaque-invitation-code-1234');
  assert.equal(app.run('activation'), true);
  assert.equal(app.historyCalls.length, 1);
  assert.equal(app.historyCalls[0][2], '/classroom/');
  assert.doesNotMatch(source, /ACCOUNT_USERNAMES|\buser-alpha\b|\buser-beta\b/);
});

test('temporary upload failures use exponential retry with one stable chunk id and preserve queue order through the final tail', async () => {
  const uploads = [];
  let attempt = 0;
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      return response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (!url.includes('/chunks')) return response({});
    attempt += 1;
    uploads.push({
      id:options.headers.get('X-Chunk-Id'),
      start:options.headers.get('X-Start-Seconds'),
      overlap:options.headers.get('X-Overlap-Seconds'),
      final:options.headers.get('X-Final-Chunk'),
    });
    if (attempt === 1) return response({detail:'잠시 사용할 수 없습니다.'},503);
    if (attempt === 2) return response({detail:'처리 대기열이 찼습니다.'},429);
    const start = Number(options.headers.get('X-Start-Seconds'));
    const finalized = options.headers.get('X-Final-Chunk') === 'true';
    return response({
      segments:[{id:`segment-${start}`,start,end:start + 1,text:`문장 ${start}`}],
      recording_available:true,recording_finalized:finalized,
    });
  });

  await app.run('startRecording()');
  app.microphone().tail = chunk(14, 2.01, 2, true);
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  const firstId = app.run('pending[0].id');
  assert.equal(attempt, 1);
  assert.equal(app.run('recording'), true);
  assert.equal(app.run('pending.length'), 1);
  assert.equal(app.run('retryAttempt'), 1);
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 1000));

  app.microphone().callbacks.onChunk(chunk(6, 10, 2));
  await tick();
  assert.equal(attempt, 1, 'new audio waits behind the retrying head');
  assert.equal(app.run('pending.length'), 2);
  assert.equal(app.run('recording'), true, 'a temporary failure does not immediately stop capture');

  await app.run('stopRecording()');
  assert.equal(app.run('pending.length'), 3);
  const secondId = app.run('pending[1].id');
  const tailId = app.run('pending[2].id');
  assert.equal(app.run('pending[2].startSeconds'), 14);

  await app.runTimeout(1000);
  assert.equal(attempt, 2);
  assert.equal(app.run('pending.length'), 3);
  assert.equal(app.run('retryAttempt'), 2);
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 2000));

  await app.runTimeout(2000);
  assert.equal(app.run('pending.length'), 0);
  assert.equal(app.run('sendError'), '');
  assert.equal(app.run('retryAttempt'), 0);
  assert.deepEqual(uploads.map(item => item.id), [firstId,firstId,firstId,secondId,tailId]);
  assert.deepEqual(uploads.map(item => item.start), ['0','0','0','6','14']);
  assert.deepEqual(uploads.map(item => item.overlap), ['0','0','0','2','2']);
  assert.deepEqual(uploads.map(item => item.final), ['false','false','false','false','true']);
  assert.deepEqual(uploads.slice(0, 3), [uploads[0],uploads[0],uploads[0]],
    'a retry preserves the UUID and all boundary/finalization headers');
  assert.equal(new Set([firstId,secondId,tailId]).size, 3);
  assert.equal(app.run('current.segments.length'), 3);
  assert.equal(app.run('current.recording_available && current.recording_finalized'), true);
  assert.equal(app.element('recording-download').disabled, false);
});

test('only an explicit not-started CLOVA 429 retries its POST with the same chunk identity', async () => {
  const sentIds = [];
  let lookups = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    if (url.endsWith('/result')) { lookups += 1; return response({state:'unknown'}); }
    if (url.endsWith('/chunks')) {
      sentIds.push(options.headers.get('X-Chunk-Id'));
      if (sentIds.length === 1) return response({detail:'처리 시작 전 대기',code:'chunk_not_started',safe_to_retry:true},429,{'Retry-After':'2'});
      return response({segments:[{id:'safe-retry',start:0,end:7,text:'중복 없는 문장'}],recording_available:true,recording_finalized:false});
    }
    return response({});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => app.run('retryTimer !== null'),'safe CLOVA retry');
  assert.equal(app.run('[...liveQueue.chunks.values()][0].state'),'queued');
  assert.equal(app.run('sendError'),'');
  await app.runTimeout(2000);
  assert.equal(sentIds.length,2);
  assert.equal(sentIds[0],sentIds[1]);
  assert.equal(lookups,0);
  assert.equal(app.run('pending.length'),0);
  assert.equal(app.element('transcript').children[0].children[1].textContent,'중복 없는 문장');
});

test('a lost CLOVA response is recovered by hash-checked GET without another audio POST', async () => {
  const audio = chunk(0), digest = await webcrypto.subtle.digest('SHA-256',await audio.blob.arrayBuffer());
  const expectedHash = Buffer.from(digest).toString('hex');
  let uploads = 0, lookups = 0, chunkId = '';
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    if (url.endsWith('/chunks')) {
      uploads += 1; chunkId = options.headers.get('X-Chunk-Id');
      throw new TypeError('response lost after server commit');
    }
    if (url.endsWith('/result')) {
      lookups += 1;
      assert.ok(url.endsWith(`/chunks/${chunkId}/result`));
      assert.equal(options.body,undefined);
      assert.notEqual(options.method,'POST');
      assert.equal(options.headers.get('X-Chunk-Payload-SHA256'),expectedHash);
      assert.equal(options.headers.get('X-Start-Seconds'),'0');
      assert.equal(options.headers.get('X-Overlap-Seconds'),'0');
      assert.equal(options.headers.get('X-Final-Chunk'),'false');
      return response({state:'done',result:{segments:[{id:'recovered',start:0,end:7,text:'서버에서 회수한 문장'}],
        recording_available:true,recording_finalized:false}});
    }
    return response({});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(audio);
  await until(() => app.run('pending.length === 0'),'CLOVA saved result recovery');
  assert.equal(uploads,1);
  assert.equal(lookups,1);
  assert.equal(app.run('liveQueue.chunks.size'),0);
  assert.equal(app.element('transcript').children[0].children[1].textContent,'서버에서 회수한 문장');
  assert.equal(app.run('recording'),true);
});

test('pending CLOVA recovery polls GET only and continues through an explicit microphone stop', async () => {
  let uploads = 0, lookups = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    if (url.endsWith('/chunks')) {
      uploads += 1;
      if (uploads === 1) throw new TypeError('response lost during processing');
      assert.equal(options.headers.get('X-Final-Chunk'),'true');
      return response({segments:[],recording_available:true,recording_finalized:true});
    }
    if (url.endsWith('/result')) {
      lookups += 1;
      if (lookups < 3) return response({state:'pending'});
      return response({state:'done',result:{segments:[{id:'last-sentence',start:0,end:7,text:'회수한 마지막 문장'}],
        recording_available:true,recording_finalized:false}});
    }
    return response({});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  app.microphone().tail = chunk(6,2.2,2,true);
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => app.run('retryTimer !== null'),'pending result timer');
  await app.run('stopRecording()');
  assert.equal(app.run('pending.length'),2);
  await app.runTimeout(2000);
  assert.equal(uploads,1);
  assert.equal(lookups,2);
  await app.runTimeout(4000);
  await until(() => app.run('pending.length === 0'),'final result and tail');
  assert.equal(uploads,2,'only the distinct final tail is uploaded after recovery');
  assert.equal(lookups,3);
  assert.equal(app.run('current.segments.length'),1);
  assert.equal(app.run('current.recording_finalized'),true);
  assert.equal(app.element('transcript').children[0].children[1].textContent,'회수한 마지막 문장');
});

test('pending CLOVA lookups have a finite retry budget and preserve an unresolved WAV', async () => {
  let uploads = 0, lookups = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    if (url.endsWith('/chunks')) { uploads += 1; throw new TypeError('no response'); }
    if (url.endsWith('/result')) { lookups += 1; return response({state:'pending'}); }
    return response({});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => app.run('retryTimer !== null'),'bounded result polling');
  for (const delay of [2000,4000,8000,15000,15000]) await app.runTimeout(delay);
  assert.equal(uploads,1);
  assert.equal(lookups,6);
  assert.equal(app.run('retryTimer'),null);
  assert.ok(app.run('sendError'));
  assert.equal(app.run('pending.length'),1);
  assert.equal(app.run('liveQueue.chunks.size'),1);
});

test('restored blocked CLOVA audio is acknowledged directly after GET confirmation without a queued transition', async () => {
  let saved = false, uploads = 0, lookups = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    if (url.endsWith('/chunks')) { uploads += 1; throw new TypeError('response lost'); }
    if (url.endsWith('/result')) {
      lookups += 1;
      return response(saved ? {state:'done',result:{segments:[{id:'saved',start:0,end:7,text:'복구 문장'}],
        recording_available:true,recording_finalized:false}} : {state:'unknown'});
    }
    return response({});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => app.run('!!sendError'),'initial blocked result');
  const queue = app.run('liveQueue');
  queue.recoverOwner = async () => ({sessions:[...queue.sessions.values()],chunks:[...queue.chunks.values()],
    inflightChunks:[],stats:await queue.getStats('user-alpha')});
  queue.markChunkQueued = async () => { throw new Error('a confirmed blocked row must not become queued'); };
  app.run('capture=null; captureSession=null; recording=false; pending=[]; liveSessions.clear(); sendError=""');
  saved = true;
  await app.run('recoverDurableLiveAudio(user)');
  await until(() => app.run('pending.length === 0'),'blocked result acknowledgement');
  assert.equal(uploads,1);
  assert.equal(lookups,2);
  assert.equal(queue.chunks.size,0);
  assert.equal(app.element('transcript').children[0].children[1].textContent,'복구 문장');
});

test('untrusted retry flags and unavailable or malformed CLOVA lookup responses never authorize another POST', async () => {
  for (const scenario of ['generic429','string-flag','wrong-code','wrong-status','unknown','old-server','malformed','malformed-final','failed-lookup']) {
    let uploads = 0;
    const app = setup(async (url,options = {}) => {
      if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
        asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
      if (url.endsWith('/chunks')) {
        uploads += 1;
        if (scenario === 'generic429') return response({detail:'busy'},429);
        if (scenario === 'string-flag') return response({code:'chunk_not_started',safe_to_retry:'true'},429);
        if (scenario === 'wrong-code') return response({code:'other',safe_to_retry:true},429);
        if (scenario === 'wrong-status') return response({code:'chunk_not_started',safe_to_retry:true},503);
        throw new TypeError('response lost');
      }
      if (url.endsWith('/result')) {
        if (scenario === 'old-server') return response({detail:'not found'},404);
        if (scenario === 'malformed') return response({state:'done',result:{segments:[{text:'불완전'}],recording_finalized:true}});
        if (scenario === 'malformed-final') return response({state:'done',result:{segments:[],recording_finalized:'true'}});
        if (scenario === 'failed-lookup') throw new TypeError('lookup failed');
        return response({state:'unknown'});
      }
      return response({});
    });
    app.run('transcriptionProviders.clova.configured=true');
    app.element('asr-provider').value = 'clova';
    await app.run('startRecording()');
    app.microphone().callbacks.onChunk(chunk(0));
    await until(() => app.run('!!sendError'),scenario);
    assert.equal(uploads,1,scenario);
    assert.equal(app.run('retryTimer'),null,scenario);
    assert.equal(app.run('liveQueue.chunks.size'),1,scenario);
    assert.equal(app.run('current.segments.length'),0,scenario);
    assert.notEqual(app.run('current.recording_finalized'),true,scenario);
  }
});

test('changing accounts during an IndexedDB inflight write cannot send the old audio with a new token', async () => {
  let uploads = 0;
  const write = deferred();
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    if (url.endsWith('/chunks')) uploads += 1;
    return response({segments:[]});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  const queue = app.run('liveQueue'), original = queue.markChunkInflight.bind(queue);
  let writing = false;
  queue.markChunkInflight = async (...args) => { writing = true; await write.promise; return original(...args); };
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => writing,'slow inflight storage');
  app.run("user='user-beta'; token='beta-token'; sendError=''");
  write.resolve();
  await until(() => app.run('!sending'),'stale upload preflight');
  assert.equal(uploads,0);
  assert.equal([...queue.chunks.values()][0].owner,'user-alpha');
  assert.equal([...queue.chunks.values()][0].state,'queued');
  assert.equal(app.run('sendError'),'');
});

test('a late CLOVA lookup and a pending lookup timer cannot affect another account', async () => {
  for (const phase of ['response','timer']) {
    const lookup = deferred();
    let lookups = 0, uploads = 0;
    const app = setup(async (url,options = {}) => {
      if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
        asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
      if (url.endsWith('/chunks')) { uploads += 1; throw new TypeError('response lost'); }
      if (url.endsWith('/result')) { lookups += 1; return phase === 'response' ? lookup.promise : response({state:'pending'}); }
      return response({});
    });
    app.run('transcriptionProviders.clova.configured=true');
    app.element('asr-provider').value = 'clova';
    await app.run('startRecording()');
    app.microphone().callbacks.onChunk(chunk(0));
    await until(() => phase === 'response' ? lookups === 1 : app.run('retryTimer !== null'),'old result lookup');
    app.run("user='user-beta'; token='beta-token'; sendError=''");
    if (phase === 'response') {
      lookup.resolve(response({state:'done',result:{segments:[{id:'old',start:0,end:7,text:'다른 계정의 늦은 문장'}],recording_finalized:true}}));
      await until(() => app.run('!sending'),'late lookup rejection');
    } else await app.runTimeout(2000);
    assert.equal(uploads,1);
    assert.equal(lookups,1);
    assert.equal(app.run('current.segments.length'),0);
    assert.equal(app.run('liveQueue.chunks.size'),1);
    assert.equal(app.run('sendError'),'');
    assert.doesNotMatch(JSON.stringify(app.element('transcript').children),/다른 계정의 늦은 문장/);
  }
});

test('an ambiguous CLOVA chunk failure never auto-retries or silently switches to Qwen', async () => {
  let uploads = 0, lookups = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) return response({
      id:options.headers.get('X-Lecture-Id'),title:'클로바 수업',language:'ko',asr_provider:'clova',created_at:new Date().toISOString(),segments:[],
    },201);
    if (url.endsWith('/result')) { lookups += 1; return response({state:'unknown'}); }
    if (url.endsWith('/chunks')) {
      uploads += 1;
      throw new TypeError('connection lost after upload');
    }
    return response({});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  app.microphone().tail = chunk(6,2.2,2,true);
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => app.run('!!sendError && !sending'),'unknown CLOVA result and durable blocked state');

  assert.equal(uploads,1);
  assert.equal(lookups,1);
  assert.equal(app.run('liveQueue.chunks.values().next().value.state'),'blocked');
  assert.equal(app.run('liveQueue.markInflightCalls'),1,
    'CLOVA ambiguity is persisted before the request leaves the browser');
  assert.equal(app.run('retryTimer'),null);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 1000 || timer.delay === 2000));
  assert.equal(app.run('recording'),true,'an ambiguous response must not end microphone capture');
  assert.equal(app.microphone().stopCalls,undefined);
  assert.equal(app.run('pending.length'),1,'the failed chunk stays available for manual recovery');
  assert.ok(app.run("pending.every(item => item.asrProvider === 'clova')"));
  assert.match(app.run('sendError'),/자동 재전송하지 않았/);
  assert.match(app.run('sendError'),/중복 기록/);
  assert.doesNotMatch(app.run('sendError'),/비용|과금/);
  assert.match(app.element('retry').textContent,/위험 이해.*수동 재전송/);
  assert.doesNotMatch(app.run('sendError'),/Qwen으로/);

  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.microphone().stopCalls,1);
  assert.equal(app.run('pending.length'),2,'an explicit stop appends the final tail behind the blocked chunk');
});

test('ambiguous CLOVA failure retains one WAV across delayed durable writes and real SHA-256 completion', async () => {
  for (const phase of ['inflight-write','digest','blocked-write']) {
    const entered = deferred(), release = deferred();
    let uploads = 0, lookups = 0, digestCalls = 0, enteredGate = false, settled = false;
    const gate = async () => { enteredGate = true; entered.resolve(); await release.promise; };
    const cryptoImplementation = {
      randomUUID:() => webcrypto.randomUUID(),
      getRandomValues:array => webcrypto.getRandomValues(array),
      subtle:{digest:async (...args) => {
        digestCalls += 1;
        if (phase === 'digest') await gate();
        return webcrypto.subtle.digest(...args);
      }},
    };
    const app = setup(async (url,options = {}) => {
      if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),
        title:'합성 클로바 수업',language:'ko',asr_provider:'clova',created_at:new Date().toISOString(),segments:[]},201);
      if (url.endsWith('/chunks')) { uploads += 1; throw new TypeError('synthetic response lost'); }
      if (url.endsWith('/result')) {
        lookups += 1;
        assert.match(options.headers.get('X-Chunk-Payload-SHA256'),/^[a-f0-9]{64}$/);
        assert.notEqual(options.method,'POST');
        return response({state:'unknown'});
      }
      return response({});
    },{cryptoImplementation});
    app.run('transcriptionProviders.clova.configured=true');app.element('asr-provider').value='clova';
    await app.run('startRecording()');
    const queue=app.run('liveQueue');
    if (phase !== 'digest') {
      const method=phase==='inflight-write'?'markChunkInflight':'markChunkBlocked';
      const original=queue[method].bind(queue);
      queue[method]=async (...args)=>{await gate();return original(...args);};
    }
    const audio=chunk(0);
    app.microphone().callbacks.onChunk(audio);
    const complete=until(()=>app.run('!!sendError && !sending'),`${phase}: durable unknown-result completion`).then(()=>{settled=true;});
    // Observe early rejections as well as successful completion; finally still
    // releases the external operation if an assertion fails.
    void complete.catch(()=>{});
    try {
      await until(()=>enteredGate,`${phase}: external operation entered`);
      await entered.promise;
      for(let turns=0;turns<250;turns+=1) await tick();
      assert.equal(settled,false,'the old 200-turn budget can be exhausted while legitimate work is pending');
      assert.equal(app.run('recording'),true);assert.equal(queue.chunks.size,1);
      assert.equal(uploads,phase==='inflight-write'?0:1);
      assert.equal(lookups,phase==='blocked-write'?1:0);
      assert.equal(app.run('retryTimer'),null);
    } finally { release.resolve(); }
    await complete;
    assert.equal(uploads,1);assert.equal(lookups,1);assert.equal(digestCalls,1);
    assert.equal(queue.markInflightCalls,1);assert.equal(queue.chunks.size,1);
    const stored=[...queue.chunks.values()][0];assert.equal(stored.state,'blocked');
    assert.deepEqual(new Uint8Array(await stored.blob.arrayBuffer()),new Uint8Array(await audio.blob.arrayBuffer()));
    assert.equal(app.run('pending.length'),1);assert.equal(app.run('current.segments.length'),0);
    assert.equal(app.run('pending[0].asrProvider'),'clova');assert.equal(app.run('retryTimer'),null);
    assert.equal(app.run('recording'),true);assert.equal(app.microphone().stopCalls,undefined);
    assert.match(app.run('sendError'),/자동 재전송하지 않았/);
    assert.match(app.element('retry').textContent,/위험 이해.*수동 재전송/);
    await app.run('drain()');assert.equal(uploads,1,'even another drain cannot replay an ambiguous chunk');
    await app.run('stopRecording()');
  }
});

test('asynchronous test waits retain a bounded monotonic deadline for work that never settles', async () => {
  const started=hostPerformance.now();
  await assert.rejects(until(()=>false,'synthetic stalled operation',{timeoutMs:20}),/synthetic stalled operation did not settle/);
  assert.ok(hostPerformance.now()-started>=20);
});

test('re-login preserves the manual CLOVA retry gate until the user explicitly resubmits', async () => {
  let uploads = 0;
  let lecture = null;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/auth/login')) return response({token:'renewed-clova-token',user:{username:'user-alpha',is_admin:false}});
    if (url.endsWith('/lectures') && options.method === 'POST') {
      lecture = {id:options.headers.get('X-Lecture-Id'),title:'클로바 수업',language:'ko',asr_provider:'clova',created_at:new Date().toISOString(),segments:[]};
      return response({...lecture},201);
    }
    if (url.endsWith('/lectures') && !options.method) return response([{...lecture,segment_count:0}]);
    if (url.endsWith('/imports')) return response([]);
    if (url.endsWith('/status')) return response({model_state:'ready',transcription_providers:{qwen:{configured:true},clova:{configured:true}}});
    if (url.endsWith('/result')) return response({state:'unknown'});
    if (lecture && url.endsWith(`/lectures/${lecture.id}/chunks`)) {
      uploads += 1;
      if (uploads === 1) throw new TypeError('ambiguous response loss');
      return response({segments:[],recording_available:true,recording_finalized:options.headers.get('X-Final-Chunk') === 'true'});
    }
    return response({});
  });
  app.run('transcriptionProviders.clova.configured=true');
  app.element('asr-provider').value = 'clova';
  await app.run('startRecording()');
  app.microphone().tail = chunk(6,2.2,2,true);
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => app.run('!!sendError'),'manual CLOVA recovery gate');
  assert.equal(uploads,1);
  assert.ok(app.run("sendError.includes('자동 재전송하지 않았')"));
  assert.equal(app.run('recording'),true);
  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.run('pending.length'),2);

  app.run("token=''; showLogin(false)");
  app.element('username').value = 'user-alpha';
  app.element('password').value = 'same-account-password';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  for (let index = 0; index < 4; index += 1) await tick();
  assert.equal(app.run('token'),'renewed-clova-token');
  assert.equal(uploads,1,'successful re-login must not implicitly resend an ambiguous CLOVA chunk');
  assert.equal(app.run('pending.length'),2);
  assert.ok(app.run("sendError.includes('중복 기록')"));
  assert.doesNotMatch(app.run('sendError'),/비용|과금/);
  assert.match(app.element('retry').textContent,/위험 이해.*수동 재전송/);

  app.element('retry').onclick();
  for (let index = 0; index < 5; index += 1) await tick();
  assert.equal(uploads,3,'only the explicit manual action sends the failed head and saved final tail');
  assert.equal(app.run('pending.length'),0);
  assert.equal(app.run('sendError'),'');
});

test('CLOVA provider failures including HTTP 424 remain manual while Qwen retry policy is unchanged', async () => {
  const app = setup(async () => response({}));
  assert.equal(app.run('PERMANENT_UPLOAD_STATUSES.has(424)'),true);
  assert.equal(app.run("retryableUpload({status:424})"),false);
  assert.equal(app.run("retryableUpload({status:503})"),true);
  assert.equal(app.run("retryableUpload({transient:true})"),true);
});

test('an in-flight idempotent 409 retries while a changed-payload 409 remains manual', async () => {
  let attempt = 0;
  const ids = [];
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      return response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    attempt += 1;
    ids.push(options.headers.get('X-Chunk-Id'));
    if (attempt === 1) return response({detail:'이 음성을 이미 처리하고 있습니다.'},409,{'Retry-After':'2'});
    return response({segments:[]});
  });

  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  assert.equal(attempt, 1);
  assert.equal(app.run('sendError'), '');
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 2000));
  await app.runTimeout(2000);
  assert.equal(attempt, 2);
  assert.equal(app.run('pending.length'), 0);
  assert.equal(ids[0], ids[1]);

  const rejected = setup(async (url,options = {}) => url.endsWith('/lectures')
    ? response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201)
    : response({detail:'같은 ID의 내용이 다릅니다.'},409));
  await rejected.run('startRecording()');
  rejected.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  assert.match(rejected.run('sendError'), /자동 재시도를 멈췄어요/);
  assert.equal(rejected.run('retryTimer'), null);
  assert.equal(rejected.run("retryableUpload({status:502})"), true);
  assert.equal(rejected.run("retryableUpload({status:530})"), true);
  assert.equal(rejected.run("retryableUpload({status:408})"), true);
  assert.equal(rejected.run("retryableUpload({status:422})"), false);
  assert.equal(rejected.run("retryableUpload({status:507})"), false);
});

test('permanent upload failure blocks sending without ending capture and retains an explicit final tail', async () => {
  const uploads = [];
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      return response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    uploads.push(options.headers.get('X-Chunk-Id'));
    return response({detail:'녹음 저장 공간이 부족합니다.'},507);
  });

  await app.run('startRecording()');
  app.microphone().tail = chunk(6, 2.2, 2, true);
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();

  assert.equal(app.run('recording'), true);
  assert.equal(app.microphone().stopCalls, undefined);
  assert.equal(uploads.length, 1);
  assert.equal(app.run('pending.length'), 1);
  assert.equal(app.run('retryTimer'), null);
  assert.match(app.run('sendError'), /자동 재시도를 멈췄어요/);

  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.microphone().stopCalls, 1);
  assert.equal(app.run('pending.length'), 2);
  assert.notEqual(app.run('pending[0].id'), app.run('pending[1].id'));
  assert.equal(app.run('pending[1].startSeconds'), 6);
  assert.equal(app.run('pending[1].overlapSeconds'), 2);
  assert.equal(app.run('pending[1].final'), true);
  assert.equal(app.run('retryTimer'), null);
  assert.ok(![...app.timeouts.values()].some(timer => timer.delay === 1000 || timer.delay === 2000));
  assert.match(app.run('sendError'), /자동 재시도를 멈췄어요/);
  assert.equal(app.element('queue-warning').hidden, false);
});

test('temporary outage keeps recording after the former queue cap and persists an explicit final tail', async () => {
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) {
      return response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    return response({detail:'잠시 사용할 수 없습니다.'},503);
  });

  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  for (const start of [6,14,22,30,38,46]) {
    app.microphone().callbacks.onChunk(chunk(start, 10, 2));
  }
  assert.equal(app.run('pending.length'), 7);
  assert.equal(app.run('recording'), true);
  assert.equal(app.microphone().stopCalls, undefined);

  app.microphone().tail = chunk(70, 2.2, 2, true);
  app.microphone().callbacks.onChunk(chunk(54, 10, 2));
  app.microphone().callbacks.onChunk(chunk(62, 10, 2));
  await tick(); await tick();
  assert.equal(app.run('recording'), true);
  assert.equal(app.microphone().stopCalls, undefined);
  assert.equal(app.run('pending.length'), 9, 'queued audio may grow beyond the former in-memory stop threshold');

  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.run('pending.length'), 10, 'the explicit final tail is retained after all outage chunks');
  assert.equal(app.run('pending[9].startSeconds'), 70);
  assert.equal(app.run('pending[9].overlapSeconds'), 2);
  assert.equal(app.run('pending[9].final'), true);
});

test('repeated 503 responses keep capture alive and cap delay rather than the retry count', async () => {
  let uploads = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) {
      return response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (url.includes('/chunks')) {
      uploads += 1;
      return response({detail:'모델 처리 실패'},503,{'Retry-After':'0'});
    }
    return response({});
  });
  await app.run('startRecording()');
  app.microphone().tail = chunk(6, 2.2, 2, true);
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  for (let retry = 0; retry < 8; retry += 1) {
    const delay = Math.min(30000, 1000 * (2 ** retry));
    await app.runTimeout(delay);
  }
  assert.equal(uploads, 9, 'one initial attempt plus eight automatic retries');
  assert.notEqual(app.run('retryTimer'), null);
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 30000));
  assert.equal(app.run('sendError'), '');
  assert.equal(app.run('recording'), true);
  assert.equal(app.microphone().stopCalls, undefined);
  assert.equal(app.run('pending.length'), 1);

  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.run('pending.length'), 2, 'the failed head and explicit final microphone tail remain recoverable');
});

test('skipping the last failed chunk finalizes the saved recording and applies its flags', async () => {
  const finalization = deferred();
  const lectureId = webcrypto.randomUUID();
  let request;
  const app = setup((url, options = {}) => {
    if (url.endsWith(`/lectures/${lectureId}/recording-finalize`)) {
      request = {url,options};
      return finalization.promise;
    }
    return response({});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
    renderCurrent();
  `);
  await app.seedDurableFailedFinal({lectureId});
  await app.run('skipFailedChunk()');
  await tick();
  assert.equal(app.run('pending.length'), 0);
  assert.equal(app.run('recordingFinalizePending'), true);
  assert.equal(app.run('isBusy()'), true);
  for (const id of ['new-note','record-button','recording-file','download','delete-lecture']) {
    assert.equal(app.element(id).disabled, true, `${id} must lock while finalizing the recording`);
  }
  assert.equal(app.element('recording-download').disabled,false,'read-only local rescue is independent of finalization');
  assert.equal(request.url, `https://classroom.example/lectures/${lectureId}/recording-finalize`);
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers.get('Authorization'), 'Bearer old-token');
  assert.ok([...app.timeouts.values()].some(timer => timer.delay === 60000),
    'tail inference gets the same timeout budget as a regular audio chunk');

  finalization.resolve(response({recording_available:true,recording_finalized:true}));
  await tick(); await tick();
  assert.equal(app.run('recordingFinalizePending'), false);
  assert.equal(app.run('current.recording_available && current.recording_finalized'), true);
  assert.equal(app.run('lectures[0].recording_available && lectures[0].recording_finalized'), true);
  assert.equal(app.element('recording-download').disabled, false);
  assert.match(app.element('notice').textContent, /WAV로 마무리/);
});

test('a skipped final chunk starts its reserved correction once after recording finalization', async () => {
  const lectureId = webcrypto.randomUUID();
  let finalizations = 0, correctionPosts = 0, correctionSawRecoveredTail = false;
  let app;
  app = setup((url, options = {}) => {
    if (url.endsWith(`/lectures/${lectureId}/recording-finalize`)) {
      finalizations += 1;
      return response({recording_available:true,recording_finalized:true,
        segments:[{id:'recovered-tail',start:4,end:4.6,text:'복구된 마지막 문장'}]});
    }
    if (url.endsWith(`/lectures/${lectureId}/correction`) && options.method === 'POST') {
      correctionPosts += 1;
      correctionSawRecoveredTail = app.run("current.segments.some(segment => segment.id === 'recovered-tail')");
      return response({lecture_id:lectureId,status:'queued'});
    }
    return response({});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'복구 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'saved',start:0,end:4,text:'저장된 원문'}],recording_available:true,recording_finalized:false};
    lectures=[current];
  `);
  await app.seedDurableFailedFinal({lectureId});
  app.run(`
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,status:'scheduled'});
  `);
  await app.run('skipFailedChunk()');
  await tick(); await tick(); await tick();
  assert.equal(finalizations,1);
  assert.equal(correctionPosts,1);
  assert.equal(correctionSawRecoveredTail,true,'the reserved correction must start after the recovered tail is merged');
  assert.equal(app.run("current.segments.filter(segment => segment.id === 'recovered-tail').length"),1);
  assert.equal(app.run('scheduledCorrections.size'),0);
  assert.equal(app.run('current.recording_finalized'),true);
  app.run(`applyRecordingFlags(${JSON.stringify(lectureId)},{recording_available:true,recording_finalized:true,segments:[{id:'recovered-tail',start:4,end:4.6,text:'복구된 마지막 문장'}]})`);
  await tick(); await tick();
  assert.equal(correctionPosts,1,'repeated finalized state must not submit the reservation again');
  assert.equal(app.run("current.segments.filter(segment => segment.id === 'recovered-tail').length"),1,
    'an idempotent finalization response must not duplicate the recovered segment');
});

test('a failed recording finalization leaves its correction reserved without locking recovery controls', async () => {
  const lectureId = webcrypto.randomUUID();
  const newerCaptureId = webcrypto.randomUUID();
  const newerLectureId = webcrypto.randomUUID();
  let correctionPosts = 0;
  const app = setup((url, options = {}) => {
    if (url.endsWith('/recording-finalize')) {
      return response({detail:'마지막 음성 인식을 마무리하지 못했습니다.'},503,{'Retry-After':'5'});
    }
    if (url.endsWith(`/lectures/${lectureId}/correction`) && options.method === 'POST') {
      correctionPosts += 1;
      return response({lecture_id:lectureId,status:'queued'});
    }
    return response({});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'복구 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'saved',start:0,end:4,text:'저장된 원문'}],recording_available:true,recording_finalized:false};
    lectures=[current];
  `);
  await app.seedDurableFailedFinal({lectureId});
  app.run(`
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,finalized:false,status:'scheduled'});
  `);
  await app.run('skipFailedChunk()');
  await tick(); await tick();
  assert.equal(app.run('recordingFinalizePending'),false);
  assert.equal(app.run(`scheduledCorrections.get(${JSON.stringify(lectureId)}).status`),'scheduled');
  assert.equal(app.run('hasOwnerLockedWork()'),true,'same-owner reauthentication must retain the reservation');
  assert.equal(app.run('isBusy()'),false,'a stored retryable reservation must not deadlock the workspace');
  assert.equal(app.element('recording-finalize').disabled,false,'the explicit finalization button retries separately from WAV download');
  assert.equal(app.element('logout').disabled,false);

  app.run(`
    captureSession={id:${JSON.stringify(newerCaptureId)},owner:user,asrProvider:'qwen',lecture:{id:${JSON.stringify(newerLectureId)},segments:[]}};
    applyRecordingFlags(${JSON.stringify(lectureId)},{recording_available:true,recording_finalized:true,segments:[]});
  `);
  await tick(); await tick();
  assert.equal(correctionPosts,1,'a later successful finalization still honors the old reservation');
  assert.equal(app.run('captureSession.id'),newerCaptureId,'finalizing an older lesson must not release a newer capture');
});

test('skipping the only failed chunk never claims that a server WAV exists', async () => {
  const lectureId = webcrypto.randomUUID();
  const app = setup((url) => url.endsWith('/recording-finalize')
    ? response({recording_available:false,recording_finalized:true})
    : response({}));
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:false,recording_finalized:false};
    lectures=[{...current}];
  `);
  await app.seedDurableFailedFinal({lectureId,error:'첫 조각 실패'});
  await app.run('skipFailedChunk()');
  await tick(); await tick();
  assert.equal(app.run('current.recording_available'), false);
  assert.equal(app.run('current.recording_finalized'), true);
  assert.equal(app.element('recording-download').disabled, false);
  assert.match(app.element('recording-download').textContent,/기기에 남은/);
  assert.match(app.element('notice').textContent, /내려받을 녹음은 없습니다/);
  assert.doesNotMatch(app.element('notice').textContent, /WAV로 마무리/);
});

test('recording finalization retries once after a transient response loss', async () => {
  const lectureId = webcrypto.randomUUID();
  let finalizeCalls = 0;
  let app;
  app = setup(async (url) => {
    if (!url.endsWith('/recording-finalize')) return response({});
    finalizeCalls += 1;
    if (finalizeCalls === 1) throw app.run("new TypeError('response lost')");
    return response({recording_available:true,recording_finalized:true});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
  `);
  await app.seedDurableFailedFinal({lectureId});
  await app.run('skipFailedChunk()');
  await tick(); await tick();
  assert.equal(finalizeCalls, 2);
  assert.equal(app.run('recordingFinalizePending'), false);
  assert.equal(app.run('current.recording_finalized'), true);
});

test('recording finalization waits for an already running deterministic guard', async () => {
  const lectureId = webcrypto.randomUUID();
  let finalizeCalls = 0;
  const app = setup(async (url) => {
    if (!url.endsWith('/recording-finalize')) return response({});
    finalizeCalls += 1;
    if (finalizeCalls === 1) {
      return response({detail:'마지막 음성을 이미 처리하고 있습니다.'},409,{'Retry-After':'2'});
    }
    return response({recording_available:true,recording_finalized:true,segments:[]});
  });
  app.run(`
    current={id:${JSON.stringify(lectureId)},title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
  `);
  await app.seedDurableFailedFinal({lectureId});
  await app.run('skipFailedChunk()');
  await tick(); await tick();
  assert.equal(finalizeCalls,1);
  assert.equal(app.run('recordingFinalizePending'),true);
  await app.runTimeout(2000);
  assert.equal(finalizeCalls,2);
  assert.equal(app.run('recordingFinalizePending'),false);
  assert.equal(app.run('current.recording_finalized'),true);
});

test('a stale recording-finalize response cannot alter the next account', async () => {
  const finalization = deferred();
  const oldLectureId = webcrypto.randomUUID();
  const nextLectureId = webcrypto.randomUUID();
  const app = setup((url) => url.endsWith('/recording-finalize') ? finalization.promise : response({}));
  app.run(`
    current={id:${JSON.stringify(oldLectureId)},title:'이전 수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
  `);
  await app.seedDurableFailedFinal({lectureId:oldLectureId});
  await app.run('skipFailedChunk()');
  await tick();
  assert.equal(app.run('recordingFinalizePending'), true);
  app.run(`
    showLogin(); const previousUser=user; token='next-token'; user='next-user';
    if (previousUser !== user) { scheduledCorrections.clear(); scrubAccountWorkspace(); }
    current={id:${JSON.stringify(nextLectureId)},title:'다음 수업',created_at:'2026-01-02T00:00:00Z',segments:[],recording_available:false,recording_finalized:false};
    lectures=[{...current}];
  `);
  finalization.resolve(response({recording_available:true,recording_finalized:true,
    segments:[{id:'old-tail',start:3,end:4,text:'다른 계정에 보이면 안 됨'}]}));
  await tick(); await tick();
  assert.equal(app.run('current.id'), nextLectureId);
  assert.equal(app.run('current.recording_available || current.recording_finalized'), false);
  assert.equal(app.run("current.segments.some(segment => segment.id === 'old-tail')"),false);
  assert.equal(app.run('lectures[0].id'), nextLectureId);
  assert.equal(app.run('recordingFinalizePending'), false);
});

test('a failed head requires a separate download request and save confirmation before skipping', async () => {
  let uploads = 0;
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) {
      return response({id:options.headers.get('X-Lecture-Id'),title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    uploads += 1;
    if (uploads === 1) return response({detail:'결정적 모델 오류'},422);
    return response({segments:[]});
  });
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();
  assert.equal(app.run('recording'),true);
  app.microphone().tail = chunk(5,3,3,true);
  await app.run('stopRecording()');
  await tick(); await tick();
  assert.equal(app.run('pending.length'), 2);
  assert.ok(app.run('sendError'));
  await app.run('saveFailedChunk()');
  await tick();
  assert.equal(app.run('pending.length'), 2, 'asking for a download never deletes audio');
  assert.equal(app.run('pending[0].downloadRequested'), true);
  assert.equal(app.element('skip-failed').hidden, false);
  app.run('skipFailedChunk()');
  await tick(); await tick();
  assert.equal(app.created('a').clicked, true);
  assert.match(app.created('a').download, /처리실패\.wav$/);
  assert.equal(uploads, 2, 'the next queued chunk is sent after the preserved head is skipped');
  assert.equal(app.run('pending.length'), 0);
  assert.equal(app.run('sendError'), '');
});

test('a failed live chunk uses its own lecture title while a past note is open', async () => {
  const app = setup(async () => response({}));
  app.run(`
    const live={id:'live-failed',title:'현재 수업',created_at:'2026-01-02T00:00:00Z',segments:[]};
    const past={id:'past-open',title:'지난 수업',created_at:'2026-01-01T00:00:00Z',segments:[]};
    current=past; lectures=[live,past]; captureSession={id:'live-capture',lecture:live};
    pending=[{blob:TestEncodeWav(new Float32Array(16000)),owner:user,startSeconds:0,durationSeconds:1,
      overlapSeconds:0,final:false,id:'failed-live',lectureId:live.id}];
    sendError='전송 실패';
  `);
  await app.run('saveFailedChunk()');
  assert.match(app.created('a').download,/^현재 수업_/);
  assert.doesNotMatch(app.created('a').download,/지난 수업/);
});

async function localAudioExportFixture() {
  const app = setup(async()=>{throw new Error('export must not use the network');});
  await app.run(`(async()=>{
    await openLiveQueue();
    const captureId='71000000-0000-4000-8000-000000000001';
    const session={id:captureId,owner:user,title:'합성 구조 수업',createdAt:Date.now(),nextRuntimeSequence:2,
      durable:true,asrProvider:'clova',persistChain:Promise.resolve(),storageFailed:false};
    liveSessions.set(captureId,session); captureSession=session;
    const first=TestEncodeWav(new Float32Array(128000));
    const next=new Float32Array(176000);next.fill(.25,48000);
    const second=TestEncodeWav(next);
    liveQueue.sessions.set(captureId,{...session,persistChain:undefined});
    liveQueue.chunks.set('72000000-0000-4000-8000-000000000001',{
      id:'72000000-0000-4000-8000-000000000001',captureId,owner:user,sequence:0,
      startSamples:0,durationSamples:128000,overlapSamples:0,blob:first,state:'blocked'});
    pending=[{id:'72000000-0000-4000-8000-000000000001',captureId,owner:user,sequence:0,
      startSeconds:0,durationSeconds:8,overlapSeconds:0,blob:first,durable:true},
      {id:'72000000-0000-4000-8000-000000000002',captureId,owner:user,sequence:1,
      startSeconds:5,durationSeconds:11,overlapSeconds:3,blob:second}];
    capture={recording:true,paused:false,inputHealth:{status:'receiving'},
      snapshotForExport:()=>({sequence:2,startSamples:208000,durationSamples:80000,overlapSamples:48000,
        blob:TestEncodeWav(new Float32Array(80000).fill(.25))})};
    recording=true; sendError='합성 전송 보류';updateControls();
  })()`);
  return app;
}

test('local rescue combines all remaining silence, speech and current PCM without stopping or settling the queue',async()=>{
  const app=await localAudioExportFixture();
  await app.run('prepareLocalAudioExport()');
  const links=app.createdAll('a').filter(link=>link.download?.includes('_기기보관_'));
  assert.equal(links.length,1);
  const data=new DataView(await app.objectUrlBlob(links[0].href).arrayBuffer());
  assert.equal(data.byteLength,44+18*32000,'all remaining unique samples, not only the silent head');
  assert.equal(data.getInt16(44,true),0);
  assert.equal(data.getInt16(44+8*32000,true),8192);
  assert.equal(data.getInt16(data.byteLength-2,true),8192);
  assert.equal(app.run('pending.length'),2);assert.equal(app.run('liveQueue.chunks.size'),1);
  assert.equal(app.run('pending.some(row=>row.downloadRequested)'),false);
  assert.equal(app.run('recording'),true);assert.equal(app.run('capture.recording'),true);
  assert.equal(app.run('sendError'),'합성 전송 보류');
  assert.equal(app.element('local-audio-dialog').open,true);
  assert.equal(app.element('pause-button').disabled,false);
  assert.match(app.element('local-audio-status').textContent,/1개 파일/);
  app.run('clearLocalAudioExports()');
  assert.equal(app.objectUrlBlob(links[0].href),undefined);
  assert.equal(app.run('pending.length'),2);
});

test('local export has no sendError requirement and tolerates IDB failure with an explicit RAM-only warning',async()=>{
  const app=await localAudioExportFixture();
  app.run("sendError='';liveQueue.readExportSnapshot=async()=>{throw new Error('synthetic storage unavailable')};updateControls();");
  assert.equal(app.element('save-failed').hidden,true);
  assert.equal(app.element('local-audio-open').disabled,false);
  await app.run('prepareLocalAudioExport()');
  assert.equal(app.createdAll('a').filter(link=>link.download?.includes('_기기보관_')).length,1);
  assert.match(app.element('local-audio-status').textContent,/메모리에서 읽은 음성만/);
  assert.equal(app.run('pending.length'),2);
});

test('expired login can rescue its already authenticated locked audio but no cached or selected identity is accepted',async()=>{
  const app=await localAudioExportFixture();
  app.run("token='';showLogin(false);");
  assert.equal(app.element('auth-local-audio-open').hidden,false);
  await app.run('prepareLocalAudioExport()');
  assert.equal(app.createdAll('a').filter(link=>link.download?.includes('_기기보관_')).length,1);
  app.run("clearLocalAudioExports();user='';updateControls();");
  await app.run('prepareLocalAudioExport()');
  assert.equal(app.element('local-audio-dialog').open,false);
});

test('local export drops a late snapshot after owner change and never offers a foreign owner WAV',async()=>{
  const app=await localAudioExportFixture(),gate=deferred();
  const queue=app.run('liveQueue');queue.readExportSnapshot=()=>gate.promise;
  const work=app.run('prepareLocalAudioExport()');
  app.run("user='user-beta';token='new-session';clearLocalAudioExports();");
  gate.resolve({sessions:[],chunks:[],snapshots:[]});await work;
  assert.equal(app.createdAll('a').filter(link=>link.download?.includes('_기기보관_')).length,0);
  assert.equal(app.element('local-audio-files').children.length,0);
  const next=await localAudioExportFixture();
  next.run("liveQueue.readExportSnapshot=async()=>({sessions:[],chunks:[{owner:'other-owner'}],snapshots:[]});");
  await next.run('prepareLocalAudioExport()');
  assert.match(next.element('local-audio-status').textContent,/계정 정보/);
  assert.equal(next.createdAll('a').filter(link=>link.download?.includes('_기기보관_')).length,0);
});

test('invalid zero-byte RAM WAV cannot be downloaded or grant skip permission',async()=>{
  const app=await localAudioExportFixture();
  app.run("pending[0].blob=new Blob([],{type:'audio/wav'});");
  await app.run('saveFailedChunk()');
  assert.equal(app.created('a'),undefined);
  assert.equal(app.run('pending[0].downloadRequested'),undefined);
  assert.equal(app.run('pending.length'),2);
  assert.match(app.element('notice').textContent,/원본은 지우지 않았습니다/);
});

test('bulk rescue can still save later sound with an explicit missing warning when the first WAV is unreadable',async()=>{
  const app=await localAudioExportFixture();
  app.run("pending[0].blob=new Blob([],{type:'audio/wav'});liveQueue.chunks.clear();");
  await app.run('prepareLocalAudioExport()');
  const links=app.createdAll('a').filter(link=>link.download?.includes('_기기보관_'));
  assert.equal(links.length,1);
  assert.equal(app.objectUrlBlob(links[0].href).size,44+13*32000);
  assert.match(app.element('local-audio-status').textContent,/읽지 못한 음성 항목 1개/);
  assert.match(app.element('local-audio-status').textContent,/누락이 있습니다/);
  const warning=app.element('local-audio-status').textContent;
  links[0].onclick({preventDefault(){throw new Error('same owner should download');}});
  assert.equal(app.element('local-audio-status').textContent,warning,'download notice must not erase known missing audio');
  assert.equal(app.run('pending.length'),2);assert.equal(app.run('pending[0].blob.size'),0);
  assert.equal(app.run('pending.some(row=>row.downloadRequested)'),false);
});

test('blocked upload messaging never claims continuing microphone capture when input is gone or stopped',async()=>{
  const app=await localAudioExportFixture();
  app.run("inputUnavailable=true;capture.inputHealth={status:'unavailable'};updateControls();");
  assert.match(app.element('processing-text').textContent,/새 음성을 받지 못하고/);
  assert.match(app.element('capture-input-status').textContent,/중단 중의 소리는 녹음되지 않습니다/);
  assert.equal(app.element('record-dot').classList.values.has('live'),false);
  assert.equal(app.element('pause-button').disabled,false);
  app.run("inputUnavailable=false;recording=false;capture=null;updateControls();");
  assert.match(app.element('capture-input-status').textContent,/녹음하고 있지 않습니다/);
  assert.match(app.element('record-state').textContent,/녹음 중 아님/);
  assert.doesNotMatch(app.element('processing-text').textContent,/음성은 계속 보관 중/);
  assert.equal(app.element('local-audio-open').disabled,false);
});

async function ongoingRecordingFixture(source = 'microphone') {
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') return response({
      id:options.headers.get('X-Lecture-Id'),title:'수업',language:'ko',
      created_at:new Date().toISOString(),asr_provider:'qwen',segments:[],
    },201);
    return response({});
  });
  app.element('audio-source').value = source;
  await app.run('startRecording()');
  await tick(); await tick();
  return app;
}

test('resumed context alone cannot clear the unavailable warning or start the recording clock',async()=>{
  const app=await ongoingRecordingFixture();
  const mic=app.microphone();mic.inputHealth={status:'unavailable'};
  mic.callbacks.onInputUnavailable(new Error('synthetic input stalled'),{});
  mic.resumeInput=async()=>{mic.inputHealth={status:'waiting'};return true;};
  await app.run('resumeRecording()');
  assert.equal(app.run('inputUnavailable'),true);assert.equal(app.run('timer'),null);
  mic.callbacks.onInputRecovered({});
  assert.equal(app.run('inputUnavailable'),true,'a control event without PCM is not recovery');
  mic.inputHealth={status:'receiving'};mic.callbacks.onLevel(0);
  assert.equal(app.run('inputUnavailable'),false);assert.notEqual(app.run('timer'),null);
  assert.equal(app.run('recording'),true);assert.equal(mic.stopCalls,undefined);
});

test('manual pause remains explicit during suspended input and one resume command restores the intended state',async()=>{
  const app=await ongoingRecordingFixture(),mic=app.microphone();
  await app.run('pauseRecording()');mic.inputHealth={status:'paused'};
  mic.callbacks.onInputUnavailable(new Error('context suspended while manually paused'),{});
  assert.match(app.element('capture-input-status').textContent,/일시정지 중/);
  assert.equal(app.element('pause-button').textContent,'▶ 받아쓰기 재개');
  mic.resume=async()=>{mic.resumeCalls=(mic.resumeCalls || 0)+1;mic.paused=false;mic.recording=true;mic.inputHealth={status:'waiting'};};
  await app.run('resumeRecording()');
  assert.equal(mic.resumeCalls,1);assert.equal(mic.resumeInputCalls,undefined);
  assert.equal(app.run('inputUnavailable'),true);assert.equal(app.run('timer'),null);
  mic.inputHealth={status:'receiving'};mic.callbacks.onLevel(0);
  assert.equal(app.run('recording'),true);assert.equal(app.run('paused'),false);
});

test('RAM rescue never creates or upgrades a missing IndexedDB database',async()=>{
  const app=setup(async()=>{throw new Error('no network');});
  app.run(`pending=[{id:'72000000-0000-4000-8000-000000000003',owner:user,
    captureId:'71000000-0000-4000-8000-000000000003',sequence:0,startSeconds:0,
    durationSeconds:1,overlapSeconds:0,blob:TestEncodeWav(new Float32Array(16000).fill(.2))}];`);
  await app.run('prepareLocalAudioExport()');
  assert.equal(app.createdAll('a').filter(link=>link.download?.includes('_기기보관_')).length,1);
  assert.equal(app.run('liveQueue'),null);assert.equal(app.run('liveQueueReady'),null);
  assert.match(app.element('local-audio-status').textContent,/메모리에서 읽은 음성만/);
});

test('quiet input never pauses the recording UI or its elapsed clock for microphone or system audio', async () => {
  for (const source of ['microphone', 'system']) {
    const app = await ongoingRecordingFixture(source);
    const sessionId = app.run('captureSession.id');
    const timerId = app.run('timer');
    for (let second = 10; second <= 120; second += 10) {
      app.microphone().callbacks.onLevel(0);
      app.run(`performance.now = () => ${second * 1000}; updateControls()`);
      await app.runInterval(500);
    }
    assert.equal(app.run('recording'),true);
    assert.equal(app.run('paused'),false);
    assert.equal(app.run('timer'),timerId);
    assert.equal(app.run('captureSession.id'),sessionId);
    assert.equal(app.element('elapsed').textContent,'02:00');
    assert.equal(app.element('pause-button').textContent,'Ⅱ 일시정지');
    assert.equal(app.element('pause-button').disabled,false);
    assert.equal(app.element('record-button').disabled,false);
    assert.equal(app.microphone().pauseCalls || 0,0);
    assert.equal(app.microphone().stopCalls || 0,0);
    await app.run('stopRecording()');
    assert.equal(app.microphone().stopCalls,1);
  }
});

test('manual pause still freezes time and input levels cannot resume it without the resume button', async () => {
  const app = await ongoingRecordingFixture();
  const sessionId = app.run('captureSession.id');
  app.run('performance.now = () => 12000');
  app.microphone().capturedSeconds = 12;
  app.element('pause-button').onclick();
  await tick(); await tick();
  assert.equal(app.run('paused'),true);
  assert.equal(app.run('recording'),false);
  assert.equal(app.run('timer'),null);
  assert.equal(app.element('pause-button').textContent,'▶ 받아쓰기 재개');
  assert.equal(app.element('record-button').disabled,false);
  for (const level of [0,0.001,0.9,0]) {
    app.microphone().callbacks.onLevel(level);
    app.run('performance.now = () => 62000; startElapsedClock(); updateControls()');
    assert.equal(app.run('paused'),true);
    assert.equal(app.run('timer'),null);
    assert.equal(app.element('elapsed').textContent,'00:12');
  }
  app.element('pause-button').onclick();
  await tick(); await tick();
  assert.equal(app.microphone().pauseCalls,1);
  assert.equal(app.microphone().resumeCalls,1);
  assert.equal(app.microphone().stopCalls || 0,0);
  assert.equal(app.run('captureSession.id'),sessionId);
  assert.equal(app.run('recording'),true);
  assert.notEqual(app.run('timer'),null);
  app.run('performance.now = () => 64000');
  await app.runInterval(500);
  assert.equal(app.element('elapsed').textContent,'00:14','manual pause time is excluded');
  await app.run('stopRecording()');
});

test('input recovery restarts active recording time but never overrides a manual pause', async () => {
  const app = await ongoingRecordingFixture();
  app.run('performance.now = () => 4000');
  app.microphone().callbacks.onInputUnavailable(new Error('temporary input loss'),{});
  assert.equal(app.run('timer'),null);
  assert.equal(app.microphone().stopCalls || 0,0);
  app.run('performance.now = () => 14000');
  app.microphone().callbacks.onInputRecovered({});
  assert.notEqual(app.run('timer'),null);
  app.run('performance.now = () => 16000');
  await app.runInterval(500);
  assert.equal(app.element('elapsed').textContent,'00:06');
  app.microphone().capturedSeconds = 6;
  await app.run('pauseRecording()');
  app.microphone().callbacks.onInputUnavailable(new Error('temporary input loss'),{});
  app.microphone().callbacks.onInputRecovered({});
  assert.equal(app.run('paused'),true);
  assert.equal(app.run('recording'),false);
  assert.equal(app.run('timer'),null);
  assert.equal(app.microphone().resumeCalls || 0,0);
  await app.run('stopRecording()');
});

test('repeated elapsed clock starts preserve time while manual and input transitions keep it stopped', async () => {
  const app = await ongoingRecordingFixture();
  const firstTimer = app.run('timer');
  app.run('performance.now = () => 4000; startElapsedClock()');
  assert.equal(app.run('timer'),firstTimer);
  assert.equal(app.run('elapsedStartedAt'),0,'a duplicate start must not discard elapsed active time');
  app.run('stopElapsedClock()');
  for (const blocked of ['inputUnavailable','inputReconnectNeeded','starting','pausing','resuming','stopping','paused']) {
    app.run(`${blocked}=true; startElapsedClock()`);
    assert.equal(app.run('timer'),null,`${blocked} must keep the elapsed clock stopped`);
    app.run(`${blocked}=false`);
  }
  app.microphone().paused = true;
  app.run('startElapsedClock()');
  assert.equal(app.run('timer'),null);
  app.microphone().paused = false;
  await app.run('stopRecording()');
});

test('automatic silence controls and callbacks are absent rather than disabled by default', async () => {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  const audio = await readFile(new URL('../web/audio.js', import.meta.url), 'utf8');
  assert.doesNotMatch(html,/auto-silence-pause|auto-silence-help|manual-pause-button/);
  assert.doesNotMatch(source,/onSilenceStateChange|setAutoPauseEnabled|autoPauseEnabled|autoPaused/);
  assert.doesNotMatch(audio,/AUTO_SILENCE|_silenceEnabled|onSilenceStateChange|setAutoPauseEnabled|autoPauseEnabled|autoPaused/);
  assert.match(html,/id="pause-button"/,'the normal manual pause control remains');
});

async function storageFailureFixture({provider = 'clova',code = 'live_queue_error'} = {}) {
  const uploads = [];
  const app = setup((url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),
      title:'Storage recovery fixture',created_at:new Date().toISOString(),asr_provider:provider,segments:[]},201);
    if (url.includes('/chunks')) {
      uploads.push({id:options.headers.get('X-Chunk-Id'),final:options.headers.get('X-Final-Chunk'),
        start:options.headers.get('X-Start-Seconds'),body:options.body});
      return response({segments:[],recording_available:true,recording_finalized:options.headers.get('X-Final-Chunk') === 'true'});
    }
    throw new Error(`unexpected fixture request: ${url}`);
  });
  await app.run('openLiveQueue()');
  const queue = app.run('liveQueue'), originalEnqueue = queue.enqueueChunk.bind(queue);
  const failure = {enabled:true,calls:0};
  queue.enqueueChunk = async (...args) => {
    failure.calls += 1;
    if (failure.enabled) { const error = new Error('synthetic storage failure'); error.code = code; throw error; }
    return originalEnqueue(...args);
  };
  app.run(`transcriptionProviders.clova={configured:true}; micProviderPreference=${JSON.stringify(provider)};
    $('asr-provider').value=${JSON.stringify(provider)};`);
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => failure.calls === 1,'first failed local write');
  for (const delay of [500,1000,2000]) await app.runTimeout(delay);
  await until(() => app.run('!!captureSession.storageFailed && !sending'),'storage failure upload gate');
  return {app,queue,uploads,failure,originalEnqueue};
}

test('local storage recovery preserves Qwen and CLOVA chunk identity, ordering and final audio without restarting capture', async () => {
  for (const provider of ['qwen','clova']) {
    const {app,queue,uploads,failure} = await storageFailureFixture({provider});
    assert.equal(uploads.length,0,'even local Qwen waits for durable audio before transmission');
    assert.equal(app.run('pending[0].storageBlocked'),true);
    assert.equal(app.element('retry').textContent,'임시 저장 다시 시도');
    app.microphone().callbacks.onChunk(chunk(5,11,3));
    await app.run('captureSession.persistChain');
    assert.equal(failure.calls,4,'new chunks do not spin on a failed storage latch');
    const expectedIds = app.run('pending.map(item => item.id)');
    const expectedBlobs = app.run('pending.map(item => item.blob)');
    assert.match(app.element('save-state').textContent,/메모리에만 00:16/);
    assert.equal(app.run('volatileAudioStats().seconds'),16,'overlap is not counted as new audio');
    failure.enabled = false;
    await app.runTimeout(5000);
    await until(() => uploads.length === 2 && app.run('pending.length') === 0,'stored backlog upload');
    assert.deepEqual(uploads.map(item => item.id),Array.from(expectedIds));
    for (let i = 0; i < uploads.length; i += 1) {
      assert.deepEqual(new Uint8Array(await uploads[i].body.arrayBuffer()),new Uint8Array(await expectedBlobs[i].arrayBuffer()));
    }
    assert.equal(app.run('captureSession.storageFailed'),false);
    assert.equal(app.run('volatileAudioStats().bytes'),0);
    assert.equal(app.run('recording'),true);
    assert.equal(app.microphone().stopCalls || 0,0);
    app.microphone().tail = chunk(13,3,3,true);
    await app.run('stopRecording()');
    await until(() => uploads.length === 3 && app.run('pending.length') === 0,'final stored tail');
    assert.equal(uploads[2].final,'true');
    assert.equal(new Set(uploads.map(item => item.id)).size,3);
    assert.equal(queue.chunks.size,0);
  }
});

test('quota retries are bounded in frequency while RAM-only duration warns without automatically pausing or stopping', async () => {
  const {app,uploads,failure} = await storageFailureFixture({code:'live_queue_quota'});
  assert.equal([...app.timeouts.values()].filter(timer => timer.delay === 30000).length,1);
  for (let index = 1; index < 39; index += 1) app.microphone().callbacks.onChunk(chunk(index * 8 - 3,11,3));
  await app.run('captureSession.persistChain');
  assert.equal(failure.calls,4);
  assert.equal(app.run('volatileAudioStats().seconds'),312);
  assert.match(app.element('queue-message').textContent,/메모리에 남은 음성이 많이 쌓였습니다/);
  assert.match(app.element('queue-message').textContent,/직접 일시정지/);
  assert.equal(app.run('recording'),true);
  assert.equal(app.microphone().pauseCalls || 0,0);
  assert.equal(app.microphone().stopCalls || 0,0);
  await app.runTimeout(30000);
  await until(() => failure.calls === 5,'single quota probe');
  assert.equal([...app.timeouts.values()].filter(timer => timer.delay === 60000).length,1);
  assert.equal(uploads.length,0);
  assert.equal(app.element('save-failed').hidden,false,'manual WAV rescue remains available');
});

test('manual storage recovery is single-flight and does not grant CLOVA replay approval', async () => {
  const {app,queue,uploads,failure,originalEnqueue} = await storageFailureFixture();
  const gate = deferred(); let blockedWrites = 0;
  failure.enabled = false;
  queue.enqueueChunk = async (...args) => { blockedWrites += 1; await gate.promise; return originalEnqueue(...args); };
  const first = app.run('retryPending()');
  const second = app.run('retryPending()');
  await until(() => blockedWrites === 1,'single recovery write');
  assert.equal(app.run('manualRetryApprovedIds.size'),0);
  assert.equal(uploads.length,0);
  gate.resolve(); await Promise.all([first,second]);
  await until(() => uploads.length === 1 && app.run('pending.length') === 0,'one safe upload');
  assert.equal(blockedWrites,1);
  assert.equal(app.run('manualRetryApprovedIds.size'),0);
});

test('recovering local storage cannot turn an already inflight CLOVA row into a new POST', async () => {
  const {app,queue,failure,originalEnqueue} = await storageFailureFixture();
  const item = app.run('pending[0]'), session = app.run('captureSession');
  await originalEnqueue(session.owner,session.id,app.run('storedChunkInput(pending[0])'));
  await queue.markChunkInflight(session.owner,item.id);
  failure.enabled = false;
  // Prevent the independent GET reconciler from starting: this test checks
  // exactly the storage operation's persisted and runtime safety gates.
  app.run("sendError='unresolved provider request'; token='';");
  await app.run('recoverSessionStorage(captureSession)');
  assert.equal(app.run('pending[0].durable'),true);
  assert.equal(app.run('pending[0].blocked'),true);
  assert.equal(app.run('pending[0].recoveryOnly'),true);
  assert.equal(app.run('pending[0].inflight'),true);
  assert.equal(app.run('sendError'),'unresolved provider request');
  assert.equal(queue.markInflightCalls,1);
  assert.equal(app.run('manualRetryApprovedIds.size'),0);
});

test('storage recovery stops across owner, server and capture-session boundaries without sending stale audio', async () => {
  for (const change of ["user='user-beta'; token='other-token';",
    "apiUrl='https://other.trycloudflare.com'; token='other-token';",'liveSessions.clear();']) {
    const {app,queue,uploads,failure,originalEnqueue} = await storageFailureFixture();
    app.microphone().callbacks.onChunk(chunk(5,11,3)); await app.run('captureSession.persistChain');
    const gate = deferred(); let writes = 0;
    failure.enabled = false;
    queue.enqueueChunk = async (...args) => { writes += 1; await gate.promise; return originalEnqueue(...args); };
    const recovering = app.run('recoverSessionStorage(captureSession)');
    await until(() => writes === 1,'pending old-scope storage commit');
    app.run(change); app.run("sendError='new scope status';");
    gate.resolve(); await recovering;
    assert.equal(writes,1,'a committed old-owner write is kept, but the next write is not started');
    assert.equal(uploads.length,0);
    assert.equal(app.run('sendError'),'new scope status');
    assert.equal(app.run('pending[1].durable'),false);
    assert.equal(app.run('pending[1].blob instanceof Blob'),true);
  }
});

test('a downloaded and explicitly skipped RAM-only chunk advances storage metadata without ever replaying its audio', async () => {
  const {app,uploads,failure,queue} = await storageFailureFixture();
  app.microphone().callbacks.onChunk(chunk(5,11,3)); await app.run('captureSession.persistChain');
  const skippedId = app.run('pending[0].id'), remainingId = app.run('pending[1].id');
  await app.run('saveFailedChunk()');
  await app.run('skipFailedChunk()');
  await until(() => app.run('pending.length') === 1 && !app.run('sending'),'manual RAM-only skip');
  assert.equal([...queue.sessions.values()][0].nextSequence,1,'skip cursor is durable before RAM is discarded');
  assert.equal(app.run('pending.some(item => item.id === ' + JSON.stringify(skippedId) + ')'),false);
  failure.enabled = false;
  await app.run('recoverSessionStorage(captureSession)');
  await until(() => uploads.length === 1 && app.run('pending.length') === 0,'remaining audio only');
  assert.equal(uploads[0].id,remainingId);
  const storedSession = [...queue.sessions.values()][0];
  assert.equal(storedSession.nextSequence,2); assert.equal(storedSession.capturedSamples,16 * 16000);
  assert.equal(app.run('captureSession.storageReceipts?.size || 0'),0);
});

test('storage recovery restores a paused session but never resumes its microphone or clock', async () => {
  const {app,queue,failure} = await storageFailureFixture();
  await app.run('pauseRecording()');
  await app.run('captureSession.persistChain');
  assert.equal(app.run('paused'),true);
  failure.enabled = false;
  await app.run('recoverSessionStorage(captureSession)');
  assert.equal(app.run('paused'),true); assert.equal(app.run('recording'),false);
  assert.equal(app.run('timer'),null); assert.equal(app.microphone().resumeCalls || 0,0);
  assert.equal([...queue.sessions.values()][0].state,'paused');
});

test('PCM snapshots wait behind failed earlier chunks and are saved before the same session resumes uploads', async () => {
  const {app,queue,uploads,failure} = await storageFailureFixture();
  const snapshot = {sequence:1,startSamples:80000,durationSamples:80000,overlapSamples:48000,
    blob:encodeWav(new Float32Array(80000).fill(0.125))};
  await app.microphone().callbacks.onSnapshot(snapshot);
  assert.equal(queue.snapshots.size,0,'never store future PCM ahead of the failed chunk');
  assert.equal(app.run('captureSession.latestSnapshot.sequence'),1);
  failure.enabled = false;
  await app.run('recoverSessionStorage(captureSession)');
  await until(() => uploads.length === 1 && !app.run('sending'));
  const id = app.run('captureSession.id');
  assert.equal(queue.snapshots.get(id).durationSamples,80000);
  assert.equal(app.run('localPcmSnapshots.get(captureSession.id).durationSamples'),80000);
  assert.equal(app.run('captureSession.latestSnapshot'),null);
  assert.equal(app.run('captureSession.storageFailed'),false);
  app.microphone().tail = chunk(5,5,3,true);
  await app.run('stopRecording()');
  await until(() => uploads.length === 2 && app.run('pending.length') === 0);
  assert.equal(app.run('localPcmSnapshots.size'),0);
  assert.equal(queue.snapshots.size,0);
  await app.microphone().callbacks.onSnapshot(snapshot);
  assert.equal(queue.snapshots.size,0,'late callbacks do not resurrect a finalized session');
});

test('PCM snapshot state is not exposed after owner or server changes during a write', async () => {
  for (const change of ["user='user-beta'; localPcmSnapshots.clear()", "apiUrl='https://changed.example'"]) {
    const {app,queue,failure} = await storageFailureFixture();
    failure.enabled = false;
    await app.run('recoverSessionStorage(captureSession)');
    await until(() => !app.run('sending'));
    const original = queue.saveSnapshot.bind(queue), gate = deferred();
    queue.saveSnapshot = async (...args) => { await gate.promise; return original(...args); };
    const snapshot = {sequence:1,startSamples:80000,durationSamples:80000,overlapSamples:48000,
      blob:encodeWav(new Float32Array(80000))};
    const saving = app.microphone().callbacks.onSnapshot(snapshot);
    await tick();
    app.run(change);
    gate.resolve(); await saving;
    if (change.startsWith('user')) assert.equal(app.run('localPcmSnapshots.size'),0);
    else assert.notEqual(app.run('localPcmSnapshots.get(captureSession.id)?.durationSamples'),80000);
  }
});

test('completed explicit-skip storage receipts do not move a completed session back to stopped', async () => {
  const {app,queue,uploads,failure} = await storageFailureFixture();
  const session = app.run('captureSession');
  app.run('pending[0].downloadRequested=true');
  await app.run('skipFailedChunk()');
  app.microphone().tail = chunk(5,3,3,true);
  await app.run('stopRecording()');
  await until(() => !!app.run('sendError') && !app.run('sending'));
  app.run('pending[0].downloadRequested=true');
  await app.run('skipFailedChunk()');
  failure.enabled = false;
  const update = queue.updateSession.bind(queue);
  queue.updateSession = async (owner,id,changes) => {
    if (queue.sessions.get(id)?.state === 'completed' && changes.state !== 'completed') throw new Error('illegal completed transition');
    return update(owner,id,changes);
  };
  // The app may already have released an explicitly discarded final session.
  // In that case there is no remaining automatic upload or state rewrite.
  if (app.run('liveSessions.has(captureSession.id)')) await app.run('recoverSessionStorage(captureSession)');
  assert.equal(uploads.length,0);
  assert.equal(session.storageRepairUnsafe || false,false);
});

test('a downloaded RAM-only chunk is kept when its durable skip decision cannot remove the PCM journal', async () => {
  const {app,queue,uploads} = await storageFailureFixture();
  const id = app.run('pending[0].id');
  app.run('pending[0].downloadRequested=true');
  queue.advanceSettledChunk = async () => { throw new Error('temporary journal write failure'); };
  await app.run('skipFailedChunk()');
  assert.equal(app.run('pending[0].id'),id);
  assert.equal(app.run('pending[0].blob instanceof Blob'),true);
  assert.equal(uploads.length,0);
  assert.match(app.element('notice').textContent,/건너뛰지 못했습니다/);
});

test('an initially unavailable local session can be recreated with the same identity before any audio is sent', async () => {
  const uploads = [];
  const app = setup((url,options = {}) => {
    if (url.endsWith('/lectures')) return response({id:options.headers.get('X-Lecture-Id'),title:'Session retry',
      created_at:new Date().toISOString(),asr_provider:'qwen',segments:[]},201);
    uploads.push(options.headers.get('X-Chunk-Id'));
    return response({segments:[],recording_available:true,recording_finalized:false});
  });
  await app.run('openLiveQueue()');
  const queue = app.run('liveQueue'), create = queue.createSession.bind(queue);
  let available = false, attempts = 0;
  queue.createSession = async value => { attempts += 1; if (!available) throw new Error('temporary unavailable'); return create(value); };
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
  await until(() => attempts === 1);
  for (const delay of [500,1000,2000]) await app.runTimeout(delay);
  await until(() => app.run('!!captureSession.storageFailed && !sending'));
  const sessionId = app.run('captureSession.id'), chunkId = app.run('pending[0].id');
  assert.equal(uploads.length,0); assert.equal(app.run('captureSession.durable'),false);
  available = true;
  await app.run('retryPending()');
  await until(() => uploads.length === 1 && app.run('pending.length') === 0);
  assert.deepEqual(uploads,[chunkId]); assert.equal(app.run('captureSession.id'),sessionId);
  assert.equal(queue.sessions.get(sessionId).nextSequence,1);
});

test('logout scrubs recovery timers and stale callbacks cannot recreate another account storage', async () => {
  const {app,queue,failure} = await storageFailureFixture();
  const timer = [...app.timeouts.values()].find(item => item.delay === 5000);
  const create = queue.createSession.bind(queue); let creates = 0;
  queue.createSession = async value => { creates += 1; return create(value); };
  failure.enabled = false;
  // Forced identity clearing simulates a revoked login. Normal logout already
  // refuses to discard active capture or unsent, owner-locked audio.
  app.run("user='user-beta'; token='new-token'; scrubAccountWorkspace();");
  assert.equal([...app.timeouts.values()].some(item => item === timer),false);
  timer.callback(); await tick(); await tick();
  assert.equal(creates,0);
  assert.equal(app.run('liveQueueBytes'),0);
  assert.equal(app.run('volatileAudioStats().count'),0);
});

test('local ownership and corruption errors do not enter automatic storage repair loops', async () => {
  for (const code of ['live_queue_owner_mismatch','live_queue_conflict','live_queue_corrupt']) {
    const {app,uploads} = await storageFailureFixture({code});
    assert.equal(app.run('captureSession.storageRepairUnsafe'),true);
    assert.equal([...app.timeouts.values()].some(item => item.delay === 5000 || item.delay === 30000),false);
    app.run('nudgeStorageRecovery()'); await tick();
    assert.equal(uploads.length,0);
    assert.equal(app.run('recording'),true);
  }
});

test('capture advancing during a slow storage repair defers future PCM until its own predecessor is durable', async () => {
  for (const provider of ['qwen','clova']) {
    const {app,queue,uploads,failure,originalEnqueue} = await storageFailureFixture({provider});
    const gate = deferred(); let writes = 0;
    failure.enabled = false;
    queue.enqueueChunk = async (...args) => {
      if (++writes === 1) await gate.promise;
      return originalEnqueue(...args);
    };
    const save = queue.saveSnapshot.bind(queue), savedSequences = [];
    queue.saveSnapshot = async (owner,id,snapshot) => {
      if (snapshot.sequence > queue.sessions.get(id).nextSequence) {
        const error = new Error('future PCM cursor'); error.code = 'live_queue_conflict'; throw error;
      }
      savedSequences.push(snapshot.sequence);
      return save(owner,id,snapshot);
    };
    const firstId = app.run('pending[0].id');
    const recovering = app.run('recoverSessionStorage(captureSession)');
    await until(() => writes === 1,'repair stopped inside the first chunk write');
    app.microphone().callbacks.onChunk(chunk(5,11,3));
    const secondId = app.run('pending[1].id');
    const snapshot = {sequence:2,startSamples:208000,durationSamples:80000,overlapSamples:48000,
      blob:encodeWav(Float32Array.from({length:80000},(_,index) => (index % 31) / 64))};
    const snapshotSaving = app.microphone().callbacks.onSnapshot(snapshot);
    assert.equal(app.run('captureSession.latestSnapshot.sequence'),2);
    gate.resolve();
    assert.equal(await recovering,true);
    await snapshotSaving;
    await until(() => uploads.length === 2 && app.run('pending.length') === 0,'new queued chunk and latest PCM finish');
    assert.deepEqual(uploads.map(value => value.id),[firstId,secondId]);
    assert.equal(writes,2,'the fixed repair batch and queued writer never both insert the new chunk');
    assert.ok(savedSequences.every(sequence => sequence === 2));
    const stored = queue.snapshots.get(app.run('captureSession.id'));
    assert.equal(stored.sequence,2); assert.equal(stored.durationSamples,80000);
    assert.deepEqual(new Uint8Array(await stored.blob.arrayBuffer()),new Uint8Array(await snapshot.blob.arrayBuffer()));
    assert.equal(app.run('captureSession.latestSnapshot'),null);
    assert.equal(app.run('captureSession.storageFailed'),false);
    assert.equal(app.run('captureSession.storageRepairUnsafe || false'),false);
    assert.equal(app.run('recording'),true);
  }
});

test('a failed deferred PCM flush never marks its committed predecessor RAM-only or drops the newest PCM', async () => {
  const {app,queue,failure,originalEnqueue} = await storageFailureFixture();
  const gate = deferred(); let writes = 0;
  failure.enabled = false;
  queue.enqueueChunk = async (...args) => {
    if (++writes === 1) await gate.promise;
    return originalEnqueue(...args);
  };
  const save = queue.saveSnapshot.bind(queue);
  queue.saveSnapshot = async () => { const error = new Error('snapshot quota'); error.code = 'live_queue_quota'; throw error; };
  app.run("token='';");
  const recovering = app.run('recoverSessionStorage(captureSession)');
  await until(() => writes === 1);
  app.microphone().callbacks.onChunk(chunk(5,11,3));
  const snapshot = {sequence:2,startSamples:208000,durationSamples:80000,overlapSamples:48000,
    blob:encodeWav(new Float32Array(80000).fill(0.25))};
  const snapshotSaving = app.microphone().callbacks.onSnapshot(snapshot);
  gate.resolve(); await recovering; await snapshotSaving;
  assert.equal(app.run('pending[1].durable'),true);
  assert.equal(app.run('pending[1].blob'),null);
  assert.equal(app.run('captureSession.latestSnapshot'),snapshot);
  assert.equal(app.run('captureSession.storageFailed'),true);
  assert.equal(app.run('captureSession.storageRepairUnsafe'),false);
  assert.equal(app.run('volatileAudioStats().seconds'),2,'only PCM beyond both committed chunks remains volatile');
  queue.saveSnapshot = save;
  assert.equal(await app.run('recoverSessionStorage(captureSession)'),true);
  assert.equal(writes,2,'committed chunks are not reconstructed from their cleared RAM blobs');
  assert.equal(app.run('captureSession.latestSnapshot'),null);
  assert.equal(app.run('volatileAudioStats().count'),0);
});

test('unfinished PCM alone warns about RAM-only audio and excludes overlap, queued coverage and other owners', async () => {
  const app = await ongoingRecordingFixture();
  const queue = app.run('liveQueue'), save = queue.saveSnapshot.bind(queue);
  queue.saveSnapshot = async () => { const error = new Error('snapshot quota'); error.code = 'live_queue_quota'; throw error; };
  const snapshot = {sequence:0,startSamples:0,durationSamples:32000,overlapSamples:0,
    blob:encodeWav(new Float32Array(32000))};
  await app.microphone().callbacks.onSnapshot(snapshot);
  assert.equal(app.run('pending.length'),0);
  assert.equal(app.run('volatileAudioStats().seconds'),2);
  assert.equal(app.run('volatileAudioStats().bytes'),snapshot.blob.size);
  assert.match(app.element('save-state').textContent,/메모리에만 00:02/);
  assert.equal(app.run("volatileAudioStats('user-beta').count"),0);
  app.microphone().callbacks.onChunk(chunk(0));
  await app.run('captureSession.persistChain');
  assert.equal(app.run('volatileAudioStats().seconds'),8,'the same PCM in a queued WAV is not counted twice');
  queue.saveSnapshot = save;
  assert.equal(await app.run('recoverSessionStorage(captureSession)'),true);
  assert.equal(app.run('volatileAudioStats().count'),0);
});

test('explicit RAM-only skip serializes its durable decision ahead of a concurrent storage retry', async () => {
  const {app,queue,uploads,failure,originalEnqueue} = await storageFailureFixture();
  app.microphone().callbacks.onChunk(chunk(5,11,3));
  await app.run('captureSession.persistChain');
  const skippedId = app.run('pending[0].id'), remainingId = app.run('pending[1].id');
  app.run('pending[0].downloadRequested=true');
  const gate = deferred(), advance = queue.advanceSettledChunk.bind(queue);
  let deciding = false;
  queue.advanceSettledChunk = async (...args) => { deciding = true; await gate.promise; return advance(...args); };
  const enqueued = [];
  failure.enabled = false;
  queue.enqueueChunk = async (...args) => { enqueued.push(args[2].id); return originalEnqueue(...args); };
  const skipping = app.run('skipFailedChunk()');
  await until(() => deciding,'skip decision paused before its durable commit');
  const repairing = app.run('recoverSessionStorage(captureSession)');
  await tick(); await tick();
  assert.deepEqual(enqueued,[],'storage retry waits for the earlier explicit decision');
  gate.resolve(); await skipping; assert.equal(await repairing,true);
  await until(() => uploads.length === 1 && app.run('pending.length') === 0);
  assert.deepEqual(enqueued,[remainingId]);
  assert.deepEqual(uploads.map(item => item.id),[remainingId]);
  assert.equal(queue.chunks.has(skippedId),false);
  assert.equal(app.run('captureSession.storageRepairUnsafe || false'),false);
});

test('a late durable skip commit never clears a different account or server upload state', async () => {
  for (const change of ["user='user-beta'; token='next-account-token';", "apiUrl='https://next-server.example';",
    "token='new-session-token'; requestGeneration += 1;"]) {
    const {app,queue,uploads} = await storageFailureFixture();
    const oldSession = app.run('captureSession');
    app.run('pending[0].downloadRequested=true');
    const gate = deferred(), advance = queue.advanceSettledChunk.bind(queue);
    let deciding = false;
    queue.advanceSettledChunk = async (...args) => { deciding = true; await gate.promise; return advance(...args); };
    const skipping = app.run('skipFailedChunk()');
    await until(() => deciding);
    app.run(`${change} sendError='new scope upload status'; sending=true;
      pending=[{id:'new-scope-audio',owner:user,captureId:'new-scope-capture'}];
      $('notice').textContent='new scope notice';`);
    gate.resolve(); await skipping;
    assert.equal(queue.sessions.get(oldSession.id).nextSequence,1,'old owner decision safely committed');
    assert.equal(app.run('pending[0].id'),'new-scope-audio');
    assert.equal(app.run('sendError'),'new scope upload status');
    assert.equal(app.run('sending'),true);
    assert.equal(app.element('notice').textContent,'new scope notice');
    assert.equal(uploads.length,0);
  }
});

const recoveryCode = 'r'.repeat(43);
const recoveryAccount = {account_id:'opaque-peer',label:'member-beta',is_self:false,activated:true};
function recoveryResponse(overrides = {}) {
  return {username:recoveryAccount.label,reset_code:recoveryCode,expires_at:Math.floor(Date.now()/1000)+1800,...overrides};
}
function openRecoveryFixture(app) {
  app.run(`adminAuthorized=true; adminOverview={accounts:[${JSON.stringify(recoveryAccount)}]};
    openAdminRecovery(${JSON.stringify(recoveryAccount)});`);
}
function readResetLink(app,username = 'user-alpha',extra = '') {
  app.location.hash = `#username=${encodeURIComponent(username)}&reset_code=${recoveryCode}${extra}`;
  app.run('consumeAccountLink()');
}

test('recovery supports dotted account names while enforcing the server account format', () => {
  const app = setup(() => response({}));
  app.run(`adminAuthorized=true; openAdminRecovery({account_id:'opaque-dot',label:'member.beta',activated:true})`);
  assert.equal(app.run('adminRecovery.username'),'member.beta');
  app.run('closeAdminRecovery()');
  readResetLink(app,'member.beta');
  assert.equal(app.run('passwordReset.username'),'member.beta');
  assert.equal(app.element('username').value,'member.beta');
  for (const username of ['.user','user.','UPPER','user name','a'.repeat(33)]) {
    readResetLink(app,username);
    assert.equal(app.run('passwordReset'),null,username);
  }
});

test('password recovery is available only for activated non-self admin targets', () => {
  const app = setup(() => response({}));
  app.run(`adminAuthorized=true; renderAdminAccounts([
    {account_id:'opaque-self',label:user,is_self:true,activated:true},
    ${JSON.stringify(recoveryAccount)},
    {account_id:'opaque-invite',label:'invited-user',activated:false},
    {account_id:'opaque-unknown',label:'unknown-user'}]);`);
  const rows = app.element('admin-accounts').children;
  assert.equal(rows[0].children.length,3);
  assert.equal(rows[1].children[3].textContent,'비밀번호 복구');
  assert.equal(rows[2].children.length,3); assert.equal(rows[3].children.length,3);
  rows[1].children[3].onclick();
  assert.equal(app.element('admin-recovery-dialog').open,true);
  assert.equal(app.element('admin-recovery-target').textContent,'member-beta 계정');
  app.run(`closeAdminRecovery(); openAdminRecovery({account_id:'self',label:user,activated:true})`);
  assert.equal(app.element('admin-recovery-dialog').open,false);
  app.run(`adminAuthorized=false; openAdminRecovery(${JSON.stringify(recoveryAccount)})`);
  assert.equal(app.element('admin-recovery-dialog').open,false);
});

test('admin recovery explicitly issues a fixed Pages fragment without persisting or automatically copying secrets', async () => {
  const issued = deferred(), calls = [];
  const app = setup((url,options) => { calls.push({url,options}); return issued.promise; });
  openRecoveryFixture(app);
  app.element('admin-recovery-password').value = 'synthetic-admin-password';
  const issuing = app.element('admin-recovery-form').onsubmit({preventDefault(){}});
  assert.equal(app.element('admin-recovery-password').value,'');
  assert.equal(app.element('admin-recovery-password').disabled,true);
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'https://classroom.example/admin/password-resets');
  assert.deepEqual(JSON.parse(calls[0].options.body),{account_id:'opaque-peer',current_password:'synthetic-admin-password'});
  issued.resolve(response(recoveryResponse())); await issuing;
  const link = new URL(app.element('admin-recovery-link').value);
  assert.equal(link.origin + link.pathname,'https://superwonso.github.io/stt_server_cdh/');
  assert.equal(link.search,'');
  assert.deepEqual([...new URLSearchParams(link.hash.slice(1))],[['username','member-beta'],['reset_code',recoveryCode]]);
  assert.equal(app.element('admin-recovery-result').hidden,false);
  assert.equal(app.clipboardWrites.length,0);
  assert.equal(app.storageWrites.length,0);
  await app.element('admin-recovery-copy').onclick();
  assert.deepEqual(app.clipboardWrites,[link.href]);
  app.element('admin-recovery-close').onclick();
  assert.equal(app.run('adminRecovery'),null);
  assert.equal(app.element('admin-recovery-link').value,'');
  assert.equal(app.element('admin-recovery-target').textContent,'');
  assert.equal(app.element('admin-recovery-dialog').open,false);
  openRecoveryFixture(app);
  assert.equal(app.element('admin-recovery-link').value,'');
  assert.equal(app.element('admin-recovery-result').hidden,true);
});

test('admin reauthentication errors preserve the login and require explicit fresh credentials for issue or revoke', async () => {
  const calls = [];
  const app = setup((url,options) => {
    calls.push({url,options});
    return response(calls.length === 1 ? {detail:'현재 비밀번호를 확인해 주세요.',code:'reauth_invalid'} : {status:'revoked'},calls.length === 1 ? 403 : 200);
  });
  openRecoveryFixture(app); app.element('admin-recovery-password').value = 'wrong-fixture';
  await app.run('runAdminRecovery()');
  assert.equal(app.run('adminAuthorized'),true); assert.equal(app.run('token'),'old-token');
  assert.equal(app.element('admin-recovery-password').value,'');
  assert.match(app.element('admin-recovery-error').textContent,/자동으로 다시 발급하지/);
  assert.equal(calls.length,1);
  await app.element('admin-recovery-revoke').onclick();
  assert.equal(calls.length,1,'a new explicit password is mandatory');
  app.element('admin-recovery-password').value = 'new-fixture';
  await app.element('admin-recovery-revoke').onclick();
  assert.equal(calls[1].url,'https://classroom.example/admin/password-resets/revoke');
  assert.deepEqual(JSON.parse(calls[1].options.body),{account_id:'opaque-peer',current_password:'new-fixture'});
  assert.match(app.element('admin-recovery-status').textContent,/취소했어요/);
  assert.equal(app.element('admin-recovery-link').value,'');
});

test('admin issuance does not retry ambiguous failures and rejects cross-target malformed or expired links', async () => {
  for (const result of [null,{username:'another-owner'},{reset_code:'short'},
    {expires_at:Date.now()/1000-1},{expires_at:Date.now()/1000+7200}]) {
    let posts = 0;
    const app = setup(() => { posts += 1; if (result === null) throw new TypeError('synthetic lost response'); return response(recoveryResponse(result)); });
    openRecoveryFixture(app); app.element('admin-recovery-password').value='fixture';
    await app.run('runAdminRecovery()'); await tick();
    assert.equal(posts,1); assert.equal(app.element('admin-recovery-link').value,'');
    assert.equal(app.element('admin-recovery-password').value,'');
    assert.match(app.element('admin-recovery-error').textContent,/이전 복구 링크는 무효/);
    assert.doesNotMatch(app.element('admin-recovery-error').textContent,new RegExp(recoveryCode));
  }
});

test('admin link results and secrets are discarded on close logout identity session or origin changes', async () => {
  for (const change of ["closeAdminRecovery()", "token=''; showLogin()", "user='other-user'; token='other-token'; showLogin()",
    "setServer('https://replacement.trycloudflare.com')", "token='replacement-token'; resetAdminState()"] ) {
    const issued = deferred(); const app = setup(() => issued.promise);
    openRecoveryFixture(app); app.element('admin-recovery-password').value='fixture';
    const request = app.run('runAdminRecovery()');
    app.run(change);
    issued.resolve(response(recoveryResponse())); await request;
    assert.equal(app.element('admin-recovery-link').value,'',change);
    assert.equal(app.element('admin-recovery-password').value,'',change);
    assert.equal(app.run('adminRecovery'),null,change);
    assert.equal(app.storageWrites.some(([,value]) => value.includes(recoveryCode)),false);
  }
});

test('closing and immediately reopening a recovery dialog cannot resurrect an old response or old close event', async () => {
  const issued = deferred(); const app = setup(() => issued.promise);
  openRecoveryFixture(app); app.element('admin-recovery-password').value='fixture';
  const request = app.run('runAdminRecovery()');
  app.run('closeAdminRecovery()');
  openRecoveryFixture(app);
  app.element('admin-recovery-dialog').onclose();
  assert.equal(app.element('admin-recovery-dialog').open,true);
  issued.resolve(response(recoveryResponse())); await request;
  assert.equal(app.element('admin-recovery-link').value,'');
  assert.equal(app.run('adminRecovery.username'),'member-beta');
});

test('displayed recovery links expire out of memory and DOM without a server request', async () => {
  let calls = 0;
  const app = setup(() => { calls += 1; return response(recoveryResponse()); });
  openRecoveryFixture(app); app.element('admin-recovery-password').value='fixture';
  await app.run('runAdminRecovery()');
  const expiryTimer = app.run('adminRecoveryExpiryTimer');
  app.timeouts.get(expiryTimer).callback();
  assert.equal(app.run('adminRecovery.link'),'');
  assert.equal(app.element('admin-recovery-link').value,'');
  assert.match(app.element('admin-recovery-status').textContent,/만료/);
  assert.equal(calls,1);
});

test('reset fragments are removed before discovery and cannot select an API or restore a saved login', async () => {
  const sessionItems = new Map(), saved = storedAuthFixture(sessionItems), calls = [];
  const app = setup((url,options) => {
    assert.deepEqual(app.historyCalls[0],[null,'','/classroom/']);
    calls.push({url,options});
    return response(url.startsWith('./config.json') ? runtimeConfig({apiUrl:saved.apiOrigin}) : {status:'ok'});
  },{sessionItems});
  blankAuthentication(app);
  app.location.hash = `#username=user-alpha&reset_code=${recoveryCode}&api=https%3A%2F%2Fevil.trycloudflare.com&origin=http%3A%2F%2Flocalhost%3A8765`;
  await app.run('init()');
  assert.equal(sessionItems.size,0); assert.equal(app.run('token'),'');
  assert.equal(app.run('apiUrl'),saved.apiOrigin); assert.equal(app.run('passwordReset.code'),recoveryCode);
  assert.equal(app.element('username').readOnly,true);
  assert.equal(app.element('password').minLength,4);
  assert.equal(app.element('password-confirm').required,true);
  assert.equal(app.element('password').autocomplete,'new-password');
  assert.equal(calls.some(call => call.url.includes('/auth/') || call.url.includes('evil.')),false);
  assert.equal(app.storageWrites.some(([,value]) => value.includes(recoveryCode)),false);
});

test('ambiguous duplicate incomplete and malformed reset links fail closed without session restoration', async () => {
  for (const hash of [`#username=user-alpha&reset_code=${recoveryCode}&setup_code=invitation`,
    `#username=user-alpha&reset_code=${recoveryCode}&reset_code=${recoveryCode}`,
    `#username=user-alpha&username=other&reset_code=${recoveryCode}`,
    '#username=user-alpha&reset_code=',`#reset_code=${recoveryCode}`,'#username=user-alpha&reset_code=bad']) {
    const sessionItems = new Map(), saved = storedAuthFixture(sessionItems); let authCalls=0;
    const app = setup(url => { if (url.includes('/auth/')) authCalls+=1;
      return response(url.startsWith('./config.json') ? runtimeConfig({apiUrl:saved.apiOrigin}) : {status:'ok'});
    },{sessionItems});
    blankAuthentication(app); app.location.hash=hash;
    await app.run('init()');
    assert.equal(app.run('passwordReset'),null); assert.equal(app.run('activation'),false);
    assert.equal(authCalls,0); assert.equal(sessionItems.size,0);
    assert.match(app.element('auth-error').textContent,/관리자에게 새 링크/);
    assert.equal(app.historyCalls.length,1);
  }
});

test('reset success is anonymous and returns to manual login while preserving same-owner microphone and class', async () => {
  const calls = [];
  const app = setup((url,options) => { calls.push({url,options}); return response({status:'password_reset'}); });
  app.run(`recording=true; capture={stop(){throw new Error('must not stop capture')}};
    current={id:'in-progress',title:'Synthetic ongoing class',segments:[],recording_finalized:false}; lectures=[current];`);
  readResetLink(app);
  assert.equal(app.run('recording'),true); assert.equal(app.run('current.id'),'in-progress');
  app.element('password').value='abcd'; app.element('password-confirm').value='abcd';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(calls.length,1); assert.equal(calls[0].url,'https://classroom.example/auth/reset-password');
  assert.equal(calls[0].options.headers.has('Authorization'),false);
  assert.deepEqual(JSON.parse(calls[0].options.body),{username:'user-alpha',reset_code:recoveryCode,password:'abcd',password_confirm:'abcd'});
  assert.equal(app.run('token'),''); assert.equal(app.run('passwordReset'),null);
  assert.equal(app.run('authenticating'),false); assert.equal(await app.run('restoreStoredSession()'),false);
  assert.equal(app.run('recording'),true); assert.equal(app.run('current.id'),'in-progress');
  assert.equal(app.element('username').value,'user-alpha'); assert.equal(app.element('username').readOnly,false);
  assert.equal(app.element('password').value,''); assert.equal(app.element('password-confirm').value,'');
  assert.match(app.element('auth-description').textContent,/새 비밀번호로 로그인/);
});

test('a reset link for another account cannot interrupt or reassign existing owner audio', async () => {
  let calls = 0;
  const app = setup(() => { calls += 1; return response({}); });
  app.run(`recording=true; capture={stop(){throw new Error('must not stop capture')}};`);
  readResetLink(app,'member-beta');
  assert.equal(app.run('passwordReset'),null); assert.equal(app.run('user'),'user-alpha');
  assert.equal(app.run('token'),'old-token'); assert.equal(app.run('recording'),true);
  assert.equal(calls,0); assert.match(app.element('auth-error').textContent,/다른 계정의 녹음/);
});

test('reset password bounds confirmation and server verification are checked before any POST', async () => {
  for (const [password,confirm,verified] of [['abc','abc',true],['x'.repeat(129),'x'.repeat(129),true],
    ['abcd','abce',true],['abcd','abcd',false]]) {
    let calls=0; const app = setup(() => {calls+=1; return response({});});
    readResetLink(app); if (!verified) app.run("connectionState='unverified'");
    app.element('password').value=password; app.element('password-confirm').value=confirm;
    await app.element('auth-form').onsubmit({preventDefault(){}});
    assert.equal(calls,0); assert.equal(app.element('password').value,'');
    assert.equal(app.element('password-confirm').value,''); assert.equal(app.element('auth-error').hidden,false);
    assert.equal(app.run('passwordReset.code'),recoveryCode);
  }
});

test('reset errors clear passwords but never retry or revive a prior authenticated session', async () => {
  for (const value of [null,{status:'unexpected'}]) {
    let calls=0; const app = setup(() => { calls+=1; if (value === null) throw new TypeError('synthetic network loss'); return response(value); });
    readResetLink(app); app.element('password').value='abcd'; app.element('password-confirm').value='abcd';
    await app.element('auth-form').onsubmit({preventDefault(){}}); await tick();
    assert.equal(calls,1); assert.equal(app.run('token'),'');
    assert.equal(app.element('password').value,''); assert.equal(app.element('password-confirm').value,'');
    assert.equal(app.element('auth-error').hidden,false);
    assert.equal(await app.run('restoreStoredSession()'),false);
  }
});

test('late reset responses cannot alter a closed form new account session or changed API origin', async () => {
  for (const change of ["$('auth-toggle').onclick()", "setServer('https://new.trycloudflare.com')",
    "setActivation(false); user='new-owner'; token='new-session'; ++requestGeneration;",
    "token=''; showLogin()"] ) {
    const gate = deferred(); const app = setup(() => gate.promise);
    readResetLink(app); app.element('password').value='abcd'; app.element('password-confirm').value='abcd';
    const saving = app.element('auth-form').onsubmit({preventDefault(){}});
    app.run(change); app.element('auth-description').textContent='new scope';
    gate.resolve(response({status:'password_reset'})); await saving;
    assert.equal(app.run('passwordReset'),null); assert.equal(app.element('auth-description').textContent,'new scope');
    assert.equal(app.element('password').value,''); assert.equal(app.element('password-confirm').value,'');
    assert.equal(app.run('authenticating'),false);
  }
});

test('an older login response cannot log in or clear a newly submitted reset form', async () => {
  const login = deferred(), reset = deferred(), calls = [];
  const app = setup((url,options) => {
    calls.push({url,options}); return url.endsWith('/auth/login') ? login.promise : reset.promise;
  });
  app.run("token=''; user=''");
  app.element('username').value='user-alpha'; app.element('password').value='prior-login-password';
  const loggingIn = app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('authenticating'),true);
  readResetLink(app);
  assert.equal(app.run('authenticating'),false);
  app.element('password').value='abcd'; app.element('password-confirm').value='abcd';
  const resetting = app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(app.run('authenticating'),true);
  login.resolve(response({token:'must-not-activate',user:{username:'user-alpha'}})); await loggingIn;
  assert.equal(app.run('token'),''); assert.equal(app.run('authenticating'),true);
  assert.equal(app.run('passwordReset.code'),recoveryCode);
  reset.resolve(response({status:'password_reset'})); await resetting;
  assert.equal(app.run('authenticating'),false); assert.equal(app.run('token'),'');
  assert.equal(calls.length,2); assert.equal(app.element('auth-error').hidden,true);
});

test('unrelated lesson rendering generations do not strand an account recovery operation', async () => {
  const issued = deferred(), app = setup(() => issued.promise);
  openRecoveryFixture(app); app.element('admin-recovery-password').value='fixture';
  const issuance = app.run('runAdminRecovery()');
  app.run('++requestGeneration');
  issued.resolve(response(recoveryResponse())); await issuance;
  assert.equal(app.run('adminAction'),''); assert.notEqual(app.element('admin-recovery-link').value,'');
  app.run('closeAdminRecovery()');
  const changed = deferred(), resetting = setup(() => changed.promise);
  readResetLink(resetting); resetting.element('password').value='abcd'; resetting.element('password-confirm').value='abcd';
  const saving = resetting.element('auth-form').onsubmit({preventDefault(){}});
  resetting.run('++requestGeneration');
  changed.resolve(response({status:'password_reset'})); await saving;
  assert.equal(resetting.run('passwordReset'),null); assert.equal(resetting.run('authenticating'),false);
});

const usageStates = (overrides = {}) => ({completed:0,failed:0,pending:0,cancelled:0,...overrides});
function usageAccount(label,overrides = {}) {
  return {account_id:`opaque-${label}`,label,lectures:{total:0,active:0,trashed:0},
    recording:{known_seconds:0,qwen_seconds:0,clova_seconds:0,known_lectures:0,unknown_lectures:0},
    imports:usageStates(),ai:{correction:usageStates(),summary:usageStates(),translation:usageStates(),question:usageStates(),study_note:usageStates()},...overrides};
}
function usageFixture(period = 'month',accounts = null) {
  const rows = accounts || [usageAccount('user-alpha',{
    lectures:{total:2,active:1,trashed:1},recording:{known_seconds:600,qwen_seconds:600,clova_seconds:0,known_lectures:1,unknown_lectures:1},
    imports:usageStates({completed:1,pending:1}),
    ai:{correction:usageStates({completed:1}),summary:usageStates({completed:2}),translation:usageStates({failed:1}),question:usageStates({completed:3}),study_note:usageStates()},
  }),usageAccount('member.beta',{
    lectures:{total:1,active:1,trashed:0},recording:{known_seconds:3600,qwen_seconds:0,clova_seconds:3600,known_lectures:1,unknown_lectures:0},
    imports:usageStates({failed:1}),
    ai:{correction:usageStates(),summary:usageStates({completed:1}),translation:usageStates(),question:usageStates({pending:1}),study_note:usageStates()},
  })];
  const {account_id,label,...totals} = usageAccount('total');
  const add = (sum,item) => { for (const key of Object.keys(sum)) {
    if (typeof sum[key] === 'object') add(sum[key],item[key]); else sum[key] += item[key];
  }};
  for (const row of rows) add(totals,row);
  return {period,timezone:'Asia/Seoul',start_at:period === 'all' ? null : period === 'today' ? '2026-09-07T15:00:00Z' : '2026-08-31T15:00:00Z',
    end_at:'2026-09-08T04:00:00Z',generated_at:'2026-09-08T04:00:00Z',accounts:rows,totals,
    scope:{basis:'retained_lectures',date_field:'lecture_created_at',includes_trashed:true,excludes_permanently_deleted:true,
      excludes_deleting:true,ai_counts:'latest_saved_state_except_question_rows',imports:'linked_retained_lectures',duration:'finalized_archive_or_completed_import_metadata'},
    billing:{available:false,reason:'not_recorded'}};
}
function openUsageFixture(app) { app.run("adminAuthorized=true; $('admin-dialog').showModal()"); }

test('admin retained usage reconciles whole and per-account class time and AI-result counts', async () => {
  const calls = [], snapshot = usageFixture();
  snapshot.accounts[0].lecture_title = 'must not retain source content';
  snapshot.billing.secret = 'must not retain billing detail';
  const app = setup((url,options) => { calls.push({url,options}); return response(snapshot); });
  openUsageFixture(app); await app.run('loadAdminUsage()');
  assert.equal(calls.length,1); assert.equal(calls[0].url,'https://classroom.example/admin/usage?period=month');
  assert.equal(calls[0].options.method,undefined,'usage is read-only');
  assert.equal(app.element('admin-usage-period').value,'month');
  assert.equal(app.element('admin-usage-lectures').textContent,'3개');
  assert.equal(app.element('admin-usage-recording').textContent,'1시간 10분');
  assert.equal(app.element('admin-usage-ai').textContent,'7개');
  assert.match(app.element('admin-usage-lecture-detail').textContent,/일반 2개 · 휴지통 1개/);
  assert.match(app.element('admin-usage-recording-detail').textContent,/미확인 1개 수업/);
  const rows = app.element('admin-usage-accounts').children;
  assert.equal(rows.length,2); assert.equal(rows[1].children[0].textContent,'member.beta');
  assert.equal(rows[0].children[1].children[0].children[1].textContent,'2개');
  assert.equal(rows[0].children[1].children[1].children[1].textContent,'10분');
  assert.equal(rows[1].children[1].children[1].children[1].textContent,'1시간 0분');
  assert.equal(rows[0].children[1].children[2].children[1].textContent,'6개');
  assert.match(rows[0].children[3].textContent,/파일 변환: 완료 1 · 대기\/처리 1/);
  assert.match(rows[0].children.at(-2).textContent,/질문: 완료 3/);
  assert.match(rows[0].children.at(-1).textContent,/수업 정리본: 완료 0/);
  assert.doesNotMatch(app.run('JSON.stringify(adminUsageSnapshot)'),/must not retain|billing|lecture_title/);
  assert.match(app.element('admin-usage-updated').textContent,/이번 달 생성 수업 · KST/);
  assert.ok([...app.timeouts.values()].some(item => item.delay === 20000));
});

test('zero retained usage and unknown durations remain distinct from unavailable data', async () => {
  const zero = setup(() => response(usageFixture('month',[usageAccount('zero-user')])));
  openUsageFixture(zero); await zero.run('loadAdminUsage()');
  assert.equal(zero.element('admin-usage-lectures').textContent,'0개');
  assert.equal(zero.element('admin-usage-recording').textContent,'0분');
  assert.equal(zero.element('admin-usage-ai').textContent,'0개');
  const unknown = usageAccount('unknown-user',{lectures:{total:1,active:1,trashed:0},
    recording:{known_seconds:0,qwen_seconds:0,clova_seconds:0,known_lectures:0,unknown_lectures:1}});
  const app = setup(() => response(usageFixture('month',[unknown])));
  openUsageFixture(app); await app.run('loadAdminUsage()');
  assert.equal(app.element('admin-usage-recording').textContent,'0분');
  assert.match(app.element('admin-usage-recording-detail').textContent,/시간 확인 0개 · 미확인 1개/);
  for (const status of [404,500]) {
    const unavailable = setup(() => response({detail:'synthetic unavailable'},status));
    openUsageFixture(unavailable); await unavailable.run('loadAdminUsage()');
    assert.equal(unavailable.element('admin-usage-lectures').textContent,'—');
    assert.equal(unavailable.element('admin-usage-recording').textContent,'—');
    assert.equal(unavailable.element('admin-usage-error').hidden,false);
    assert.match(unavailable.element('admin-usage-updated').textContent,/0건으로 집계한 것이 아닙니다/);
  }
});

test('retained usage rejects invalid shape scope totals duplicate accounts and non-finite metrics', async () => {
  const mutations = [data => {data.totals.lectures.total+=1;},data => {data.totals.ai.question.completed+=1;},
    data => {data.accounts[0].recording.known_seconds=NaN;},data => {data.accounts[0].recording.qwen_seconds=-1;},
    data => {data.accounts[0].imports.completed='1';},data => {data.accounts[0].ai.summary.pending=0.5;},
    data => {data.accounts.push(data.accounts[0]);},data => {data.scope.includes_trashed=false;},
    data => {data.timezone='UTC';},data => {data.period='today';},data => {data.generated_at='not a date';},
    data => {data.billing.available=true;},data => {data.accounts[0].label='<script>';},
    data => {data.accounts[0].recording.unknown_lectures=0;},data => {delete data.totals;},
    data => {data.accounts[0].recording.qwen_seconds=0;}];
  for (const mutate of mutations) {
    const data = usageFixture(); mutate(data);
    const app = setup(() => response(data)); openUsageFixture(app); await app.run('loadAdminUsage()');
    assert.equal(app.run('adminUsageSnapshot'),null);
    assert.equal(app.element('admin-usage-accounts').children.length,0);
    assert.equal(app.element('admin-usage-lectures').textContent,'—');
    assert.match(app.element('admin-usage-error').textContent,/합계를 확인하지 못/);
  }
});

test('a failed usage refresh preserves a clearly stale last-known value but a period change immediately clears it', async () => {
  let calls=0; const pending = deferred();
  const app = setup(() => { calls+=1; return calls===1 ? response(usageFixture()) : calls===2
    ? response({detail:'synthetic connection failure'},503) : pending.promise; });
  openUsageFixture(app); await app.run('loadAdminUsage()'); await app.run('loadAdminUsage()');
  assert.equal(app.element('admin-usage-lectures').textContent,'3개');
  assert.match(app.element('admin-usage-updated').textContent,/마지막 확인값/);
  app.element('admin-usage-period').value='today'; app.element('admin-usage-period').onchange();
  assert.equal(app.element('admin-usage-lectures').textContent,'—');
  assert.equal(app.element('admin-usage-accounts').children.length,0);
  assert.match(app.element('admin-usage-updated').textContent,/오늘 현황/);
  pending.resolve(response(usageFixture('today',[usageAccount('zero-user')]))); await tick(); await tick();
  assert.equal(app.element('admin-usage-lectures').textContent,'0개');
  assert.equal(app.element('admin-usage-error').hidden,true);
});

test('rapid usage period changes abort and discard older results with a strict request allowlist', async () => {
  const requests = [];
  const app = setup((url,options) => { const gate=deferred(); requests.push({url,options,gate}); return gate.promise; });
  openUsageFixture(app); const month = app.run('loadAdminUsage()');
  app.element('admin-usage-period').value='today'; app.element('admin-usage-period').onchange();
  app.element('admin-usage-period').value='all'; app.element('admin-usage-period').onchange();
  assert.deepEqual(requests.map(item=>item.url.split('?')[1]),['period=month','period=today','period=all']);
  assert.equal(requests[0].options.signal.aborted,true); assert.equal(requests[1].options.signal.aborted,true);
  requests[2].gate.resolve(response(usageFixture('all',[usageAccount('latest-user')]))); await tick(); await tick();
  requests[1].gate.resolve(response(usageFixture('today'))); requests[0].gate.resolve(response(usageFixture())); await month; await tick();
  assert.equal(app.run('adminUsageSnapshot.period'),'all');
  assert.equal(app.element('admin-usage-lectures').textContent,'0개');
  assert.equal(app.element('admin-usage-accounts').children[0].children[0].textContent,'latest-user');
  app.element('admin-usage-period').value='today&account_id=someone'; app.element('admin-usage-period').onchange();
  assert.equal(requests.length,3); assert.equal(app.element('admin-usage-period').value,'all');
});

test('usage rows controllers and timers are scrubbed on close logout and API identity changes', async () => {
  for (const change of ["closeAdminDialog()", "token=''; showLogin()", "user='other'; token='other-token'; resetAdminState()",
    "setServer('https://other.trycloudflare.com')"]) {
    let calls=0; const late=deferred();
    const app=setup(() => ++calls===1 ? response(usageFixture()) : late.promise);
    openUsageFixture(app); await app.run('loadAdminUsage()');
    const oldTimer=app.timeouts.get(app.run('adminUsageRefreshTimer')).callback;
    const refreshing=app.run('loadAdminUsage()'); app.run(change);
    late.resolve(response(usageFixture())); await refreshing; oldTimer(); await tick();
    assert.equal(app.run('adminUsageSnapshot'),null,change);
    assert.equal(app.element('admin-usage-accounts').children.length,0,change);
    assert.equal(app.element('admin-usage-lectures').textContent,'—',change);
    assert.equal(app.run('adminUsageController'),null,change); assert.equal(calls,2,change);
  }
});

test('usage requires administrator access and a 403 never exposes or retains account totals', async () => {
  let calls=0; const app=setup(() => {calls+=1;return response({detail:'administrator required'},403);});
  await app.run('loadAdminUsage()'); assert.equal(calls,0);
  openUsageFixture(app); await app.run('loadAdminUsage()');
  assert.equal(calls,1); assert.equal(app.run('adminAuthorized'),false);
  assert.equal(app.element('admin-open').hidden,true); assert.equal(app.run('adminUsageSnapshot'),null);
  assert.equal(app.element('admin-usage-accounts').children.length,0);
});

test('usage refresh is independent from active recording and recovery password entry', async () => {
  const app=setup(url => response(url.includes('/admin/usage?') ? usageFixture() : recoveryResponse()));
  openUsageFixture(app); openRecoveryFixture(app);
  app.run("recording=true; capture={stop(){throw new Error('do not stop microphone')}}");
  app.element('admin-recovery-password').value='unsent-admin-password';
  await app.run('loadAdminUsage()');
  assert.equal(app.run('recording'),true); assert.equal(app.element('admin-recovery-dialog').open,true);
  assert.equal(app.element('admin-recovery-password').value,'unsent-admin-password');
  assert.equal(app.run('adminAction'),'');
  await app.run('runAdminRecovery()');
  const link=app.element('admin-recovery-link').value;
  await app.run('loadAdminUsage()');
  assert.equal(app.element('admin-recovery-link').value,link);
  assert.equal(app.run('recording'),true); assert.equal(app.run('adminUsageSnapshot.totals.lectures.total'),3);
});

test('usage polls only while the administrator dialog is visible and avoids polling during recovery writes', async () => {
  let calls=0; const app=setup(() => {calls+=1;return response(usageFixture());});
  openUsageFixture(app); await app.run('loadAdminUsage()');
  app.document.hidden=true; await app.runTimeout(20000); assert.equal(calls,1);
  app.document.hidden=false; app.run("adminAction='password-reset'"); await app.runTimeout(20000); assert.equal(calls,1);
  app.run("adminAction=''"); await app.runTimeout(20000); assert.equal(calls,2);
  const timer=app.timeouts.get(app.run('adminUsageRefreshTimer')).callback;
  app.run('closeAdminDialog()'); timer(); await tick(); assert.equal(calls,2);
});

function importSelectionFile(name='dropped.wav',type='audio/wav',size=100) {
  const value=new Blob([new Uint8Array(Math.min(size,100))],{type});
  Object.defineProperties(value,{name:{value:name},size:{value:size}}); return value;
}
function recordingDrop(files,{items,types=['Files']}={}) {
  return {dataTransfer:{files,items:items || files.map(file=>({kind:'file',getAsFile:()=>file})),types,dropEffect:'none'},
    defaultPrevented:false,stopped:false,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;}};
}

test('dragged recording selects one immutable File without FileList assignment or upload until explicit start',async()=>{
  const starts=[],operation=deferred();let requests=0;
  const app=setup(()=>{requests++;return response({});},{FileUploader:class{
    detach(){} start(file,options){this.running=true;starts.push({file,options});return operation.promise;}
  }});
  const file=importSelectionFile('새 강의.m4a','');
  Object.defineProperty(app.element('recording-file'),'files',{configurable:true,get:()=>[],set(){throw new Error('FileList assignment unsupported');}});
  app.run("current={id:'english-class',language:'en',segments:[]}; renderCurrent()");
  const event=recordingDrop([file]);await app.element('recording-dropzone').ondrop(event);
  assert.equal(event.defaultPrevented,true);assert.equal(event.stopped,true);
  assert.equal(app.run('currentRecordingFile()'),file);assert.equal(app.run('current'),null);
  assert.equal(app.element('language').value,'ko');assert.equal(app.element('lecture-title').value,'새 강의');
  assert.match(app.element('recording-file-selection').textContent,/새 강의\.m4a/);
  assert.equal(app.element('import-button').disabled,false);assert.equal(requests,0);assert.equal(starts.length,0);
  await app.run('startOrResumeFileImport()');
  assert.equal(starts.length,1);assert.equal(starts[0].file,file);assert.equal(starts[0].options.title,'새 강의');
  assert.equal(app.element('recording-file').disabled,true);
});

test('picker and drop reject empty unsupported oversized multiple directory and text input while retaining prior selection',async()=>{
  const app=setup(()=>response({})),prior=importSelectionFile('kept.wav');
  app.element('recording-file').files=[prior];app.element('recording-file').onchange();
  for(const file of [importSelectionFile('empty.wav','audio/wav',0),importSelectionFile('notes.txt','text/plain'),importSelectionFile('large.wav','audio/wav',1024**3+1)]) {
    app.element('recording-file').files=[file];app.element('recording-file').onchange();
    assert.equal(app.run('currentRecordingFile()'),prior);
    await app.element('recording-dropzone').ondrop(recordingDrop([file]));
    assert.equal(app.run('currentRecordingFile()'),prior);
  }
  await app.element('recording-dropzone').ondrop(recordingDrop([prior,prior]));
  const directory=recordingDrop([prior],{items:[{kind:'file',webkitGetAsEntry:()=>({isDirectory:true})}]});
  await app.element('recording-dropzone').ondrop(directory);assert.match(app.element('notice').textContent,/폴더/);
  const text=recordingDrop([],{types:['text/plain'],items:[{kind:'string'}]});
  await app.element('recording-dropzone').ondrop(text);
  assert.equal(text.defaultPrevented,false,'Ordinary text drag is not blocked');
  assert.equal(app.run('currentRecordingFile()'),prior);assert.match(app.element('recording-file-selection').textContent,/kept\.wav/);
});

test('file selection and drop cannot replace a file during live capture authentication or active file work',async()=>{
  for(const state of ["recording=true","paused=true","starting=true","stopping=true","sending=true","recordingFinalizePending=true",
    "authenticating=true","loggingOut=true","token=''","importStarting=true","importCancelling=true",
    "fileUploader={running:true,file:'original'}; importJob={status:'uploading'}",
    "fileUploader={running:false}; importJob={status:'processing'}"]){
    const app=setup(()=>response({})),prior=importSelectionFile('original.wav'),next=importSelectionFile('replacement.wav');
    await app.element('recording-dropzone').ondrop(recordingDrop([prior]));
    app.run(`${state}; updateControls()`);
    assert.equal(app.element('recording-file').disabled,true,state);
    await app.element('recording-dropzone').ondrop(recordingDrop([next]));
    app.element('recording-file').files=[next];app.element('recording-file').onchange();
    assert.equal(app.run('currentRecordingFile()'),prior,state);
    assert.equal(app.element('import-button').disabled,true,state);
  }
});

test('a stopped upload uses the dropped File only for the existing explicit fingerprint-checked resume',async()=>{
  const app=setup(()=>response({}));
  app.run("globalThis.resumeFiles=[]; fileUploader={running:false,resume(file){globalThis.resumeFiles.push(file);return new Promise(()=>{});}}; importJob={id:'same-job',status:'uploading',lecture_id:'lesson'}; updateControls()");
  const file=importSelectionFile('same-original.wav');
  await app.element('recording-dropzone').ondrop(recordingDrop([file]));
  assert.equal(app.run('globalThis.resumeFiles.length'),0);
  assert.equal(app.element('import-button').textContent,'같은 파일 이어 올리기');
  await app.run('startOrResumeFileImport()');
  assert.equal(app.run('globalThis.resumeFiles.length'),1);assert.equal(app.run('globalThis.resumeFiles[0]'),file);
  assert.equal(app.run('importJob.id'),'same-job');
});

test('delayed file handle checks cannot override a newer picker selection or a changed account token or origin',async()=>{
  for(const change of ['picker','owner','token','origin','recording']){
    const app=setup(()=>response({})),handle=deferred(),old=importSelectionFile('older.wav'),latest=importSelectionFile('latest.wav');
    const event=recordingDrop([old],{items:[{kind:'file',getAsFileSystemHandle:()=>handle.promise}]});
    const dropping=app.element('recording-dropzone').ondrop(event);
    assert.equal(app.element('import-button').disabled,true);
    if(change==='picker'){app.element('recording-file').files=[latest];app.element('recording-file').onchange();}
    else if(change==='owner') app.run("user='different-user'");
    else if(change==='token') app.run("token='different-token'");
    else if(change==='origin') app.run("apiUrl='https://different.example'");
    else app.run('recording=true');
    handle.resolve({kind:'file'});await dropping;
    assert.equal(app.run('currentRecordingFile()'),change==='picker'?latest:null,change);
    assert.equal(app.run('recordingSelectionLoading'),false,change);
    assert.doesNotMatch(app.element('recording-file-selection').textContent || '',/older/);
  }
});

test('page-level file drops prevent navigation but preserve ordinary text drags and explicit picker accessibility',async()=>{
  const app=setup(()=>response({})),file=importSelectionFile();
  const outside=recordingDrop([file]);await app.dispatchDocument('drop',outside);
  assert.equal(outside.defaultPrevented,true);assert.equal(app.run('currentRecordingFile()'),null);
  assert.match(app.element('notice').textContent,/영역/);
  const text=recordingDrop([],{types:['text/plain'],items:[{kind:'string'}]});
  await app.dispatchDocument('dragover',text);await app.dispatchDocument('drop',text);
  assert.equal(text.defaultPrevented,false);
  const drag=recordingDrop([file]);app.element('recording-dropzone').ondragenter(drag);
  app.element('recording-dropzone').ondragenter(drag);
  assert.equal(app.element('recording-dropzone').classList.values.has('is-dragover'),true);
  app.element('recording-dropzone').ondragleave();assert.equal(app.element('recording-dropzone').classList.values.has('is-dragover'),true);
  await app.dispatchDocument('dragend');assert.equal(app.element('recording-dropzone').classList.values.has('is-dragover'),false);
  app.element('recording-file').files=[file];app.element('recording-file').onchange();
  assert.equal(app.run('currentRecordingFile()'),file);
});

function studyNoteEnvelope(status = 'completed') {
  const time='2026-01-01T00:00:00Z';
  return {configured:true,model:'synthetic-study-note',study_note:status === null ? null : {
    lecture_id:'study-lesson',status,model:'synthetic-study-note',error_code:null,error:null,
    created_at:time,updated_at:time,completed_at:status === 'completed' ? time : null,
    document:status === 'completed' ? {paragraphs:[{heading:'개념과 조건',source_ids:['study-a','study-b'],
      text:'**개념**을 살펴봅니다. 조건이 불명확해요.',edits:[
        {original:'살펴봅시다',replacement:'살펴봅니다',uncertain:false},
        {original:'흐릿해요',replacement:'불명확해요',uncertain:true},
      ]}]} : null,
    markdown:status === 'completed' ? '# 수업 정리본\n\n## 개념과 조건\n원문 00:05–00:20\n\n**개념**을 살펴봅니다.' : null,
  }};
}
function studyNoteApp(handler) {
  return setup((url,options={})=>url.endsWith('/study-note') ? handler(url,options)
    : response({configured:false,model:'synthetic',summary:null}));
}
function openStudyNoteFixture(app,{finalized=true,open=true}={}) {
  app.run(`current={id:'study-lesson',title:'합성 수업',created_at:'2026-01-01T00:00:00Z',recording_finalized:${finalized},segments:[
    {id:'study-a',start:5,end:10,text:'개념을 살펴봅시다.'},{id:'study-b',start:10,end:20,text:'조건이 흐릿해요.'}]};renderCurrent();`);
  if(open) { app.element('study-note-details').open=true; app.element('study-note-details').ontoggle(); }
}

test('study note generation is explicit finalized-only and independent of other live recording', async () => {
  const methods=[]; let posted=false;
  const app=studyNoteApp((_url,options)=>{
    methods.push(options.method);
    if(options.method==='POST') {posted=true;return response(studyNoteEnvelope('queued'),202);}
    return response(studyNoteEnvelope(posted ? 'completed' : null));
  });
  openStudyNoteFixture(app,{finalized:false}); app.element('study-note-create').onclick(); await tick();
  assert.deepEqual(methods,[]); assert.equal(app.element('study-note-create').disabled,true);
  app.run('current.recording_finalized=true;renderCurrent()');
  assert.deepEqual(methods,[]);
  app.element('study-note-details').open=true; app.element('study-note-details').ontoggle();
  await until(()=>app.run('studyNoteView.loaded')); assert.deepEqual(methods,['GET']);
  app.run(`recording=true;capture={recording:true,paused:false,stopCalls:0,stop(){this.stopCalls++;}};
    captureSession={id:'another-live-class',lecture:{id:'another-live-class'},source:'microphone',asrProvider:'qwen'};`);
  const raw=app.run('JSON.stringify(current.segments)');
  app.element('study-note-create').onclick(); app.element('study-note-create').onclick();
  await until(()=>app.run('studyNoteView.row?.status')==='queued');
  assert.deepEqual(methods,['GET','POST']); await app.runTimeout(3000);
  assert.equal(app.run('studyNoteView.row.status'),'completed'); assert.equal(app.run('recording'),true);
  assert.equal(app.run('capture.stopCalls'),0); assert.equal(app.run('JSON.stringify(current.segments)'),raw);
  const link=app.element('study-note-download'),url=link.href;
  assert.equal(link.hidden,false); assert.equal(app.element('study-note-create').disabled,true);
  assert.equal(await app.objectUrlBlob(url).text(),studyNoteEnvelope().study_note.markdown);
  assert.match(link.download,/수업 정리본\.md$/);
  const paragraph=app.element('study-note-content').children[0];
  assert.match(paragraph.children[1].textContent,/00:05–00:20/);
  assert.equal(paragraph.children[2].children[0].tagName,'STRONG');
  assert.match(paragraph.children[3].children[0].textContent,/문맥을 참고한 복원 1곳/);
  assert.match(paragraph.children[4].children[0].textContent,/불명확.*1곳/);
  app.run('renderCurrent()'); assert.equal(link.href,url); assert.equal(app.element('study-note-content').children[0],paragraph);
  app.run("correctionView='corrected';reviewView.query='old';$('transcript-search').value='old'");
  paragraph.children.at(-1).onclick(); assert.equal(app.run('correctionView'),'raw');
  assert.equal(app.element('transcript-search').value,''); assert.equal(app.run('recording'),true);
});

test('lost study-note POST response requires GET first and never repeats automatically', async () => {
  const methods=[],app=studyNoteApp((_url,options)=>{
    methods.push(options.method); if(options.method==='POST') throw new Error('synthetic network loss');
    return response(studyNoteEnvelope(methods.length===1 ? null : 'completed'));
  });
  openStudyNoteFixture(app); await until(()=>app.run('studyNoteView.loaded'));
  app.element('study-note-create').onclick(); await until(()=>!!app.run('studyNoteView.error'));
  app.element('study-note-create').onclick(); await tick();
  assert.deepEqual(methods,['GET','POST']); assert.match(app.element('study-note-state').textContent,/상태 새로고침/);
  app.element('study-note-refresh').onclick(); await until(()=>app.run('studyNoteView.row?.status')==='completed');
  assert.deepEqual(methods,['GET','POST','GET']);
});

test('study-note polling is bounded and closing details stops polling without cancelling server work', async () => {
  const methods=[],app=studyNoteApp((_url,options)=>{methods.push(options.method);return response(studyNoteEnvelope('processing'));});
  openStudyNoteFixture(app); await until(()=>app.run('studyNoteView.loaded'));
  app.run('studyNoteView.polls=199'); await app.runTimeout(3000);
  assert.equal(app.run('studyNoteView.polls'),200); assert.equal(app.run('studyNotePollTimer'),null);
  assert.match(app.element('study-note-state').textContent,/서버의 작업은 계속/);
  app.element('study-note-refresh').onclick(); await until(()=>!app.run('studyNoteView.busy'));
  const callback=app.timeouts.get(app.run('studyNotePollTimer')).callback;
  app.element('study-note-details').open=false; app.element('study-note-details').ontoggle(); await callback();
  assert.equal(app.run('studyNotePollTimer'),null); assert.deepEqual(methods,['GET','GET','GET']);
});

test('study-note explicit capacity and input rejections expose safe server reasons without auto retry', async () => {
  for(const status of [400,403,409,413,422,429,503]) {
    const methods=[],app=studyNoteApp((_url,options)=>{methods.push(options.method);return options.method==='POST'
      ? response({detail:'합성 입력 한도를 확인하세요.'},status) : response(studyNoteEnvelope(null));});
    openStudyNoteFixture(app); await until(()=>app.run('studyNoteView.loaded')); app.element('study-note-create').onclick();
    await until(()=>!!app.run('studyNoteView.error')); assert.match(app.element('study-note-state').textContent,/합성 입력 한도/);
    assert.deepEqual(methods,['GET','POST']);assert.equal(app.run('studyNotePollTimer'),null);
  }
});

test('study-note late responses are discarded after owner token API lecture raw-source or dialog changes', async () => {
  for(const change of ["user='user-beta'","token='new-token'","apiUrl='https://changed.example'",
    "current.id='another'","current.segments[0].text='다른 원문'","current.segments[0].start=0",
    "current.segments.reverse()","current.recording_finalized=false",
    "$('study-note-details').open=false;$('study-note-details').ontoggle()"]){
    const pending=deferred(),app=studyNoteApp(()=>pending.promise); openStudyNoteFixture(app);
    await tick(); app.run(change); pending.resolve(response(studyNoteEnvelope())); await tick(); await tick();
    assert.equal(app.run('studyNoteView.row'),null,change); assert.equal(app.element('study-note-content').children.length,0,change);
    assert.equal(app.element('study-note-download').hidden,true,change);
  }
});

test('study-note malformed coverage or missing metadata fail closed without a download', async () => {
  for(const mutate of [r=>r.document.paragraphs[0].source_ids.reverse(),r=>r.document.paragraphs[0].source_ids.pop(),
    r=>r.document.paragraphs[0].source_ids=['study-a','foreign'],r=>r.lecture_id='other',r=>delete r.error_code,
    r=>r.document.paragraphs[0].edits[0].replacement='본문에 없음']){
    const value=studyNoteEnvelope();mutate(value.study_note);const app=studyNoteApp(()=>response(value));
    openStudyNoteFixture(app);await until(()=>!!app.run('studyNoteView.error'));
    assert.equal(app.run('studyNoteView.row'),null);assert.equal(app.element('study-note-download').hidden,true);
    assert.equal(app.element('study-note-content').children.length,0);
  }
});

test('study-note unsupported disabled and failed services have explicit safe recovery controls', async () => {
  for(const disabled of [true,false]){
    let calls=0;const app=studyNoteApp(()=>{++calls;return disabled?response({...studyNoteEnvelope(null),configured:false}):response({detail:'not found'},404);});
    openStudyNoteFixture(app);await until(()=>!app.run('studyNoteView.busy'));
    assert.equal(app.element('study-note-create').disabled,true);
    assert.match(app.element('study-note-state').textContent,disabled?/API 설정/:/아직 지원하지 않거나/);
    app.element('study-note-create').onclick();await tick();assert.equal(calls,1);
  }
  const methods=[],app=studyNoteApp((_url,options)=>{methods.push(options.method);return response(studyNoteEnvelope('failed'));});
  openStudyNoteFixture(app);await until(()=>app.run('studyNoteView.loaded'));
  assert.match(app.element('study-note-create').textContent,/다시/);app.element('study-note-create').onclick();
  await until(()=>!app.run('studyNoteView.busy'));assert.deepEqual(methods,['GET','POST']);
});

test('study-note URLs are revoked on logout or source changes and stale download clicks are blocked', async () => {
  for(const change of ["token='';showLogin(false)","user='user-beta';renderCurrent()",
    "apiUrl='https://changed.example';renderCurrent()","current.segments[0].text='다른 원문';renderCurrent()"]){
    const app=studyNoteApp(()=>response(studyNoteEnvelope()));openStudyNoteFixture(app);await until(()=>app.run('studyNoteView.row?.status')==='completed');
    const url=app.element('study-note-download').href;assert.ok(app.objectUrlBlob(url));app.run(change);
    assert.equal(app.objectUrlBlob(url),undefined,change);assert.equal(app.element('study-note-download').hidden,true,change);
    assert.equal(app.element('study-note-content').children.length,0,change);
  }
  const app=studyNoteApp(()=>response(studyNoteEnvelope()));openStudyNoteFixture(app);await until(()=>app.run('studyNoteView.row?.status')==='completed');
  const url=app.element('study-note-download').href;app.run("current.segments[0].end=11");let blocked=false;
  app.element('study-note-download').onclick({preventDefault(){blocked=true;}});assert.equal(blocked,true);assert.equal(app.objectUrlBlob(url),undefined);
  const expired=studyNoteApp(()=>response(studyNoteEnvelope()));openStudyNoteFixture(expired);
  await until(()=>expired.run('studyNoteView.row?.status')==='completed');
  const expiredURL=expired.element('study-note-download').href;expired.run('authSessionExpiresAt=Date.now()-1');let expiryBlocked=false;
  expired.element('study-note-download').onclick({preventDefault(){expiryBlocked=true;}});
  assert.equal(expiryBlocked,true);assert.equal(expired.objectUrlBlob(expiredURL),undefined);
});

test('study-note model markup is literal except safe emphasis without executable or external elements', async () => {
  const value=studyNoteEnvelope();value.study_note.document.paragraphs[0].text+=' <img src=x> [외부](https://invalid.example) <script>bad()</script>';
  const app=studyNoteApp(()=>response(value));openStudyNoteFixture(app);await until(()=>app.run('studyNoteView.row?.status')==='completed');
  const body=app.element('study-note-content').children[0].children[2];
  assert.ok(body.children.every(node=>['SPAN','STRONG'].includes(node.tagName)));
  assert.match(body.children.at(-1).textContent,/<img src=x>/);assert.equal(app.createdAll('script').length,0);assert.equal(app.createdAll('img').length,0);
});

test('retained usage requires study-note metrics and includes saved study-note results in totals', async () => {
  const value=usageFixture();value.accounts[0].ai.study_note.completed=2;value.totals.ai.study_note.completed=2;
  const app=setup(()=>response(value));openUsageFixture(app);await app.run('loadAdminUsage()');
  assert.equal(app.element('admin-usage-ai').textContent,'9개');
  assert.match(app.element('admin-usage-accounts').children[0].children.at(-1).textContent,/수업 정리본: 완료 2/);
  delete value.accounts[0].ai.study_note;await app.run('loadAdminUsage()');assert.match(app.run('adminUsageError'),/합계/);
});

test('older usage servers consistently missing study-note metrics remain readable without invented zeros', async () => {
  const value=usageFixture();for(const row of [value.totals,...value.accounts])delete row.ai.study_note;
  const app=setup(()=>response(value));openUsageFixture(app);await app.run('loadAdminUsage()');
  assert.equal(app.run('adminUsageError'),'');assert.equal(app.run('adminUsageSnapshot.studyNotesSupported'),false);
  assert.equal(app.element('admin-usage-ai').textContent,'7개');assert.match(app.element('admin-usage-ai-detail').textContent,/서버 미지원/);
  assert.equal(app.element('admin-usage-accounts').children[0].children.at(-1).textContent,'수업 정리본: 서버 미지원');
  value.totals.ai.study_note=usageStates();await app.run('loadAdminUsage()');assert.match(app.run('adminUsageError'),/합계/);
  for(const row of value.accounts)row.ai.study_note=usageStates();value.accounts[0].ai.study_note.completed=-1;
  await app.run('loadAdminUsage()');assert.match(app.run('adminUsageError'),/합계/);
});

test('partial TXT and MD exports snapshot already received text during pause blocked uploads and active capture',async()=>{
  const app=setup(()=>response({}));
  app.run(`current={id:'partial-lesson',title:'부분 수업',created_at:'2026-01-01T00:00:00Z',language:'ko',recording_finalized:false,
    segments:[{id:'partial-a',start:2,end:4,text:'현재 받은 문장'}]};
    pending=[{id:'waiting',owner:user,lectureId:current.id}];sendError='전송 보류';paused=true;renderCurrent();`);
  assert.equal(app.element('download').disabled,false);assert.equal(app.element('export-format').disabled,false);
  const retained=app.run('JSON.stringify(pending)');
  for(const format of ['text','markdown']){
    app.element('export-format').value=format;app.element('download').onclick();const link=app.created('a');
    assert.match(link.download,/_부분_.*\.(txt|md)$/);const before=await app.objectUrlBlob(link.href).text();
    assert.match(before,/현재 받은 문장/);assert.match(before,/내보낸 시점/);assert.match(before,/이후 받아쓴 내용은 포함하지 않습니다/);
    app.run("current.segments.push({id:crypto.randomUUID(),start:4,end:5,text:'추가 문장'});recording=true;paused=false;renderCurrent()");
    assert.equal(await app.objectUrlBlob(link.href).text(),before);assert.equal(app.run('JSON.stringify(pending)'),retained);assert.equal(app.run('sendError'),'전송 보류');
  }
  assert.ok([...app.timeouts.values()].some(timer=>timer.delay===60000));
  const url=app.created('a').href;app.run("token='';showLogin(false)");assert.equal(app.objectUrlBlob(url),undefined);
  const count=app.createdAll('a').length;app.element('download').onclick();assert.equal(app.createdAll('a').length,count);
});

test('expired text export cannot use a timer-delayed token or export another account workspace',()=>{
  const app=setup(()=>response({}));app.run("current={id:'own',title:'수업',segments:[{text:'문장'}]};renderCurrent();authSessionExpiresAt=Date.now()-1");
  app.element('download').onclick();assert.equal(app.createdAll('a').length,0);assert.equal(app.run('token'),'');
  for(const change of ["user='another-owner'","apiUrl='https://another.example'"]){
    const other=setup(()=>response({}));other.run("current={id:'owned',title:'수업',segments:[{text:'문장'}]};renderCurrent()");
    other.run(change);other.element('download').onclick();assert.equal(other.createdAll('a').length,0);
  }
});

test('saved finalized WAV ticket and local residual WAV are available without disturbing other audio work',async()=>{
  const requests=[],app=setup((url,options)=>{requests.push({url,options});return response({path:`/recording-downloads/${'z'.repeat(43)}`});});
  app.run(`current={id:'saved',title:'저장된 수업',recording_available:true,recording_finalized:true,segments:[]};
    recording=true;capture={recording:true,stopCalls:0,stop(){this.stopCalls++;}};pending=[{id:'other',owner:user,lectureId:'other'}];sendError='전송 보류';renderCurrent()`);
  assert.equal(app.element('recording-download').disabled,false);await app.run('downloadRecording()');
  assert.equal(requests.length,1);assert.match(requests[0].url,/recording-download-ticket$/);
  assert.equal(app.run('recording'),true);assert.equal(app.run('capture.stopCalls'),0);assert.equal(app.run('pending.length'),1);assert.equal(app.run('sendError'),'전송 보류');
  const local=await localAudioExportFixture();local.run("current={id:captureSession.id,title:'기기 수업',recording_finalized:false,recording_available:true,segments:[]};updateControls()");
  assert.equal(local.element('recording-download').disabled,false);await local.run('downloadRecording()');
  assert.equal(local.element('local-audio-dialog').open,true);assert.ok(local.element('local-audio-files').children.length>0);
  assert.equal(local.run('recording'),true);assert.equal(local.run('sendError'),'합성 전송 보류');
});

function continuationApp({dropParent=false,rejectChunks=false}={}){
  const calls=[],audioCalls=[];const app=setup((url,options={})=>{
    if(url.endsWith('/status'))return response({capabilities:{lecture_continuations:true},transcription_providers:{qwen:{configured:true},clova:{configured:true}}});
    if(url.endsWith('/lectures')&&options.method==='POST'){
      const body=JSON.parse(options.body),id=options.headers.get('X-Lecture-Id');calls.push({id,body});
      return response({id,title:body.title,language:body.language,asr_provider:body.asr_provider,created_at:'2026-01-01T00:00:00Z',
        segments:[],recording_available:false,recording_finalized:false,...(dropParent?{}:{continuation_of:body.continuation_of||null,continuations:[]})});
    }
    if(url.endsWith('/chunks')){
      const lectureId=url.split('/').at(-2);assert.equal(app.run(`isHeldCapture(${JSON.stringify(lectureId)})`),false);
      audioCalls.push({lectureId,id:options.headers.get('X-Chunk-Id')});
      if(rejectChunks)return response({detail:'합성 모델 처리 실패'},422);
      return response({segments:[{id:webcrypto.randomUUID(),start:0,end:8,text:'새 수업 실제 전사'}],recording_available:true,recording_finalized:options.headers.get('X-Final-Chunk')==='true'});
    }
    return response({});
  });return {app,calls,audioCalls};
}
async function seedContinuationParent(app){
  const parent=webcrypto.randomUUID();
  app.run(`current={id:${JSON.stringify(parent)},title:'원본 수업',language:'ko',asr_provider:'qwen',created_at:'2026-01-01T00:00:00Z',
    recording_available:true,recording_finalized:false,continuation_of:null,continuations:[],segments:[{id:'source',start:0,end:1,text:'원본 문장'}]};lectures=[current];renderCurrent()`);
  await app.seedDurableFailedFinal({lectureId:parent,error:'이전 CLOVA 처리 여부 불명확'});
  await app.run('updateStatus()');return parent;
}

test('continuation preserves an unfinished held parent while actually transcribing a separately linked child',async()=>{
  const {app,calls,audioCalls}=continuationApp(),parent=await seedContinuationParent(app);
  const old=app.run('pending[0]'),oldRaw=app.run('JSON.stringify(current.segments)'),error=app.run('sendError');
  app.run(`liveSessions.get(${JSON.stringify(parent)}).createdAt=Date.now()+1000`);
  assert.equal(app.element('continue-recording').disabled,false);app.element('continue-recording').onclick();
  await until(()=>app.run('recording && !draft'));
  assert.equal(calls.length,1);assert.notEqual(calls[0].id,parent);assert.equal(calls[0].body.continuation_of,parent);
  assert.equal(app.run('sendError'),'');assert.equal(app.run(`liveSessions.get(${JSON.stringify(parent)}).heldError`),error);assert.equal(app.run('pending[0]'),old);
  assert.equal(app.run(`JSON.stringify(lectures.find(l=>l.id===${JSON.stringify(parent)}).segments)`),oldRaw);
  assert.equal(app.run(`lectures.find(l=>l.id===${JSON.stringify(parent)}).recording_finalized`),false);
  assert.ok(app.run(`captureSession.createdAt>liveSessions.get(${JSON.stringify(parent)}).createdAt`));
  const child=app.run('captureSession.id');app.microphone().callbacks.onChunk(chunk(0,8));
  await until(()=>audioCalls.length===1&&app.run('pending.length===1&&!sending'));
  assert.equal(audioCalls[0].lectureId,child);assert.equal(app.run('pending[0]'),old);
  assert.match(app.run('current.segments[0].text'),/새 수업 실제 전사/);
  assert.equal(app.run('sendError'),'');assert.equal(app.element('pause-button').disabled,false);
  await app.run('pauseRecording()');await app.run('resumeRecording()');assert.equal(app.run('recording'),true);assert.equal(app.run('captureSession.id'),child);
  const stored=await app.run('liveQueue.getSession(user,captureSession.id)');assert.equal(stored.continuationOf,parent);
  assert.equal(app.run(`runtimeSessionFromStored(${JSON.stringify(stored)}).continuationOf`),parent);
  app.run('captureSession.lecture=null');await app.run('assignLecture(captureSession)');
  assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1],'same child UUID and full parent request are replayed');
});

test('continuation rejects unsupported servers live sessions foreign queues and unassigned drafts without starting or posting',async()=>{
  for(const change of ["continuationCapability=''","capture={recording:false};paused=true","draft={id:crypto.randomUUID(),lecture:null}",
    "pending[0].owner='another-owner'","token='different-token'","apiUrl='https://another.example'"]){
    const {app,calls}=continuationApp();await seedContinuationParent(app);app.run(change);app.element('continue-recording').onclick();await tick();
    assert.equal(calls.length,0,change);assert.equal(app.microphone(),undefined,change);
  }
});

test('a server that omits the requested continuation never receives child audio and retains its local parent reference',async()=>{
  const {app,calls}=continuationApp({dropParent:true}),parent=await seedContinuationParent(app);
  app.element('continue-recording').onclick();await until(()=>calls.length===1&&!app.run('starting'));
  await until(()=>!app.run('captureSession.creating'));assert.equal(app.run('captureSession.lecture'),null);
  assert.equal(app.run('captureSession.continuationOf'),parent);assert.equal(app.run('recording'),true);
  assert.match(app.run('sendError'),/원래 수업과의 연결/);
  assert.equal(app.run(`liveSessions.get(${JSON.stringify(parent)}).heldError`),'이전 CLOVA 처리 여부 불명확');
  app.microphone().callbacks.onChunk(chunk(0,8));await until(()=>app.run('pending.length===2 && pending[1].durable'));
  assert.equal(app.run('pending[1].lectureReady'),false);assert.equal(calls.length,1);
});

test('blocked uploads no longer prevent reading a different saved lecture or its available text',async()=>{
  const other={id:webcrypto.randomUUID(),title:'다른 수업',recording_finalized:true,recording_available:true,segments:[{id:'other-raw',start:0,end:1,text:'별도 문장'}]};
  const app=setup(url=>url.endsWith(other.id)?response(other):response({}));
  app.run("pending=[{id:'old',owner:user,lectureId:'old'}];sendError='보류';capture=null;draft=null;recording=false;paused=false;updateControls()");
  await app.run(`selectLecture(${JSON.stringify({id:other.id})})`);assert.equal(app.run('current.id'),other.id);
  assert.equal(app.element('download').disabled,false);assert.equal(app.element('recording-download').disabled,false);assert.equal(app.run('sendError'),'보류');
});

test('logout and import completion clear dropped private file references and their visible names',async()=>{
  const app=setup(url=>url.endsWith('/lectures')?response([]):response({})),file=importSelectionFile('private-recording.wav');
  await app.element('recording-dropzone').ondrop(recordingDrop([file]));
  app.run('showLogin()');
  assert.equal(app.run('selectedRecordingFile'),null);assert.equal(app.element('recording-file-selection').textContent,'');
  app.run("token='fresh';user='user-alpha'");
  await app.element('recording-dropzone').ondrop(recordingDrop([file]));
  await app.run("runFileImport(Promise.resolve({id:'finished',status:'completed',raw_deleted:true}),importGeneration)");
  assert.equal(app.run('selectedRecordingFile'),null);assert.equal(app.element('recording-file').value,'');
  assert.equal(app.element('recording-file-selection').textContent,'');
});

test('continuation keeps automatic language detection and rejects malformed parent language metadata',async()=>{
  const {app,calls}=continuationApp();await seedContinuationParent(app);
  app.run('current.language=null;renderCurrent()');app.element('continue-recording').onclick();
  await until(()=>app.run('recording && !draft'));
  assert.equal(calls[0].body.language,null);assert.equal(app.run('captureSession.language'),null);
  assert.equal(app.element('language').value,'auto');
  assert.equal((await app.run('liveQueue.getSession(user,captureSession.id)')).language,null);
  for(const malformed of ['undefined',"'auto'","''"]){
    const fixture=continuationApp();await seedContinuationParent(fixture.app);
    fixture.app.run(`current.language=${malformed};updateControls()`);fixture.app.element('continue-recording').onclick();
    await tick();assert.equal(fixture.calls.length,0);assert.equal(fixture.app.microphone(),undefined);
  }
});

test('hidden parent recovery verifies the original creation body once then accepts refreshed hidden navigation links',async()=>{
  const parent=webcrypto.randomUUID(),child=webcrypto.randomUUID(),posts=[],audioPosts=[];
  const hidden={id:child,title:'이어 녹음',language:'en',asr_provider:'qwen',continuation_of:null,continuations:[],
    created_at:'2026-01-01T00:00:00Z',recording_available:true,recording_finalized:false,segments:[]};
  const app=setup((url,options={})=>{
    if(url.endsWith('/lectures')&&options.method==='POST'){
      posts.push({id:options.headers.get('X-Lecture-Id'),body:JSON.parse(options.body)});
      return response({...hidden,continuation_creation_verified:true});
    }
    if(url.endsWith('/chunks')){audioPosts.push(options.headers.get('X-Chunk-Id'));return response({recording_available:true,recording_finalized:false,
      segments:[{id:`result-${audioPosts.length}`,start:audioPosts.length-1,end:audioPosts.length,text:'복구된 문장'}]});}
    return response({});
  });
  await app.run(`(async()=>{
    current=${JSON.stringify(hidden)};lectures=[current];continuationCapability=JSON.stringify([user,token,apiUrl]);
    await openLiveQueue();await liveQueue.createSession({id:${JSON.stringify(child)},owner:user,title:'이어 녹음',language:'en',
      source:'microphone',asrProvider:'qwen',continuationOf:${JSON.stringify(parent)},createdAt:Date.now()});
    liveQueue.recoverOwner=async owner=>({sessions:[...liveQueue.sessions.values()].filter(s=>s.owner===owner).map(s=>({...s})),
      chunks:[...liveQueue.chunks.values()].filter(c=>c.owner===owner).map(c=>({...c})),snapshots:[],stats:{bytes:32044,count:1}});
    await liveQueue.updateSession(user,current.id,{lectureCreated:true});
    await liveQueue.enqueueChunk(user,current.id,{id:crypto.randomUUID(),startSamples:0,durationSamples:16000,overlapSamples:0,final:false,blob:TestEncodeWav(new Float32Array(16000).fill(.2))});
    const stored=await liveQueue.getSession(user,current.id),unverified=runtimeSessionFromStored(stored);
    globalThis.__unverifiedLecture=lectureForStoredProvider(unverified,{...current,continuation_creation_verified:true});
    renderCurrent();await recoverDurableLiveAudio(user);
  })()`);
  assert.equal(app.run('__unverifiedLecture'),null,'GET/list flags alone must never authorize a hidden parent');
  await until(()=>audioPosts.length===1&&app.run('pending.length===0&&!sending'));
  assert.equal(posts.length,1);assert.equal(posts[0].id,child);assert.equal(posts[0].body.continuation_of,parent);
  assert.equal(app.run(`liveSessions.get(${JSON.stringify(child)}).continuationVerified`),true);
  assert.equal(app.run('current.continuation_of'),null);assert.match(app.run('current.segments[0].text'),/복구된/);
  await app.run(`(async()=>{
    lectures=[${JSON.stringify(hidden)}];current={...lectures[0]};
    await liveQueue.enqueueChunk(user,current.id,{id:crypto.randomUUID(),startSamples:16000,durationSamples:16000,overlapSamples:0,final:false,blob:TestEncodeWav(new Float32Array(16000).fill(.2))});
    await recoverDurableLiveAudio(user);
  })()`);
  await until(()=>audioPosts.length===2&&app.run('pending.length===0&&!sending'));
  assert.equal(posts.length,1,'already verified capture is not recreated after list refresh hides its parent');
  assert.equal(new Set(audioPosts).size,2);assert.equal(app.run('sendError'),'');
  assert.equal(app.run(`lectureForStoredProvider(liveSessions.get(${JSON.stringify(child)}),{...current,continuation_of:crypto.randomUUID()})`),null);
  assert.equal(app.run(`lectureForStoredProvider(liveSessions.get(${JSON.stringify(child)}),{...current,language:'ko'})`),null);
  app.run("apiUrl='https://different.example'");
  assert.equal(app.run(`lectureForStoredProvider(liveSessions.get(${JSON.stringify(child)}),current)`),null,'creation verification is bound to its API origin');
});

test('server partial WAV uses a validated separate ticket without finalizing or changing live and queued audio',async()=>{
  const calls=[],ticket={path:`/recording-snapshots/${'p'.repeat(43)}`,expires_in:60,bytes:320044,duration_seconds:10,scope:'server_saved_prefix'};
  const app=setup((url,options)=>{calls.push({url,options});return response(ticket);});
  app.run(`current={id:'unfinished',title:'부분 녹음',recording_available:true,recording_finalized:false,segments:[]};
    partialRecordingCapability=JSON.stringify([user,token,apiUrl]);recording=true;capture={stopCalls:0,stop(){this.stopCalls++;}};
    pending=[{id:'waiting',owner:user,lectureId:current.id}];sendError='전송 보류';renderCurrent()`);
  assert.equal(app.element('recording-partial-download').disabled,false);await app.run('downloadPartialRecording()');
  assert.equal(calls.length,1);assert.match(calls[0].url,/recording-snapshot-ticket$/);assert.equal(calls[0].options.method,'POST');
  const link=app.created('a');assert.match(link.href,/\/recording-snapshots\//);assert.match(link.download,/_서버저장부분_/);
  assert.equal(app.run('capture.stopCalls'),0);assert.equal(app.run('recording'),true);assert.equal(app.run('pending.length'),1);
  assert.equal(app.run('sendError'),'전송 보류');assert.equal(app.run('current.recording_finalized'),false);
  assert.match(app.element('notice').textContent,/미전송 음성은 별도/);
});

test('server partial WAV rejects unsupported capabilities malformed tickets and late owner or origin responses',async()=>{
  const ticket={path:`/recording-snapshots/${'p'.repeat(43)}`,expires_in:60,bytes:32044,duration_seconds:1,scope:'server_saved_prefix'};
  const seed=app=>app.run(`current={id:'unfinished',title:'부분 녹음',recording_available:true,recording_finalized:false,segments:[]};partialRecordingCapability=JSON.stringify([user,token,apiUrl]);renderCurrent()`);
  for(const change of ["partialRecordingCapability=''","current.recording_finalized=true","token='changed'"]){
    let count=0;const app=setup(()=>{count++;return response(ticket);});seed(app);app.run(change);await app.run('downloadPartialRecording()');assert.equal(count,0);
  }
  for(const invalid of [{path:'https://untrusted.example/file'},{scope:'whole_class'},{bytes:44},{duration_seconds:2},{expires_in:600}]){
    const app=setup(()=>response({...ticket,...invalid}));seed(app);await app.run('downloadPartialRecording()');assert.equal(app.createdAll('a').length,0);
  }
  for(const change of ["user='another-owner'","token='changed'","apiUrl='https://another.example'","current={id:'different'}"]){
    const wait=deferred(),app=setup(()=>wait.promise);seed(app);const operation=app.run('downloadPartialRecording()');
    await tick();app.run(change);wait.resolve(response(ticket));await operation;assert.equal(app.createdAll('a').length,0);
  }
});

test('late continuation assignment cannot repopulate a cleared workspace after IndexedDB confirmation waits',async()=>{
  for(const change of ["token='';showLogin(false);lectures=[];current=null", "user='another-owner';token='another-token';lectures=[];current=null", "apiUrl='https://another.example';lectures=[];current=null"]){
    const {app}=continuationApp();await seedContinuationParent(app);
    app.run(`globalThis.__idbWaiting=false;globalThis.__releaseAssignment=null;
      const update=liveQueue.updateSession.bind(liveQueue);liveQueue.updateSession=async(owner,id,fields)=>{
        const result=await update(owner,id,fields);if(fields.lectureCreated){__idbWaiting=true;await new Promise(resolve=>{__releaseAssignment=resolve;});}return result;
      };`);
    app.element('continue-recording').onclick();await until(()=>app.run('__idbWaiting'));
    app.run(change);const errorBefore=app.run('sendError');app.run('__releaseAssignment()');await until(()=>!app.run('captureSession.creating'));
    assert.equal(app.run('lectures.length'),0);assert.equal(app.run('current'),null);
    assert.equal(app.run('sendError'),errorBefore);
  }
});

test('lecture assignment refuses foreign or unauthenticated sessions before sending a body',async()=>{
  for(const change of ["captureSession.owner='another-owner'","token=''"]){
    const {app,calls}=continuationApp();await seedContinuationParent(app);
    app.run(`captureSession.lecture=null;${change}`);
    await assert.rejects(app.run('assignLecture(captureSession)'));assert.equal(calls.length,0);
  }
});

async function seedHeldScenario(app,{count=1}={}){
  const id=webcrypto.randomUUID();
  await app.run(`(async()=>{
    current={id:${JSON.stringify(id)},title:'이전 보관 수업',language:'ko',asr_provider:'clova',created_at:'2026-01-01T00:00:00Z',
      recording_available:true,recording_finalized:false,segments:[]};lectures=[current];
    await openLiveQueue();const session={id:current.id,owner:user,lecture:current,title:current.title,language:'ko',source:'microphone',asrProvider:'clova',
      createdAt:Date.now()-1000,durable:true,cancelled:true,persistChain:Promise.resolve(),storeReady:Promise.resolve(),assignmentTimer:null,nextRuntimeSequence:${count}};
    await liveQueue.createSession(storedSessionInput(session));await liveQueue.updateSession(user,session.id,{lectureCreated:true});
    liveSessions.set(session.id,session);captureSession=session;
    const blob=TestEncodeWav(new Float32Array(800).fill(.1));
    for(let index=0;index<${count};index++){
      const stored=await liveQueue.enqueueChunk(user,session.id,{id:crypto.randomUUID(),startSamples:index*800,durationSamples:800,overlapSamples:0,final:index===${count}-1,blob});
      if(index===0){await liveQueue.markChunkInflight(user,stored.id);await liveQueue.markChunkBlocked(user,stored.id,'unknown_result');}
      pending.push({id:stored.id,captureId:session.id,lectureId:session.id,owner:user,asrProvider:'clova',sequence:stored.sequence,
        sessionCreatedAt:session.createdAt,startSeconds:index*.05,durationSeconds:.05,overlapSeconds:0,final:stored.final,
        blob:null,durable:true,byteLength:stored.byteLength,lectureReady:true,blocked:index===0,inflight:false,persistPromise:Promise.resolve()});
    }
    sendError='이전 CLOVA 결과를 확인하지 못해 자동 재전송하지 않았어요.';renderCurrent();
  })()`);
  return id;
}

test('holding 403 previous chunks permits independent real transcription and another new lesson without deleting old audio',async()=>{
  const {app,audioCalls}=continuationApp(),oldId=await seedHeldScenario(app,{count:403});
  const queue=app.run('liveQueue'),before=[...queue.chunks.values()].map(row=>({...row}));
  await app.run('prepareIndependentLesson()');
  assert.equal(app.run('current'),null);assert.equal(app.run('pending.length'),403);assert.equal(app.run('activePendingCount()'),0);
  assert.equal(queue.sessions.get(oldId).uploadHeld,true);assert.equal(audioCalls.length,0);assert.equal(app.run('sendError'),'');
  assert.equal(app.element('new-note').disabled,false);assert.equal(app.element('record-button').disabled,false);
  assert.equal(app.element('processing').hidden,true);assert.match(app.element('record-state').textContent,/보관 중/);
  assert.equal(app.element('held-audio-list').dataset.signature,undefined);assert.doesNotMatch(JSON.stringify(app.element('held-audio-list').dataset),/token|bearer/i);
  app.element('asr-provider').value='qwen';await app.run('startRecording()');const childId=app.run('captureSession.id');
  app.microphone().callbacks.onChunk(chunk(0,1));await until(()=>audioCalls.length===1&&app.run('!sending'));
  assert.equal(audioCalls[0].lectureId,childId);assert.match(app.run('current.segments[0].text'),/새 수업 실제 전사/);
  app.microphone().tail=chunk(1,.05,0,true);await app.run('stopRecording()');await until(()=>audioCalls.length===2&&app.run('!sending'));
  assert.equal(app.run('pending.length'),403);assert.equal(queue.chunks.size,403);
  const storedAfter=[...queue.chunks.values()];
  assert.ok(storedAfter.every(row=>row.uploadHeld===true),'every retained chunk carries the legacy-reader hold guard');
  assert.deepEqual(storedAfter.map(({uploadHeld,...row})=>row),before,'only the hold guard changes; all 403 UUIDs, order, states, attempts and original Blob objects remain unchanged');
  for(const row of before)assert.deepEqual(new Uint8Array(await queue.chunks.get(row.id).blob.arrayBuffer()),new Uint8Array(await row.blob.arrayBuffer()));
  assert.equal(app.element('new-note').disabled,false);await app.run('prepareIndependentLesson()');
  assert.equal(app.element('record-button').disabled,false);assert.equal(app.run('pending.length'),403);
  assert.equal(app.element('logout').disabled,true,'retained private work keeps its owner guard');
  app.run("token='';showLogin(false)");assert.equal(app.element('held-audio-list').children.length,0);
});

test('explicit held CLOVA recovery discards stale retry approval and queries unknown results without resubmitting audio',async()=>{
  let uploads=0,lookups=0;const app=setup((url,options={})=>{
    if(url.endsWith('/result')){lookups++;return response({state:'unknown'});}
    if(url.endsWith('/chunks')){uploads++;return response({segments:[]});}
    return response({});
  });const id=await seedHeldScenario(app);const original=app.run('pending[0]');
  app.run('manualRetryApprovedIds.add(pending[0].id)');await app.run('prepareIndependentLesson()');
  assert.equal(app.run('manualRetryApprovedIds.size'),0);
  app.run('manualRetryApprovedIds.add(pending[0].id)');await app.run(`restoreHeldAudio(${JSON.stringify(id)})`);
  await until(()=>app.run('!!sendError&&!sending'));
  assert.equal(uploads,0);assert.equal(lookups,1);assert.equal(app.run('pending[0]'),original);
  assert.equal(app.run('manualRetryApprovedIds.size'),0);assert.equal(app.run('liveQueue.chunks.size'),1);
  assert.equal(app.run('liveQueue.chunks.values().next().value.uploadHeld'),undefined,'explicit restore removes only the hold guard');
  assert.match(app.element('retry').textContent,/위험 이해.*수동 재전송/);
});

test('fresh durable holds precede stale blocked state and manual approvals while a different capture continues',async()=>{
  const {app,audioCalls}=continuationApp(),oldId=await seedHeldScenario(app);await app.run('prepareIndependentLesson()');
  const original={...app.run('liveQueue.chunks.values().next().value')};
  app.element('asr-provider').value='qwen';await app.run('startRecording()');
  app.run(`liveSessions.get(${JSON.stringify(oldId)}).uploadHeld=false;manualRetryApprovedIds.add(pending[0].id);sendError=''`);
  app.microphone().callbacks.onChunk(chunk(0,1));await until(()=>audioCalls.length===1&&app.run('!sending'));
  assert.equal(app.run(`isHeldCapture(${JSON.stringify(oldId)})`),true);assert.equal(app.run('manualRetryApprovedIds.size'),0);
  assert.deepEqual({...app.run('liveQueue.chunks.values().next().value')},original);assert.equal(app.run('pending.length'),1);
  assert.equal(app.run('recording'),true);assert.equal(app.run('sendError'),'');
});

test('holding stops and persists a final microphone tail without sending it before the durable hold',async()=>{
  const {app,audioCalls}=continuationApp();await app.run('startRecording()');
  app.run("sendError='이전 오류'");app.microphone().callbacks.onChunk(chunk(0,1));
  await until(()=>app.run('pending[0]?.durable'));app.microphone().tail=chunk(1,.1,0,true);
  await app.run('prepareIndependentLesson()');
  assert.equal(app.microphone().stopCalls,1);assert.equal(app.run('capture'),null);assert.equal(app.run('current'),null);
  assert.equal(app.run('pending.length'),2);assert.equal(app.run('pending[1].final'),true);
  assert.equal(audioCalls.length,0);assert.equal(app.run('heldAudioSessions().length'),1);
});

test('RAM-only or unverified other-tab capture cannot be abandoned to start a new lesson',async()=>{
  const {app}=continuationApp();await app.run('startRecording()');
  const queue=app.run('liveQueue');queue.enqueueChunk=async()=>{throw Object.assign(new Error('synthetic quota'),{code:'live_queue_quota'});};
  app.microphone().callbacks.onChunk(chunk(0,1));
  for(const delay of [500,1000,2000]){await until(()=>[...app.timeouts.values()].some(timer=>timer.delay===delay));await app.runTimeout(delay);}
  await until(()=>app.run('pending.length===1&&!pending[0].durable&&!!sendError'));
  const retained=app.run('pending[0].blob'),id=app.run('captureSession.id');await app.run('prepareIndependentLesson()');
  assert.equal(app.run('pending[0].blob'),retained);assert.equal(app.run('current.id'),id);assert.equal(app.run('isHeldCapture(captureSession.id)'),false);
  assert.match(app.element('notice').textContent,/메모리에만|탭을 닫거나 새로고침하지/);assert.equal(app.element('record-button').disabled,true);
  assert.ok(app.run('captureSession.captureLease'));assert.equal(app.coordination.releasedCaptureLeases,0);
  const other=continuationApp();await seedHeldScenario(other.app);other.app.coordination.acquireLiveCapture=async()=>({supported:true,acquired:false});
  await other.app.run('prepareIndependentLesson()');assert.equal(other.app.run('pending.length'),1);assert.equal(other.app.run('heldAudioSessions().length'),0);
  assert.match(other.app.element('notice').textContent,/다른 탭/);
});

test('holding an unregistered durable draft preserves its creation identity while freeing a new assignment slot',async()=>{
  const {app,calls}=continuationApp();const id=await seedHeldScenario(app);
  app.run(`const previous=liveSessions.get(${JSON.stringify(id)});previous.lecture=null;draft=previous;current=null;lectures=[];`);
  const snapshot=app.run('JSON.stringify(storedSessionInput(draft))');await app.run('prepareIndependentLesson()');
  assert.equal(app.run('draft'),null);assert.equal(app.run('pending.length'),1);assert.equal(calls.length,0);
  assert.equal(app.run(`liveSessions.get(${JSON.stringify(id)}).id`),JSON.parse(snapshot).id);
  app.element('asr-provider').value='qwen';await app.run('startRecording()');
  assert.equal(calls.length,1);assert.notEqual(calls[0].id,id);assert.equal(app.run(`liveSessions.get(${JSON.stringify(id)}).uploadHeld`),true);
});

test('late hold confirmation does not reset another account or server workspace',async()=>{
  for(const change of ["user='user-beta';token='beta-token'","apiUrl='https://different.example'"]){
    const {app}=continuationApp();await seedHeldScenario(app);const wait=deferred();let entered=false;
    const queue=app.run('liveQueue'),original=queue.setSessionUploadHeld.bind(queue);
    queue.setSessionUploadHeld=async(...args)=>{entered=true;await wait.promise;return original(...args);};
    const operation=app.run('prepareIndependentLesson()');await until(()=>entered);
    app.run(`${change};current={id:'new-workspace',segments:[]}`);wait.resolve();await operation;
    assert.equal(app.run('current.id'),'new-workspace');assert.equal(app.run('pending.length'),1);
    assert.equal(app.element('held-audio-list').dataset.signature,undefined);
  }
});

test('fresh held-session reads cannot send old audio with a token or origin changed during IndexedDB wait',async()=>{
  for(const change of ["token='replacement-token'","apiUrl='https://different.example'","user='user-beta';token='beta-token'"]){
    const {app,audioCalls}=continuationApp();await app.run('startRecording()');
    const queue=app.run('liveQueue'),original=queue.getSession.bind(queue),wait=deferred();let entered=false;
    queue.getSession=async(...args)=>{entered=true;await wait.promise;return original(...args);};
    app.microphone().callbacks.onChunk(chunk(0,1));await until(()=>entered);
    app.run(change);wait.resolve();await until(()=>app.run('!sending'));
    assert.equal(audioCalls.length,0);assert.equal(app.run('pending.length'),1);assert.equal(queue.chunks.size,1);
  }
});

test('failed WAV download and explicit skip target the active lesson behind a held physical queue head',async()=>{
  const {app,audioCalls}=continuationApp({rejectChunks:true});await seedHeldScenario(app);
  await app.run('prepareIndependentLesson()');
  const old=app.run('pending[0]'),queue=app.run('liveQueue'),storedOld={...queue.chunks.get(old.id)};
  app.element('asr-provider').value='qwen';await app.run('startRecording()');
  const activeId=app.run('captureSession.id');app.microphone().callbacks.onChunk(chunk(0,1));
  await until(()=>audioCalls.length===1&&app.run('!!sendError&&!sending'));
  const failed=app.run('nextPendingChunk()');app.microphone().callbacks.onChunk(chunk(1,1));
  await until(()=>app.run('pending.length===3&&pending.every(item=>item.durable)'));
  await app.run('saveFailedChunk()');
  assert.equal(failed.downloadRequested,true);assert.equal(old.downloadRequested,undefined);
  const downloaded=app.objectUrlBlob(app.created('a').href);
  assert.deepEqual(new Uint8Array(await downloaded.arrayBuffer()),new Uint8Array(await queue.chunks.get(failed.id).blob.arrayBuffer()));
  assert.equal(app.run('pending.length'),3,'download does not acknowledge either lecture');
  await app.run('skipFailedChunk()');await until(()=>audioCalls.length===2&&app.run('!!sendError&&!sending'));
  assert.equal(app.run('pending[0]'),old);assert.equal(app.run('pending.length'),2);
  assert.deepEqual(queue.chunks.get(old.id),storedOld,'the held audio and all original processing metadata remain intact');
  assert.equal(queue.chunks.has(failed.id),false);assert.ok(audioCalls.every(call=>call.lectureId===activeId));
  assert.equal(app.run('recording'),true);assert.equal(app.microphone().stopCalls,undefined);
});
