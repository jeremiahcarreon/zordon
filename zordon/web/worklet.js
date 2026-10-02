// AudioWorkletProcessor: mono float input at the context rate (whatever the
// device gave us: 48000 on nearly every phone and laptop, 44100 on some Macs
// and older iPhones) -> 16 kHz Int16 PCM, posted to the main thread as 20 ms
// frames (320 samples = 640 bytes), which is exactly one "audio" message.
//
// Facts relied on (MDN AudioWorkletProcessor.process):
//  - inputs[n][m] is a Float32Array of (currently) 128 samples in [-1, 1];
//    "you must always check the size of the sample array rather than assuming".
//  - inputs[0] is an EMPTY array when nothing is connected -> guard it.
//  - returning true keeps the node alive.
//  - `sampleRate` is a global in AudioWorkletGlobalScope (the context's rate).
//
// Anti-aliasing: a windowed-sinc FIR low-pass at 0.45 x targetRate (7.2 kHz)
// runs at the input rate before the fractional-ratio decimation, so content
// above 8 kHz does not fold back into the speech band. Linear interpolation
// between filtered samples handles non-integer ratios (44.1 kHz -> 2.75625).
//
// Messages:
//   in : { muted: bool }                 pause/resume producing frames
//   out: { pcm16: ArrayBuffer, rate, peak }  one 320-sample Int16 frame (transferred)

class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.targetRate = o.targetRate || 16000;
    this.frameSamples = o.frameSamples || 320; // 20 ms @ 16 kHz
    this.ratio = sampleRate / this.targetRate; // 3.0 for 48k, 2.75625 for 44.1k
    this.pos = 0; // fractional read cursor into `acc`
    this.acc = new Float32Array(0); // filtered samples not yet consumed
    this.out = new Int16Array(this.frameSamples);
    this.outLen = 0;
    this.peak = 0;
    this.muted = false;

    // FIR design: Hamming-windowed sinc, odd length, unity DC gain.
    const cutoff = Math.min(0.45 * this.targetRate, 0.45 * sampleRate);
    const taps = Math.max(9, 2 * Math.round(8 * Math.max(1, this.ratio)) + 1);
    this.h = Pcm16Downsampler.designLowpass(taps, cutoff / sampleRate);
    this.hist = new Float32Array(taps - 1); // last taps-1 raw input samples

    this.port.onmessage = (e) => {
      if (e.data && 'muted' in e.data) this.muted = !!e.data.muted;
    };
  }

  // fcNorm = cutoff / sampleRate (cycles per sample).
  static designLowpass(taps, fcNorm) {
    const h = new Float32Array(taps);
    const mid = (taps - 1) / 2;
    let sum = 0;
    for (let n = 0; n < taps; n++) {
      const k = n - mid;
      const sinc = k === 0 ? 2 * fcNorm : Math.sin(2 * Math.PI * fcNorm * k) / (Math.PI * k);
      const w = 0.54 - 0.46 * Math.cos((2 * Math.PI * n) / (taps - 1));
      h[n] = sinc * w;
      sum += h[n];
    }
    for (let n = 0; n < taps; n++) h[n] /= sum;
    return h;
  }

  // Filter one block at the input rate. Returns a Float32Array of block.length.
  filter(block) {
    const h = this.h;
    const taps = h.length;
    const histLen = taps - 1;
    const joined = new Float32Array(histLen + block.length);
    joined.set(this.hist, 0);
    joined.set(block, histLen);
    const out = new Float32Array(block.length);
    for (let i = 0; i < block.length; i++) {
      let s = 0;
      for (let k = 0; k < taps; k++) s += h[k] * joined[i + k];
      out[i] = s;
    }
    this.hist = joined.subarray(joined.length - histLen);
    return out;
  }

  process(inputs, outputs, parameters) {
    const ch = inputs[0] && inputs[0][0]; // mono: channelCount:1 requested
    if (!ch || ch.length === 0) return true; // nothing connected yet
    if (this.muted) {
      // Keep the filter state moving so there is no click on unmute, but emit nothing.
      this.filter(ch);
      this.acc = new Float32Array(0);
      this.pos = 0;
      return true;
    }

    const filtered = this.filter(ch);
    const joined = new Float32Array(this.acc.length + filtered.length);
    joined.set(this.acc, 0);
    joined.set(filtered, this.acc.length);

    // Linear-interpolation decimation at the fractional ratio.
    let p = this.pos;
    while (p + 1 < joined.length) {
      const i0 = p | 0;
      const frac = p - i0;
      const s = joined[i0] + (joined[i0 + 1] - joined[i0]) * frac;
      const v = s < -1 ? -1 : s > 1 ? 1 : s;
      const q = v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
      this.out[this.outLen++] = q;
      const a = q < 0 ? -q : q;
      if (a > this.peak) this.peak = a;
      if (this.outLen === this.frameSamples) {
        // Transfer the buffer (zero-copy) and start a fresh one.
        this.port.postMessage(
          { pcm16: this.out.buffer, rate: this.targetRate, peak: this.peak },
          [this.out.buffer],
        );
        this.out = new Int16Array(this.frameSamples);
        this.outLen = 0;
        this.peak = 0;
      }
      p += this.ratio;
    }
    // Keep the unread tail (the sample before the cursor is needed for
    // interpolation). When the cursor already points past the end of this
    // block, carry the overshoot into `pos` instead of resetting it to zero;
    // dropping it would stretch time by one sample per block (about 1 percent
    // of pitch at 48 kHz).
    const keepFrom = Math.min(p | 0, joined.length);
    this.acc = joined.slice(keepFrom);
    this.pos = p - keepFrom;
    return true;
  }
}

registerProcessor('pcm16-downsampler', Pcm16Downsampler);
