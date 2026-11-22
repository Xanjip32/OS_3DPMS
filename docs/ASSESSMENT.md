# OS_3DPMS technical assessment (audit before the universal-adapter work)

Audited commit: `21ad801` ("Confirm Bambu print start and show why the printer refuses it"),
version `1.5.0-preview.2`. Branch for the follow-up work: `feature/universal-adapters`.

## 1. Existing architecture

OS_3DPMS is a single Python process using **only the standard library** (no pip packages).

| Module | Role |
|---|---|
| `run.py` | Entry point, calls `dashboard.web.main()` |
| `dashboard/web.py` (~1100 lines) | `App` (shared state: config, camera hubs, Mainsail forwards, Bambu watchers, print queue, audit log) and the HTTP `Handler` (public port 8080 for Tailscale, admin port 8079 for the local Setup page). Both bind to `127.0.0.1` only. Also file library, queue and upload routes |
| `dashboard/config.py` | Load/validate/save `config.json`; secrets encrypted with Windows DPAPI (`winsecret.py`) |
| `dashboard/klipper.py` | Moonraker HTTP: status (`print_stats` + `display_status` only), webcam list, multipart upload with optional start |
| `dashboard/bambu.py` | Bambu LAN: own minimal MQTT 3.1.1 client over TLS (port 8883) with certificate pinning (`tlspin.py`), `BambuWatcher` thread keeping live status, FTPS (port 990) upload, print start with confirmation |
| `dashboard/cameras.py` | `CameraHub` per printer: MJPEG/JPEG HTTP cameras and the Bambu P1/A1 JPEG feed on port 6000; only runs while someone watches |
| `dashboard/monitor.py` | One thread polls every printer every 5 s, turns state changes into events (started, complete, error, paused, cancelled, offline) |
| `dashboard/notify.py` | ntfy, Discord, Telegram providers, rate-limited |
| `dashboard/discover.py` | Klipper scan of private /24 networks, Bambu UDP announcements |
| `dashboard/forward.py` | Local TCP forward to each printer's Mainsail (published with `tailscale serve`) |
| `dashboard/tailscale.py`, `tsinstall.py`, `qr.py` | Tailscale status, login, publish, verified installer, QR code |
| `web/` | Vanilla JS single-page dashboard (`app.js`) and Setup page (`setup.js`), CSP without inline scripts |
| `tests/` | 115 `unittest` tests incl. a fake Moonraker (`mock_printer.py`); CI runs ruff (`E9,F,B`) and the tests on Ubuntu + Windows, Python 3.10 and 3.14 |

Data flow: browser → `Handler` → `App` → `klipper`/`bambu` module functions chosen by
`if p["type"] == ...` branches spread over `web.py`, `monitor.py` and `cameras.py`.

## 2. Features that work today

- Dashboard with camera grid/list, status chips, search and filters; Mainsail tab inside the app.
- Klipper: status, progress, file, camera discovery, upload with optional start (skipped while busy).
- Bambu P1/A1/X1: live MQTT status, error codes, camera (P1/A1), FTPS upload, confirmed print start.
- Files library, per-printer queue with a "bed is clear" manual start, Devices page add/edit/remove.
- Notifications (ntfy/Discord/Telegram) for started/complete/error/paused/cancelled/offline.
- Remote access through Tailscale only (`tailscale serve`), identity header required, allow-list.
- Strong existing hardening: localhost-only listeners, DNS-rebinding checks, CSRF header/origin
  checks, CSP, DPAPI secrets, TLS pinning for Bambu, same-host redirects only, audit log.

## 3. Existing printer integrations

| Protocol | State |
|---|---|
| Klipper / Moonraker HTTP | Partial: 2 objects queried, no temperatures, no object discovery, no WebSocket, no pause/resume/cancel |
| Bambu MQTT/FTPS/camera | Good: status, camera, upload, start. No pause/resume/stop, no temperature/fan/layer/AMS/HMS in the status, no read-only mode |
| OctoPrint (Marlin) | Missing |
| Marlin USB serial | Missing |
| PrusaLink | Missing |

## 4. Missing functionality (against the brief)

1. A shared adapter interface with capability detection; protocol logic is spread through `if/else`.
2. Moonraker object discovery, full status (temperatures, fan, position, layers, errors),
   WebSocket subscription with delta merging and reconnect, history, pause/resume/cancel.
3. OctoPrint, Marlin serial and PrusaLink adapters.
4. Bambu read-only mode, `allow_remote_control` / `allow_file_upload` switches, pause/resume/stop,
   richer telemetry (temperatures, fans, layers, AMS, HMS).
5. Normalized status model, printer metadata (manufacturer, model, protocol, location), multi-location.
6. Concurrent monitoring with per-printer timeouts and backoff.
7. Job history (completed/failed with times and errors); printer-specific file validation.
8. Optional password login (users, password hashing, sessions, login rate limiting) for setups
   that cannot rely on Tailscale identity alone; roles for viewing vs. controlling.
9. Control endpoints (pause/resume/cancel) with confirmation for dangerous actions.
10. Notifications for reconnect, printer errors and temperature/filament warnings; email provider.
11. Docs: adapter guide, Bambu modes, remote-access model, MCP design.

## 5. Technical problems and potential bugs

- **Sequential monitoring** (`Monitor.poll_once`): each offline Klipper printer costs a 3 s
  timeout, so 10 offline printers delay every update by ~30 s.
- `tests.test_dashboard.DashboardTest.test_mainsail_forward_is_local_only` failed once in a full
  run on Linux and passed alone: a timing race with the forwarder's port (flaky, pre-existing).
- `klipper.status()` reports only `print_stats.state`; a Klipper host in `shutdown`/`error`
  (klippy not ready) shows as `standby` or `offline` instead of an error.
- Queue jobs disappear when started; there is no history of finished or failed jobs.
- Upload extension check is global (`.gcode/.gco/.g/.3mf`): a `.3mf` can be sent to a Klipper
  printer that cannot print it.
- `App.reload()` restarts every camera hub, watcher and forwarder on any printer change.

## 6. Security weaknesses

- Anyone allowed into the dashboard may start prints and upload files (no viewer/operator split).
- Only one authentication method (Tailscale identity); `require_tailscale_identity: false` opens the
  dashboard to every tailnet device with no login.
- No login rate limiting (no login exists); no session concept for non-Tailscale use.
- Bambu control is on by default once an access code is saved; the brief requires read-only first.
- Upload start on Bambu does not check the printer state first (documented known limit).
- Secrets are DPAPI-encrypted on Windows but stored in plain text in `config.json` on Linux/macOS
  (file is chmod 600). Environment-variable secrets are not supported.
- Moonraker by default trusts the LAN (mitigated by `tools/secure_printers.py`).

## 7. Files that need modification

`dashboard/config.py` (new printer types and fields), `dashboard/monitor.py` (adapters,
concurrency, new events), `dashboard/web.py` (adapter-driven status/upload/control routes, login,
locations), `dashboard/klipper.py` (full status reuse), `dashboard/bambu.py` (richer status,
pause/resume/stop), `dashboard/notify.py` (new events, email), `web/app.js`, `web/app.css`,
`web/index.html` (temperatures, controls by capability, location filter, login),
`README.md`, `SECURITY.md`, `CHANGELOG.md`, `config.example.json`, `.github/workflows/ci.yml`.

## 8. New files

`dashboard/adapters/{__init__,base,moonraker,octoprint,marlin_serial,bambu,prusalink}.py`,
`dashboard/wsclient.py` (stdlib WebSocket client), `dashboard/services/{printer_manager,job_manager}.py`,
`dashboard/auth.py`, `web/login.html`, mock servers in `tests/` (Moonraker WebSocket, OctoPrint,
PrusaLink, Marlin serial), new tests, and `docs/ADAPTERS.md`, `docs/BAMBU.md`,
`docs/REMOTE_ACCESS.md`, `docs/MCP.md`, `docs/DECISIONS.md`.

## Decisions taken for this work (hands-off handover)

See `docs/DECISIONS.md` for every choice made without asking, with the reason.
