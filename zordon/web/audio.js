// Browser audio path for the Zordon web client: microphone capture through the
// worklet downsampler, and gapless playback of streamed int16 speech with the
// barge-in flush. Classic script; publishes window.ZordonAudio.
//
// iOS Safari rules this file follows (MDN Web Audio best practices, WebKit):
//  * the AudioContext is created or resumed INSIDE a tap handler (unlock()),
//    otherwise it starts 'suspended' and nothing plays;
//  * the context can become 'interrupted' (phone call, Siri, headset change);
//    resume() is retried from the next gesture;
//  * no sampleRate is forced on the context or the mic: the device rate is
//    used and the worklet downsamples to 16 kHz;
//  * AudioWorklet needs Safari 14.1 / iOS 14.5 or later.
//
// Barge-in: every speech chunk carries a generation; a "flush" message names
// the generation whose older chunks are stale. flush() stops every scheduled
// buffer source within one render quantum and later chunks with a smaller
// generation are dropped in onSpeech().

(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module && module.exports) module.exports = api;
  root.ZordonAudio = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  function protocol() {
    if (root.ZordonProtocol) return root.ZordonProtocol;
    if (typeof require === 'function') return require('./protocol.js');
    throw new Error('ZordonProtocol must load before audio.js');
  }

  var CAPTURE_RATE = 16000;
  var FRAME_SAMPLES = 320;
  var SCHEDULE_LEAD_S = 0.03; // re-anchor 30 ms ahead of "now" after an underrun
  var STOP_GATE_MS = 5000; // Stop button: how long to drop speech if no flush follows
  // A stuck cursor (clock jump after sleep) shows as a far-future start time while NOTHING is
  // scheduled. A long queue with sources still scheduled is normal: speech is synthesized
  // faster than it plays, so a whole response can sit in the queue. Never re-anchor over it:
  // that starts new chunks on top of the ones still playing.
  var STALE_CURSOR_S = 5;

  function noop() {}

  function ZordonAudio(opts) {
    opts = opts || {};
    this.P = protocol();
    this.send = opts.send || noop; // function(messageObject) -> boolean
    this.workletUrl = opts.workletUrl || 'worklet.js';
    this.onFrame = opts.onFrame || noop; // function(peakInt16)
    this.onPlaybackChange = opts.onPlaybackChange || noop; // function(active)
    this.onCaptureChange = opts.onCaptureChange || noop; // function(active)
    this.onPlaybackBlocked = opts.onPlaybackBlocked || noop; // function(contextState)
    this.onCaptureEnded = opts.onCaptureEnded || noop; // function(reason)
    this.log = opts.log || noop;

    this.ctx = null;
    this.gain = null;
    this.sink = null;
    this.workletLoaded = false;
    this.stream = null;
    this.source = null;
    this.worklet = null;

    this.callActive = false;
    this.micMuted = false;
    this.paused = false; // page hidden
    this.speakerMuted = false;
    this.seq = 0;

    this.nextStartTime = 0;
    this.scheduled = new Set();
    this.lastFlushGeneration = 0;
    this.currentSentenceId = null;
    this.blockedReported = false;
    // Sentence ids whose final chunk was scheduled (so a flush can tell which later
    // sentences never finished).
    this.finished = new Set();
    // Local Stop: drop speech until the agent's own flush (a new generation) or hello
    // arrives, at most STOP_GATE_MS, so playback does not resume mid-sentence.
    this.stopGateUntil = 0;

    this.stats = {
      framesSent: 0,
      chunksPlayed: 0,
      chunksDropped: 0,
      chunksBlocked: 0,
      underruns: 0,
      lastFlushMs: null,
      flushes: 0,
    };
  }

  // ---- context ---------------------------------------------------------------

  ZordonAudio.prototype.hasAudioContext = function () {
    return typeof root.AudioContext === 'function' || typeof root.webkitAudioContext === 'function';
  };

  // The one place an AudioContext is created, so every context gets the state
  // handler that re-arms the "tap to enable audio" notice. Returns false when
  // Web Audio is unavailable or construction failed.
  ZordonAudio.prototype._createContext = function () {
    var self = this;
    if (self.ctx) return true;
    var Ctor = root.AudioContext || root.webkitAudioContext;
    if (!Ctor) return false;
    try {
      self.ctx = new Ctor({ latencyHint: 'interactive' }); // no sampleRate: device default
      self.gain = self.ctx.createGain();
      self.gain.gain.value = self.speakerMuted ? 0 : 1;
      self.gain.connect(self.ctx.destination);
    } catch (_) {
      self.ctx = null;
      self.gain = null;
      return false;
    }
    self.ctx.onstatechange = function () {
      self.log('audio context ' + self.ctx.state);
      if (self.ctx.state === 'running') self.blockedReported = false;
    };
    return true;
  };

  // Create or resume the AudioContext. Call from inside a user gesture; safe to
  // call again from any later gesture (recovers from 'suspended'/'interrupted').
  ZordonAudio.prototype.unlock = function () {
    var self = this;
    if (!self._createContext()) {
      return Promise.reject(new Error('Web Audio is not available in this browser'));
    }
    var p = self.ctx.state !== 'running' ? self.ctx.resume() : Promise.resolve();
    p = p.then(function () {
      if (self.ctx.state === 'running') self.blockedReported = false;
    });
    return p.then(function () {
      // Play one silent sample inside the gesture so iOS treats output as unlocked.
      try {
        var src = self.ctx.createBufferSource();
        src.buffer = self.ctx.createBuffer(1, 1, self.ctx.sampleRate);
        src.connect(self.ctx.destination);
        src.start(0);
      } catch (_) {
        /* ignore */
      }
      return self.ctx.state;
    });
  };

  Object.defineProperty(ZordonAudio.prototype, 'contextState', {
    get: function () {
      return this.ctx ? this.ctx.state : 'none';
    },
  });

  Object.defineProperty(ZordonAudio.prototype, 'contextSampleRate', {
    get: function () {
      return this.ctx ? this.ctx.sampleRate : null;
    },
  });

  // ---- capture -----------------------------------------------------------------

  // Start a call: unlock the context, load the worklet, open the microphone.
  // Must run inside the Talk tap. Resolves when frames are flowing.
  ZordonAudio.prototype.startCall = function () {
    var self = this;
    if (!root.navigator || !root.navigator.mediaDevices || !root.navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error('Microphone access needs HTTPS (or localhost) and a modern browser'));
    }
    return self
      .unlock()
      .then(function () {
        if (!self.ctx.audioWorklet) throw new Error('AudioWorklet is not supported in this browser');
        if (self.workletLoaded) return null;
        return self.ctx.audioWorklet.addModule(self.workletUrl).then(function () {
          self.workletLoaded = true;
        });
      })
      .then(function () {
        if (self.stream) return self.stream;
        return root.navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            channelCount: 1,
          },
          video: false,
        });
      })
      .then(function (stream) {
        if (!self.stream) {
          self.stream = stream;
          var track = stream.getAudioTracks()[0];
          if (track) {
            track.onended = function () {
              self.log('mic track ended');
              self.endCall();
              self.onCaptureEnded('microphone stopped');
            };
            try {
              self.log('mic settings ' + JSON.stringify(track.getSettings()));
            } catch (_) {
              /* ignore */
            }
          }
          self.source = self.ctx.createMediaStreamSource(stream);
          self.worklet = new root.AudioWorkletNode(self.ctx, 'pcm16-downsampler', {
            numberOfInputs: 1,
            numberOfOutputs: 1,
            channelCount: 1,
            channelCountMode: 'explicit',
            processorOptions: { targetRate: CAPTURE_RATE, frameSamples: FRAME_SAMPLES },
          });
          self.worklet.port.onmessage = function (e) {
            self._onFrame(e.data);
          };
          self.source.connect(self.worklet);
          // A silent sink keeps every engine pulling the worklet; its output is unused.
          self.sink = self.ctx.createGain();
          self.sink.gain.value = 0;
          self.worklet.connect(self.sink);
          self.sink.connect(self.ctx.destination);
        }
        self.seq = 0;
        self.callActive = true;
        self._applyCaptureGate();
        self.onCaptureChange(true);
        return true;
      });
  };

  ZordonAudio.prototype.endCall = function () {
    var wasActive = this.callActive;
    this.callActive = false;
    if (this.stream) {
      this.stream.getTracks().forEach(function (t) {
        t.onended = null;
        try {
          t.stop();
        } catch (_) {
          /* ignore */
        }
      });
      this.stream = null;
    }
    if (this.source) {
      try {
        this.source.disconnect();
      } catch (_) {
        /* ignore */
      }
      this.source = null;
    }
    if (this.worklet) {
      this.worklet.port.onmessage = null;
      try {
        this.worklet.disconnect();
      } catch (_) {
        /* ignore */
      }
      this.worklet = null;
    }
    if (this.sink) {
      try {
        this.sink.disconnect();
      } catch (_) {
        /* ignore */
      }
      this.sink = null;
    }
    if (wasActive) this.onCaptureChange(false);
    return Promise.resolve();
  };

  ZordonAudio.prototype._applyCaptureGate = function () {
    var gated = !this.callActive || this.micMuted || this.paused;
    if (this.stream) {
      this.stream.getAudioTracks().forEach(function (t) {
        t.enabled = !gated;
      });
    }
    if (this.worklet) this.worklet.port.postMessage({ muted: gated });
  };

  ZordonAudio.prototype.setMicMuted = function (muted) {
    this.micMuted = !!muted;
    this._applyCaptureGate();
  };

  // Page hidden: stop producing frames but keep the mic permission and the socket.
  ZordonAudio.prototype.pauseCapture = function () {
    this.paused = true;
    this._applyCaptureGate();
  };

  ZordonAudio.prototype.resumeCapture = function () {
    var self = this;
    self.paused = false;
    self._applyCaptureGate();
    if (self.ctx && self.ctx.state !== 'running') {
      // Allowed without a gesture on Android Chrome; iOS may need the next tap.
      return self.ctx.resume().catch(function () {
        return self.ctx.state;
      });
    }
    return Promise.resolve(self.contextState);
  };

  Object.defineProperty(ZordonAudio.prototype, 'captureActive', {
    get: function () {
      return this.callActive && !this.micMuted && !this.paused;
    },
  });

  ZordonAudio.prototype._onFrame = function (data) {
    if (!data || !data.pcm16) return;
    if (!this.callActive || this.micMuted || this.paused) return;
    var b64 = this.P.int16ToBase64(data.pcm16);
    var sent = this.send(this.P.buildAudio(b64, this.seq));
    if (sent !== false) {
      this.seq++;
      this.stats.framesSent++;
    }
    this.onFrame(data.peak || 0);
  };

  // ---- playback ----------------------------------------------------------------

  ZordonAudio.prototype.setSpeakerMuted = function (muted) {
    this.speakerMuted = !!muted;
    if (this.gain) this.gain.gain.value = this.speakerMuted ? 0 : 1;
  };

  Object.defineProperty(ZordonAudio.prototype, 'playbackActive', {
    get: function () {
      return this.scheduled.size > 0;
    },
  });

  Object.defineProperty(ZordonAudio.prototype, 'stopGateActive', {
    get: function () {
      return this.stopGateUntil > Date.now();
    },
  });

  // Schedule one "speech" message. Returns true when it was queued for playback.
  ZordonAudio.prototype.onSpeech = function (msg) {
    if (msg.generation < this.lastFlushGeneration || this.stopGateActive) {
      this.stats.chunksDropped++;
      return false;
    }
    // Create a context so it is ready; it stays suspended until a gesture.
    if (!this._createContext()) return false;
    if (this.ctx.state !== 'running') {
      this.stats.chunksBlocked++;
      if (!this.blockedReported) {
        this.blockedReported = true;
        this.onPlaybackBlocked(this.ctx.state);
      }
      return false;
    }
    var i16 = this.P.base64ToInt16(msg.pcm);
    if (i16.length === 0) return false;
    var f32 = this.P.int16ToFloat32(i16);
    var buf = this.ctx.createBuffer(1, f32.length, msg.sample_rate);
    buf.getChannelData(0).set(f32);
    var src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    var now = this.ctx.currentTime;
    if (this.nextStartTime < now + 0.01) {
      if (this.scheduled.size > 0) this.stats.underruns++;
      this.nextStartTime = now + SCHEDULE_LEAD_S;
    }
    if (this.scheduled.size === 0 && this.nextStartTime - now > STALE_CURSOR_S) {
      // Nothing is playing yet the cursor is far ahead: a clock jump. Re-anchor.
      this.nextStartTime = now + SCHEDULE_LEAD_S;
    }
    src.start(this.nextStartTime); // start() is once-only per node
    this.nextStartTime += buf.duration;
    src.zordonSentence = msg.sentence_id;
    src.zordonFinal = !!msg.final;
    var self = this;
    var wasActive = this.scheduled.size > 0;
    this.scheduled.add(src);
    this.currentSentenceId = msg.sentence_id;
    if (msg.final) this.finished.add(msg.sentence_id);
    this.stats.chunksPlayed++;
    src.onended = function () {
      self.scheduled.delete(src);
      try {
        src.disconnect();
      } catch (_) {
        /* ignore */
      }
      if (self.scheduled.size === 0) self.onPlaybackChange(false);
    };
    if (!wasActive) this.onPlaybackChange(true);
    return true;
  };

  // Barge-in. Stops everything scheduled, resets the cursor and records the
  // generation so late chunks are dropped. Returns the sentence ids that were
  // cut off (still scheduled at the time) and how long the flush took.
  ZordonAudio.prototype.flush = function (generation) {
    var t0 = typeof performance !== 'undefined' ? performance.now() : Date.now();
    if (typeof generation === 'number') {
      if (generation > this.lastFlushGeneration) this.lastFlushGeneration = generation;
      // The agent's own flush arrived: whatever follows is a new generation.
      this.stopGateUntil = 0;
    }
    var ids = [];
    var seen = {};
    this.scheduled.forEach(function (src) {
      try {
        src.stop(0);
      } catch (_) {
        /* already ended */
      }
      try {
        src.disconnect();
      } catch (_) {
        /* ignore */
      }
      src.onended = null;
      var id = src.zordonSentence;
      if (typeof id === 'number' && !seen[id]) {
        seen[id] = true;
        ids.push(id);
      }
    });
    var hadAudio = this.scheduled.size > 0;
    this.scheduled.clear();
    this.nextStartTime = 0;
    this.stats.flushes++;
    var ms = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0;
    this.stats.lastFlushMs = ms;
    if (hadAudio) this.onPlaybackChange(false);
    return { interrupted: ids, ms: ms, hadAudio: hadAudio };
  };

  // The Stop button: silence now and keep dropping speech of the current
  // generation until the agent's flush (or hello) arrives, at most STOP_GATE_MS.
  // Returns the same record as flush().
  ZordonAudio.prototype.stopLocal = function (holdMs) {
    var res = this.flush();
    this.stopGateUntil = Date.now() + (typeof holdMs === 'number' ? holdMs : STOP_GATE_MS);
    return res;
  };

  // True when the final chunk of this sentence was scheduled.
  ZordonAudio.prototype.sentenceFinished = function (sentenceId) {
    return this.finished.has(sentenceId);
  };

  // A new connection (hello): the agent's generation counter may have restarted,
  // so forget the old gate; sentence ids may repeat, so forget them too.
  ZordonAudio.prototype.resetGeneration = function () {
    this.lastFlushGeneration = 0;
    this.stopGateUntil = 0;
    this.finished.clear();
  };

  // Time of audio still queued, in seconds (for the UI).
  ZordonAudio.prototype.queuedSeconds = function () {
    if (!this.ctx || this.scheduled.size === 0) return 0;
    return Math.max(0, this.nextStartTime - this.ctx.currentTime);
  };

  ZordonAudio.CAPTURE_RATE = CAPTURE_RATE;
  ZordonAudio.FRAME_SAMPLES = FRAME_SAMPLES;
  return ZordonAudio;
});
