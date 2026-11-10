# Changelog

## Unreleased

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
