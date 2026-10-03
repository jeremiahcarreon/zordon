// Node test for zordon/web/protocol.js: the client mirror must agree with
// zordon/transport/protocol.py on the closed command set and the message type
// lists, refuse unknown commands, and accept a well-formed sample of every
// agent -> client message type. Run: node tests/web/protocol_test.cjs
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const ROOT = path.join(__dirname, '..', '..');
const P = require(path.join(ROOT, 'zordon', 'web', 'protocol.js'));
const PY = fs.readFileSync(path.join(ROOT, 'zordon', 'transport', 'protocol.py'), 'utf8');

// Pull a `NAME = ( "a", "b", ... )` tuple of string literals out of the Python source.
function pyTuple(name) {
  const m = PY.match(new RegExp('^' + name + '\\s*=\\s*\\(([\\s\\S]*?)\\)', 'm'));
  assert.ok(m, `${name} not found in protocol.py`);
  const items = [];
  const re = /"([^"]+)"/g;
  let x;
  while ((x = re.exec(m[1])) !== null) items.push(x[1]);
  assert.ok(items.length > 0, `${name} is empty`);
  return items;
}

// ---- lists agree with Python ---------------------------------------------------
assert.deepStrictEqual(P.COMMANDS, pyTuple('COMMANDS'), 'COMMANDS differs from protocol.py');
assert.deepStrictEqual(P.OUTBOUND_TYPES, pyTuple('OUTBOUND_TYPES'), 'OUTBOUND_TYPES differs');
assert.deepStrictEqual(P.INBOUND_TYPES, pyTuple('INBOUND_TYPES'), 'INBOUND_TYPES differs');
const pyVersion = PY.match(/^PROTOCOL_VERSION\s*=\s*(\d+)/m);
assert.ok(pyVersion, 'PROTOCOL_VERSION not found');
assert.strictEqual(P.PROTOCOL_VERSION, Number(pyVersion[1]));
console.log(`ok lists agree with protocol.py (${P.COMMANDS.length} commands, ${P.OUTBOUND_TYPES.length} outbound types)`);

// ---- buildCommand ---------------------------------------------------------------
for (const name of P.COMMANDS) {
  const m = P.buildCommand(name, {});
  assert.deepStrictEqual(m, { type: 'command', name, args: {} });
}
assert.deepStrictEqual(P.buildCommand('focus', { session_id: 'abc' }), {
  type: 'command',
  name: 'focus',
  args: { session_id: 'abc' },
});
assert.deepStrictEqual(P.buildCommand('stop').args, {});
assert.deepStrictEqual(P.buildCommand('delete', { session_id: 's', confirm: true, extra: undefined }).args, {
  session_id: 's',
  confirm: true,
});
for (const bad of ['rm_rf', 'accept_trust', 'client_background', '', 'APPROVE', null, undefined, 42]) {
  assert.throws(() => P.buildCommand(bad, {}), /unknown command/, `buildCommand accepted ${String(bad)}`);
}
assert.throws(() => P.buildCommand('focus', 'not-an-object'), /args must be an object/);
console.log('ok buildCommand accepts the closed set and refuses everything else');

// ---- other builders ---------------------------------------------------------------
assert.deepStrictEqual(P.buildText('hello'), { type: 'text', text: 'hello' });
assert.throws(() => P.buildText(''), /1\.\.8000/);
assert.throws(() => P.buildText('x'.repeat(8001)), /1\.\.8000/);
for (const a of P.CALL_ACTIONS) assert.deepStrictEqual(P.buildCall(a), { type: 'call', action: a });
assert.throws(() => P.buildCall('hangup'), /unknown call action/);
assert.deepStrictEqual(P.buildFlushAck(7), { type: 'flush_ack', generation: 7 });
assert.deepStrictEqual(P.buildPing(), { type: 'ping' });
assert.deepStrictEqual(P.buildPing(12.5), { type: 'ping', ts: 12.5 });
assert.deepStrictEqual(P.buildAudio('AAAA', 3), { type: 'audio', pcm: 'AAAA', seq: 3 });
console.log('ok text/call/flush_ack/ping/audio builders');

// ---- validateInbound: one well-formed sample per outbound type --------------------
const SAMPLES = {
  hello: {
    type: 'hello',
    protocol: 1,
    version: '0.1.0',
    focused_session: null,
    verbosity: 'minimal',
    tool_chatter: false,
    providers: { stt: 'faster-whisper', tts: 'kokoro' },
    tts_sample_rate: 24000,
    tunnel_url: null,
  },
  sessions: {
    type: 'sessions',
    sessions: [
      {
        session_id: 'abc',
        directory: '/home/u/proj',
        title: 'proj',
        last_active: 1.0,
        attached: true,
        running: true,
        state: 'idle',
        permission_mode: 'default',
        focused: true,
      },
    ],
  },
  speech: { type: 'speech', sentence_id: 1, seq: 2, generation: 3, sample_rate: 24000, pcm: 'AAAA', final: true },
  flush: { type: 'flush', generation: 4 },
  transcript: {
    type: 'transcript',
    row_id: 1,
    session_id: 'abc',
    kind: 'spoken',
    text: 'Done, tests pass.',
    raw_lines: ['tests pass'],
    ts: 1.5,
    sentence_id: 1,
    spoken: true,
  },
  state: { type: 'state', session_id: 'abc', state: 'working', detail: 'producing output', ts: 2.0 },
  prompt: {
    type: 'prompt',
    prompt_id: 1,
    session_id: 'abc',
    kind: 'permission',
    title: 'Bash command',
    options: ['Yes', 'Yes, and always allow access to /tmp from this project', 'No'],
    raw_lines: ['Do you want to proceed?'],
    cleared: false,
  },
  settings: {
    type: 'settings',
    verbosity: 'normal',
    tool_chatter: true,
    muted: false,
    providers: { stt: 'openai' },
    permission_mode: 'plan',
  },
  error: { type: 'error', message: 'nope', code: 'bad_command' },
  pong: { type: 'pong', ts: 3.0 },
  tunnel: { type: 'tunnel', url: 'https://x.trycloudflare.com', qr_svg: '<svg></svg>' },
  update: { type: 'update', current: '0.1.0', latest: '0.2.0', command: 'zordon update', auto: false, notes_url: 'https://example.com/commits/main' },
  health: {
    type: 'health',
    status: 'fail',
    ts: 5.0,
    items: [
      { key: 'tmux', label: 'tmux', status: 'ok', detail: 'server running', fix: '' },
      { key: 'vad', label: 'voice detection', status: 'fail', detail: 'voice input is off: Silero VAD model missing', fix: 'zordon doctor --download' },
    ],
  },
};
for (const t of P.OUTBOUND_TYPES) {
  assert.ok(SAMPLES[t], `no sample for outbound type ${t}`);
  assert.strictEqual(P.validateInbound(SAMPLES[t]), SAMPLES[t], `validateInbound rejected ${t}`);
  assert.strictEqual(P.parseInbound(JSON.stringify(SAMPLES[t])).type, t);
}
// Optional fields may be absent entirely.
assert.ok(P.validateInbound({ type: 'pong' }));
assert.ok(P.validateInbound({ type: 'tunnel', url: null }));
assert.ok(P.validateInbound({ type: 'error', message: 'x' }));
assert.ok(P.validateInbound({ type: 'flush', generation: 0 }));
assert.ok(P.validateInbound({ type: 'update', current: '1', latest: '2', command: 'zordon update' }));
assert.ok(P.validateInbound({ type: 'health', status: 'ok', ts: 1, items: [] }));
assert.ok(P.validateInbound({ type: 'health', status: 'ok', ts: 1, items: [{ key: 'tmux', label: 'tmux', status: 'ok' }] }));
console.log('ok validateInbound accepts every outbound type');

// ---- validateInbound: rejections ---------------------------------------------------
const BAD = [
  null,
  42,
  'speech',
  [],
  {},
  { type: 'nope' },
  { type: 'audio', pcm: 'AAAA', seq: 0 }, // inbound-only type never arrives from the agent
  { type: 'command', name: 'stop', args: {} },
  { type: 'speech', sentence_id: 1, seq: 2, generation: 3, sample_rate: 24000 }, // pcm missing
  { type: 'speech', sentence_id: 1, seq: 2, generation: '3', sample_rate: 24000, pcm: 'AAAA' },
  { type: 'speech', sentence_id: 1, seq: 2, generation: 3, sample_rate: 0, pcm: 'AAAA' },
  { type: 'flush' },
  { type: 'flush', generation: 1.5 },
  { type: 'transcript', row_id: 1, session_id: 'a', kind: 'weird', text: '', raw_lines: [], ts: 1 },
  { type: 'transcript', row_id: 1, session_id: 'a', kind: 'spoken', text: '', raw_lines: [1], ts: 1 },
  { type: 'prompt', prompt_id: 1, session_id: 'a', kind: 'ask', title: '', options: [], raw_lines: [] },
  { type: 'prompt', prompt_id: 1, session_id: 'a', kind: 'plan', title: '', options: 'x', raw_lines: [] },
  { type: 'state', session_id: 'a', state: 'idle' }, // ts missing
  { type: 'sessions', sessions: [{ session_id: 'x' }] },
  { type: 'sessions', sessions: 'none' },
  { type: 'hello', protocol: 1 },
  { type: 'settings', verbosity: 'minimal', tool_chatter: 'yes', muted: false, providers: {} },
  { type: 'error', message: 7 },
  { type: 'tunnel', url: 5 },
  { type: 'update', current: '1', latest: '2' }, // command missing
  { type: 'update', current: '1', latest: '2', command: 'x', auto: 'yes' },
  { type: 'health', status: 'meh', ts: 1, items: [] },
  { type: 'health', status: 'ok', items: [] }, // ts missing
  { type: 'health', status: 'ok', ts: 1, items: [{ key: 'tmux', label: 'tmux', status: 'broken' }] },
  { type: 'health', status: 'ok', ts: 1, items: [{ key: 'tmux', status: 'ok' }] }, // label missing
  { type: 'health', status: 'ok', ts: 1, items: 'none' },
];
for (const b of BAD) assert.strictEqual(P.validateInbound(b), null, `accepted bad message ${JSON.stringify(b)}`);
assert.strictEqual(P.parseInbound('{not json'), null);
assert.strictEqual(P.parseInbound(''), null);
assert.strictEqual(P.parseInbound('x'.repeat(P.MAX_INBOUND_BYTES * 4 + 1)), null);
console.log(`ok validateInbound rejects ${BAD.length} malformed shapes`);

// ---- unsafe option labels ------------------------------------------------------------
const UNSAFE = [
  'Yes, and always allow access to /tmp/x from this project',
  'Yes, and switch to auto mode · auto mode handles these prompts for you',
  'Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)',
  'Yes, and use auto mode',
  "Yes, don't ask again",
  'Allow all',
];
const SAFE = ['Yes', 'No', 'Yes, manually approve edits', 'Tell Claude what to change', 'Tabs', 'Chat about this'];
for (const s of UNSAFE) assert.strictEqual(P.isUnsafeOption(s), true, `should be unsafe: ${s}`);
for (const s of SAFE) assert.strictEqual(P.isUnsafeOption(s), false, `should be safe: ${s}`);
assert.strictEqual(P.isUnsafeOption(undefined), false);
assert.deepStrictEqual(P.PERMISSION_MODES, ['default', 'acceptEdits', 'plan', 'auto', 'dontAsk']);
console.log('ok unsafe option detection and permission mode list');

// ---- base64 <-> int16 --------------------------------------------------------------------
const samples = new Int16Array(P.FRAME_SAMPLES);
for (let i = 0; i < samples.length; i++) samples[i] = Math.round(32767 * Math.sin(i / 7)) - (i % 3);
samples[0] = -32768;
samples[1] = 32767;
samples[2] = -1;
samples[3] = 0;
const b64 = P.int16ToBase64(samples);
assert.strictEqual(typeof b64, 'string');
assert.strictEqual(Buffer.from(b64, 'base64').length, P.FRAME_BYTES, 'a frame is 640 bytes');
assert.ok(/^[A-Za-z0-9+/]+=*$/.test(b64), 'base64 alphabet');
const back = P.base64ToInt16(b64);
assert.strictEqual(back.length, samples.length);
for (let i = 0; i < samples.length; i++) assert.strictEqual(back[i], samples[i], `sample ${i}`);
// Little-endian on the wire, matching the agent's int16 LE expectation.
assert.deepStrictEqual(Array.from(Buffer.from(P.int16ToBase64(new Int16Array([1, -2])), 'base64')), [1, 0, 0xfe, 0xff]);
// ArrayBuffer input works too (what the worklet transfers).
assert.strictEqual(P.int16ToBase64(samples.buffer), b64);
// Agree with Node's own encoder.
assert.strictEqual(b64, Buffer.from(samples.buffer).toString('base64'));
const f32 = P.int16ToFloat32(new Int16Array([-32768, 0, 16384]));
assert.deepStrictEqual(Array.from(f32), [-1, 0, 0.5]);
console.log('ok base64 helpers round-trip int16 little-endian');

console.log('protocol_test: all passed');
