'use strict';

const FRAME_MS = 200;               // grid refresh on the printer PC: about 5 pictures a second per camera
const REMOTE_FRAME_MS = 1000;       // over Tailscale (often a phone): 1 a second, so links are not overrun
const STALE_MS = 20000;             // keep showing the last picture this long before explaining the gap
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
  return !!p.ui_port;
}

// Show a JPEG blob in an <img>; resolves once it is drawn (or could not be).
function drawInto(image, blob, onDrawn) {
  return new Promise(resolve => {
    const url = URL.createObjectURL(blob);
    image.onload = () => {
      if (image.dataset.url) URL.revokeObjectURL(image.dataset.url);
      image.dataset.url = url;
      onDrawn?.();
      resolve();
    };
    image.onerror = () => { URL.revokeObjectURL(url); resolve(); };   // damaged picture: wait for the next
    image.src = url;
  });
}

function clearImage(image) {
  image.removeAttribute('src');
  if (image.dataset.url) URL.revokeObjectURL(image.dataset.url);
  delete image.dataset.url;
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
  mimg.src = `/cam/${encodeURIComponent(p.id)}?t=${Date.now()}`;
  modal.classList.add('show');
}
function closeModal() {
  clearTimeout(modalTimer);
  modal.classList.remove('show');
  clearImage(mimg);
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
  const startBox = q('.start');
  let pending = null;
  let busy = false;
  let stopped = false;

  t.stop = () => {
    stopped = true;
    cameraObserver.unobserve(t);
    clearImage(img);
  };
  t.update = st => {
    chipEl.textContent = st && st.state ? stateText(st) : '';
    chipEl.className = 'chip ' + ((st && st.state) || '');
    if (!busy) fileLine.textContent = st && ACTIVE.has(st.state) ? (st.file || '') : '';
  };
  t.pick = () => input.click();
  t.isBusy = () => busy;

  q('.hide').addEventListener('click', e => {
    e.stopPropagation();
    if (busy) { toast(`${p.name}: wait until the upload finishes`, true); return; }
    hidePrinter(p.id, true);
  });

  img.style.transform = camTransform(p);

  // Camera: pictures while the card is on screen, one at a time with fetch (not img.src) so the
  // server's reason is shown when there is none. A missed picture never blanks the card: the last
  // one stays until no new picture came for STALE_MS.
  let lastFrame = 0;
  let lastError = '';
  const onScreen = () => !document.hidden && !modal.classList.contains('show')
    && !(seenByObserver.has(t) && !visibleCameras.has(t));
  const drawn = () => {
    lastFrame = Date.now();
    lastError = '';
    t.classList.remove('nocam');
  };
  const show = blob => (stopped ? Promise.resolve() : drawInto(img, blob, drawn));
  function explain(message) {
    lastError = String(message || 'Camera offline').replace(/\.+$/, '');
    if (Date.now() - lastFrame < STALE_MS) return;
    nocam.textContent = `${lastError}. Retrying…`;
    t.classList.add('nocam');
  }
  async function nextFrame() {
    if (stopped) return;
    if (!onScreen()) {
      setTimeout(nextFrame, 1000);
      return;
    }
    try {
      const r = await fetch(`/snap/${id}?t=${Date.now()}`, { cache: 'no-store' });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.error || `Camera error (HTTP ${r.status})`);
      }
      await show(await r.blob());
      setTimeout(nextFrame, info.admin ? FRAME_MS : REMOTE_FRAME_MS);
    } catch (e) {
      if (stopped) return;
      explain(e.message);
      setTimeout(nextFrame, 3000);
    }
  }
  nocam.textContent = 'Loading camera…';
  t.classList.add('nocam');
  cameraObserver.observe(t);
  nextFrame();

  const openUi = () => window.open(mainsailUrl(p), '_blank', 'noopener');
  q('.view').addEventListener('click', () => openModal(p));
  q('.live').addEventListener('click', () => openModal(p));
  if (canMainsail(p)) q('.ui').addEventListener('click', openUi);

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
  startBox.addEventListener('change', () => q('.warn').classList.toggle('hidden', !startBox.checked));

  function ask(file) {
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

// Devices page: Add, Edit and Remove. On the printer PC, and remotely for accounts listed in Setup.
function deviceActions(p) {
  const box = actionButtons(p, false);
  if (!info.can_edit) return box;
  box.append(
    el('button', { text: 'Edit', onclick: () => openPrinterDialog(p.id) }),
    el('button', { text: 'Remove', onclick: () => removePrinter(p) }));
  return box;
}

// The saved printer list without secrets (the page never gets access codes or API keys).
async function devicesList() {
  const r = await fetch('/api/devices');
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data.printers;
}

async function saveDevices(list) {
  const r = await fetch('/api/devices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Dashboard': '1' },
    // Leaving a code out keeps the saved one; the has_ markers only describe it.
    body: JSON.stringify({ printers: list.map(({ has_access_code, has_api_key, ...x }) => x) }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
  printers = await (await fetch('/api/printers')).json();
  render();
}

async function removePrinter(p) {
  if (!confirm(`Remove ${p.name} from OS_3DPMS?\n\nThe printer itself is not changed. You can add it again.`)) return;
  try {
    await saveDevices((await devicesList()).filter(x => x.id !== p.id));
    toast(`Removed ${p.name}`);
  } catch (e) {
    toast(`${p.name} was not removed: ${e.message}`, true);
  }
}

const printerDialog = $('printer-dialog');
let editing = null;   // saved settings of the printer being edited; null while adding one
const slug = text => (text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32) || 'printer';
const showBambuFields = () => {
  printerDialog.querySelector('.bambu-only').hidden = (editing ? editing.type : $('printer-type').value) !== 'bambu';
};
$('printer-type').addEventListener('change', showBambuFields);
$('printer-cancel').addEventListener('click', () => printerDialog.close());
document.querySelectorAll('.add-printer').forEach(b => b.addEventListener('click', () => openPrinterDialog(null)));

async function openPrinterDialog(id) {
  let list;
  try { list = await devicesList(); } catch (e) { toast(e.message, true); return; }
  editing = id ? list.find(x => x.id === id) : null;
  if (id && !editing) { toast('That printer was removed meanwhile.', true); return; }
  $('printer-title').textContent = editing ? `Edit ${editing.name}` : 'Add printer';
  $('printer-type-field').hidden = !!editing;
  $('printer-name').value = editing?.name || '';
  $('printer-host').value = editing?.host || '';
  $('printer-code').value = '';
  $('printer-code').placeholder = editing?.has_access_code ? 'saved (leave empty to keep it)' : '';
  $('printer-serial').value = editing?.serial || '';
  $('printer-rotate').value = String(editing?.rotate || 0);
  $('printer-msg').textContent = '';
  showBambuFields();
  printerDialog.showModal();
}

$('printer-form').addEventListener('submit', async e => {
  e.preventDefault();
  const msg = $('printer-msg');
  const button = $('printer-save');
  button.disabled = true;
  msg.className = 'msg';
  msg.textContent = 'Saving…';
  try {
    const list = await devicesList();
    const type = editing ? editing.type : $('printer-type').value;
    const fields = { name: $('printer-name').value.trim(), host: $('printer-host').value.trim(),
      rotate: Number($('printer-rotate').value) };
    if (type === 'bambu') {
      fields.serial = $('printer-serial').value.trim();
      if ($('printer-code').value.trim()) fields.access_code = $('printer-code').value.trim();
    }
    if (editing) {
      const i = list.findIndex(x => x.id === editing.id);
      if (i < 0) throw new Error('That printer was removed meanwhile.');
      list[i] = { ...list[i], ...fields };
    } else {
      const ids = new Set(list.map(x => x.id));
      let id = slug(fields.name);
      for (let n = 2; ids.has(id); n++) id = `${slug(fields.name).slice(0, 29)}-${n}`;
      const item = { id, type, ...fields };
      if (type === 'klipper') {   // its Mainsail page gets the next free port, as in Setup
        const ports = new Set(list.map(x => Number(x.ui_port)).filter(Boolean));
        item.ui_port = 8101;
        while (ports.has(item.ui_port)) item.ui_port++;
      }
      list.push(item);
    }
    await saveDevices(list);
    printerDialog.close();
    toast(editing ? `Saved ${fields.name}` : `Added ${fields.name}. Its camera and status appear in a few seconds.`);
  } catch (err) {
    msg.className = 'msg bad';
    msg.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

function actionButtons(p, withSend) {
  const box = el('div', { class: 'row-actions' });
  box.append(el('button', { text: 'Camera', onclick: () => openModal(p) }));
  if (canMainsail(p)) box.append(el('button', { text: 'Mainsail', onclick: () => window.open(mainsailUrl(p), '_blank', 'noopener') }));
  if (withSend && tiles.has(p.id)) {
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
    { label: '', class: 'actions-col', cell: deviceActions },
  ], rows));
}

// ---------------------------------------------------------------- files page (library on the printer PC)
const filesBox = $('files');
const filesAdd = $('files-add');
const filesInput = $('files-input');
let fileNames = new Set();

async function renderFiles() {
  if (!('user' in info)) return;   // start() renders again once it knows where this page runs
  let data;
  try {
    const r = await fetch('/api/files');
    data = await r.json();
    if (!r.ok) throw new Error(data.error);
  } catch (e) {
    notice(filesBox, e.message || 'Cannot load the files.');
    return;
  }
  fileNames = new Set(data.files.map(f => f.name));
  $('files-free').textContent = `${(data.free / 1073741824).toFixed(1)} GB free on the printer PC`;
  if (!data.files.length) {
    notice(filesBox, 'No files yet. Upload a .gcode or .3mf file to keep it here and send it to any printer.');
    return;
  }
  filesBox.replaceChildren(table([
    { label: 'Name', cell: f => el('b', { text: f.name }) },
    { label: 'Size', class: 'muted', cell: f => `${(f.size / 1048576).toFixed(1)} MB` },
    { label: 'Added', class: 'muted', cell: f => new Date(f.modified * 1000).toLocaleString() },
    { label: '', class: 'actions-col', cell: fileActions },
  ], data.files));
}

// POST to a Files or Queue action; returns [message, bad] for toast().
async function apiPost(url) {
  try {
    const r = await fetch(url, { method: 'POST', headers: { 'X-Dashboard': '1' } });
    const data = await r.json().catch(() => ({}));
    return r.ok && data.ok ? [data.message] : [data.error || `Failed (HTTP ${r.status})`, true];
  } catch (e) {
    return ['Lost connection to the dashboard', true];
  }
}

function fileActions(f) {
  const name = encodeURIComponent(f.name);
  const pick = el('select', { 'aria-label': `Printer for ${f.name}` },
    ...printers.map(p => el('option', { value: p.id, text: p.name })));
  const send = async start => {
    const p = printers.find(x => x.id === pick.value);
    if (!p) return;
    if (start && !confirm(`Start printing ${f.name} on ${p.name}?\n\nCheck that the bed is clear first.`)) return;
    toast(`Sending ${f.name} to ${p.name}…`);
    toast(...await apiPost(`/api/files/send?name=${name}&printer=${encodeURIComponent(p.id)}&print=${start ? 1 : 0}`));
  };
  return el('div', { class: 'row-actions' }, pick,
    el('button', { class: 'primary', text: 'Send', onclick: () => send(false) }),
    el('button', { text: 'Send and print', onclick: () => send(true) }),
    el('button', { text: 'Add to queue', onclick: async () => {
      toast(...await apiPost(`/api/queue/add?name=${name}&printer=${encodeURIComponent(pick.value)}`));
    } }),
    el('button', { text: 'Delete', onclick: async () => {
      if (!confirm(`Delete ${f.name} from the printer PC?`)) return;
      toast(...await apiPost(`/api/files/delete?name=${name}`));
      renderFiles();
    } }));
}

// ---------------------------------------------------------------- queue page (kept on the printer PC)
const queueBox = $('queue');

async function renderQueue() {
  if (!('user' in info)) return;   // start() renders again once it knows where this page runs
  let data;
  try {
    const r = await fetch('/api/queue');
    data = await r.json();
    if (!r.ok) throw new Error(data.error);
  } catch (e) {
    notice(queueBox, e.message || 'Cannot load the queue.');
    return;
  }
  const n = data.jobs.length;
  $('queue-count').textContent = n ? `${n} job${n === 1 ? '' : 's'}` : '';
  if (!n) {
    notice(queueBox, 'The queue is empty. Click + Add job, or Add to queue on the Files page.');
    return;
  }
  queueBox.replaceChildren(table([
    { label: '#', cell: j => String(data.jobs.indexOf(j) + 1) },
    { label: 'File', cell: j => el('b', { text: j.file }) },
    { label: 'Printer', cell: j => j.printer_name },
    { label: 'Status', cell: j => el('span', { class: 'chip ' + (j.ready ? 'complete' : ''),
      text: j.ready ? 'Ready' : `Waiting: ${j.waiting}` }) },
    { label: 'Added', class: 'muted', cell: j => el('span', { text: new Date(j.added * 1000).toLocaleString(),
      title: `Added by ${j.by}` }) },
    { label: '', class: 'actions-col', cell: queueActions },
  ], data.jobs));
}

function queueActions(j) {
  const box = el('div', { class: 'row-actions' });
  if (j.ready) {
    box.append(el('button', { class: 'primary', text: 'Bed is clear, start', onclick: async () => {
      if (!confirm(`Start printing ${j.file} on ${j.printer_name}?\n\nOnly if the bed is clear.`)) return;
      toast(`Sending ${j.file} to ${j.printer_name}…`);
      toast(...await apiPost(`/api/queue/start?id=${encodeURIComponent(j.id)}`));
      renderQueue();
    } }));
  }
  box.append(el('button', { text: 'Remove', onclick: async () => {
    toast(...await apiPost(`/api/queue/remove?id=${encodeURIComponent(j.id)}`));
    renderQueue();
  } }));
  return box;
}

// Printers finish and free up on their own: keep the open Queue page current.
setInterval(() => {
  if (!document.hidden && location.hash.replace(/^#\/?/, '') === 'queue') renderQueue();
}, 5000);

filesAdd.addEventListener('click', () => filesInput.click());
// Upload a file into Files; resolves to the name it was saved under.
function uploadToLibrary(file, onProgress) {
  return new Promise((resolve, reject) => {
    if (!UPLOAD_RE.test(file.name)) { reject(new Error('Only .gcode or .3mf files')); return; }
    if (file.size > maxUploadMb * 1048576) { reject(new Error(`File is larger than ${maxUploadMb} MB`)); return; }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/files?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader('X-Dashboard', '1');
    xhr.upload.addEventListener('progress', e => {
      if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
    });
    xhr.addEventListener('load', () => {
      let r = {};
      try { r = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status === 200 && r.ok) resolve(r.name);
      else reject(new Error(r.error || `Upload failed (HTTP ${xhr.status})`));
    });
    xhr.addEventListener('error', () => reject(new Error('Lost connection to the dashboard')));
    xhr.send(file);
  });
}

filesInput.addEventListener('change', async () => {
  const file = filesInput.files[0];
  filesInput.value = '';
  if (!file) return;
  if (fileNames.has(file.name) && !confirm(`${file.name} is already in the library. Replace it?`)) return;
  filesAdd.disabled = true;
  try {
    toast(`Saved ${await uploadToLibrary(file, pct => { filesAdd.textContent = `Uploading ${pct}%`; })}`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    filesAdd.disabled = false;
    filesAdd.textContent = '+ Upload file';
    renderFiles();
  }
});

// Queue page: Add job (a file from Files, or a new upload, for one printer).
const jobDialog = $('job-dialog');
$('job-cancel').addEventListener('click', () => jobDialog.close());
$('queue-add').addEventListener('click', async () => {
  $('job-msg').textContent = '';
  $('job-upload').value = '';
  $('job-printer').replaceChildren(...printers.map(p => el('option', { value: p.id, text: p.name })));
  let files = [];
  try { files = (await (await fetch('/api/files')).json()).files; } catch (e) { /* upload one instead */ }
  $('job-file').replaceChildren(...(files.length ? files.map(f => el('option', { value: f.name, text: f.name }))
    : [el('option', { value: '', text: 'No files yet: upload one below' })]));
  jobDialog.showModal();
});
$('job-form').addEventListener('submit', async e => {
  e.preventDefault();
  const msg = $('job-msg');
  const button = $('job-save');
  button.disabled = true;
  msg.className = 'msg';
  try {
    const file = $('job-upload').files[0];
    const name = file ? await uploadToLibrary(file, pct => { msg.textContent = `Uploading ${file.name}… ${pct}%`; })
      : $('job-file').value;
    if (!name) throw new Error('Choose a file, or upload one.');
    const [text, bad] = await apiPost(`/api/queue/add?name=${encodeURIComponent(name)}&printer=${encodeURIComponent($('job-printer').value)}`);
    if (bad) throw new Error(text);
    jobDialog.close();
    toast(text);
    renderQueue();
  } catch (err) {
    msg.className = 'msg bad';
    msg.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

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
  $('account-text').textContent = info.admin ? 'You are on the printer PC itself.'
    : `Signed in through Tailscale as ${info.user || 'a tailnet user'}.`;
  $('setup-text').textContent = info.admin
    ? 'Add or edit printers, phone notifications and remote access.'
    : 'Printers, phone notifications and remote access are set up on the printer PC (Setup page).';
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
  if (page === 'files') renderFiles();
  if (page === 'queue') renderQueue();
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
  // Stays visible when notifications are blocked, so a click can explain how to allow them.
  alertsBtn.classList.toggle('hidden', !canNotify || Notification.permission === 'granted');
  renderSettings();
}

alertsBtn.addEventListener('click', async () => {
  if (alertsBtn.disabled) return;
  alertsBtn.disabled = true;
  const label = alertsBtn.textContent;
  alertsBtn.textContent = 'Setting up…';
  try {
    let permission = Notification.permission;
    if (permission === 'default') {
      toast('Choose "Allow" when the browser asks. No question? Click the bell or lock icon at the left of the address bar.');
      permission = await Notification.requestPermission();
    }
    if (permission === 'denied') {
      throw new Error('Notifications are blocked for this site. Click the lock icon at the left of the address bar, '
        + 'set Notifications to Allow, reload the page, then click Enable alerts again.');
    }
    if (permission !== 'granted') throw new Error('Notifications were not allowed. Click Enable alerts again and choose Allow.');
    toast('Alerts are on: this page pops up a message when a print finishes or fails while it is open. '
      + 'For alerts on your phone when the app is closed, set up Phone notifications (ntfy) in Setup.');
  } catch (e) {
    toast(e.message || 'Could not set up alerts.', true);
  } finally {
    alertsBtn.disabled = false;
    alertsBtn.textContent = label;
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
function showUser() {
  const who = info.admin ? 'This PC (admin)' : (info.user || '');
  $('who').textContent = who;
  $('who').classList.toggle('hidden', !who);
  document.querySelectorAll('.admin-only').forEach(a => a.classList.toggle('hidden', !info.admin));
  document.querySelectorAll('.can-edit').forEach(a => a.classList.toggle('hidden', !info.can_edit));
}

async function start() {
  route();
  try {
    const me = await fetch('/api/me');
    info = await me.json();
    if (!me.ok) { notice(grid, info.error || 'Access denied'); return; }
    showUser();
    maxUploadMb = info.max_upload_mb || maxUploadMb;
    printers = await (await fetch('/api/printers')).json();
    render();
    route();
    updateAlertsButton();
    pollStatuses();
    pollEvents();
  } catch (e) {
    notice(grid, 'Cannot reach the dashboard server.');
  }
}
start();
