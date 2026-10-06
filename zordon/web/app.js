// Zordon web client. One page, no framework, no build step.
//
// Loads after protocol.js (wire schema mirror) and audio.js (capture and
// playback). Everything the agent sends is validated by ZordonProtocol before
// it is rendered, and everything rendered from agent data goes through DOM
// text nodes, never innerHTML.

(function () {
  'use strict';

  var P = window.ZordonProtocol;
  var Audio = window.ZordonAudio;
  if (!P || !Audio) {
    document.body.textContent = 'Zordon failed to load its scripts.';
    return;
  }

  var $ = function (id) {
    return document.getElementById(id);
  };

  var PING_INTERVAL_MS = 15000;
  var DEAD_SOCKET_MS = 45000; // no inbound traffic for this long -> force a reconnect
  var RECONNECT_BASE_MS = 1000;
  var RECONNECT_MAX_MS = 30000;
  var GATE_AFTER_FAILURES = 3; // consecutive handshake failures after a good connection
  var MAX_ROWS = 600;
  var UPLOAD_MAX_BYTES = 25 * 1024 * 1024; // mirrors zordon.transport.ws.UPLOAD_MAX_BYTES
  var LOCAL_ECHO_SETTLE_MS = 4000; // unmirrored local rows become ordinary rows after this
  var VAD_ONSET_MS = 60; // 3 x 20 ms frames, from the design
  var KOKORO_VOICES = [
    'af_heart',
    'af_bella',
    'af_nicole',
    'af_sarah',
    'af_sky',
    'am_adam',
    'am_michael',
    'bf_emma',
    'bf_isabella',
    'bm_george',
    'bm_lewis',
  ];
  var STATE_LABELS = {
    idle: 'idle',
    working: 'working',
    awaiting_permission: 'needs permission',
    awaiting_plan_approval: 'plan ready',
    awaiting_question: 'question',
    stalled: 'stalled',
    detached: 'detached',
  };

  // ---- state -----------------------------------------------------------------------

  var S = {
    ws: null,
    connected: false,
    everOpened: false,
    failures: 0,
    attempt: 0,
    reconnectTimer: 0,
    pingTimer: 0,
    tickTimer: 0,
    lastInbound: 0,
    rttMs: null,
    hello: null,
    agents: {}, // adapter key -> installed (from hello.agents)
    defaultAgent: 'claude-code',
    focused: null,
    sessions: [],
    sessionsById: {},
    states: {}, // session_id -> {state, detail, ts}
    rows: {}, // row_id -> {el, msg}
    rowOrder: [],
    pendingLocal: [], // locally echoed user rows awaiting the server's copy
    localSeq: 0,
    prompts: {}, // prompt_id -> {el, msg}
    callActive: false,
    pausedByVisibility: false,
    micMuted: false,
    speakerMuted: false,
    settings: { verbosity: 'minimal', tool_chatter: false, muted: false, providers: {}, permission_mode: null, launch_mode: null },
    tunnel: null,
    health: null, // last `health` message
    healthOpen: null, // key of the item whose detail is shown, or null
    healthStripOpen: true,
    update: null, // last `update` message
    updateDismissed: '', // "<latest>:<auto>" the user dismissed
    atBottom: true,
    unseen: 0,
    expandedDirs: {},
    lastFlushMs: null,
    gateOpen: false,
    uploading: 0,
    pickerAutoOpened: false,
    home: null, // the user's home directory (hello.home); projects live under it
    projects: [], // last `projects` message, most recently used first
    focusedProject: null,
    // The new-project walkthrough (decision 0018). `step` is where|agent|ask|ready.
    np: { open: false, step: 'where', path: null, listing: null, folder: null, existing: false, name: '', agent: null, mode: 'default', scope: true, pending: null },
  };

  var MODE_WORDS = {
    default: 'asks before acting',
    acceptEdits: 'accepts edits',
    plan: 'plan mode',
    auto: 'auto mode',
    dontAsk: "doesn't ask",
    bypassPermissions: 'no permission checks',
  };
  var NP_MODES = [
    { value: 'default', label: 'Ask me before acting', help: 'Safest. You confirm each command and file change by voice.' },
    { value: 'auto', label: 'Handle routine things itself', help: 'Claude Code approves routine commands on its own and asks for the rest. Recommended for voice.' },
    { value: 'bypassPermissions', label: 'Never ask', help: 'Runs everything without asking. Read the warning below.' },
  ];

  // ---- DOM helpers -----------------------------------------------------------------------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k.slice(0, 2) === 'on') node.addEventListener(k.slice(2), v);
        else if (k === 'dataset') for (var d in v) node.dataset[d] = v[d];
        else node.setAttribute(k, v === true ? '' : v);
      }
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(function (c) {
        if (c === null || c === undefined || c === false) return;
        node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      });
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function show(node, visible) {
    if (visible) node.removeAttribute('hidden');
    else node.setAttribute('hidden', '');
  }

  function basename(path) {
    var p = String(path || '').replace(/\/+$/, '');
    var i = p.lastIndexOf('/');
    return i === -1 ? p : p.slice(i + 1) || p;
  }

  function relTime(ts) {
    if (!ts) return 'never';
    var d = Date.now() / 1000 - ts;
    if (d < 0) d = 0;
    if (d < 45) return 'just now';
    if (d < 3600) return Math.round(d / 60) + ' min ago';
    if (d < 86400) return Math.round(d / 3600) + ' h ago';
    return Math.round(d / 86400) + ' d ago';
  }

  function clock(ts) {
    var dt = new Date((ts || Date.now() / 1000) * 1000);
    var h = dt.getHours();
    var m = dt.getMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
  }

  function stateClass(state) {
    if (!state) return 'state-detached';
    if (state.indexOf('awaiting') === 0) return 'state-awaiting';
    if (P.STATES.indexOf(state) === -1) return 'state-detached';
    return 'state-' + state;
  }

  function stateLabel(state) {
    return STATE_LABELS[state] || state || 'unknown';
  }

  // ---- toasts ------------------------------------------------------------------------

  function toast(text, level, ms) {
    level = level || 'info';
    var node = el('div', { class: 'toast toast-' + level, role: level === 'error' ? 'alert' : 'status' }, [
      el('span', { class: 'toast-text', text: text }),
      el('button', {
        type: 'button',
        class: 'toast-close',
        'aria-label': 'Dismiss',
        text: '×',
        onclick: function () {
          dismiss();
        },
      }),
    ]);
    var box = $('toasts');
    box.appendChild(node);
    while (box.children.length > 4) box.removeChild(box.firstChild);
    var timer = setTimeout(dismiss, ms || (level === 'error' ? 8000 : 4500));
    function dismiss() {
      clearTimeout(timer);
      if (node.parentNode) node.parentNode.removeChild(node);
    }
  }

  // ---- socket ------------------------------------------------------------------------

  function wsUrl() {
    return (location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/ws';
  }

  function send(obj) {
    if (!S.ws || S.ws.readyState !== WebSocket.OPEN) return false;
    try {
      S.ws.send(JSON.stringify(obj));
      return true;
    } catch (e) {
      return false;
    }
  }

  function cmd(name, args) {
    var msg;
    try {
      msg = P.buildCommand(name, args);
    } catch (e) {
      toast(e.message, 'error');
      return false;
    }
    var ok = send(msg);
    if (!ok) toast('Not connected', 'error');
    return ok;
  }

  function connect() {
    if (S.ws && (S.ws.readyState === WebSocket.CONNECTING || S.ws.readyState === WebSocket.OPEN)) return;
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = 0;
    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (e) {
      onSocketClosed(false);
      return;
    }
    S.ws = ws;
    var opened = false;
    setConn('connecting');
    ws.onopen = function () {
      opened = true;
      S.connected = true;
      S.everOpened = true;
      S.failures = 0;
      S.attempt = 0;
      S.lastInbound = Date.now();
      setGate(false);
      setConn('connected');
      startPing();
      // Re-request the picker contents; the agent also sends hello first.
      cmd('list_sessions');
      if (S.callActive) {
        send(P.buildCall('start'));
        if (document.visibilityState === 'hidden') send(P.buildCall('pause'));
      }
    };
    ws.onmessage = function (ev) {
      S.lastInbound = Date.now();
      if (typeof ev.data !== 'string') return;
      var msg = P.parseInbound(ev.data);
      if (!msg) return; // unknown or malformed: ignore
      try {
        handle(msg);
      } catch (e) {
        if (window.console) console.error('handler failed', msg.type, e);
      }
    };
    ws.onerror = function () {
      /* onclose follows */
    };
    ws.onclose = function () {
      if (S.ws === ws) S.ws = null;
      onSocketClosed(opened);
    };
  }

  function onSocketClosed(wasOpen) {
    var hadConnection = S.connected;
    S.connected = false;
    stopPing();
    setConn('offline');
    if (!wasOpen) S.failures++;
    if (!S.everOpened) {
      // First handshake refused: almost certainly no session cookie yet.
      setGate(true, 'Enter the token to connect.');
      return;
    }
    if (!wasOpen && S.failures >= GATE_AFTER_FAILURES) {
      setGate(true, 'Reconnecting... if the agent restarted, enter the token again.');
    }
    if (hadConnection) toast('Connection lost, reconnecting', 'warn', 3000);
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (S.reconnectTimer) return;
    var delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * Math.pow(2, S.attempt));
    delay = delay * (0.7 + Math.random() * 0.6);
    S.attempt++;
    S.reconnectTimer = setTimeout(function () {
      S.reconnectTimer = 0;
      connect();
    }, delay);
    setConn('reconnecting in ' + Math.round(delay / 1000) + ' s');
  }

  function reconnectNow() {
    if (S.connected) return;
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = 0;
    S.attempt = 0;
    connect();
  }

  function startPing() {
    stopPing();
    S.pingTimer = setInterval(function () {
      if (!S.connected) return;
      if (Date.now() - S.lastInbound > DEAD_SOCKET_MS) {
        try {
          S.ws.close();
        } catch (_) {
          /* ignore */
        }
        return;
      }
      send(P.buildPing(Date.now() / 1000));
    }, PING_INTERVAL_MS);
  }

  function stopPing() {
    if (S.pingTimer) clearInterval(S.pingTimer);
    S.pingTimer = 0;
  }

  // ---- token gate ----------------------------------------------------------------------

  function setGate(open, message) {
    S.gateOpen = open;
    show($('gate'), open);
    if (message !== undefined) $('gate-msg').textContent = message;
    if (open) {
      setTimeout(function () {
        $('token').focus();
      }, 50);
    }
  }

  function authenticate(token) {
    var btn = $('gate-submit');
    btn.disabled = true;
    $('gate-msg').textContent = 'Checking token...';
    return fetch('/auth', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token }),
    })
      .then(function (r) {
        if (r.ok) {
          $('gate-msg').textContent = 'Connected. Opening the session...';
          $('token').value = '';
          S.failures = 0;
          reconnectNow();
          return;
        }
        if (r.status === 401 || r.status === 403) {
          $('gate-msg').textContent = 'That token was not accepted.';
        } else if (r.status === 429) {
          $('gate-msg').textContent = 'Too many attempts. Wait a minute and try again.';
        } else {
          $('gate-msg').textContent = 'The agent answered ' + r.status + '.';
        }
      })
      .catch(function () {
        $('gate-msg').textContent = 'Could not reach the agent. Is zordon serve running?';
      })
      .then(function () {
        btn.disabled = false;
      });
  }

  // ---- inbound dispatch ------------------------------------------------------------------

  function handle(msg) {
    switch (msg.type) {
      case 'hello':
        return onHello(msg);
      case 'sessions':
        return onSessions(msg);
      case 'speech':
        return onSpeech(msg);
      case 'flush':
        return onFlush(msg);
      case 'transcript':
        return onTranscript(msg);
      case 'state':
        return onState(msg);
      case 'prompt':
        return onPrompt(msg);
      case 'settings':
        return onSettings(msg);
      case 'error':
        return onError(msg);
      case 'pong':
        return onPong(msg);
      case 'tunnel':
        return onTunnel(msg);
      case 'update':
        return onUpdate(msg);
      case 'health':
        return onHealth(msg);
      case 'projects':
        return onProjects(msg);
      case 'browse':
        return onBrowse(msg);
      case 'heard':
        return onHeard(msg);
      case 'draft':
        return onDraft(msg);
      default:
        return undefined;
    }
  }

  // Everything keyed on the agent's counters (generation, row/prompt/sentence ids)
  // is forgotten when a new hello arrives: the agent may have restarted, and it
  // resends the sessions and the transcript tail right after hello anyway.
  function resetAgentState() {
    audio.resetGeneration();
    Object.keys(S.rows).forEach(function (id) {
      var r = S.rows[id];
      if (r.el.parentNode) r.el.parentNode.removeChild(r.el);
    });
    S.rows = {};
    S.rowOrder = [];
    S.pendingLocal.forEach(function (p) {
      if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
    });
    S.pendingLocal = [];
    Object.keys(S.prompts).forEach(removePrompt);
    S.unseen = 0;
    renderJump();
    show($('feed-empty'), true);
  }

  function onHello(msg) {
    var again = S.hello !== null;
    S.hello = msg;
    if (msg.protocol !== P.PROTOCOL_VERSION) {
      toast('Protocol mismatch: agent ' + msg.protocol + ', client ' + P.PROTOCOL_VERSION, 'warn');
    }
    if (again) resetAgentState();
    S.focused = msg.focused_session || null;
    S.settings.verbosity = msg.verbosity;
    S.settings.tool_chatter = msg.tool_chatter;
    if (typeof msg.muted === 'boolean') S.settings.muted = msg.muted; // WEB-7: hello carries the agent's mute state
    S.settings.providers = msg.providers || {};
    S.agents = msg.agents || {};
    S.defaultAgent = msg.default_agent || 'claude-code';
    if (typeof msg.home === 'string' && msg.home) S.home = msg.home;
    renderAgentSelects();
    $('st-version').textContent = 'zordon ' + msg.version + ' (protocol ' + msg.protocol + ')';
    if (msg.tunnel_url && !S.tunnel) onTunnel({ type: 'tunnel', url: msg.tunnel_url, qr_svg: null });
    renderSettings();
    renderFocus();
  }

  function onSessions(msg) {
    S.sessions = msg.sessions.slice();
    S.sessionsById = {};
    var focused = null;
    S.sessions.forEach(function (s) {
      S.sessionsById[s.session_id] = s;
      if (s.focused) focused = s.session_id;
      if (!S.states[s.session_id] || S.states[s.session_id].state !== s.state) {
        S.states[s.session_id] = { state: s.state, detail: (S.states[s.session_id] || {}).detail || '', ts: Date.now() / 1000 };
      }
    });
    var before = S.focused;
    // The snapshot is authoritative: no focused flag means admin mode (nothing focused).
    S.focused = focused;
    // The last `settings` message described the session focused at that time.
    if (S.focused !== before) S.settings.permission_mode = null;
    renderSessions();
    renderProjects();
    renderFocus();
    renderSettings();
  }

  function onSpeech(msg) {
    audio.onSpeech(msg);
  }

  function onFlush(msg) {
    var res = audio.flush(msg.generation);
    S.lastFlushMs = res.ms;
    markInterrupted(res.interrupted, msg.sentence_id);
    send(P.buildFlushAck(msg.generation));
    renderLatency();
  }

  // Sentences cut by a flush: the ones that were playing (interrupted), the one the
  // agent names, and every later sentence whose final chunk never arrived (the
  // agent dropped them before they reached us).
  function markInterrupted(interrupted, agentSentenceId) {
    var cut = {};
    (interrupted || []).forEach(function (sid) {
      cut[sid] = true;
    });
    if (typeof agentSentenceId === 'number') cut[agentSentenceId] = true;
    var ids = Object.keys(cut).map(Number);
    if (ids.length) {
      var base = Math.min.apply(null, ids);
      Object.keys(S.rows).forEach(function (id) {
        var m = S.rows[id].msg;
        if (m.kind !== 'spoken' || typeof m.sentence_id !== 'number') return;
        if (m.sentence_id > base && m.spoken !== true && !audio.sentenceFinished(m.sentence_id)) {
          cut[m.sentence_id] = true;
        }
      });
    }
    Object.keys(cut).forEach(function (sid) {
      markCutOff(Number(sid));
    });
  }

  function onState(msg) {
    var prev = (S.states[msg.session_id] || {}).state;
    S.states[msg.session_id] = { state: msg.state, detail: msg.detail || '', ts: msg.ts };
    var s = S.sessionsById[msg.session_id];
    if (s) s.state = msg.state;
    if (msg.session_id === S.focused && prev !== msg.state) {
      if (msg.state === 'working' && prev !== 'working') playCue('working');
      else if (msg.state === 'idle' && prev === 'working') playCue('done');
      else if (msg.state.indexOf('awaiting') === 0) playCue('ask');
    }
    // A prompt that is no longer reflected in the state is gone.
    if (msg.state.indexOf('awaiting') !== 0) {
      Object.keys(S.prompts).forEach(function (pid) {
        if (S.prompts[pid].msg.session_id === msg.session_id) removePrompt(pid);
      });
    }
    renderSessions();
    renderFocus();
  }

  function onSettings(msg) {
    if (typeof msg.tts_speed === 'number') {
      var sl = $('set-speed');
      if (sl && document.activeElement !== sl) sl.value = String(msg.tts_speed);
      var sv = $('set-speed-value');
      if (sv) sv.textContent = msg.tts_speed.toFixed(2) + 'x';
    }
    if (typeof msg.system_prompt === 'string') {
      var pt = $('set-prompt');
      if (pt && document.activeElement !== pt) pt.value = msg.system_prompt;
      var st = $('set-prompt-state');
      if (st) st.textContent = msg.system_prompt_custom ? 'your text' : "Zordon's default";
    }
    S.settings.verbosity = msg.verbosity;
    S.settings.tool_chatter = msg.tool_chatter;
    S.settings.muted = msg.muted;
    S.settings.providers = msg.providers || {};
    S.settings.permission_mode = msg.permission_mode || null;
    if (msg.launch_mode && P.PERMISSION_MODES.indexOf(msg.launch_mode) !== -1 && S.settings.launch_mode !== msg.launch_mode) {
      // The configured default for new sessions; preselect it once per value so a
      // choice the user made in the form is not undone by every settings refresh.
      S.settings.launch_mode = msg.launch_mode;
      var sel = $('new-mode');
      if (sel) sel.value = msg.launch_mode;
    }
    renderSettings();
  }

  function onPong(msg) {
    if (typeof msg.ts === 'number') {
      S.rttMs = Math.max(0, Math.round(Date.now() - msg.ts * 1000));
      renderLatency();
    }
  }

  function onTunnel(msg) {
    S.tunnel = msg.url ? msg : null;
    var box = $('tunnel');
    show(box, !!S.tunnel);
    var a = $('tunnel-url');
    var qr = $('tunnel-qr');
    clear(qr);
    if (!S.tunnel) {
      a.textContent = '';
      a.removeAttribute('href');
      return;
    }
    a.textContent = msg.url;
    a.href = msg.url;
    if (msg.qr_svg && /^\s*<svg[\s>]/i.test(msg.qr_svg)) {
      // Rendered through an <img> so the SVG can never run script in this page.
      var img = el('img', {
        alt: 'QR code for ' + msg.url,
        src: 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(msg.qr_svg),
      });
      qr.appendChild(img);
    }
  }

  // ---- health strip and banners -----------------------------------------------------------

  var HEALTH_WORDS = { ok: 'all good', warn: 'warning', fail: 'problem' };

  function sentenceCase(text) {
    var t = String(text || '');
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
  }

  function healthLine(item) {
    var line = sentenceCase(item.detail || item.label);
    if (item.status !== 'ok' && item.fix) line += (/[.!?]$/.test(line) ? '' : '.') + ' Fix: ' + item.fix;
    return line;
  }

  function onHealth(msg) {
    S.health = msg;
    if (S.healthOpen && !msg.items.some(function (i) { return i.key === S.healthOpen; })) S.healthOpen = null;
    renderHealth();
  }

  function renderHealth() {
    var h = S.health;
    var strip = $('health-strip');
    var badge = $('health-badge');
    var banner = $('health-banner');
    if (!h) {
      show(strip, false);
      show(badge, false);
      show(banner, false);
      show($('health-detail'), false);
      $('st-health').textContent = '-';
      return;
    }
    var fails = h.items.filter(function (i) { return i.status === 'fail'; });
    var warns = h.items.filter(function (i) { return i.status === 'warn'; });
    // Overall badge.
    show(badge, true);
    badge.className = 'health-badge health-' + h.status;
    var count = h.status === 'fail' ? fails.length : h.status === 'warn' ? warns.length : 0;
    $('health-badge-text').textContent = count ? count + ' ' + HEALTH_WORDS[h.status] + (count > 1 ? 's' : '') : HEALTH_WORDS.ok;
    badge.title = 'Health: ' + h.status + (S.healthStripOpen ? ' (tap to hide details)' : ' (tap to show details)');
    badge.setAttribute('aria-expanded', S.healthStripOpen ? 'true' : 'false');
    // One dot per item.
    clear(strip);
    h.items.forEach(function (item) {
      var text = item.label + ': ' + healthLine(item);
      var dot = el(
        'button',
        {
          type: 'button',
          class: 'health-item health-' + item.status + (S.healthOpen === item.key ? ' open' : ''),
          title: text,
          'aria-label': text,
          dataset: { key: item.key, status: item.status },
          onclick: function () {
            S.healthOpen = S.healthOpen === item.key ? null : item.key;
            renderHealth();
          },
        },
        [el('span', { class: 'health-dot' }), el('span', { class: 'health-label', text: item.label })],
      );
      strip.appendChild(dot);
    });
    show(strip, S.healthStripOpen);
    // Detail box for the tapped dot.
    var open = null;
    h.items.forEach(function (i) { if (i.key === S.healthOpen) open = i; });
    show($('health-detail'), !!open && S.healthStripOpen);
    $('health-detail-text').textContent = open ? open.label + ': ' + healthLine(open) : '';
    // Persistent banner for anything failed.
    clear($('health-banner-text'));
    fails.forEach(function (item) {
      $('health-banner-text').appendChild(el('span', { class: 'banner-line', text: healthLine(item) }));
    });
    show(banner, fails.length > 0);
    // Settings drawer summary.
    var names = (h.status === 'fail' ? fails : warns).map(function (i) { return i.label; });
    $('st-health').textContent = h.status + (names.length ? ': ' + names.join(', ') : ' (' + h.items.length + ' parts checked)');
  }

  function onUpdate(msg) {
    S.update = msg;
    renderUpdate();
  }

  function updateKey(msg) {
    return msg ? msg.latest + ':' + (msg.auto ? '1' : '0') : '';
  }

  function renderUpdate() {
    var u = S.update;
    var banner = $('update-banner');
    if (!u || S.updateDismissed === updateKey(u)) {
      show(banner, false);
      return;
    }
    var text = u.auto
      ? 'Zordon ' + u.latest + ' installed. Restart zordon serve to use it' + (u.command ? ' (' + u.command + ')' : '') + '.'
      : 'Zordon ' + u.latest + ' is available (you have ' + u.current + ') \u2014 ' + u.command;
    $('update-banner-text').textContent = text;
    var link = $('update-banner-link');
    if (u.notes_url && /^https:\/\//.test(u.notes_url)) {
      link.href = u.notes_url;
      show(link, true);
    } else {
      link.removeAttribute('href');
      show(link, false);
    }
    show(banner, true);
  }

  // ---- transcript feed ------------------------------------------------------------------

  function feedAtBottom() {
    var f = $('feed');
    return f.scrollHeight - f.scrollTop - f.clientHeight < 48;
  }

  function scrollFeedToBottom() {
    var f = $('feed');
    f.scrollTop = f.scrollHeight;
    S.unseen = 0;
    renderJump();
  }

  function renderJump() {
    var j = $('jump');
    show(j, !!S.focused && !S.atBottom && S.unseen > 0);
    $('jump-count').textContent = S.unseen > 0 ? String(S.unseen) : '';
  }

  function sessionTag(sessionId) {
    var s = S.sessionsById[sessionId];
    return s ? s.title || basename(s.directory) : sessionId.slice(0, 8);
  }

  function isOtherSession(sessionId) {
    // An empty session id is an agent-wide notice, not another session.
    return !!(S.focused && sessionId && sessionId !== S.focused);
  }

  function buildRow(msg) {
    var other = isOtherSession(msg.session_id);
    var li = el('li', {
      class: 'row kind-' + msg.kind + (other ? ' other' : '') + (msg.spoken === false ? ' cut' : ''),
      dataset: { rowId: String(msg.row_id), sessionId: msg.session_id },
    });
    if (msg.sentence_id !== undefined && msg.sentence_id !== null) li.dataset.sentenceId = String(msg.sentence_id);
    var timeEl = el('time', { text: clock(msg.ts) });
    var meta = el('span', { class: 'row-meta' }, [
      other ? el('span', { class: 'row-session', text: sessionTag(msg.session_id) }) : null,
      timeEl,
    ]);
    // A spoken row shows Claude's own text (the raw lines, as written) up front; the
    // sentences as spoken sit in the hidden block the bubble opens on tap, each marked
    // when cut off. Rows without raw text (user, notices) show their text.
    var showRaw = msg.kind === 'spoken' && msg.raw_lines && msg.raw_lines.length;
    var first = el('span', { class: 'sentence' + (msg.spoken === false ? ' cut' : ''), text: msg.text, dataset: { rowId: String(msg.row_id) } });
    var rawEl = el('div', { class: 'row-raw', text: showRaw ? msg.raw_lines.join('\n') : '' });
    var textEl = el('span', { class: 'row-text' + (showRaw ? ' spoken-list' : '') }, [first]);
    var main = el('div', { class: 'row-main' }, [
      showRaw ? rawEl : textEl,
      el('span', { class: 'cut-mark', text: 'cut off' }),
      meta,
    ]);
    li.appendChild(main);
    // The block the bubble opens on tap: the spoken sentences for a spoken row (Claude's
    // text is up front), the raw lines for anything else that has them.
    var pre = null;
    if (msg.kind === 'spoken' || (msg.raw_lines && msg.raw_lines.length)) {
      pre = el('pre', { class: 'raw', hidden: true });
      if (showRaw) {
        pre.classList.add('spoken-block');
        pre.appendChild(el('span', { class: 'muted small', text: 'As spoken: ' }));
        pre.appendChild(textEl);
      } else {
        pre.textContent = (msg.raw_lines || []).join('\n');
      }
      li.appendChild(pre);
      li.classList.add('expandable');
      main.addEventListener('click', function () {
        var open = pre.hasAttribute('hidden');
        show(pre, open);
        li.classList.toggle('open', open);
      });
    }
    var rawSeen = {};
    (msg.raw_lines || []).forEach(function (line) { rawSeen[line] = true; });
    li._parts = { text: textEl, pre: pre, raw: showRaw ? rawEl : null, time: timeEl, first: first, rawSeen: rawSeen };
    return li;
  }

  // ---- what Zordon heard / the unsent draft (decision 0019) ---------------------

  var heardTimer = null;
  function onHeard(msg) {
    var box = $('heard');
    if (!box) return;
    $('heard-text').textContent = msg.text;
    box.classList.toggle('partial', !!msg.partial);
    show(box, true);
    if (heardTimer) clearTimeout(heardTimer);
    if (!msg.partial) heardTimer = setTimeout(function () { show(box, false); }, 8000);
  }

  // ---- sound cues (working / done / a question) ----------------------------------

  function cuesEnabled() {
    try {
      return localStorage.getItem('zordon.cues') !== 'off';
    } catch (_) {
      return true;
    }
  }
  function playCue(kind) {
    if (!cuesEnabled()) return;
    try {
      if (audio && typeof audio.cue === 'function') audio.cue(kind);
    } catch (_) {}
  }

  function modelLabel(id) {
    if (!id) return '';
    var m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(id);
    if (m) return m[1].charAt(0).toUpperCase() + m[1].slice(1) + ' ' + m[2] + '.' + m[3];
    return id.charAt(0).toUpperCase() + id.slice(1);
  }

  function renderWorkModel() {
    var el = $('work-model');
    if (!el) return;
    var s = S.focused ? S.sessionsById[S.focused] : null;
    var parts = [];
    if (s && s.model) parts.push(modelLabel(s.model));
    if (s && s.effort) parts.push(s.effort + ' effort');
    el.textContent = parts.join(' · ');
    show(el, parts.length > 0);
  }

  function renderWorkStatus() {
    var el = $('work-status');
    if (!el) return;
    var st = S.focused ? ((S.states[S.focused] || {}).state || (S.sessionsById[S.focused] || {}).state) : null;
    var text = st === 'working' ? 'working' : st && st.indexOf('awaiting') === 0 ? 'waiting for you' : st === 'stalled' ? 'quiet for a while' : '';
    $('work-status-text').textContent = text;
    el.classList.toggle('is-working', st === 'working');
    el.classList.toggle('is-waiting', !!st && st.indexOf('awaiting') === 0);
    show(el, !!text);
  }

  var draftEditedAt = 0;
  var draftEditTimer = null;
  function onDraft(msg) {
    var box = $('draft');
    if (!box) return;
    var ta = $('draft-text');
    if (msg.state === 'composing' && msg.text) {
      // The user may be typing in the box right now: do not stomp on their edit.
      if (Date.now() - draftEditedAt > 1500 || document.activeElement !== ta) ta.value = msg.text;
      show(box, true);
    } else {
      show(box, false);
      ta.value = '';
    }
  }

  function sendDraftEdit() {
    cmd('set_draft', { text: $('draft-text').value });
  }

  // Spoken sentences of one answer arrive one row at a time; on the page they belong in
  // one bubble. A sentence joins the previous bubble when that bubble is spoken, from the
  // same session, and nothing else (a user message, a notice) came between.
  var MERGE_GAP_S = 120;
  function mergeTarget(msg) {
    if (msg.kind !== 'spoken' || !S.rowOrder.length) return null;
    var last = S.rows[S.rowOrder[S.rowOrder.length - 1]];
    if (!last || last.msg.kind !== 'spoken' || last.msg.session_id !== msg.session_id) return null;
    if (typeof msg.ts === 'number' && typeof last.msg.ts === 'number' && msg.ts - last.msg.ts > MERGE_GAP_S) return null;
    return last.el;
  }

  function appendSentence(li, msg) {
    var parts = li._parts || {};
    var span = el('span', { class: 'sentence' + (msg.spoken === false ? ' cut' : ''), text: msg.text, dataset: { rowId: String(msg.row_id) } });
    if (msg.sentence_id !== undefined && msg.sentence_id !== null) span.dataset.sentenceId = String(msg.sentence_id);
    if (parts.text) {
      parts.text.appendChild(el('span', { class: 'gap', text: ' ' }));
      parts.text.appendChild(span);
    }
    if (msg.raw_lines && msg.raw_lines.length) {
      // Sentences of one paragraph each carry the whole paragraph as their raw text; the
      // bubble shows every raw line once, in order.
      var fresh = [];
      for (var i = 0; i < msg.raw_lines.length; i++) {
        var line = msg.raw_lines[i];
        if (!parts.rawSeen[line]) {
          parts.rawSeen[line] = true;
          fresh.push(line);
        }
      }
      if (fresh.length) {
        if (parts.raw) parts.raw.textContent += (parts.raw.textContent ? '\n' : '') + fresh.join('\n');
        else if (parts.pre) parts.pre.textContent += (parts.pre.textContent ? '\n' : '') + fresh.join('\n');
      }
    }
    if (parts.time) parts.time.textContent = clock(msg.ts);
    return span;
  }

  function onTranscript(msg) {
    var existing = S.rows[msg.row_id];
    var wasAtBottom = feedAtBottom();
    if (existing) {
      if (existing.span) {
        existing.span.textContent = msg.text;
        existing.span.classList.toggle('cut', msg.spoken === false);
        existing.msg = msg;
        return;
      }
      var fresh = buildRow(msg);
      existing.el.parentNode.replaceChild(fresh, existing.el);
      existing.el = fresh;
      existing.msg = msg;
      return;
    }
    if (msg.kind === 'user') reconcileLocalEcho(msg);
    if (msg.kind === 'notice' && /^Start a new project:/.test(msg.text || '')) openNewProject();
    var target = mergeTarget(msg);
    if (target) {
      var span = appendSentence(target, msg);
      S.rows[msg.row_id] = { el: target, msg: msg, span: span };
      S.rowOrder.push(msg.row_id);
      if (wasAtBottom) scrollFeedToBottom();
      return;
    }
    var li = buildRow(msg);
    $('rows').appendChild(li);
    S.rows[msg.row_id] = { el: li, msg: msg };
    S.rowOrder.push(msg.row_id);
    pruneRows();
    show($('feed-empty'), false);
    if (wasAtBottom || msg.kind === 'user') {
      scrollFeedToBottom();
    } else {
      S.unseen++;
      renderJump();
    }
  }

  function pruneRows() {
    while (S.rowOrder.length > MAX_ROWS) {
      var id = S.rowOrder.shift();
      var r = S.rows[id];
      if (r && r.el.parentNode) r.el.parentNode.removeChild(r.el);
      delete S.rows[id];
    }
  }

  function markCutOff(sentenceId) {
    var ids = Object.keys(S.rows);
    for (var i = 0; i < ids.length; i++) {
      var r = S.rows[ids[i]];
      if (r.msg.sentence_id === sentenceId) {
        r.msg.spoken = false;
        // The sentence itself is marked (a bubble holds a whole answer); the bubble gets
        // the class too only when its first sentence is the one cut.
        var span = r.span || (r.el._parts && r.el._parts.first);
        if (span) span.classList.add('cut');
        if (!r.span) r.el.classList.add('cut');
      }
    }
  }

  function addLocalUserRow(text) {
    var id = 'local-' + ++S.localSeq;
    var li = el('li', { class: 'row kind-user pending', dataset: { rowId: id } }, [
      el('div', { class: 'row-main' }, [
        el('span', { class: 'row-text', text: text }),
        el('span', { class: 'row-meta' }, [el('time', { text: clock() })]),
      ]),
    ]);
    $('rows').appendChild(li);
    S.pendingLocal.push({ id: id, text: text, el: li, ts: Date.now() });
    show($('feed-empty'), false);
    scrollFeedToBottom();
    // Keep the local echo even if the agent never mirrors it: after a while it becomes an
    // ordinary row (subject to the row cap) and stops waiting for a server copy.
    setTimeout(function () {
      li.classList.remove('pending');
      settleLocalRow(id);
    }, LOCAL_ECHO_SETTLE_MS);
  }

  function settleLocalRow(id) {
    for (var i = 0; i < S.pendingLocal.length; i++) {
      var p = S.pendingLocal[i];
      if (p.id !== id) continue;
      S.pendingLocal.splice(i, 1);
      if (!p.el.parentNode) return;
      S.rows[id] = { el: p.el, msg: { row_id: id, session_id: S.focused || '', kind: 'user', text: p.text, ts: p.ts / 1000 } };
      S.rowOrder.push(id);
      pruneRows();
      return;
    }
  }

  function reconcileLocalEcho(msg) {
    var text = msg.text.trim();
    for (var i = 0; i < S.pendingLocal.length; i++) {
      var p = S.pendingLocal[i];
      if (p.text.trim() === text) {
        if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
        S.pendingLocal.splice(i, 1);
        return;
      }
    }
  }

  // ---- prompt cards -------------------------------------------------------------------

  function questionLine(msg) {
    var lines = msg.raw_lines || [];
    for (var i = lines.length - 1; i >= 0; i--) {
      var t = lines[i].replace(/^[\s❯>]+/, '').trim();
      if (!t || /^\d+\.\s/.test(t)) continue;
      if (t.slice(-1) === '?') return t;
    }
    return '';
  }

  function promptKindLabel(kind) {
    return { permission: 'Permission', plan: 'Plan approval', question: 'Question', trust: 'Trust this folder?' }[kind] || kind;
  }

  function buildPromptCard(msg) {
    var card = el('div', { class: 'prompt kind-' + msg.kind, dataset: { promptId: String(msg.prompt_id) } });
    var other = isOtherSession(msg.session_id);
    card.appendChild(
      el('div', { class: 'p-head' }, [
        el('span', { class: 'p-kind', text: promptKindLabel(msg.kind) }),
        el('span', { class: 'p-session', text: sessionTag(msg.session_id) + (other ? ' (background)' : '') }),
      ]),
    );
    if (msg.title) card.appendChild(el('h3', { class: 'p-title', text: msg.title }));
    var q = questionLine(msg);
    if (q && q !== msg.title) card.appendChild(el('p', { class: 'p-question', text: q }));

    if (msg.kind === 'trust') {
      card.appendChild(
        el('p', {
          class: 'p-warn',
          text:
            'Trusting lets Claude Code read, edit and run files in this folder. Only trust a project you created or have reviewed.',
        }),
      );
    }

    if (msg.options && msg.options.length && msg.kind !== 'question') {
      var ul = el('ul', { class: 'p-options' });
      msg.options.forEach(function (opt) {
        var unsafe = P.isUnsafeOption(opt);
        ul.appendChild(
          el('li', { class: unsafe ? 'unsafe' : '' }, [
            el('span', { text: opt }),
            unsafe ? el('em', { text: ' not offered by Zordon' }) : null,
          ]),
        );
      });
      card.appendChild(ul);
    }

    if (msg.raw_lines && msg.raw_lines.length) {
      card.appendChild(
        el('details', { class: 'p-raw' }, [
          el('summary', { text: 'Terminal lines' }),
          el('pre', { text: msg.raw_lines.join('\n') }),
        ]),
      );
    }

    var actions = el('div', { class: 'p-actions' });
    var base = { session_id: msg.session_id, prompt_id: msg.prompt_id };
    function action(label, cls, fn) {
      actions.appendChild(
        el('button', {
          type: 'button',
          class: 'btn ' + cls,
          text: label,
          onclick: function () {
            if (fn() !== false) settle(card);
          },
        }),
      );
    }
    switch (msg.kind) {
      case 'permission':
        action('Yes', 'primary', function () {
          return cmd('approve', base);
        });
        action('No', 'danger', function () {
          return cmd('deny', base);
        });
        break;
      case 'plan':
        action('Approve (manual edits)', 'primary', function () {
          return cmd('plan_approve', base);
        });
        action('Revise...', '', function () {
          askRevision(function (text) {
            if (cmd('plan_revise', Object.assign({ text: text }, base))) settle(card);
          });
          return false;
        });
        action('Deny', 'danger', function () {
          return cmd('plan_deny', base);
        });
        break;
      case 'question':
        (msg.options || []).forEach(function (opt, i) {
          action(opt, i === 0 ? 'primary' : '', function () {
            return cmd('answer', Object.assign({ option: i + 1, label: opt }, base));
          });
        });
        break;
      case 'trust':
        action('Trust this folder', 'primary', function () {
          return cmd('approve', base);
        });
        action("Don't trust", 'danger', function () {
          return cmd('deny', base);
        });
        break;
      default:
        break;
    }
    card.appendChild(actions);
    return card;
  }

  // Disable the buttons once an answer was sent; re-enable if the card is still here later.
  function settle(card) {
    var buttons = card.querySelectorAll('button');
    buttons.forEach(function (b) {
      b.disabled = true;
    });
    card.classList.add('settling');
    setTimeout(function () {
      if (!card.parentNode) return;
      buttons.forEach(function (b) {
        b.disabled = false;
      });
      card.classList.remove('settling');
    }, 8000);
  }

  function onPrompt(msg) {
    if (msg.cleared) {
      removePrompt(msg.prompt_id);
      return;
    }
    var card = buildPromptCard(msg);
    var existing = S.prompts[msg.prompt_id];
    if (existing) existing.el.parentNode.replaceChild(card, existing.el);
    else $('prompts').appendChild(card);
    S.prompts[msg.prompt_id] = { el: card, msg: msg };
    if (navigator.vibrate) {
      try {
        navigator.vibrate(30);
      } catch (_) {
        /* ignore */
      }
    }
  }

  function removePrompt(promptId) {
    var p = S.prompts[promptId];
    if (!p) return;
    if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
    delete S.prompts[promptId];
  }

  function askRevision(done) {
    var dlg = $('revise-dialog');
    var ta = $('revise-text');
    ta.value = '';
    if (!dlg.showModal) {
      var t = window.prompt('What should change in the plan?');
      if (t && t.trim()) done(t.trim());
      return;
    }
    dlg.onclose = function () {
      dlg.onclose = null;
      if (dlg.returnValue === 'ok' && ta.value.trim()) done(ta.value.trim());
    };
    dlg.showModal();
    ta.focus();
  }

  function confirmDialog(title, text, okLabel, done) {
    var dlg = $('confirm-dialog');
    if (!dlg.showModal) {
      if (window.confirm(text)) done();
      return;
    }
    $('confirm-title').textContent = title;
    $('confirm-text').textContent = text;
    $('confirm-ok').textContent = okLabel;
    dlg.onclose = function () {
      dlg.onclose = null;
      if (dlg.returnValue === 'ok') done();
    };
    dlg.showModal();
  }

  // ---- session picker ---------------------------------------------------------------------

  function sortedSessions() {
    return S.sessions.slice().sort(function (a, b) {
      if (a.focused !== b.focused) return a.focused ? -1 : 1;
      if (a.attached !== b.attached) return a.attached ? -1 : 1;
      return (b.last_active || 0) - (a.last_active || 0);
    });
  }

  function renderSessions() {
    var list = $('session-list');
    clear(list);
    var items = sortedSessions();
    show($('sessions-empty'), items.length === 0);
    items.forEach(function (s) {
      var st = (S.states[s.session_id] || {}).state || s.state;
      var full = !!S.expandedDirs[s.session_id];
      var li = el('li', { class: 'session' + (s.focused ? ' focused' : ''), dataset: { id: s.session_id } });
      var dirBtn = el('button', {
        type: 'button',
        class: 's-dir' + (full ? ' full' : ''),
        title: s.directory,
        text: full ? s.directory : basename(s.directory),
        onclick: function () {
          S.expandedDirs[s.session_id] = !full;
          renderSessions();
        },
      });
      li.appendChild(
        el('div', { class: 's-main' }, [
          el('div', { class: 's-title', text: s.title || basename(s.directory) || s.session_id }),
          dirBtn,
          el('div', { class: 's-meta' }, [
            el('span', { class: 'chip ' + stateClass(st), text: stateLabel(st) }),
            s.attached ? el('span', { class: 'badge', text: 'attached' }) : null,
            s.running ? el('span', { class: 'badge', text: 'running' }) : null,
            s.permission_mode ? el('span', { class: 'badge mode', text: s.permission_mode }) : null,
            el('span', { class: 'badge agent', text: agentLabel(s.agent || 'claude-code') }),
            el('span', { class: 'muted small', text: relTime(s.last_active) }),
          ]),
        ]),
      );
      var actions = el('div', { class: 's-actions' });
      if (!s.focused) {
        actions.appendChild(
          el('button', {
            type: 'button',
            class: 'btn small primary',
            text: 'Focus',
            onclick: function () {
              if (cmd('focus', { session_id: s.session_id })) closeSheets();
            },
          }),
        );
      }
      if (!s.running || !s.attached) {
        actions.appendChild(
          el('button', {
            type: 'button',
            class: 'btn small',
            text: 'Resume',
            onclick: function () {
              cmd('resume', { session_id: s.session_id });
            },
          }),
        );
      }
      if (s.attached) {
        actions.appendChild(
          el('button', {
            type: 'button',
            class: 'btn small',
            text: 'Detach',
            onclick: function () {
              cmd('detach', { session_id: s.session_id });
            },
          }),
        );
      }
      actions.appendChild(
        el('button', {
          type: 'button',
          class: 'btn small danger',
          text: 'Delete',
          onclick: function () {
            confirmDialog(
              'Delete session?',
              'This kills the tmux pane for "' + (s.title || basename(s.directory)) + '". The Claude Code session history stays on disk.',
              'Delete',
              function () {
                cmd('delete', { session_id: s.session_id, confirm: true });
              },
            );
          },
        }),
      );
      li.appendChild(actions);
      list.appendChild(li);
    });
  }

  function renderFocus() {
    var s = S.focused ? S.sessionsById[S.focused] : null;
    var proj = focusedProject();
    var title = proj ? proj.name : s ? s.title || basename(s.directory) : S.focused ? S.focused.slice(0, 8) : 'Projects';
    $('focus-title').textContent = title;
    var chip = $('focus-state');
    var st = S.focused ? (S.states[S.focused] || {}).state || (s && s.state) : null;
    chip.className = 'chip ' + (S.connected ? stateClass(st) : 'state-detached');
    // In admin mode (nothing focused) there is no state to show; "NONE" reads like a fault.
    chip.textContent = S.connected ? (st ? stateLabel(st) : '') : 'offline';
    chip.hidden = S.connected && !st;
    var stLine = $('st-state');
    if (!S.focused) stLine.textContent = 'no project open';
    else {
      var info = S.states[S.focused];
      stLine.textContent = info
        ? stateLabel(info.state) + (info.detail ? ' - ' + info.detail : '') + ' (' + relTime(info.ts) + ')'
        : s
          ? stateLabel(s.state)
          : 'unknown';
    }
    document.body.dataset.state = st || 'none';
    renderWorkStatus();
    renderWorkModel();
    renderView();
    // Rows from the focused session are no longer "other"; cheap to recompute.
    Object.keys(S.rows).forEach(function (id) {
      var r = S.rows[id];
      r.el.classList.toggle('other', isOtherSession(r.msg.session_id));
    });
  }

  // ---- settings ----------------------------------------------------------------------------

  // Fill a <select> with `values`. When `current` is not one of them, either show it
  // as its own option or, with `otherLabel`, a disabled placeholder (so a value the
  // client refuses to offer is never rendered as a choice). An empty `current` with
  // `otherLabel` shows a disabled "unknown" placeholder instead of a misleading default.
  // Adapter keys as shown to people. Unknown keys fall back to the key itself.
  var AGENT_LABELS = { 'claude-code': 'Claude Code', 'claude-headless': 'Claude Code (headless)', codex: 'Codex', generic: 'Generic pane' };

  function agentLabel(key) {
    return AGENT_LABELS[key] || key;
  }

  // Order: the configured default first, then installed adapters, then the rest.
  function agentKeys() {
    var keys = Object.keys(S.agents || {});
    if (keys.indexOf(S.defaultAgent) === -1) keys.push(S.defaultAgent);
    return keys.sort(function (a, b) {
      if (a === S.defaultAgent) return -1;
      if (b === S.defaultAgent) return 1;
      var ia = S.agents[a] ? 0 : 1;
      var ib = S.agents[b] ? 0 : 1;
      return ia - ib || a.localeCompare(b);
    });
  }

  // Fill an agent <select>: installed adapters enabled, the others disabled and labelled.
  function fillAgentSelect(select, current) {
    var keys = agentKeys();
    clear(select);
    var chosen = null;
    keys.forEach(function (k) {
      var installed = S.agents[k] !== false;
      var opt = el('option', { value: k, text: agentLabel(k) + (installed ? '' : ' (not installed)') });
      if (!installed) opt.disabled = true;
      select.appendChild(opt);
      if (chosen === null && installed && (k === current || current === undefined)) chosen = k;
    });
    if (chosen === null) chosen = keys.filter(function (k) { return S.agents[k] !== false; })[0] || keys[0] || '';
    if (chosen) select.value = chosen;
  }

  function renderAgentSelects() {
    fillAgentSelect($('new-agent'), S.defaultAgent);
    fillAgentSelect($('attach-agent'), 'generic');
  }

  function fillSelect(select, values, current, otherLabel) {
    clear(select);
    var seen = {};
    values.forEach(function (v) {
      seen[v] = true;
      select.appendChild(el('option', { value: v, text: v }));
    });
    if (current && seen[current]) {
      select.value = current;
      return;
    }
    if (otherLabel) {
      var label = current ? otherLabel : 'unknown';
      select.appendChild(el('option', { value: '__other__', text: label, disabled: true }));
      select.value = '__other__';
      return;
    }
    if (current) {
      select.appendChild(el('option', { value: current, text: current }));
      select.value = current;
    }
  }

  function renderSettings() {
    var st = S.settings;
    $('set-verbosity').value = st.verbosity || 'minimal';
    $('set-tool-chatter').checked = !!st.tool_chatter;
    $('set-agent-mute').checked = !!st.muted;
    $('set-speaker-mute').checked = S.speakerMuted;
    var prov = st.providers || {};
    fillSelect($('set-stt'), P.STT_PROVIDERS, prov.stt || '');
    fillSelect($('set-tts'), P.TTS_PROVIDERS, prov.tts || '');
    fillSelect($('set-voice'), KOKORO_VOICES, prov.voice || prov.tts_voice || '');
    var focusedSession = S.focused ? S.sessionsById[S.focused] : null;
    var mode = st.permission_mode || (focusedSession && focusedSession.permission_mode) || '';
    fillSelect($('set-mode'), P.PERMISSION_MODES, mode, 'other (set in Claude Code)');
    renderLatency();
    renderAudioStatus();
  }

  function renderLatency() {
    $('st-rtt').textContent = S.rttMs === null ? '-' : S.rttMs + ' ms round trip';
    var parts = [];
    if (S.rttMs !== null) parts.push('about ' + (S.rttMs + VAD_ONSET_MS) + ' ms (ping + ' + VAD_ONSET_MS + ' ms onset)');
    if (S.lastFlushMs !== null) parts.push('last flush ' + S.lastFlushMs.toFixed(1) + ' ms');
    $('st-bargein').textContent = parts.length ? parts.join(', ') : '-';
  }

  function renderAudioStatus() {
    var bits = [];
    bits.push('context ' + audio.contextState);
    if (audio.contextSampleRate) bits.push(Math.round(audio.contextSampleRate / 1000) + ' kHz');
    if (S.callActive) bits.push(audio.captureActive ? 'capturing' : S.micMuted ? 'mic muted' : 'paused');
    if (audio.playbackActive) bits.push('speaking');
    bits.push(audio.stats.framesSent + ' frames sent');
    if (audio.stats.chunksDropped) bits.push(audio.stats.chunksDropped + ' stale chunks dropped');
    $('st-audio').textContent = bits.join(', ');
  }

  function setConn(text) {
    $('st-conn').textContent = text;
    var dot = $('conn-dot');
    dot.className = 'dot ' + (S.connected ? 'conn-online' : text === 'connecting' ? 'conn-connecting' : 'conn-offline');
    dot.setAttribute('aria-label', text);
    dot.title = text;
    renderFocus();
  }

  // ---- sheets ---------------------------------------------------------------------------------

  function openSheet(id) {
    closeSheets();
    show($(id), true);
    document.body.classList.add('sheet-open');
    if (id === 'sessions' && S.connected) cmd('list_sessions');
  }

  function closeSheets() {
    show($('sessions'), false);
    show($('settings'), false);
    show($('new-project'), false);
    S.np.open = false;
    document.body.classList.remove('sheet-open');
  }

  // ---- projects (decision 0018) -----------------------------------------------------------------

  function onError(msg) {
    // A walkthrough error belongs inline, next to what the user just did.
    if (S.np.open && S.np.pending) {
      S.np.pending = null;
      npError(msg.message);
      return;
    }
    toast(msg.message, 'error');
  }

  function onProjects(msg) {
    S.projects = msg.projects.slice();
    S.focusedProject = msg.focused_project || null;
    if (S.np.open && S.np.pending === 'create_project') {
      // The server answered with sessions + projects: the project exists and is focused.
      S.np.pending = null;
      closeSheets();
    }
    renderProjects();
    renderFocus();
  }

  function focusedProject() {
    if (!S.focused) return null;
    for (var i = 0; i < S.projects.length; i++) {
      var p = S.projects[i];
      if (p.focused || p.session_id === S.focused) return p;
    }
    return null;
  }

  function shortPath(path) {
    var p = String(path || '');
    var home = S.home || '';
    if (home && (p === home || p.indexOf(home.replace(/\/+$/, '') + '/') === 0)) return '~' + p.slice(home.replace(/\/+$/, '').length);
    return p;
  }

  function modeWord(mode) {
    return MODE_WORDS[mode] || mode || '';
  }

  // Which main view: the projects view whenever nothing is focused, else the work view.
  function renderView() {
    var work = !!S.focused;
    show($('projects-view'), !work);
    show($('feed'), work);
    show($('work-head'), work);
    show($('composer'), work);
    show($('jump'), work && !S.atBottom && S.unseen > 0);
    document.body.classList.toggle('view-projects', !work);
    document.body.classList.toggle('view-work', work);
    if (work) {
      var proj = focusedProject();
      var s = S.sessionsById[S.focused];
      $('work-title').textContent = proj ? proj.name : s ? s.title || basename(s.directory) : S.focused.slice(0, 8);
      var mode = (proj && proj.permission_mode) || (s && s.permission_mode) || '';
      $('work-mode').textContent = modeWord(mode);
      show($('work-mode'), !!mode);
    }
  }

  function renderProjects() {
    var list = $('project-list');
    clear(list);
    var items = S.projects.slice();
    show($('projects-empty'), items.length === 0);
    show($('projects-section'), true);
    items.forEach(function (p) {
      var li = el('li', { class: 'project' + (p.focused ? ' focused' : '') + (p.exists === false ? ' missing' : ''), dataset: { id: p.id } });
      var badges = [];
      if (p.exists === false) badges.push(el('span', { class: 'badge missing', text: 'Folder missing' }));
      else if (p.running) badges.push(el('span', { class: 'badge running', text: p.focused ? 'Open' : 'Running' }));
      else badges.push(el('span', { class: 'badge', text: 'Paused' }));
      if (p.permission_mode) badges.push(el('span', { class: 'badge mode' + (p.permission_mode === 'bypassPermissions' ? ' danger' : ''), text: modeWord(p.permission_mode) }));
      if (p.agent && p.agent !== S.defaultAgent) badges.push(el('span', { class: 'badge agent', text: agentLabel(p.agent) }));
      var open = el(
        'button',
        {
          type: 'button',
          class: 'project-open',
          disabled: p.exists === false,
          'aria-label': 'Open ' + p.name,
          onclick: function () {
            openProject(p);
          },
        },
        [
          el('span', { class: 'project-name', text: p.name }),
          el('span', { class: 'project-dir', text: shortPath(p.directory) }),
          el('span', { class: 'project-meta' }, badges.concat([el('span', { class: 'muted small', text: relTime(p.last_used) })])),
        ],
      );
      li.appendChild(open);
      li.appendChild(
        el('button', {
          type: 'button',
          class: 'link-btn project-forget',
          text: 'Forget',
          onclick: function () {
            confirmDialog('Forget this project?', 'Files stay where they are. Zordon just stops listing "' + p.name + '".', 'Forget', function () {
              cmd('forget_project', { project_id: p.id, confirm: true });
            });
          },
        }),
      );
      list.appendChild(li);
    });
  }

  function openProject(p) {
    if (cmd('open_project', { project_id: p.id })) toast('Opening ' + p.name, 'info', 2000);
  }

  function goToProjects() {
    // Pause: focus nothing on the server; every pane keeps running.
    closeSheets();
    if (S.focused) cmd('admin');
    else renderView();
  }

  // ---- the new-project walkthrough ----------------------------------------------------------

  var NP_STEPS = ['where', 'agent', 'ask', 'ready'];

  function installedAgents() {
    // Assistants a project can be *started* with. The generic adapter only attaches to a
    // pane somebody else started, so it is not a choice here (it stays under Advanced).
    // The headless runner is a way of running Claude Code, chosen by the switch, not an assistant.
    return agentKeys().filter(function (k) {
      return k !== 'generic' && k !== 'claude-headless' && S.agents[k] !== false;
    });
  }

  function npNeedsAgentStep() {
    return installedAgents().length > 1;
  }

  function openNewProject() {
    var np = S.np;
    np.open = true;
    np.step = 'where';
    np.folder = null;
    np.existing = false;
    np.name = '';
    np.agent = S.defaultAgent;
    np.mode = 'default';
    np.scope = true;
    np.talk = true;
    np.headless = false;
    np.pending = null;
    np.listing = null;
    $('np-folder-name').value = '';
    $('np-scope').checked = true;
    $('np-talk').checked = true;
    $('np-headless').checked = false;
    npError('');
    closeSheets();
    show($('new-project'), true);
    np.open = true;
    document.body.classList.add('sheet-open');
    renderNp();
    npBrowse(null);
  }

  function npBrowse(path) {
    S.np.path = path;
    S.np.pending = 'browse';
    var args = path ? { path: path } : {};
    if (!cmd('browse', args)) S.np.pending = null;
  }

  function onBrowse(msg) {
    if (!S.np.open) return;
    if (S.np.pending === 'browse') S.np.pending = null;
    S.np.listing = msg;
    S.np.path = msg.path;
    if (!S.home) S.home = msg.home;
    renderNp();
  }

  function npError(text) {
    var box = $('np-error');
    box.textContent = text || '';
    show(box, !!text);
  }

  function npSetStep(step) {
    S.np.step = step;
    npError('');
    renderNp();
  }

  function npNext() {
    var np = S.np;
    if (np.step === 'where') {
      if (!np.folder) {
        npError('Pick a folder first: create a new one or use the one you are in.');
        return;
      }
      npSetStep(npNeedsAgentStep() ? 'agent' : 'ask');
    } else if (np.step === 'agent') {
      npSetStep('ask');
    } else if (np.step === 'ask') {
      npSetStep('ready');
    }
  }

  function npBack() {
    var np = S.np;
    if (np.step === 'ready') npSetStep('ask');
    else if (np.step === 'ask') npSetStep(npNeedsAgentStep() ? 'agent' : 'where');
    else if (np.step === 'agent') npSetStep('where');
    else closeSheets();
  }

  // "Create a new folder here": the server makes it (inside home only) when the project starts;
  // the walkthrough just remembers the choice and moves on.
  function npCreateFolder() {
    var name = $('np-folder-name').value.trim();
    var listing = S.np.listing;
    if (!listing) return;
    if (!name) {
      npError('Give the new folder a name.');
      return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$/.test(name)) {
      npError('Use letters, numbers, spaces, dots, dashes or underscores, starting with a letter or number.');
      return;
    }
    var taken = (listing.entries || []).some(function (e) {
      return e.name.toLowerCase() === name.toLowerCase();
    });
    if (taken) {
      npError('There is already a folder called ' + name + ' here. Open it and tap "Use this folder", or pick another name.');
      return;
    }
    S.np.folder = { parent: listing.path, name: name, existing: false, display: shortPath(listing.path.replace(/\/+$/, '') + '/' + name) };
    S.np.name = name;
    npNext();
  }

  function npUseFolder() {
    var listing = S.np.listing;
    if (!listing) return;
    var here = listing.path;
    if (S.home && here.replace(/\/+$/, '') === S.home.replace(/\/+$/, '')) {
      npError('Your whole home directory is too big for one project. Open or create a folder inside it.');
      return;
    }
    var proj = projectAtPath(here);
    if (proj) {
      npError('This folder is already the project "' + proj.name + '". Continue it from the projects list instead.');
      return;
    }
    S.np.folder = { parent: here, name: '', existing: true, display: shortPath(here) };
    S.np.name = basename(here);
    npNext();
  }

  function projectAtPath(path) {
    var want = String(path || '').replace(/\/+$/, '');
    for (var i = 0; i < S.projects.length; i++) {
      if (String(S.projects[i].directory).replace(/\/+$/, '') === want) return S.projects[i];
    }
    return null;
  }

  function npStart() {
    var np = S.np;
    if (!np.folder) {
      npSetStep('where');
      return;
    }
    var args = { parent: np.folder.parent, name: np.folder.name || np.name, permission_mode: np.mode, scope_edits: !!np.scope, talk_first: np.talk !== false, runner: np.headless ? 'headless' : 'terminal' };
    if (np.folder.existing) args.existing = true;
    if (np.agent && np.agent !== S.defaultAgent) args.agent = np.agent;
    np.pending = 'create_project';
    npError('');
    $('np-start').disabled = true;
    if (!cmd('create_project', args)) {
      np.pending = null;
      $('np-start').disabled = false;
      return;
    }
    toast('Starting ' + (np.folder.name || np.name) + '...', 'info', 2500);
    setTimeout(function () {
      $('np-start').disabled = false;
    }, 4000);
  }

  function renderNp() {
    var np = S.np;
    if (!np.open) return;
    var steps = NP_STEPS.filter(function (s) {
      return s !== 'agent' || npNeedsAgentStep();
    });
    var idx = steps.indexOf(np.step);
    $('np-step-label').textContent = 'Step ' + (idx + 1) + ' of ' + steps.length;
    NP_STEPS.forEach(function (s) {
      show($('np-step-' + s), s === np.step);
    });
    show($('np-next'), np.step !== 'ready' && np.step !== 'where');
    show($('np-start'), np.step === 'ready');
    $('np-back').textContent = np.step === 'where' ? 'Cancel' : 'Back';

    if (np.step === 'where') renderNpWhere();
    if (np.step === 'agent') renderNpAgents();
    if (np.step === 'ask') renderNpModes();
    if (np.step === 'ready') renderNpSummary();
  }

  function renderNpWhere() {
    var listing = S.np.listing;
    var folders = $('np-folders');
    clear(folders);
    if (!listing) {
      $('np-crumb').textContent = 'Loading folders...';
      show($('np-up'), false);
      show($('np-folders-empty'), false);
      return;
    }
    $('np-crumb').textContent = shortPath(listing.path) || '~';
    show($('np-up'), !!listing.parent);
    show($('np-folders-empty'), listing.entries.length === 0);
    listing.entries.forEach(function (e) {
      var proj = e.project_id ? projectById(e.project_id) : null;
      var btn = el(
        'button',
        {
          type: 'button',
          class: 'np-folder' + (proj ? ' is-project' : ''),
          onclick: function () {
            npBrowse(e.path);
          },
        },
        [
          el('span', { class: 'np-folder-name', text: e.name }),
          proj ? el('span', { class: 'badge', text: 'already a project' }) : e.has_git ? el('span', { class: 'badge', text: 'git' }) : null,
        ],
      );
      folders.appendChild(el('li', null, btn));
    });
    var canCreate = listing.can_create !== false;
    $('np-create-folder').disabled = !canCreate;
    $('np-folder-name').disabled = !canCreate;
    var atHome = !listing.parent;
    $('np-use-folder').disabled = atHome || !!projectAtPath(listing.path);
    $('np-use-folder').textContent = atHome ? 'Use this folder (open a folder first)' : 'Use this folder: ' + (basename(listing.path) || '~');
  }

  function projectById(id) {
    for (var i = 0; i < S.projects.length; i++) if (S.projects[i].id === id) return S.projects[i];
    return null;
  }

  function choiceList(ul, items, current, onPick) {
    clear(ul);
    items.forEach(function (it) {
      var on = it.value === current;
      ul.appendChild(
        el(
          'li',
          null,
          el(
            'button',
            {
              type: 'button',
              class: 'np-choice' + (on ? ' on' : '') + (it.danger ? ' danger' : ''),
              role: 'radio',
              'aria-checked': on ? 'true' : 'false',
              dataset: { value: it.value },
              onclick: function () {
                onPick(it.value);
              },
            },
            [el('span', { class: 'np-choice-label', text: it.label }), it.help ? el('span', { class: 'np-choice-help muted small', text: it.help }) : null],
          ),
        ),
      );
    });
  }

  function renderNpAgents() {
    var items = installedAgents().map(function (k) {
      return { value: k, label: agentLabel(k), help: k === S.defaultAgent ? 'Your default' : '' };
    });
    if (items.every(function (i) { return i.value !== S.np.agent; })) S.np.agent = S.defaultAgent;
    choiceList($('np-agents'), items, S.np.agent, function (v) {
      S.np.agent = v;
      renderNp();
    });
  }

  function renderNpModes() {
    var items = NP_MODES.map(function (m) {
      return { value: m.value, label: m.label, help: m.help, danger: m.value === 'bypassPermissions' };
    });
    choiceList($('np-modes'), items, S.np.mode, function (v) {
      S.np.mode = v;
      renderNp();
    });
    show($('np-bypass-warn'), S.np.mode === 'bypassPermissions');
    $('np-scope').checked = !!S.np.scope;
    $('np-talk').checked = S.np.talk !== false;
    $('np-headless').checked = !!S.np.headless;
    var canHeadless = (S.np.agent || S.defaultAgent) === 'claude-code';
    $('np-headless').disabled = !canHeadless;
    if (!canHeadless) S.np.headless = false;
  }

  function renderNpSummary() {
    var np = S.np;
    var dl = $('np-summary');
    clear(dl);
    function row(k, v) {
      dl.appendChild(el('dt', { text: k }));
      dl.appendChild(el('dd', { text: v }));
    }
    row('Folder', np.folder ? np.folder.display : '-');
    row('Assistant', agentLabel(np.agent || S.defaultAgent));
    var m = null;
    NP_MODES.forEach(function (x) { if (x.value === np.mode) m = x; });
    row('Permissions', m ? m.label : modeWord(np.mode));
    row('File edits', np.scope ? 'kept inside the folder' : 'anywhere the agent is allowed');
    row('Before acting', np.talk !== false ? 'asks and says its plan first' : 'gets to work');
    row('Runs', np.headless ? 'headless, no terminal' : 'in a terminal pane');
  }

  // ---- composer, uploads --------------------------------------------------------------------------

  function autosize(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
  }

  function submitText() {
    var ta = $('text');
    var text = ta.value.trim();
    if (!text) return;
    var ok;
    if ($('raw-send').checked) ok = cmd('send_text', { text: text });
    else {
      try {
        ok = send(P.buildText(text));
      } catch (e) {
        toast(e.message, 'error');
        return;
      }
      if (!ok) toast('Not connected', 'error');
    }
    if (!ok) return;
    addLocalUserRow(text);
    ta.value = '';
    autosize(ta);
  }

  function insertIntoText(fragment) {
    var ta = $('text');
    var cur = ta.value;
    ta.value = cur + (cur && !/\s$/.test(cur) ? ' ' : '') + fragment + ' ';
    autosize(ta);
    ta.focus();
  }

  function uploadFiles(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return;
    list.forEach(function (file) {
      uploadOne(file);
    });
  }

  function uploadOne(file) {
    var status = $('upload-status');
    if (file.size > UPLOAD_MAX_BYTES) {
      toast(file.name + ' is larger than ' + Math.round(UPLOAD_MAX_BYTES / (1024 * 1024)) + ' MB', 'error');
      return Promise.resolve();
    }
    S.uploading++;
    show(status, true);
    status.textContent = 'Uploading ' + file.name + '...';
    cmd('upload', { name: file.name, size: file.size });
    var fd = new FormData();
    fd.append('file', file, file.name);
    return fetch('/upload', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('upload failed (' + r.status + ')');
        return r.json();
      })
      .then(function (data) {
        var p = data && (data.path || data.file || data.filename);
        if (!p) throw new Error('upload response had no path');
        insertIntoText(p);
        toast('Uploaded ' + file.name, 'info', 2500);
      })
      .catch(function (e) {
        toast(file.name + ': ' + e.message, 'error');
      })
      .then(function () {
        S.uploading--;
        if (S.uploading <= 0) {
          S.uploading = 0;
          show(status, false);
        }
      });
  }

  // ---- call controls -----------------------------------------------------------------------------

  var audio = new Audio({
    send: send,
    workletUrl: 'worklet.js',
    onFrame: onMicFrame,
    onPlaybackChange: function (active) {
      document.body.classList.toggle('speaking', active);
      renderAudioStatus();
    },
    onCaptureChange: function () {
      updateCallUI();
    },
    onPlaybackBlocked: function () {
      toast('Tap anywhere to enable audio playback', 'warn', 6000);
    },
    onCaptureEnded: function (reason) {
      S.callActive = false;
      send(P.buildCall('end'));
      updateCallUI();
      toast('Call ended: ' + reason, 'warn');
    },
    log: function (line) {
      if (window.console) console.log('[audio] ' + line);
    },
  });

  var lastLevelPaint = 0;
  function onMicFrame(peak) {
    var now = Date.now();
    if (now - lastLevelPaint < 80) return;
    lastLevelPaint = now;
    var pct = Math.min(100, Math.round((peak / 32768) * 140));
    $('mic-level').style.width = pct + '%';
  }

  function toggleCall() {
    if (S.callActive) {
      endCall();
      return;
    }
    var btn = $('btn-talk');
    btn.disabled = true;
    $('talk-label').textContent = 'Starting';
    audio
      .startCall()
      .then(function () {
        S.callActive = true;
        S.pausedByVisibility = false;
        send(P.buildCall('start'));
        if (!S.connected) toast('Call will start when the connection is back', 'warn');
      })
      .catch(function (e) {
        toast('Microphone: ' + (e && e.message ? e.message : e), 'error');
        audio.endCall();
      })
      .then(function () {
        btn.disabled = false;
        updateCallUI();
      });
  }

  function endCall() {
    S.callActive = false;
    S.pausedByVisibility = false;
    audio.endCall();
    send(P.buildCall('end'));
    $('mic-level').style.width = '0%';
    updateCallUI();
  }

  function updateCallUI() {
    var talk = $('btn-talk');
    talk.classList.toggle('active', S.callActive);
    talk.setAttribute('aria-pressed', S.callActive ? 'true' : 'false');
    $('talk-label').textContent = S.callActive ? 'End call' : 'Talk';
    var mute = $('btn-mute');
    mute.disabled = !S.callActive;
    mute.classList.toggle('active', S.micMuted);
    mute.setAttribute('aria-pressed', S.micMuted ? 'true' : 'false');
    mute.textContent = S.micMuted ? 'Unmute' : 'Mute';
    document.body.classList.toggle('in-call', S.callActive);
    renderAudioStatus();
  }

  // ---- wiring ----------------------------------------------------------------------------------------

  function wire() {
    // Any first gesture unlocks audio output so speech can play before a call starts,
    // and recovers from iOS 'interrupted'.
    document.addEventListener(
      'pointerdown',
      function () {
        if (audio.hasAudioContext() && (!audio.ctx || audio.ctx.state !== 'running')) {
          audio.unlock().then(renderAudioStatus, function () {});
        }
      },
      { passive: true },
    );

    $('gate-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var token = $('token').value.trim();
      if (token) authenticate(token);
    });

    $('btn-projects').addEventListener('click', goToProjects);
    $('focus-chip').addEventListener('click', goToProjects);
    $('btn-pause').addEventListener('click', goToProjects);
    $('btn-new-project').addEventListener('click', openNewProject);
    $('btn-continue-project').addEventListener('click', function () {
      var sec = $('projects-section');
      if (sec.scrollIntoView) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      sec.classList.add('highlight');
      setTimeout(function () {
        sec.classList.remove('highlight');
      }, 1200);
      if (S.connected) cmd('list_projects');
    });
    $('btn-advanced').addEventListener('click', function () {
      openSheet('sessions');
    });
    $('btn-advanced-settings').addEventListener('click', function () {
      openSheet('sessions');
    });
    // Walkthrough.
    $('np-close').addEventListener('click', closeSheets);
    $('np-back').addEventListener('click', npBack);
    $('np-next').addEventListener('click', npNext);
    $('np-start').addEventListener('click', npStart);
    $('np-up').addEventListener('click', function () {
      if (S.np.listing && S.np.listing.parent) npBrowse(S.np.listing.parent);
    });
    $('np-create-form').addEventListener('submit', function (e) {
      e.preventDefault();
      npCreateFolder();
    });
    $('np-use-folder').addEventListener('click', npUseFolder);
    $('np-scope').addEventListener('change', function (e) {
      S.np.scope = !!e.target.checked;
    });
    $('np-talk').addEventListener('change', function (e) {
      S.np.talk = !!e.target.checked;
    });
    var cues = $('set-cues');
    if (cues) {
      cues.checked = cuesEnabled();
      cues.addEventListener('change', function (e) {
        try {
          localStorage.setItem('zordon.cues', e.target.checked ? 'on' : 'off');
        } catch (_) {}
      });
    }
    $('draft-send').addEventListener('click', function () {
      if (draftEditTimer) clearTimeout(draftEditTimer);
      cmd('send_draft', { text: $('draft-text').value });
    });
    $('draft-clear').addEventListener('click', function () {
      if (draftEditTimer) clearTimeout(draftEditTimer);
      $('draft-text').value = '';
      cmd('set_draft', { text: '' });
    });
    $('draft-text').addEventListener('input', function () {
      draftEditedAt = Date.now();
      if (draftEditTimer) clearTimeout(draftEditTimer);
      draftEditTimer = setTimeout(sendDraftEdit, 600);
    });
    $('draft-text').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (draftEditTimer) clearTimeout(draftEditTimer);
        cmd('send_draft', { text: $('draft-text').value });
      }
    });
    $('set-prompt-save').addEventListener('click', function () {
      cmd('set_system_prompt', { text: $('set-prompt').value });
    });
    $('set-prompt-reset').addEventListener('click', function () {
      cmd('set_system_prompt', { text: '' });
    });
    $('np-headless').addEventListener('change', function (e) {
      S.np.headless = !!e.target.checked;
    });
    $('btn-settings').addEventListener('click', function () {
      openSheet('settings');
      renderSettings();
    });
    $('btn-sessions-close').addEventListener('click', closeSheets);
    $('btn-settings-close').addEventListener('click', closeSheets);
    $('btn-refresh').addEventListener('click', function () {
      cmd('list_sessions');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && document.body.classList.contains('sheet-open')) closeSheets();
    });
    if ($('advanced')) $('advanced').open = false;

    fillSelect($('new-mode'), P.PERMISSION_MODES, 'default');
    $('new-session').addEventListener('submit', function (e) {
      e.preventDefault();
      var dir = $('new-dir').value.trim();
      if (!dir) return;
      var mode = $('new-mode').value;
      if (P.PERMISSION_MODES.indexOf(mode) === -1) mode = 'default';
      var agent = $('new-agent').value || S.defaultAgent;
      var args = { directory: dir, permission_mode: mode };
      if (agent && agent !== S.defaultAgent) args.agent = agent;
      if (cmd('start', args)) {
        $('new-dir').value = '';
        toast('Starting ' + agentLabel(agent) + ' in ' + basename(dir), 'info', 2500);
      }
    });
    renderAgentSelects();
    $('attach-pane').addEventListener('submit', function (e) {
      e.preventDefault();
      var target = $('attach-target').value.trim();
      if (!target) return;
      var agent = $('attach-agent').value || 'generic';
      if (cmd('attach', { target: target, agent: agent })) {
        $('attach-target').value = '';
        toast('Attaching to ' + target + ' as ' + agentLabel(agent), 'info', 2500);
      }
    });

    // Call controls.
    $('btn-talk').addEventListener('click', toggleCall);
    $('btn-mute').addEventListener('click', function () {
      S.micMuted = !S.micMuted;
      audio.setMicMuted(S.micMuted);
      if (!S.micMuted) $('mic-level').style.width = '0%';
      updateCallUI();
    });
    $('btn-hush').addEventListener('click', function () {
      var res = audio.stopLocal();
      markInterrupted(res.interrupted, null);
      cmd('hush');
    });
    var speedTimer = null;
    $('set-speed').addEventListener('input', function (e) {
      $('set-speed-value').textContent = Number(e.target.value).toFixed(2) + 'x';
      if (speedTimer) clearTimeout(speedTimer);
      speedTimer = setTimeout(function () {
        cmd('set_speed', { speed: Number(e.target.value) });
      }, 250);
    });
    $('btn-stop').addEventListener('click', function () {
      // Silence right away and keep dropping speech of this generation until the
      // agent's own flush (sent by the stop command) arrives.
      var res = audio.stopLocal();
      markInterrupted(res.interrupted, null);
      cmd('stop');
    });
    $('btn-repeat').addEventListener('click', function () {
      cmd('repeat');
    });

    // Composer.
    var ta = $('text');
    $('composer').addEventListener('submit', function (e) {
      e.preventDefault();
      submitText();
    });
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        submitText();
      }
    });
    ta.addEventListener('input', function () {
      autosize(ta);
    });
    $('raw-send').addEventListener('change', function () {
      ta.placeholder = $('raw-send').checked ? 'Send literally to the pane' : 'Type to Claude Code';
    });

    // Uploads: buttons, inputs, drag and drop anywhere on the page.
    $('btn-attach').addEventListener('click', function () {
      $('file-input').click();
    });
    $('btn-camera').addEventListener('click', function () {
      $('camera-input').click();
    });
    ['file-input', 'camera-input'].forEach(function (id) {
      $(id).addEventListener('change', function (e) {
        uploadFiles(e.target.files);
        e.target.value = '';
      });
    });
    var dragDepth = 0;
    document.addEventListener('dragenter', function (e) {
      if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') === -1) return;
      dragDepth++;
      show($('drop-hint'), true);
    });
    document.addEventListener('dragleave', function () {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) show($('drop-hint'), false);
    });
    document.addEventListener('dragover', function (e) {
      e.preventDefault();
    });
    document.addEventListener('drop', function (e) {
      e.preventDefault();
      dragDepth = 0;
      show($('drop-hint'), false);
      if (e.dataTransfer && e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
    });

    // Feed scrolling.
    var feed = $('feed');
    feed.addEventListener(
      'scroll',
      function () {
        S.atBottom = feedAtBottom();
        if (S.atBottom) S.unseen = 0;
        renderJump();
      },
      { passive: true },
    );
    $('jump').addEventListener('click', scrollFeedToBottom);

    // Health strip and banners.
    $('health-badge').addEventListener('click', function () {
      S.healthStripOpen = !S.healthStripOpen;
      renderHealth();
    });
    $('health-detail-close').addEventListener('click', function () {
      S.healthOpen = null;
      renderHealth();
    });
    $('update-banner-close').addEventListener('click', function () {
      S.updateDismissed = updateKey(S.update);
      renderUpdate();
    });

    // Settings controls.
    $('set-verbosity').addEventListener('change', function (e) {
      cmd('set_verbosity', { level: e.target.value });
    });
    $('set-tool-chatter').addEventListener('change', function (e) {
      cmd('set_tool_chatter', { enabled: e.target.checked });
    });
    $('set-agent-mute').addEventListener('change', function (e) {
      cmd(e.target.checked ? 'mute' : 'unmute');
    });
    $('set-speaker-mute').addEventListener('change', function (e) {
      S.speakerMuted = e.target.checked;
      audio.setSpeakerMuted(S.speakerMuted);
    });
    $('set-stt').addEventListener('change', function (e) {
      cmd('set_provider', { kind: 'stt', name: e.target.value });
    });
    $('set-tts').addEventListener('change', function (e) {
      cmd('set_provider', { kind: 'tts', name: e.target.value });
    });
    $('set-voice').addEventListener('change', function (e) {
      cmd('set_provider', { kind: 'voice', name: e.target.value });
      cmd('preview_voice', { name: e.target.value });  // hear it as you pick it
    });
    $('btn-preview-voice').addEventListener('click', function () {
      cmd('preview_voice', { name: $('set-voice').value });
    });
    $('set-mode').addEventListener('change', function (e) {
      var mode = e.target.value;
      if (P.PERMISSION_MODES.indexOf(mode) === -1) return;
      cmd('set_permission_mode', Object.assign({ mode: mode }, S.focused ? { session_id: S.focused } : {}));
    });

    // Backgrounding: pause capture, keep the socket.
    function onHidden() {
      if (S.callActive && !S.pausedByVisibility) {
        S.pausedByVisibility = true;
        audio.pauseCapture();
        send(P.buildCall('pause'));
      }
    }
    function onVisible() {
      if (S.callActive && S.pausedByVisibility) {
        S.pausedByVisibility = false;
        audio.resumeCapture().then(renderAudioStatus);
        send(P.buildCall('resume'));
      }
      if (!S.connected && S.everOpened) reconnectNow();
      renderSessions();
    }
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') onHidden();
      else onVisible();
    });
    window.addEventListener('pagehide', onHidden);
    window.addEventListener('online', function () {
      if (!S.connected) reconnectNow();
    });

    // Periodic refresh of relative times and status lines.
    S.tickTimer = setInterval(function () {
      if (document.visibilityState === 'hidden') return;
      if (!$('sessions').hasAttribute('hidden')) renderSessions();
      if (!S.focused) renderProjects();
      renderFocus();
      if (!$('settings').hasAttribute('hidden')) renderAudioStatus();
    }, 15000);
  }

  // ---- boot --------------------------------------------------------------------------------------------

  wire();
  updateCallUI();
  renderSettings();
  renderFocus();
  if (window.isSecureContext === false) {
    toast('Microphone capture needs HTTPS or localhost; text mode still works.', 'warn', 8000);
  }
  connect();
})();
