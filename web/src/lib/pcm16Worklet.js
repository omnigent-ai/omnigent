// AudioWorklet processor for dictation. Linear-interpolation downsample from
// the context capture rate to 16 kHz, Float32 to Int16, posted in 100 ms chunks.
// (When the context already runs at 16 kHz, as dictation asks, the step is 1
// and the loop is a plain format conversion.)
//
// Any message on the port means "flush": the partially-filled chunk is posted,
// then a null marker, so stop() can capture trailing speech that hasn't crossed
// the 100 ms boundary before tearing the graph down.
const TARGET_RATE = 16000; // dictation.ts's TARGET_RATE
const CHUNK_SAMPLES = TARGET_RATE / 10; // 100 ms per posted chunk
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.pos = 0; // fractional read position, carried across blocks
    this.pending = new Int16Array(CHUNK_SAMPLES);
    this.filled = 0;
    this.port.onmessage = () => {
      if (this.filled > 0) {
        const out = this.pending.slice(0, this.filled);
        this.filled = 0;
        this.port.postMessage(out, [out.buffer]);
      }
      this.port.postMessage(null);
    };
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;
    let pos = this.pos;
    while (pos < channel.length) {
      const i = Math.floor(pos);
      const s0 = channel[i];
      const s1 = i + 1 < channel.length ? channel[i + 1] : s0;
      const sample = s0 + (s1 - s0) * (pos - i);
      const clamped = Math.max(-1, Math.min(1, sample));
      this.pending[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      if (this.filled === CHUNK_SAMPLES) {
        const out = this.pending;
        this.pending = new Int16Array(CHUNK_SAMPLES);
        this.filled = 0;
        this.port.postMessage(out, [out.buffer]);
      }
      pos += this.step;
    }
    this.pos = pos - channel.length;
    return true;
  }
}
registerProcessor("omnigent-pcm16-downsampler", Pcm16Downsampler);
