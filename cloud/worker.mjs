import { buildPushPayload } from '@block65/webcrypto-web-push';
import assets from './assets.mjs';

const idPattern = /^[a-z0-9][a-z0-9-]{0,31}$/;
const PHOTO_MS = 30_000;   // a camera picture is shown for up to 30 s while the next one is on its way
const DEMAND_MS = 15_000;
const MAX_VIEWERS = 20;
const isWebSocket = request => request.headers.get('Upgrade')?.toLowerCase() === 'websocket';
const headers = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
};
const json = (value, status = 200) => Response.json(value, { status, headers });

async function connectorAuthorized(request, key) {
  const token = request.headers.get('Authorization')?.match(/^Bearer ([A-Za-z0-9_-]{43,128})$/)?.[1];
  if (!token || !key || key.length < 43) return false;
  const encode = new TextEncoder();
  const digest = async value => new Uint8Array(await crypto.subtle.digest('SHA-256', encode.encode(value)));
  const [a, b] = await Promise.all([digest(token), digest(key)]);
  return a.reduce((difference, byte, i) => difference | (byte ^ b[i]), 0) === 0;
}

// ---------------------------------------------------------------- Google sign-in
// The Worker signs the owner in with Google itself (OpenID Connect, code flow with PKCE),
// then keeps them signed in with an HMAC-signed, HttpOnly cookie. No Cloudflare Access needed.
const SESSION_COOKIE = '__Host-os3dpms_session';
const LOGIN_COOKIE = '__Host-os3dpms_login';
const SESSION_DAYS = 30;
const GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth';
const GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token';
const GOOGLE_ISSUERS = ['https://accounts.google.com', 'accounts.google.com'];

const b64url = bytes => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const unb64url = text => Uint8Array.from(atob(text.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
const randomText = (size = 32) => b64url(crypto.getRandomValues(new Uint8Array(size)));
const encoder = new TextEncoder();
const googleReady = env => !!(env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET && env.OWNER_EMAIL);

// The cookie-signing key is derived from the Google client secret, so rotating that secret signs everyone out.
async function signingKey(env) {
  const material = await crypto.subtle.digest('SHA-256', encoder.encode('os3dpms-session-v1:' + env.GOOGLE_CLIENT_SECRET));
  return crypto.subtle.importKey('raw', material, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}

async function seal(env, value) {
  const body = b64url(encoder.encode(JSON.stringify(value)));
  const mac = await crypto.subtle.sign('HMAC', await signingKey(env), encoder.encode(body));
  return body + '.' + b64url(new Uint8Array(mac));
}

async function unseal(env, token) {
  const [body, mac, extra] = (token || '').split('.');
  if (!body || !mac || extra !== undefined || body.length > 2048) return null;
  try {
    const ok = await crypto.subtle.verify('HMAC', await signingKey(env), unb64url(mac), encoder.encode(body));
    if (!ok) return null;
    const value = JSON.parse(new TextDecoder().decode(unb64url(body)));
    return value && typeof value.exp === 'number' && value.exp > Date.now() ? value : null;
  } catch {
    return null;
  }
}

function readCookie(request, name) {
  for (const part of (request.headers.get('Cookie') || '').split(';')) {
    const [key, ...rest] = part.trim().split('=');
    if (key === name) return rest.join('=');
  }
  return '';
}

const cookie = (name, value, maxAge) =>
  `${name}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;

const escapeHtml = text => String(text).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);

function page(title, message, button, status = 200, extraHeaders = {}) {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Printers</title>
<link rel="icon" href="/icon-192.png"><link rel="stylesheet" href="/auth/style.css"></head>
<body><main><img src="/icon-192.png" alt="" width="64" height="64"><h1>${escapeHtml(title)}</h1>
<p>${escapeHtml(message)}</p>${button ? `<a class="btn" href="/auth/login">${escapeHtml(button)}</a>` : ''}
</main></body></html>`;
  return new Response(html, { status, headers: { ...headers, 'Content-Type': 'text/html; charset=utf-8', ...extraHeaders } });
}

const LOGIN_CSS = `body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f6f8fa;color:#1f2328;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px;box-sizing:border-box}
main{background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:32px;max-width:380px;width:100%;text-align:center}
h1{font-size:20px;margin:12px 0 8px}p{color:#59636e;margin:0 0 24px}
.btn{display:inline-block;background:#1f6feb;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-weight:600}
@media (prefers-color-scheme:dark){body{background:#0f1115;color:#e6e8ec}main{background:#161a21;border-color:#2b313b}p{color:#9aa4b2}}`;

async function startLogin(request, env) {
  const url = new URL(request.url);
  const state = randomText(), nonce = randomText(), verifier = randomText(48);
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(verifier))));
  const target = new URL(GOOGLE_AUTH);
  target.search = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    redirect_uri: url.origin + '/auth/callback',
    response_type: 'code',
    scope: 'openid email',
    state, nonce,
    code_challenge: challenge,
    code_challenge_method: 'S256',
    login_hint: env.OWNER_EMAIL,
    prompt: 'select_account',
  });
  const login = await seal(env, { state, nonce, verifier, exp: Date.now() + 10 * 60_000 });
  return new Response(null, { status: 302, headers: { ...headers, Location: target.href, 'Set-Cookie': cookie(LOGIN_COOKIE, login, 600) } });
}

async function finishLogin(request, env) {
  const url = new URL(request.url);
  const clearLogin = { 'Set-Cookie': cookie(LOGIN_COOKIE, '', 0) };
  const fail = message => page('Sign-in failed', message, 'Try again', 403, clearLogin);
  const login = await unseal(env, readCookie(request, LOGIN_COOKIE));
  const code = url.searchParams.get('code');
  if (!login || !code || url.searchParams.get('state') !== login.state) {
    return fail('The sign-in link expired or was opened in a different browser.');
  }
  let claims;
  try {
    // The ID token comes straight from Google's token endpoint over TLS, authenticated with our client
    // secret, so its claims can be trusted without checking the JWT signature (OpenID Connect Core 3.1.3.7).
    const response = await fetch(GOOGLE_TOKEN, {
      method: 'POST',
      redirect: 'manual',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        code,
        client_id: env.GOOGLE_CLIENT_ID,
        client_secret: env.GOOGLE_CLIENT_SECRET,
        redirect_uri: url.origin + '/auth/callback',
        grant_type: 'authorization_code',
        code_verifier: login.verifier,
      }),
    });
    const tokens = await response.json();
    // Google's error code only (for example invalid_client); never the code or tokens.
    if (!tokens.id_token) {
      console.log('google token error', response.status, tokens.error, tokens.error_description);
      const reason = String(tokens.error || response.status).replace(/[^a-z0-9_]/gi, '').slice(0, 40);
      return fail(`Google refused the sign-in (${reason}). Check the client ID and secret saved in the Worker.`);
    }
    claims = JSON.parse(new TextDecoder().decode(unb64url(String(tokens.id_token).split('.')[1])));
  } catch (error) {
    console.log('google sign-in error', error?.name, error?.message);
    const reason = String(error?.name || 'error').replace(/[^a-z0-9_]/gi, '').slice(0, 40);
    return fail(`Google did not confirm the sign-in (${reason}). Try again.`);
  }
  const email = String(claims.email || '').toLowerCase();
  const valid = GOOGLE_ISSUERS.includes(claims.iss) && claims.aud === env.GOOGLE_CLIENT_ID
    && claims.nonce === login.nonce && claims.exp * 1000 > Date.now() && claims.email_verified === true;
  if (!valid) {
    console.log('google claims rejected', { iss: claims.iss, audOk: claims.aud === env.GOOGLE_CLIENT_ID,
      nonceOk: claims.nonce === login.nonce, expOk: claims.exp * 1000 > Date.now(), verified: claims.email_verified });
  }
  if (!valid) {
    const failed = [!GOOGLE_ISSUERS.includes(claims.iss) && 'issuer', claims.aud !== env.GOOGLE_CLIENT_ID && 'client',
      claims.nonce !== login.nonce && 'nonce', !(claims.exp * 1000 > Date.now()) && 'expired',
      claims.email_verified !== true && 'unverified email'].filter(Boolean).join(', ');
    return fail(`Google did not confirm the sign-in (${failed}). Try again.`);
  }
  if (email !== env.OWNER_EMAIL.toLowerCase()) {
    return fail('This Google account is not allowed on this dashboard. Sign in with the owner account.');
  }
  const session = await seal(env, { email, exp: Date.now() + SESSION_DAYS * 86_400_000 });
  const out = new Headers({ ...headers, Location: '/' });
  out.append('Set-Cookie', cookie(SESSION_COOKIE, session, SESSION_DAYS * 86_400));
  out.append('Set-Cookie', clearLogin['Set-Cookie']);
  return new Response(null, { status: 302, headers: out });
}

// Returns the signed-in owner's email, or '' when the viewer is not the owner.
// Google sign-in is used when it is configured; Cloudflare Access still works as an alternative.
async function viewerEmail(request, ctx, env) {
  if (!env.OWNER_EMAIL) return '';
  const owner = env.OWNER_EMAIL.toLowerCase();
  if (googleReady(env)) {
    const session = await unseal(env, readCookie(request, SESSION_COOKIE));
    if (session?.email === owner) return owner;
  }
  if (env.ACCESS_AUD && ctx.access?.aud === env.ACCESS_AUD) {
    const identity = await ctx.access.getIdentity();
    if (identity?.email?.toLowerCase() === owner) return owner;
  }
  return '';
}

// Files the sign-in page needs before the viewer is signed in. None of them reveal printer data.
const PUBLIC_ASSETS = new Set(['/icon-192.png', '/icon-512.png', '/apple-touch-icon.png', '/manifest.webmanifest']);

// Secrets pasted from some terminals carry a byte-order mark or a trailing newline; drop them.
const SECRET_NAMES = ['OWNER_EMAIL', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'ACCESS_AUD', 'CONNECTOR_KEY',
  'VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY'];
function cleanEnv(env) {
  const clean = Object.create(env);
  for (const name of SECRET_NAMES) {
    if (typeof env[name] === 'string') clean[name] = env[name].replace(/^﻿/, '').trim();
  }
  return clean;
}

export default {
  async fetch(request, rawEnv, ctx) {
    const env = cleanEnv(rawEnv);
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;
    const connector = (path === '/connector' && (method === 'POST' || method === 'PUT'))
      || (path === '/connector/live' && method === 'GET');
    if (connector) {
      if (!await connectorAuthorized(request, env.CONNECTOR_KEY)) return json({ error: 'Invalid connection key.' }, 403);
      if (path === '/connector/live' && !isWebSocket(request)) return json({ error: 'Expected a WebSocket.' }, 426);
      return env.RELAY.getByName('host').fetch(request);
    }
    const asset = method === 'GET' && Object.hasOwn(assets, path) ? assets[path] : null;
    // The page may open its live camera link (a WebSocket) to this same site.
    const csp = path === '/' ? { 'Content-Security-Policy': `${headers['Content-Security-Policy']}; connect-src 'self' wss://${url.host}` } : {};
    const sendAsset = () => new Response(Uint8Array.from(atob(asset.base64), c => c.charCodeAt(0)),
      { headers: { ...headers, 'Content-Type': asset.type, ...csp } });
    if (method === 'GET' && path.startsWith('/auth/')) {
      if (path === '/auth/style.css') return new Response(LOGIN_CSS, { headers: { ...headers, 'Content-Type': 'text/css' } });
      if (!googleReady(env)) return page('Sign-in is not set up', 'Add the Google client ID and secret to the Worker.', '', 503);
      if (path === '/auth/login') return startLogin(request, env);
      if (path === '/auth/callback') return finishLogin(request, env);
      if (path === '/auth/logout') {
        return page('Signed out', 'You are signed out of the printer dashboard on this device.', 'Sign in again', 200,
          { 'Set-Cookie': cookie(SESSION_COOKIE, '', 0) });
      }
    }
    if (asset && PUBLIC_ASSETS.has(path)) return sendAsset();
    const viewer = await viewerEmail(request, ctx, env);
    if (!viewer) {
      if (method === 'GET' && path === '/' && googleReady(env)) {
        return page('OS_3DPMS Printers', 'Sign in with the owner Google account to see your printers.', 'Sign in with Google');
      }
      return json({ error: 'Sign in with the dashboard owner account.' }, 403);
    }
    if (asset) return sendAsset();
    if (method === 'GET' && path === '/api/me') {
      return json({ monitor_only: true, user: viewer, push_key: env.VAPID_PUBLIC_KEY || '',
        logout: googleReady(env) ? '/auth/logout' : '' });
    }
    if (method === 'GET' && (path === '/api/printers' || /^\/(snap|api\/status)\/[a-z0-9-]+$/.test(path))) {
      return env.RELAY.getByName('host').fetch(request);
    }
    if (method === 'GET' && path === '/live') {
      if (!isWebSocket(request)) return json({ error: 'Expected a WebSocket.' }, 426);
      // Browsers send cookies with WebSockets from any site: only this dashboard's own page may connect.
      if (request.headers.get('Origin') !== url.origin) return json({ error: 'Cross-site request blocked.' }, 403);
      return env.RELAY.getByName('host').fetch(request);
    }
    if (method === 'POST' && (path === '/api/push' || path === '/api/push/test')) {
      if (request.headers.get('Origin') !== url.origin || request.headers.get('X-Dashboard') !== '1') {
        return json({ error: 'Cross-site request blocked.' }, 403);
      }
      return env.RELAY.getByName('host').fetch(request);
    }
    return json({ error: 'Route not found.' }, 404);
  },
};

function pushSubscription(value) {
  const { endpoint, keys } = value || {};
  if (typeof endpoint !== 'string' || endpoint.length > 2048) return null;
  let url;
  try { url = new URL(endpoint); } catch { return null; }
  const host = url.hostname;
  const allowed = host === 'fcm.googleapis.com' || host.endsWith('.push.apple.com')
    || host.endsWith('.push.services.mozilla.com') || host.endsWith('.notify.windows.com');
  if (!allowed || url.protocol !== 'https:' || url.username || url.password || url.port
      || !/^[A-Za-z0-9_-]{87}$/.test(keys?.p256dh) || !/^[A-Za-z0-9_-]{22}$/.test(keys?.auth)) return null;
  return { endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth }, expirationTime: null };
}

export class PrinterRelay {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = cleanEnv(env);
    this.printers = new Map();
    this.photos = new Map();          // printer id -> { packet, bytes, time }
    this.demand = new Map();          // printer id -> last picture request over plain HTTP
    this.cameraErrors = new Map();    // printer id -> why the printer PC has no picture
    this.viewers = new Map();         // browser WebSocket -> { watch, busy, sent }
    this.uplink = null;               // the printer PC's live WebSocket
    this.lastWanted = '';
    this.lastSeen = 0;
    this.session = '';
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    const now = Date.now();
    // The Worker forwards these two addresses only for WebSocket upgrades it has already checked.
    if (path === '/connector/live') return this.openUplink();
    if (path === '/live') return this.openViewer();
    if (path === '/connector' && request.method === 'POST') {
      const body = await request.text();
      if (body.length > 64_000) return json({ error: 'Report too large.' }, 413);
      let report;
      try { report = JSON.parse(body); } catch { return json({ error: 'Invalid report.' }, 400); }
      if (!report || !/^[a-f0-9]{32}$/.test(report.session) || !Array.isArray(report.printers)) {
        return json({ error: 'Invalid report.' }, 400);
      }
      this.printers = new Map(report.printers.slice(0, 32).filter(p => p && idPattern.test(p.id)).map(p => [p.id, p]));
      if (this.session !== report.session) this.photos.clear();
      this.session = report.session;
      this.lastSeen = now;
      for (const [id, time] of this.demand) {
        if (now - time > DEMAND_MS || !this.printers.has(id)) this.demand.delete(id);
      }
      for (const [id, photo] of this.photos) {
        if (now - photo.time > PHOTO_MS || !this.printers.has(id)) this.photos.delete(id);
      }
      this.cameraErrors = new Map([...this.printers.values()]
        .filter(p => typeof p.camera_error === 'string' && p.camera_error)
        .map(p => [p.id, p.camera_error.slice(0, 300)]));
      for (const [ws, viewer] of this.viewers) {
        for (const id of viewer.watch) this.explain(ws, id, now);
      }
      this.sendWanted();
      if (Array.isArray(report.events) && report.events.length) {
        await this.notify(report.session, report.events.slice(-20));
      }
      return json({ cameras: this.wantedCameras(now) });
    }
    if (path === '/connector' && request.method === 'PUT') {
      const id = url.searchParams.get('camera');
      if (!this.printers.has(id) || !this.wantedCameras(now).includes(id)) {
        return json({ error: 'Camera is not requested.' }, 409);
      }
      const bytes = new Uint8Array(await request.arrayBuffer());
      if (bytes.length > 1_000_000 || bytes[0] !== 255 || bytes[1] !== 216) {
        return json({ error: 'Expected a JPEG preview up to 1 MB.' }, 400);
      }
      const packet = new Uint8Array(1 + id.length + bytes.length);
      packet[0] = id.length;
      packet.set(new TextEncoder().encode(id), 1);
      packet.set(bytes, 1 + id.length);
      this.storePhoto(id, packet, now);
      return new Response(null, { status: 204, headers });
    }
    if (path.startsWith('/api/push')) return this.subscribe(request, path.endsWith('/test'));
    const online = now - this.lastSeen < 25_000;
    if (path === '/api/printers') {
      if (!online) return json({ error: 'Waiting for the printer PC to connect.' }, 503);
      return json([...this.printers.values()].map(({ status, camera_error, ...printer }) => printer));
    }
    const id = path.split('/').at(-1);
    if (path.startsWith('/api/status/')) {
      return json(online ? this.printers.get(id)?.status || {} : { state: 'offline' });
    }
    if (path.startsWith('/snap/') && online && this.printers.has(id)) {
      const known = this.demand.has(id);
      this.demand.set(id, now);
      if (!known) this.sendWanted();
      const photo = this.photos.get(id);
      if (photo && now - photo.time < PHOTO_MS) {
        return new Response(photo.bytes, { headers: { ...headers, 'Content-Type': 'image/jpeg' } });
      }
      return json({ error: this.cameraErrors.get(id) || 'Waiting for a camera preview.' }, 503);
    }
    return json({ error: online ? 'Waiting for a camera preview.' : 'The printer PC is not connected.' }, 503);
  }

  // ---------------------------------------------------------------- live camera link
  // The printer PC keeps one WebSocket open while someone watches, and sends each new picture as
  // [id length][id][JPEG]. Each browser gets the newest picture of the cameras it shows, one at a
  // time: the next goes out when the browser says it has drawn the last ("ack"), so a slow phone
  // skips pictures instead of falling behind.

  wantedCameras(now) {
    const ids = new Set();
    for (const [id, time] of this.demand) if (now - time <= DEMAND_MS) ids.add(id);
    for (const viewer of this.viewers.values()) for (const id of viewer.watch) ids.add(id);
    return [...ids].filter(id => this.printers.has(id)).slice(0, 8);
  }

  sendWanted() {
    if (!this.uplink) return;
    const text = JSON.stringify({ cameras: this.wantedCameras(Date.now()) });
    if (text === this.lastWanted) return;
    this.lastWanted = text;
    try { this.uplink.send(text); } catch { this.uplink = null; }
  }

  openUplink() {
    const [client, server] = Object.values(new WebSocketPair());
    server.accept();
    const old = this.uplink;
    this.uplink = server;
    if (old) try { old.close(1000, 'Replaced by a newer connection.'); } catch { /* already closed */ }
    server.addEventListener('message', event => this.onUplink(server, event.data));
    const gone = () => {
      if (this.uplink === server) this.uplink = null;
      try { server.close(1000); } catch { /* already closed */ }
    };
    server.addEventListener('close', gone);
    server.addEventListener('error', gone);
    this.lastWanted = '';
    this.sendWanted();
    return new Response(null, { status: 101, webSocket: client });
  }

  onUplink(ws, data) {
    if (ws !== this.uplink || typeof data === 'string') return;   // text from the PC is only a keep-alive
    const packet = new Uint8Array(data);
    const length = packet[0];
    const id = String.fromCharCode(...packet.subarray(1, 1 + length));
    const photo = packet.subarray(1 + length);
    const now = Date.now();
    if (!idPattern.test(id) || !this.wantedCameras(now).includes(id)) return;
    if (photo.length > 1_000_000 || photo[0] !== 255 || photo[1] !== 216) return;
    this.storePhoto(id, packet, now);
  }

  storePhoto(id, packet, now) {
    this.photoCount = (this.photoCount || 0) + 1;
    this.photos.set(id, { packet, bytes: packet.subarray(1 + packet[0]), time: now, number: this.photoCount });
    for (const [ws, viewer] of this.viewers) if (viewer.watch.has(id)) this.offer(ws, viewer, id);
  }

  offer(ws, viewer, id) {
    const photo = this.photos.get(id);
    if (!photo || viewer.sent.get(id) === photo.number) return;
    const busy = viewer.busy.get(id);
    if (busy && Date.now() - busy < 10_000) return;   // still drawing the last picture
    try {
      ws.send(photo.packet);
      viewer.busy.set(id, Date.now());
      viewer.sent.set(id, photo.number);
    } catch {
      this.dropViewer(ws);
    }
  }

  // Tell a viewer why a camera has no picture, when the printer PC knows.
  explain(ws, id, now) {
    const error = this.cameraErrors.get(id);
    const photo = this.photos.get(id);
    if (!error || (photo && now - photo.time < PHOTO_MS)) return;
    try { ws.send(JSON.stringify({ camera: id, error })); } catch { this.dropViewer(ws); }
  }

  openViewer() {
    if (this.viewers.size >= MAX_VIEWERS) return json({ error: 'Too many dashboards are open.' }, 429);
    const [client, server] = Object.values(new WebSocketPair());
    server.accept();
    const viewer = { watch: new Set(), busy: new Map(), sent: new Map() };
    this.viewers.set(server, viewer);
    server.addEventListener('message', event => this.onViewer(server, viewer, event.data));
    const gone = () => this.dropViewer(server);
    server.addEventListener('close', gone);
    server.addEventListener('error', gone);
    return new Response(null, { status: 101, webSocket: client });
  }

  onViewer(ws, viewer, data) {
    if (typeof data !== 'string' || data.length > 4096) return;
    let message;
    try { message = JSON.parse(data); } catch { return; }
    const now = Date.now();
    if (Array.isArray(message?.watch)) {
      const watch = new Set(message.watch.filter(id => typeof id === 'string' && idPattern.test(id)).slice(0, 32));
      for (const id of watch) {
        if (viewer.watch.has(id)) continue;
        viewer.busy.delete(id);   // a new card: send it the newest picture at once
        viewer.sent.delete(id);
      }
      viewer.watch = watch;
      for (const id of watch) {
        this.offer(ws, viewer, id);
        this.explain(ws, id, now);
      }
      this.sendWanted();
    } else if (typeof message?.ack === 'string') {
      viewer.busy.delete(message.ack);
      if (viewer.watch.has(message.ack)) this.offer(ws, viewer, message.ack);
    }
  }

  dropViewer(ws) {
    if (this.viewers.delete(ws)) this.sendWanted();
    try { ws.close(1000); } catch { /* already closed */ }
  }

  async subscribe(request, test) {
    let value;
    try { value = JSON.parse((await request.text()).slice(0, 4096)); }
    catch { return json({ error: 'Invalid push subscription.' }, 400); }
    const subscription = pushSubscription(value);
    if (!subscription) return json({ error: 'This notification provider is not supported.' }, 400);
    if (!this.env.VAPID_PRIVATE_KEY || !this.env.VAPID_PUBLIC_KEY) return json({ error: 'Alerts are not configured.' }, 503);
    const id = this.pushPrefix + subscription.endpoint;
    const devices = await this.ctx.storage.list({ prefix: this.pushPrefix, limit: 10 });
    if (test) {
      if (!devices.has(id)) return json({ error: 'Enable alerts first.' }, 400);
      const delivered = await this.send(subscription, { title: 'OS_3DPMS', body: 'Printer alerts are working.' });
      return json(delivered ? { ok: true } : { error: 'Could not send the test alert.' }, delivered ? 200 : 502);
    }
    if (devices.size >= 10 && !devices.has(id)) return json({ error: 'This dashboard already has ten alert devices.' }, 409);
    await this.ctx.storage.put(id, subscription);
    return json({ ok: true });
  }

  get pushPrefix() {
    return 'push:' + this.env.OWNER_EMAIL?.toLowerCase() + ':';
  }

  async notify(session, events) {
    const last = await this.ctx.storage.get('last-event');
    const fresh = events.filter(e => Number.isSafeInteger(e?.id) && typeof e.text === 'string'
      && (session !== last?.session || e.id > last.id));
    if (!fresh.length) return;
    await this.ctx.storage.put('last-event', { session, id: Math.max(...fresh.map(e => e.id)) });
    const devices = await this.ctx.storage.list({ prefix: this.pushPrefix, limit: 10 });
    const event = fresh.at(-1);
    const payload = {
      title: fresh.length === 1 ? String(event.name || 'OS_3DPMS').slice(0, 64) : `${fresh.length} printer updates`,
      body: fresh.slice(-4).map(item => item.text).join('\n').slice(0, 450),
    };
    this.ctx.waitUntil(Promise.allSettled([...devices.values()].map(device => this.send(device, payload))));
  }

  async send(subscription, message) {
    try {
      const payload = await buildPushPayload({ data: JSON.stringify(message), options: { ttl: 300 } }, subscription, {
        subject: 'mailto:' + this.env.OWNER_EMAIL,
        publicKey: this.env.VAPID_PUBLIC_KEY,
        privateKey: this.env.VAPID_PRIVATE_KEY,
      });
      const response = await fetch(subscription.endpoint, { ...payload, redirect: 'manual' });
      if (response.status === 404 || response.status === 410) {
        await this.ctx.storage.delete(this.pushPrefix + subscription.endpoint);
      }
      return response.ok;
    } catch {
      return false;
    }
  }
}
