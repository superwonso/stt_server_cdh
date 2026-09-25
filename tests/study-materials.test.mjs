import assert from 'node:assert/strict';
import {test} from 'node:test';
import {File} from 'node:buffer';
import {setImmediate as turn} from 'node:timers/promises';
import {createHash} from 'node:crypto';
import {createMaterialPanel,validMaterial} from '../web/study-materials.js';

const A='11111111-1111-4111-8111-111111111111', B='22222222-2222-4222-8222-222222222222';
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
const clone=value=>structuredClone(value);
class Element {
  constructor(tag,document) { this.tagName=tag; this.ownerDocument=document; this.children=[]; this.dataset={}; this.listeners={}; this.attributes={}; this._text=''; this.disabled=false; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(child=>child.textContent).join(''); }
  set innerHTML(_) { throw Error('Untrusted HTML must never be rendered'); }
  append(...children) { for(const child of children) { child.parent=this; this.children.push(child); } }
  replaceChildren(...children) { this._text=''; this.children=[]; this.append(...children); }
  remove() { if(this.parent) this.parent.children=this.parent.children.filter(child=>child!==this); }
  setAttribute(key,value) { this.attributes[key]=value; }
  addEventListener(name,callback) { (this.listeners[name]??=[]).push(callback); }
  async fire(name) { for(const callback of this.listeners[name]||[]) await callback({target:this}); }
  click() { if(this.tagName==='a') { this.ownerDocument.downloads.push({url:this.href,filename:this.download}); return; } if(!this.disabled) return this.fire('click'); }
}
function all(root,predicate) { return [root,...root.children.flatMap(child=>all(child,predicate))].filter(predicate); }
const action=(root,name)=>all(root,node=>node.dataset.action===name)[0];
function row(id=A,scope={kind:'lecture',id:A}) {
  return {id,filename:'synthetic.pdf',kind:'pdf',size_bytes:3,uploaded_bytes:0,status:'processing',revision:1,
    error_code:'awaiting_upload',unit_count:0,warning_count:0,course_id:scope.kind==='course'?scope.id:null,
    lecture_id:scope.kind==='lecture'?scope.id:null};
}
function harness(t) {
  const document={downloads:[],defaultView:{confirm:()=>true},createElement(tag){return new Element(tag,this);}};
  const container=new Element('div',document), records=new Map(), calls=[], changed=[], created=[], revoked=[];
  let key='synthetic-owner:synthetic-token:synthetic-server',hook=null;
  t.mock.method(URL,'createObjectURL',blob=>{const url='blob:synthetic-'+created.length;created.push({url,blob});return url;});
  t.mock.method(URL,'revokeObjectURL',url=>revoked.push(url));
  async function api(path,options={}) {
    const call={path,method:options.method||'GET',...options}; calls.push(call);
    if(hook) { const result=await hook(call); if(result!==undefined) return result; }
    if(options.signal?.aborted) throw Object.assign(Error('abort'),{name:'AbortError'});
    const scopeMatch=path.match(/^\/(lectures|courses)\/([^/]+)\/materials$/);
    if(scopeMatch) {
      const scope={kind:scopeMatch[1]==='lectures'?'lecture':'course',id:scopeMatch[2]};
      if(call.method==='GET') return {materials:[...records.values()].filter(item=>validMaterial(item.row,scope)).map(item=>clone(item.row))};
      const body=JSON.parse(options.body), previous=records.get(body.id);
      if(previous && (previous.hash!==body.sha256 || previous.row.filename!==body.filename || previous.row.size_bytes!==body.size_bytes)) {
        throw Object.assign(Error('synthetic immutable mismatch'),{status:409});
      }
      if(!previous) records.set(body.id,{row:{...row(body.id,scope),filename:body.filename,kind:body.filename.toLowerCase().endsWith('.pdf')?'pdf':'pptx',size_bytes:body.size_bytes},hash:body.sha256,bytes:Buffer.alloc(0)});
      return clone(records.get(body.id).row);
    }
    const match=path.match(/^\/study-materials\/([^/]+)(?:\/(content|convert|original))?$/);
    assert.ok(match,'Unexpected route '+path);
    const item=records.get(match[1]); assert.ok(item,'Expected reserved material');
    if(match[2]==='content') {
      const bytes=Buffer.from(await options.body.arrayBuffer()),offset=Number(options.headers['X-Upload-Offset']);
      assert.equal(options.headers['Content-Type'],'application/octet-stream');
      assert.equal(options.headers['X-Part-SHA256'],hash(bytes));
      assert.equal(offset,item.bytes.length);
      item.bytes=Buffer.concat([item.bytes,bytes]);item.row.uploaded_bytes=item.bytes.length;
      return clone(item.row);
    }
    if(match[2]==='convert') { assert.equal(hash(item.bytes),item.hash);item.row.error_code='converting';return clone(item.row); }
    if(match[2]==='original') { assert.equal(options.responseType,'material-file');return new Blob([item.bytes],{type:'application/octet-stream'}); }
    if(call.method==='DELETE') { records.delete(match[1]);return {status:'deleted'}; }
    return {...clone(item.row),document:item.document?clone(item.document):null};
  }
  const panel=createMaterialPanel({container,api,scopeKey:()=>key,onChanged:scope=>changed.push(scope)});
  t.after(()=>panel.destroy());
  function ready(id,text='# Synthetic final slide\n<script>not executable</script>') {
    const item=records.get(id);item.row.status='ready';item.row.error_code=null;item.row.unit_count=1;item.row.revision++;
    item.document={markdown:text};
  }
  async function select(file) { const picker=action(container,'select-file');picker.files=[file];await picker.fire('change'); }
  async function click(name) {const button=action(container,name);assert.ok(button,'Missing '+name);assert.equal(button.disabled,false,'Disabled '+name);await button.click();}
  return {panel,container,document,records,calls,changed,created,revoked,ready,select,click,
    set key(value){key=value;},set hook(value){hook=value;}};
}

test('material metadata rejects foreign scope, inconsistent totals and unsafe names',()=>{
  const scope={kind:'lecture',id:A},base=row();assert.equal(validMaterial(base,scope),true);
  for(const change of [{lecture_id:B},{course_id:B},{id:'../file'},{size_bytes:33554433},
    {uploaded_bytes:4},{uploaded_bytes:-1},{filename:'../x.pdf'},{filename:'x.pptx'},{error_code:'raw private error'},
    {status:'ready',error_code:null},{unit_count:NaN},{revision:0}]) {
    assert.equal(validMaterial({...base,...change},scope),false);
  }
});

test('file selection does not send bytes; explicit upload hashes bounded parts and conversion is separate',async t=>{
  const h=harness(t);await h.panel.setScope({kind:'lecture',id:A});
  const bytes=Buffer.alloc(480*1024+7,0x61),file=new File([bytes],'자료.pdf');
  await h.select(file);assert.equal(h.calls.length,1);
  await h.click('upload');
  const id=[...h.records.keys()][0];
  assert.deepEqual(h.records.get(id).bytes,bytes);
  assert.equal(h.records.get(id).hash,hash(bytes));
  assert.equal(h.calls.filter(call=>call.method==='PUT').length,2);
  assert.deepEqual(h.calls.filter(call=>call.method==='PUT').map(call=>call.body.size),[480*1024,7]);
  assert.equal(h.calls.filter(call=>call.path.endsWith('/convert')).length,0);
  await h.click('convert');assert.equal(h.calls.filter(call=>call.path.endsWith('/convert')).length,1);
  assert.ok(h.changed.length>=2);
});

test('invalid files do not reserve or upload and errors stay plain text',async t=>{
  const h=harness(t);await h.panel.setScope({kind:'lecture',id:A});
  for(const file of [new File(['x'],'secret.exe'),new File([],'empty.pdf'),new File(['x'],'../x.pdf')]) {
    await h.select(file);assert.equal(action(h.container,'upload').disabled,true);
  }
  assert.equal(h.calls.length,1);assert.match(h.container.textContent,/PDF 또는 PPTX/);
});

test('partial reselection validates immutable hash before resuming from exact acknowledged offset',async t=>{
  const h=harness(t),bytes=Buffer.from('complete synthetic PDF');
  h.records.set(B,{row:{...row(B),filename:'resume.pdf',size_bytes:bytes.length,uploaded_bytes:8},hash:hash(bytes),bytes:bytes.subarray(0,8)});
  await h.panel.setScope({kind:'lecture',id:A});await h.click('resume');
  await h.select(new File([bytes],'resume.pdf'));await h.click('upload');
  assert.equal(h.calls.find(call=>call.method==='POST').path,`/lectures/${A}/materials`);
  assert.equal(JSON.parse(h.calls.find(call=>call.method==='POST').body).id,B);
  assert.equal(h.calls.find(call=>call.method==='PUT').headers['X-Upload-Offset'],'8');
  assert.deepEqual(h.records.get(B).bytes,bytes);assert.equal(h.records.size,1);
});

test('same name and size with different contents cannot resume or overwrite original bytes',async t=>{
  const h=harness(t),bytes=Buffer.from('abcdef');
  h.records.set(B,{row:{...row(B),filename:'resume.pdf',size_bytes:6,uploaded_bytes:3},hash:hash(bytes),bytes:bytes.subarray(0,3)});
  await h.panel.setScope({kind:'lecture',id:A});await h.click('resume');
  await h.select(new File(['xxxxxx'],'resume.pdf'));await h.click('upload');
  assert.match(h.container.textContent,/처음 올린 파일과 내용이 다릅니다/);
  assert.equal(h.calls.filter(call=>call.method==='PUT').length,0);
  assert.deepEqual(h.records.get(B).bytes,Buffer.from('abc'));
});

test('uncertain reservation response retries the same UUID rather than allocating a duplicate',async t=>{
  const h=harness(t);await h.panel.setScope({kind:'lecture',id:A});await h.select(new File(['abc'],'test.pdf'));
  let failed=false;
  h.hook=async call=>{if(call.method==='POST'&&!failed){failed=true;throw Error('synthetic network loss');}};
  await h.click('upload');const first=JSON.parse(h.calls.find(call=>call.method==='POST').body).id;
  h.hook=null;await h.click('upload');
  assert.deepEqual(h.calls.filter(call=>call.method==='POST').map(call=>JSON.parse(call.body).id),[first,first]);
  assert.equal(h.records.size,1);
});

test('late list response from a previous lecture cannot render or replace the current list',async t=>{
  const h=harness(t);let release,oldSignal;
  h.hook=call=>call.path===`/lectures/${A}/materials`?new Promise(resolve=>{release=resolve;oldSignal=call.signal;}):undefined;
  const old=h.panel.setScope({kind:'lecture',id:A});await turn();
  await h.panel.setScope({kind:'lecture',id:B});assert.equal(oldSignal.aborted,true);
  release({materials:[{...row(),filename:'old-private.pdf'}]});await old;
  assert.doesNotMatch(h.container.textContent,/old-private/);
});

test('scope change during upload aborts its request and prevents subsequent old-lecture parts',async t=>{
  const h=harness(t);await h.panel.setScope({kind:'lecture',id:A});await h.select(new File([Buffer.alloc(480*1024+4)],'test.pdf'));
  let release,signal;h.hook=call=>call.method==='PUT'?new Promise(resolve=>{release=resolve;signal=call.signal;}):undefined;
  const pending=h.click('upload');while(!release) await turn();
  const id=[...h.records.keys()][0],old={...h.records.get(id).row,uploaded_bytes:480*1024};
  await h.panel.setScope({kind:'lecture',id:B});assert.equal(signal.aborted,true);
  release(old);await pending;
  assert.equal(h.calls.filter(call=>call.method==='PUT').length,1);
  assert.equal(h.calls.filter(call=>call.path===`/lectures/${B}/materials`&&call.method==='POST').length,0);
  assert.doesNotMatch(h.container.textContent,/test.pdf/);
});

test('old detached buttons and changed auth identity cannot issue cross-owner requests',async t=>{
  const h=harness(t);h.records.set(B,{row:{...row(B),uploaded_bytes:3},bytes:Buffer.from('abc'),hash:hash('abc')});
  await h.panel.setScope({kind:'lecture',id:A});const old=action(h.container,'convert');
  await h.panel.setScope({kind:'course',id:B});const before=h.calls.length;await old.click();
  assert.equal(h.calls.length,before);
  h.key='different-owner';await h.panel.refresh();assert.equal(h.calls.length,before);
});

test('ready preview is text-only and authenticated downloads revoke URLs at reset',async t=>{
  const h=harness(t);h.records.set(B,{row:{...row(B),uploaded_bytes:3},bytes:Buffer.from('abc'),hash:hash('abc')});h.ready(B);
  await h.panel.setScope({kind:'lecture',id:A});await h.click('preview');
  const pre=all(h.container,node=>node.tagName==='pre')[0];assert.match(pre.textContent,/<script>not executable<\/script>/);
  assert.equal(all(h.container,node=>node.tagName==='script').length,0);
  await h.click('original');await h.click('markdown');
  assert.deepEqual(h.document.downloads.map(item=>item.filename),['synthetic.pdf','synthetic.md']);
  assert.equal(await h.created[0].blob.text(),'abc');assert.match(await h.created[1].blob.text(),/final slide/);
  h.panel.reset();assert.equal(h.container.children.length,0);
  assert.deepEqual(new Set(h.revoked),new Set(h.created.map(item=>item.url)));
});

test('late original response after identity change never creates a downloadable URL',async t=>{
  const h=harness(t);h.records.set(B,{row:{...row(B),uploaded_bytes:3},bytes:Buffer.from('abc'),hash:hash('abc')});
  await h.panel.setScope({kind:'lecture',id:A});let release;
  h.hook=call=>call.path.endsWith('/original')?new Promise(resolve=>release=resolve):undefined;
  const pending=h.click('original');await turn();h.key='different-owner';release(new Blob(['abc']));await pending;
  assert.equal(h.created.length,0);assert.equal(h.document.downloads.length,0);
});

test('foreign manifest response cannot populate a course or issue follow-up requests',async t=>{
  const h=harness(t);h.hook=()=>({materials:[row()]});
  await h.panel.setScope({kind:'course',id:B});assert.match(h.container.textContent,/자료 목록을 확인하지 못했습니다/);
  assert.equal(action(h.container,'convert'),undefined);assert.equal(h.calls.length,1);
});

test('conversion polling stops after reset and never starts conversion automatically',async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  const h=harness(t);h.records.set(B,{row:{...row(B),uploaded_bytes:3,error_code:'converting'},bytes:Buffer.from('abc'),hash:hash('abc')});
  await h.panel.setScope({kind:'lecture',id:A});const initial=h.calls.length;
  t.mock.timers.tick(1500);await turn();assert.equal(h.calls.length,initial+1);
  h.panel.reset();const stopped=h.calls.length;t.mock.timers.tick(30000);await turn();
  assert.equal(h.calls.length,stopped);assert.equal(h.calls.filter(call=>call.method==='POST').length,0);
});

test('delete requires explicit confirmation and changes only the selected material',async t=>{
  const h=harness(t);h.records.set(B,{row:row(B),bytes:Buffer.alloc(0),hash:hash('abc')});
  await h.panel.setScope({kind:'lecture',id:A});h.changed.length=0;h.document.defaultView.confirm=()=>false;
  await h.click('delete');assert.equal(h.records.size,1);assert.equal(h.calls.filter(call=>call.method==='DELETE').length,0);
  h.document.defaultView.confirm=()=>true;await h.click('delete');assert.equal(h.records.size,0);assert.equal(h.changed.length,1);
});


test('polling a completed conversion notifies the owner once and stops polling',async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  const h=harness(t);h.records.set(B,{row:{...row(B),uploaded_bytes:3,error_code:'converting'},bytes:Buffer.from('abc'),hash:hash('abc')});
  await h.panel.setScope({kind:'lecture',id:A});h.changed.length=0;h.ready(B);
  t.mock.timers.tick(1500);await turn();assert.equal(h.changed.length,1);
  assert.ok(action(h.container,'preview'));assert.match(h.container.textContent,/변환 완료/);
  const stopped=h.calls.length;t.mock.timers.tick(30000);await turn();assert.equal(h.calls.length,stopped);
});
