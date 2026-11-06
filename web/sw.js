'use strict';
// Service worker for the installed app. It caches nothing: cameras and status must be live.
// It only shows a helpful page when the dashboard cannot be reached (usually: Tailscale is off).

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Printers</title></head>
<body style="margin:0;background:#0f1115;color:#e6e8ec;font:16px/1.5 system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:24px">
<div><h1 style="font-size:20px">Can't reach your printers</h1>
<p>Open the <b>Tailscale</b> app and make sure it is connected, then pull down or reopen this app.</p>
<p style="color:#8b919c">The printer PC must also be on.</p></div></body></html>`;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', event => {
  if (event.request.mode !== 'navigate') return;   // camera streams and API calls go straight to the network
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE_PAGE, {
    status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' },
  })));
});

// Tapping a notification opens (or focuses) the dashboard.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: 'window' }).then(list => {
    for (const c of list) if ('focus' in c) return c.focus();
    return self.clients.openWindow('/');
  }));
});
