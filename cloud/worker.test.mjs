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

test('owner signs in with Google; other accounts and forged cookies are refused', async t => {
  const google = { GOOGLE_CLIENT_ID: 'client.apps.googleusercontent.com', GOOGLE_CLIENT_SECRET: 'secret', ACCESS_AUD: '' };
  const { fetch } = fixture(google);
  const page = await fetch('/', {}, {});
  assert.equal(page.status, 200);
  assert.match(await page.text(), /Sign in with Google/);
  assert.equal((await fetch('/api/printers', {}, {})).status, 403);
  assert.equal((await fetch('/icon-192.png', {}, {})).status, 200);

  const signIn = async email => {
    const start = await fetch('/auth/login', {}, {});
    assert.equal(start.status, 302);
    const target = new URL(start.headers.get('Location'));
    assert.equal(target.origin, 'https://accounts.google.com');
    assert.equal(target.searchParams.get('code_challenge_method'), 'S256');
    const login = start.headers.get('Set-Cookie').split(';')[0];
    const claims = { iss: 'https://accounts.google.com', aud: google.GOOGLE_CLIENT_ID, email, email_verified: true,
      nonce: target.searchParams.get('nonce'), exp: Date.now() / 1000 + 300 };
    const token = 'x.' + Buffer.from(JSON.stringify(claims)).toString('base64url') + '.x';
    const mock = t.mock.method(globalThis, 'fetch', async (url, options) => {
      assert.equal(url, 'https://oauth2.googleapis.com/token');
      assert.ok(new URLSearchParams(options.body).get('code_verifier'));
      return Response.json({ id_token: token });
    });
    const state = target.searchParams.get('state');
    const wrongState = await fetch('/auth/callback?code=c&state=bad', { headers: { Cookie: login } }, {});
    assert.equal(wrongState.status, 403);
    const done = await fetch(`/auth/callback?code=c&state=${state}`, { headers: { Cookie: login } }, {});
    mock.mock.restore();
    return done;
  };

  const stranger = await signIn('other@example.com');
  assert.equal(stranger.status, 403);
  assert.doesNotMatch(stranger.headers.get('Set-Cookie'), /session=[^;]/);

  const ok = await signIn(owner);
  assert.equal(ok.status, 302);
  const session = ok.headers.getSetCookie().find(c => c.includes('session=')).split(';')[0];
  const me = await fetch('/api/me', { headers: { Cookie: session } }, {});
  assert.equal((await me.json()).user, owner);

  const [name, value] = session.split('=');
  const forged = name + '=' + value.slice(0, -2) + (value.endsWith('AA') ? 'BB' : 'AA');
  assert.equal((await fetch('/api/me', { headers: { Cookie: forged } }, {})).status, 403);
  const fake = Buffer.from(JSON.stringify({ email: owner, exp: Date.now() + 1e9 })).toString('base64url');
  assert.equal((await fetch('/api/me', { headers: { Cookie: `${name}=${fake}.AAAA` } }, {})).status, 403);
  const out = await fetch('/auth/logout', {}, {});
  assert.match(out.headers.get('Set-Cookie'), /Max-Age=0/);
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
