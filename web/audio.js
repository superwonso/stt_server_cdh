export const PCM_SAMPLE_RATE = 16000;
export const TARGET_CHUNK_SECONDS = 8;
export const CHUNK_SECONDS = 15;
export const OVERLAP_SECONDS = 3;
export const PAUSE_SECONDS = 0.24;
export const PAUSE_RMS = 0.006;
const AUTO_RESUME_RETRY_MS = 2000;
const RECONNECT_DRAIN_TIMEOUT_MS = 350;
const MIN_UPLOAD_SAMPLES = Math.ceil(PCM_SAMPLE_RATE * 0.05);

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
 * Call start(), resumeInput(), and reconnect() directly from a user gesture. onChunk receives
 * overlapping WAVs; only stop() can emit one with final=true. A hidden page
 * calls checkpoint(), which emits at most one non-final WAV without resetting
 * the resampler or timeline.
 *
 * Browser/OS suspension and track mute are recoverable. They call
 * onInputUnavailable(error, details), retry AudioContext.resume() while the
 * page is visible, and call onInputRecovered(details) after input returns.
 * A permanently ended track, closed context, or failed worklet calls
 * onReconnectNeeded(error, details) after its old graph has been drained. The
 * caller must expose a button which calls reconnect(); this intentionally does
 * not finalize the current lecture. onInterrupted is retained only as a
 * deprecated constructor option and is never used to auto-stop a lecture.
 */
export class MicrophoneCapture {
  constructor({
    onChunk,
    onLevel = () => {},
    onInterrupted = () => {},
    onInputUnavailable = () => {},
    onInputRecovered = () => {},
    onReconnectNeeded = () => {},
    source = 'microphone',
  } = {}) {
    if (typeof onChunk !== 'function') throw new TypeError('onChunk 콜백이 필요합니다.');
    if (!['microphone', 'system'].includes(source)) {
      throw new TypeError('오디오 입력은 microphone 또는 system이어야 합니다.');
    }
    this.onChunk = onChunk;
    this.onLevel = onLevel;
    // Kept so older callers do not fail construction. Interruptions no longer
    // invoke it because those callers historically finalized the lecture.
    this.onInterrupted = onInterrupted;
    this.onInputUnavailable = onInputUnavailable;
    this.onInputRecovered = onInputRecovered;
    this.onReconnectNeeded = onReconnectNeeded;
    this.captureSource = source;
    this._state = 'idle';
    this._startPromise = null;
    this._reconnectPromise = null;
    this._reconnectPreparation = null;
    this._pausePromise = null;
    this._resumePromise = null;
    this._stopPromise = null;
    this._stopRequested = false;
    this._desiredPaused = false;
    this._pauseBoundaryPending = false;
    this._context = null;
    this._stream = null;
    this._audioStream = null;
    this._node = null;
    this._source = null;
    this._sourceConnected = false;
    this._silentGain = null;
    this._resampler = null;
    this._wakeLock = null;
    this._wakeRequest = null;
    this._resourceListeners = [];
    this._sessionListeners = [];
    this._resourceGeneration = 0;
    this._controlReceipt = null;
    this._controlSequence = 0;
    this._resumeAttempt = null;
    this._resumeRetryTimer = null;
    this._unavailableReasons = new Map();
    this._availabilityEpisode = false;
    this._reconnectReported = false;
    this._reconnectError = null;
    this._reconnectReason = null;
  }

  get recording() { return this._state === 'recording'; }
  get paused() { return this._state === 'paused'; }
  get capturedSeconds() { return ((this._chunkStartSamples || 0) + (this._chunkUsed || 0)) / PCM_SAMPLE_RATE; }
  get reconnectNeeded() { return this._state === 'reconnect-needed'; }

  start() {
    if (this._state !== 'idle' || this._startPromise || this._reconnectPromise
        || this._pausePromise || this._resumePromise || this._stopPromise) {
      const name = this.captureSource === 'system' ? '화면 오디오' : '마이크';
      return Promise.reject(new Error(`${name}가 이미 사용 중입니다. 먼저 현재 녹음을 종료해 주세요.`));
    }
    this._state = 'starting';
    this._stopRequested = false;
    this._desiredPaused = false;
    this._pauseBoundaryPending = false;
    this._resetAvailability();
    this._chunkStartSamples = 0;
    this._chunkOverlap = 0;
    this._hasEmitted = false;
    this._chunk = new Float32Array(PCM_SAMPLE_RATE * CHUNK_SECONDS);
    this._chunkUsed = 0;
    this._installSessionListeners();
    const cancelled = new Promise((_, reject) => {
      this._cancelStart = () => reject(new Error(
        this.captureSource === 'system' ? '화면 오디오 공유 시작이 취소되었습니다.' : '마이크 시작이 취소되었습니다.',
      ));
    });
    this._startPromise = this._openCapture(cancelled, 'starting').catch(async (error) => {
      await this._releaseResources();
      this._releaseSessionListeners();
      this._resampler = null;
      this._state = 'idle';
      throw captureError(error, this.captureSource);
    }).finally(() => {
      this._startPromise = null;
      this._cancelStart = null;
    });
    return this._startPromise;
  }

  reconnect() {
    if (this._reconnectPromise) return this._reconnectPromise;
    if (this._state !== 'reconnect-needed') {
      return Promise.reject(new Error('오디오 입력을 다시 연결해야 할 때만 재연결할 수 있습니다.'));
    }
    if (this._reconnectPreparation) {
      return Promise.reject(new Error('이전 오디오 입력을 정리하고 있습니다. 재연결 버튼이 활성화되면 다시 눌러 주세요.'));
    }

    // Do not await before _openCapture(): getDisplayMedia in particular must be
    // invoked in the same user-activation turn as the reconnect button.
    this._state = 'reconnecting';
    this._reconnectReported = false;
    const cancelled = new Promise((_, reject) => {
      this._cancelReconnect = () => reject(new Error(
        this.captureSource === 'system' ? '화면 오디오 재연결이 취소되었습니다.' : '마이크 재연결이 취소되었습니다.',
      ));
    });
    this._reconnectPromise = this._openCapture(cancelled, 'reconnecting').catch(async (error) => {
      await this._releaseResources();
      if (!this._stopRequested && this._state !== 'idle') {
        this._state = 'reconnect-needed';
        this._reconnectError = captureError(error, this.captureSource);
        this._reconnectReason = 'reconnect-failed';
        this._markInputUnavailable('reconnect', this._reconnectError, 'reconnect-failed', true);
      }
      throw captureError(error, this.captureSource);
    }).finally(() => {
      this._reconnectPromise = null;
      this._cancelReconnect = null;
      if (this._state === 'reconnect-needed' && !this._stopRequested) {
        this._reportReconnectNeeded();
      }
    });
    return this._reconnectPromise;
  }

  resumeInput() {
    // Use the same button for a temporary AudioContext suspension and a fully
    // ended device. Calling reconnect() here preserves getDisplayMedia's user
    // activation because there is no await before it opens the picker.
    if (this._state === 'reconnect-needed') return this.reconnect();
    if (this._state === 'reconnecting') {
      return this._reconnectPromise
        || Promise.reject(new Error('오디오 입력을 다시 연결하고 있습니다. 잠시 기다려 주세요.'));
    }
    if (!['recording', 'paused', 'pausing', 'resuming'].includes(this._state)
        || this._stopRequested) {
      return Promise.reject(new Error('진행 중인 수업의 오디오 입력만 재개할 수 있습니다.'));
    }
    if (!this._context || this._context.state === 'closed') {
      const error = new Error('오디오 입력이 종료되었습니다. 재연결 안내가 나타나면 버튼을 다시 눌러 주세요.');
      this._requireReconnect(error, 'context-closed');
      return Promise.reject(error);
    }

    const context = this._context;
    const finish = () => {
      if (context !== this._context) {
        throw new Error('오디오 입력이 바뀌었습니다. 현재 안내에 따라 다시 연결해 주세요.');
      }
      if (context.state === 'closed') {
        const error = new Error('브라우저가 오디오 입력을 종료했습니다. 재연결 안내가 나타나면 버튼을 다시 눌러 주세요.');
        this._requireReconnect(error, 'context-closed');
        throw error;
      }
      if (context.state !== 'running') {
        const error = new Error('브라우저가 아직 오디오 입력을 재개하지 않았습니다. 이 페이지를 화면에 둔 채 다시 눌러 주세요.');
        this._markInputUnavailable('context-state', error, `context-${context.state}`);
        this._scheduleInputResume();
        throw error;
      }
      if (this._resumeRetryTimer !== null) clearTimeout(this._resumeRetryTimer);
      this._resumeRetryTimer = null;
      this._clearInputUnavailable('context-state', 'user-resume');
      if (this.recording) this._requestWakeLock();
      if ([...this._unavailableReasons.keys()].some((key) => key.startsWith('track-muted-'))) {
        this._requireReconnect(
          new Error(this.captureSource === 'system'
            ? '공유 오디오 신호가 돌아오지 않았습니다. 같은 수업에서 화면을 다시 선택해 주세요.'
            : '마이크 신호가 돌아오지 않았습니다. 같은 수업에서 마이크를 다시 연결해 주세요.'),
          'track-muted-user-reconnect',
        );
      }
      // false means the context resumed but another condition (normally a
      // muted live track) is still waiting for its own recovery event.
      return this._unavailableReasons.size === 0;
    };

    if (context.state === 'running') return Promise.resolve().then(finish);
    let resumed;
    try {
      resumed = context.resume();
    } catch (cause) {
      const error = new Error('브라우저가 오디오 입력 재개 요청을 받지 못했습니다. 페이지를 화면에 둔 채 다시 눌러 주세요.', { cause });
      if (context.state === 'closed') this._requireReconnect(error, 'context-closed');
      else {
        this._markInputUnavailable('context-state', error, `context-${context.state}`);
        this._scheduleInputResume();
      }
      return Promise.reject(error);
    }
    return Promise.resolve(resumed).then(finish, (cause) => {
      const error = new Error('브라우저가 오디오 입력을 재개하지 못했습니다. 페이지를 화면에 둔 채 다시 눌러 주세요.', { cause });
      if (context.state === 'closed') this._requireReconnect(error, 'context-closed');
      else {
        this._markInputUnavailable('context-state', error, `context-${context.state}`);
        this._scheduleInputResume();
      }
      throw error;
    });
  }

  async _openCapture(cancelled, expectedState) {
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
    const generation = ++this._resourceGeneration;
    this._context = context;
    if (!context.audioWorklet) {
      throw new Error('이 브라우저에서는 오디오 녹음을 사용할 수 없습니다. 브라우저를 최신 버전으로 업데이트해 주세요.');
    }
    const resumed = context.resume();
    const constraints = system ? {
      video: true,
      audio: true,
      systemAudio: 'include',
      surfaceSwitching: 'include',
    } : {
      video: false,
      audio: { channelCount: { ideal: 1 }, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    };
    // These permission calls intentionally happen before the first await.
    const permission = getMedia.call(navigator.mediaDevices, constraints).then((stream) => {
      if (this._context !== context || this._state !== expectedState || this._stopRequested) {
        stream.getTracks().forEach((track) => track.stop());
      } else {
        this._stream = stream;
      }
      return stream;
    });
    const module = context.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url));
    await Promise.race([Promise.all([permission, resumed, module]), cancelled]);
    if (this._context !== context || this._state !== expectedState || this._stopRequested) {
      throw new Error(system ? '화면 오디오 연결이 취소되었습니다.' : '마이크 연결이 취소되었습니다.');
    }
    if (context.state !== 'running') {
      throw new Error(system
        ? '화면 오디오가 일시 중단되어 있습니다. 이 페이지를 화면에 표시한 상태에서 다시 연결해 주세요.'
        : '마이크가 일시 중단되어 있습니다. 이 페이지를 화면에 표시한 상태에서 다시 연결해 주세요.');
    }
    const audioTracks = this._stream?.getAudioTracks() || [];
    if (!audioTracks.some((track) => track.readyState === 'live')) {
      throw new Error(system
        ? '선택한 화면에 공유된 오디오가 없습니다. 공유 창에서 “탭 오디오 공유” 또는 “시스템 오디오 공유”를 켜 주세요.'
        : '마이크 연결이 종료되었습니다. 연결을 확인한 뒤 다시 연결해 주세요.');
    }

    this._resampler = new StreamingResampler(context.sampleRate);
    this._audioStream = system ? new MediaStream(audioTracks) : this._stream;
    this._source = context.createMediaStreamSource(this._audioStream);
    const node = new AudioWorkletNode(context, 'classroom-pcm', {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
    });
    this._node = node;
    this._silentGain = context.createGain();
    this._silentGain.gain.value = 0;
    node.port.onmessage = ({ data }) => {
      if (generation !== this._resourceGeneration || node !== this._node) return;
      if (data?.type === 'samples' && this._resampler) {
        try {
          this._acceptSamples(data.samples);
        } catch (error) {
          this._requireReconnect(error, 'sample-processing-error');
        }
      } else if (data?.type === this._controlReceipt?.ack
          && data.id === this._controlReceipt?.id) {
        this._controlReceipt.resolve();
      }
    };
    this._listen(node, 'processorerror', () => this._requireReconnect(
      new Error(system
        ? '화면 오디오 처리가 중단되었습니다. 같은 수업에서 화면 오디오를 다시 연결해 주세요.'
        : '마이크 오디오 처리가 중단되었습니다. 같은 수업에서 마이크를 다시 연결해 주세요.'),
      'processor-error',
    ));
    this._listen(context, 'statechange', () => {
      if (generation !== this._resourceGeneration || context !== this._context
          || this._state === 'idle' || this._state === 'stopping') return;
      if (context.state === 'closed') {
        this._requireReconnect(new Error(
          '브라우저가 오디오 처리를 종료했습니다. 같은 수업에서 오디오 입력을 다시 연결해 주세요.',
        ), 'context-closed');
      } else if (context.state === 'running') {
        this._clearInputUnavailable('context-state', 'context-running');
      } else if (context.state === 'suspended' || context.state === 'interrupted') {
        this._markInputUnavailable(
          'context-state',
          new Error(system
            ? '브라우저가 화면 오디오를 잠시 멈췄습니다. 페이지로 돌아오면 자동으로 재개합니다.'
            : '브라우저가 마이크 입력을 잠시 멈췄습니다. 페이지로 돌아오면 자동으로 재개합니다.'),
          `context-${context.state}`,
        );
        this._scheduleInputResume();
      }
    });
    audioTracks.forEach((track, index) => {
      const muteKey = `track-muted-${index}`;
      this._listen(track, 'ended', () => this._requireReconnect(
        new Error(system
          ? '공유 오디오가 종료되었습니다. 같은 수업에서 재생할 화면을 다시 선택해 주세요.'
          : '마이크 연결이 끊어졌습니다. 같은 수업에서 마이크를 다시 연결해 주세요.'),
        'track-ended',
      ));
      this._listen(track, 'mute', () => {
        if (track.readyState !== 'live') return;
        this._markInputUnavailable(
          muteKey,
          new Error(system
            ? '공유 오디오가 잠시 중단되었습니다. 입력이 돌아올 때까지 기다립니다.'
            : '마이크 입력이 잠시 중단되었습니다. 입력이 돌아올 때까지 기다립니다.'),
          'track-muted',
        );
        this._scheduleInputResume();
      });
      this._listen(track, 'unmute', () => {
        this._clearInputUnavailable(muteKey, 'track-unmuted');
      });
    });
    if (system) {
      // The display track only keeps the browser share alive; video is never
      // routed, encoded, uploaded, or stored.
      for (const track of this._stream.getVideoTracks()) {
        this._listen(track, 'ended', () => this._requireReconnect(
          new Error('화면 공유가 종료되었습니다. 같은 수업에서 화면을 다시 선택해 주세요.'),
          'display-ended',
        ));
      }
    }

    node.connect(this._silentGain);
    this._silentGain.connect(context.destination);
    if (context.state !== 'running'
        || !audioTracks.some((track) => track.readyState === 'live')
        || (system && !this._stream.getVideoTracks().some((track) => track.readyState === 'live'))) {
      throw new Error(system
        ? '선택한 화면 오디오가 연결 준비 중 종료되었습니다. 같은 수업에서 화면을 다시 선택해 주세요.'
        : '마이크가 연결 준비 중 종료되었습니다. 같은 수업에서 마이크를 다시 연결해 주세요.');
    }
    this._state = this._desiredPaused ? 'paused' : 'recording';
    if (!this._desiredPaused) this._connectSource();

    // A successful reconnect clears the permanent failure, but a newly opened
    // track can already be muted. In that case keep the same unavailable episode.
    this._unavailableReasons.clear();
    if (context.state !== 'running') {
      this._unavailableReasons.set('context-state', {
        error: new Error(system
          ? '새로 연결한 화면 오디오가 브라우저에서 잠시 중단되어 있습니다.'
          : '새로 연결한 마이크가 브라우저에서 잠시 중단되어 있습니다.'),
        reason: `context-${context.state}`,
      });
    }
    audioTracks.forEach((track, index) => {
      if (track.muted && track.readyState === 'live') {
        this._unavailableReasons.set(`track-muted-${index}`, {
          error: new Error(system
            ? '새로 선택한 공유 오디오가 아직 음소거되어 있습니다.'
            : '새로 연결한 마이크가 아직 음소거되어 있습니다.'),
          reason: 'track-muted',
        });
      }
    });
    if (this._unavailableReasons.size === 0) {
      this._reportInputRecovered(expectedState === 'reconnecting' ? 'reconnected' : 'started');
    } else if (!this._availabilityEpisode || expectedState === 'reconnecting') {
      const first = this._unavailableReasons.values().next().value;
      this._availabilityEpisode = true;
      this._notify(this.onInputUnavailable, first.error, {
        reason: first.reason,
        reconnectNeeded: false,
        source: this.captureSource,
      });
    }
    if (this._unavailableReasons.has('context-state')) this._scheduleInputResume();
    this._reconnectError = null;
    this._reconnectReason = null;
    this._reconnectReported = false;
    if (this.recording) this._requestWakeLock();
  }

  _listen(target, event, handler, bucket = this._resourceListeners) {
    target.addEventListener(event, handler);
    bucket.push(() => target.removeEventListener(event, handler));
  }

  _installSessionListeners() {
    this._releaseSessionListeners();
    this._listen(document, 'visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        this._requestWakeLock();
        this._scheduleInputResume();
      } else {
        this.checkpoint();
      }
    }, this._sessionListeners);
    this._listen(globalThis, 'pagehide', () => {
      this.checkpoint();
    }, this._sessionListeners);
  }

  _releaseSessionListeners() {
    this._sessionListeners.splice(0).forEach((remove) => remove());
  }

  _notify(callback, ...args) {
    try { callback(...args); } catch { /* UI callbacks must not terminate capture. */ }
  }

  _markInputUnavailable(key, error, reason, reconnectNeeded = false, checkpointAcceptedAudio = true) {
    if (this._state === 'idle' || this._state === 'stopping') return;
    const normalized = captureError(error, this.captureSource);
    this._unavailableReasons.set(key, { error: normalized, reason });
    if (this._availabilityEpisode) return;
    this._availabilityEpisode = true;
    // Persist every complete sample already accepted by the worklet before a
    // mobile browser gets a chance to freeze or discard the page process.
    if (checkpointAcceptedAudio) this.checkpoint();
    this._notify(this.onInputUnavailable, normalized, {
      reason,
      reconnectNeeded,
      source: this.captureSource,
    });
  }

  _clearInputUnavailable(key, recoveredBy) {
    this._unavailableReasons.delete(key);
    if (this._unavailableReasons.size === 0) this._reportInputRecovered(recoveredBy);
  }

  _reportInputRecovered(recoveredBy) {
    if (!this._availabilityEpisode || this._state === 'reconnect-needed'
        || this._state === 'reconnecting' || this._state === 'stopping'
        || this._state === 'idle') return;
    this._availabilityEpisode = false;
    this._reconnectReported = false;
    this._notify(this.onInputRecovered, {
      reason: recoveredBy,
      source: this.captureSource,
    });
  }

  _resetAvailability() {
    this._unavailableReasons.clear();
    this._availabilityEpisode = false;
    this._reconnectReported = false;
    this._reconnectError = null;
    this._reconnectReason = null;
  }

  _scheduleInputResume() {
    if (this._resumeRetryTimer !== null || this._resumeAttempt
        || !this._context || this._context.state === 'closed'
        || this._state === 'idle' || this._state === 'stopping'
        || this._state === 'reconnect-needed' || this._state === 'reconnecting') return;
    if (document.visibilityState !== 'visible') return;
    const context = this._context;
    if (context.state === 'running') return;
    let resumed;
    try { resumed = context.resume(); } catch { return; }
    let attempt;
    attempt = Promise.resolve(resumed).catch(() => {}).finally(() => {
      if (this._context === context && context.state === 'running') {
        this._clearInputUnavailable('context-state', 'automatic-resume');
      }
      if (this._resumeAttempt === attempt) this._resumeAttempt = null;
      if (this._context === context && context.state !== 'running'
          && context.state !== 'closed' && document.visibilityState === 'visible') {
        this._resumeRetryTimer = setTimeout(() => {
          this._resumeRetryTimer = null;
          this._scheduleInputResume();
        }, AUTO_RESUME_RETRY_MS);
      }
    });
    this._resumeAttempt = attempt;
  }

  _requireReconnect(error, reason) {
    if (this._state === 'idle' || this._state === 'starting'
        || this._state === 'stopping' || this._state === 'reconnect-needed'
        || this._state === 'reconnecting' || this._stopRequested) return;
    this._desiredPaused = this._state === 'paused' || this._state === 'pausing';
    this._state = 'reconnect-needed';
    this._disconnectSource();
    this._cancelControl(error);
    this._reconnectError = captureError(error, this.captureSource);
    this._reconnectReason = reason;
    // Defer the drain by one microtask so the promise is published before any
    // onChunk/onInputUnavailable callback can synchronously request stop().
    const preparation = Promise.resolve().then(() => this._drainDisconnectedGraph()).catch(() => {});
    this._reconnectPreparation = preparation;
    preparation.finally(() => {
      if (this._reconnectPreparation === preparation) this._reconnectPreparation = null;
      if (this._state === 'reconnect-needed' && !this._stopRequested) {
        this._reportReconnectNeeded();
      }
    });
    // The disconnected graph is drained immediately below. Waiting for its
    // resampler tail lets us persist one complete boundary instead of first
    // checkpointing and then creating a second near-overlap-only WAV for the
    // final interpolation sample.
    this._markInputUnavailable('reconnect', this._reconnectError, reason, true, false);
    this._notify(this.onLevel, 0);
  }

  async _drainDisconnectedGraph() {
    try {
      if (this._node && this._context?.state === 'running') {
        await this._sendControl(
          'pause',
          'paused',
          '중단된 오디오의 마지막 버퍼를 확인하지 못했습니다.',
          RECONNECT_DRAIN_TIMEOUT_MS,
        ).catch(() => {});
      }
      if (this._resampler) {
        try { this._consumePCM(this._resampler.flush()); } catch { /* Keep already accepted PCM. */ }
        this._resampler = null;
      }
      this._pauseBoundaryPending = false;
      this.checkpoint();
    } finally {
      await this._releaseResources();
    }
  }

  _reportReconnectNeeded() {
    if (this._reconnectReported || this._state !== 'reconnect-needed') return;
    this._reconnectReported = true;
    this._notify(this.onReconnectNeeded, this._reconnectError, {
      reason: this._reconnectReason,
      source: this.captureSource,
    });
  }

  _acceptSamples(samples) {
    this._consumePCM(this._resampler.push(samples));
    if (this.recording) {
      let power = 0;
      for (const sample of samples) power += sample * sample;
      this._notify(this.onLevel, Math.min(1, Math.sqrt(power / Math.max(1, samples.length))));
    }
  }

  _consumePCM(samples) {
    // Quiet input is still recording data. Only an explicit manual pause or
    // unavailable input may interrupt capture; amplitude never removes PCM.
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
      this._chunkStartSamples += count;
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

  checkpoint() {
    if (!this._chunk || this._state === 'idle' || this._state === 'starting'
        || this._state === 'stopping') return false;
    const fresh = this._chunkUsed - this._chunkOverlap;
    if (fresh <= 0 || this._chunkUsed < MIN_UPLOAD_SAMPLES) return false;
    return this._emitChunk(false);
  }

  _sendControl(type, ack, timeoutMessage, timeoutMs = 2500) {
    if (!this._node || this._context?.state === 'closed') {
      return Promise.reject(new Error(timeoutMessage));
    }
    if (this._controlReceipt) {
      return Promise.reject(new Error('다른 오디오 경계 처리가 끝나기를 기다리고 있습니다.'));
    }
    return new Promise((resolve, reject) => {
      const id = ++this._controlSequence;
      const receipt = {
        id,
        ack,
        resolve: () => {
          clearTimeout(receipt.timer);
          if (this._controlReceipt === receipt) this._controlReceipt = null;
          resolve();
        },
        reject: (error) => {
          clearTimeout(receipt.timer);
          if (this._controlReceipt === receipt) this._controlReceipt = null;
          reject(error);
        },
        timer: null,
      };
      receipt.timer = setTimeout(() => {
        if (this._controlReceipt === receipt) this._controlReceipt = null;
        reject(new Error(timeoutMessage));
      }, timeoutMs);
      this._controlReceipt = receipt;
      try {
        this._node.port.postMessage({ type, id });
      } catch (error) {
        receipt.reject(error);
      }
    });
  }

  _cancelControl(error = new Error('오디오 경계 처리가 취소되었습니다.')) {
    this._controlReceipt?.reject(error);
  }

  _connectSource() {
    if (!this._source || !this._node || this._sourceConnected) return;
    this._source.connect(this._node);
    this._sourceConnected = true;
  }

  _disconnectSource() {
    if (!this._source || !this._sourceConnected) return;
    try { this._source.disconnect(); } catch { /* Already disconnected. */ }
    this._sourceConnected = false;
  }

  pause() {
    if (this._pausePromise) return this._pausePromise;
    if (this._state === 'paused') return Promise.resolve();
    if (this._state !== 'recording' || this._resumePromise || this._stopPromise) {
      return Promise.reject(new Error('현재 녹음 중일 때만 일시정지할 수 있습니다.'));
    }
    this._desiredPaused = true;
    this._state = 'pausing';
    this._pausePromise = this._finishPause().catch((error) => {
      if (this._state === 'pausing') {
        this._state = 'recording';
        this._desiredPaused = false;
      }
      throw error;
    }).finally(() => { this._pausePromise = null; });
    return this._pausePromise;
  }

  async _finishPause() {
    let boundaryConfirmed = false;
    if (this._context && this._context.state !== 'closed') {
      if (this._context.state !== 'running') {
        await this._context.resume().catch(() => {});
      }
    }
    if (this._context?.state === 'running') {
      try {
        await this._sendControl(
          'pause',
          'paused',
          '오디오가 응답하지 않아 일시정지 경계를 확인하지 못했습니다.',
        );
        boundaryConfirmed = true;
      } catch (error) {
        // A suspended graph is already producing no PCM. Pause locally and keep
        // the lecture open; a permanent failure changes state in its event handler.
        if (this._state === 'reconnect-needed') throw error;
      }
    }
    if (this._state === 'reconnect-needed') throw this._reconnectError;
    this._disconnectSource();
    if (boundaryConfirmed) {
      if (this._resampler) this._consumePCM(this._resampler.flush());
      this._resampler = null;
      this._pauseBoundaryPending = false;
    } else {
      // Keep the live resampler until resume/stop. The worklet may still hold up
      // to one small block which must be drained before a new resampler is made.
      this._pauseBoundaryPending = !!this._resampler;
    }
    // Do not resend an overlap-only guard on repeated boundaries. A final stop
    // still sends that guard later so the server can close the recording.
    const freshSamples = this._chunkUsed - this._chunkOverlap;
    // The API's shortest WAV is 50 ms. Keep a smaller first boundary in this
    // capture buffer until resume/stop instead of padding the server recording
    // beyond the real timeline and conflicting with the next resumed samples.
    // After the first upload, the retained overlap already makes the WAV long
    // enough for the API. Flush even one fresh sample so a paused tab crash
    // cannot strand up to another 50 ms of real audio in browser memory.
    if (freshSamples > 0 && this._chunkUsed >= MIN_UPLOAD_SAMPLES) this._emitChunk(false);
    this._state = 'paused';
    this._notify(this.onLevel, 0);
    await this._releaseWakeLock();
  }

  resume() {
    if (this._resumePromise) return this._resumePromise;
    if (this._state === 'recording') return Promise.resolve();
    if (this._state !== 'paused' || this._pausePromise || this._stopPromise) {
      return Promise.reject(new Error('일시정지된 녹음만 재개할 수 있습니다.'));
    }
    this._desiredPaused = false;
    this._state = 'resuming';
    this._resumePromise = this._finishResume().catch((error) => {
      if (this._state === 'resuming') {
        this._disconnectSource();
        if (!this._pauseBoundaryPending) this._resampler = null;
        this._state = 'paused';
        this._desiredPaused = true;
      }
      throw error;
    }).finally(() => { this._resumePromise = null; });
    return this._resumePromise;
  }

  async _finishResume() {
    const system = this.captureSource === 'system';
    const readinessError = (requireRunning = false) => {
      if (!this._stream?.getAudioTracks().some((track) => track.readyState === 'live')) {
        return new Error(system
          ? '공유 오디오가 종료되었습니다. 같은 수업에서 화면 오디오를 다시 연결해 주세요.'
          : '마이크 연결이 종료되었습니다. 같은 수업에서 마이크를 다시 연결해 주세요.');
      }
      if (!this._context || this._context.state === 'closed') {
        return new Error('오디오 처리가 종료되었습니다. 같은 수업에서 입력을 다시 연결해 주세요.');
      }
      if (requireRunning && this._context.state !== 'running') {
        return new Error('오디오 처리가 아직 재개되지 않았습니다. 페이지를 화면에 둔 뒤 다시 눌러 주세요.');
      }
      return null;
    };
    const before = readinessError();
    if (before) {
      this._requireReconnect(before, 'resume-input-ended');
      throw before;
    }
    // This call is still reached from the user's resume-button gesture.
    await this._context.resume();
    const resumed = readinessError(true);
    if (resumed) throw resumed;
    if (this._pauseBoundaryPending) {
      await this._sendControl(
        'pause',
        'paused',
        '일시정지 직전 오디오를 아직 정리하지 못했습니다. 잠시 후 재개를 다시 눌러 주세요.',
      );
      if (this._resampler) this._consumePCM(this._resampler.flush());
      this._resampler = null;
      this._pauseBoundaryPending = false;
      this.checkpoint();
    }
    this._resampler = new StreamingResampler(this._context.sampleRate);
    try {
      await this._sendControl(
        'resume',
        'resumed',
        '오디오가 응답하지 않아 녹음 재개를 확인하지 못했습니다.',
      );
    } catch (error) {
      if (this._state === 'reconnect-needed') throw error;
      // The worklet may have accepted resume just before the timeout. Continue
      // live so stop() can later drain it instead of finalizing this lecture now.
    }
    if (this._state === 'reconnect-needed') throw this._reconnectError;
    this._connectSource();
    const connected = readinessError(true);
    if (connected) {
      this._requireReconnect(connected, 'resume-input-ended');
      throw connected;
    }
    this._state = 'recording';
    this._requestWakeLock();
  }

  stop() {
    if (this._stopPromise) return this._stopPromise;
    if (this._state === 'idle') return Promise.resolve();
    this._stopRequested = true;
    this._stopPromise = (async () => {
      // Navigation/auth expiry can request a stop while a pause or resume
      // acknowledgement is in flight. Serialize the controls so two commands
      // never overwrite one another's receipt or split the boundary.
      await this._pausePromise?.catch(() => {});
      await this._resumePromise?.catch(() => {});
      await this._finishStop();
    })().finally(() => {
      this._stopPromise = null;
      this._stopRequested = false;
    });
    return this._stopPromise;
  }

  async _finishStop() {
    if (this._state === 'starting') {
      this._cancelStart?.();
      await this._startPromise?.catch(() => {});
      return;
    }
    if (this._state === 'reconnecting') {
      this._state = 'stopping';
      this._cancelReconnect?.();
      await this._reconnectPromise?.catch(() => {});
    }
    this._state = 'stopping';
    await this._reconnectPreparation?.catch(() => {});
    let flushError = null;
    try {
      if (this._node && this._context?.state !== 'closed') {
        try {
          const confirmed = this._sendControl(
            'stop',
            'stopped',
            this.captureSource === 'system'
              ? '화면 오디오가 응답하지 않아 마지막 조각의 완전한 수신을 확인하지 못했습니다. 받은 내용은 저장되며, 공유를 다시 시작할 수 있습니다.'
              : '마이크가 응답하지 않아 마지막 오디오 조각의 완전한 수신을 확인하지 못했습니다. 받은 내용은 저장되며, 녹음을 다시 시작할 수 있습니다.',
          );
          // An interrupted tablet may need this to deliver its flush receipt.
          if (this._context.state !== 'running') this._context.resume().catch(() => {});
          await confirmed;
        } catch (error) {
          flushError = error;
        }
      } else if (this._node) {
        flushError = new Error('기기가 오디오 처리를 종료하여 마지막 오디오 조각을 확인하지 못했습니다. 받은 내용은 저장됩니다.');
      }
      if (this._resampler) this._consumePCM(this._resampler.flush());
      this._emitChunk(true);
    } finally {
      await this._releaseResources();
      this._resampler = null;
      this._pauseBoundaryPending = false;
      this._releaseSessionListeners();
      this._resetAvailability();
      this._state = 'idle';
      this._notify(this.onLevel, 0);
    }
    if (flushError) throw flushError;
  }

  _requestWakeLock() {
    if (!this.recording || this._wakeLock || this._wakeRequest
        || !navigator.wakeLock || document.visibilityState !== 'visible') return;
    const context = this._context;
    let request;
    request = navigator.wakeLock.request('screen').then((lock) => {
      if (!this.recording || context !== this._context) {
        return lock.release().catch(() => {});
      }
      this._wakeLock = lock;
      lock.addEventListener('release', () => {
        if (this._wakeLock === lock) this._wakeLock = null;
      }, { once: true });
    }).catch(() => {
      // Recording works without this optional permission (e.g. low battery).
    }).finally(() => {
      if (this._wakeRequest === request) this._wakeRequest = null;
      // A request belonging to a dead graph can finish after reconnect().
      if (this.recording && context !== this._context) this._requestWakeLock();
    });
    this._wakeRequest = request;
  }

  async _releaseWakeLock() {
    const lock = this._wakeLock;
    this._wakeLock = null;
    if (lock) await lock.release().catch(() => {});
  }

  async _releaseResources() {
    ++this._resourceGeneration;
    if (this._resumeRetryTimer !== null) clearTimeout(this._resumeRetryTimer);
    this._resumeRetryTimer = null;
    this._resumeAttempt = null;
    this._resourceListeners.splice(0).forEach((remove) => remove());
    this._cancelControl(new Error('오디오 입력이 정리되었습니다.'));
    for (const node of [this._source, this._node, this._silentGain]) {
      try { node?.disconnect(); } catch { /* Already disconnected. */ }
    }
    this._stream?.getTracks().forEach((track) => {
      try { track.stop(); } catch { /* Track is already unavailable. */ }
    });
    if (this._node) this._node.port.onmessage = null;
    try { this._node?.port.close(); } catch { /* Port is already closed. */ }
    const context = this._context;
    this._context = null;
    this._stream = null;
    this._audioStream = null;
    this._source = null;
    this._sourceConnected = false;
    this._node = null;
    this._silentGain = null;
    await this._releaseWakeLock();
    if (context && context.state !== 'closed') await context.close().catch(() => {});
  }
}
