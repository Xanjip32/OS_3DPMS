import { buildPushPayload } from '@block65/webcrypto-web-push';
import assets from './assets.mjs';

const idPattern = /^[a-z0-9][a-z0-9-]{0,31}$/;
const headers = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
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

async function viewerAuthorized(ctx, env) {
  if (!env.ACCESS_AUD || ctx.access?.aud !== env.ACCESS_AUD || !env.OWNER_EMAIL) return false;
  const identity = await ctx.access.getIdentity();
  return identity?.email?.toLowerCase() === env.OWNER_EMAIL.toLowerCase();
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;
    if (path === '/connector' && (method === 'POST' || method === 'PUT')) {
      if (!await connectorAuthorized(request, env.CONNECTOR_KEY)) return json({ error: 'Invalid connection key.' }, 403);
      return env.RELAY.getByName('host').fetch(request);
    }
    if (!await viewerAuthorized(ctx, env)) return json({ error: 'Sign in with the dashboard owner account.' }, 403);
    if (method === 'GET' && Object.hasOwn(assets, path)) {
      const asset = assets[path];
      return new Response(Uint8Array.from(atob(asset.base64), c => c.charCodeAt(0)),
        { headers: { ...headers, 'Content-Type': asset.type } });
    }
    if (method === 'GET' && path === '/api/me') {
      return json({ monitor_only: true, push_key: env.VAPID_PUBLIC_KEY || '' });
    }
    if (method === 'GET' && (path === '/api/printers' || /^\/(snap|api\/status)\/[a-z0-9-]+$/.test(path))) {
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
    this.env = env;
    this.printers = new Map();
    this.photos = new Map();
    this.demand = new Map();
    this.lastSeen = 0;
    this.session = '';
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    const now = Date.now();
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
        if (now - time > 15_000 || !this.printers.has(id)) this.demand.delete(id);
      }
      for (const [id, photo] of this.photos) {
        if (now - photo.time > 10_000 || !this.printers.has(id)) this.photos.delete(id);
      }
      if (Array.isArray(report.events) && report.events.length) {
        await this.notify(report.session, report.events.slice(-20));
      }
      return json({ cameras: [...this.demand.keys()].slice(0, 8) });
    }
    if (path === '/connector' && request.method === 'PUT') {
      const id = url.searchParams.get('camera');
      if (!this.printers.has(id) || now - (this.demand.get(id) || 0) > 15_000) {
        return json({ error: 'Camera is not requested.' }, 409);
      }
      const bytes = new Uint8Array(await request.arrayBuffer());
      if (bytes.length > 1_000_000 || bytes[0] !== 255 || bytes[1] !== 216) {
        return json({ error: 'Expected a JPEG preview up to 1 MB.' }, 400);
      }
      this.photos.set(id, { bytes, time: now });
      return new Response(null, { status: 204, headers });
    }
    if (path.startsWith('/api/push')) return this.subscribe(request, path.endsWith('/test'));
    const online = now - this.lastSeen < 25_000;
    if (path === '/api/printers') {
      if (!online) return json({ error: 'Waiting for the printer PC to connect.' }, 503);
      return json([...this.printers.values()].map(({ status, ...printer }) => printer));
    }
    const id = path.split('/').at(-1);
    if (path.startsWith('/api/status/')) {
      return json(online ? this.printers.get(id)?.status || {} : { state: 'offline' });
    }
    if (path.startsWith('/snap/') && online && this.printers.has(id)) {
      this.demand.set(id, now);
      const photo = this.photos.get(id);
      if (photo && now - photo.time < 10_000) {
        return new Response(photo.bytes, { headers: { ...headers, 'Content-Type': 'image/jpeg' } });
      }
    }
    return json({ error: 'Waiting for a camera preview.' }, 503);
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
      const response = await fetch(subscription.endpoint, { ...payload, redirect: 'error' });
      if (response.status === 404 || response.status === 410) {
        await this.ctx.storage.delete(this.pushPrefix + subscription.endpoint);
      }
      return response.ok;
    } catch {
      return false;
    }
  }
}
