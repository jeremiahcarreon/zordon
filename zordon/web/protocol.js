// Client-side mirror of zordon/transport/protocol.py.
//
// Classic script (no module syntax) so it loads with a plain <script> tag on
// every mobile browser and can also be require()d from the Node tests. It
// publishes one object, window.ZordonProtocol / module.exports.
//
// Rules copied from the Python side:
//   * COMMANDS is the closed set of client commands; buildCommand() refuses
//     anything else, exactly as CommandIn does on the agent.
//   * Inbound (agent -> client) messages are checked by shape in
//     validateInbound(); anything unknown or malformed yields null and is
//     ignored by the app.
//   * Audio frames are 20 ms of 16 kHz mono int16 (320 samples, 640 bytes),
//     base64 encoded in the "pcm" field.

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) module.exports = api;
  root.ZordonProtocol = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var PROTOCOL_VERSION = 1;

  // Keep in step with protocol.COMMANDS (tests/web/protocol_test.cjs compares them).
  var COMMANDS = [
    'list_sessions',
    'focus',
    'start',
    'resume',
    'detach',
    'delete',
    'send_text',
    'approve',
    'deny',
    'plan_approve',
    'plan_revise',
    'plan_deny',
    'answer',
    'stop',
    'mute',
    'unmute',
    'set_verbosity',
    'set_tool_chatter',
    'set_permission_mode',
    'set_provider',
    'repeat',
    'status',
    'upload',
  ];

  var OUTBOUND_TYPES = [
    'hello',
    'sessions',
    'speech',
    'flush',
    'transcript',
    'state',
    'prompt',
    'settings',
    'error',
    'pong',
    'tunnel',
  ];
  var INBOUND_TYPES = ['audio', 'command', 'text', 'call', 'flush_ack', 'ping'];

  var TRANSCRIPT_KINDS = ['spoken', 'user', 'notice', 'raw'];
  var PROMPT_KINDS = ['permission', 'plan', 'question', 'trust'];
  var CALL_ACTIONS = ['start', 'end', 'pause', 'resume'];
  var STATES = [
    'idle',
    'working',
    'awaiting_permission',
    'awaiting_plan_approval',
    'awaiting_question',
    'stalled',
    'detached',
  ];
  var VERBOSITY_LEVELS = ['minimal', 'normal', 'technical'];
  // The only modes the client ever offers. Anything wider is set in Claude
  // Code's own settings file, never from here.
  var PERMISSION_MODES = ['default', 'acceptEdits', 'plan', 'auto', 'dontAsk'];
  var STT_PROVIDERS = ['faster-whisper', 'openai', 'groq'];
  var TTS_PROVIDERS = ['kokoro', 'elevenlabs', 'openai'];

  var CAPTURE_RATE = 16000;
  var FRAME_SAMPLES = 320; // 20 ms
  var FRAME_BYTES = FRAME_SAMPLES * 2;
  var MAX_INBOUND_BYTES = 64 * 1024;

  // Permission / plan menu options that widen permissions. The client never
  // renders a button for these; the agent refuses them by voice as well.
  var UNSAFE_OPTION_PATTERNS = [
    /always allow/i,
    /\balways\b/i,
    /auto[ -]?mode/i,
    /switch to auto/i,
    /accept edits/i,
    /don'?t ask/i,
    /do not ask/i,
    /skip permissions/i,
    /allow all/i,
    /yes to all/i,
    /for this session/i,
  ];

  function isUnsafeOption(label) {
    if (typeof label !== 'string') return false;
    for (var i = 0; i < UNSAFE_OPTION_PATTERNS.length; i++) {
      if (UNSAFE_OPTION_PATTERNS[i].test(label)) return true;
    }
    return false;
  }

  // ---- type helpers ------------------------------------------------------------

  function isObj(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }
  function isStr(v) {
    return typeof v === 'string';
  }
  function isNum(v) {
    return typeof v === 'number' && isFinite(v);
  }
  function isInt(v) {
    return isNum(v) && Math.floor(v) === v;
  }
  function isBool(v) {
    return typeof v === 'boolean';
  }
  function isStrList(v) {
    if (!Array.isArray(v)) return false;
    for (var i = 0; i < v.length; i++) if (!isStr(v[i])) return false;
    return true;
  }
  function isStrDict(v) {
    if (!isObj(v)) return false;
    for (var k in v) if (Object.prototype.hasOwnProperty.call(v, k) && !isStr(v[k])) return false;
    return true;
  }
  function optional(v, pred) {
    return v === undefined || v === null || pred(v);
  }
  function oneOf(list) {
    return function (v) {
      return list.indexOf(v) !== -1;
    };
  }

  // ---- inbound (agent -> client) validation -----------------------------------

  var VALIDATORS = {
    hello: function (m) {
      return (
        isInt(m.protocol) &&
        isStr(m.version) &&
        optional(m.focused_session, isStr) &&
        isStr(m.verbosity) &&
        isBool(m.tool_chatter) &&
        isStrDict(m.providers) &&
        isInt(m.tts_sample_rate) &&
        optional(m.tunnel_url, isStr)
      );
    },
    sessions: function (m) {
      if (!Array.isArray(m.sessions)) return false;
      for (var i = 0; i < m.sessions.length; i++) {
        var s = m.sessions[i];
        if (
          !isObj(s) ||
          !isStr(s.session_id) ||
          !isStr(s.directory) ||
          !isStr(s.title) ||
          !optional(s.last_active, isNum) ||
          !isBool(s.attached) ||
          !isBool(s.running) ||
          !isStr(s.state) ||
          !optional(s.permission_mode, isStr) ||
          !optional(s.focused, isBool)
        ) {
          return false;
        }
      }
      return true;
    },
    speech: function (m) {
      return (
        isInt(m.sentence_id) &&
        isInt(m.seq) &&
        isInt(m.generation) &&
        isInt(m.sample_rate) &&
        m.sample_rate > 0 &&
        isStr(m.pcm) &&
        optional(m.final, isBool)
      );
    },
    flush: function (m) {
      return isInt(m.generation);
    },
    transcript: function (m) {
      return (
        isInt(m.row_id) &&
        isStr(m.session_id) &&
        oneOf(TRANSCRIPT_KINDS)(m.kind) &&
        isStr(m.text) &&
        isStrList(m.raw_lines) &&
        isNum(m.ts) &&
        optional(m.sentence_id, isInt) &&
        optional(m.spoken, isBool)
      );
    },
    state: function (m) {
      return isStr(m.session_id) && isStr(m.state) && optional(m.detail, isStr) && isNum(m.ts);
    },
    prompt: function (m) {
      return (
        isInt(m.prompt_id) &&
        isStr(m.session_id) &&
        oneOf(PROMPT_KINDS)(m.kind) &&
        isStr(m.title) &&
        isStrList(m.options) &&
        isStrList(m.raw_lines) &&
        optional(m.cleared, isBool)
      );
    },
    settings: function (m) {
      return (
        isStr(m.verbosity) &&
        isBool(m.tool_chatter) &&
        isBool(m.muted) &&
        isStrDict(m.providers) &&
        optional(m.permission_mode, isStr)
      );
    },
    error: function (m) {
      return isStr(m.message) && optional(m.code, isStr);
    },
    pong: function (m) {
      return optional(m.ts, isNum);
    },
    tunnel: function (m) {
      return optional(m.url, isStr) && optional(m.qr_svg, isStr);
    },
  };

  // Returns the message when it is a well-formed agent -> client message,
  // null otherwise (unknown type, missing or mistyped field). Callers ignore null.
  function validateInbound(msg) {
    if (!isObj(msg) || !isStr(msg.type)) return null;
    var check = VALIDATORS[msg.type];
    if (!check) return null;
    try {
      return check(msg) ? msg : null;
    } catch (_) {
      return null;
    }
  }

  // Parse a raw WebSocket text frame. Null when unparseable or invalid.
  function parseInbound(raw) {
    if (!isStr(raw) || raw.length > MAX_INBOUND_BYTES * 4) return null;
    var msg;
    try {
      msg = JSON.parse(raw);
    } catch (_) {
      return null;
    }
    return validateInbound(msg);
  }

  // ---- outbound (client -> agent) builders ------------------------------------

  function buildCommand(name, args) {
    if (COMMANDS.indexOf(name) === -1) {
      throw new Error('unknown command: ' + String(name));
    }
    var a = {};
    if (args !== undefined && args !== null) {
      if (!isObj(args)) throw new Error('command args must be an object');
      for (var k in args) {
        if (Object.prototype.hasOwnProperty.call(args, k) && args[k] !== undefined) a[k] = args[k];
      }
    }
    return { type: 'command', name: name, args: a };
  }

  function buildText(text) {
    if (!isStr(text) || text.length === 0 || text.length > 8000) {
      throw new Error('text must be 1..8000 characters');
    }
    return { type: 'text', text: text };
  }

  function buildCall(action) {
    if (CALL_ACTIONS.indexOf(action) === -1) throw new Error('unknown call action: ' + String(action));
    return { type: 'call', action: action };
  }

  function buildAudio(pcmBase64, seq) {
    return { type: 'audio', pcm: pcmBase64, seq: seq >>> 0 };
  }

  function buildFlushAck(generation) {
    return { type: 'flush_ack', generation: generation | 0 };
  }

  function buildPing(ts) {
    var m = { type: 'ping' };
    if (isNum(ts)) m.ts = ts;
    return m;
  }

  // ---- base64 <-> int16 PCM ----------------------------------------------------

  var B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  var B64_REV = null;

  function bytesToBase64(bytes) {
    if (typeof btoa === 'function') {
      var CHUNK = 0x2000;
      var parts = [];
      for (var i = 0; i < bytes.length; i += CHUNK) {
        parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK)));
      }
      return btoa(parts.join(''));
    }
    // Pure JS fallback (Node without btoa, old engines).
    var out = '';
    var n = bytes.length;
    for (var j = 0; j < n; j += 3) {
      var b0 = bytes[j];
      var b1 = j + 1 < n ? bytes[j + 1] : 0;
      var b2 = j + 2 < n ? bytes[j + 2] : 0;
      out += B64[b0 >> 2];
      out += B64[((b0 & 3) << 4) | (b1 >> 4)];
      out += j + 1 < n ? B64[((b1 & 15) << 2) | (b2 >> 6)] : '=';
      out += j + 2 < n ? B64[b2 & 63] : '=';
    }
    return out;
  }

  function base64ToBytes(str) {
    if (typeof atob === 'function') {
      var bin = atob(str);
      var out = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    }
    if (!B64_REV) {
      B64_REV = {};
      for (var c = 0; c < B64.length; c++) B64_REV[B64[c]] = c;
    }
    var clean = str.replace(/[^A-Za-z0-9+/]/g, '');
    var len = Math.floor((clean.length * 3) / 4);
    var bytes = new Uint8Array(len);
    var p = 0;
    for (var j = 0; j < clean.length; j += 4) {
      var e0 = B64_REV[clean[j]] || 0;
      var e1 = B64_REV[clean[j + 1]] || 0;
      var e2 = j + 2 < clean.length ? B64_REV[clean[j + 2]] : 0;
      var e3 = j + 3 < clean.length ? B64_REV[clean[j + 3]] : 0;
      if (p < len) bytes[p++] = (e0 << 2) | (e1 >> 4);
      if (p < len) bytes[p++] = ((e1 & 15) << 4) | (e2 >> 2);
      if (p < len) bytes[p++] = ((e2 & 3) << 6) | e3;
    }
    return bytes;
  }

  // Int16Array (or an ArrayBuffer of int16 LE) -> base64 string.
  function int16ToBase64(samples) {
    var bytes;
    if (samples && samples.buffer && typeof samples.byteLength === 'number') {
      bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
    } else if (samples && typeof samples.byteLength === 'number') {
      bytes = new Uint8Array(samples); // ArrayBuffer (duck-typed: works across realms)
    } else {
      throw new Error('int16ToBase64 expects an Int16Array or ArrayBuffer');
    }
    return bytesToBase64(bytes);
  }

  // base64 string -> Int16Array (little-endian, as the agent sends it).
  function base64ToInt16(str) {
    var bytes = base64ToBytes(str);
    var n = bytes.byteLength >> 1;
    var out = new Int16Array(n);
    // Byte-wise assembly keeps this correct on big-endian hosts and on odd offsets.
    for (var i = 0; i < n; i++) {
      var lo = bytes[2 * i];
      var hi = bytes[2 * i + 1];
      var v = (hi << 8) | lo;
      out[i] = v > 0x7fff ? v - 0x10000 : v;
    }
    return out;
  }

  // Int16 -> Float32 in [-1, 1).
  function int16ToFloat32(i16) {
    var f = new Float32Array(i16.length);
    for (var i = 0; i < i16.length; i++) f[i] = i16[i] / 32768;
    return f;
  }

  return {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    COMMANDS: COMMANDS,
    OUTBOUND_TYPES: OUTBOUND_TYPES,
    INBOUND_TYPES: INBOUND_TYPES,
    TRANSCRIPT_KINDS: TRANSCRIPT_KINDS,
    PROMPT_KINDS: PROMPT_KINDS,
    CALL_ACTIONS: CALL_ACTIONS,
    STATES: STATES,
    VERBOSITY_LEVELS: VERBOSITY_LEVELS,
    PERMISSION_MODES: PERMISSION_MODES,
    STT_PROVIDERS: STT_PROVIDERS,
    TTS_PROVIDERS: TTS_PROVIDERS,
    CAPTURE_RATE: CAPTURE_RATE,
    FRAME_SAMPLES: FRAME_SAMPLES,
    FRAME_BYTES: FRAME_BYTES,
    MAX_INBOUND_BYTES: MAX_INBOUND_BYTES,
    UNSAFE_OPTION_PATTERNS: UNSAFE_OPTION_PATTERNS,
    isUnsafeOption: isUnsafeOption,
    validateInbound: validateInbound,
    parseInbound: parseInbound,
    buildCommand: buildCommand,
    buildText: buildText,
    buildCall: buildCall,
    buildAudio: buildAudio,
    buildFlushAck: buildFlushAck,
    buildPing: buildPing,
    bytesToBase64: bytesToBase64,
    base64ToBytes: base64ToBytes,
    int16ToBase64: int16ToBase64,
    base64ToInt16: base64ToInt16,
    int16ToFloat32: int16ToFloat32,
  };
});
