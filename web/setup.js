'use strict';

const $ = id => document.getElementById(id);
let tsRunning = false;
let printers = [];

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

// ---------------------------------------------------------------- step 1 + 3 + 4
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
  const tsKey = [ts.installed, ts.state, ts.user, ts.dns_name].join('|');
  if (tsKey !== lastTsKey) { lastTsKey = tsKey; body.replaceChildren(); renderTailscale(ts, body); }
  renderPublish(st);
}

let lastTsKey = '';

function renderTailscale(ts, body) {
  if (!ts.installed) {
    setState('ts-state', 'not installed', 'todo');
    body.append(
      el('p', { text: 'Install Tailscale on this PC, then click "Check again".' }),
      el('div', { class: 'actions' },
        link(ts.download_url || 'https://tailscale.com/download', 'Download Tailscale', true),
        el('button', { text: 'Check again', onclick: () => { lastTsKey = ''; refresh(); } })));
  } else if (tsRunning) {
    setState('ts-state', 'connected', 'ok');
    body.append(el('p', { text: `Signed in as ${ts.user || 'your account'}. This PC is ${ts.dns_name}.` }));
  } else if (ts.state === 'Error') {
    setState('ts-state', 'error', 'todo');
    body.append(el('p', { class: 'msg bad', text: ts.detail || 'Tailscale is not responding. Start the Tailscale app.' }),
      el('div', { class: 'actions' }, el('button', { text: 'Check again', onclick: refresh })));
  } else {
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
            msg.textContent = 'Finish signing in in the new tab. This page updates by itself.';
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
    body.append(el('p', { text: 'Sign in with the same account you use on your other devices.' }),
      el('div', { class: 'actions' }, btn, msg));
  }
}

function renderPublish(st) {
  // Step 3: publish status
  const ports = $('ports');
  ports.replaceChildren();
  const entries = Object.entries(st.published);
  for (const [port, ok] of entries) {
    const what = port === '443' ? 'Dashboard' : `Mainsail on port ${port}`;
    ports.append(el('li', { text: `${ok ? '✓' : '✗'} ${what}` }));
  }
  const allPublished = entries.length > 0 && entries.every(([, ok]) => ok);
  setState('pub-state', !tsRunning ? 'needs step 1' : allPublished ? 'published' : 'not published', allPublished ? 'ok' : 'todo');
  $('publish').disabled = !tsRunning;

  // Step 4
  const done = $('done-body');
  if (tsRunning && allPublished && st.public_url) {
    const copy = el('button', { text: 'Copy link', onclick: () => navigator.clipboard.writeText(st.public_url) });
    done.replaceChildren(
      el('p', { text: 'On any device signed in to the same Tailscale account, open:' }),
      el('div', { class: 'actions' }, el('span', { class: 'url', text: st.public_url }), copy, link(st.public_url, 'Open', true)),
      el('p', { text: 'Bookmark it. Phones work too: install the Tailscale app and sign in.' }));
  } else {
    done.replaceChildren(el('p', { text: 'Finish steps 1–3 first.' }));
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

// ---------------------------------------------------------------- step 2: printers
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

function renderPrinters() {
  const list = $('printer-list');
  list.replaceChildren();
  if (!printers.length) list.append(el('p', { text: 'No printers yet. Add one below.' }));
  printers.forEach((p, i) => {
    const bind = (key, input, kind) => {
      const read = () => (kind === 'bool' ? input.checked : kind === 'int' ? Number(input.value) || 0 : input.value.trim());
      if (kind === 'bool') input.checked = !!p[key]; else input.value = p[key] ?? '';
      input.addEventListener('input', () => { p[key] = read(); });
      input.addEventListener('change', () => { p[key] = read(); });
      return input;
    };
    const rotate = el('select', {}, ...[0, 90, 180, 270].map(d => el('option', { value: String(d), text: `${d}°` })));
    const row = el('div', { class: 'printer-row' },
      el('div', { class: 'kind' }, p.type === 'bambu' ? 'Bambu Lab' : 'Klipper / Mainsail',
        el('button', { text: 'Remove', onclick: () => { printers.splice(i, 1); renderPrinters(); } })),
      field('Name', bind('name', el('input', { placeholder: 'Voron 500' }))),
      field('IP address', bind('host', el('input', { placeholder: '192.168.1.21' }))),
      field('Camera rotation', bind('rotate', rotate, 'int')));

    if (p.type === 'klipper') {
      row.append(field('Mainsail port (tailnet)', bind('ui_port', el('input', { type: 'number', min: '1024', max: '65535' }), 'int')));
      row.append(field('Moonraker API key (optional)', bind('api_key', el('input', {
        type: 'password', autocomplete: 'off', placeholder: p.has_api_key ? 'saved (leave empty to keep)' : 'only if Moonraker needs it',
      }))));
    } else {
      row.append(field('LAN access code', bind('access_code', el('input', {
        type: 'password', autocomplete: 'off', placeholder: p.has_access_code ? 'saved (leave empty to keep)' : 'from printer screen',
      }))));
      row.append(field('Serial number (to start prints)', bind('serial', el('input', { placeholder: 'optional' }))));
      row.append(el('label', { class: 'check' }, bind('use_ams', el('input', { type: 'checkbox' }), 'bool'), 'Print from AMS'));
    }
    row.append(el('label', { class: 'check' }, bind('flip_horizontal', el('input', { type: 'checkbox' }), 'bool'), 'Mirror camera'));
    list.append(row);
  });
  setState('pr-state', printers.length ? `${printers.length} configured` : 'none yet', printers.length ? 'ok' : 'todo');
}

function addPrinter(type) {
  const p = { type, name: '', host: '', rotate: 0 };
  if (type === 'klipper') p.ui_port = nextUiPort();
  printers.push(p);
  renderPrinters();
}
$('add-klipper').addEventListener('click', () => addPrinter('klipper'));
$('add-bambu').addEventListener('click', () => addPrinter('bambu'));

$('save').addEventListener('click', async () => {
  const msg = $('save-msg');
  const used = new Set();
  const payload = printers.map(p => {
    const item = Object.assign({}, p);
    delete item.has_access_code;
    delete item.has_api_key;
    if (!item.id) item.id = slug(item.name);
    let id = item.id, n = 2;
    while (used.has(id)) id = `${item.id}-${n++}`;
    item.id = id;
    used.add(id);
    return item;
  });
  const allowed = $('allowed').value.split(',').map(s => s.trim()).filter(Boolean);
  msg.className = 'msg';
  msg.textContent = 'Saving…';
  try {
    await api('/api/setup/config', { printers: payload, allowed_users: allowed });
    msg.className = 'msg ok';
    msg.textContent = 'Saved. If you added a Klipper printer, click Publish again.';
    await loadConfig();
    refresh();
  } catch (e) {
    msg.className = 'msg bad';
    msg.textContent = e.message;
  }
});

// ---------------------------------------------------------------- step 4: notifications
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
    await loadConfig();
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
