/* AudioWorklet globals are supplied by the browser's audio rendering scope. */
class ClassroomPCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(2048);
    this.used = 0;
    this.stopped = false;
    this.port.onmessage = ({ data }) => {
      if (data?.type !== 'stop' || this.stopped) return;
      this.flush();
      this.stopped = true;
      this.port.postMessage({ type: 'stopped', id: data.id });
    };
  }

  flush() {
    if (!this.used) return;
    const samples = this.block.slice(0, this.used);
    this.used = 0;
    this.port.postMessage({ type: 'samples', samples }, [samples.buffer]);
  }

  append(sample) {
    this.block[this.used] = sample;
    this.used += 1;
    if (this.used === this.block.length) this.flush();
  }

  process(inputs, outputs) {
    if (this.stopped) return false;
    const channels = inputs[0] || [];
    const frameCount = channels.reduce((largest, channel) => Math.max(largest, channel.length), 0);
    for (let frame = 0; frame < frameCount; frame += 1) {
      let total = 0;
      let activeChannels = 0;
      for (const channel of channels) {
        if (frame < channel.length) {
          total += channel[frame];
          activeChannels += 1;
        }
      }
      this.append(activeChannels ? total / activeChannels : 0);
    }
    // The connected output is muted by the main thread; clear it defensively.
    for (const output of outputs || []) {
      for (const channel of output) channel.fill(0);
    }
    return true;
  }
}

registerProcessor('classroom-pcm', ClassroomPCMProcessor);
