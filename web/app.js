const thread = document.getElementById('thread');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const modelSel = document.getElementById('model');
const providerSel = document.getElementById('provider');
const agentBtn = document.getElementById('agent');
let providers = [];

function labelFor(id) {
  const p = providers.find(x => x.id === id);
  return p ? p.label : id;
}
function providerLabel() { return labelFor(providerSel.value); }

async function loadProviders() {
  try {
    const r = await fetch('/api/providers', { signal: AbortSignal.timeout(10000) });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
    providers = d.providers || [];
    providerSel.innerHTML = '';
    for (const p of providers) {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.label;
      providerSel.appendChild(o);
    }
    providerSel.value = d.default || (providers[0] && providers[0].id) || '';
    await loadModels();
  } catch (e) {
    setStatus('providers unavailable — ' + String(e).slice(0, 80), true);
  }
}
providerSel.addEventListener('change', () => {
  // Drop the old provider's model immediately: sending it to the new
  // provider is a guaranteed 400 while the new list is still loading.
  modelSel.dataset.provider = '';
  modelSel.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = ''; placeholder.textContent = 'loading…'; placeholder.disabled = true;
  modelSel.appendChild(placeholder);
  loadModels(true);
});
// Full-control is the default; remember an explicit opt-out across reloads.
let agentOn = true;
try { agentOn = localStorage.getItem('neurons.agent') !== 'off'; } catch {}
agentBtn.classList.toggle('on', agentOn);
agentBtn.textContent = agentOn ? 'agent on' : 'agent';
agentBtn.addEventListener('click', () => {
  agentOn = !agentOn;
  agentBtn.classList.toggle('on', agentOn);
  agentBtn.textContent = agentOn ? 'agent on' : 'agent';
  try { localStorage.setItem('neurons.agent', agentOn ? 'on' : 'off'); } catch {}
});

const history = [{ role: 'system', content: "You are Neurons, an agent running on the user's own Windows PC with full access to it. run_command executes real commands in git-bash and the file tools reach any path, so use absolute paths such as C:/Users/Cameron/... instead of guessing. Prefer acting over asking: inspect, run, read what you need, then report what actually happened. You can also drive the desktop: click, type_text, press_key, scroll, list/focus/close windows and read or write the clipboard. Always pass type_text/press_key a 'window' title so keystrokes cannot land in the wrong app, and take a screenshot after a GUI action to check it worked. You can also write files (write_file), find files by glob (find_files), fetch URLs (http_get), launch apps or URLs (launch_app), inspect processes and system stats, and control volume. In agent mode, write one short sentence before each tool call explaining what you are about to do and why, so the trace reads like live thinking steps. Agent mode is on by default; keep going until the task is actually complete. After every tool result, decide the next action and take it, using as many steps as needed. Do not stop after one tool call, and do not reply with a plan when you can execute it. Only stop when the job is done or you are genuinely blocked. Every agent turn must call a tool; use the finish tool with your final answer only when the task is complete. Spawn subagents with delegate for independent subtasks, and call the webhook tool when the user asks you to notify an endpoint." }];
let busy = false;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function scrollDown() {
  const main = document.getElementById('main');
  main.scrollTop = main.scrollHeight;
}

const statusEl = document.getElementById('status');

function setStatus(text, isError) {
  statusEl.textContent = text;
  statusEl.classList.toggle('err', !!isError);
}

async function loadModels(refresh) {
  setStatus('loading models…', false);
  try {
    const q = '/api/models?provider=' + encodeURIComponent(providerSel.value) +
      (refresh ? '&refresh=1' : '');
    const r = await fetch(q, { signal: AbortSignal.timeout(25000) });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
    const ids = (d.data || []).map(m => m.id).sort();
    if (!ids.length) throw new Error('no models returned');
    modelSel.innerHTML = '';
    for (const id of ids) {
      const o = document.createElement('option');
      o.value = id; o.textContent = id;
      modelSel.appendChild(o);
    }
    const preferred = d.default_model;
    modelSel.value = ids.includes(preferred) ? preferred : ids[0];
    modelSel.dataset.provider = providerSel.value;
    setStatus(ids.length + ' models  ·  ' + (d.provider_label || providerLabel()), false);
  } catch (e) {
    // Never leave an empty dropdown with no explanation.
    const def = (providers.find(p => p.id === providerSel.value) || {}).model || '';
    modelSel.innerHTML = '';
    const o = document.createElement('option');
    o.value = def; o.textContent = def || 'no models';
    modelSel.appendChild(o);
    delete modelSel.dataset.provider;
    setStatus('models unavailable — ' + String(e).slice(0, 80), true);
  }
}

function handleFrame(frame, sink) {
  const nl = String.fromCharCode(10);
  for (const line of frame.split(nl)) {
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload) continue;
    if (payload === '[DONE]') { sink.done = true; return; }
    let d;
    try { d = JSON.parse(payload); } catch { continue; }
    if (d.error) { sink.error = typeof d.error === 'string' ? d.error : JSON.stringify(d.error); return; }
    const choice = (d.choices || [])[0];
    if (!choice) continue;
    const delta = choice.delta || {};
    // Providers disagree on the field name for the reasoning channel.
    const reasoningDelta = delta.reasoning || delta.reasoning_content || delta.thinking || '';
    if (reasoningDelta) { sink.reasoning += reasoningDelta; sink.render(); }
    if (delta.content) { sink.content += delta.content; sink.render(); }
    if (d.model) sink.model = d.model;
  }
}

let activeCtrl = null;
let stopRequested = false;
let queue = [];
let queueEditId = null;
let nextQueueId = 1;

function renderQueue() {
  const info = document.getElementById('queueInfo');
  const list = document.getElementById('queueList');
  if (!info || !list) return;
  info.textContent = queue.length ? queue.length + ' queued · next runs automatically' : '';
  list.innerHTML = '';
  list.hidden = queue.length === 0;
  if (!queue.length) return;

  queue.forEach((item, index) => {
    const row = el('div', 'queue-row');
    row.dataset.queueId = String(item.id);
    row.appendChild(el('span', 'queue-num', String(index + 1)));
    if (item.id === queueEditId) {
      const ta = el('textarea', 'queue-edit');
      ta.value = item.text;
      ta.rows = 2;
      row.appendChild(ta);
      const save = el('button', 'queue-action', 'save');
      save.addEventListener('click', () => {
        const value = ta.value.trim();
        if (!value) return;
        item.text = value;
        queueEditId = null;
        renderQueue();
        pumpQueue();
      });
      const cancel = el('button', 'queue-action', 'cancel');
      cancel.addEventListener('click', () => { queueEditId = null; renderQueue(); pumpQueue(); });
      row.appendChild(save);
      row.appendChild(cancel);
      setTimeout(() => ta.focus(), 0);
    } else {
      row.appendChild(el('span', 'queue-text', item.text));
      const edit = el('button', 'queue-action', 'edit');
      edit.addEventListener('click', () => { queueEditId = item.id; renderQueue(); });
      const remove = el('button', 'queue-action danger', 'remove');
      remove.addEventListener('click', () => {
        queue = queue.filter(entry => entry.id !== item.id);
        if (queueEditId === item.id) queueEditId = null;
        renderQueue();
        pumpQueue();
      });
      row.appendChild(edit);
      row.appendChild(remove);
    }
    list.appendChild(row);
  });

  const clear = el('button', 'queue-clear', 'clear all');
  clear.addEventListener('click', () => {
    queue = [];
    queueEditId = null;
    renderQueue();
  });
  list.appendChild(clear);
}

function pumpQueue() {
  if (busy || queueEditId !== null || !queue.length) return;
  const next = queue.shift();
  renderQueue();
  setTimeout(() => runPrompt(next.text), 0);
}

function friendlyError(error) {
  const raw = String(error || '');
  if (/Failed to fetch|NetworkError|Load failed|ERR_CONNECTION|fetch failed/i.test(raw)) {
    return 'network error — provider connection dropped. Retry.';
  }
  return raw;
}

function addRetry(bubble, text) {
  const row = el('div', 'retry-row');
  const button = el('button', 'retry', 'retry');
  button.type = 'button';
  button.addEventListener('click', () => {
    if (busy) return;
    button.disabled = true;
    input.value = text;
    submitPrompt();
  });
  row.appendChild(button);
  bubble.appendChild(row);
}

window.addRetry = addRetry;
window.friendlyError = friendlyError;

function finishPrompt() {
  busy = false;
  sendBtn.disabled = false;
  sendBtn.textContent = 'Send';
  const stop = document.getElementById('stop');
  stop.hidden = true;
  stop.textContent = 'stop';
  activeCtrl = null;
  input.focus();
  scrollDown();
  pumpQueue();
}

async function submitPrompt() {
  const text = input.value.trim();
  if (!text) return;
  input.value = ''; input.style.height = 'auto';
  if (busy) {
    queue.push({ id: nextQueueId++, text });
    renderQueue();
    return;
  }
  await runPrompt(text);
}

async function runPrompt(text) {
  await stateReady;
  if (modelSel.dataset.provider !== providerSel.value) {
    await loadModels();
    if (modelSel.dataset.provider !== providerSel.value) {
      setStatus('models for ' + providerLabel() + ' are not loaded yet — try again', true);
      return;
    }
  }
  busy = true;
  sendBtn.disabled = false;
  sendBtn.textContent = 'queue';
  stopRequested = false;
  document.getElementById('stop').hidden = false;
  input.value = ''; input.style.height = 'auto';

  const userMsg = el('div', 'msg user');
  userMsg.appendChild(el('div', 'bubble', text));
  thread.appendChild(userMsg);
  history.push({ role: 'user', content: text });
  hideWelcome();
  await ensureConversation(text);
  if (conversationId) await saveMessage('user', text, { provider: providerSel.value });
  scrollDown();

  const msg = el('div', 'msg assistant');
  const think = el('details', 'think');
  think.open = true;
  const summary = el('summary', null, 'sending…');
  const pre = el('pre', null, '');
  think.appendChild(summary);
  think.appendChild(pre);
  const bubble = el('div', 'bubble', '▍');
  const meta = el('div', 'meta', '');
  msg.appendChild(think); msg.appendChild(bubble); msg.appendChild(meta);
  thread.appendChild(msg);
  scrollDown();

  // Visible feedback from the instant Send is clicked, not when tokens arrive.
  const started = performance.now();
  let firstToken = false;
  let thinkGone = false;
  const timer = setInterval(() => {
    const s = ((performance.now() - started) / 1000).toFixed(1);
    if (!firstToken) {
      summary.textContent = (agentOn && sink.step ? 'thinking · step ' + sink.step + ' … ' : 'waiting for the model… ') + s + 's';
    } else {
      if (!sink.content && sink.reasoning) {
        summary.textContent = 'thinking… ' + sink.reasoning.length + ' chars · ' + s + 's';
      }
      meta.textContent = (sink.model || modelSel.value) + '  ·  ' +
        labelFor(sink.provider || providerSel.value) + '  ·  ' + s + 's';
    }
  }, 100);

  // Never hang forever on a queued/throttled provider.
  const ctrl = new AbortController();
  activeCtrl = ctrl;
  const hardStop = setTimeout(() => ctrl.abort(), agentOn ? 1860000 : 300000);

  const sink = {
    content: '', reasoning: '', model: '', error: '', done: false, step: 0,
    render() {
      if (this.reasoning || this.content) firstToken = true;
      if (this.reasoning) {
        think.open = true;
        summary.textContent = 'thinking…';   // live reasoning, streamed below
      }
      pre.textContent = this.reasoning;
      if (this.content) {
        bubble.textContent = this.content;
        // Keep the panel visible even with no reasoning, so its absence is
        // explained instead of silently disappearing.
        if (!this.reasoning) summary.textContent = 'no reasoning channel from this model';
      }
      if (this.model) meta.textContent = this.model;
      scrollDown();
    }
  };

  // Agent mode: streamed tool loop, so thinking and each tool step appear live.
  if (agentOn) {
    const nl = String.fromCharCode(10);
    let trace = '';
    let live = '';
    let stepsSeen = 0;
    const renderTrace = () => { pre.textContent = trace; think.open = true; scrollDown(); };
    const handleAgentFrame = (frame) => {
      for (const line of frame.split(nl)) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        if (payload === '[DONE]') { sink.done = true; return; }
        let d;
        try { d = JSON.parse(payload); } catch { continue; }
        if (d.type === 'step') {
          sink.step = d.step;
          trace += (trace ? nl + nl : '') + '──── step ' + d.step + ' of ' + d.max_steps + ' ────' + nl;
          summary.textContent = 'thinking · step ' + d.step + ' of ' + d.max_steps;
          renderTrace();
        } else if (d.type === 'reasoning' && d.delta) {
          firstToken = true;
          trace += d.delta;
          summary.textContent = 'thinking…';
          renderTrace();
        } else if (d.type === 'content' && d.delta) {
          firstToken = true;
          live += d.delta;
          bubble.textContent = live;
          scrollDown();
        } else if (d.type === 'tool') {
          if (live) {
            trace += (trace ? nl : '') + '[says] ' + live.trim() + nl;
            live = '';
            bubble.textContent = '▍';
          }
          trace += '→ ' + d.tool + ' ' + JSON.stringify(d.args || {}) + nl;
          summary.textContent = 'running ' + d.tool + '…';
          renderTrace();
        } else if (d.type === 'tool_result') {
          trace += '← ' + String(d.result || '').replaceAll(nl, nl + '  ').slice(0, 1600) + nl;
          renderTrace();
        } else if (d.type === 'nudge') {
          trace += '↻ ' + (d.message || 'pushing the model to act') + nl;
          summary.textContent = 'pushing for action…';
          renderTrace();
        } else if (d.type === 'retry') {
          trace += '↻ ' + (d.reason || 'provider hiccup') + ' — retrying (' +
            (d.attempt || 1) + '/' + (d.max || 3) + ')' + nl;
          summary.textContent = 'retrying provider…';
          renderTrace();
        } else if (d.type === 'warning') {
          trace += '⚠ ' + (d.message || 'provider warning') + nl;
          renderTrace();
        } else if (d.type === 'fallback') {
          sink.fallback = '  ·  fell back from ' + labelFor(d.from);
          sink.provider = d.to;
          trace = 'provider ' + labelFor(d.from) + ' failed: ' + d.reason +
            nl + 'fell back to ' + labelFor(d.to) + nl;
          summary.textContent = 'falling back to ' + labelFor(d.to) + '…';
          renderTrace();
        } else if (d.type === 'error') {
          sink.error = d.error || 'agent failed';
        } else if (d.type === 'done') {
          sink.model = d.model || modelSel.value;
          sink.provider = d.provider || sink.provider || providerSel.value;
          sink.content = d.text || live;
          bubble.textContent = sink.content || '(the model ended without an answer — try again or switch models)';
          if (d.steps) stepsSeen = d.steps.length;
          sink.done = true;
        }
      }
    };

    try {
      const res = await fetch('/api/agent/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: providerSel.value, model: modelSel.value, messages: history }),
        signal: ctrl.signal
      });
      if (!res.ok || !res.body) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.error || ('HTTP ' + res.status));
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf(nl + nl)) >= 0) {
          handleAgentFrame(buf.slice(0, i));
          buf = buf.slice(i + 2);
        }
      }
      if (buf.trim()) handleAgentFrame(buf);
      const reply = sink.content || live;
      history.push({ role: 'assistant', content: reply || '(no answer text)' });
      if (reply && !sink.error) await saveMessage('assistant', reply, { provider: sink.provider || providerSel.value });
    } catch (e) {
      sink.error = (e && e.name === 'AbortError')
        ? (stopRequested ? 'stopped by you' : 'timed out after 31 minutes')
        : friendlyError(e);
    } finally {
      clearTimeout(hardStop);
    }

    clearInterval(timer);
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    if (sink.error) {
      if (!sink.content) bubble.textContent = sink.error;
      bubble.style.color = '#f87171';
      addRetry(bubble, text);
      trace += (trace ? nl : '') + '[error] ' + sink.error;
      renderTrace();
    } else {
      meta.textContent = (sink.model || modelSel.value) + '  ·  ' +
        labelFor(sink.provider || providerSel.value) + (sink.fallback || '') +
        '  ·  agent  ·  ' + secs + 's';
      if (stepsSeen || trace.trim()) {
        summary.textContent = 'thinking + tools · ' + stepsSeen + ' tool' +
          (stepsSeen === 1 ? '' : 's') + ' · ' + secs + 's';
        think.open = true;
      } else {
        summary.textContent = 'no reasoning channel from this model';
      }
    }
    finishPrompt();
    return;
  }

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: providerSel.value, model: modelSel.value, messages: history }),
      signal: ctrl.signal
    });
    if (!res.ok || !res.body) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.error || ('HTTP ' + res.status));
    }
    sink.provider = res.headers.get('X-Neurons-Provider') || providerSel.value;

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    const nl = String.fromCharCode(10);
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf(nl + nl)) >= 0) {
        handleFrame(buf.slice(0, i), sink);
        buf = buf.slice(i + 2);
      }
    }
    if (buf.trim()) handleFrame(buf, sink);
  } catch (e) {
    sink.error = (e && e.name === 'AbortError')
      ? (stopRequested ? 'stopped by you' : 'timed out after 5 minutes — the provider may be queueing; retry or switch models')
      : friendlyError(e);
  } finally {
    clearTimeout(hardStop);
  }

  clearInterval(timer);
  if (sink.error) {
    bubble.textContent = sink.error;
    bubble.style.color = '#f87171';
    addRetry(bubble, text);
  } else {
    const reply = sink.content || sink.reasoning;
    history.push({ role: 'assistant', content: reply });
    if (reply) await saveMessage('assistant', reply, { provider: sink.provider || providerSel.value });
  }
  if (sink.reasoning) {
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    summary.textContent = 'thought · ' + sink.reasoning.length + ' chars · ' + secs + 's';
  } else {
    summary.textContent = 'no reasoning channel from this model';
  }
  if (!sink.error) {
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    meta.textContent = (sink.model || '') + '  ·  ' + labelFor(sink.provider || providerSel.value) + '  ·  ' + secs + 's';
  }

  finishPrompt();
}

sendBtn.addEventListener('click', submitPrompt);
document.getElementById('stop').addEventListener('click', () => {
  if (!activeCtrl) return;
  stopRequested = true;
  queue = [];
  queueEditId = null;
  renderQueue();
  document.getElementById('stop').textContent = 'stopping…';
  activeCtrl.abort();
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitPrompt(); }
});
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
});

// ---------------------------------------------------------------- toolkit
const drawer = document.getElementById('drawer');
const kbody = document.getElementById('kbody');
const ktabs = document.getElementById('ktabs');
const ksearch = document.getElementById('ksearch');
const toolkitBtn = document.getElementById('toolkit');
const installBtn = document.getElementById('kinstall');
let kdata = null;
let ktab = 'all';

function setDrawer(open) {
  if (open) {
    document.getElementById('personal').classList.remove('open');
    document.getElementById('personal').setAttribute('aria-hidden', 'true');
    const conn = document.getElementById('connections');
    if (conn) { conn.classList.remove('open'); conn.setAttribute('aria-hidden', 'true'); }
    document.getElementById('scrim').hidden = false;
  } else if (!document.getElementById('personal').classList.contains('open') &&
             !document.getElementById('connections').classList.contains('open')) {
    document.getElementById('scrim').hidden = true;
  }
  drawer.classList.toggle('open', open);
  drawer.setAttribute('aria-hidden', open ? 'false' : 'true');
  if (open) { loadToolkit(); ksearch.focus(); }
}
toolkitBtn.addEventListener('click', () => setDrawer(!drawer.classList.contains('open')));
document.getElementById('kclose').addEventListener('click', () => setDrawer(false));
installBtn.addEventListener('click', async () => {
  const original = 'update skills';
  installBtn.disabled = true;
  installBtn.textContent = 'updating…';
  try {
    const res = await fetch('/api/skills/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ overwrite: true })
    });
    const d = await res.json();
    if (!res.ok || d.error) throw new Error(d.error || ('HTTP ' + res.status));
    kdata = null;
    if (drawer.classList.contains('open')) {
      await loadToolkit();
      ktab = 'skills';
      renderToolkit();
    }
    const updated = d.installed ? d.installed.length : 0;
    const errors = d.errors ? d.errors.length : 0;
    const msg = (updated ? 'updated ' + updated + ' skills' : 'skills already current') +
      (errors ? ' · ' + errors + ' errors' : '');
    setStatus(msg, !!errors);
    installBtn.textContent = updated ? 'updated' : 'current';
  } catch (e) {
    setStatus('skill update failed — ' + String(e).slice(0, 120), true);
    installBtn.textContent = 'failed';
  } finally {
    installBtn.disabled = false;
    setTimeout(() => { installBtn.textContent = original; }, 4000);
  }
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setDrawer(false); });
ksearch.addEventListener('input', () => renderToolkit());

async function loadToolkit() {
  if (kdata) { renderToolkit(); return; }
  kbody.innerHTML = '<div class="empty">loading…</div>';
  try {
    const r = await fetch('/api/toolkit');
    kdata = await r.json();
    const n = kdata.local.length + kdata.skills.length +
      kdata.mcp.reduce((a, s) => a + s.tools.length, 0);
    document.getElementById('kcount').textContent = String(n);
  } catch (e) {
    kbody.innerHTML = '';
    kbody.appendChild(el('div', 'empty bad', 'toolkit unavailable: ' + String(e)));
    return;
  }
  renderToolkit();
}

async function runTool(name, ta, out, btn) {
  let args = {};
  const raw = ta.value.trim();
  if (raw) {
    try { args = JSON.parse(raw); }
    catch (e) { out.className = 'out bad'; out.textContent = 'invalid JSON: ' + e.message; return; }
  }
  btn.disabled = true;
  out.className = 'out';
  out.textContent = 'running…';
  try {
    const res = await fetch('/api/tool', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, args })
    });
    const d = await res.json();
    const text = String(d.result || d.error || '(no output)');
    out.className = 'out ' + (d.error ? 'bad' : 'ok');
    out.textContent = text.slice(0, 4000);
    if (text.startsWith('SCREENSHOT:')) {
      const p = text.slice(11).split('|')[0];
      const img = new Image();
      img.className = 'shot';
      img.src = '/api/screenshot?path=' + encodeURIComponent(p);
      out.textContent = '';
      out.appendChild(img);
      out.appendChild(el('div', 'meta', p));
    }
  } catch (e) {
    out.className = 'out bad';
    out.textContent = String(e);
  }
  btn.disabled = false;
}

function toolItem(name, desc, tag) {
  const d = el('details', 'item');
  const s = el('summary');
  s.appendChild(el('span', null, name));
  s.appendChild(el('span', 'tag', tag));
  d.appendChild(s);
  const body = el('div', 'item-body');
  if (desc) body.appendChild(el('p', null, desc));
  const ta = el('textarea');
  ta.value = '{}';
  ta.spellcheck = false;
  body.appendChild(ta);
  const run = el('button', 'run', 'Run');
  const out = el('pre', 'out', '');
  run.addEventListener('click', () => runTool(name, ta, out, run));
  body.appendChild(run);
  body.appendChild(out);
  d.appendChild(body);
  return d;
}

function skillItem(name, desc) {
  const d = el('details', 'item');
  const s = el('summary');
  s.appendChild(el('span', null, name));
  s.appendChild(el('span', 'tag', 'skill'));
  d.appendChild(s);
  const body = el('div', 'item-body');
  body.appendChild(el('p', null, desc || 'no description found in SKILL.md'));
  const use = el('button', 'run', 'Use in chat');
  use.addEventListener('click', () => {
    input.value = 'Use the "' + name + '" skill. ';
    setDrawer(false);
    input.focus();
  });
  body.appendChild(use);
  d.appendChild(body);
  return d;
}

function renderToolkit() {
  kbody.innerHTML = '';
  if (!kdata) return;
  const q = ksearch.value.trim().toLowerCase();
  const hit = (name, desc) => !q || (name + ' ' + (desc || '')).toLowerCase().includes(q);
  const mcpCount = kdata.mcp.reduce((a, s) => a + s.tools.length, 0);
  const counts = {
    all: kdata.local.length + kdata.skills.length + mcpCount,
    mcp: mcpCount,
    skills: kdata.skills.length,
    tools: kdata.local.length
  };
  ktabs.innerHTML = '';
  for (const key of ['all', 'mcp', 'skills', 'tools']) {
    const b = el('button', 'tab' + (ktab === key ? ' on' : ''), key + ' ' + counts[key]);
    b.addEventListener('click', () => { ktab = key; renderToolkit(); });
    ktabs.appendChild(b);
  }

  if (ktab === 'all' || ktab === 'mcp') {
    kbody.appendChild(el('div', 'group-title', 'MCP servers'));
    if (!kdata.mcp.length) kbody.appendChild(el('div', 'empty', 'none configured in .mcp.json'));
    for (const srv of kdata.mcp) {
      const head = el('div', 'srv');
      head.appendChild(el('span', 'dot ' + (srv.connected ? 'ok' : 'bad')));
      head.appendChild(el('span', null, srv.name));
      head.appendChild(el('span', 'tag', srv.connected ? srv.tools.length + ' tools' : 'offline'));
      kbody.appendChild(head);
      if (srv.error) kbody.appendChild(el('div', 'empty bad', srv.error));
      for (const t of srv.tools) {
        if (hit(t.name, t.description)) kbody.appendChild(toolItem(t.name, t.description, t.label));
      }
    }
  }

  if (ktab === 'all' || ktab === 'skills') {
    kbody.appendChild(el('div', 'group-title', 'Skills'));
    if (!kdata.skills.length) kbody.appendChild(el('div', 'empty', 'no .agents/skills/*/SKILL.md found'));
    for (const sk of kdata.skills) {
      if (hit(sk.name, sk.description)) kbody.appendChild(skillItem(sk.name, sk.description));
    }
  }

  if (ktab === 'all' || ktab === 'tools') {
    kbody.appendChild(el('div', 'group-title', 'Tools'));
    for (const t of kdata.local) {
      if (hit(t.name, t.description)) kbody.appendChild(toolItem(t.name, t.description, 'local'));
    }
  }
}

loadProviders();
input.focus();