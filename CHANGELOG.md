# Changelog

## Unreleased

### Bed check and opt-in auto-start (branch `feature/bed-check-orca-plugin`)

- **Bed check** per printer: save an *empty-bed picture* on the Queue page; **Test now** compares
  the camera with it and shows the score. No AI and no extra packages: the built-in reader takes
  the 8×8 block averages straight from baseline JPEGs; Pillow is used when installed (optional,
  also reads progressive JPEGs). Brightness and color are compared over a configurable bed area
  (`bed_check_roi`), with light and white-balance drift compensated
- **Auto-start** of the next queued job (`auto_start`, default **off**): only when the job is ready
  by the existing queue rules, the camera picture is fresh and changing, a part was seen and then
  removed (`bed_check_require_removal`, default on), and `bed_check_frames` pictures in a row match
  the empty bed. Any error means no start; the manual **Bed is clear, start** button stays
- Audit log entry `queue-auto-start` (user `auto-start`, with the score); new notification events
  **auto_started** and **bed_not_clear** (on by default); a refused automatic start sends a warning
  and leaves the job for a person
- Queue starts re-check readiness while holding the printer's upload lock, so a job can never be
  sent twice (also when two people press the button at the same time)
- New API: `GET /api/bedcheck`, `GET /api/bedcheck/<id>/reference.jpg`,
  `POST /api/bedcheck/<id>/reference`, `/reference/delete`, `/test`
- Docs: [docs/BED_CHECK.md](docs/BED_CHECK.md) (how it works, settings, limits)

### Orca Slicer plugin and API tokens (branch `feature/bed-check-orca-plugin`)

- **Orca Slicer printer agent** `orca_plugin/os3dpms_printer_agent_any.py` (beta, single PEP 723
  file, standard library only): agents **OS_3DPMS (queue)** and **OS_3DPMS (print now)**, printer
  list and Bambu-style status in Orca's Device tab, pause / resume / cancel. Needs an Orca Slicer
  Nightly with plugin support and *Use printer agents instead of print hosts*. Tested against a
  stand-in `orca` module only, not yet inside a real Orca Slicer
- **API tokens** (`Authorization: Bearer os3_...`, roles operator / viewer, stored as SHA-256 in
  `api_tokens`): Setup page card, `run.py --add-token NAME [--role viewer]` / `--remove-token NAME`.
  Host and Tailscale allow-list checks still apply; wrong tokens are audited and rate-limited
- New API: `GET /api/plugin/printers`, `POST /api/plugin/jobs?printer=&name=&action=queue|send|print`,
  admin `GET/POST /api/setup/tokens`, `POST /api/setup/tokens/delete`
- `.github/workflows/publish-orcacloud.yml`: publishes the plugin to the Orca Cloud Plugin Hub on
  `orca-plugin-v*` releases, off until the repository variable `ORCACLOUD_PUBLISH` is `true`
- Docs: [docs/ORCA_PLUGIN.md](docs/ORCA_PLUGIN.md)

### Webcam (branch `feature/bed-check-orca-plugin`)

- `camera.remote_max_fps` (default 5, as before): picture cap for remote viewers is now a
  setting, and paced by time, so the newest picture is sent when one is due (no fixed sleep after
  each picture)
- JPEG snapshot cameras are polled over one kept-alive HTTP connection every
  `camera.snapshot_interval_s` (default 0.2 s) instead of a new connection plus a fixed sleep;
  a camera that closes the connection, or answers oddly, falls back to a plain request
- MJPEG streams: the part's `Content-Length` is used when present, so a picture with an embedded
  thumbnail is no longer cut short; without it (or when it does not match) the start/end markers
  are used as before
- `camera.idle_seconds` (default 20) and per-printer `camera_always_on` (Devices dialog: *Keep the
  camera connected without viewers*), useful for the bed check

### Marlin through OctoPrint (branch `feature/bed-check-orca-plugin`)

- OctoPrint status is now **pushed** over OctoPrint's raw SockJS WebSocket (`/sockjs/websocket`,
  standard library, passive `/api/login` with the API key), with REST polling as fallback when it
  is off (`"websocket": false`), refused or quiet for 15 s. Tested against a fake OctoPrint only
- The type is now called **Marlin through OctoPrint** on the Setup and Devices pages, with a note
  that a Marlin printer behind OctoPrint uses it (not *Marlin USB serial*, which is for a printer
  plugged into this PC). Docs: [docs/ADAPTERS.md](docs/ADAPTERS.md)

### Universal printer adapters (branch `feature/universal-adapters`)

- **Breaking for Bambu remote control:** Bambu printers are now **read-only by default**, also
  printers saved by older versions (status, temperatures, AMS, HMS and camera work; no commands
  are sent). To upload or start/pause/stop remotely set *Mode → Control* and tick the switches in
  Devices (`"mode": "control"`, `"allow_file_upload"`, `"allow_remote_control"`)
- New printer types: **OctoPrint** (Marlin), **PrusaLink** (experimental), **Marlin USB serial**
  (monitor only; optional `pyserial`). Every printer type goes through an adapter with capability
  detection (`dashboard/adapters/`, see `docs/ADAPTERS.md`)
- **Klipper live over WebSocket**: Moonraker object discovery, subscription with delta merging,
  heartbeat and reconnect with backoff, HTTP fallback; Klippy shutdown/error shows as an error
- Cards show **temperatures**, layer and remaining time, printer warnings, and **Pause / Resume /
  Cancel** where the printer allows it (cancel asks first). **Details** opens a 30-minute
  temperature chart, diagnostics and print history
- **Locations**: `location`, `manufacturer`, `model` per printer and a location filter
- **Concurrent monitoring** with per-printer timeouts and backoff: offline printers no longer
  slow down the others
- New notifications: printer **reconnected** and printer **warnings** (Bambu HMS, filament/AMS);
  **e-mail (SMTP)** channel (config file only, `docs/NOTIFICATIONS.md`)
- **Job history** (`history.json`): finished, failed and cancelled prints per printer
- Optional **password login** (`auth.mode`: `tailscale`, `password`, `tailscale+password`) with
  `admin` / `operator` / `viewer` roles, `run.py --add-user` / `--remove-user`, login rate
  limiting; `read_only_users` for Tailscale accounts that may only watch
- New API: `/api/printers/<id>` (+ `/diagnostics`, `/history`, `/files`, `/temperatures`),
  `POST /api/printers/<id>/pause|resume|cancel|start` (cancel/start need a confirmation),
  `/api/locations`, `/api/history`
- Uploads check the file type per printer (`.3mf` only for Bambu, `.bgcode` for PrusaLink) and
  ask before overwriting an existing file
- Setup page: add OctoPrint, PrusaLink and Marlin USB printers (*+ Other type*), Bambu mode and
  location per printer
- Secrets can come from environment variables (`api_key_env`, `access_code_env`, ...)
- Fixed a flaky Mainsail forward test (port reuse race)
- Docs: `docs/ASSESSMENT.md`, `ADAPTERS.md`, `BAMBU.md`, `REMOTE_ACCESS.md`, `NOTIFICATIONS.md`,
  `MCP.md` (design only), `DECISIONS.md`

### Earlier unreleased changes

- **Bambu print start is confirmed**: OS_3DPMS waits for the printer to accept the print and shows
  its reason when it refuses (newer firmware: turn on LAN Only Mode and Developer Mode), instead of
  always saying "print started". A queued job whose start is refused now stays in the Queue
- The print command goes over the open status connection when there is one, with its own
  sequence id; a .3mf without a sliced plate is refused before upload; the AMS mapping follows
  the project's filaments; spaces in file names become underscores for Bambu printers
- **Mainsail tab**: each printer's Mainsail inside the app, next to Dashboard (like a slicer's device
  tab), with a printer picker and Open in browser. It stays loaded while you use the other tabs. The
  Mainsail buttons on the cards, in Devices and in the camera view switch to it
- Files page: **Download** a G-code or 3MF file from the printer PC
- Devices page: **+ Add printer**, **Edit** and **Remove** now work over Tailscale too, in a dialog
  inside the dashboard, for accounts listed in Setup → Allowed Tailscale accounts. A new Klipper
  printer's Mainsail port is published on Tailscale automatically, and a removed one's is taken down
- Queue page: **+ Add job**: choose a printer and a file from Files, or upload a new file right there
- Cameras over Tailscale send fewer pictures so phones and slow links keep up: 1 a second per
  card in the grid and up to 5 a second in the big view (about a fifth of the data before). The
  printer PC itself still gets every picture

## 1.5.0-preview.2 (2026-11-10)

- Setup shows a QR code of the Tailscale dashboard link: scan it with a phone camera to open the
  dashboard, then choose Install app. Made on the PC (no QR web service sees the link)
- Removed the Cloudflare cloud dashboard. Tailscale is the one way in from outside: faster,
  private, and nothing on the public internet. Phone alerts while the app is closed use ntfy,
  Discord or Telegram (Setup → Phone notifications). An old Cloudflare key in `config.json` is
  removed from the file the next time OS_3DPMS starts

## 1.5.0-preview.1 (2026-11-10)

- Devices page: **+ Add printer**, and **Edit** / **Remove** on each printer (on the printer PC).
  Edit opens that printer in Setup; Remove asks first and leaves the printer itself unchanged
- Remote access: Tailscale has its own step in Setup, opened until it is done, with the steps for
  your phone or laptop once the dashboard is published. The cloud dashboard shows **Full
  dashboard** and per-printer **Mainsail** buttons that open the Tailscale pages (only real
  `*.ts.net` links are used)
- Queue page: add files to a printer's queue from the Files page; when the printer is free, its
  next job shows **Bed is clear, start**. Jobs run in order, nothing starts by itself, and the
  queue survives restarts
- Files page: upload a G-code or 3MF file once, keep it on the printer PC, and send it to any
  printer (**Send** or **Send and print**, which asks you to check the bed first); delete files
  and see the free disk space
- Cloud cameras: live over WebSockets. The printer PC pushes each camera's newest picture
  (about one a second) while it is on screen, and the page gets it the moment it arrives. Before,
  each picture was a separate upload and download, which from far-away Cloudflare locations took
  1 to 7 seconds and blanked every camera when one upload was slow. Falls back to one request
  per picture when a WebSocket cannot connect
- Cameras keep the last picture through short gaps, and explain only when pictures stopped for
  20 seconds; the cloud dashboard shows the printer PC's reason (for example HTTP 500 from the
  camera service) instead of "Waiting for a camera preview"
- Cloud uploads: one slow camera no longer cancels the other cameras' pictures; requests to
  Cloudflare wait up to 10 seconds instead of 4
- Alerts: **Enable alerts** asks for notification permission itself, says what is happening,
  explains blocked notifications, replaces a subscription made for an older alert key, and
  sends a test alert straight away
- Security: the dashboard and Mainsail forwards refuse requests for other website names, so a
  malicious page cannot use a browser on the printer PC to upload or start prints (DNS rebinding)
- Security: printer redirects are followed only on the same printer (the API key never leaves it),
  and a Bambu upload's file connection must present the pinned printer certificate
- `tools/secure_printers.py` requires a Mainsail login on Klipper printers (Wi-Fi, dashboard and
  Tailscale alike) and gives OS_3DPMS each printer's API key so it keeps working; `--undo` reverts
- Cloud: the printer PC names itself when it connects, so Cloudflare no longer blocks it as a
  bot (403, error 1010) and the cloud dashboard shows the printers
- Setup: **Find printers on my network** finds Klipper and Bambu Lab printers and adds them in
  one click, with their name, camera rotation and the Bambu serial number
- Bambu: a missing serial number is filled in from the printer's network announcement, which
  turns on live status, notifications and remote start
- Bambu: **Check connection** logs in with the saved access code and says when it is refused
- Bambu camera: empty frame headers no longer end the connection; X1, H2D and P2S printers are
  named as RTSP cameras that need a bridge
- Bambu status: a print stopped on the printer shows as cancelled, not failed; print errors use
  Bambu's own code format (for example 0300-400C)
- Cameras: a failing camera answers in about a second instead of eight, so it no longer slows the
  other camera cards; retries slow down from 3 to 30 seconds; HTTP 500 from a Klipper camera
  explains that the printer's camera service (crowsnest) has no picture
- Fixed a socket leak on every retry of a camera that answers with an HTTP error
- CI: fixed the lint failure (unused import) and test on Python 3.10, since GitHub's Ubuntu 26.04
  runners no longer have Python 3.9

- Klipper cameras: use the camera link Mainsail uses even when it names the printer by
  hostname or localhost, and fall back to the usual camera addresses when one fails
- Camera cards and the setup check show which camera address failed and why
- Setup can import all printers from the old printer-dashboard in one click, including the
  Bambu access code that worked there
- Stopping or starting OS_3DPMS turns off the old printer-dashboard and its auto-start, so
  the two no longer fight over cameras (a Bambu P1S allows one camera viewer)
- Cloud dashboard has its own Google sign-in; Cloudflare Zero Trust is optional

- New dashboard layout: left sidebar (bottom tabs on phones) with Dashboard, Devices, Queue,
  Files and Settings pages
- Dashboard shows printing / paused / idle / offline counts, search, All / Active / Inactive
  filters, and a camera grid or list view
- Devices page lists every printer with type, status and quick buttons; Settings page shows the
  signed-in account, app install, alerts and hidden cameras
- Cloud dashboard shows the signed-in Gmail address; docs explain Google sign-in with
  Cloudflare Access

- Connect a Windows printer host to a protected Cloudflare Workers dashboard for camera previews and status.
- Receive print alerts directly from the installed cloud dashboard through Web Push.

- Show a camera preview automatically after saving a new or edited printer.
- Show four camera cards on a desktop dashboard and scroll for additional printers.
- Bundle Python in the Windows package and make Tailscale installation optional.
- Detect Tailscale in custom Windows install locations and show when it is disconnected.
- Load Windows' built-in signature checker before verifying the Tailscale installer.
- Simplify setup around printers and remote access, with optional settings in expandable sections.
- Refresh the monitoring dashboard with a clearer camera grid and direct links to local setup.
- Save allowed Tailscale accounts separately from printer settings.
- Setup can check printer and camera connectivity independently before publishing on Tailscale.
- Configure a Klipper printer HTTP port or an HTTP camera override directly in setup.
- Dashboard snapshots report offline when the camera stops sending frames.

## 1.3.1

- × button on each camera hides it on that device; "Hidden (n)" menu at the top shows it again.
  Hidden cameras stop streaming. The choice is saved in the browser, so each device can differ

## 1.3.0

- The dashboard installs as an app on Windows, Mac, Android and iPhone (web app manifest,
  icons, service worker). "Install app" button in the dashboard header
- Installed app shows a clear "connect Tailscale" page when the printer PC cannot be reached
- Phone pop-up alerts go through the service worker, so they work on Android too
- Layout respects the phone notch and home bar

## 1.2.0

- "Install Tailscale" button on the setup page (Windows): downloads the official MSI,
  verifies the Tailscale Inc. signature, installs silently after one Windows prompt
- `install.bat` uses the same verified installer instead of winget for Tailscale
- `run.py --install-tailscale` for scripted installs
- Sign-in and approval links are limited to `https://*.tailscale.com`; a fallback link
  appears when the browser blocks the sign-in tab

## 1.1.0

- Notifications when a print starts, finishes, fails, pauses, is cancelled, or a printer
  stops answering while printing
- Phone delivery through ntfy, Discord webhooks or Telegram bots, set up on the setup page,
  with a "Send test" button and an optional camera picture
- Pop-up alerts in the open dashboard (browser notifications)
- Live status for Bambu Lab printers over MQTT (needs the serial number)
- One background monitor polls the printers; the dashboard reads its cached status

## 1.0.0

First public release.

- Camera grid for Klipper (Moonraker/Mainsail) and Bambu Lab printers, fitted to the screen
- Click to open Mainsail; drag-and-drop G-code upload with optional print start
- Tailscale as the only way in: localhost-only listeners published with `tailscale serve`,
  Tailscale identity required on every request, optional user allow-list
- Guided setup page: Tailscale login, printer editor, one-click publish
- Security: CSRF protection, strict CSP, DPAPI-encrypted secrets, Bambu certificate pinning,
  upload limits, audit log
- Windows installer script that installs Python and Tailscale with winget
