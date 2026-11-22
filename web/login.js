'use strict';
// Password sign-in (auth mode "password" or "tailscale+password"). The session cookie is HttpOnly.
document.getElementById('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const err = document.getElementById('login-error');
  err.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'X-Dashboard': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: document.getElementById('user').value, password: document.getElementById('password').value }),
    });
    if (r.ok) { location.href = '/'; return; }
    err.textContent = (await r.json()).error || 'Sign in failed';
  } catch (e) {
    err.textContent = 'OS_3DPMS is not answering';
  }
});
