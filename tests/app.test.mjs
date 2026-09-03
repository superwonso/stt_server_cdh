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
function deferred() { let resolve, reject; const promise = new Promise((yes,no) => { resolve = yes; reject = no; }); return {promise,resolve,reject}; }
function setup(fetch, { FileUploader = class { detach() {} } } = {}) {
  const elements = new Map(), intervals = new Set(), timeouts = new Map();
  const location = {hash:'',hostname:'student.github.io',pathname:'/classroom/',search:''};
  const historyCalls = [];
  let id = 0, mic;
  const element = name => {
    if (!elements.has(name)) elements.set(name, {
      value: name === 'language' ? 'ko' : '', style:{}, classList:{toggle(){}},
      replaceChildren(){},append(){},querySelectorAll(){return [];},focus(){},showModal(){},close(){},
      removeAttribute(name){ delete this[name]; },
      click(){ this.clicked = true; },
    });
    return elements.get(name);
  };
  const context = vm.createContext({
    Blob, Headers, URL, URLSearchParams, AbortController, console, crypto:webcrypto,
    document:{getElementById:element,querySelector:element,createElement:element,addEventListener(){}},
    window:{addEventListener(){}}, performance:{now:() => 0},
    location,history:{replaceState(...args){historyCalls.push(args);}},
    localStorage:{getItem(){return '';},setItem(){}},
    setTimeout:(callback,delay = 0) => { const value = ++id; timeouts.set(value,{callback,delay}); return value; },
    clearTimeout:value => timeouts.delete(value),
    setInterval:() => { const value = ++id; intervals.add(value); return value; },clearInterval:value => intervals.delete(value),
    fetch,
    TestCapture:class {
      constructor(callbacks) { mic = this; this.callbacks = callbacks; }
      async start() { this.recording = true; }
      async stop() {
        if (!this.recording) return;
        this.recording = false; this.stopCalls = (this.stopCalls || 0) + 1;
        this.callbacks.onChunk(this.tail || {blob:encodeWav(new Float32Array(160).fill(0.25)),startSeconds:0,durationSeconds:0.01,overlapSeconds:0,final:true});
        if (this.stopError) throw this.stopError;
      }
    },
    TestFileUploader:FileUploader,
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run("apiUrl='https://classroom.example'; token='old-token'; user='user-alpha';");
  const runTimeout = async delay => {
    const entry = [...timeouts].find(([, timer]) => timer.delay === delay);
    assert.ok(entry, `missing ${delay} ms timer`);
    timeouts.delete(entry[0]); entry[1].callback(); await tick(); await tick();
  };
  return {run,element,intervals,timeouts,runTimeout,location,historyCalls,microphone:() => mic};
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
  Object.defineProperty(app.element('recording-file'), 'files', {configurable:true,value:[file]});
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
    return response({segments:[{id:`segment-${start}`,start,end:start + 1,text:`문장 ${start}`}]});
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
});

test('permanent upload failure stops safely without an automatic loop and retains the tail for manual recovery', async () => {
  const uploads = [];
  const app = setup(async (url, options) => {
    if (url.endsWith('/lectures')) {
      return response({id:'rejected-lesson',title:'수업',created_at:new Date().toISOString(),segments:[]},201);
    }
    uploads.push(options.headers.get('X-Chunk-Id'));
    return response({detail:'오디오 형식이 올바르지 않습니다.'},422);
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
  assert.equal(app.element('a').clicked, true);
  assert.match(app.element('a').download, /처리실패\.wav$/);
  assert.equal(uploads, 2, 'the next queued chunk is sent after the preserved head is skipped');
  assert.equal(app.run('pending.length'), 0);
  assert.equal(app.run('sendError'), '');
});
