'use strict';

const FRAME_MS = 200;               // grid refresh: about 5 frames per second per camera
const UPLOAD_RE = /\.(gcode|gco|g|3mf)$/i;
const grid = document.getElementById('grid');
const visibleCameras = new WeakSet();
const cameraObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (entry.isIntersecting) visibleCameras.add(entry.target);
    else visibleCameras.delete(entry.target);
  }
});
let maxUploadMb = 1024;
let monitorOnly = false;
let pushKey = '';
let pushRegistered = false;
let pushManager = null;
let pushSubscription = null;

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
let modalTimer;

function openModal(p) {
  modalPrinter = p;
  document.getElementById('mname').textContent = p.name;
  document.getElementById('mopen').classList.toggle('hidden', !p.ui_port);
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
  $('.open').textContent = 'Live view';
  $('.view').setAttribute('aria-label', `View camera for ${p.name}`);
  if (!p.ui_port) $('.ui').remove();

  const img = $('.view img');
  const chip = $('.chip');
  const fileLine = $('.file');
  const nocam = $('.view .nocam');
  const bar = $('.progress > div');
  const input = $('input[type=file]');
  const sendBtn = $('.send');
  if (monitorOnly) sendBtn.classList.add('hidden');
  const startBox = $('.start');
  let pending = null;
  let busy = false;
  let stopped = false;
  t.stop = () => {
    stopped = true;
    cameraObserver.unobserve(t);
    img.removeAttribute('src');
  };
  $('.hide').addEventListener('click', e => {
    e.stopPropagation();
    if (busy) { toast(`${p.name}: wait until the upload finishes`, true); return; }
    hidePrinter(p.id, true);
  });

  img.style.transform = camTransform(p);

  // Camera: poll snapshots. Short requests keep browser connections free.
  function nextFrame() {
    if (stopped) return;
    if (document.hidden || !visibleCameras.has(t) || modal.classList.contains('show')) {
      setTimeout(nextFrame, 1000);
      return;
    }
    img.src = `/snap/${id}?t=${Date.now()}`;
  }
  img.addEventListener('load', () => { t.classList.remove('nocam'); setTimeout(nextFrame, monitorOnly ? 4000 : FRAME_MS); });
  img.addEventListener('error', () => {
    t.classList.add('nocam');
    nocam.textContent = 'Camera offline. Retrying…';
    setTimeout(nextFrame, 3000);
  });
  cameraObserver.observe(t);
  nextFrame();

  const openUi = () => window.open(mainsailUrl(p), '_blank', 'noopener');
  $('.view').addEventListener('click', () => openModal(p));
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
    if (monitorOnly) return;
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

function notice(text, localSetup = false) {
  const panel = document.createElement('div');
  panel.className = 'notice';
  const message = document.createElement('p');
  message.textContent = text;
  panel.append(message);
  if (localSetup) {
    const link = document.createElement('a');
    link.className = 'btn primary';
    link.href = '/setup';
    link.textContent = 'Add a printer';
    panel.append(link);
  }
  grid.replaceChildren(panel);
}

// ---------------------------------------------------------------- alerts
// Print events (finished, failed, paused...) show as a toast, and as a system
// pop-up when the browser allows notifications (needs HTTPS or localhost).
const alertsBtn = document.getElementById('alerts');
const canNotify = 'Notification' in window && window.isSecureContext;
function updateAlertsButton() {
  if (monitorOnly) {
    alertsBtn.classList.toggle('hidden', !canNotify || !pushKey || !pushManager || Notification.permission === 'denied');
    alertsBtn.textContent = pushRegistered ? 'Test alerts' : 'Enable alerts';
  } else {
    alertsBtn.classList.toggle('hidden', !canNotify || Notification.permission !== 'default');
  }
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
    notice('All cameras are hidden. Open Hidden to show them again.');
  }
  renderHiddenMenu();
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

async function pollCloudPrinters() {
  try {
    const response = await fetch('/api/printers');
    const list = await response.json().catch(() => null);
    if (!response.ok || !Array.isArray(list)) throw new Error(list?.error || 'Reload and sign in to view your printers.');
    if (JSON.stringify(list) !== JSON.stringify(printers) || !grid.querySelector('.tile')) {
      for (const tile of tiles.values()) tile.stop();
      tiles.clear();
      printers = list;
      if (printers.length) render();
      else notice('Add a printer in Setup on the printer PC.');
    }
  } catch (e) {
    for (const tile of tiles.values()) tile.stop();
    tiles.clear();
    printers = [];
    notice(e.message);
  }
  setTimeout(pollCloudPrinters, 8000);
}

async function start() {
  try {
    const me = await fetch('/api/me');
    const info = await me.json();
    if (!me.ok) { notice(info.error || 'Access denied'); return; }
    monitorOnly = !!info.monitor_only;
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
      pollCloudPrinters();
      return;
    }
    maxUploadMb = info.max_upload_mb || maxUploadMb;
    document.getElementById('setup-link').classList.toggle('hidden', !info.admin);
    const list = await (await fetch('/api/printers')).json();
    if (!list.length) {
      notice(info.admin ? 'No printers yet.' : 'Add printers from Setup on the printer PC.', info.admin);
      return;
    }
    printers = list;
    render();
    updateAlertsButton();
    pollEvents();
  } catch (e) {
    notice('Cannot reach the dashboard server.');
  }
}
start();
