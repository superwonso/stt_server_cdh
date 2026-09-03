export const PCM_SAMPLE_RATE = 16000;
export const TARGET_CHUNK_SECONDS = 8;
export const CHUNK_SECONDS = 15;
export const OVERLAP_SECONDS = 3;
export const PAUSE_SECONDS = 0.24;
export const PAUSE_RMS = 0.006;
const TRACK_MUTE_GRACE_MS = 5000;

/** A continuous, anti-aliased resampler; state survives both input and WAV boundaries. */
export class StreamingResampler {
  constructor(inputRate, outputRate = PCM_SAMPLE_RATE) {
    if (!Number.isFinite(inputRate) || inputRate <= 0
        || !Number.isFinite(outputRate) || outputRate <= 0) {
      throw new Error('올바른 입력 오디오 샘플 속도를 확인할 수 없습니다.');
    }
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    this.ratio = inputRate / outputRate;
    this.totalInput = 0;
    this.nextOutput = 0;
    this.bufferStart = 0;
    this.buffer = new Float32Array(0);
    this.finished = false;
    this.radius = 32;
    this.phaseCount = 1024;
    this.firstSample = 0;
    this.lastSample = 0;
    if (inputRate === outputRate) return;

    // A Blackman-windowed sinc removes frequencies above the new Nyquist limit.
    const cutoff = Math.min(1, outputRate / inputRate) * 0.94;
    this.kernels = new Array(this.phaseCount + 1);
    for (let phase = 0; phase <= this.phaseCount; phase += 1) {
      const kernel = new Float64Array(this.radius * 2);
      let sum = 0;
      for (let tap = 0; tap < kernel.length; tap += 1) {
        const distance = tap - this.radius + 1 - phase / this.phaseCount;
        const x = Math.PI * cutoff * distance;
        const sinc = Math.abs(x) < 1e-12 ? 1 : Math.sin(x) / x;
        const window = 0.42 + 0.5 * Math.cos(Math.PI * distance / this.radius)
          + 0.08 * Math.cos(2 * Math.PI * distance / this.radius);
        kernel[tap] = cutoff * sinc * window;
        sum += kernel[tap];
      }
      for (let tap = 0; tap < kernel.length; tap += 1) kernel[tap] /= sum;
      this.kernels[phase] = kernel;
    }
  }

  push(samples) {
    if (this.finished) throw new Error('이미 종료된 오디오 변환입니다.');
    if (!samples.length) return new Float32Array(0);
    if (!this.totalInput) this.firstSample = samples[0];
    this.lastSample = samples[samples.length - 1];
    this.totalInput += samples.length;
    if (this.inputRate === this.outputRate) {
      this.nextOutput += samples.length;
      return samples.slice();
    }
    const joined = new Float32Array(this.buffer.length + samples.length);
    joined.set(this.buffer);
    joined.set(samples, this.buffer.length);
    this.buffer = joined;
    return this.read(false);
  }

  flush() {
    if (this.finished) return new Float32Array(0);
    this.finished = true;
    if (this.inputRate === this.outputRate) return new Float32Array(0);
    const tail = this.read(true);
    this.buffer = new Float32Array(0);
    return tail;
  }

  read(final) {
    if (!this.totalInput) return new Float32Array(0);
    const finalLength = Math.round(this.totalInput / this.ratio);
    const available = Math.max(0, finalLength - this.nextOutput);
    const output = new Float32Array(available);
    let written = 0;
    while (this.nextOutput < finalLength) {
      // Calculate from the integer output index to avoid long-session drift.
      const position = this.nextOutput * this.ratio;
      const center = Math.floor(position);
      if (!final && center + this.radius >= this.totalInput) break;
      const phase = Math.round((position - center) * this.phaseCount);
      const kernel = this.kernels[phase];
      const first = center - this.radius + 1;
      let value = 0;
      for (let tap = 0; tap < kernel.length; tap += 1) {
        const index = first + tap;
        const sample = index < 0 ? this.firstSample
          : index >= this.totalInput ? this.lastSample
            : this.buffer[index - this.bufferStart];
        value += sample * kernel[tap];
      }
      output[written++] = value;
      this.nextOutput += 1;
    }
    // Keep the filter history needed by the next input block.
    const keepFrom = Math.max(0, Math.floor(this.nextOutput * this.ratio) - this.radius + 1);
    const discard = Math.min(this.buffer.length, Math.max(0, keepFrom - this.bufferStart));
    if (discard) {
      this.buffer = this.buffer.slice(discard);
      this.bufferStart += discard;
    }
    return written === output.length ? output : output.slice(0, written);
  }
}

export function encodeWav(samples, sampleRate = PCM_SAMPLE_RATE) {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  const writeText = (offset, value) => {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  };
  writeText(0, 'RIFF');
  view.setUint32(4, bytes.byteLength - 8, true);
  writeText(8, 'WAVE');
  writeText(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Number.isFinite(samples[index]) ? Math.max(-1, Math.min(1, samples[index])) : 0;
    view.setInt16(44 + index * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  return new Blob([bytes], { type: 'audio/wav' });
}

function captureError(error, source = 'microphone') {
  if (source === 'system') {
    const messages = {
      NotAllowedError: '화면 또는 탭의 오디오 공유 권한이 필요합니다. 공유 창에서 오디오 공유를 켠 뒤 다시 시작해 주세요.',
      PermissionDeniedError: '화면 또는 탭의 오디오 공유 권한이 필요합니다. 공유 창에서 오디오 공유를 허용해 주세요.',
      NotFoundError: '공유할 화면이나 탭을 찾을 수 없습니다. 재생할 화면을 연 뒤 다시 시도해 주세요.',
      DevicesNotFoundError: '공유할 화면이나 탭을 찾을 수 없습니다. 재생할 화면을 연 뒤 다시 시도해 주세요.',
      NotReadableError: '선택한 화면의 오디오를 읽을 수 없습니다. 다른 화면이나 브라우저 탭을 선택해 주세요.',
      TrackStartError: '선택한 화면의 오디오를 시작할 수 없습니다. 다른 화면이나 브라우저 탭을 선택해 주세요.',
      SecurityError: '브라우저에서 화면 오디오 공유를 차단했습니다. HTTPS 주소와 사이트 권한을 확인해 주세요.',
      NotSupportedError: '이 브라우저에서는 화면 오디오 공유를 지원하지 않습니다. 데스크톱 Chrome 또는 Edge를 사용해 주세요.',
      InvalidStateError: '화면 오디오 공유 창을 열 수 없습니다. 이 페이지를 화면에 표시하고 버튼을 다시 눌러 주세요.',
      AbortError: '화면 오디오 공유가 취소되었습니다. 다시 시작해 주세요.',
    };
    return messages[error?.name] ? new Error(messages[error.name], { cause: error })
      : error instanceof Error && /[가-힣]/.test(error.message) ? error
        : new Error('화면 오디오를 처리하지 못했습니다. 데스크톱 Chrome 또는 Edge에서 다시 시도해 주세요.', { cause: error });
  }
  const messages = {
    NotAllowedError: '마이크 권한이 필요합니다. 브라우저의 사이트 설정에서 마이크를 허용한 뒤 다시 시작해 주세요.',
    PermissionDeniedError: '마이크 권한이 필요합니다. 브라우저의 사이트 설정에서 마이크를 허용해 주세요.',
    NotFoundError: '사용할 마이크를 찾을 수 없습니다. 마이크가 연결되어 있는지 확인해 주세요.',
    DevicesNotFoundError: '사용할 마이크를 찾을 수 없습니다. 마이크 연결을 확인해 주세요.',
    NotReadableError: '마이크를 열 수 없습니다. 다른 앱의 마이크 사용을 종료하고 다시 시도해 주세요.',
    TrackStartError: '마이크를 열 수 없습니다. 다른 앱의 마이크 사용을 종료해 주세요.',
    SecurityError: '브라우저에서 마이크 사용을 차단했습니다. HTTPS 주소와 사이트 권한을 확인해 주세요.',
    NotSupportedError: '이 브라우저에서는 마이크 오디오 처리를 지원하지 않습니다. 최신 Chrome, Edge 또는 Safari를 사용해 주세요.',
    InvalidStateError: '오디오가 중단되었거나 페이지가 비활성 상태입니다. 페이지를 화면에 표시하고 다시 시작해 주세요.',
    AbortError: '기기가 마이크 시작을 중단했습니다. 마이크 연결을 확인한 뒤 다시 시도해 주세요.',
  };
  return messages[error?.name] ? new Error(messages[error.name], { cause: error })
    : error instanceof Error && /[가-힣]/.test(error.message) ? error
      : new Error('마이크 오디오를 처리하지 못했습니다. 페이지를 새로 고침한 뒤 다시 시도해 주세요.', { cause: error });
}

/**
 * Call start() directly from a button gesture (before awaiting a server request).
 * onChunk({blob, startSeconds, durationSeconds, overlapSeconds, final}) runs
 * once per WAV. Except for the first WAV, each WAV starts with the final three
 * seconds of the previous WAV so a recognizer can resolve words at boundaries.
 * stop() delivers the final partial WAV before resolving; uploads are caller-owned.
 * onLevel receives RMS in [0, 1]. onInterrupted receives a Korean Error once;
 * the caller should call stop() and update its UI when this callback fires.
 * source='system' opens the browser's display picker and consumes only its
 * audio track. Browsers require a display/video choice, but video is never
 * connected to the worklet or represented in an emitted WAV.
 */
export class MicrophoneCapture {
  constructor({ onChunk, onLevel = () => {}, onInterrupted = () => {}, source = 'microphone' } = {}) {
    if (typeof onChunk !== 'function') throw new TypeError('onChunk 콜백이 필요합니다.');
    if (!['microphone', 'system'].includes(source)) {
      throw new TypeError('오디오 입력은 microphone 또는 system이어야 합니다.');
    }
    this.onChunk = onChunk;
    this.onLevel = onLevel;
    this.onInterrupted = onInterrupted;
    this.captureSource = source;
    this._state = 'idle';
    this._startPromise = null;
    this._stopPromise = null;
    this._context = null;
    this._stream = null;
    this._audioStream = null;
    this._node = null;
    this._source = null;
    this._silentGain = null;
    this._wakeLock = null;
    this._wakeRequest = null;
    this._listeners = [];
    this._flushReceipt = null;
    this._interruptionReported = false;
  }

  get recording() { return this._state === 'recording'; }

  start() {
    if (this._state !== 'idle' || this._startPromise || this._stopPromise) {
      const name = this.captureSource === 'system' ? '화면 오디오' : '마이크';
      return Promise.reject(new Error(`${name}가 이미 사용 중입니다. 먼저 현재 녹음을 종료해 주세요.`));
    }
    this._state = 'starting';
    this._interruptionReported = false;
    this._chunkStartSamples = 0;
    this._chunkOverlap = 0;
    this._hasEmitted = false;
    this._chunk = new Float32Array(PCM_SAMPLE_RATE * CHUNK_SECONDS);
    this._chunkUsed = 0;
    const cancelled = new Promise((_, reject) => {
      this._cancelStart = () => reject(new Error(
        this.captureSource === 'system' ? '화면 오디오 공유 시작이 취소되었습니다.' : '마이크 시작이 취소되었습니다.',
      ));
    });
    this._startPromise = this._begin(cancelled).finally(() => {
      this._startPromise = null;
      this._cancelStart = null;
    });
    return this._startPromise;
  }

  async _begin(cancelled) {
    try {
      const system = this.captureSource === 'system';
      const getMedia = system
        ? navigator.mediaDevices?.getDisplayMedia
        : navigator.mediaDevices?.getUserMedia;
      if (!globalThis.isSecureContext || typeof getMedia !== 'function') {
        throw new Error(system
          ? '화면 오디오 공유는 HTTPS 사이트의 데스크톱 Chrome 또는 Edge에서 사용할 수 있습니다.'
          : '마이크 녹음은 HTTPS 사이트 또는 이 컴퓨터의 localhost에서 사용할 수 있습니다.');
      }
      const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!Context || !globalThis.AudioWorkletNode) {
        throw new Error(system
          ? '이 브라우저는 실시간 화면 오디오 처리를 지원하지 않습니다. 데스크톱 Chrome 또는 Edge를 사용해 주세요.'
          : '이 브라우저는 실시간 마이크 녹음을 지원하지 않습니다. 최신 Chrome, Edge 또는 Safari를 사용해 주세요.');
      }
      // Use the device's real rate. Requesting 16 kHz is unreliable on tablets.
      const context = new Context({ latencyHint: 'interactive' });
      this._context = context;
      if (!context.audioWorklet) {
        throw new Error('이 브라우저에서는 오디오 녹음을 사용할 수 없습니다. 브라우저를 최신 버전으로 업데이트해 주세요.');
      }
      // Both calls happen before the first await, preserving the button gesture.
      const resumed = context.resume();
      // getDisplayMedia requires a video choice even though this application
      // only routes the returned audio tracks through Web Audio. No video data
      // is connected to the worklet, encoded, uploaded, or stored.
      const constraints = system ? {
        video: true,
        audio: true,
        systemAudio: 'include',
        surfaceSwitching: 'include',
      } : {
        video: false,
        audio: { channelCount: { ideal: 1 }, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      };
      const permission = getMedia.call(navigator.mediaDevices, constraints).then((stream) => {
        if (this._context !== context || this._state !== 'starting') {
          stream.getTracks().forEach((track) => track.stop());
        } else {
          this._stream = stream;
        }
        return stream;
      });
      const module = context.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url));
      await Promise.race([Promise.all([permission, resumed, module]), cancelled]);
      if (context.state !== 'running') {
        throw new Error(system
          ? '화면 오디오가 일시 중단되어 있습니다. 이 페이지를 화면에 표시한 상태에서 시작 버튼을 다시 눌러 주세요.'
          : '마이크가 일시 중단되어 있습니다. 이 페이지를 화면에 표시한 상태에서 시작 버튼을 다시 눌러 주세요.');
      }
      if (!this._stream?.getAudioTracks().some((track) => track.readyState === 'live')) {
        throw new Error(system
          ? '선택한 화면에 공유된 오디오가 없습니다. 공유 창에서 “탭 오디오 공유” 또는 “시스템 오디오 공유”를 켜 주세요.'
          : '마이크 연결이 종료되었습니다. 연결을 확인한 뒤 다시 시작해 주세요.');
      }
      this._resampler = new StreamingResampler(context.sampleRate);
      this._audioStream = system
        ? new MediaStream(this._stream.getAudioTracks())
        : this._stream;
      this._source = context.createMediaStreamSource(this._audioStream);
      this._node = new AudioWorkletNode(context, 'classroom-pcm', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
      });
      this._silentGain = context.createGain();
      this._silentGain.gain.value = 0;
      this._node.port.onmessage = ({ data }) => {
        if (data?.type === 'samples' && this._resampler) {
          try {
            this._acceptSamples(data.samples);
          } catch (error) {
            this._interrupt(error);
          }
        } else if (data?.type === 'stopped' && data.id === this._flushReceipt?.id) {
          this._flushReceipt.resolve();
        }
      };
      this._listen(this._node, 'processorerror', () => this._interrupt(
        new Error(system
          ? '화면 오디오 처리가 중단되었습니다. 받아쓰기를 종료한 뒤 다시 시작해 주세요.'
          : '마이크 오디오 처리가 중단되었습니다. 녹음을 종료한 뒤 다시 시작해 주세요.'),
      ));
      this._listen(context, 'statechange', () => {
        if (this.recording && context.state !== 'running') {
          this._interrupt(new Error(system
            ? '기기 또는 브라우저가 화면 오디오를 중단했습니다. 페이지를 다시 열고 받아쓰기를 시작해 주세요.'
            : '기기 또는 브라우저가 마이크 녹음을 중단했습니다. 페이지를 다시 열고 녹음을 시작해 주세요.'));
        }
      });
      for (const track of this._stream.getAudioTracks()) {
        this._listen(track, 'ended', () => this._interrupt(
          new Error(system
            ? '공유 오디오가 종료되었습니다. 재생할 화면을 다시 선택해 주세요.'
            : '마이크 연결이 끊어졌습니다. 마이크를 확인한 뒤 녹음을 다시 시작해 주세요.'),
        ));
        // `mute` can be a brief browser/OS interruption. Ending a long class on
        // the first event loses more audio than waiting a short, bounded grace.
        let muteTimer = null;
        const clearMuteTimer = () => {
          if (muteTimer !== null) clearTimeout(muteTimer);
          muteTimer = null;
        };
        this._listen(track, 'mute', () => {
          clearMuteTimer();
          muteTimer = setTimeout(() => {
            muteTimer = null;
            if (this.recording && track.readyState === 'live' && track.muted) {
              this._interrupt(new Error(system
                ? '공유 오디오가 5초 이상 중단되었습니다. 화면 공유를 다시 시작해 주세요.'
                : '마이크 입력이 5초 이상 중단되었습니다. 이 페이지에서 녹음을 다시 시작해 주세요.'));
            }
          }, TRACK_MUTE_GRACE_MS);
        });
        this._listen(track, 'unmute', clearMuteTimer);
        this._listeners.push(clearMuteTimer);
      }
      if (system) {
        // The display track is required by getDisplayMedia and is retained only
        // so the browser's “stop sharing” control can end this capture cleanly.
        // It never enters the audio graph or any network request.
        for (const track of this._stream.getVideoTracks()) {
          this._listen(track, 'ended', () => this._interrupt(
            new Error('화면 공유가 종료되었습니다. 받은 오디오를 정리하고 받아쓰기를 마칩니다.'),
          ));
        }
      }
      this._listen(document, 'visibilitychange', () => {
        if (document.visibilityState === 'visible') this._requestWakeLock();
      });
      this._node.connect(this._silentGain);
      this._silentGain.connect(context.destination);
      this._state = 'recording';
      this._source.connect(this._node);
      this._requestWakeLock();
    } catch (error) {
      await this._releaseResources();
      this._state = 'idle';
      throw captureError(error, this.captureSource);
    }
  }

  _listen(target, event, handler) {
    target.addEventListener(event, handler);
    this._listeners.push(() => target.removeEventListener(event, handler));
  }

  _interrupt(error) {
    if (!this.recording || this._interruptionReported) return;
    this._interruptionReported = true;
    this.onInterrupted(captureError(error, this.captureSource));
  }

  _acceptSamples(samples) {
    this._appendPCM(this._resampler.push(samples));
    if (this.recording) {
      let power = 0;
      for (const sample of samples) power += sample * sample;
      this.onLevel(Math.min(1, Math.sqrt(power / Math.max(1, samples.length))));
    }
  }

  _appendPCM(samples) {
    let offset = 0;
    while (offset < samples.length) {
      const count = Math.min(samples.length - offset, this._chunk.length - this._chunkUsed);
      this._chunk.set(samples.subarray(offset, offset + count), this._chunkUsed);
      this._chunkUsed += count;
      offset += count;
      if (this._chunkUsed === this._chunk.length || this._endsInPause()) this._emitChunk();
    }
  }

  _endsInPause() {
    const target = PCM_SAMPLE_RATE * TARGET_CHUNK_SECONDS;
    const pause = Math.round(PCM_SAMPLE_RATE * PAUSE_SECONDS);
    // The retained guard was already sent in the previous WAV. Wait for eight
    // seconds of genuinely new audio before looking for a quiet boundary.
    const fresh = this._chunkUsed - this._chunkOverlap;
    if (fresh < target || fresh < pause) return false;
    let power = 0;
    for (let index = this._chunkUsed - pause; index < this._chunkUsed; index += 1) {
      const sample = Number.isFinite(this._chunk[index]) ? this._chunk[index] : 0;
      power += sample * sample;
    }
    return Math.sqrt(power / pause) <= PAUSE_RMS;
  }

  _emitChunk(final = false) {
    if (!this._chunkUsed) return false;
    const fresh = this._chunkUsed - this._chunkOverlap;
    // An empty stop before the first audio sample must not create a WAV. Once a
    // regular WAV was sent, however, send its retained guard once more as the
    // final WAV even when no new PCM arrived. This lets the server finalize the
    // last boundary without fabricating timeline duration.
    if (final && fresh === 0 && !this._hasEmitted) return false;
    const count = this._chunkUsed;
    const item = {
      blob: encodeWav(this._chunk.subarray(0, count)),
      startSeconds: this._chunkStartSamples / PCM_SAMPLE_RATE,
      durationSeconds: count / PCM_SAMPLE_RATE,
      overlapSeconds: this._chunkOverlap / PCM_SAMPLE_RATE,
      final,
    };
    this._hasEmitted = true;
    if (final) {
      this._chunkUsed = 0;
      this._chunkOverlap = 0;
    } else {
      const retained = Math.min(Math.round(PCM_SAMPLE_RATE * OVERLAP_SECONDS), count);
      this._chunk.copyWithin(0, count - retained, count);
      this._chunkStartSamples += count - retained;
      this._chunkUsed = retained;
      this._chunkOverlap = retained;
    }
    // The caller owns upload queueing; capture must never wait for the network.
    this.onChunk(item);
    return true;
  }

  stop() {
    if (this._stopPromise) return this._stopPromise;
    if (this._state === 'idle') return Promise.resolve();
    this._stopPromise = this._finishStop().finally(() => { this._stopPromise = null; });
    return this._stopPromise;
  }

  async _finishStop() {
    if (this._state === 'starting') {
      this._cancelStart?.();
      await this._startPromise?.catch(() => {});
      return;
    }
    this._state = 'stopping';
    let flushError = null;
    try {
      if (this._node && this._context?.state !== 'closed') {
        try {
          await new Promise((resolve, reject) => {
            const id = Date.now();
            const timer = setTimeout(() => reject(new Error(
              this.captureSource === 'system'
                ? '화면 오디오가 응답하지 않아 마지막 조각의 완전한 수신을 확인하지 못했습니다. 받은 내용은 저장되며, 공유를 다시 시작할 수 있습니다.'
                : '마이크가 응답하지 않아 마지막 오디오 조각의 완전한 수신을 확인하지 못했습니다. 받은 내용은 저장되며, 녹음을 다시 시작할 수 있습니다.',
            )), 2500);
            this._flushReceipt = { id, resolve: () => { clearTimeout(timer); resolve(); } };
            this._node.port.postMessage({ type: 'stop', id });
            // An interrupted tablet may need this to deliver its flush receipt.
            if (this._context.state !== 'running') this._context.resume().catch(() => {});
          });
        } catch (error) {
          flushError = error;
        }
      } else if (this._node) {
        flushError = new Error('기기가 오디오 처리를 종료하여 마지막 오디오 조각을 확인하지 못했습니다. 받은 내용은 저장됩니다.');
      }
      if (this._resampler) this._appendPCM(this._resampler.flush());
      this._emitChunk(true);
    } finally {
      await this._releaseResources();
      this._resampler = null;
      this._state = 'idle';
      this.onLevel(0);
    }
    if (flushError) throw flushError;
  }

  _requestWakeLock() {
    if (!this.recording || this._wakeLock || this._wakeRequest
        || !navigator.wakeLock || document.visibilityState !== 'visible') return;
    const context = this._context;
    this._wakeRequest = navigator.wakeLock.request('screen').then((lock) => {
      if (!this.recording || context !== this._context) {
        return lock.release().catch(() => {});
      }
      this._wakeLock = lock;
      lock.addEventListener('release', () => {
        if (this._wakeLock === lock) this._wakeLock = null;
      }, { once: true });
    }).catch(() => {
      // Recording works without this optional permission (e.g. low battery).
    }).finally(() => { this._wakeRequest = null; });
  }

  async _releaseResources() {
    this._listeners.splice(0).forEach((remove) => remove());
    for (const node of [this._source, this._node, this._silentGain]) {
      try { node?.disconnect(); } catch { /* Already disconnected. */ }
    }
    this._stream?.getTracks().forEach((track) => track.stop());
    this._node?.port.close();
    const context = this._context;
    this._context = null;
    this._stream = null;
    this._audioStream = null;
    this._source = null;
    this._node = null;
    this._silentGain = null;
    this._flushReceipt = null;
    const lock = this._wakeLock;
    this._wakeLock = null;
    if (lock) await lock.release().catch(() => {});
    if (context && context.state !== 'closed') await context.close().catch(() => {});
  }
}
