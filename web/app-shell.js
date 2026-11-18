/* Neurons app shell. This runs after app.js, so it can build on the existing
   streaming chat, queue and toolkit code without replacing them. */
const $ = (id) => document.getElementById(id);
const THEMES = ['graphite', 'aurora', 'ember', 'paper', 'terminal'];
const stateReady = new Promise((resolve) => { window.__neuronsStateResolve = resolve; });

let profile = {};
let training = {};
let memories = [];
let conversations = [];
let conversationId = null;
let stateLoaded = false;
let toastTimer = null;

// The server replaces this with a personalized system prompt. Keeping the
// client prompt generic means the app is not tied to one machine.
history[0].content = (
  "You are Neurons, a local-first agent with real tools for files, shell, " +
  "browser, screen and desktop control. Prefer acting over asking. Run the " +
  "next concrete step, inspect the result, and keep going until the task is " +
  "done. Never claim a command ran unless you saw its result. The server " +
  "injects the user's profile and long-term memories; use the remember tool " +
  "for durable preferences or facts, not for transient commands. Use finish " +
  "with a concise final answer when the task is complete."
);

function toast(text, bad) {
  const node = $('toast');
  node.textContent = text;
  node.className = 'toast' + (bad ? ' bad' : '');
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
}

function initialFor(name) {
  return (String(name || 'N').trim().slice(0, 1) || 'N').toUpperCase();
}

function applyTheme(theme) {
  const safe = THEMES.includes(theme) ? theme : 'graphite';
  document.documentElement.dataset.theme = safe;
  $('themeSel').value = safe;
  $('ptheme').value = safe;
  try { localStorage.setItem('neurons.theme', safe); } catch {}
}

function renderIdentity() {
  const name = String(profile.name || '').trim();
  $('avatar').textContent = initialFor(name);
  $('brandSub').textContent = name ? 'for ' + name : 'local agent';
  $('welcomeTitle').textContent = name ? 'What are we building, ' + name + '?' : 'What are we building?';
  $('welcomeLead').textContent = name
    ? 'Neurons remembers your preferences and can work on this machine. What should it do next?'
    : 'Neurons can read files, run commands, browse, and drive this machine. It remembers what you tell it.';
}

function fillProfileForm() {
  $('pname').value = profile.name || '';
  $('pabout').value = profile.about || '';
  $('pstyle').value = profile.style || 'balanced';
  $('ptheme').value = profile.theme || document.documentElement.dataset.theme || 'graphite';
  $('bgblur').value = String(profile.background_blur ?? 0);
  $('bgdim').value = String(profile.background_dim ?? 42);
  $('bgblurvalue').textContent = String(profile.background_blur ?? 0);
  $('bgdimvalue').textContent = String(profile.background_dim ?? 42);
  applyBackground();
}

function applyBackground() {
  const name = String(profile.background || '');
  const blur = Math.max(0, Math.min(24, Number(profile.background_blur ?? 0)));
  const dim = Math.max(0, Math.min(80, Number(profile.background_dim ?? 42)));
  const root = document.documentElement;
  root.style.setProperty('--bg-blur', blur + 'px');
  root.style.setProperty('--bg-dim', String(dim / 100));
  if (name) {
    root.style.setProperty('--custom-bg', 'url("/api/background/file/' + encodeURIComponent(name) + '")');
    document.body.classList.add('has-bg');
    $('bgstatus').textContent = 'custom background';
  } else {
    root.style.removeProperty('--custom-bg');
    document.body.classList.remove('has-bg');
    $('bgstatus').textContent = 'using theme backdrop';
  }
}

function renderTraining() {
  const box = $('trainingStats');
  if (!box) return;
  $('trainingToggle').checked = !!training.enabled;
  const count = Number(training.contributions || 0);
  const tokens = Number(training.tokens || 0);
  box.textContent = training.enabled
    ? (count ? count + ' local contributions · ' + tokens.toLocaleString() + ' estimated tokens' : 'opted in · no contributions yet')
    : 'not contributing';
  $('trainingExport').classList.toggle('disabled', !count);
  $('trainingClear').disabled = !count;
}

async function saveBackgroundSettings() {
  const payload = {
    background_blur: Number($('bgblur').value),
    background_dim: Number($('bgdim').value),
  };
  try {
    const res = await fetch('/api/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok && data.profile) {
      profile = data.profile;
      applyBackground();
    }
  } catch {}
}

async function uploadBackground(file) {
  if (!file) return;
  if (file.size > 16 * 1024 * 1024) { toast('background must be under 16 MB', true); return; }
  try {
    const res = await fetch('/api/background', {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    });
    const data = await res.json();
    if (!res.ok || !data.profile) throw new Error(data.error || ('HTTP ' + res.status));
    profile = data.profile;
    applyBackground();
    toast('background updated');
  } catch (e) {
    toast('background upload failed — ' + String(e).slice(0, 100), true);
  }
}

async function clearBackground() {
  try {
    const res = await fetch('/api/background/delete', { method: 'POST' });
    const data = await res.json();
    if (!res.ok || !data.profile) throw new Error(data.error || ('HTTP ' + res.status));
    profile = data.profile;
    applyBackground();
    toast('background removed');
  } catch { toast('could not remove background', true); }
}

async function setTraining(enabled) {
  try {
    const res = await fetch('/api/training', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    training = data;
    renderTraining();
    toast(enabled ? 'Neuron Train is on — local ledger only' : 'Neuron Train is off');
  } catch (e) { toast('could not update training setting', true); }
}

async function clearTraining() {
  if (!window.confirm('Clear the local training ledger?')) return;
  try {
    const res = await fetch('/api/training', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'clear' }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    training = data;
    renderTraining();
    toast('training ledger cleared');
  } catch { toast('could not clear ledger', true); }
}

function showWelcome() { $('welcome').hidden = false; }
function hideWelcome() { $('welcome').hidden = true; }

function clearMessages() {
  thread.querySelectorAll('.msg').forEach((node) => node.remove());
}

function renderSavedMessage(role, content) {
  const msg = el('div', 'msg ' + role);
  msg.appendChild(el('div', 'bubble', content));
  if (role === 'assistant') msg.appendChild(el('div', 'meta', 'saved'));
  thread.appendChild(msg);
}

function renderConversations() {
  const box = $('convos');
  box.innerHTML = '';
  if (!conversations.length) {
    box.appendChild(el('div', 'empty', 'No saved chats yet.'));
    return;
  }
  for (const convo of conversations) {
    const button = el('button', 'convo' + (convo.id === conversationId ? ' active' : ''));
    button.type = 'button';
    button.title = convo.title || 'Untitled chat';
    button.appendChild(el('span', 'convo-title', convo.title || 'Untitled chat'));
    const del = el('span', 'convo-del', '×');
    del.title = 'delete chat';
    del.addEventListener('click', (event) => {
      event.stopPropagation();
      removeConversation(convo.id);
    });
    button.appendChild(del);
    button.addEventListener('click', () => loadConversation(convo.id));
    box.appendChild(button);
  }
}

function renderMemories() {
  const box = $('memories');
  box.innerHTML = '';
  if (!memories.length) {
    box.appendChild(el('div', 'empty', 'Nothing saved yet. Tell Neurons a preference or add one here.'));
    return;
  }
  for (const memory of memories) {
    const row = el('div', 'memory-item');
    row.appendChild(el('span', null, memory.text));
    const del = el('button', 'memory-del', '×');
    del.type = 'button';
    del.title = 'forget this';
    del.addEventListener('click', () => removeMemory(memory.id));
    row.appendChild(del);
    box.appendChild(row);
  }
}

function setSidebar(open) {
  document.body.classList.toggle('sidebar-open', open);
  if (open) {
    $('scrim').hidden = false;
  } else if (!$('personal').classList.contains('open') &&
             !$('connections').classList.contains('open') &&
             !$('drawer').classList.contains('open')) {
    $('scrim').hidden = true;
  }
}

function setPersonal(open) {
  if (open) {
    $('connections').classList.remove('open');
    $('connections').setAttribute('aria-hidden', 'true');
    $('drawer').classList.remove('open');
    $('drawer').setAttribute('aria-hidden', 'true');
  }
  $('personal').classList.toggle('open', open);
  $('personal').setAttribute('aria-hidden', open ? 'false' : 'true');
  if (open) {
    fillProfileForm();
    renderMemories();
    renderTraining();
    $('scrim').hidden = false;
  } else if (!$('personal').classList.contains('open') &&
             !$('connections').classList.contains('open') &&
             !$('drawer').classList.contains('open')) {
    $('scrim').hidden = true;
  }
}

async function refreshConversations() {
  try {
    const res = await fetch('/api/conversations');
    const data = await res.json();
    if (res.ok && Array.isArray(data.conversations)) {
      conversations = data.conversations;
      renderConversations();
    }
  } catch {}
}

async function ensureConversation(text) {
  if (conversationId) return conversationId;
  try {
    const res = await fetch('/api/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: text }),
    });
    const data = await res.json();
    if (!res.ok || !data.conversation) throw new Error(data.error || ('HTTP ' + res.status));
    conversationId = data.conversation.id;
    conversations = [data.conversation, ...conversations.filter((c) => c.id !== conversationId)];
    renderConversations();
    $('dbStatus').textContent = 'sqlite · saved';
    return conversationId;
  } catch (error) {
    toast('could not start a saved chat — ' + String(error).slice(0, 90), true);
    return null;
  }
}

async function saveMessage(role, content, meta) {
  if (!conversationId || !content) return;
  try {
    const res = await fetch('/api/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId, role, content, meta: meta || {} }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    if (role === 'user') await refreshConversations();
  } catch (error) {
    console.warn('message not saved', error);
  }
}

async function removeConversation(id) {
  if (busy) { toast('stop the current run first', true); return; }
  if (!window.confirm('Delete this chat?')) return;
  try {
    const res = await fetch('/api/conversations/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    conversations = conversations.filter((c) => c.id !== id);
    if (conversationId === id) resetChat();
    else renderConversations();
    toast('chat deleted');
  } catch (error) {
    toast('could not delete chat', true);
  }
}

async function removeMemory(id) {
  try {
    const res = await fetch('/api/memory/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    memories = memories.filter((m) => m.id !== id);
    renderMemories();
    toast('memory removed');
  } catch (error) {
    toast('could not remove memory', true);
  }
}

async function addMemory() {
  const text = $('memtext').value.trim();
  if (!text) return;
  try {
    const res = await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, source: 'user' }),
    });
    const data = await res.json();
    if (!res.ok || !data.memory) throw new Error(data.error || ('HTTP ' + res.status));
    memories = [data.memory, ...memories.filter((m) => m.id !== data.memory.id)];
    $('memtext').value = '';
    renderMemories();
    toast('remembered');
  } catch (error) {
    toast('could not save memory — ' + String(error).slice(0, 90), true);
  }
}

async function persistTheme(theme) {
  applyTheme(theme);
  try {
    const res = await fetch('/api/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme }),
    });
    const data = await res.json();
    if (res.ok && data.profile) profile = data.profile;
  } catch {}
}

async function saveProfile() {
  const payload = {
    name: $('pname').value.trim(),
    about: $('pabout').value.trim(),
    style: $('pstyle').value,
    theme: $('ptheme').value,
  };
  try {
    const res = await fetch('/api/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.profile) throw new Error(data.error || ('HTTP ' + res.status));
    profile = data.profile;
    applyTheme(profile.theme);
    fillProfileForm();
    renderIdentity();
    toast('profile saved');
  } catch (error) {
    toast('could not save profile — ' + String(error).slice(0, 90), true);
  }
}

function resetChat() {
  if (busy) { toast('stop the current run first', true); return; }
  if (queue.length) { queue = []; queueEditId = null; renderQueue(); }
  conversationId = null;
  history.splice(1);
  clearMessages();
  showWelcome();
  renderConversations();
  setSidebar(false);
  input.focus();
}

async function loadConversation(id) {
  if (busy) { toast('stop the current run first', true); return; }
  try {
    const res = await fetch('/api/conversations?id=' + encodeURIComponent(id));
    const data = await res.json();
    if (!res.ok || !Array.isArray(data.messages)) throw new Error(data.error || ('HTTP ' + res.status));
    conversationId = data.id;
    clearMessages();
    history.splice(1);
    for (const message of data.messages) {
      if (message.role !== 'user' && message.role !== 'assistant') continue;
      history.push({ role: message.role, content: message.content });
      renderSavedMessage(message.role, message.content);
    }
    if (data.messages.length) hideWelcome(); else showWelcome();
    renderConversations();
    setSidebar(false);
    setPersonal(false);
    scrollDown();
  } catch (error) {
    toast('could not load chat', true);
  }
}

async function loadState() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    profile = data.profile || {};
    training = data.training || {};
    memories = data.memories || [];
    conversations = data.conversations || [];
    const storedTheme = (() => { try { return localStorage.getItem('neurons.theme'); } catch { return ''; } })();
    applyTheme(storedTheme || profile.theme || 'graphite');
    fillProfileForm();
    applyBackground();
    renderIdentity();
    renderTraining();
    renderMemories();
    renderConversations();
    if (data.conversation && data.conversation.messages && data.conversation.messages.length) {
      conversationId = data.conversation.id;
      clearMessages();
      history.splice(1);
      for (const message of data.conversation.messages) {
        if (message.role !== 'user' && message.role !== 'assistant') continue;
        history.push({ role: message.role, content: message.content });
        renderSavedMessage(message.role, message.content);
      }
      hideWelcome();
      scrollDown();
    } else {
      conversationId = null;
      showWelcome();
    }
    $('dbStatus').textContent = 'sqlite · saved';
    stateLoaded = true;
  } catch (error) {
    toast('memory unavailable — chats will not persist', true);
  } finally {
    window.__neuronsStateResolve();
  }
}

$('newchat').addEventListener('click', resetChat);
$('personalbtn').addEventListener('click', () => setPersonal(true));
$('personalclose').addEventListener('click', () => setPersonal(false));
$('psave').addEventListener('click', saveProfile);
$('bgpick').addEventListener('click', () => $('bgfile').click());
$('bgfile').addEventListener('change', () => uploadBackground($('bgfile').files[0]));
$('bgclear').addEventListener('click', clearBackground);
$('bgblur').addEventListener('input', () => {
  $('bgblurvalue').textContent = $('bgblur').value;
  profile.background_blur = Number($('bgblur').value);
  applyBackground();
});
$('bgdim').addEventListener('input', () => {
  $('bgdimvalue').textContent = $('bgdim').value;
  profile.background_dim = Number($('bgdim').value);
  applyBackground();
});
$('bgblur').addEventListener('change', saveBackgroundSettings);
$('bgdim').addEventListener('change', saveBackgroundSettings);
$('trainingToggle').addEventListener('change', () => setTraining($('trainingToggle').checked));
$('trainingClear').addEventListener('click', clearTraining);
$('sideConnections').addEventListener('click', () => {
  $('connectionsBtn').click();
});
$('memadd').addEventListener('click', addMemory);
$('memtext').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); addMemory(); }
});
$('themeSel').addEventListener('change', () => persistTheme($('themeSel').value));
$('ptheme').addEventListener('change', () => persistTheme($('ptheme').value));
$('menuBtn').addEventListener('click', () => setSidebar(!document.body.classList.contains('sidebar-open')));
$('scrim').addEventListener('click', () => { setSidebar(false); setPersonal(false); });
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') { setSidebar(false); setPersonal(false); }
});
for (const button of document.querySelectorAll('.suggestion')) {
  button.addEventListener('click', () => {
    input.value = button.dataset.prompt || '';
    input.focus();
    submitPrompt();
  });
}

loadState();
