// Headless behaviour tests for zordon/web/app.js + audio.js: a minimal DOM, a fake
// WebSocket and a fake Web Audio stack, then agent -> client frames are fed in and
// the state/DOM effect is asserted. Run: node tests/web/app_test.cjs
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ROOT = path.join(__dirname, '..', '..');
const WEB = path.join(ROOT, 'zordon', 'web');

// ---- minimal DOM ---------------------------------------------------------------
class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  toggle(c, force) { const on = force === undefined ? !this.set.has(c) : !!force; on ? this.set.add(c) : this.set.delete(c); return on; }
  contains(c) { return this.set.has(c); }
}
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null; this.attrs = {};
    this.classList = new ClassList(); this.dataset = {}; this.style = {}; this.listeners = {};
    this._text = ''; this.disabled = false; this.checked = false; this._value = '';
    this.scrollTop = 0; this.scrollHeight = 1000; this.clientHeight = 500; this.returnValue = '';
    this.files = []; this.placeholder = ''; this.title = ''; this.open = false;
  }
  get value() {
    if (this.tagName === 'SELECT') {
      const opts = this.children.filter((c) => c.tagName === 'OPTION');
      const sel = opts.find((o) => o._selected);
      if (sel) return sel.attrs.value;
      return opts.length ? opts[0].attrs.value : '';
    }
    return this._value;
  }
  set value(v) {
    if (this.tagName === 'SELECT') {
      const opts = this.children.filter((c) => c.tagName === 'OPTION');
      opts.forEach((o) => { o._selected = o.attrs.value === String(v); });
      return;
    }
    this._value = String(v);
  }
  get className() { return [...this.classList.set].join(' '); }
  set className(v) { this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get href() { return this.attrs.href; }
  set href(v) { this.attrs.href = String(v); }
  get src() { return this.attrs.src; }
  set src(v) { this.attrs.src = String(v); }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i === -1) throw new Error('not a child'); this.children.splice(i, 1); c.parentNode = null; return c; }
  replaceChild(n, old) { const i = this.children.indexOf(old); if (i === -1) throw new Error('not a child'); if (n.parentNode) n.parentNode.removeChild(n); this.children[i] = n; n.parentNode = this; old.parentNode = null; return old; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') document._ids[v] = this; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  dispatch(t, ev) { ev = ev || {}; ev.target = ev.target || this; ev.preventDefault = ev.preventDefault || (() => {}); (this.listeners[t] || []).forEach((f) => f(ev)); }
  click() { this.dispatch('click'); }
  focus() {}
  querySelectorAll(sel) {
    const out = [];
    const tag = sel.toUpperCase();
    const walk = (n) => n.children.forEach((c) => { if (c instanceof Element) { if (c.tagName === tag) out.push(c); walk(c); } });
    walk(this);
    return out;
  }
  showModal() { this.open = true; }
  close() { this.open = false; if (this.onclose) this.onclose(); }
}
class TextNode { constructor(t) { this.text = t; this.parentNode = null; this.children = []; } get textContent() { return this.text; } }
const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const document = {
  _ids: {},
  body: new Element('body'),
  visibilityState: 'visible',
  createElement: (t) => new Element(t),
  createTextNode: (t) => new TextNode(t),
  getElementById(id) { if (!this._ids[id]) { const e = new Element('div'); e.setAttribute('id', id); } return this._ids[id]; },
  addEventListener(t, fn) { (this._l = this._l || {})[t] = (this._l[t] || []).concat(fn); },
  dispatch(t, ev) { ((this._l || {})[t] || []).forEach((f) => f(ev || {})); },
};
for (const m of html.matchAll(/<([a-z0-9]+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const e = new Element(m[1]);
  e.setAttribute('id', m[2]);
  if (/\bhidden\b/.test(m[0])) e.setAttribute('hidden', '');
}
for (const v of ['minimal', 'normal', 'technical']) {
  const o = new Element('option'); o.setAttribute('value', v); o._text = v; document.getElementById('set-verbosity').appendChild(o);
}

// ---- fake WebSocket ---------------------------------------------------------
const sent = [];
class FakeWS {
  constructor(url) { this.url = url; this.readyState = 0; FakeWS.last = this; setTimeout(() => this._open(), 0); }
  _open() { this.readyState = 1; this.onopen && this.onopen(); }
  send(s) { if (this.readyState !== 1) throw new Error('send on closed socket'); sent.push(JSON.parse(s)); }
  close() { if (this.readyState === 3) return; this.readyState = 3; this.onclose && this.onclose({}); }
  receive(frame) { this.onmessage({ data: JSON.stringify(frame) }); }
}
FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;

// ---- fake Web Audio (context starts running: a gesture already happened) -------
const audioLog = { started: [], stopped: 0, contexts: [] };
class FakeNode {
  constructor(ctx) { this.ctx = ctx; this.gain = { value: 1 }; this.port = { postMessage() {}, onmessage: null }; this.buffer = null; }
  connect() {}
  disconnect() {}
  start(t) { audioLog.started.push({ node: this, at: t, sentence: this.zordonSentence }); }
  stop() { audioLog.stopped++; }
}
class FakeCtx {
  constructor() {
    audioLog.contexts.push(this);
    this.state = 'running'; this.sampleRate = 48000; this.currentTime = 0;
    this.audioWorklet = { addModule: () => Promise.resolve() };
    this.destination = {};
  }
  resume() { this.state = 'running'; return Promise.resolve(); }
  createGain() { return new FakeNode(this); }
  createBufferSource() { return new FakeNode(this); }
  createBuffer(ch, n, rate) { const d = new Float32Array(n); return { duration: n / rate, getChannelData: () => d, length: n, sampleRate: rate }; }
  createMediaStreamSource() { return new FakeNode(this); }
}
class FakeWorkletNode extends FakeNode {}

const fetches = [];
const win = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, performance, Date, Math, JSON, Object, Array, String, Number, Boolean, Error, encodeURIComponent, decodeURIComponent, Set, Map, Promise, RegExp, parseInt, parseFloat, isFinite, isNaN,
  Int16Array, Float32Array, Uint8Array, ArrayBuffer, btoa: (s) => Buffer.from(s, 'binary').toString('base64'), atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  location: { protocol: 'http:', host: '127.0.0.1:8765' },
  isSecureContext: true,
  WebSocket: FakeWS,
  AudioContext: FakeCtx,
  AudioWorkletNode: FakeWorkletNode,
  navigator: { vibrate: null, mediaDevices: { getUserMedia: () => Promise.reject(new Error('no mic')) } },
  fetch: (url, opts) => { fetches.push({ url, opts }); return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, path: '/p/a.png' }) }); },
  FormData: class { constructor() { this.parts = []; } append(k, v, n) { this.parts.push([k, v, n]); } },
  addEventListener(t, fn) { (this._l = this._l || {})[t] = (this._l[t] || []).concat(fn); },
  prompt: () => 'typed feedback', confirm: () => true,
};
win.window = win; win.globalThis = win; win.document = document;
const ctx = vm.createContext(win);
for (const f of ['protocol.js', 'audio.js', 'app.js']) vm.runInContext(fs.readFileSync(path.join(WEB, f), 'utf8'), ctx, { filename: f });

const $ = (id) => document.getElementById(id);
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 5));
const recv = (frame) => FakeWS.last.receive(frame);
const cmds = (name) => sent.filter((m) => m.type === 'command' && m.name === name);
const rows = () => $('rows').children;
const rowBySentence = (sid) => rows().find((r) => r.dataset.sentenceId === String(sid));
const pcm = Buffer.alloc(640).toString('base64');
const now = () => Date.now() / 1000;

const hello = (over) => Object.assign({ type: 'hello', protocol: 1, version: '0.1.0', focused_session: 's1', verbosity: 'minimal', tool_chatter: false, providers: { stt: 'faster-whisper', tts: 'kokoro' }, tts_sample_rate: 24000, tunnel_url: null }, over || {});
const sessions = (focusedId) => ({
  type: 'sessions',
  sessions: [
    { session_id: 's1', directory: '/home/u/proj', title: 'proj', last_active: now(), attached: true, running: true, state: 'idle', permission_mode: 'default', focused: focusedId === 's1' },
    { session_id: 's2', directory: '/home/u/other', title: '', last_active: null, attached: false, running: false, state: 'detached', permission_mode: 'plan', focused: focusedId === 's2' },
  ],
});
const spoken = (rowId, sid, text) => ({ type: 'transcript', row_id: rowId, session_id: 's1', kind: 'spoken', text: text || 'row ' + rowId, raw_lines: [], ts: now(), sentence_id: sid, spoken: null });
const speech = (sid, gen, final) => ({ type: 'speech', sentence_id: sid, seq: 0, generation: gen, sample_rate: 24000, pcm, final: !!final });

let passed = 0;
function ok(cond, what) { assert.ok(cond, what); passed++; console.log('ok ' + what); }

(async () => {
  await tick();
  recv(hello());
  recv(sessions('s1'));

  // ---- WEB-1: a new hello resets the flush gate ----
  recv({ type: 'flush', generation: 6 });
  recv(spoken(10, 100));
  recv(speech(100, 3, false));
  ok(audioLog.started.length === 0, 'speech older than the last flush is dropped');
  recv(hello());
  recv(sessions('s1'));
  recv(spoken(11, 1));
  recv(speech(1, 1, false));
  ok(audioLog.started.length === 1, 'after a new hello (agent restart) generation 1 plays again');

  // ---- WEB-8: hello resets row/prompt bookkeeping; new notices do not overwrite old rows ----
  recv({ type: 'transcript', row_id: -1000000001, session_id: '', kind: 'notice', text: 'first notice', raw_lines: [], ts: now() });
  recv(spoken(12, 2, 'later row'));
  recv(hello());
  recv(sessions('s1'));
  ok(rows().length === 0 && $('prompts').children.length === 0, 'hello clears rows and prompt cards (the agent resends the tail)');
  recv(spoken(12, 2, 'later row again'));
  recv({ type: 'transcript', row_id: -1000000001, session_id: '', kind: 'notice', text: 'notice after restart', raw_lines: [], ts: now() });
  ok(rows().map((r) => r.textContent).join('|').indexOf('later row again') < rows().map((r) => r.textContent).join('|').indexOf('notice after restart'), 'reused notice id lands at the bottom, not in place of the old row');

  // ---- WEB-9: agent-wide notices (session_id "") are not "other" ----
  const globalNotice = rows()[rows().length - 1];
  ok(!globalNotice.classList.contains('other'), 'global notice is not dimmed as another session');
  recv({ type: 'transcript', row_id: -5, session_id: 's2', kind: 'notice', text: 'bg notice', raw_lines: [], ts: now() });
  ok(rows()[rows().length - 1].classList.contains('other'), 'a notice for another session still is');

  // ---- WEB-4: a flush marks every unfinished sentence after the interrupted one ----
  recv(hello());
  recv(sessions('s1'));
  recv(spoken(20, 200));
  recv(spoken(21, 201));
  recv(spoken(22, 202));
  recv(spoken(23, 203));
  recv(speech(200, 1, true)); // finished before the barge-in
  audioLog.started[audioLog.started.length - 1].node.onended(); // ... and played out
  recv(speech(201, 1, false)); // playing when the barge-in happened
  // 202 synthesized but its chunks never reached us; 203 never synthesized
  recv({ type: 'flush', generation: 2, sentence_id: 201 });
  ok(!rowBySentence(200).classList.contains('cut'), 'finished sentence before the cut stays spoken');
  ok(rowBySentence(201).classList.contains('cut'), 'interrupted sentence marked cut');
  ok(rowBySentence(202).classList.contains('cut') && rowBySentence(203).classList.contains('cut'), 'later sentences whose chunks never finished marked cut');
  ok(sent.some((m) => m.type === 'flush_ack' && m.generation === 2), 'flush_ack sent');
  // a flush without sentence_id but with audio scheduled still works
  recv(spoken(24, 204));
  recv(spoken(25, 205));
  recv(speech(204, 2, false));
  recv({ type: 'flush', generation: 3 });
  ok(rowBySentence(204).classList.contains('cut') && rowBySentence(205).classList.contains('cut'), 'flush without sentence_id cuts from the scheduled sentence on');

  // ---- WEB-3: Stop gates speech locally until the agent's flush arrives ----
  const before = audioLog.started.length;
  recv(spoken(30, 300));
  recv(speech(300, 3, false));
  ok(audioLog.started.length === before + 1, 'speech of the current generation plays');
  $('btn-stop').click();
  ok(cmds('stop').length === 1, 'Stop sends the stop command');
  recv(speech(300, 3, false));
  recv(speech(300, 3, true));
  ok(audioLog.started.length === before + 1, 'after Stop, chunks of the same generation are dropped (no mid-sentence resume)');
  ok(rowBySentence(300).classList.contains('cut'), 'the stopped sentence is marked cut');
  recv({ type: 'flush', generation: 4 });
  recv(spoken(31, 301));
  recv(speech(301, 4, false));
  ok(audioLog.started.length === before + 2, "the agent's flush lifts the gate and the next generation plays");

  // ---- WEB-5: permission-mode select follows the focused session ----
  recv({ type: 'settings', verbosity: 'minimal', tool_chatter: false, muted: false, providers: {}, permission_mode: 'default' });
  ok($('set-mode').value === 'default', 'settings message sets the mode select');
  recv(sessions('s2'));
  ok($('focus-title').textContent === 'other', 'focus moved to s2');
  ok($('set-mode').value === 'plan', 'mode select shows the newly focused session mode: ' + $('set-mode').value);
  recv(sessions('s1'));
  ok($('set-mode').value === 'default', 'and back again');

  // ---- WEB-10: unmirrored local echo rows join the capped list ----
  recv(hello());
  recv(sessions('s1'));
  $('text').value = 'never mirrored';
  $('composer').dispatch('submit');
  ok(rows().some((r) => /^local-/.test(r.dataset.rowId)), 'local echo row added');
  const settleMs = Number((fs.readFileSync(path.join(WEB, 'app.js'), 'utf8').match(/LOCAL_ECHO_SETTLE_MS = (\d+)/) || [])[1]);
  ok(settleMs > 0 && settleMs <= 5000, 'local echo settle time is bounded');

  // ---- WEB-12: oversize upload never POSTs ----
  const nFetch = fetches.length;
  const nUpload = cmds('upload').length;
  vm.runInContext('window.__files = [{ name: "big.bin", size: 26 * 1024 * 1024 }]', ctx);
  $('file-input').files = vm.runInContext('window.__files', ctx);
  $('file-input').dispatch('change', { target: $('file-input') });
  await tick();
  ok(fetches.length === nFetch && cmds('upload').length === nUpload, 'a >25 MB file is refused client-side without a POST or upload command');
  ok($('toasts').children.some((t) => /larger than 25 MB/.test(t.textContent)), 'and the user is told why');

  // ---- WEB-11: every AudioContext gets onstatechange ----
  ok(audioLog.contexts.length >= 1 && audioLog.contexts.every((c) => typeof c.onstatechange === 'function'), 'AudioContext created by onSpeech has onstatechange');

  console.log(`app_test: all passed (${passed} checks)`);
  process.exit(0);
})().catch((e) => { console.error('app_test failed:', e); process.exit(1); });
