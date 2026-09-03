import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';
import {
  StreamingResampler, encodeWav, MicrophoneCapture, OVERLAP_SECONDS,
} from '../web/audio.js';

function join(parts) {
  const result = new Float32Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) { result.set(part, offset); offset += part.length; }
  return result;
}

function convert(samples, rate, blockSize) {
  const resampler = new StreamingResampler(rate);
  const parts = [];
  for (let start = 0; start < samples.length; start += blockSize) {
    parts.push(resampler.push(samples.subarray(start, start + blockSize)));
  }
  parts.push(resampler.flush());
  assert.equal(resampler.flush().length, 0);
  return join(parts);
}

function rms(samples) {
  return Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length);
}

test('resampling preserves duration and is independent of incoming block boundaries', () => {
  for (const rate of [8000, 16000, 44100, 48000, 96000]) {
    const count = rate * 2 + 103;
    const samples = Float32Array.from({ length: count }, (_, index) =>
      Math.sin(2 * Math.PI * 831 * index / rate) * 0.7 + (index % 43) / 1000);
    const oneBlock = convert(samples, rate, samples.length);
    assert.equal(oneBlock.length, Math.round(count * 16000 / rate));
    for (const blockSize of [1, 127, 2048]) {
      assert.deepEqual(convert(samples, rate, blockSize), oneBlock, `${rate} Hz / ${blockSize} frames`);
    }
  }
});

test('downsampling retains speech-band sound and rejects frequencies that would alias', () => {
  for (const rate of [44100, 48000]) {
    const tone = (frequency) => Float32Array.from({ length: rate }, (_, index) =>
      Math.sin(2 * Math.PI * frequency * index / rate));
    const speech = convert(tone(1000), rate, 2048).slice(100, -100);
    const outOfBand = convert(tone(11000), rate, 2048).slice(100, -100);
    assert.ok(rms(speech) > 0.69);
    assert.ok(rms(outOfBand) < 0.01);
  }
});

test('WAV output is mono signed PCM16 with a 16 kHz header and clipping', async () => {
  const blob = encodeWav(new Float32Array([-2, -1, 0, 1, 2, NaN]));
  assert.equal(blob.type, 'audio/wav');
  const bytes = await blob.arrayBuffer();
  const view = new DataView(bytes);
  assert.equal(new TextDecoder().decode(bytes.slice(0, 4)), 'RIFF');
  assert.equal(new TextDecoder().decode(bytes.slice(8, 12)), 'WAVE');
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint32(28, true), 32000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), 12);
  assert.deepEqual(Array.from({ length: 6 }, (_, i) => view.getInt16(44 + 2 * i, true)),
    [-32768, -32768, 0, 32767, 32767, 0]);
});

function prepareCapture(inputRate, chunks) {
  const capture = new MicrophoneCapture({ onChunk: chunk => chunks.push(chunk) });
  capture._resampler = new StreamingResampler(inputRate);
  capture._chunk = new Float32Array(16000 * 15);
  capture._chunkUsed = 0;
  capture._chunkStartSamples = 0;
  capture._chunkOverlap = 0;
  capture._hasEmitted = false;
  capture._state = 'recording';
  return capture;
}

function pcmSamples(chunk) { return (chunk.blob.size - 44) / 2; }

test('capture retains a three-second guard while every source sample contributes once', async () => {
  const chunks = [];
  const capture = prepareCapture(44100, chunks);
  const samples = new Float32Array(44100 * 32 + 103).fill(0.25);
  for (let start = 0; start < samples.length; start += 2048) {
    capture._acceptSamples(samples.subarray(start, start + 2048));
  }
  await capture.stop();
  assert.equal(chunks.length, 3);
  assert.deepEqual(chunks.map(chunk => chunk.startSeconds), [0, 12, 24]);
  assert.deepEqual(chunks.map(chunk => chunk.overlapSeconds), [0, 3, 3]);
  assert.deepEqual(chunks.map(chunk => chunk.final), [false, false, true]);
  assert.deepEqual(chunks.slice(0, 2).map(chunk => chunk.durationSeconds), [15, 15]);
  const count = Math.round(samples.length * 16000 / 44100);
  assert.equal(chunks[2].durationSeconds, (count - 384000) / 16000);
  assert.equal(chunks.reduce((sum, chunk) =>
    sum + pcmSamples(chunk) - Math.round(chunk.overlapSeconds * 16000), 0), count);
  assert.equal(chunks.at(-1).startSeconds + chunks.at(-1).durationSeconds, count / 16000);
  assert.equal(capture.recording, false);
});

test('capture prefers a quiet boundary after eight fresh seconds with overlap and no loss', async () => {
  const chunks = [];
  const capture = prepareCapture(16000, chunks);
  const speech = new Float32Array(16000 * 9).fill(0.2);
  const pause = new Float32Array(16000 * 0.3);
  const tail = new Float32Array(16000).fill(0.2);
  const samples = join([speech, pause, tail]);
  for (let start = 0; start < samples.length; start += 320) {
    capture._acceptSamples(samples.subarray(start, start + 320));
  }
  await capture.stop();
  assert.equal(chunks.length, 2);
  assert.ok(chunks[0].durationSeconds >= 9.24 && chunks[0].durationSeconds <= 9.3);
  assert.equal(chunks[0].overlapSeconds, 0);
  assert.equal(chunks[0].final, false);
  assert.equal(chunks[1].overlapSeconds, OVERLAP_SECONDS);
  assert.equal(chunks[1].final, true);
  assert.equal(chunks[1].startSeconds, chunks[0].durationSeconds - OVERLAP_SECONDS);
  assert.equal(chunks.reduce((sum, chunk) =>
    sum + pcmSamples(chunk) - Math.round(chunk.overlapSeconds * 16000), 0), samples.length);
  assert.equal(chunks[1].startSeconds + chunks[1].durationSeconds, samples.length / 16000);
});

test('target duration ignores retained overlap and stop finalizes an overlap-only guard', async () => {
  const chunks = [];
  const capture = prepareCapture(16000, chunks);
  capture._acceptSamples(new Float32Array(16000 * 15).fill(0.2));
  assert.equal(chunks.length, 1);
  capture._acceptSamples(new Float32Array(16000 * 7));
  assert.equal(chunks.length, 1, 'three retained seconds do not count toward the eight-second target');
  capture._acceptSamples(new Float32Array(16000));
  assert.equal(chunks.length, 2);
  assert.equal(chunks[1].durationSeconds, 11);
  assert.equal(chunks[1].overlapSeconds, 3);
  await capture.stop();
  assert.equal(chunks.length, 3);
  assert.deepEqual(chunks.map(chunk => chunk.final), [false, false, true]);
  assert.equal(chunks[2].durationSeconds, 3);
  assert.equal(chunks[2].overlapSeconds, 3);
  assert.equal(chunks[2].startSeconds, 20);
  assert.equal(chunks.reduce((sum, chunk) =>
    sum + pcmSamples(chunk) - Math.round(chunk.overlapSeconds * 16000), 0), 16000 * 23);
});

test('stopping before any PCM does not emit an empty or synthetic final WAV', async () => {
  const chunks = [];
  const capture = prepareCapture(16000, chunks);
  await capture.stop();
  assert.deepEqual(chunks, []);
});

test('worklet mixes mono, flushes its partial block before acknowledgement, and then stops', async () => {
  const messages = [];
  let Processor;
  const context = vm.createContext({
    Float32Array,
    AudioWorkletProcessor: class {
      constructor() { this.port = { postMessage: (message) => messages.push(message) }; }
    },
    registerProcessor: (name, implementation) => {
      assert.equal(name, 'classroom-pcm');
      Processor = implementation;
    },
  });
  vm.runInContext(await readFile(new URL('../web/pcm-worklet.js', import.meta.url), 'utf8'), context);
  const processor = new Processor();
  for (const length of [128, 256, 1920, 17]) {
    assert.equal(processor.process([[new Float32Array(length).fill(0.8), new Float32Array(length).fill(-0.2)]]), true);
  }
  processor.port.onmessage({ data: { type: 'stop', id: 42 } });
  assert.equal(messages.at(-1).type, 'stopped');
  assert.equal(messages.at(-1).id, 42);
  const samples = join(messages.filter((message) => message.type === 'samples').map((message) => message.samples));
  assert.equal(samples.length, 2321);
  assert.ok(samples.every((sample) => Math.abs(sample - 0.3) < 1e-6));
  assert.equal(processor.process([[new Float32Array(128)]]), false);
});

class FakeEventTarget {
  constructor(kind = '') {
    this.kind = kind;
    this.readyState = 'live';
    this.stopped = false;
    this.listeners = new Map();
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  removeEventListener(name, listener) {
    this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== listener));
  }

  dispatch(name) {
    for (const listener of [...(this.listeners.get(name) || [])]) listener({ type: name, target: this });
  }

  stop() { this.stopped = true; this.readyState = 'ended'; }
}

function installCaptureBrowser({ withAudio = true } = {}) {
  const originals = new Map();
  const replace = (name, value) => {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  };
  const audioTrack = new FakeEventTarget('audio');
  const videoTrack = new FakeEventTarget('video');
  const displayStream = {
    getAudioTracks: () => withAudio ? [audioTrack] : [],
    getVideoTracks: () => [videoTrack],
    getTracks: () => withAudio ? [audioTrack, videoTrack] : [videoTrack],
  };
  const requests = [];
  let audioGraphStream = null;
  let workletNode = null;

  class FakeMediaStream {
    constructor(tracks = []) { this.tracks = [...tracks]; }
    getAudioTracks() { return this.tracks.filter(track => track.kind === 'audio'); }
    getVideoTracks() { return this.tracks.filter(track => track.kind === 'video'); }
    getTracks() { return [...this.tracks]; }
  }

  class FakeAudioWorkletNode extends FakeEventTarget {
    constructor() {
      super('worklet');
      workletNode = this;
      this.port = {
        onmessage: null,
        postMessage: ({ type, id }) => {
          if (type === 'stop') this.port.onmessage?.({ data: { type: 'stopped', id } });
        },
        close() {},
      };
    }
    connect() {}
    disconnect() {}
  }

  class FakeAudioContext extends FakeEventTarget {
    constructor() {
      super('context');
      this.state = 'running';
      this.sampleRate = 48000;
      this.audioWorklet = { addModule: async () => {} };
      this.destination = {};
    }
    async resume() {}
    createMediaStreamSource(stream) {
      audioGraphStream = stream;
      return { connect() {}, disconnect() {} };
    }
    createGain() {
      return { gain: { value: 1 }, connect() {}, disconnect() {} };
    }
    async close() { this.state = 'closed'; }
  }

  const documentTarget = new FakeEventTarget('document');
  documentTarget.visibilityState = 'visible';
  replace('isSecureContext', true);
  replace('MediaStream', FakeMediaStream);
  replace('AudioContext', FakeAudioContext);
  replace('webkitAudioContext', undefined);
  replace('AudioWorkletNode', FakeAudioWorkletNode);
  replace('document', documentTarget);
  replace('navigator', {
    mediaDevices: {
      getUserMedia: async () => { throw new Error('getUserMedia must not be used'); },
      getDisplayMedia: async function getDisplayMedia(constraints) {
        requests.push({ constraints, receiver: this });
        return displayStream;
      },
    },
  });

  return {
    audioTrack,
    videoTrack,
    displayStream,
    requests,
    get audioGraphStream() { return audioGraphStream; },
    get workletNode() { return workletNode; },
    restore() {
      for (const [name, descriptor] of originals) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor);
        else delete globalThis[name];
      }
    },
  };
}

test('system capture requests display audio, excludes video from processing, and flushes after sharing ends', async () => {
  const browser = installCaptureBrowser();
  const chunks = [], interruptions = [];
  try {
    const capture = new MicrophoneCapture({
      source: 'system',
      onChunk: chunk => chunks.push(chunk),
      onInterrupted: error => interruptions.push(error),
    });
    const starting = capture.start();
    assert.equal(browser.requests.length, 1, 'display picker is requested synchronously from the button gesture');
    await starting;
    assert.deepEqual(browser.requests[0].constraints, {
      video: true,
      audio: true,
      systemAudio: 'include',
      surfaceSwitching: 'include',
    });
    assert.equal(browser.requests[0].receiver, globalThis.navigator.mediaDevices);
    assert.deepEqual(browser.audioGraphStream.getAudioTracks(), [browser.audioTrack]);
    assert.deepEqual(browser.audioGraphStream.getVideoTracks(), []);

    browser.workletNode.port.onmessage({
      data: { type: 'samples', samples: new Float32Array(4800).fill(0.25) },
    });
    browser.videoTrack.dispatch('ended');
    browser.audioTrack.dispatch('ended');
    assert.equal(interruptions.length, 1, 'one browser stop produces one interruption callback');
    assert.match(interruptions[0].message, /화면 공유가 종료/);

    await capture.stop();
    assert.equal(chunks.length, 1);
    assert.equal(chunks[0].final, true);
    assert.equal(chunks[0].durationSeconds, 0.1);
    assert.equal((chunks[0].blob.size - 44) / 2, 1600);
    assert.equal(browser.audioTrack.stopped, true);
    assert.equal(browser.videoTrack.stopped, true);
  } finally {
    browser.restore();
  }
});

test('system capture rejects a display choice without shared audio and releases its video track', async () => {
  const browser = installCaptureBrowser({ withAudio: false });
  try {
    const capture = new MicrophoneCapture({ source: 'system', onChunk() {} });
    await assert.rejects(capture.start(), /“탭 오디오 공유”.*“시스템 오디오 공유”/);
    assert.equal(capture.recording, false);
    assert.equal(browser.videoTrack.stopped, true);
  } finally {
    browser.restore();
  }
});

test('a transient system-track mute that recovers does not end a long capture', async () => {
  const browser = installCaptureBrowser();
  const interruptions = [];
  try {
    const capture = new MicrophoneCapture({
      source: 'system',
      onChunk() {},
      onInterrupted: error => interruptions.push(error),
    });
    await capture.start();
    browser.audioTrack.muted = true;
    browser.audioTrack.dispatch('mute');
    browser.audioTrack.muted = false;
    browser.audioTrack.dispatch('unmute');
    assert.equal(interruptions.length, 0);
    await capture.stop();
    assert.equal(interruptions.length, 0);
  } finally {
    browser.restore();
  }
});

test('capture source is explicit and rejects unknown input modes', () => {
  assert.throws(() => new MicrophoneCapture({ source: 'speaker', onChunk() {} }), /microphone 또는 system/);
  assert.equal(new MicrophoneCapture({ source: 'system', onChunk() {} }).captureSource, 'system');
});
