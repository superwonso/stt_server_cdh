import assert from 'node:assert/strict';
import { test } from 'node:test';
import { AUTH_SESSION_STORAGE_KEY, MAX_AUTH_SESSION_AGE_MS, TabAuthSessionStore } from '../web/auth-session.js';

const ORIGIN = 'https://session-test.trycloudflare.com';
const NOW = 1700000000000;
function fixture() {
  let now = NOW;
  const entries = new Map();
  const storage = {getItem:key => entries.get(key) ?? null,setItem:(key,value) => entries.set(key,value),
    removeItem:key => entries.delete(key)};
  const store = new TabAuthSessionStore({getStorage:() => storage,now:() => now});
  const identity = {token:'opaque-test-session-token',username:'test-user',apiOrigin:ORIGIN,sessionExpiresAt:(NOW + 3600000) / 1000};
  return {store,storage,entries,identity,setNow:value => { now = value; }};
}

test('tab authentication stores only its bounded session record and survives a store instance replacement', () => {
  const {store,storage,entries,identity} = fixture();
  const saved = store.save({...identity,password:'must-not-save',setup_code:'must-not-save',is_admin:true});
  assert.equal(saved.expiresAt,NOW + 3600000);
  assert.deepEqual(Object.keys(saved).sort(),['apiOrigin','expiresAt','token','username','version']);
  assert.equal(entries.size,1);
  assert.doesNotMatch(entries.get(AUTH_SESSION_STORAGE_KEY),/password|setup_code|is_admin|must-not-save/);
  const reloaded = new TabAuthSessionStore({getStorage:() => storage,now:() => NOW + 1000});
  assert.deepEqual(reloaded.read(),saved);
  const differentTab = new TabAuthSessionStore({getStorage:() => ({getItem:() => null}),now:() => NOW + 1000});
  assert.equal(differentTab.read(),null);
});

test('the client never extends the original expiry or a shorter server session', () => {
  const {store,identity,setNow} = fixture();
  const saved = store.save({...identity,sessionExpiresAt:(NOW + 7 * 86400000) / 1000});
  assert.equal(saved.expiresAt,NOW + MAX_AUTH_SESSION_AGE_MS);
  setNow(NOW + 3600000);
  const refreshed = store.save({...identity,sessionExpiresAt:(NOW + 7 * 86400000) / 1000,notAfter:saved.expiresAt});
  assert.equal(refreshed.expiresAt,saved.expiresAt);
  const shortened = store.save({...identity,sessionExpiresAt:(NOW + 7200000) / 1000,notAfter:saved.expiresAt});
  assert.equal(shortened.expiresAt,NOW + 7200000);
});

test('expired records are removed and no expiry is invented for legacy or malformed responses', () => {
  const {store,entries,identity,setNow} = fixture();
  store.save(identity);
  setNow(NOW + 3600000);
  assert.equal(store.read(),null);
  assert.equal(entries.size,0);
  for (const expiry of [undefined,'1800000000',NaN,Infinity,NOW / 1000 - 1]) {
    assert.equal(store.save({...identity,sessionExpiresAt:expiry}),null);
    assert.equal(entries.size,0);
  }
});

test('malformed, extra-field, and excessively long stored records are discarded', () => {
  const {store,entries,identity} = fixture();
  const saved = store.save(identity);
  for (const raw of ['{invalid','null','[]','x'.repeat(4097),JSON.stringify({...saved,is_admin:true}),
    JSON.stringify({...saved,expiresAt:NOW + MAX_AUTH_SESSION_AGE_MS + 1}),JSON.stringify({...saved,token:'bad\r\nheader'})]) {
    entries.set(AUTH_SESSION_STORAGE_KEY,raw);
    assert.equal(store.read(),null);
    assert.equal(entries.has(AUTH_SESSION_STORAGE_KEY),false);
  }
});

test('credentials remain pinned to an exact origin and cannot contain a path or user info', () => {
  const {store,identity,entries} = fixture();
  for (const origin of ['https://user:password@session-test.trycloudflare.com',`${ORIGIN}/path`,`${ORIGIN}/`,
    `${ORIGIN}?token=x`,'http://session-test.trycloudflare.com','javascript:alert(1)']) {
    assert.equal(store.save({...identity,apiOrigin:origin}),null,origin);
  }
  store.save(identity);
  store.discardOtherOrigin(ORIGIN);
  assert.equal(entries.size,1);
  store.discardOtherOrigin('https://replacement-test.trycloudflare.com');
  assert.equal(entries.size,0);
  assert.ok(store.save({...identity,apiOrigin:'http://127.0.0.1:8765'}));
});

test('a late unauthorized response cannot clear a newer saved login', () => {
  const {store,identity} = fixture();
  store.save({...identity,token:'replacement-session'});
  store.clearMatching(identity.token,ORIGIN);
  assert.equal(store.read().token,'replacement-session');
  store.clearMatching('replacement-session','https://old-test.trycloudflare.com');
  assert.equal(store.read().token,'replacement-session');
  store.clearMatching('replacement-session',ORIGIN);
  assert.equal(store.read(),null);
});

test('unavailable sessionStorage does not break in-memory login or retain a replaceable stale account', () => {
  const {store,storage,identity,entries} = fixture();
  store.save(identity);
  storage.setItem = () => { throw new Error('quota exceeded'); };
  assert.ok(store.save({...identity,username:'next-user',token:'next-token'}));
  assert.equal(entries.size,0);
  const unavailable = new TabAuthSessionStore({getStorage:() => { throw new Error('storage disabled'); },now:() => NOW});
  assert.equal(unavailable.read(),null);
  assert.doesNotThrow(() => unavailable.clear());
  assert.ok(unavailable.save(identity));
});
