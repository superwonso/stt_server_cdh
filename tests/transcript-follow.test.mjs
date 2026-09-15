import assert from 'node:assert/strict';
import {test} from 'node:test';
import {TranscriptFollow} from '../web/transcript-follow.js';

class Events {
  listeners = new Map();
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  emit(type, event = {}) {
    for (const callback of this.listeners.get(type) || []) callback({target:this, ...event});
  }
  get listenerCount() { return [...this.listeners.values()].reduce((sum, values) => sum + values.size, 0); }
}

function fixture({top = 800, height = 200, content = 1000} = {}) {
  const document = new Events(), viewport = new Events(), changes = [], writes = [];
  const text = {}, outside = {};
  let position = top, selection = null;
  Object.assign(viewport, {ownerDocument:document, clientHeight:height, scrollHeight:content,
    hidden:false, isConnected:true, contains:node => node === text || node === viewport});
  Object.defineProperty(viewport, 'scrollTop', {
    get:() => position,
    set:value => { writes.push(value); position = value; },
  });
  // These APIs must never be needed to follow the inner viewport.
  viewport.scrollIntoView = () => { throw new Error('page scroll is forbidden'); };
  viewport.scrollTo = () => { throw new Error('use viewport scrollTop only'); };
  const follower = new TranscriptFollow(viewport, {
    onChange:value => changes.push(value), getSelection:() => selection,
  });
  return {follower, viewport, document, changes, writes, text, outside,
    scroll(top) { position = top; viewport.emit('scroll'); },
    select(value) { selection = value; document.emit('selectionchange'); },
    append(size = 100) {
      follower.beforeUpdate(); viewport.scrollHeight += size; follower.afterUpdate({changed:true});
    }};
}

test('appended text follows the inner bottom while unchanged renders never scroll', () => {
  const {follower, viewport, writes, append} = fixture();
  append();
  assert.equal(viewport.scrollTop, 900);
  assert.equal(follower.following, true);
  follower.beforeUpdate(); follower.afterUpdate({changed:false});
  assert.deepEqual(writes, [900]);
});

test('content growth and intermediate render scroll events are not manual upward reading', () => {
  const {follower, viewport} = fixture();
  follower.beforeUpdate(); viewport.scrollHeight += 300;
  viewport.emit('scroll');
  assert.equal(follower.following, true);
  follower.afterUpdate({changed:true});
  assert.equal(viewport.scrollTop, 1100);
  assert.equal(follower.following, true);
});

test('scrolling upward pauses, appended text preserves reading position, latest resumes', () => {
  const {follower, viewport, changes, scroll, append} = fixture();
  scroll(450);
  assert.equal(follower.following, false);
  append(300);
  assert.equal(viewport.scrollTop, 450);
  scroll(1100);
  assert.equal(follower.following, false, 'reaching the bottom does not silently resume');
  follower.resume(); append();
  assert.equal(viewport.scrollTop, 1200);
  assert.deepEqual(changes, [{following:false}, {following:true}]);
});

test('beforeUpdate detects an upward scroll whose scroll event has not arrived', () => {
  const {follower, viewport, append} = fixture();
  viewport.scrollTop = 400;
  append(200);
  assert.equal(follower.following, false);
  assert.equal(viewport.scrollTop, 400);
});

test('upward wheel, keyboard and touch intent pauses before scrolling starts', () => {
  for (const event of [
    viewport => viewport.emit('wheel', {deltaY:-1}),
    viewport => viewport.emit('keydown', {key:'ArrowUp'}),
    viewport => viewport.emit('keydown', {key:'PageUp'}),
    viewport => viewport.emit('keydown', {key:'Home'}),
    viewport => viewport.emit('keydown', {key:' ', shiftKey:true}),
    viewport => {
      viewport.emit('touchstart', {touches:[{clientY:100}]});
      viewport.emit('touchmove', {touches:[{clientY:140}]});
    },
  ]) {
    const {follower, viewport, append} = fixture();
    event(viewport); append();
    assert.equal(follower.following, false);
    assert.equal(viewport.scrollTop, 800);
  }
});

test('typing, zooming, cancelled events and downward input do not pause following', () => {
  const {follower, viewport} = fixture();
  viewport.emit('keydown', {key:'ArrowUp', target:{closest:() => ({})}});
  viewport.emit('wheel', {deltaY:-1, ctrlKey:true});
  viewport.emit('wheel', {deltaY:-1, defaultPrevented:true});
  viewport.emit('wheel', {deltaY:1});
  viewport.emit('keydown', {key:'ArrowDown'});
  viewport.emit('touchstart', {touches:[{clientY:100}]});
  viewport.emit('touchmove', {touches:[{clientY:80}]});
  viewport.emit('touchend');
  viewport.emit('touchmove', {touches:[{clientY:140}]});
  assert.equal(follower.following, true);
});

test('only a noncollapsed selection touching transcript pauses following', () => {
  const {follower, viewport, select, text, outside, append} = fixture();
  select({isCollapsed:false, anchorNode:outside, focusNode:outside});
  assert.equal(follower.following, true);
  select({isCollapsed:true, anchorNode:text, focusNode:text});
  assert.equal(follower.following, true);
  select({isCollapsed:false, anchorNode:text, focusNode:text});
  assert.equal(follower.following, false);
  append(200);
  assert.equal(viewport.scrollTop, 800);
  select(null);
  assert.equal(follower.following, false, 'finishing a selection does not resume automatically');
  follower.resume();
  assert.equal(viewport.scrollTop, 1000);
});

test('a selection spanning the viewport also pauses even with both endpoints outside', () => {
  const {follower, viewport, outside, select} = fixture();
  select({isCollapsed:false, anchorNode:outside, focusNode:outside, rangeCount:1,
    getRangeAt:() => ({intersectsNode:node => node === viewport})});
  assert.equal(follower.following, false);
});

test('render-time selection detection protects selection without a selectionchange event', () => {
  const {follower, viewport, text, append} = fixture();
  follower.getSelection = () => ({isCollapsed:false, anchorNode:text, focusNode:text});
  append();
  assert.equal(follower.following, false);
  assert.equal(viewport.scrollTop, 800);
});

test('delayed programmatic scroll events do not disable a recently resumed follower', () => {
  const {follower, viewport, scroll, append} = fixture();
  append(); // Its scroll event has not arrived yet.
  scroll(400);
  follower.resume();
  viewport.emit('scroll'); viewport.emit('scroll');
  assert.equal(follower.following, true);
  append();
  assert.equal(viewport.scrollTop, 1000);
  scroll(650);
  assert.equal(follower.following, false, 'a real new upward scroll must not be ignored');
});

test('reset scopes following without moving the old view or retaining stale update state', () => {
  const {follower, viewport, writes, append} = fixture();
  follower.beforeUpdate(); follower.reset({follow:false});
  append();
  assert.equal(viewport.scrollTop, 800);
  assert.deepEqual(writes, []);
  follower.reset({follow:true});
  assert.deepEqual(writes, []);
  append();
  assert.equal(viewport.scrollTop, 1000);
  assert.equal(follower.following, true);
});

test('reset allows the host to start a new scope at zero before its first render', () => {
  const {follower, viewport} = fixture();
  follower.reset({follow:true});
  viewport.scrollTop = 0;
  viewport.emit('scroll');
  follower.beforeUpdate(); viewport.scrollHeight = 1400; follower.afterUpdate({changed:true});
  assert.equal(follower.following, true);
  assert.equal(viewport.scrollTop, 1200);
  follower.reset({follow:false});
  viewport.scrollTop = 0;
  follower.beforeUpdate(); viewport.scrollHeight = 600; follower.afterUpdate({changed:true});
  assert.equal(follower.following, false);
  assert.equal(viewport.scrollTop, 0);
});

test('hidden, detached and zero-height viewports are never scrolled', () => {
  for (const props of [{clientHeight:0}, {hidden:true}, {isConnected:false}]) {
    const {follower, viewport, writes, append} = fixture();
    Object.assign(viewport, props);
    follower.resume(); append();
    assert.deepEqual(writes, []);
    Object.assign(viewport, {clientHeight:200, hidden:false, isConnected:true});
    append();
    assert.equal(viewport.scrollTop, 1000);
  }
});

test('destroy removes listeners and later methods and events cannot move the viewport', () => {
  const {follower, viewport, document, changes, writes, text, select} = fixture();
  assert.ok(viewport.listenerCount > 0); assert.equal(document.listenerCount, 1);
  follower.destroy(); follower.destroy();
  assert.equal(viewport.listenerCount, 0); assert.equal(document.listenerCount, 0);
  viewport.emit('wheel', {deltaY:-1});
  select({isCollapsed:false, anchorNode:text, focusNode:text});
  follower.pause(); follower.reset({follow:false}); follower.resume();
  follower.beforeUpdate(); viewport.scrollHeight += 300; follower.afterUpdate({changed:true});
  assert.deepEqual(changes, []); assert.deepEqual(writes, []);
});
