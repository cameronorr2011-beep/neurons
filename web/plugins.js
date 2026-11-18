/* Connections drawer: service plugins and local coding harnesses.
   This file runs after app-shell.js and reuses its helpers. */
const connBtn = document.getElementById('connectionsBtn');
const connPanel = document.getElementById('connections');
const connBody = document.getElementById('connectionsBody');
const connCount = document.getElementById('connCount');
const connTabs = document.getElementById('connectionsTabs');
let connTab = 'plugins';
let connData = { plugins: [], connected: [] };
let harnessData = [];
let connLoaded = false;

function closeConnections() {
  connPanel.classList.remove('open');
  connPanel.setAttribute('aria-hidden', 'true');
  if (!$('personal').classList.contains('open') && !$('drawer').classList.contains('open')) {
    $('scrim').hidden = true;
  }
}

function openConnections() {
  connPanel.classList.add('open');
  connPanel.setAttribute('aria-hidden', 'false');
  $('personal').classList.remove('open');
  $('personal').setAttribute('aria-hidden', 'true');
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  $('scrim').hidden = false;
  loadConnections();
}

async function loadConnections() {
  if (!connLoaded) {
    connBody.innerHTML = '<div class="conn-loading">connecting to the local broker…</div>';
    try {
      const [p, h] = await Promise.all([
        fetch('/api/plugins', { signal: AbortSignal.timeout(10000) }).then(r => r.json()),
        fetch('/api/harnesses', { signal: AbortSignal.timeout(10000) }).then(r => r.json())
      ]);
      connData = p;
      harnessData = h.harnesses || [];
      connLoaded = true;
    } catch (e) {
      connBody.innerHTML = '<div class="conn-empty bad">connections unavailable: ' + String(e).slice(0, 120) + '</div>';
      return;
    }
  }
  renderConnections();
}

function connectionCard(plugin) {
  const card = el('article', 'conn-card' + (plugin.connected ? ' connected' : ''));
  const head = el('div', 'conn-card-head');
  const mark = el('span', 'conn-glyph', plugin.glyph || '•');
  mark.style.setProperty('--conn-color', plugin.color || 'var(--accent)');
  head.appendChild(mark);
  const title = el('div', 'conn-title');
  title.appendChild(el('strong', null, plugin.name));
  title.appendChild(el('span', 'conn-category', plugin.category || 'service'));
  head.appendChild(title);
  head.appendChild(el('span', 'conn-state' + (plugin.connected ? ' on' : ''), plugin.connected ? 'connected' : 'off'));
  card.appendChild(head);
  card.appendChild(el('p', 'conn-desc', plugin.description || ''));
  const actions = el('div', 'conn-actions');
  const docs = el('a', 'conn-link', 'docs ↗');
  docs.href = plugin.docs || '#';
  docs.target = '_blank';
  docs.rel = 'noreferrer';
  actions.appendChild(docs);
  if (plugin.connected) {
    const test = el('button', 'conn-action', 'test');
    test.addEventListener('click', () => testPlugin(plugin, test));
    const off = el('button', 'conn-action danger', 'disconnect');
    off.addEventListener('click', () => disconnectPlugin(plugin));
    actions.appendChild(test);
    actions.appendChild(off);
  } else {
    const connect = el('button', 'conn-action primary', 'connect');
    connect.addEventListener('click', () => toggleConnectForm(card, plugin));
    actions.appendChild(connect);
  }
  card.appendChild(actions);
  return card;
}

function toggleConnectForm(card, plugin) {
  if (card.querySelector('.conn-form')) {
    card.querySelector('.conn-form').remove();
    return;
  }
  const form = el('form', 'conn-form');
  for (const field of plugin.fields || []) {
    const label = el('label', 'conn-field');
    label.appendChild(document.createTextNode(field.label + (field.required ? '' : ' (optional)')));
    const input = document.createElement('input');
    input.name = field.key;
    input.type = field.secret ? 'password' : 'text';
    input.placeholder = field.placeholder || '';
    input.autocomplete = 'off';
    input.required = !!field.required;
    label.appendChild(input);
    if (field.help) label.appendChild(el('span', 'conn-help', field.help));
    form.appendChild(label);
  }
  const save = el('button', 'conn-action primary', 'save connection');
  save.type = 'submit';
  const note = el('div', 'conn-note', 'Credentials stay on this machine and are never sent to the browser.');
  form.appendChild(save);
  form.appendChild(note);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    save.disabled = true;
    save.textContent = 'saving…';
    const fields = {};
    for (const input of form.querySelectorAll('input')) fields[input.name] = input.value;
    try {
      const res = await fetch('/api/plugins/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: plugin.id, fields })
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
      connData = data;
      connLoaded = true;
      renderConnections();
      toast(plugin.name + ' connected');
    } catch (e) {
      save.disabled = false;
      save.textContent = 'save connection';
      nameError(form, String(e).slice(0, 160));
    }
  });
  card.appendChild(form);
  const first = form.querySelector('input');
  if (first) first.focus();
}

function nameError(form, text) {
  let error = form.querySelector('.conn-error');
  if (!error) {
    error = el('div', 'conn-error');
    form.appendChild(error);
  }
  error.textContent = text;
}

async function testPlugin(plugin, button) {
  button.disabled = true;
  button.textContent = 'testing…';
  try {
    const res = await fetch('/api/plugins/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: plugin.id })
    });
    const data = await res.json();
    const text = String(data.body || data.error || 'no response').slice(0, 500);
    toast((data.ok ? '✓ ' : '! ') + plugin.name + ' · ' + text);
  } catch (e) {
    toast(plugin.name + ' test failed', true);
  } finally {
    button.disabled = false;
    button.textContent = 'test';
  }
}

async function disconnectPlugin(plugin) {
  if (!window.confirm('Disconnect ' + plugin.name + '?')) return;
  try {
    const res = await fetch('/api/plugins/disconnect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: plugin.id })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    connData = data;
    renderConnections();
    toast(plugin.name + ' disconnected');
  } catch (e) {
    toast('could not disconnect ' + plugin.name, true);
  }
}

async function runHarness(harness) {
  const card = document.querySelector('[data-harness="' + harness.id + '"]');
  const prompt = card && card.querySelector('textarea');
  if (!prompt || !prompt.value.trim()) return;
  const output = card.querySelector('.harness-output');
  const run = card.querySelector('.harness-run');
  const cwd = card.querySelector('.harness-cwd');
  run.disabled = true;
  run.textContent = 'running…';
  output.textContent = 'starting ' + harness.name + '…';
  try {
    const res = await fetch('/api/harnesses/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: harness.id, prompt: prompt.value.trim(), cwd: cwd ? cwd.value : '.' })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    output.textContent = 'exit ' + data.exit_code + '\n\n' + data.output;
  } catch (e) {
    output.textContent = String(e).slice(0, 1200);
  } finally {
    run.disabled = false;
    run.textContent = 'run harness';
  }
}

function harnessCard(harness) {
  const card = el('article', 'conn-card harness-card' + (harness.installed ? ' connected' : ''));
  card.dataset.harness = harness.id;
  const head = el('div', 'conn-card-head');
  const mark = el('span', 'conn-glyph', harness.name.slice(0, 2).toUpperCase());
  mark.style.setProperty('--conn-color', harness.installed ? 'var(--accent)' : 'var(--muted)');
  head.appendChild(mark);
  const title = el('div', 'conn-title');
  title.appendChild(el('strong', null, harness.name));
  title.appendChild(el('span', 'conn-category', harness.vendor + (harness.experimental ? ' · experimental' : '')));
  head.appendChild(title);
  head.appendChild(el('span', 'conn-state' + (harness.installed ? ' on' : ''), harness.installed ? 'ready' : 'not found'));
  card.appendChild(head);
  card.appendChild(el('p', 'conn-desc', harness.description || ''));
  const actions = el('div', 'conn-actions');
  const docs = el('a', 'conn-link', 'docs ↗');
  docs.href = harness.docs || '#';
  docs.target = '_blank';
  docs.rel = 'noreferrer';
  actions.appendChild(docs);
  actions.appendChild(el('span', 'conn-note inline',
    harness.auth_configured ? harness.auth_env + ' loaded' : (harness.auth_env ? harness.auth_env + ' not set' : harness.setup)));
  card.appendChild(actions);
  if (!harness.installed) {
    card.appendChild(el('div', 'conn-note', 'Install the CLI, then reopen this panel. The command is `' + harness.command + '`.'));
    return card;
  }
  const prompt = document.createElement('textarea');
  prompt.className = 'harness-prompt';
  prompt.rows = 2;
  prompt.placeholder = 'Ask ' + harness.name + ' to do something in the workspace…';
  const row = el('div', 'harness-row');
  const cwd = document.createElement('input');
  cwd.className = 'harness-cwd';
  cwd.value = '.';
  cwd.title = 'working directory';
  const run = el('button', 'conn-action primary harness-run', 'run harness');
  run.addEventListener('click', () => runHarness(harness));
  row.appendChild(cwd);
  row.appendChild(run);
  const output = el('pre', 'harness-output');
  card.appendChild(prompt);
  card.appendChild(row);
  card.appendChild(output);
  return card;
}

function renderConnections() {
  connBody.innerHTML = '';
  const connected = (connData.plugins || []).filter(p => p.connected).length;
  connCount.textContent = String(connected);
  connCount.hidden = connected === 0;
  connTabs.innerHTML = '';
  for (const key of ['plugins', 'harnesses']) {
    const button = el('button', 'tab' + (connTab === key ? ' on' : ''),
      key === 'plugins' ? 'integrations ' + (connData.plugins || []).length : 'harnesses ' + harnessData.length);
    button.addEventListener('click', () => { connTab = key; renderConnections(); });
    connTabs.appendChild(button);
  }
  if (connTab === 'plugins') {
    const intro = el('div', 'conn-intro');
    intro.appendChild(el('strong', null, 'Bring your own accounts.'));
    intro.appendChild(el('span', null, ' Tokens stay in the local broker. The agent gets one controlled API bridge, not your passwords.'));
    connBody.appendChild(intro);
    if (!(connData.plugins || []).length) {
      connBody.appendChild(el('div', 'conn-empty', 'No integrations available.'));
      return;
    }
    for (const plugin of connData.plugins) connBody.appendChild(connectionCard(plugin));
  } else {
    connBody.appendChild(el('div', 'conn-intro', 'Local coding agents run in their own process. Their providers are marked training-exempt and never enter the Neuron Train ledger.'));
    for (const harness of harnessData) connBody.appendChild(harnessCard(harness));
  }
}

connBtn.addEventListener('click', () => {
  if (connPanel.classList.contains('open')) closeConnections();
  else openConnections();
});
document.getElementById('connclose').addEventListener('click', closeConnections);
$('scrim').addEventListener('click', closeConnections);
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeConnections(); });
