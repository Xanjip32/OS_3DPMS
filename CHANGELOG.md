# Changelog

## Unreleased

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
