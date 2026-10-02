// Node self-test for zordon/web/worklet.js: shims the AudioWorklet globals,
// feeds a 440 Hz tone at 48 kHz and 44.1 kHz through process() in 128-sample
// blocks and checks the output is 16 kHz Int16 in 320-sample frames with the
// tone intact and no clipping. Run: node tests/web/worklet_test.cjs
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const WORKLET = path.join(__dirname, '..', '..', 'zordon', 'web', 'worklet.js');
const TARGET = 16000;
const FRAME = 320;

function loadProcessor(rate, frames) {
  global.sampleRate = rate;
  global.AudioWorkletProcessor = class {
    constructor() {
      this.port = {
        postMessage: (m, transfer) => {
          assert.ok(m.pcm16 instanceof ArrayBuffer, 'frame payload must be an ArrayBuffer');
          assert.ok(Array.isArray(transfer) && transfer[0] === m.pcm16, 'buffer must be transferred');
          assert.strictEqual(m.rate, TARGET);
          frames.push(new Int16Array(m.pcm16.slice(0)));
        },
        onmessage: null,
      };
    }
  };
  let Cls = null;
  let registeredName = null;
  global.registerProcessor = (name, c) => {
    registeredName = name;
    Cls = c;
  };
  const src = fs.readFileSync(WORKLET, 'utf8');
  new Function(src)(); // runs registerProcessor
  assert.strictEqual(registeredName, 'pcm16-downsampler');
  assert.ok(Cls, 'worklet must register a processor');
  return new Cls({ processorOptions: { targetRate: TARGET, frameSamples: FRAME } });
}

function feedTone(p, rate, seconds, freq, amp, blockSize) {
  const total = Math.round(rate * seconds);
  let n = 0;
  while (n < total) {
    const len = Math.min(blockSize, total - n);
    const block = new Float32Array(len);
    for (let i = 0; i < len; i++) block[i] = amp * Math.sin((2 * Math.PI * freq * (n + i)) / rate);
    const keep = p.process([[block]], [], {});
    assert.strictEqual(keep, true, 'process() must return true to stay alive');
    n += len;
  }
}

function concat(frames) {
  const all = new Int16Array(frames.length * FRAME);
  frames.forEach((f, i) => all.set(f, i * FRAME));
  return all;
}

function estimateFreq(all, skip) {
  // Zero crossings over the steady part of the signal (skip the filter warm-up).
  let zc = 0;
  for (let i = skip + 1; i < all.length; i++) if (all[i - 1] < 0 !== all[i] < 0) zc++;
  const seconds = (all.length - skip) / TARGET;
  return zc / 2 / seconds;
}

function peakOf(all) {
  let peak = 0;
  for (let i = 0; i < all.length; i++) {
    const a = Math.abs(all[i]);
    if (a > peak) peak = a;
  }
  return peak;
}

function runTone(rate) {
  const frames = [];
  const p = loadProcessor(rate, frames);
  feedTone(p, rate, 1.0, 440, 0.5, 128);

  for (const f of frames) assert.strictEqual(f.length, FRAME, 'every frame is 320 samples');
  const expected = TARGET / FRAME; // 50 frames per second
  assert.ok(
    frames.length >= expected - 1 && frames.length <= expected,
    `${rate}: expected ~${expected} frames, got ${frames.length}`,
  );
  const all = concat(frames);
  const freq = estimateFreq(all, 320);
  assert.ok(Math.abs(freq - 440) <= 3, `${rate}: tone drifted to ${freq.toFixed(1)} Hz`);
  const peak = peakOf(all);
  const nominal = 0.5 * 32767;
  assert.ok(peak <= nominal * 1.05, `${rate}: peak ${peak} exceeds input amplitude (gain error)`);
  assert.ok(peak >= nominal * 0.9, `${rate}: peak ${peak} too low (passband loss)`);
  assert.ok(peak < 32767, `${rate}: clipping`);
  console.log(
    `ok ${rate} Hz -> ${TARGET} Hz: frames=${frames.length} samples=${all.length} freq=${freq.toFixed(1)} Hz peak=${peak}`,
  );
}

function runFullScale() {
  // A full-scale input must saturate cleanly at the int16 limits, never wrap.
  const frames = [];
  const p = loadProcessor(48000, frames);
  feedTone(p, 48000, 0.2, 300, 1.0, 128);
  const all = concat(frames);
  const peak = peakOf(all);
  assert.ok(peak <= 32768 && peak >= 32000, `full-scale peak ${peak} outside [32000, 32768]`);
  console.log(`ok full-scale clamp: peak=${peak}`);
}

function runAliasRejection() {
  // A 15 kHz tone at 48 kHz is above the 8 kHz output Nyquist; after the FIR it must be
  // strongly attenuated instead of folding back as a 1 kHz alias.
  const frames = [];
  const p = loadProcessor(48000, frames);
  feedTone(p, 48000, 0.5, 15000, 0.5, 128);
  const all = concat(frames);
  const peak = peakOf(all.subarray(640));
  assert.ok(peak < 0.5 * 32767 * 0.05, `alias rejection too weak: peak ${peak}`);
  console.log(`ok alias rejection: 15 kHz -> residual peak ${peak}`);
}

function runVariableBlockSizes() {
  // process() must not assume 128-sample blocks.
  const frames = [];
  const p = loadProcessor(48000, frames);
  feedTone(p, 48000, 0.5, 440, 0.5, 97);
  assert.ok(frames.length >= 24 && frames.length <= 25, `odd block size: ${frames.length} frames`);
  console.log(`ok odd block size: frames=${frames.length}`);
}

function runMuteAndEmptyInput() {
  const frames = [];
  const p = loadProcessor(48000, frames);
  assert.strictEqual(p.process([[]], [], {}), true, 'empty input keeps the node alive');
  assert.strictEqual(p.process([], [], {}), true, 'no input keeps the node alive');
  p.port.onmessage({ data: { muted: true } });
  feedTone(p, 48000, 0.5, 440, 0.5, 128);
  assert.strictEqual(frames.length, 0, 'muted processor emits no frames');
  p.port.onmessage({ data: { muted: false } });
  feedTone(p, 48000, 0.5, 440, 0.5, 128);
  assert.ok(frames.length >= 24, `unmuted again: ${frames.length} frames`);
  console.log(`ok mute/unmute and empty input`);
}

runTone(48000);
runTone(44100);
runFullScale();
runAliasRejection();
runVariableBlockSizes();
runMuteAndEmptyInput();
console.log('worklet_test: all passed');
