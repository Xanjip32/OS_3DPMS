'use strict';

const FRAME_MS = 200;               // grid refresh: about 5 frames per second per camera
const UPLOAD_RE = /\.(gcode|gco|g|3mf)$/i;
const grid = document.getElementById('grid');
let maxUploadMb = 1024;

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
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), bad ? 10000 : 5000);
}

// ---------------------------------------------------------------- live view
const modal = document.getElementById('modal');
const mimg = document.getElementById('mimg');
let modalPrinter = null;

function openModal(p) {
  modalPrinter = p;
  document.getElementById('mname').textContent = p.name;
  document.getElementById('mopen').classList.toggle('hidden', !p.ui_port);
  mimg.style.transform = camTransform(p);
  mimg.src = `/cam/${encodeURIComponent(p.id)}?t=${Date.now()}`;
  modal.classList.add('show');
}
function closeModal() {
  modal.classList.remove('show');
  mimg.removeAttribute('src');
  modalPrinter = null;
}
document.getElementById('mclose').addEventListener('click', closeModal);
document.getElementById('mopen').addEventListener('click', () => window.open(mainsailUrl(modalPrinter), '_blank', 'noopener'));
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// A file dropped outside a tile must not open in the browser.
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

// ---------------------------------------------------------------- tiles
function buildTile(p) {
  const t = document.getElementById('tile-template').content.firstElementChild.cloneNode(true);
  const $ = sel => t.querySelector(sel);
  const id = encodeURIComponent(p.id);

  $('.name').textContent = p.name;
  $('.target').textContent = p.name;
  $('.drop').textContent = `Drop to send to ${p.name}`;
  $('.open').textContent = p.ui_port ? 'Open Mainsail' : 'Live view';
  if (!p.ui_port) $('.ui').remove();

  const img = $('.view img');
  const chip = $('.chip');
  const fileLine = $('.file');
  const nocam = $('.view .nocam');
  const bar = $('.progress > div');
  const input = $('input[type=file]');
  const sendBtn = $('.send');
  const startBox = $('.start');
  let pending = null;
  let busy = false;
  let stopped = false;
  t.stop = () => { stopped = true; img.removeAttribute('src'); };
  $('.hide').addEventListener('click', e => {
    e.stopPropagation();
    if (busy) { toast(`${p.name}: wait until the upload finishes`, true); return; }
    hidePrinter(p.id, true);
  });

  img.style.transform = camTransform(p);

  // Camera: poll snapshots. Short requests keep browser connections free.
  function nextFrame() {
    if (stopped) return;
    if (document.hidden || modal.classList.contains('show')) { setTimeout(nextFrame, 1000); return; }
    img.src = `/snap/${id}?t=${Date.now()}`;
  }
  img.addEventListener('load', () => { t.classList.remove('nocam'); setTimeout(nextFrame, FRAME_MS); });
  img.addEventListener('error', () => {
    t.classList.add('nocam');
    nocam.textContent = 'Camera offline. Retrying…';
    setTimeout(nextFrame, 3000);
  });
  nextFrame();

  const openUi = () => window.open(mainsailUrl(p), '_blank', 'noopener');
  $('.view').addEventListener('click', () => (p.ui_port ? openUi() : openModal(p)));
  $('.live').addEventListener('click', () => openModal(p));
  if (p.ui_port) $('.ui').addEventListener('click', openUi);

  // Status (Klipper printers)
  async function status() {
    if (stopped) return;
    try {
      const r = await fetch(`/api/status/${id}`);
      if (r.ok) {
        const s = await r.json();
        if (s.state) {
          const active = s.state === 'printing' || s.state === 'paused';
          chip.textContent = s.state[0].toUpperCase() + s.state.slice(1) + (active ? ` ${Math.round(s.progress * 100)}%` : '');
          chip.className = 'chip ' + s.state;
          if (!busy) fileLine.textContent = active ? s.file : '';
        }
      }
    } catch (e) { /* dashboard restarting */ }
    setTimeout(status, 5000);
  }
  status();

  // Upload: drag and drop, or file picker
  let depth = 0;
  t.addEventListener('dragenter', e => { e.preventDefault(); depth++; t.classList.add('drag'); });
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
  startBox.addEventListener('change', () => $('.warn').classList.toggle('hidden', !startBox.checked));

  function ask(file) {
    if (busy) { toast(`${p.name}: upload already running`, true); return; }
    if (!UPLOAD_RE.test(file.name)) { toast('Only .gcode or .3mf files', true); return; }
    if (file.size > maxUploadMb * 1048576) { toast(`File is larger than ${maxUploadMb} MB`, true); return; }
    pending = file;
    $('.fname').textContent = `${file.name} (${(file.size / 1048576).toFixed(1)} MB)`;
    startBox.checked = false;
    $('.warn').classList.add('hidden');
    t.classList.add('confirming');
  }
  $('.cancel').addEventListener('click', () => { pending = null; t.classList.remove('confirming'); });
  $('.go').addEventListener('click', () => {
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

  return t;
}

// ---------------------------------------------------------------- layout
// Pick the column count that gives the biggest 16:9 camera with every tile on screen.
// Phones fall back to one scrolling column.
function layout() {
  const tiles = grid.querySelectorAll('.tile');
  const n = tiles.length;
  if (!n) return;
  const header = document.querySelector('header').offsetHeight;
  const cs = getComputedStyle(grid);
  const gap = parseFloat(cs.columnGap) || 12;
  const W = grid.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const H = window.innerHeight - header - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
  const view = tiles[0].querySelector('.view');
  const chrome = Math.max(80, tiles[0].offsetHeight - view.offsetHeight);

  let best = { cols: 1, rows: n, cam: 0 };
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols);
    const tileW = (W - gap * (cols - 1)) / cols;
    const camH = Math.min((H - gap * (rows - 1)) / rows - chrome, tileW * 9 / 16);
    if (camH > best.cam) best = { cols, rows, cam: camH };
  }

  const fit = window.innerWidth >= 700 && best.cam >= 160;
  document.body.classList.toggle('fit', fit);
  if (fit) {
    grid.style.setProperty('--cols', best.cols);
    grid.style.setProperty('--rows', best.rows);
    grid.style.height = (window.innerHeight - header) + 'px';
  } else {
    grid.style.setProperty('--cols', window.innerWidth >= 700 ? 2 : 1);
    grid.style.height = '';
  }
}
window.addEventListener('resize', layout);

function notice(text) {
  const el = document.createElement('div');
  el.className = 'notice';
  el.textContent = text;
  grid.replaceChildren(el);
}

// ---------------------------------------------------------------- alerts
// Print events (finished, failed, paused...) show as a toast, and as a system
// pop-up when the browser allows notifications (needs HTTPS or localhost).
const alertsBtn = document.getElementById('alerts');
const canNotify = 'Notification' in window && window.isSecureContext;
function updateAlertsButton() {
  alertsBtn.classList.toggle('hidden', !canNotify || Notification.permission !== 'default');
}
alertsBtn.addEventListener('click', async () => {
  try { await Notification.requestPermission(); } catch (e) { /* ignored */ }
  updateAlertsButton();
});

// ---------------------------------------------------------------- installed app
let swReg = null;
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('/sw.js').then(r => { swReg = r; }).catch(() => {});
}
const installBtn = document.getElementById('install');
const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
let installPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  installPrompt = e;
  installBtn.classList.remove('hidden');
});
window.addEventListener('appinstalled', () => installBtn.classList.add('hidden'));
if (isIOS && !standalone) installBtn.classList.remove('hidden');
installBtn.addEventListener('click', async () => {
  if (installPrompt) {
    installPrompt.prompt();
    await installPrompt.userChoice.catch(() => null);
    installPrompt = null;
    installBtn.classList.add('hidden');
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

// ---------------------------------------------------------------- hidden cameras
// Hidden cameras are remembered per device (this browser only) and stop streaming.
const HIDDEN_KEY = 'os3dpms.hidden';
const hiddenBtn = document.getElementById('hidden-btn');
const hiddenMenu = document.getElementById('hidden-menu');
const tiles = new Map();
let printers = [];
let hiddenIds = new Set();
try { hiddenIds = new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]')); } catch (e) { /* no storage */ }

function saveHidden() {
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenIds])); } catch (e) { /* no storage */ }
}

function hidePrinter(id, hide) {
  if (hide) hiddenIds.add(id); else hiddenIds.delete(id);
  saveHidden();
  render();
}

function render() {
  for (const p of printers) {
    const tile = tiles.get(p.id);
    if (!hiddenIds.has(p.id) && !tile) tiles.set(p.id, buildTile(p));
    if (hiddenIds.has(p.id) && tile) { tile.stop(); tiles.delete(p.id); }
  }
  const visible = printers.filter(p => tiles.has(p.id)).map(p => tiles.get(p.id));
  grid.replaceChildren(...visible);
  if (!visible.length) {
    const el = document.createElement('div');
    el.className = 'notice';
    el.textContent = 'All cameras are hidden. Use the "Hidden" button at the top to show them again.';
    grid.append(el);
    grid.style.height = '';
  }
  renderHiddenMenu();
  layout();
}

function renderHiddenMenu() {
  const hiddenList = printers.filter(p => hiddenIds.has(p.id));
  hiddenBtn.classList.toggle('hidden', !hiddenList.length);
  hiddenBtn.textContent = `Hidden (${hiddenList.length})`;
  const items = hiddenList.map(p => {
    const b = document.createElement('button');
    b.textContent = `Show ${p.name}`;
    b.addEventListener('click', () => hidePrinter(p.id, false));
    return b;
  });
  if (hiddenList.length > 1) {
    const all = document.createElement('button');
    all.className = 'primary';
    all.textContent = 'Show all';
    all.addEventListener('click', () => { hiddenIds.clear(); saveHidden(); render(); });
    items.push(all);
  }
  hiddenMenu.replaceChildren(...items);
  if (!hiddenList.length) toggleMenu(false);
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

async function start() {
  try {
    const me = await fetch('/api/me');
    const info = await me.json();
    if (!me.ok) { notice(info.error || 'Access denied'); return; }
    maxUploadMb = info.max_upload_mb || maxUploadMb;
    document.getElementById('who').textContent = info.admin ? 'This PC (admin) · Setup at /setup' : info.user;
    const list = await (await fetch('/api/printers')).json();
    if (!list.length) { notice('No printers yet. Add them on the setup page of the printer PC.'); return; }
    printers = list;
    render();
    updateAlertsButton();
    pollEvents();
  } catch (e) {
    notice('Cannot reach the dashboard server.');
  }
}
start();
