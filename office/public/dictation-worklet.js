// Downsamples microphone audio to 16 kHz mono signed 16-bit PCM, the format
// Omnigent's dictation stream expects, and posts each chunk to the page.
class DictationDownsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.position = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    const output = new Int16Array(Math.ceil(channel.length / this.ratio) + 1);
    let written = 0;
    while (this.position < channel.length) {
      const sample = Math.max(-1, Math.min(1, channel[Math.floor(this.position)]));
      output[written++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.position += this.ratio;
    }
    this.position -= channel.length;
    if (written) this.port.postMessage(output.slice(0, written).buffer, [output.buffer]);
    return true;
  }
}

registerProcessor("dictation-downsampler", DictationDownsampler);
