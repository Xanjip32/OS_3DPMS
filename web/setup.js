'use strict';

const $ = id => document.getElementById(id);
let tsRunning = false;
let printers = [];
const editedPrinters = new WeakSet();

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: { 'X-Dashboard': '1', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

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

function setState(id, text, kind) {
  const s = $(id);
  s.textContent = text;
  s.className = 'state' + (kind ? ' ' + kind : '');
}

function link(href, text, primary) {
  return el('a', { class: 'btn' + (primary ? ' primary' : ''), href, target: '_blank', rel: 'noopener', text });
}

// ---------------------------------------------------------------- remote access
async function refresh() {
  let st;
  try {
    st = await api('/api/setup/status');
  } catch (e) {
    setState('ts-state', 'error', 'todo');
    return;
  }
  const ts = st.tailscale;
  const body = $('ts-body');
  tsRunning = ts.state === 'Running';
  const inst = st.install || {};
  const tsKey = [ts.installed, ts.state, ts.user, ts.detail, inst.state, inst.detail].join('|');
  if (tsKey !== lastTsKey) { lastTsKey = tsKey; body.replaceChildren(); renderTailscale(ts, body, inst); }
  renderPublish(st);
}

let lastTsKey = '';

const INSTALL_BUSY = ['downloading', 'verifying', 'installing', 'starting'];

function renderInstall(ts, body, inst) {
  setState('ts-state', 'not installed', 'todo');
  const again = el('button', { text: 'Check again', onclick: () => { lastTsKey = ''; refresh(); } });
  if (!inst.supported) {
    body.append(
      el('p', { text: 'Install Tailscale on this PC, then click "Check again".' }),
      el('div', { class: 'actions' }, link(ts.download_url || 'https://tailscale.com/download', 'Download Tailscale', true), again));
    return;
  }
  if (INSTALL_BUSY.includes(inst.state)) {
    setState('ts-state', 'installing…', 'todo');
    body.append(el('p', { class: 'msg', text: inst.detail || 'Installing…' }));
    return;
  }
  const msg = el('span', { class: inst.state === 'failed' ? 'msg bad' : 'msg', text: inst.state === 'failed' ? inst.detail : '' });
  const btn = el('button', {
    class: 'primary', text: 'Install Tailscale', onclick: async () => {
      btn.disabled = true;
      msg.className = 'msg';
      msg.textContent = 'Starting…';
      try {
        const r = await api('/api/setup/install-tailscale', {});
        if (!r.ok) msg.textContent = r.error || 'Could not start the install.';
      } catch (e) {
        msg.textContent = e.message;
      }
      lastTsKey = '';
      refresh();
    },
  });
  body.append(
    el('p', { text: 'Install Tailscale on this PC. Windows will ask for permission.' }),
    el('div', { class: 'actions' }, btn, again, msg),
    inst.state === 'failed' ? link(ts.download_url, 'Download from Tailscale') : '');
}

function renderTailscale(ts, body, inst) {
  if (!ts.installed) {
    renderInstall(ts, body, inst);
  } else if (tsRunning) {
    setState('ts-state', 'connected', 'ok');
    body.append(el('p', { text: `Signed in as ${ts.user || 'your account'}.` }));
  } else if (ts.state === 'NeedsLogin') {
    setState('ts-state', 'sign in needed', 'todo');
    const msg = el('span', { class: 'msg' });
    const btn = el('button', {
      class: 'primary', text: 'Log in to Tailscale', onclick: async () => {
        btn.disabled = true;
        msg.textContent = 'Getting sign-in link…';
        try {
          const r = await api('/api/setup/login', {});
          if (r.auth_url) {
            window.open(r.auth_url, '_blank', 'noopener');
            msg.replaceChildren('Finish signing in in the new tab. ',
              link(r.auth_url, 'Open sign-in page', true));
          } else if (r.already) {
            msg.textContent = '';
          } else {
            msg.textContent = r.error || 'No sign-in link.';
          }
        } catch (e) {
          msg.textContent = e.message;
        }
        btn.disabled = false;
      },
    });
    body.append(el('p', { text: 'Use the same Tailscale account on the devices you want to connect.' }),
      el('div', { class: 'actions' }, btn, msg));
  } else {
    const states = {
      Stopped: ['Disconnected', 'Open Tailscale on this PC and choose Connect.'],
      Starting: ['Connecting…', 'Waiting for Tailscale to connect.'],
      NoState: ['Starting…', 'Waiting for Tailscale to start.'],
      NeedsMachineAuth: ['Approval needed', 'Approve this PC in your Tailscale admin console.'],
    };
    const [label, detail] = states[ts.state] || ['Check Tailscale', ts.detail || 'Open Tailscale and check its connection.'];
    setState('ts-state', label, 'todo');
    body.append(el('p', { text: detail }),
      el('div', { class: 'actions' }, el('button', { text: 'Check again', onclick: refresh })));
  }
}

function renderPublish(st) {
  const ports = $('ports');
  $('publish-details').hidden = !tsRunning;
  ports.replaceChildren();
  const entries = Object.entries(st.published);
  for (const [port, ok] of entries) {
    const what = port === '443' ? 'Dashboard' : `Mainsail on port ${port}`;
    ports.append(el('li', { text: `${ok ? '✓' : '✗'} ${what}` }));
  }
  const allPublished = entries.length > 0 && entries.every(([, ok]) => ok);
  setState('pub-state', allPublished ? 'Published' : 'Local only', allPublished ? 'ok' : '');
  $('publish').disabled = !tsRunning;

  const done = $('done-body');
  if (tsRunning && allPublished && st.public_url) {
    if (done.dataset.url === st.public_url) return;
    done.dataset.url = st.public_url;
    const copy = el('button', { text: 'Copy link', onclick: () => navigator.clipboard.writeText(st.public_url) });
    done.replaceChildren(
      el('p', { text: 'Dashboard link' }),
      el('a', { class: 'url', href: st.public_url, text: st.public_url }),
      el('div', { class: 'actions' }, copy));
  } else {
    delete done.dataset.url;
    done.replaceChildren();
  }
}

$('publish').addEventListener('click', async () => {
  const msg = $('pub-msg');
  $('publish').disabled = true;
  msg.className = 'msg';
  msg.textContent = 'Publishing…';
  try {
    const r = await api('/api/setup/publish', {});
    const failed = r.results.find(x => !x.ok);
    if (!failed) {
      msg.className = 'msg ok';
      msg.textContent = 'Published.';
    } else if (failed.action_url) {
      msg.replaceChildren('One-time step: allow HTTPS for your tailnet, then click Publish again. ',
        link(failed.action_url, 'Enable HTTPS in Tailscale', true));
    } else {
      msg.className = 'msg bad';
      msg.textContent = failed.detail || 'Publishing failed.';
    }
  } catch (e) {
    msg.className = 'msg bad';
    msg.textContent = e.message;
  }
  refresh();
});

$('save-allowed').addEventListener('click', async () => {
  const button = $('save-allowed');
  const msg = $('allowed-msg');
  const allowed = $('allowed').value.split(',').map(s => s.trim()).filter(Boolean);
  button.disabled = true;
  msg.className = 'msg';
  msg.textContent = 'Saving…';
  try {
    await api('/api/setup/config', { allowed_users: allowed });
    msg.className = 'msg ok';
    msg.textContent = 'Allowed accounts saved.';
  } catch (e) {
    msg.className = 'msg bad';
    msg.textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

// ---------------------------------------------------------------- printers
function slug(text) {
  return (text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32) || 'printer';
}

function nextUiPort() {
  const used = new Set(printers.map(p => Number(p.ui_port)).filter(Boolean));
  let port = 8101;
  while (used.has(port)) port++;
  return port;
}

function field(label, input) {
  return el('label', { class: 'field' }, label, input);
}

function renderPrinters(checkIds = new Set()) {
  const list = $('printer-list');
  list.replaceChildren();
  if (!printers.length) list.append(el('div', { class: 'printer-empty' },
    el('strong', { text: 'No printers yet' }),
    el('p', { text: 'Choose the type of printer you want to add.' })));
  printers.forEach((p, i) => {
    const bind = (key, input, kind) => {
      const read = () => (kind === 'bool' ? input.checked : kind === 'int' ? Number(input.value) || 0 : input.value.trim());
      if (kind === 'bool') input.checked = !!p[key]; else input.value = p[key] ?? '';
      input.addEventListener('input', () => { p[key] = read(); });
      input.addEventListener('change', () => { p[key] = read(); });
      return input;
    };
    const rotate = el('select', {}, ...[0, 90, 180, 270].map(d => el('option', { value: String(d), text: `${d}°` })));
    const fields = el('div', { class: 'field-grid' },
      field('Printer name', bind('name', el('input', { placeholder: p.type === 'bambu' ? 'Bambu P1S' : 'Voron 500' }))),
      field('IP address or hostname', bind('host', el('input', {
        name: 'host', placeholder: '192.168.0.23', spellcheck: 'false', autocomplete: 'off',
      }))));
    const advanced = el('div', { class: 'field-grid' });
    const row = el('div', { class: 'printer-row' },
      el('div', { class: 'kind' }, el('span', { text: p.type === 'bambu' ? 'Bambu Lab' : 'Klipper / Mainsail' }),
        el('button', { class: 'quiet', text: 'Remove', onclick: () => { printers.splice(i, 1); renderPrinters(); } })),
      fields,
      el('details', { class: 'printer-advanced' }, el('summary', { text: 'Advanced settings' }), advanced));

    if (p.type === 'klipper') {
      advanced.append(field('Printer HTTP port', bind('http_port', el('input', { type: 'number', min: '1', max: '65535', placeholder: '80' }))));
      advanced.append(field('Mainsail port (Tailscale)', bind('ui_port', el('input', { type: 'number', min: '1024', max: '65535' }))));
      advanced.append(field('Moonraker API key', bind('api_key', el('input', {
        type: 'password', autocomplete: 'off', placeholder: p.has_api_key ? 'saved (leave empty to keep)' : 'only if Moonraker needs it',
      }))));
    } else {
      fields.append(field('LAN access code', bind('access_code', el('input', {
        type: 'password', autocomplete: 'off', placeholder: p.has_access_code ? 'saved (leave empty to keep)' : 'from printer screen',
      }))));
      advanced.append(field('Serial number (for remote start)', bind('serial', el('input', { placeholder: 'Optional' }))));
      advanced.append(el('label', { class: 'check' }, bind('use_ams', el('input', { type: 'checkbox' }), 'bool'), 'Print from AMS'));
    }
    advanced.append(field('Camera URL override', bind('webcam_url', el('input', {
      placeholder: 'http://camera-ip/webcam/?action=stream',
    }))));
    advanced.append(field('Camera rotation', bind('rotate', rotate, 'int')));
    advanced.append(el('label', { class: 'check' }, bind('flip_horizontal', el('input', { type: 'checkbox' }), 'bool'), 'Mirror camera'));
    const msg = el('span', { class: 'msg', role: 'status' });
    const preview = el('div', { class: 'camera-preview' });
    const needsSave = () => !p.id || editedPrinters.has(p);
    const check = el('button', { text: 'Check connection', onclick: async () => {
      check.disabled = true;
      preview.replaceChildren();
      msg.className = 'msg';
      msg.textContent = 'Checking printer and camera…';
      try {
        const result = await api(`/api/setup/check-printer/${encodeURIComponent(p.id)}`, {});
        if (needsSave()) return;
        msg.replaceChildren(
          el('span', { class: result.printer_ok ? 'ok' : 'bad', text: result.printer_ok ? 'Printer reachable' : result.printer }),
          el('span', { class: result.camera_ok ? 'ok' : 'bad', text: result.camera_ok ? 'Camera working' : result.camera }));
        if (result.camera_ok) {
          preview.append(
            el('img', { src: `/snap/${encodeURIComponent(p.id)}?t=${Date.now()}`, alt: `Camera preview for ${p.name}` }),
            el('a', { class: 'btn', href: '/', text: 'Open live dashboard' }));
        }
      } catch (e) {
        if (needsSave()) return;
        msg.className = 'msg bad';
        msg.textContent = e.message;
      } finally {
        check.disabled = needsSave();
      }
    } });
    check.disabled = needsSave();
    const markEdited = () => {
      editedPrinters.add(p);
      check.disabled = true;
      preview.replaceChildren();
      msg.className = 'msg';
      msg.textContent = 'Save changes to check the connection.';
    };
    row.addEventListener('input', markEdited);
    row.addEventListener('change', markEdited);
    row.append(el('div', { class: 'actions connection-check' }, check, msg), preview);
    list.append(row);
    if (checkIds.has(p.id)) check.click();
  });
  const saved = printers.filter(p => p.id).length;
  setState('pr-state', saved ? `${saved} saved` : 'none saved', saved ? 'ok' : 'todo');
}

function addPrinter(type) {
  const p = { type, name: '', host: '', rotate: 0 };
  if (type === 'klipper') p.ui_port = nextUiPort();
  printers.push(p);
  renderPrinters();
  $('printer-list').lastElementChild.querySelector('input').focus();
}
$('add-klipper').addEventListener('click', () => addPrinter('klipper'));
$('add-bambu').addEventListener('click', () => addPrinter('bambu'));

$('save').addEventListener('click', async () => {
  const msg = $('save-msg');
  const missingHost = printers.findIndex(p => !p.host);
  if (missingHost !== -1) {
    msg.className = 'msg bad';
    msg.textContent = `Enter the IP address or hostname for printer ${missingHost + 1}, or remove that row.`;
    $('printer-list').children[missingHost].querySelector('input[name="host"]').focus();
    return;
  }
  const used = new Set();
  const checkIds = new Set();
  const payload = printers.map(p => {
    const item = Object.assign({}, p);
    delete item.has_access_code;
    delete item.has_api_key;
    if (!item.id) item.id = slug(item.name);
    let id = item.id, n = 2;
    while (used.has(id)) id = `${item.id}-${n++}`;
    item.id = id;
    used.add(id);
    if (!p.id || editedPrinters.has(p)) checkIds.add(id);
    return item;
  });
  msg.className = 'msg';
  msg.textContent = 'Saving…';
  try {
    await api('/api/setup/config', { printers: payload });
    msg.className = 'msg ok';
    msg.textContent = 'Printers saved.';
    printers = (await api('/api/setup/config')).printers;
    renderPrinters(checkIds);
    refresh();
  } catch (e) {
    msg.className = 'msg bad';
    msg.textContent = e.message;
  }
});

// ---------------------------------------------------------------- notifications
const EVENT_LABELS = {
  complete: 'finishes', error: 'fails / errors', paused: 'pauses',
  cancelled: 'is cancelled', started: 'starts', offline: 'printer goes offline while printing',
};
const SECRET_INPUTS = ['ntfy_url', 'ntfy_token', 'discord_webhook', 'telegram_token'];
const clearFlags = {};

function renderNotifications(n, channels) {
  const box = $('nt-events');
  box.replaceChildren();
  for (const [kind, label] of Object.entries(EVENT_LABELS)) {
    const cb = el('input', { type: 'checkbox', 'data-event': kind });
    cb.checked = !!n.events[kind];
    box.append(el('label', { class: 'check' }, cb, label));
  }
  $('nt-snapshot').checked = !!n.snapshot;
  $('telegram_chat_id').value = n.telegram_chat_id || '';
  for (const key of SECRET_INPUTS) {
    const input = $(key);
    input.value = '';
    clearFlags[key] = false;
    input.placeholder = n['has_' + key] ? 'saved (leave empty to keep)' : '';
  }
  $('ntfy_url').placeholder = n.has_ntfy_url ? 'saved (leave empty to keep)' : 'https://ntfy.sh/your-private-topic';
  setState('nt-state', channels.length ? channels.join(' + ') : 'off', channels.length ? 'ok' : '');
}

document.querySelectorAll('[data-clear]').forEach(btn => btn.addEventListener('click', () => {
  const key = btn.getAttribute('data-clear');
  const related = key === 'ntfy_url' ? ['ntfy_url', 'ntfy_token'] : [key];
  related.forEach(k => { clearFlags[k] = true; $(k).value = ''; $(k).placeholder = 'will be removed when you save'; });
}));

$('ntfy-gen').addEventListener('click', () => {
  const bytes = crypto.getRandomValues(new Uint8Array(15));
  const topic = 'os3dpms-' + Array.from(bytes, b => b.toString(36).padStart(2, '0')).join('').slice(0, 24);
  const url = `https://ntfy.sh/${topic}`;
  $('ntfy_url').value = url;
  $('ntfy-show').textContent = `Subscribe to this in the ntfy app: ${url}`;
});

$('nt-save').addEventListener('click', async () => {
  const msg = $('nt-msg');
  const payload = { events: {}, snapshot: $('nt-snapshot').checked, telegram_chat_id: $('telegram_chat_id').value.trim() };
  document.querySelectorAll('[data-event]').forEach(cb => { payload.events[cb.getAttribute('data-event')] = cb.checked; });
  for (const key of SECRET_INPUTS) {
    payload[key] = $(key).value.trim();
    if (clearFlags[key]) payload['clear_' + key] = true;
  }
  msg.className = 'msg';
  msg.textContent = 'Saving…';
  try {
    const r = await api('/api/setup/notifications', payload);
    msg.className = 'msg ok';
    msg.textContent = r.channels.length ? `Saved. Sending to: ${r.channels.join(', ')}.` : 'Saved. No phone channel set.';
    const cfg = await api('/api/setup/config');
    renderNotifications(cfg.notifications, cfg.channels);
  } catch (e) {
    msg.className = 'msg bad';
    msg.textContent = e.message;
  }
});

$('nt-test').addEventListener('click', async () => {
  const msg = $('nt-msg');
  msg.className = 'msg';
  msg.textContent = 'Sending…';
  try {
    const r = await api('/api/setup/notify-test', {});
    if (!r.channels.length) {
      msg.textContent = 'No phone channel saved yet. Fill one in and click Save first.';
      return;
    }
    const parts = Object.entries(r.results).map(([ch, res]) => `${ch}: ${res === 'ok' ? 'sent' : res}`);
    const ok = Object.values(r.results).every(v => v === 'ok');
    msg.className = 'msg ' + (ok ? 'ok' : 'bad');
    msg.textContent = parts.join(' · ');
  } catch (e) {
    msg.className = 'msg bad';
    msg.textContent = e.message;
  }
});

async function loadConfig() {
  const cfg = await api('/api/setup/config');
  printers = cfg.printers;
  $('allowed').value = cfg.allowed_users.join(', ');
  renderPrinters();
  renderNotifications(cfg.notifications, cfg.channels);
}

loadConfig().catch(e => { $('save-msg').textContent = e.message; });
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 4000);
