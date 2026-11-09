'use strict';

const FRAME_MS = 200;               // grid refresh: about 5 frames per second per camera
const STATUS_MS = 5000;
const UPLOAD_RE = /\.(gcode|gco|g|3mf)$/i;
const ACTIVE = new Set(['printing', 'paused']);
const $ = id => document.getElementById(id);
const grid = $('grid');
const visibleCameras = new WeakSet();
// Cards count as visible until the observer has reported on them, so cameras start at once.
const seenByObserver = new WeakSet();
const cameraObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    seenByObserver.add(entry.target);
    if (entry.isIntersecting) visibleCameras.add(entry.target);
    else visibleCameras.delete(entry.target);
  }
});

let info = {};
let maxUploadMb = 1024;
let monitorOnly = false;
let pushKey = '';
let pushRegistered = false;
let pushManager = null;
let pushSubscription = null;

let printers = [];
const statuses = new Map();          // printer id -> last status from the server
const tiles = new Map();             // printer id -> camera card

// ---------------------------------------------------------------- small helpers
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  kids.forEach(k => e.append(k));
  return e;
}

function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (e) { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* no storage */ }
}

function mainsailUrl(p) {
  return `${location.protocol}//${location.hostname}:${p.ui_port}/`;
}

// Camera orientation from config: rotate (degrees), flip_horizontal, flip_vertical.
function camTransform(p) {
  const parts = [];
  if (p.rotate) parts.push(`rotate(${p.rotate}deg)`);
  if (p.flip_horizontal || p.flip_vertical) {
    parts.push(`scale(${p.flip_horizontal ? -1 : 1}, ${p.flip_vertical ? -1 : 1})`);
  }
  return parts.join(' ');
}

function toast(msg, bad) {
  const t = el('div', { class: 'toast' + (bad ? ' bad' : ''), text: msg });
  $('toasts').appendChild(t);
  setTimeout(() => t.remove(), bad ? 10000 : 5000);
}

function stateOf(p) {
  return (statuses.get(p.id) || {}).state || 'unknown';
}

function stateText(st) {
  if (!st || !st.state || st.state === 'unknown') return 'No status';
  const label = st.state[0].toUpperCase() + st.state.slice(1);
  return ACTIVE.has(st.state) ? `${label} ${Math.round((st.progress || 0) * 100)}%` : label;
}

function chip(st) {
  return el('span', { class: 'chip ' + ((st && st.state) || ''), text: stateText(st) });
}

function canMainsail(p) {
  return !monitorOnly && p.ui_port;
}

// ---------------------------------------------------------------- live view
const modal = $('modal');
const mimg = $('mimg');
let modalPrinter = null;
let modalTimer;

function openModal(p) {
  modalPrinter = p;
  $('mname').textContent = p.name;
  $('mopen').classList.toggle('hidden', !canMainsail(p));
  mimg.style.transform = camTransform(p);
  if (monitorOnly) {
    const refresh = () => {
      if (!document.hidden) mimg.src = `/snap/${encodeURIComponent(p.id)}?t=${Date.now()}`;
      modalTimer = setTimeout(refresh, 2500);
    };
    refresh();
  } else {
    mimg.src = `/cam/${encodeURIComponent(p.id)}?t=${Date.now()}`;
  }
  modal.classList.add('show');
}
function closeModal() {
  clearTimeout(modalTimer);
  modal.classList.remove('show');
  mimg.removeAttribute('src');
  modalPrinter = null;
}
$('mclose').addEventListener('click', closeModal);
$('mopen').addEventListener('click', () => window.open(mainsailUrl(modalPrinter), '_blank', 'noopener'));
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// A file dropped outside a card must not open in the browser.
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

// ---------------------------------------------------------------- camera cards
function buildTile(p) {
  const t = $('tile-template').content.firstElementChild.cloneNode(true);
  const q = sel => t.querySelector(sel);
  const id = encodeURIComponent(p.id);

  q('.name').textContent = p.name;
  q('.target').textContent = p.name;
  q('.drop').textContent = `Drop to send to ${p.name}`;
  q('.open').textContent = 'Live view';
  q('.view').setAttribute('aria-label', `View camera for ${p.name}`);
  if (!canMainsail(p)) q('.ui').remove();

  const img = q('.view img');
  const chipEl = q('.chip');
  const fileLine = q('.file');
  const nocam = q('.view .nocam');
  const bar = q('.progress > div');
  const input = q('input[type=file]');
  const sendBtn = q('.send');
  if (monitorOnly) sendBtn.classList.add('hidden');
  const startBox = q('.start');
  let pending = null;
  let busy = false;
  let stopped = false;

  t.stop = () => {
    stopped = true;
    cameraObserver.unobserve(t);
    img.removeAttribute('src');
    if (frameUrl) URL.revokeObjectURL(frameUrl);
  };
  t.update = st => {
    chipEl.textContent = st && st.state ? stateText(st) : '';
    chipEl.className = 'chip ' + ((st && st.state) || '');
    if (!busy) fileLine.textContent = st && ACTIVE.has(st.state) ? (st.file || '') : '';
  };
  t.pick = () => { if (!monitorOnly) input.click(); };
  t.isBusy = () => busy;

  q('.hide').addEventListener('click', e => {
    e.stopPropagation();
    if (busy) { toast(`${p.name}: wait until the upload finishes`, true); return; }
    hidePrinter(p.id, true);
  });

  img.style.transform = camTransform(p);

  // Camera: fetch snapshots while the card is on screen. Short requests keep connections free.
  // fetch (not img.src) so the server's reason is shown when there is no picture.
  let frameUrl = '';
  async function nextFrame() {
    if (stopped) return;
    const offscreen = seenByObserver.has(t) && !visibleCameras.has(t);
    if (document.hidden || offscreen || modal.classList.contains('show')) {
      setTimeout(nextFrame, 1000);
      return;
    }
    try {
      const r = await fetch(`/snap/${id}?t=${Date.now()}`, { cache: 'no-store' });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.error || `Camera error (HTTP ${r.status})`);
      }
      const blob = await r.blob();
      if (stopped) return;
      const url = URL.createObjectURL(blob);
      img.onload = () => {
        if (frameUrl) URL.revokeObjectURL(frameUrl);
        frameUrl = url;
        t.classList.remove('nocam');
      };
      img.onerror = () => URL.revokeObjectURL(url);   // damaged picture: wait for the next one
      img.src = url;
      setTimeout(nextFrame, monitorOnly ? 4000 : FRAME_MS);
    } catch (e) {
      if (stopped) return;
      t.classList.add('nocam');
      nocam.textContent = `${e.message || 'Camera offline'}. Retrying…`;
      setTimeout(nextFrame, 3000);
    }
  }
  cameraObserver.observe(t);
  nextFrame();

  const openUi = () => window.open(mainsailUrl(p), '_blank', 'noopener');
  q('.view').addEventListener('click', () => openModal(p));
  q('.live').addEventListener('click', () => openModal(p));
  if (canMainsail(p)) q('.ui').addEventListener('click', openUi);

  // Upload: drag and drop, or file picker
  let depth = 0;
  t.addEventListener('dragenter', e => { e.preventDefault(); if (monitorOnly) return; depth++; t.classList.add('drag'); });
  t.addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; t.classList.remove('drag'); } });
  t.addEventListener('dragover', e => e.preventDefault());
  t.addEventListener('drop', e => {
    e.preventDefault();
    depth = 0;
    t.classList.remove('drag');
    if (e.dataTransfer.files.length) ask(e.dataTransfer.files[0]);
  });
  sendBtn.addEventListener('click', () => input.click());
  input.addEventListener('change', () => { if (input.files.length) ask(input.files[0]); input.value = ''; });
  startBox.addEventListener('change', () => q('.warn').classList.toggle('hidden', !startBox.checked));

  function ask(file) {
    if (monitorOnly) return;
    if (busy) { toast(`${p.name}: upload already running`, true); return; }
    if (!UPLOAD_RE.test(file.name)) { toast('Only .gcode or .3mf files', true); return; }
    if (file.size > maxUploadMb * 1048576) { toast(`File is larger than ${maxUploadMb} MB`, true); return; }
    pending = file;
    q('.fname').textContent = `${file.name} (${(file.size / 1048576).toFixed(1)} MB)`;
    startBox.checked = false;
    q('.warn').classList.add('hidden');
    t.classList.add('confirming');
  }
  q('.cancel').addEventListener('click', () => { pending = null; t.classList.remove('confirming'); });
  q('.go').addEventListener('click', () => {
    t.classList.remove('confirming');
    upload(pending, startBox.checked);
    pending = null;
  });

  function upload(file, start) {
    busy = true;
    sendBtn.disabled = true;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/upload/${id}?name=${encodeURIComponent(file.name)}&print=${start ? 1 : 0}`);
    xhr.setRequestHeader('X-Dashboard', '1');
    xhr.upload.addEventListener('progress', e => {
      if (!e.lengthComputable) return;
      const pct = Math.round(e.loaded / e.total * 100);
      bar.style.width = pct + '%';
      fileLine.textContent = pct < 100 ? `Uploading ${file.name}… ${pct}%` : `Sending to ${p.name}…`;
    });
    const done = (ok, msg) => {
      busy = false;
      sendBtn.disabled = false;
      bar.style.width = '0';
      fileLine.textContent = '';
      toast(msg, !ok);
    };
    xhr.addEventListener('load', () => {
      let r = {};
      try { r = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status === 200 && r.ok) done(true, r.message);
      else done(false, `${p.name}: ${r.error || 'upload failed (' + xhr.status + ')'}`);
    });
    xhr.addEventListener('error', () => done(false, `${p.name}: lost connection to dashboard`));
    xhr.send(file);
  }

  t.update(statuses.get(p.id));
  return t;
}

// ---------------------------------------------------------------- dashboard filters and views
let filter = load('os3dpms.filter', 'all');
let view = load('os3dpms.view', 'grid');
let query = '';
let hiddenIds = new Set(load('os3dpms.hidden', []));

function matches(p) {
  if (query && !p.name.toLowerCase().includes(query)) return false;
  const active = ACTIVE.has(stateOf(p));
  return filter === 'all' || (filter === 'active' ? active : !active);
}

function setPressed(attr, value) {
  document.querySelectorAll(`[${attr}]`).forEach(b => b.setAttribute('aria-pressed', String(b.getAttribute(attr) === value)));
}

document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => {
  filter = b.getAttribute('data-filter');
  save('os3dpms.filter', filter);
  refresh();
}));
document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => {
  view = b.getAttribute('data-view');
  save('os3dpms.view', view);
  refresh();
}));
$('search').addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); refresh(); });
$('device-search').addEventListener('input', () => renderDevices());

function hidePrinter(id, hide) {
  if (hide) hiddenIds.add(id); else hiddenIds.delete(id);
  save('os3dpms.hidden', [...hiddenIds]);
  render();
}

function notice(container, text, localSetup = false) {
  const panel = el('div', { class: 'empty-card' }, el('strong', { text }));
  if (localSetup) panel.append(el('div', { class: 'actions' }, el('a', { class: 'btn primary', href: '/setup', text: 'Add a printer' })));
  container.replaceChildren(panel);
}

// Create or remove camera cards to match the printer list and hidden cameras.
function render() {
  for (const p of printers) {
    const tile = tiles.get(p.id);
    if (!hiddenIds.has(p.id) && !tile) tiles.set(p.id, buildTile(p));
    if (hiddenIds.has(p.id) && tile) { tile.stop(); tiles.delete(p.id); }
  }
  for (const [id, tile] of tiles) {
    if (!printers.some(p => p.id === id)) { tile.stop(); tiles.delete(id); }
  }
  grid.replaceChildren(...printers.filter(p => tiles.has(p.id)).map(p => tiles.get(p.id)));
  refresh();
}

// Update everything that depends on status, filters or the view.
function refresh() {
  setPressed('data-filter', filter);
  setPressed('data-view', view);
  for (const p of printers) {
    const tile = tiles.get(p.id);
    if (!tile) continue;
    tile.update(statuses.get(p.id));
    tile.classList.toggle('filtered', !matches(p));
  }
  const shown = printers.filter(p => !hiddenIds.has(p.id) && matches(p));
  grid.classList.toggle('hidden', view !== 'grid');
  $('list').classList.toggle('hidden', view !== 'list');
  grid.querySelector(':scope > .empty-card')?.remove();
  if (!printers.length) {
    notice(grid, info.admin ? 'No printers yet.' : 'No printers yet. Add them in Setup on the printer PC.', info.admin);
    $('list').replaceChildren();
  } else if (!shown.length) {
    const why = printers.every(p => hiddenIds.has(p.id)) ? 'All cameras are hidden. Use "Hidden" above to show them.'
      : 'No printers match this filter.';
    grid.append(el('div', { class: 'empty-card' }, el('strong', { text: why })));
  }
  if (view === 'list' && printers.length) renderList(shown);
  renderCounts();
  renderDevices();
  renderHiddenMenu();
  renderSettings();
}

function renderCounts() {
  const count = s => printers.filter(p => stateOf(p) === s).length;
  const offline = printers.filter(p => ['unknown', 'offline', 'error'].includes(stateOf(p))).length;
  const idle = printers.length - count('printing') - count('paused') - offline;
  const item = (n, label, kind) => el('span', { class: 'count ' + kind }, el('i'), el('b', { text: String(n) }), ' ' + label);
  const parts = [item(count('printing'), 'printing', 'ok'), item(count('paused'), 'paused', 'warn'),
    item(idle, 'idle', 'idle'), item(offline, 'offline / error', 'bad')];
  $('counts').replaceChildren(...parts);
  $('device-counts').replaceChildren(item(printers.length, printers.length === 1 ? 'printer' : 'printers', 'idle'));
}

function actionButtons(p, withSend) {
  const box = el('div', { class: 'row-actions' });
  box.append(el('button', { text: 'Camera', onclick: () => openModal(p) }));
  if (canMainsail(p)) box.append(el('button', { text: 'Mainsail', onclick: () => window.open(mainsailUrl(p), '_blank', 'noopener') }));
  if (withSend && !monitorOnly && tiles.has(p.id)) {
    box.append(el('button', { class: 'primary', text: 'Send file', onclick: () => {
      view = 'grid';
      save('os3dpms.view', view);
      refresh();
      tiles.get(p.id).scrollIntoView({ behavior: 'smooth', block: 'center' });
      tiles.get(p.id).pick();
    } }));
  }
  return box;
}

function table(columns, rows) {
  const head = el('tr', {}, ...columns.map(c => el('th', { text: c.label, class: c.class || '' })));
  const body = rows.map(p => el('tr', {}, ...columns.map(c => {
    const value = c.cell(p);
    const td = el('td', { class: c.class || '', 'data-label': c.label });
    td.append(value instanceof Node ? value : document.createTextNode(value ?? ''));
    return td;
  })));
  return el('table', {}, el('thead', {}, head), el('tbody', {}, ...body));
}

function progressCell(p) {
  const st = statuses.get(p.id) || {};
  if (!ACTIVE.has(st.state)) return '—';
  const pct = Math.round((st.progress || 0) * 100);
  const bar = el('span', { class: 'mini-bar' }, el('span'));
  bar.firstChild.style.width = pct + '%';
  return el('span', { class: 'progress-cell' }, bar, el('span', { text: pct + '%' }));
}

function renderList(rows) {
  $('list').replaceChildren(table([
    { label: 'Name', cell: p => el('b', { text: p.name }) },
    { label: 'Status', cell: p => chip(statuses.get(p.id)) },
    { label: 'Progress', cell: progressCell },
    { label: 'File', class: 'muted', cell: p => (ACTIVE.has(stateOf(p)) && statuses.get(p.id).file) || '—' },
    { label: '', class: 'actions-col', cell: p => actionButtons(p, true) },
  ], rows));
}

function renderDevices() {
  const box = $('devices');
  if (!printers.length) { notice(box, 'No printers yet.', info.admin); return; }
  const term = $('device-search').value.trim().toLowerCase();
  const rows = printers.filter(p => !term || p.name.toLowerCase().includes(term) || (p.type || '').includes(term));
  box.replaceChildren(table([
    { label: 'Name', cell: p => el('b', { text: p.name }) },
    { label: 'Type', cell: p => (p.type === 'bambu' ? 'Bambu Lab' : 'Klipper') },
    { label: 'Status', cell: p => chip(statuses.get(p.id)) },
    { label: 'Progress', cell: progressCell },
    { label: 'Camera', class: 'muted', cell: p => (hiddenIds.has(p.id) ? 'Hidden on this device' : (p.rotate ? `Rotated ${p.rotate}°` : 'Shown')) },
    { label: '', class: 'actions-col', cell: p => actionButtons(p, false) },
  ], rows));
}

// ---------------------------------------------------------------- hidden cameras menu
const hiddenBtn = $('hidden-btn');
const hiddenMenu = $('hidden-menu');

function showButtons() {
  const hiddenList = printers.filter(p => hiddenIds.has(p.id));
  const items = hiddenList.map(p => el('button', { text: `Show ${p.name}`, onclick: () => hidePrinter(p.id, false) }));
  if (hiddenList.length > 1) {
    items.push(el('button', { class: 'primary', text: 'Show all', onclick: () => {
      hiddenIds.clear();
      save('os3dpms.hidden', []);
      render();
    } }));
  }
  return items;
}

function renderHiddenMenu() {
  const n = printers.filter(p => hiddenIds.has(p.id)).length;
  hiddenBtn.classList.toggle('hidden', !n);
  hiddenBtn.textContent = `Hidden (${n})`;
  hiddenMenu.replaceChildren(...showButtons());
  if (!n) toggleMenu(false);
}

function toggleMenu(open) {
  // Open towards the middle of the screen so the menu never runs off an edge.
  const onRight = hiddenBtn.getBoundingClientRect().left > window.innerWidth / 2;
  hiddenMenu.style.left = onRight ? 'auto' : '0';
  hiddenMenu.style.right = onRight ? '0' : 'auto';
  hiddenMenu.classList.toggle('hidden', !open);
  hiddenBtn.setAttribute('aria-expanded', String(open));
}
hiddenBtn.addEventListener('click', e => { e.stopPropagation(); toggleMenu(hiddenMenu.classList.contains('hidden')); });
document.addEventListener('click', e => { if (!hiddenMenu.contains(e.target)) toggleMenu(false); });

// ---------------------------------------------------------------- settings page
function renderSettings() {
  $('account-text').textContent = monitorOnly
    ? `Signed in as ${info.user || 'the dashboard owner'}. This is the cloud dashboard: it shows cameras and status from anywhere.`
    : info.admin ? 'You are on the printer PC itself.'
      : `Signed in through Tailscale as ${info.user || 'a tailnet user'}.`;
  $('sign-out').classList.toggle('hidden', !(monitorOnly && info.logout === '/auth/logout'));
  $('setup-text').textContent = info.admin
    ? 'Add or edit printers, phone notifications and the cloud dashboard.'
    : 'Printers, notifications and the cloud connection are set up on the printer PC (Setup page).';
  const actions = [];
  if (!installBtn.classList.contains('hidden')) actions.push(el('button', { text: 'Install app', onclick: () => installBtn.click() }));
  if (!alertsBtn.classList.contains('hidden')) actions.push(el('button', { text: alertsBtn.textContent, onclick: () => alertsBtn.click() }));
  if (!actions.length) actions.push(el('span', { class: 'muted', text: standalone ? 'Installed as an app.' : 'Nothing to set up here.' }));
  $('device-actions').replaceChildren(...actions);
  const show = showButtons();
  $('hidden-list').replaceChildren(...(show.length ? show : [el('span', { class: 'muted', text: 'No hidden cameras.' })]));
}

// ---------------------------------------------------------------- pages
const PAGES = { dashboard: 'Dashboard', devices: 'Devices', queue: 'Queue', files: 'Files', settings: 'Settings' };

function route() {
  let page = location.hash.replace(/^#\/?/, '');
  if (!PAGES[page]) page = 'dashboard';
  document.querySelectorAll('[data-page]').forEach(s => { s.hidden = s.getAttribute('data-page') !== page; });
  document.querySelectorAll('[data-nav]').forEach(a => {
    if (a.getAttribute('data-nav') === page) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $('crumb').textContent = PAGES[page];
  document.title = `${PAGES[page]} · Printers`;
}
window.addEventListener('hashchange', route);

// ---------------------------------------------------------------- status
async function pollStatuses() {
  await Promise.all(printers.map(async p => {
    try {
      const r = await fetch(`/api/status/${encodeURIComponent(p.id)}`);
      if (r.ok) statuses.set(p.id, await r.json());
    } catch (e) { /* dashboard restarting */ }
  }));
  refresh();
  setTimeout(pollStatuses, STATUS_MS);
}

// ---------------------------------------------------------------- alerts
// Print events (finished, failed, paused...) show as a toast, and as a system
// pop-up when the browser allows notifications (needs HTTPS or localhost).
const alertsBtn = $('alerts');
const canNotify = 'Notification' in window && window.isSecureContext;
function updateAlertsButton() {
  if (monitorOnly) {
    alertsBtn.classList.toggle('hidden', !canNotify || !pushKey || !pushManager || Notification.permission === 'denied');
    alertsBtn.textContent = pushRegistered ? 'Test alerts' : 'Enable alerts';
  } else {
    alertsBtn.classList.toggle('hidden', !canNotify || Notification.permission !== 'default');
  }
  renderSettings();
}
alertsBtn.addEventListener('click', async () => {
  try {
    if (!monitorOnly) {
      await Notification.requestPermission();
      return;
    }
    const base64 = pushKey.replace(/-/g, '+').replace(/_/g, '/');
    const key = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
    pushSubscription ??= await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    const response = await fetch(pushRegistered ? '/api/push/test' : '/api/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Dashboard': '1' },
      body: JSON.stringify(pushSubscription),
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.error || 'Could not set up alerts.');
    }
    toast(pushRegistered ? 'Test alert sent.' : 'Alerts enabled on this device.');
    pushRegistered = true;
  } catch (e) {
    if (monitorOnly) toast(e.message || 'Could not set up alerts.', true);
  } finally {
    updateAlertsButton();
  }
});

// ---------------------------------------------------------------- installed app
let swReg = null;
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('/sw.js').then(r => { swReg = r; }).catch(() => {});
}
const installBtn = $('install');
const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
let installPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  installPrompt = e;
  installBtn.classList.remove('hidden');
  renderSettings();
});
window.addEventListener('appinstalled', () => { installBtn.classList.add('hidden'); renderSettings(); });
if (isIOS && !standalone) installBtn.classList.remove('hidden');
installBtn.addEventListener('click', async () => {
  if (installPrompt) {
    installPrompt.prompt();
    await installPrompt.userChoice.catch(() => null);
    installPrompt = null;
    installBtn.classList.add('hidden');
    renderSettings();
  } else if (isIOS) {
    toast('In Safari: tap the Share button (square with arrow), then "Add to Home Screen".');
  }
});

function popup(event) {
  const bad = event.kind === 'error' || event.kind === 'offline';
  toast(event.text, bad);
  if (!canNotify || Notification.permission !== 'granted') return;
  const opts = { body: event.text, tag: 'os3dpms-' + event.id, icon: '/icon-192.png' };
  if (swReg) {
    swReg.showNotification(event.name, opts).catch(() => {});   // works on phones too
    return;
  }
  try {
    new Notification(event.name, opts);
  } catch (e) { /* the toast is enough */ }
}

let eventCursor = null;
async function pollEvents() {
  try {
    const r = await fetch(`/api/events?after=${eventCursor === null ? 0 : eventCursor}`);
    if (r.ok) {
      const data = await r.json();
      if (eventCursor !== null) data.events.forEach(popup);   // skip old events on first load
      eventCursor = data.last;
    }
  } catch (e) { /* dashboard restarting */ }
  setTimeout(pollEvents, 5000);
}

// ---------------------------------------------------------------- start
async function pollCloudPrinters() {
  try {
    const response = await fetch('/api/printers');
    const list = await response.json().catch(() => null);
    if (!response.ok || !Array.isArray(list)) throw new Error(list?.error || 'Reload and sign in to view your printers.');
    if (JSON.stringify(list) !== JSON.stringify(printers)) {
      for (const tile of tiles.values()) tile.stop();
      tiles.clear();
      printers = list;
      render();
    }
  } catch (e) {
    for (const tile of tiles.values()) tile.stop();
    tiles.clear();
    printers = [];
    render();
    notice(grid, e.message);
  }
  setTimeout(pollCloudPrinters, 8000);
}

function showUser() {
  const who = monitorOnly ? (info.user || 'Cloud') : info.admin ? 'This PC (admin)' : (info.user || '');
  $('who').textContent = who;
  $('who').classList.toggle('hidden', !who);
  document.querySelectorAll('.admin-only').forEach(a => a.classList.toggle('hidden', !info.admin));
}

async function start() {
  route();
  try {
    const me = await fetch('/api/me');
    info = await me.json();
    if (!me.ok) { notice(grid, info.error || 'Access denied'); return; }
    monitorOnly = !!info.monitor_only;
    showUser();
    if (monitorOnly) {
      pushKey = info.push_key;
      updateAlertsButton();
      if (canNotify && pushKey && 'PushManager' in window) {
        navigator.serviceWorker.ready.then(async registration => {
          pushManager = registration.pushManager;
          pushSubscription = await pushManager.getSubscription();
          updateAlertsButton();
        }).catch(() => {});
      }
      await pollCloudPrinters();
      pollStatuses();
      return;
    }
    maxUploadMb = info.max_upload_mb || maxUploadMb;
    printers = await (await fetch('/api/printers')).json();
    render();
    updateAlertsButton();
    pollStatuses();
    pollEvents();
  } catch (e) {
    notice(grid, 'Cannot reach the dashboard server.');
  }
}
start();
