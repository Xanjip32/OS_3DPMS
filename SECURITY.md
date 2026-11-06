# Security

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/Xanjip32/OS_3DPMS/security/advisories/new)
with steps to reproduce. You should get an answer within a week.

## Threat model

OS_3DPMS can move printer motors and heaters (by starting prints) and shows camera
images of a room. Treat access to it like physical access to the printers.

**Trusted:** the PC running OS_3DPMS and its logged-in Windows user; your Tailscale
account and the devices signed in to it (narrowed by `allowed_users` and Tailscale ACLs).

**Not trusted:** everything else on the local network, the internet, websites open in
a user's browser, and the printers' network traffic.

## Controls

| Risk | Control |
|------|---------|
| Anyone on the LAN opens the dashboard or Mainsail | Every listener binds to `127.0.0.1`. No firewall ports are opened. Remote access exists only through `tailscale serve`. |
| Unauthenticated access through Tailscale | The dashboard requires the `Tailscale-User-Login` header that `tailscale serve` sets (and strips from clients). Optional `allowed_users` allow-list. |
| Setup page reachable remotely | The admin listener is never published, and it refuses any `Host` header other than `127.0.0.1`/`localhost` (blocks DNS rebinding). |
| Malicious website sends uploads (CSRF) | POST requests need the `X-Dashboard: 1` header (forces a CORS preflight that is never granted), a matching `Origin`, and `Sec-Fetch-Site: same-origin`. |
| Script injection (XSS) | Printer names are inserted with `textContent` only. CSP `default-src 'self'` with no inline scripts or styles, `frame-ancestors 'none'`, `nosniff`. |
| Camera images embedded by other sites | `Cross-Origin-Resource-Policy: same-origin`. |
| Leaked secrets | Bambu access codes and Moonraker API keys are encrypted with Windows DPAPI in `config.json` and never returned to the browser. `config.json`, logs and pins are git-ignored. |
| Fake printer on the LAN (Bambu) | Bambu uses self-signed certificates, so the fingerprint is pinned on first use (`pins.json`). A changed certificate is refused. |
| Path traversal in file names | File names are reduced to a safe base name; only `.gcode`, `.gco`, `.g` and `.3mf` are accepted. |
| Resource exhaustion | Upload size limit, one upload per printer at a time, connection cap, 60 s socket timeout, camera frame size limit. |
| Starting a print on a busy printer | Klipper status is checked first; the print is not started while printing or paused. |
| No accountability | `audit.log` records the Tailscale user, printer, file and result of every upload, config change and publish. |

## Known limits

- Mainsail pages are published as-is to your tailnet. Anyone who may open the dashboard
  through Tailscale may also open Mainsail. Restrict users with Tailscale ACLs if needed.
- Moonraker sees all requests as coming from the dashboard PC, so it trusts them.
  Moonraker's own login (`[authorization]` with users) still works if you enable it.
- G-code is not inspected. A user who may upload can send any G-code.
- Bambu printers have no status check, so the printer itself decides whether a new print starts.
- Processes running as the same Windows user can read the DPAPI-protected secrets.

## Good practice

- Keep Tailscale, Python and printer firmware up to date.
- Use `allowed_users` or Tailscale ACLs when your tailnet is shared with other people.
- Rotate the Bambu access code (toggle LAN mode) if it was ever shared.
- Never expose OS_3DPMS with Tailscale Funnel or port forwarding.
