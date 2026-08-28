class QDC507CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.outputSamples = [];
    this.enabled = true;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === "enabled") this.enabled = Boolean(event.data.value);
    };
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let index = 0; index < input.length; index += 1) {
      this.sum += this.enabled ? input[index] : 0;
      this.count += 1;
      this.phase += 8000;
      if (this.phase >= sampleRate) {
        const value = Math.max(-1, Math.min(1, this.sum / Math.max(1, this.count)));
        this.outputSamples.push(value < 0 ? Math.round(value * 32768) : Math.round(value * 32767));
        this.phase -= sampleRate;
        this.sum = 0;
        this.count = 0;
      }
    }
    while (this.outputSamples.length >= 160) {
      const frame = new Int16Array(160);
      for (let index = 0; index < frame.length; index += 1) frame[index] = this.outputSamples[index];
      this.outputSamples.splice(0, frame.length);
      this.port.postMessage(frame.buffer, [frame.buffer]);
    }
    return true;
  }
}

class QDC507PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.offset = 0;
    this.position = 0;
    this.step = 8000 / sampleRate;
    this.port.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) return;
      const input = new Int16Array(event.data);
      for (let index = 0; index < input.length; index += 1) {
        this.samples.push(input[index] / (input[index] < 0 ? 32768 : 32767));
      }
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0] && outputs[0][0];
    if (!output) return true;
    for (let index = 0; index < output.length; index += 1) {
      const leftIndex = this.offset + Math.floor(this.position);
      if (leftIndex + 1 >= this.samples.length) {
        output[index] = 0;
        continue;
      }
      const fraction = this.position - Math.floor(this.position);
      const left = this.samples[leftIndex];
      const right = this.samples[leftIndex + 1];
      output[index] = left + (right - left) * fraction;
      this.position += this.step;
      const consumed = Math.floor(this.position);
      if (consumed > 0) {
        this.offset += consumed;
        this.position -= consumed;
      }
    }
    if (this.offset > 4096) {
      this.samples = this.samples.slice(this.offset);
      this.offset = 0;
    }
    return true;
  }
}

registerProcessor("qdc507-capture", QDC507CaptureProcessor);
registerProcessor("qdc507-playback", QDC507PlaybackProcessor);
