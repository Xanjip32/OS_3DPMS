# Changelog

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
