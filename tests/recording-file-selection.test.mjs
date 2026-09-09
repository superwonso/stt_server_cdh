import assert from 'node:assert/strict';
import {test} from 'node:test';
import {validateRecordingSelection,isFileDrag,recordingFileFromDrop} from '../web/recording-file-selection.js';

function file(name='lesson.wav',size=100,type='audio/wav') {
  const value=new Blob([new Uint8Array(Math.min(size,100))],{type});
  Object.defineProperties(value,{name:{value:name},size:{value:size}}); return value;
}
test('picker and drop use the same allowed formats and one GiB boundary without reading bytes',async()=>{
  for(const value of [file(),file('record.M4A',100,''),file('clip.bin',100,'audio/ogg'),file('clip',100,'video/mp4'),file('largest.wav',1024**3)]) {
    value.arrayBuffer=()=>{throw new Error('must not inspect file contents before explicit start');};
    assert.equal(validateRecordingSelection(value),value);
    assert.equal(await recordingFileFromDrop({types:['Files'],files:[value]}),value);
  }
  assert.throws(()=>validateRecordingSelection(file('large.wav',1024**3+1)),/1 GiB/);
  assert.throws(()=>validateRecordingSelection(file('empty.wav',0)),/내용이 있는/);
  assert.throws(()=>validateRecordingSelection(file('notes.txt',100,'text/plain')),/지원하는/);
});
test('directories multiple files and nonfile drops are rejected without selecting a partial list',async()=>{
  const value=file();
  for(const transfer of [
    {types:['Files'],files:[value,value]},
    {types:['Files'],files:[value],items:[{kind:'file'},{kind:'string'}]},
    {types:['text/plain'],files:[],items:[{kind:'string'}]},
    {types:['Files'],files:[value],items:[{kind:'file',webkitGetAsEntry:()=>({isDirectory:true})}]},
    {types:['Files'],files:[value],items:[{kind:'file',getAsFileSystemHandle:async()=>({kind:'directory'})}]},
  ]) await assert.rejects(recordingFileFromDrop(transfer),/한 개|텍스트|폴더/);
  const folderFile=file();Object.defineProperty(folderFile,'webkitRelativePath',{value:'folder/lesson.wav'});
  assert.throws(()=>validateRecordingSelection(folderFile),/폴더/);
});
test('drop pins the File before asynchronous directory checks and handles unavailable file lists',async()=>{
  const value=file();let release;
  const transfer={types:['Files'],files:[],items:[{kind:'file',getAsFile:()=>value,
    getAsFileSystemHandle:()=>new Promise(resolve=>{release=resolve;})}]};
  const result=recordingFileFromDrop(transfer);
  transfer.items=[];transfer.files=[];release({kind:'file'});
  assert.equal(await result,value);
});
test('directory-handle verification is bounded and file-only drag detection preserves ordinary text',async t=>{
  assert.equal(isFileDrag({types:['text/plain'],items:[{kind:'string'}]}),false);
  assert.equal(isFileDrag({types:['Files'],items:[],files:[]}),true);
  t.mock.timers.enable({apis:['setTimeout']});
  const pending=recordingFileFromDrop({types:['Files'],files:[file()],items:[{kind:'file',getAsFileSystemHandle:()=>new Promise(()=>{})}]});
  const rejected=assert.rejects(pending,/확인 시간이 초과/);t.mock.timers.tick(2000);await rejected;t.mock.timers.reset();
});
