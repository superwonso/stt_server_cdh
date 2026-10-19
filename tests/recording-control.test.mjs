import assert from 'node:assert/strict';
import {test} from 'node:test';
import {RecordingDeadline,validRecordingControl} from '../web/recording-control.js';
test('no-text deadline expires at exactly 180 seconds and confirmed progress starts a new budget',()=>{
 const timer=new RecordingDeadline(1000);assert.equal(timer.expired(180999),false);assert.equal(timer.expired(181000),true);
 timer.progress(170000);assert.equal(timer.expired(349999),false);assert.equal(timer.expired(350000),true);
});
test('manual pause freezes only the remaining budget and stop is terminal',()=>{
 const timer=new RecordingDeadline(1000);timer.pause(101000);assert.equal(timer.expired(999999),false);
 timer.pause(999999);timer.resume(1000000);assert.equal(timer.expired(1079999),false);assert.equal(timer.expired(1080000),true);
 timer.stop();timer.resume(1080001);timer.progress(2000000);assert.equal(timer.expired(3000000),false);
});
test('control responses must match capture identity and monotonic revision shape',()=>{
 assert.equal(validRecordingControl({state:'active',capture_id:'same',revision:1},'same'),true);
 for(const row of [{state:'stopped',capture_id:'other',revision:2},{state:'active',capture_id:'same',revision:-1},{state:'oops',capture_id:'same',revision:1}])assert.equal(validRecordingControl(row,'same'),false);
});

test('submitted ASR keeps the original no-text deadline through pause, retries and resume',()=>{
 const timer=new RecordingDeadline(1000);timer.requestStarted('same-chunk',51000);timer.pause(101000);
 assert.equal(timer.expired(180999),false);assert.equal(timer.expired(181000),true);
 timer.resume(170000);timer.requestStarted('same-chunk',170000);timer.progress(175000);timer.pause(176000);
 assert.equal(timer.expired(180999),false);assert.equal(timer.expired(181000),true,'retry or new text cannot prolong an unresolved request');
 timer.requestConfirmed('same-chunk');assert.equal(timer.expired(999999),false,'confirmed work leaves a genuinely idle pause exempt');
});
test('a request introduced during an old pause cannot acquire a fresh timeout budget',()=>{
 const timer=new RecordingDeadline(1000);timer.pause(101000);timer.requestStarted('late',999999);
 assert.equal(timer.requests.get('late'),181000);assert.equal(timer.expired(999999),true);
});
