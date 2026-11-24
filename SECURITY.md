# Security

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/Xanjip32/OpenPrintDeck/security/advisories/new)
with steps to reproduce. You should get an answer within a week.

## Threat model

OpenPrintDeck can move printer motors and heaters (by starting prints) and shows camera
images of a room. Treat access to it like physical access to the printers.

**Trusted:** the PC running OpenPrintDeck and its logged-in Windows user; your Tailscale
account and the devices signed in to it (narrowed by `allowed_users` and Tailscale ACLs).

**Not trusted:** everything else on the local network, the internet, websites open in
a user's browser, and the printers' network traffic.

## Controls

| Risk | Control |
|------|---------|
| Anyone on the LAN opens the dashboard or Mainsail | Every listener binds to `127.0.0.1`. No firewall ports are opened. Remote access exists only through `tailscale serve`. |
| Printer discovery | **Find printers** in setup asks each address of this PC's private /24 networks for Moonraker's `/server/info` (ports 80 and 7125), only when clicked. Bambu announcements are received on UDP port 2021 while setup searches, or while a saved Bambu printer still lacks its serial number. That socket only receives: it never answers. The printer address is taken from the packet's sender, and names are length-limited and inserted with `textContent`. |
| Unauthenticated access through Tailscale | The dashboard requires the `Tailscale-User-Login` header that `tailscale serve` sets (and strips from clients). Optional `allowed_users` allow-list. |
| Malicious website uses this PC's browser (DNS rebinding) | The dashboard and the Mainsail forwards accept only the `Host` names `127.0.0.1`, `localhost` and `*.ts.net`. A website that points its own name at this PC is refused, even if it sets the Tailscale header. |
| Printer sends the dashboard elsewhere (redirect / SSRF) | Camera and Moonraker requests follow redirects only on the same printer, so the API key and requests never go to another computer. |
| Swapped Bambu upload | The FTPS data connection must present the same pinned printer certificate as the login connection. |
| Setup page reachable remotely | The admin listener is never published, and it refuses any `Host` header other than `127.0.0.1`/`localhost` (blocks DNS rebinding). |
| Printers changed remotely | The Devices page may add, edit and remove printers over Tailscale only for accounts listed in *Allowed Tailscale accounts* (an empty list allows nobody remotely). Access codes and API keys are never sent to the browser; the same CSRF checks as uploads apply, and every change is in `audit.log`. |
| Malicious website sends uploads (CSRF) | POST requests need the `X-Dashboard: 1` header (forces a CORS preflight that is never granted), a matching `Origin`, and `Sec-Fetch-Site: same-origin`. |
| Script injection (XSS) | Printer names are inserted with `textContent` only. CSP `default-src 'self'` with no inline scripts or styles, `frame-ancestors 'none'`, `nosniff`. |
| Camera images embedded by other sites | `Cross-Origin-Resource-Policy: same-origin`. |
| Leaked secrets | Bambu access codes and Moonraker API keys are encrypted with Windows DPAPI in `config.json` and never returned to the browser. `config.json`, logs and pins are git-ignored. |
| Fake printer on the LAN (Bambu) | Bambu uses self-signed certificates, so the fingerprint is pinned on first use (`pins.json`). A changed certificate is refused. |
| Path traversal in file names | File names are reduced to a safe base name; only `.gcode`, `.gco`, `.g` and `.3mf` are accepted. |
| Resource exhaustion | Upload size limit, one upload per printer at a time, connection cap, 60 s socket timeout, camera frame size limit. |
| Starting a print on a busy printer | Klipper status is checked first; the print is not started while printing or paused. |
| Fake Tailscale installer | Downloaded over HTTPS from `pkgs.tailscale.com` only (redirects elsewhere are refused). The MSI runs only if Windows Authenticode reports a valid signature from `O=Tailscale Inc.`. Install is only reachable from the local setup page. |
| Fake sign-in link | Only `https://` links on `tailscale.com` are shown as sign-in or approval links. |
| Notifications used to call arbitrary URLs (SSRF) | Only https ntfy links, `discord.com` webhooks and the Telegram API are accepted. |
| Notification secrets leaked | ntfy links/tokens, Discord webhooks and Telegram tokens are DPAPI-encrypted and never returned to the browser. |
| Camera pictures leaving the network | Attaching pictures to notifications is off by default and labelled on the setup page. |
| Notification spam / mentions | At most 20 messages a minute; Discord mentions (`@everyone`) are disabled. |
| Viewers acting on printers | `read_only_users` (Tailscale) and the `viewer` role (password login) may watch only; every upload/control route checks this on the server, not just in the UI. |
| Brute-forcing a dashboard password | PBKDF2-SHA256 with 310 000 iterations and a per-user salt, minimum 10 characters, constant-time compare; 5 attempts per 15 minutes per source and account and 20 per account; failures are audited. Sessions are random 256-bit tokens in HttpOnly, SameSite=Strict cookies (Secure on `*.ts.net`), kept in memory only. |
| Accidental cancel / start | Cancel and start need `{"confirm": "<printer id>"}` (HTTP 428 otherwise) and a confirmation in the dashboard; start is refused while the printer prints or is paused; control is rate limited (30 a minute per user). |
| Arbitrary G-code / settings changes | No endpoint accepts G-code or temperature/settings changes. Marlin serial sends only a fixed allowlist of read commands. Bambu settings (LAN, cloud, Developer Mode) are never touched. |
| Unwanted control of Bambu printers | Bambu printers are read-only by default (`mode: read_only` sends no commands); control needs `mode: control` and the per-printer `allow_*` switches. |
| Overwriting printer files | An upload whose name already exists on the printer or in Files returns 409 until the user confirms overwrite. |
| SSRF through new adapters | OctoPrint / PrusaLink / Moonraker requests and camera links stay on the configured printer host; e-mail uses verified TLS (plain SMTP only to localhost). |
| Secrets on Linux/macOS | `<field>_env` reads a secret from an environment variable instead of `config.json`. |
| No accountability | `audit.log` records the Tailscale user, printer, file and result of every upload, config change and publish. |

## Known limits

- Mainsail pages are published as-is to your tailnet. Anyone who may open the dashboard
  through Tailscale may also open Mainsail. Restrict users with Tailscale ACLs if needed.
- By default Moonraker trusts every private network address, so anyone on the LAN (and anyone
  who may open Mainsail through the dashboard) controls the printer. `tools/secure_printers.py`
  trusts only the printer itself and requires a Mainsail login; OpenPrintDeck then uses the API key.
- G-code files are not inspected. A user who may upload can send a file containing any G-code.
- Password sessions are lost on restart (by design) and are not bound to an IP address.
- PrusaLink Digest authentication uses MD5 (the printer's protocol); keep it on a trusted LAN.
- Bambu status is checked before an upload with start; if the printer gives no status, the
  printer itself refuses a new job while busy.
- Notification text (printer name, file name) is sent to the notification service you choose.
  Anyone who knows an ntfy.sh topic name can read it; use the generated private topic.
- Processes running as the same Windows user can read the DPAPI-protected secrets.

## Good practice

- Keep Tailscale, Python and printer firmware up to date.
- Use `allowed_users` or Tailscale ACLs when your tailnet is shared with other people.
- Rotate the Bambu access code (toggle LAN mode) if it was ever shared.
- Never expose OpenPrintDeck with Tailscale Funnel or port forwarding.
