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

  // ---- WEB-13: agent adapters in the picker ----
  recv(hello({ agents: { 'claude-code': true, codex: false, generic: true }, default_agent: 'claude-code' }));
  recv(sessions('s1'));
  const agentOpts = $('new-agent').children.filter((c) => c.tagName === 'OPTION');
  ok(agentOpts.length === 3 && agentOpts[0].attrs.value === 'claude-code', 'new-session agent select lists the adapters with the default first');
  const codexOpt = agentOpts.find((o) => o.attrs.value === 'codex');
  ok(codexOpt && codexOpt.disabled && /not installed/.test(codexOpt.textContent), 'an uninstalled agent is disabled and labelled');
  ok($('new-agent').value === 'claude-code', 'the default agent is selected');
  ok($('attach-agent').value === 'generic', 'the attach form defaults to the generic adapter');
  $('new-dir').value = '/home/u/proj';
  $('new-session').dispatch('submit');
  const startCmd = cmds('start')[cmds('start').length - 1];
  ok(startCmd && startCmd.args.agent === undefined, 'starting with the default agent sends no agent field');
  $('new-agent').value = 'generic';
  $('new-dir').value = '/home/u/proj';
  $('new-session').dispatch('submit');
  const startGeneric = cmds('start')[cmds('start').length - 1];
  ok(startGeneric && startGeneric.args.agent === 'generic', 'a non-default agent is sent with start');
  $('attach-target').value = 'work:@1.%3';
  $('attach-pane').dispatch('submit');
  const attachCmd = cmds('attach')[cmds('attach').length - 1];
  ok(attachCmd && attachCmd.args.target === 'work:@1.%3' && attachCmd.args.agent === 'generic', 'the attach form sends the attach command with target and agent');
  ok($('session-list').children.some((li) => /Claude Code/.test(li.textContent)), 'session rows carry the agent badge');

  // ---- WEB-14: health strip, failure banner, update banner ----
  ok($('health-strip').hasAttribute('hidden') && $('health-banner').hasAttribute('hidden'), 'no health UI before the first health message');
  const healthMsg = {
    type: 'health',
    status: 'fail',
    ts: now(),
    items: [
      { key: 'tmux', label: 'tmux', status: 'ok', detail: 'server running', fix: '' },
      { key: 'router', label: 'router', status: 'warn', detail: 'keyword router only', fix: 'set providers.keys.anthropic' },
      { key: 'vad', label: 'voice detection', status: 'fail', detail: 'voice input is off: Silero VAD model missing', fix: 'zordon doctor --download' },
    ],
  };
  recv(healthMsg);
  const dots = $('health-strip').children;
  ok(!$('health-strip').hasAttribute('hidden') && dots.length === 3, 'one strip entry per health item');
  ok(dots[0].classList.contains('health-ok') && dots[1].classList.contains('health-warn') && dots[2].classList.contains('health-fail'), 'entries carry the item status as a class (green/amber/red)');
  ok(/voice input is off/i.test(dots[2].getAttribute('title')) && /Fix: zordon doctor --download/.test(dots[2].getAttribute('title')), 'hover text carries detail and fix');
  ok(dots[2].textContent.indexOf('voice detection') !== -1, 'the label is shown next to the dot');
  ok(!$('health-badge').hasAttribute('hidden') && $('health-badge').classList.contains('health-fail') && /1 problem/.test($('health-badge-text').textContent), 'overall badge shows the worst status and a count');
  ok(!$('health-banner').hasAttribute('hidden') && /^Voice input is off: Silero VAD model missing\. Fix: zordon doctor --download$/.test($('health-banner-text').textContent), 'a failed item raises the persistent banner with its fix');
  dots[2].click();
  ok(!$('health-detail').hasAttribute('hidden') && /voice detection: Voice input is off/.test($('health-detail-text').textContent), 'tapping a dot shows its detail');
  $('health-detail-close').click();
  ok($('health-detail').hasAttribute('hidden'), 'and the detail box closes');
  $('health-badge').click();
  ok($('health-strip').hasAttribute('hidden'), 'the badge collapses the strip');
  $('health-badge').click();
  ok(!$('health-strip').hasAttribute('hidden'), 'and expands it again');
  recv(Object.assign({}, healthMsg, { status: 'ok', items: healthMsg.items.map((i) => Object.assign({}, i, { status: 'ok', fix: '' })) }));
  ok($('health-banner').hasAttribute('hidden') && $('health-badge').classList.contains('health-ok') && /all good/.test($('health-badge-text').textContent), 'the banner goes away once nothing fails');
  ok(/^ok/.test($('st-health').textContent), 'the settings drawer summarises health: ' + $('st-health').textContent);
  recv({ type: 'health', status: 'ok', ts: now(), items: [{ key: 'tmux', label: 'tmux', status: 'ok', detail: 'server running' }] });
  ok($('health-strip').children.length === 1, 'the strip is rebuilt from each message, not appended to');

  ok($('update-banner').hasAttribute('hidden'), 'no update banner before an update message');
  recv({ type: 'update', current: '0.1.0', latest: '0.2.0', command: 'zordon update', auto: false, notes_url: 'https://example.com/commits/main' });
  ok(!$('update-banner').hasAttribute('hidden') && /Zordon 0\.2\.0 is available/.test($('update-banner-text').textContent) && /zordon update/.test($('update-banner-text').textContent), 'update banner names the version and the command');
  ok(!$('update-banner-link').hasAttribute('hidden') && $('update-banner-link').href === 'https://example.com/commits/main', 'the notes link is shown for an https URL');
  $('update-banner-close').click();
  ok($('update-banner').hasAttribute('hidden'), 'the update banner is dismissible');
  recv({ type: 'update', current: '0.1.0', latest: '0.2.0', command: 'zordon update', auto: false });
  ok($('update-banner').hasAttribute('hidden'), 'the same update stays dismissed');
  recv({ type: 'update', current: '0.1.0', latest: '0.2.0', command: 'restart zordon serve', auto: true, notes_url: 'javascript:alert(1)' });
  ok(!$('update-banner').hasAttribute('hidden') && /installed\. Restart zordon serve/.test($('update-banner-text').textContent), 'auto=true says the update is installed and asks for a restart');
  ok($('update-banner-link').hasAttribute('hidden') && !$('update-banner-link').hasAttribute('href'), 'a non-https notes URL is never linked');

  // ---- long responses stay sequential: no re-anchoring over a full queue (overlap bug) ----
  {
    const before = audioLog.started.length;
    // 1 s of 24 kHz silence per chunk; 40 chunks = 40 s queued while the clock stands still.
    const big = Buffer.alloc(24000 * 2).toString('base64');
    for (let i = 0; i < 40; i++) recv({ type: 'speech', sentence_id: 900 + i, seq: i, generation: 2, sample_rate: 24000, pcm: big, final: true });
    const starts = audioLog.started.slice(before).map((s) => s.at);
    let monotone = true;
    for (let i = 1; i < starts.length; i++) if (starts[i] < starts[i - 1] + 0.999) monotone = false;
    ok(starts.length === 40 && monotone, 'forty seconds of speech are scheduled back to back, never on top of each other');
    ok(starts[starts.length - 1] - starts[0] > 38, 'the last chunk starts ~39 s after the first (no re-anchor to now)');
  }

  // ---- WEB-15: projects view (admin mode) and work view ----
  {
    const findText = (node, re) => node.textContent && re.test(node.textContent);
    const buttons = (node) => node.querySelectorAll('button');
    // Nothing focused: the projects view is shown, the feed hidden.
    recv(hello({ focused_session: null, agents: { 'claude-code': true, codex: true, generic: true }, default_agent: 'claude-code', home: '/home/u' }));
    recv({ type: 'sessions', sessions: [] });
    ok(!$('projects-view').hasAttribute('hidden') && $('feed').hasAttribute('hidden') && $('work-head').hasAttribute('hidden'), 'no focus: projects view shown, feed and work header hidden');
    ok($('focus-title').textContent === 'Projects', 'header chip says Projects in admin mode');
    ok(!$('projects-empty').hasAttribute('hidden'), 'no projects yet: the empty hint shows');
    // A projects message renders one card per project with folder, badges and a Forget.
    const projectsMsg = {
      type: 'projects',
      focused_project: null,
      projects: [
        { id: 'p1', name: 'Website', directory: '/home/u/Code/site', agent: 'claude-code', permission_mode: 'auto', scope_edits: true, running: true, session_id: 's1', focused: false, state: 'idle', last_used: now(), exists: true },
        { id: 'p2', name: 'Yolo', directory: '/home/u/yolo', agent: 'claude-code', permission_mode: 'bypassPermissions', scope_edits: true, running: false, session_id: null, focused: false, state: null, last_used: now() - 3600, exists: true },
        { id: 'p3', name: 'Gone', directory: '/home/u/gone', agent: 'codex', permission_mode: 'default', scope_edits: true, running: false, session_id: null, focused: false, state: null, last_used: 1, exists: false },
      ],
    };
    recv(projectsMsg);
    const cards = $('project-list').children;
    ok(cards.length === 3 && $('projects-empty').hasAttribute('hidden'), 'one card per project');
    ok(findText(cards[0], /Website/) && findText(cards[0], /~\/Code\/site/) && findText(cards[0], /Running/) && findText(cards[0], /auto mode/), 'card shows name, home-relative folder, Running and the mode in words');
    ok(findText(cards[1], /Paused/) && findText(cards[1], /no permission checks/), 'a bypass project is labelled plainly');
    ok(findText(cards[2], /Folder missing/) && (buttons(cards[2])[0].disabled || buttons(cards[2])[0].hasAttribute('disabled')), 'a project whose folder is gone cannot be opened');
    buttons(cards[0])[0].click();
    const openCmd = cmds('open_project')[cmds('open_project').length - 1];
    ok(openCmd && openCmd.args.project_id === 'p1', 'tapping a card sends open_project');
    buttons(cards[1])[1].click(); // Forget -> confirm dialog
    ok($('confirm-dialog').open && /Files stay where they are/.test($('confirm-text').textContent), 'Forget asks first and says files stay');
    $('confirm-dialog').returnValue = 'ok';
    $('confirm-dialog').close();
    const forgetCmd = cmds('forget_project')[cmds('forget_project').length - 1];
    ok(forgetCmd && forgetCmd.args.project_id === 'p2' && forgetCmd.args.confirm === true, 'confirming sends forget_project with confirm');

    // Focus arrives: the work view takes over with the project name and mode.
    recv(Object.assign({}, projectsMsg, { focused_project: 'p1', projects: projectsMsg.projects.map((p) => Object.assign({}, p, { focused: p.id === 'p1' })) }));
    recv(sessions('s1'));
    ok($('projects-view').hasAttribute('hidden') && !$('feed').hasAttribute('hidden') && !$('work-head').hasAttribute('hidden'), 'focus: work view shown');
    ok($('work-title').textContent === 'Website' && $('work-mode').textContent === 'auto mode', 'work header names the project and its mode');
    ok($('focus-title').textContent === 'Website', 'header chip names the project too');
    $('btn-pause').click();
    ok(cmds('admin').length === 1, 'Pause sends admin (the agent keeps running)');
    recv({ type: 'sessions', sessions: sessions('s1').sessions.map((x) => Object.assign({}, x, { focused: false })) });
    recv(Object.assign({}, projectsMsg, { focused_project: null }));
    ok(!$('projects-view').hasAttribute('hidden'), 'after admin the projects view is back');
    const sessionsHidden = $('sessions').hasAttribute('hidden');
    ok(sessionsHidden, 'the raw session picker no longer pops up on its own');
  }

  // ---- WEB-16: the new-project walkthrough ----
  {
    const nBrowse = cmds('browse').length;
    $('btn-new-project').click();
    ok(!$('new-project').hasAttribute('hidden') && !$('np-step-where').hasAttribute('hidden'), 'Start a new project opens the walkthrough on the folder step');
    ok(cmds('browse').length === nBrowse + 1 && cmds('browse')[nBrowse].args.path === undefined, 'it asks for the home folder listing first (no path)');
    recv({ type: 'browse', path: '/home/u', parent: null, home: '/home/u', can_create: true, entries: [
      { name: 'Code', path: '/home/u/Code', has_git: false, project_id: null },
      { name: 'yolo', path: '/home/u/yolo', has_git: true, project_id: 'p2' },
    ] });
    ok($('np-up').hasAttribute('hidden') && $('np-crumb').textContent === '~', 'at home there is no Up and the crumb is ~');
    const folderBtns = $('np-folders').querySelectorAll('button');
    ok(folderBtns.length === 2 && /Code/.test(folderBtns[0].textContent) && /already a project/.test(folderBtns[1].textContent), 'folders listed; an existing project is marked');
    ok($('np-use-folder').disabled, 'the whole home folder cannot be a project');
    folderBtns[0].click();
    const b2 = cmds('browse')[cmds('browse').length - 1];
    ok(b2.args.path === '/home/u/Code', 'tapping a folder browses into it');
    recv({ type: 'browse', path: '/home/u/Code', parent: '/home/u', home: '/home/u', can_create: true, entries: [{ name: 'site', path: '/home/u/Code/site', has_git: true, project_id: 'p1' }] });
    ok(!$('np-up').hasAttribute('hidden') && $('np-crumb').textContent === '~/Code', 'inside a folder Up appears and the crumb is relative to home');
    // Create a new folder: a taken name is refused client-side, a fresh one moves on.
    $('np-folder-name').value = 'site';
    $('np-create-form').dispatch('submit');
    ok(!$('np-error').hasAttribute('hidden') && /already a folder called site/.test($('np-error').textContent), 'a name that exists here is refused inline');
    $('np-folder-name').value = 'My App';
    $('np-create-form').dispatch('submit');
    // claude-code and codex installed -> agent step shows; the generic (attach-only) adapter is never offered.
    ok(!$('np-step-agent').hasAttribute('hidden'), 'with more than one installed assistant the assistant step is shown');
    const agentBtns = $('np-agents').querySelectorAll('button');
    ok(agentBtns.length === 2 && agentBtns[0].getAttribute('aria-checked') === 'true', 'installed assistants listed, default preselected');
    ok(Array.from(agentBtns).every(function (b) { return !/Generic/.test(b.textContent); }), 'the generic pane adapter is not a project assistant');
    $('np-next').click();
    ok(!$('np-step-ask').hasAttribute('hidden'), 'then the permissions step');
    const modeBtns = $('np-modes').querySelectorAll('button');
    ok(modeBtns.length === 3 && modeBtns[0].dataset.value === 'default' && modeBtns[1].dataset.value === 'auto' && modeBtns[2].dataset.value === 'bypassPermissions', 'three choices: ask, auto, never ask');
    ok($('np-bypass-warn').hasAttribute('hidden'), 'no bypass warning while a safe mode is chosen');
    modeBtns[2].click();
    ok(!$('np-bypass-warn').hasAttribute('hidden'), 'choosing Never ask shows the warning');
    $('np-next').click();
    ok(!$('np-step-ready').hasAttribute('hidden') && /My App/.test($('np-summary').textContent) && /Never ask/.test($('np-summary').textContent), 'summary names folder and mode');
    $('np-start').click();
    const create = cmds('create_project')[cmds('create_project').length - 1];
    ok(create && create.args.parent === '/home/u/Code' && create.args.name === 'My App' && create.args.permission_mode === 'bypassPermissions' && create.args.scope_edits === true && create.args.existing === undefined && create.args.agent === undefined, 'Start sends create_project with the chosen folder, bypass mode and scope');
    ok(create.args.talk_first === true, 'talk-first is on by default');
    ok(create.args.runner === 'terminal', 'a terminal pane unless headless was ticked');
    // A server error lands inline, not as a toast.
    recv({ type: 'error', message: 'My App already exists.', code: 'command' });
    ok(!$('np-error').hasAttribute('hidden') && /already exists/.test($('np-error').textContent), 'a create error is shown inside the walkthrough');
    $('np-start').click();
    recv({ type: 'sessions', sessions: [{ session_id: 's9', directory: '/home/u/Code/My App', title: 'My App', last_active: now(), attached: true, running: true, state: 'working', permission_mode: 'bypassPermissions', focused: true }] });
    recv({ type: 'projects', focused_project: 'p9', projects: [{ id: 'p9', name: 'My App', directory: '/home/u/Code/My App', permission_mode: 'bypassPermissions', running: true, session_id: 's9', focused: true, exists: true }] });
    ok($('new-project').hasAttribute('hidden') && !$('work-head').hasAttribute('hidden') && $('work-title').textContent === 'My App', 'on success the walkthrough closes and the work view shows the new project');

    // With a single installed assistant the assistant step is skipped.
    recv(hello({ focused_session: null, agents: { 'claude-code': true, codex: false, generic: true }, default_agent: 'claude-code', home: '/home/u' }));
    recv({ type: 'sessions', sessions: [] });
    $('btn-new-project').click();
    recv({ type: 'browse', path: '/home/u', parent: null, home: '/home/u', can_create: true, entries: [{ name: 'Code', path: '/home/u/Code', has_git: false, project_id: null }] });
    $('np-folders').querySelectorAll('button')[0].click();
    recv({ type: 'browse', path: '/home/u/Code', parent: '/home/u', home: '/home/u', can_create: true, entries: [] });
    $('np-use-folder').click();
    ok(!$('np-step-ask').hasAttribute('hidden') && $('np-step-agent').hasAttribute('hidden'), 'one installed assistant: straight from folder to permissions');
    ok(/Step 2 of 3/.test($('np-step-label').textContent), 'the step counter skips it too');
    $('np-next').click();
    $('np-start').click();
    const create2 = cmds('create_project')[cmds('create_project').length - 1];
    ok(create2.args.existing === true && create2.args.parent === '/home/u/Code' && create2.args.permission_mode === 'default', 'Use this folder sends existing=true with the safe default mode');
    $('np-close').click();
    ok($('new-project').hasAttribute('hidden'), 'close hides the walkthrough');
    // The voice shim's notice opens it.
    recv({ type: 'transcript', row_id: -77, session_id: '', kind: 'notice', text: 'Start a new project: tap the button in Projects.', raw_lines: [], ts: now() });
    ok(!$('new-project').hasAttribute('hidden'), 'the "new project" voice notice opens the walkthrough');
    $('np-close').click();
  }

  // ---- heard line and the unsent draft (decision 0019) ----
  {
    recv({ type: 'heard', text: 'add retry logic to the uploader', confidence: 0.9, ts: now() });
    ok(!$('heard').hasAttribute('hidden') && $('heard-text').textContent === 'add retry logic to the uploader', 'what Zordon heard shows at once');
    recv({ type: 'draft', session_id: 's1', text: 'add retry logic to the uploader', state: 'composing', ts: now() });
    ok(!$('draft').hasAttribute('hidden') && /retry logic/.test($('draft-text').textContent), 'the unsent draft is shown while composing');
    const before = sent.length;
    $('draft-send').click();
    const last = sent[sent.length - 1];
    ok(sent.length === before + 1 && last.type === 'text' && last.text === 'send it', 'the Send button sends the cue');
    recv({ type: 'draft', session_id: 's1', text: 'add retry logic to the uploader', state: 'sent', ts: now() });
    ok($('draft').hasAttribute('hidden'), 'a sent draft disappears');
    recv({ type: 'draft', session_id: 's1', text: 'never mind this', state: 'composing', ts: now() });
    $('draft-clear').click();
    ok(sent[sent.length - 1].text === 'scratch that', 'the Clear button sends the cue');
    recv({ type: 'draft', session_id: 's1', text: '', state: 'cleared', ts: now() });
    ok($('draft').hasAttribute('hidden'), 'a cleared draft disappears');
  }

  console.log(`app_test: all passed (${passed} checks)`);
  process.exit(0);
})().catch((e) => { console.error('app_test failed:', e); process.exit(1); });
