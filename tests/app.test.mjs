import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { encodeWav } from '../web/audio.js';

const source = (await readFile(new URL('../web/app.js', import.meta.url), 'utf8'))
  .replace("import { MicrophoneCapture } from './audio.js';", 'const MicrophoneCapture = TestCapture;')
  .replace("import { FileImportCancelledError, RecordingFileUploader, isTerminalImportState } from './file-import.js';", `
    const FileImportCancelledError = class extends Error {};
    const RecordingFileUploader = TestFileUploader;
    const isTerminalImportState = state => ['completed','failed','cancelled'].includes(state?.status);
  `)
  .replace('void init();', '');
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (value, status = 200, headers = {}) => ({
  status, ok:status < 400, json:async () => value,
  headers:{get(name){ return headers[name] ?? headers[name.toLowerCase()] ?? null; }},
});
const chunk = (startSeconds, durationSeconds = 8, overlapSeconds = 0, final = false) => ({
  blob:encodeWav(new Float32Array(Math.max(1, Math.round(durationSeconds * 16000))).fill(0.25)),
  startSeconds,durationSeconds,overlapSeconds,final,
});
const isoSeconds = milliseconds => new Date(Math.floor(milliseconds / 1000) * 1000).toISOString().replace('.000Z','Z');
function runtimeConfig({state = 'online', apiUrl = 'https://fresh-tunnel.trycloudflare.com', publishedMs = Date.now() - 60000, expiresMs = publishedMs + 86400000} = {}) {
  return {version:1,state,apiUrl,publishedAt:isoSeconds(publishedMs),expiresAt:isoSeconds(expiresMs)};
}
function deferred() { let resolve, reject; const promise = new Promise((yes,no) => { resolve = yes; reject = no; }); return {promise,resolve,reject}; }
function setup(fetch, { FileUploader = class { detach() {} }, storedServer = '' } = {}) {
  const elements = new Map(), createdElements = new Map(), intervals = new Map(), timeouts = new Map(), objectUrls = new Map();
  const documentListeners = new Map();
  const location = {hash:'',hostname:'student.github.io',pathname:'/classroom/',search:''};
  const historyCalls = [];
  let storedServerValue = storedServer;
  let id = 0, mic;
  const makeElement = (name, value = '') => {
    const node = {
    name,tagName:name.toUpperCase(),value,style:{},children:[],open:false,
    classList:{values:new Set(),toggle(className,force){
      const enabled = force === undefined ? !this.values.has(className) : !!force;
      if (enabled) this.values.add(className); else this.values.delete(className);
      return enabled;
    }},
    replaceChildren(...children){ this.children = [...children]; },
    append(...children){ this.children.push(...children); },
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
    showModal(){ this.open = true; },
    close(){ this.open = false; },
    removeAttribute(attribute){ delete this[attribute]; },
    setAttribute(attribute,value){ this[attribute] = String(value); },
    click(){ this.clicked = true; },
    };
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
    const initialValue = name === 'language' ? 'ko' : name === 'export-format' ? 'text' : '';
    if (!elements.has(name)) elements.set(name, makeElement(name,initialValue));
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
  const document = {
    hidden:false,getElementById:element,querySelector:element,createElement,
    addEventListener(name,callback) {
      const callbacks = documentListeners.get(name) || [];
      callbacks.push(callback); documentListeners.set(name,callbacks);
    },
  };
  const context = vm.createContext({
    Blob, Headers, URL:TestURL, URLSearchParams, AbortController, console, crypto:webcrypto,
    document,
    window:{addEventListener(){}}, performance:{now:() => 0},
    location,history:{replaceState(...args){historyCalls.push(args);}},
    localStorage:{
      getItem(key){ return key === 'yeobaek-server' ? storedServerValue : ''; },
      setItem(key,value){ if (key === 'yeobaek-server') storedServerValue = String(value); },
    },
    setTimeout:(callback,delay = 0) => { const value = ++id; timeouts.set(value,{callback,delay}); return value; },
    clearTimeout:value => timeouts.delete(value),
    setInterval:(callback,delay = 0) => { const value = ++id; intervals.set(value,{callback,delay}); return value; },clearInterval:value => intervals.delete(value),
    fetch,
    TestCapture:class {
      constructor(callbacks) { mic = this; this.callbacks = callbacks; this.recording = false; this.paused = false; }
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
      async stop() {
        if (!this.recording && !this.paused) return;
        this.recording = false; this.paused = false; this.stopCalls = (this.stopCalls || 0) + 1;
        this.callbacks.onChunk(this.tail || {blob:encodeWav(new Float32Array(160).fill(0.25)),startSeconds:0,durationSeconds:0.01,overlapSeconds:0,final:true});
        if (this.stopError) throw this.stopError;
      }
    },
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
  const dispatchDocument = async name => {
    for (const callback of documentListeners.get(name) || []) callback();
    await tick(); await tick();
  };
  const created = tag => createdElements.get(tag)?.at(-1);
  const createdAll = tag => createdElements.get(tag) || [];
  const objectUrlBlob = value => objectUrls.get(value);
  return {run,element,created,createdAll,objectUrlBlob,intervals,timeouts,runTimeout,runInterval,dispatchDocument,document,location,historyCalls,storedServer:() => storedServerValue,microphone:() => mic};
}

test('activation presents and enforces the four-character minimum in the browser', () => {
  const app = setup(async () => response({}));
  app.run('setActivation(true)');
  assert.equal(app.element('password').minLength, 4);
  assert.equal(app.element('password-confirm').minLength, 4);
  assert.match(app.element('password-label').textContent, /4자 이상/);
});

test('interruption during lecture creation stops once and preserves the short tail without restarting a timer', async () => {
  const creation = deferred(), uploads = [];
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) return creation.promise;
    uploads.push(options); return response({segments:[]});
  });
  const start = app.run('startRecording()');
  await tick();
  assert.equal(app.run('recording'), true);
  assert.equal(app.element('record-button').disabled, false);
  app.microphone().callbacks.onInterrupted(new Error('중단됨'));
  await tick();
  creation.resolve(response({id:'lesson',title:'수업',created_at:new Date().toISOString(),segments:[]}));
  await start; await tick();
  assert.equal(app.run('recording || starting || stopping'), false);
  assert.equal(app.intervals.size, 0);
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].body.size, 1644);
  const audio = new DataView(await uploads[0].body.arrayBuffer());
  assert.equal(audio.getInt16(44, true), 8192);
  assert.equal(audio.getInt16(44 + 160 * 2, true), 0);
  assert.equal(app.run('pending.length'), 0);
});

test('the selected computer-audio source is passed to capture without a preceding network request', async () => {
  const requests = [];
  const app = setup(async (url, options) => {
    requests.push({url,method:options.method || 'GET'});
    if (url.endsWith('/lectures')) return response({id:'system-lesson',title:'온라인 강의',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    return response({segments:[]});
  });
  app.element('audio-source').value = 'system';
  app.element('audio-source').onchange();
  assert.equal(app.element('source-privacy').hidden, false);
  const starting = app.run('startRecording()');
  assert.equal(app.microphone().callbacks.source, 'system');
  assert.equal(requests.length, 0, 'capture is opened synchronously before lecture creation');
  await starting;
  await app.run('stopRecording()');
  await tick();
});

test('pause flushes a non-final boundary and resume continues the same lecture and timeline', async () => {
  let lectureCreates = 0;
  const uploads = [];
  const app = setup(async (url,options = {}) => {
    if (url.endsWith('/lectures')) {
      lectureCreates += 1;
      return response({id:'pause-lesson',title:'수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (url.includes('/lectures/pause-lesson/chunks')) {
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
  assert.ok(uploads.every(item => item.url.includes('/lectures/pause-lesson/chunks')));
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

test('a paused session remains busy and recording-present, and auth expiry stops its final guard', async () => {
  const app = setup(async url => url.endsWith('/lectures')
    ? response({id:'paused-auth-lesson',title:'수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201)
    : response({segments:[],recording_available:true,recording_finalized:true}));
  await app.run('startRecording()');
  app.microphone().pauseTail = chunk(0,2,0,false);
  app.microphone().tail = chunk(0,2,2,true);
  await app.run('pauseRecording()');
  assert.equal(app.run('isBusy()'),true,'beforeunload and navigation must remain guarded while paused');
  assert.equal(app.run('presenceActivity()'),'recording');
  app.run('showLogin()');
  await tick(); await tick();
  assert.equal(app.microphone().stopCalls,1);
  assert.equal(app.run('recording || paused || stopping'),false);
  assert.equal(app.element('workspace').hidden,true);
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
  const app = setup((url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') return creation.promise;
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
  creation.resolve(response({id:'new',title:'새 수업',created_at:'2026-02-01T00:00:00Z',segments:[]},201));
  await starting;
  assert.deepEqual(Array.from(app.run('lectures.map(lecture => lecture.id)')), ['new','old']);
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

test('Markdown and plain-text exports preserve content safely and lock during audio work', async () => {
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
  for (const id of ['export-format','download','recording-download','delete-lecture']) {
    assert.equal(app.element(id).disabled, true, `${id} must lock while recording`);
  }
});

test('AI correction stays raw by default, polls to completion, and exports only the selected version', async () => {
  const requests = [];
  let reads = 0;
  const app = setup(async (url, options = {}) => {
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
  const app = setup(async () => {
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
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') {
      return response({id:'live-correction',title:'현재 수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (url.endsWith('/lectures/live-correction/chunks')) {
      const final = options.headers.get('X-Final-Chunk') === 'true';
      const start = Number(options.headers.get('X-Start-Seconds'));
      events.push(final ? 'final-chunk' : 'chunk');
      return response({
        segments:[{id:`live-${start}`,start,end:start + 1,text:`문장 ${start}`}],
        recording_available:true,recording_finalized:final,
      });
    }
    if (url.endsWith('/lectures/live-correction/correction') && options.method === 'POST') {
      correctionPosts += 1; events.push('correction');
      return response({lecture_id:'live-correction',status:'queued',raw_revision:'a'.repeat(64)});
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
  assert.equal(app.run("scheduledCorrections.get('live-correction').status"),'scheduled');
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
  let loginPosts = 0, correctionPosts = 0;
  const app = setup((url, options = {}) => {
    if (url === `${oldUrl}/lectures` && options.method === 'POST') {
      return response({id:'moving-live',title:'현재 수업',language:'ko',created_at:'2026-01-01T00:00:00Z',segments:[]},201);
    }
    if (url === `${oldUrl}/lectures/moving-live/chunks`) {
      return response({segments:[{id:'first',start:0,end:2,text:'첫 문장'}],recording_available:true,recording_finalized:false});
    }
    if (url === `${newUrl}/auth/login`) {
      loginPosts += 1;
      return response({token:'new-live-token',user:{username:'user-alpha',is_admin:false}});
    }
    if (url === `${newUrl}/lectures` && !options.method) {
      return response([{id:'moving-live',title:'현재 수업',language:'ko',created_at:'2026-01-01T00:00:00Z',
        segments:[{id:'first',start:0,end:2,text:'첫 문장'}],recording_available:true,recording_finalized:false}]);
    }
    if (url === `${newUrl}/imports`) return response([]);
    if (url === `${newUrl}/status`) return response({model_state:'ready'});
    if (url === `${newUrl}/lectures/moving-live/chunks`) {
      assert.equal(options.headers.get('Authorization'),'Bearer new-live-token');
      assert.equal(options.headers.get('X-Final-Chunk'),'true');
      events.push('final');
      return response({segments:[{id:'tail',start:6,end:9,text:'마지막 문장'}],recording_available:true,recording_finalized:true});
    }
    if (url === `${newUrl}/lectures/moving-live/correction` && options.method === 'POST') {
      correctionPosts += 1; events.push('correction');
      return response({lecture_id:'moving-live',status:'queued'});
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
  assert.equal(app.run('pending.length'),1);
  assert.equal(app.run("scheduledCorrections.get('moving-live').sessionToken"),'');
  assert.equal(app.run("scheduledCorrections.get('moving-live').server"),'');
  assert.equal(app.element('username').value,'user-alpha');

  app.element('username').value = 'user-beta';
  app.element('password').value = 'different-account';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  assert.equal(loginPosts,0);
  assert.match(app.element('auth-error').textContent,/이전 계정/);

  app.element('username').value = 'user-alpha';
  app.element('password').value = 'same-account';
  await app.element('auth-form').onsubmit({preventDefault(){}});
  for (let index = 0; index < 6; index += 1) await tick();
  assert.equal(loginPosts,1);
  assert.equal(app.run('pending.length'),0);
  assert.equal(correctionPosts,1);
  assert.deepEqual(events,['final','correction']);
  assert.equal(app.run('scheduledCorrections.size'),0);
});

test('a finalized past lecture can be corrected while live chunks stay attached to the active lecture', async () => {
  const chunkUrls = [];
  let pastCorrectionPosts = 0;
  const past = {
    id:'past-finalized',title:'지난 수업',language:'ko',created_at:'2026-01-01T00:00:00Z',
    recording_available:true,recording_finalized:true,
    segments:[{id:'past-raw',start:0,end:2,text:'지난 수업 원문'}],
  };
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') {
      return response({id:'active-live',title:'현재 수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (url.endsWith('/lectures/active-live/chunks')) {
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
  for (const id of ['new-note','logout','recording-file','download','recording-download','delete-lecture']) {
    assert.equal(app.element(id).disabled,true,`${id} must remain locked during background capture`);
  }
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
  assert.ok(chunkUrls.every(url => url.endsWith('/lectures/active-live/chunks')));
  assert.equal(app.run('current.id'),'past-finalized');
  assert.equal(app.run("captureSession.lecture.segments.length"),2);
  assert.equal(app.element('transcript').children[0],pastTranscriptNode,
    'background live chunks must not rebuild the past transcript being read');

  app.element('return-live-capture').onclick();
  assert.equal(app.run('current.id'),'active-live');
  assert.equal(app.run('current.segments.length'),2,'returning must retain chunks received while viewing history');
  assert.equal(app.element('live-capture-banner').hidden,true);
  assert.equal(app.element('main-content').focused,true);
  await app.run('stopRecording()');
  await tick(); await tick();
});

test('paused capture supports both current-lecture scheduling and past-lecture correction', async () => {
  let activeCorrectionPosts = 0, pastCorrectionPosts = 0;
  const past = {
    id:'paused-past',title:'지난 기록',language:'ko',created_at:'2026-01-01T00:00:00Z',recording_finalized:true,
    segments:[{id:'old',start:0,end:1,text:'지난 원문'}],
  };
  const app = setup(async (url, options = {}) => {
    if (url.endsWith('/lectures') && options.method === 'POST') {
      return response({id:'paused-live',title:'일시정지 수업',language:'ko',created_at:new Date().toISOString(),segments:[]},201);
    }
    if (url.endsWith('/lectures/paused-live/chunks')) {
      const start = Number(options.headers.get('X-Start-Seconds'));
      return response({segments:[{id:`p-${start}`,start,end:start + 1,text:'현재 원문'}],recording_available:true,
        recording_finalized:options.headers.get('X-Final-Chunk') === 'true'});
    }
    if (url.endsWith('/lectures/paused-live/correction') && options.method === 'POST') {
      activeCorrectionPosts += 1; return response({lecture_id:'paused-live',status:'queued'});
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
  assert.equal(app.run("scheduledCorrections.has('paused-live')"),true);
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
  assert.equal(app.run('current.id'),'paused-live');
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
    showLogin(); token='next-token'; user='next-user';
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
  assert.match(app.element('recording-download').textContent, /마무리/);
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

test('recording download repairs an unfinished saved WAV before requesting its ticket', async () => {
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
    current={id:'unfinished',title:'복구 수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}]; renderCurrent(); renderHistory();
  `);
  assert.equal(app.element('recording-download').disabled, false);
  assert.match(app.element('recording-download').textContent, /마무리/);
  await app.run('downloadRecording()');
  assert.deepEqual(requests.map(request => request.url.replace('https://classroom.example','')), [
    '/lectures/unfinished/recording-finalize',
    '/lectures/unfinished/recording-download-ticket',
  ]);
  assert.ok(requests.every(request => request.method === 'POST' && request.authorization === 'Bearer old-token'));
  assert.equal(app.run('current.recording_finalized'), true);
  assert.equal(app.run('lectures[0].recording_finalized'), true);
  assert.equal(app.created('a').href, `https://classroom.example${ticketPath}`);
  assert.equal(app.created('a').clicked, true);
});

test('deletion requires confirmation and invalidates an older lecture response', async () => {
  const lateLecture = deferred();
  const lateList = deferred();
  const requests = [];
  const app = setup((url, options = {}) => {
    requests.push({url,method:options.method || 'GET'});
    if (url.endsWith('/lectures') && !options.method) return lateList.promise;
    if (url.endsWith('/lectures/second') && !options.method) return lateLecture.promise;
    if (url.endsWith('/lectures/first') && options.method === 'DELETE') return response(null,204);
    return response({});
  });
  app.run(`
    lectures=[
      {id:'first',title:'지울 수업',created_at:'2026-01-02T00:00:00Z'},
      {id:'second',title:'늦은 수업',created_at:'2026-01-01T00:00:00Z'},
    ];
    current={id:'first',title:'지울 수업',created_at:'2026-01-02T00:00:00Z',segments:[]};
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
  assert.deepEqual(Array.from(app.run('lectures.map(lecture => lecture.id)')), ['second']);
  assert.ok(requests.some(item => item.method === 'DELETE' && item.url.endsWith('/lectures/first')));

  lateLecture.resolve(response({id:'second',title:'늦게 온 내용',created_at:'2026-01-01T00:00:00Z',segments:[]}));
  lateList.resolve(response([
    {id:'first',title:'삭제 전 목록',created_at:'2026-01-02T00:00:00Z'},
    {id:'second',title:'남은 수업',created_at:'2026-01-01T00:00:00Z'},
  ]));
  await selecting;
  await refreshing;
  assert.equal(app.run('current'), null, 'the response started before deletion must not restore a note');
  assert.deepEqual(Array.from(app.run('lectures.map(lecture => lecture.id)')), ['second'], 'a stale list must not restore the deleted note');
});

test('deletion safely retries once after a lost response', async () => {
  let deleteCalls = 0;
  let app;
  app = setup(async (_url, options = {}) => {
    if (options.method !== 'DELETE') return response({});
    deleteCalls += 1;
    if (deleteCalls === 1) throw app.run("new TypeError('response lost')");
    return response({status:'deleted'});
  });
  app.run(`
    current={id:'lost-response',title:'응답이 끊긴 수업',created_at:'2026-01-01T00:00:00Z',segments:[]};
    lectures=[current]; renderCurrent(); renderHistory();
  `);
  app.element('delete-lecture').onclick();
  await app.element('delete-confirm').onclick();
  assert.equal(deleteCalls, 2);
  assert.equal(app.run('current'), null);
  assert.equal(app.run('lectures.length'), 0);
  assert.match(app.element('notice').textContent, /삭제했어요/);
});

test('deleting an unfinished recovered lesson clears its local correction reservation', async () => {
  const app = setup((url, options = {}) => options.method === 'DELETE'
    ? response(null,204) : response({}));
  app.run(`
    current={id:'delete-reserved',title:'지울 미완료 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'saved',start:0,end:1,text:'저장됨'}],recording_available:true,recording_finalized:false};
    lectures=[current]; captureSession={id:'ended-capture',lecture:current};
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,finalized:false,status:'scheduled'});
    updateControls();
  `);
  assert.equal(app.run('isBusy()'),false);
  app.element('delete-lecture').onclick();
  await app.element('delete-confirm').onclick();
  assert.equal(app.run('scheduledCorrections.size'),0);
  assert.equal(app.run('captureSession'),null);
  assert.equal(app.run('hasOwnerLockedWork()'),false);
});

test('an explicit deletion failure keeps the lecture available for a later retry', async () => {
  let deleteCalls = 0;
  const app = setup(async (_url, options = {}) => {
    if (options.method === 'DELETE') {
      deleteCalls += 1;
      return response({detail:'녹음 파일을 지우지 못했습니다.'},503);
    }
    return response({});
  });
  app.run(`
    current={id:'cleanup-failed',title:'남겨 둘 수업',created_at:'2026-01-01T00:00:00Z',segments:[]};
    lectures=[current]; renderCurrent(); renderHistory();
  `);
  app.element('delete-lecture').onclick();
  await app.element('delete-confirm').onclick();
  assert.equal(deleteCalls, 1, 'an explicit server failure must not be blindly retried');
  assert.equal(app.run('current.id'), 'cleanup-failed');
  assert.equal(app.run('lectures[0].id'), 'cleanup-failed');
  assert.equal(app.element('delete-dialog').open, false);
  assert.match(app.element('notice').textContent, /삭제하지 못했습니다/);
});

test('closing deletion confirmation makes no request and an old account response cannot clear the next account', async () => {
  const deletion = deferred();
  let deleteCalls = 0;
  const app = setup((url, options = {}) => {
    if (options.method === 'DELETE') { deleteCalls += 1; return deletion.promise; }
    return response({});
  });
  app.run(`current={id:'old-lesson',title:'기존 수업',created_at:'2026-01-01T00:00:00Z',segments:[]}; lectures=[current]; renderCurrent(); renderHistory()`);
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
    current={id:'next-lesson',title:'다음 계정 수업',created_at:'2026-01-02T00:00:00Z',segments:[]};
    lectures=[current];
  `);
  deletion.resolve(response(null,204));
  await removing;
  assert.equal(app.run('current.id'), 'next-lesson');
  assert.equal(app.run('lectures[0].id'), 'next-lesson');
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
    current={id:'private-note',title:'이전 계정 비공개 수업',language:'en',created_at:'2026-01-01T00:00:00Z',
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
  const app = setup(async url => url.endsWith('/lectures')
    ? response({id:'flush-warning-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201)
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
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      lectureIds.push(options.headers.get('X-Lecture-Id'));
      attempts += 1;
      if (attempts === 1) throw new TypeError('offline');
      return response({id:'retry-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]});
    }
    uploads += 1; return response({segments:[]});
  });
  await app.run('startRecording()');
  assert.equal(app.run('draft.buffered.length'), 1);
  assert.equal(app.run('isBusy()'), true);
  assert.ok(app.run('sendError'));
  await app.run('retryPending()'); await tick();
  assert.equal(uploads, 1);
  assert.equal(lectureIds.length, 2);
  assert.equal(lectureIds[0], lectureIds[1]);
  assert.equal(app.run('draft'), null);
  assert.equal(app.run('pending.length'), 0);
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

test('the workspace stays hidden until login history and import recovery finish', async () => {
  const imports = deferred();
  const app = setup((url) => {
    if (url.endsWith('/auth/login')) return response({token:'fresh-token',user:{username:'user-alpha'}});
    if (url.endsWith('/lectures')) return response([]);
    if (url.endsWith('/imports')) return imports.promise;
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
  await submitting;
  assert.equal(app.element('auth-screen').hidden, true);
  assert.equal(app.element('workspace').hidden, false);
  assert.equal(app.element('record-button').disabled, false);
  assert.equal(app.element('recording-file').disabled, false);
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

test('server changes are blocked throughout recording even before the first audio chunk', async () => {
  let requests = 0;
  const app = setup(async () => { requests += 1; return response({}); });
  app.run('recording=true');
  app.element('api-url').value = 'https://other.example';
  await app.element('connection-form').onsubmit({preventDefault(){}});
  assert.equal(requests, 0);
  assert.equal(app.run('apiUrl'), 'https://classroom.example');
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
  app.run("pending=[{blob:new Blob([new Uint8Array(1644)],{type:'audio/wav'}),startSeconds:0,durationSeconds:0.05,overlapSeconds:0,final:false,id:'stable-id',lectureId:'lesson'}]; sendError='old tunnel failed'");
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
    current={id:'lesson',title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[]}; lectures=[current];
    pending=[{blob:new Blob([new Uint8Array(1644)],{type:'audio/wav'}),startSeconds:0,durationSeconds:0.05,overlapSeconds:0,final:false,id:'stable-id',lectureId:'lesson'}];
    setConnectionState('connected'); renderCurrent();
  `);
  await app.run('drain()');

  assert.equal(requests.some(request => request.url.startsWith(oldUrl)), false);
  assert.deepEqual(requests.map(request => request.url), [requests[0].url,`${newUrl}/health`]);
  assert.equal(app.run('apiUrl'), newUrl);
  assert.equal(app.run('token'), '');
  assert.equal(app.run('user'), 'user-alpha');
  assert.equal(app.run('pending[0].id'), 'stable-id');
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
      return response({id:'resilient-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
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

test('an in-flight idempotent 409 retries while a changed-payload 409 remains manual', async () => {
  let attempt = 0;
  const ids = [];
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      return response({id:'racing-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201);
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

  const rejected = setup(async url => url.endsWith('/lectures')
    ? response({id:'changed-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201)
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

test('permanent upload failure stops safely without an automatic loop and retains the tail for manual recovery', async () => {
  const uploads = [];
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      return response({id:'rejected-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    uploads.push(options.headers.get('X-Chunk-Id'));
    return response({detail:'녹음 저장 공간이 부족합니다.'},507);
  });

  await app.run('startRecording()');
  app.microphone().tail = chunk(6, 2.2, 2, true);
  app.microphone().callbacks.onChunk(chunk(0));
  await tick(); await tick();

  assert.equal(app.run('recording || starting || stopping'), false);
  assert.equal(app.microphone().stopCalls, 1);
  assert.equal(uploads.length, 1);
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

test('temporary outage keeps recording below the queue limit and stops once backpressure reaches the limit', async () => {
  const app = setup(async (url) => {
    if (url.endsWith('/lectures')) {
      return response({id:'backpressure-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201);
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

  app.microphone().tail = chunk(62, 2.2, 2, true);
  app.microphone().callbacks.onChunk(chunk(54, 10, 2));
  await tick(); await tick();
  assert.equal(app.run('recording || starting || stopping'), false);
  assert.equal(app.microphone().stopCalls, 1);
  assert.equal(app.run('pending.length'), 9, 'the required final tail is retained beyond the normal queue cap');
  assert.equal(app.run('pending[8].startSeconds'), 62);
  assert.equal(app.run('pending[8].overlapSeconds'), 2);
  assert.equal(app.run('pending[8].final'), true);
});

test('repeated 503 responses stop after the bounded automatic retry budget', async () => {
  let uploads = 0;
  const app = setup(async url => {
    if (url.endsWith('/lectures')) {
      return response({id:'failing-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    uploads += 1;
    return response({detail:'모델 처리 실패'},503,{'Retry-After':'0'});
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
  assert.equal(app.run('retryTimer'), null);
  assert.match(app.run('sendError'), /자동 재전송 8회/);
  assert.equal(app.run('recording || starting || stopping'), false);
  assert.equal(app.microphone().stopCalls, 1);
  assert.equal(app.run('pending.length'), 2, 'the failed head and final microphone tail remain recoverable');
});

test('skipping the last failed chunk finalizes the saved recording and applies its flags', async () => {
  const finalization = deferred();
  let request;
  const app = setup((url, options = {}) => {
    if (url.endsWith('/lectures/recoverable-lesson/recording-finalize')) {
      request = {url,options};
      return finalization.promise;
    }
    return response({});
  });
  app.run(`
    current={id:'recoverable-lesson',title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
    pending=[{id:'failed-final',lectureId:'recoverable-lesson',final:true,downloadRequested:true}];
    sendError='마지막 조각 실패'; renderCurrent();
  `);
  app.run('skipFailedChunk()');
  assert.equal(app.run('pending.length'), 0);
  assert.equal(app.run('recordingFinalizePending'), true);
  assert.equal(app.run('isBusy()'), true);
  for (const id of ['new-note','record-button','recording-file','download','recording-download','delete-lecture']) {
    assert.equal(app.element(id).disabled, true, `${id} must lock while finalizing the recording`);
  }
  assert.match(request.url, /\/lectures\/recoverable-lesson\/recording-finalize$/);
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
  let finalizations = 0, correctionPosts = 0, correctionSawRecoveredTail = false;
  let app;
  app = setup((url, options = {}) => {
    if (url.endsWith('/lectures/skipped-correction/recording-finalize')) {
      finalizations += 1;
      return response({recording_available:true,recording_finalized:true,
        segments:[{id:'recovered-tail',start:4,end:4.6,text:'복구된 마지막 문장'}]});
    }
    if (url.endsWith('/lectures/skipped-correction/correction') && options.method === 'POST') {
      correctionPosts += 1;
      correctionSawRecoveredTail = app.run("current.segments.some(segment => segment.id === 'recovered-tail')");
      return response({lecture_id:'skipped-correction',status:'queued'});
    }
    return response({});
  });
  app.run(`
    current={id:'skipped-correction',title:'복구 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'saved',start:0,end:4,text:'저장된 원문'}],recording_available:true,recording_finalized:false};
    lectures=[current];
    captureSession={id:'capture-for-skip',lecture:current};
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,status:'scheduled'});
    pending=[{id:'failed-final',lectureId:current.id,final:true,downloadRequested:true}];
    sendError='마지막 조각 실패'; renderCurrent();
    skipFailedChunk();
  `);
  await tick(); await tick(); await tick();
  assert.equal(finalizations,1);
  assert.equal(correctionPosts,1);
  assert.equal(correctionSawRecoveredTail,true,'the reserved correction must start after the recovered tail is merged');
  assert.equal(app.run("current.segments.filter(segment => segment.id === 'recovered-tail').length"),1);
  assert.equal(app.run('scheduledCorrections.size'),0);
  assert.equal(app.run('current.recording_finalized'),true);
  app.run("applyRecordingFlags('skipped-correction',{recording_available:true,recording_finalized:true,segments:[{id:'recovered-tail',start:4,end:4.6,text:'복구된 마지막 문장'}]})");
  await tick(); await tick();
  assert.equal(correctionPosts,1,'repeated finalized state must not submit the reservation again');
  assert.equal(app.run("current.segments.filter(segment => segment.id === 'recovered-tail').length"),1,
    'an idempotent finalization response must not duplicate the recovered segment');
});

test('a failed recording finalization leaves its correction reserved without locking recovery controls', async () => {
  let correctionPosts = 0;
  const app = setup((url, options = {}) => {
    if (url.endsWith('/recording-finalize')) {
      return response({detail:'마지막 음성 인식을 마무리하지 못했습니다.'},503,{'Retry-After':'5'});
    }
    if (url.endsWith('/lectures/retry-recovery/correction') && options.method === 'POST') {
      correctionPosts += 1;
      return response({lecture_id:'retry-recovery',status:'queued'});
    }
    return response({});
  });
  app.run(`
    current={id:'retry-recovery',title:'복구 수업',created_at:'2026-01-01T00:00:00Z',
      segments:[{id:'saved',start:0,end:4,text:'저장된 원문'}],recording_available:true,recording_finalized:false};
    lectures=[current]; captureSession={id:'capture-recovery',lecture:current};
    scheduledCorrections.set(current.id,{lectureId:current.id,owner:user,sessionToken:token,server:apiUrl,
      captureId:captureSession.id,finalized:false,status:'scheduled'});
    pending=[{id:'failed-final',lectureId:current.id,final:true,downloadRequested:true}];
    sendError='마지막 조각 실패';
    skipFailedChunk();
  `);
  await tick(); await tick();
  assert.equal(app.run('recordingFinalizePending'),false);
  assert.equal(app.run("scheduledCorrections.get('retry-recovery').status"),'scheduled');
  assert.equal(app.run('hasOwnerLockedWork()'),true,'same-owner reauthentication must retain the reservation');
  assert.equal(app.run('isBusy()'),false,'a stored retryable reservation must not deadlock the workspace');
  assert.equal(app.element('recording-download').disabled,false,'the user can retry finalization from the WAV button');
  assert.equal(app.element('logout').disabled,false);

  app.run(`
    captureSession={id:'newer-capture',lecture:{id:'newer-lesson',segments:[]}};
    applyRecordingFlags('retry-recovery',{recording_available:true,recording_finalized:true,segments:[]});
  `);
  await tick(); await tick();
  assert.equal(correctionPosts,1,'a later successful finalization still honors the old reservation');
  assert.equal(app.run('captureSession.id'),'newer-capture','finalizing an older lesson must not release a newer capture');
});

test('skipping the only failed chunk never claims that a server WAV exists', async () => {
  const app = setup((url) => url.endsWith('/recording-finalize')
    ? response({recording_available:false,recording_finalized:true})
    : response({}));
  app.run(`
    current={id:'empty-recording',title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:false,recording_finalized:false};
    lectures=[{...current}];
    pending=[{id:'failed-only',lectureId:'empty-recording',final:true,downloadRequested:true}];
    sendError='첫 조각 실패';
    skipFailedChunk();
  `);
  await tick(); await tick();
  assert.equal(app.run('current.recording_available'), false);
  assert.equal(app.run('current.recording_finalized'), true);
  assert.equal(app.element('recording-download').disabled, true);
  assert.match(app.element('notice').textContent, /내려받을 녹음은 없습니다/);
  assert.doesNotMatch(app.element('notice').textContent, /WAV로 마무리/);
});

test('recording finalization retries once after a transient response loss', async () => {
  let finalizeCalls = 0;
  let app;
  app = setup(async (url) => {
    if (!url.endsWith('/recording-finalize')) return response({});
    finalizeCalls += 1;
    if (finalizeCalls === 1) throw app.run("new TypeError('response lost')");
    return response({recording_available:true,recording_finalized:true});
  });
  app.run(`
    current={id:'retry-finalize',title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
    pending=[{id:'failed-final',lectureId:'retry-finalize',final:true,downloadRequested:true}];
    sendError='마지막 조각 실패';
    skipFailedChunk();
  `);
  await tick(); await tick();
  assert.equal(finalizeCalls, 2);
  assert.equal(app.run('recordingFinalizePending'), false);
  assert.equal(app.run('current.recording_finalized'), true);
});

test('recording finalization waits for an already running deterministic guard', async () => {
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
    current={id:'busy-finalize',title:'수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
    pending=[{id:'failed-final',lectureId:'busy-finalize',final:true,downloadRequested:true}];
    sendError='마지막 조각 실패';
    skipFailedChunk();
  `);
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
  const app = setup((url) => url.endsWith('/recording-finalize') ? finalization.promise : response({}));
  app.run(`
    current={id:'old-lesson',title:'이전 수업',created_at:'2026-01-01T00:00:00Z',segments:[],recording_available:true,recording_finalized:false};
    lectures=[{...current}];
    pending=[{id:'failed-final',lectureId:'old-lesson',final:true,downloadRequested:true}];
    sendError='마지막 조각 실패';
    skipFailedChunk();
  `);
  assert.equal(app.run('recordingFinalizePending'), true);
  app.run(`
    showLogin(); token='next-token'; user='next-user';
    current={id:'next-lesson',title:'다음 수업',created_at:'2026-01-02T00:00:00Z',segments:[],recording_available:false,recording_finalized:false};
    lectures=[{...current}];
  `);
  finalization.resolve(response({recording_available:true,recording_finalized:true,
    segments:[{id:'old-tail',start:3,end:4,text:'다른 계정에 보이면 안 됨'}]}));
  await tick(); await tick();
  assert.equal(app.run('current.id'), 'next-lesson');
  assert.equal(app.run('current.recording_available || current.recording_finalized'), false);
  assert.equal(app.run("current.segments.some(segment => segment.id === 'old-tail')"),false);
  assert.equal(app.run('lectures[0].id'), 'next-lesson');
  assert.equal(app.run('recordingFinalizePending'), false);
});

test('a failed head requires a separate download request and save confirmation before skipping', async () => {
  let uploads = 0;
  const app = setup(async url => {
    if (url.endsWith('/lectures')) {
      return response({id:'recoverable-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    uploads += 1;
    if (uploads === 1) return response({detail:'결정적 모델 오류'},422);
    return response({segments:[]});
  });
  await app.run('startRecording()');
  app.microphone().callbacks.onChunk(chunk(0));
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
    pending=[{blob:new Blob([new Uint8Array(32044)],{type:'audio/wav'}),startSeconds:0,durationSeconds:1,
      overlapSeconds:0,final:false,id:'failed-live',lectureId:live.id}];
    sendError='전송 실패';
  `);
  await app.run('saveFailedChunk()');
  assert.match(app.created('a').download,/^현재 수업_/);
  assert.doesNotMatch(app.created('a').download,/지난 수업/);
});
