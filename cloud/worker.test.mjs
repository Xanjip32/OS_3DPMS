import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import test from 'node:test';
import worker, { PrinterRelay } from './worker.mjs';

const base = 'https://os3dpms.owner.workers.dev';
const owner = 'owner@example.com';
const key = 'a'.repeat(43);
const session = 'a'.repeat(32);
const viewer = { access: { aud: 'dashboard', getIdentity: async () => ({ email: owner }) } };

function fixture(vars = {}) {
  const values = new Map();
  const pending = [];
  const storage = {
    get: async k => values.get(k),
    put: async (k, value) => values.set(k, value),
    delete: async k => values.delete(k),
    list: async ({ prefix }) => new Map([...values].filter(([k]) => k.startsWith(prefix))),
  };
  const env = { CONNECTOR_KEY: key, ACCESS_AUD: 'dashboard', OWNER_EMAIL: owner, ...vars };
  const relay = new PrinterRelay({ storage, waitUntil: promise => pending.push(promise) }, env);
  env.RELAY = { getByName: () => relay };
  const fetch = (path, options, ctx = viewer) => worker.fetch(new Request(base + path, options), env, ctx);
  const report = (events = []) => fetch('/connector', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + key },
    body: JSON.stringify({ session, events, printers: [{ id: 'voron', name: 'Voron', status: { state: 'printing' } }] }),
  }, {});
  return { fetch, report, pending };
}

test('private viewer can request a photo, but only the paired host can publish one', async () => {
  const { fetch, report } = fixture();
  assert.equal((await fetch('/', {}, {})).status, 403);
  assert.equal((await fetch('/api/printers', {}, { access: { ...viewer.access, aud: 'other' } })).status, 403);
  assert.equal((await fetch('/api/printers', {}, { access: { ...viewer.access, getIdentity: async () => ({ email: 'other@example.com' }) } })).status, 403);
  assert.equal((await (await fetch('/api/me')).json()).user, owner);
  assert.equal((await fetch('/setup')).status, 404);
  assert.equal((await fetch('/api/upload/voron', { method: 'POST' })).status, 404);
  assert.equal((await fetch('/connector', { method: 'POST', body: '{}' }, {})).status, 403);
  assert.equal((await fetch('/api/printers')).status, 503);
  assert.deepEqual(await (await report()).json(), { cameras: [] });
  assert.equal((await fetch('/snap/voron')).status, 503);
  assert.deepEqual(await (await report()).json(), { cameras: ['voron'] });
  const photo = Uint8Array.of(255, 216, 255, 217);
  const uploaded = await fetch('/connector?camera=voron', {
    method: 'PUT', headers: { Authorization: 'Bearer ' + key }, body: photo,
  }, {});
  assert.equal(uploaded.status, 204);
  assert.deepEqual(new Uint8Array(await (await fetch('/snap/voron')).arrayBuffer()), photo);
  assert.deepEqual(await (await fetch('/api/status/voron')).json(), { state: 'printing' });
});

function pushKeys() {
  const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
  const { x, y, d } = privateKey.export({ format: 'jwk' });
  const pub = Buffer.concat([Buffer.of(4), Buffer.from(x, 'base64url'), Buffer.from(y, 'base64url')]);
  return { private: d, public: pub.toString('base64url') };
}

test('an opted-in phone receives a test and one encrypted alert for a print event', async t => {
  const keys = pushKeys();
  const { fetch, report, pending } = fixture({ VAPID_PRIVATE_KEY: keys.private, VAPID_PUBLIC_KEY: keys.public });
  const push = t.mock.method(globalThis, 'fetch', async (_url, options) => {
    assert.equal(new Headers(options.headers).get('Content-Encoding'), 'aes128gcm');
    return new Response(null, { status: 201 });
  });
  const subscription = { endpoint: 'https://web.push.apple.com/subscription', keys: {
    p256dh: pushKeys().public, auth: Buffer.alloc(16, 1).toString('base64url'),
  } };
  const subscribe = (path, origin, sub = subscription) => fetch(path, {
    method: 'POST', headers: { Origin: origin, 'X-Dashboard': '1' }, body: JSON.stringify(sub),
  });
  assert.equal((await subscribe('/api/push', 'https://untrusted.example')).status, 403);
  assert.equal((await subscribe('/api/push', base, { ...subscription, endpoint: 'https://127.0.0.1/test' })).status, 400);
  assert.equal((await subscribe('/api/push', base)).status, 200);
  assert.equal((await subscribe('/api/push/test', base)).status, 200);
  const event = { id: 1, name: 'Voron', text: 'Print finished' };
  await report([event]);
  await report([event]);
  await Promise.all(pending);
  assert.equal(push.mock.callCount(), 2);
});
