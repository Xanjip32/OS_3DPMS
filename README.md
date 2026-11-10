# OS_3DPMS — Open Source 3D Printer Monitoring System

View your printer cameras and print status in one dashboard. Click a camera to
enlarge it, or use the **Mainsail** button for Klipper printer controls. Drag a
`.gcode` file onto a printer to send it and optionally start the print.

**Current Windows preview:** Install OS_3DPMS on the printer-room PC, add a printer,
and confirm its camera appears. Setup and the dashboard work on that PC without
a remote access account.

**Remote access:** Use the [Cloudflare dashboard](cloud/README.md) for browser
access and phone alerts, or use the optional Tailscale integration.

Works with a mix of printers:

- **Klipper printers** with Mainsail or Fluidd (Moonraker API): Voron, Two Trees Sapphire, RatRig, Creality with Klipper, and others
- **Bambu Lab** P1S / P1P / A1 / X1 in LAN mode

## Features

- Simple navigation: Dashboard, Devices, Queue, Files and Settings in a sidebar (bottom tabs on phones)
- Status counts, search, All / Active / Inactive filters, and camera grid or list view
- Camera cards fill the screen on a desktop and stack on phones
- Click a camera to enlarge it. Use the Mainsail button to open printer controls.
- Drag and drop G-code upload, with an optional "start print" (skipped when the printer is busy)
- Files page: keep G-code and 3MF files on the printer PC (the `files` folder next to
  `config.json`) and send any of them to any printer, with or without starting the print. Shows
  the free disk space. Works on the PC and over Tailscale; the cloud dashboard only points there
- Queue page: line up files per printer (**Add to queue** on the Files page). Each printer runs its
  jobs in order; when it is free (standby, finished or cancelled), its next job shows **Bed is
  clear, start**. Nothing starts by itself. The queue is kept in `queue.json` next to `config.json`
- Print status and progress for Klipper and Bambu printers
- Notifications when a print finishes, fails, pauses or a printer goes offline: pop-ups in the
  dashboard, and on your phone through ntfy, Discord or Telegram
- Per-camera rotation and mirroring
- Hide cameras you do not need on a device (× button), and show them again from the top bar
- Guided setup for adding printers and checking their cameras
- Python standard library only: no packages to install, small attack surface

## Quick start (Windows)

1. Extract the Windows ZIP on a PC that can reach the printers. Keep the extracted folder in a
   permanent location, for example `C:\OS_3DPMS`.
2. Double-click **`install.bat`**. The Windows package includes Python. The source download
   can use an existing Python or install it with `winget`. The installer sets OS_3DPMS to start
   at login and opens [Setup](http://127.0.0.1:8079/setup).
3. Click **Find printers on my network** and **Add all**, or choose **Add Klipper** / **Add Bambu**
   and enter the printer IP. Then click **Save printers**.
   Bambu printers also need the LAN access code. Setup checks the printer and shows a camera
   preview as soon as it receives a frame. Click **Open live dashboard** to see your feeds.

For optional Tailscale access, expand **Remote access** in Setup, install or connect Tailscale,
and click **Publish dashboard**. Open the resulting link on a device connected to the same tailnet.

To stop: `stop.bat`. To remove: `uninstall.bat`.

If a printer or camera fails to connect, click **Save printers**, then **Check connection**
on that printer in setup. The test runs on the printer-room PC and reports the printer and
camera separately. That PC needs network access to the printer's local address. For Klipper,
enter the HTTP port used by the printer's Mainsail or Fluidd page if it differs from 80.
For Bambu, the printer check tests reachability of its LAN port. The camera check needs
a working feed and LAN access code for the built-in JPEG camera.

### Install the Tailscale dashboard as an app (PC, iPhone, Android)

The dashboard installs like an app: its own window or full screen, with an icon, and no address bar.
Every device needs the Tailscale app connected and signed in to the same account.

- **Windows / Mac (Chrome or Edge):** open the dashboard link, click **Install app** at the top
  (or the install icon in the address bar). It appears in the Start menu and taskbar.
- **Android (Chrome):** open the link, tap **Install app** (or menu ⋮ → *Install app*).
- **iPhone / iPad (Safari):** open the link in **Safari**, tap **Share** (square with arrow),
  then **Add to Home Screen** → **Add**. Other iPhone browsers cannot install it.

If the app shows "Can't reach your printers", open Tailscale and connect.

### Linux / macOS

```bash
git clone https://github.com/Xanjip32/OS_3DPMS.git && cd OS_3DPMS
python3 run.py --open-setup
```

For optional remote access, install Tailscale and run as a user that may call
`tailscale serve` (`sudo tailscale set --operator=$USER`).

## Printer notes

**Klipper / Mainsail.** The camera URL is read from Moonraker's webcam list, so whatever
provides an HTTP MJPEG stream or a JPEG snapshot works here. Set **Camera URL override**
in setup to use a different HTTP MJPEG or JPEG snapshot URL. If uploads fail with 401, add the printer PC to
`trusted_clients` in `moonraker.conf`, or enter a Moonraker API key on the setup page.

Out of the box, Moonraker lets any device on your network control the printer without a login.
Run `python tools\secure_printers.py` on the printer PC (printers idle) to require a Mainsail
login everywhere: it saves each printer's API key in OS_3DPMS, creates your login, and trusts
only the printer itself (KlipperScreen keeps working). `--undo` puts the old settings back.

**Bambu Lab.** You need the LAN access code (printer screen: Settings → WLAN / Network). The
code changes when LAN mode is switched off and on, so read it again if the camera stops
working. Uploads go to the SD card, so a card must be inserted. The serial number turns on
live status, notifications and remote start: **Find printers** fills it in, and OS_3DPMS also
picks it up from the printer's network announcement when it is left empty. Send `.gcode.3mf`
files exported from Bambu Studio or OrcaSlicer to start prints remotely.
**Check connection** in setup logs in with the access code and says if the printer refuses it.
P1 and A1 printers allow one camera viewer at a time: close Bambu Studio's or Handy's camera
view, or other viewers, while OS_3DPMS shows the camera. Newer firmware may require *LAN Only Mode* / *Developer Mode* for third-party
tools, and future firmware updates may change this.
The built-in camera reader uses the Bambu JPEG feed on port 6000 (P1P, P1S, A1, A1 mini). X1, H2D
and P2S cameras use RTSP on port 322: setup says so, and you can set **Camera URL override** to an
HTTP MJPEG or JPEG snapshot from a camera bridge such as go2rtc.

## Notifications

Set them up in step 4 of the setup page and click **Send test**.

- **Dashboard pop-ups:** click **Enable alerts** at the top of the dashboard.
- **ntfy** (free, no account): install the [ntfy app](https://ntfy.sh), click
  *Make a private topic link* on the setup page, subscribe to that link in the app, and save.
  The topic name works like a password, so keep it private.
- **Discord:** channel settings → Integrations → Webhooks → New webhook → Copy URL.
- **Telegram:** create a bot with @BotFather, send your bot a message, and enter the bot token
  and your chat id (for example from @userinfobot).

Choose which events notify you. "Attach a camera picture" is off by default, because the
picture is then stored by the notification service.

## Configuration

The setup page writes `config.json`. Settings that are only in the file:

| Key | Default | Meaning |
|-----|---------|---------|
| `port` | `8080` | Dashboard port on 127.0.0.1 (published as HTTPS 443) |
| `admin_port` | `8079` | Setup page port on 127.0.0.1 (never published) |
| `max_upload_mb` | `1024` | Largest accepted G-code file |
| `allowed_users` | `[]` | Tailscale logins allowed in; empty = everyone in your tailnet |
| `require_tailscale_identity` | `true` | Refuse requests without a Tailscale user (for example tagged devices) |

Per printer: `webcam_url` (override the camera URL), `rotate` (0/90/180/270),
`flip_horizontal`, `flip_vertical`, `http_port`.

See [`config.example.json`](config.example.json).

## Security

Read [SECURITY.md](SECURITY.md) for the full threat model. In short:

- All listeners bind to `127.0.0.1`. Remote access exists only through `tailscale serve`.
- Remote dashboard requests must carry the Tailscale identity header by default.
  An optional allow-list restricts access to specific accounts.
- The setup page is never published and rejects non-local `Host` headers (DNS rebinding).
- Uploads need a same-origin custom header and pass Origin / `Sec-Fetch-Site` checks (CSRF).
- Strict Content-Security-Policy, no inline scripts, `X-Frame-Options: DENY`.
- Secrets are encrypted at rest with Windows DPAPI and never sent to the browser.
- Bambu TLS certificates are pinned on first use.
- Upload size limit, one upload per printer at a time, connection cap and timeouts.
- Every upload, config change and publish is written to `audit.log` with the Tailscale user.

## Development

```bash
python -m unittest discover -s tests -t . -v   # tests use a fake Moonraker printer
python tests/mock_printer.py 9001              # fake printer for UI work
```

Project layout:

```
dashboard/   server code (web.py = HTTP server, klipper.py, bambu.py, cameras.py, tailscale.py, ...)
web/         dashboard and setup pages (HTML/CSS/JS, no build step)
tests/       unit and integration tests, fake printer
scripts/     helper scripts used by the .bat files
```

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Not affiliated with Bambu Lab, Two Trees, Voron Design, Mainsail, Klipper or Tailscale.
Starting prints remotely is your responsibility: make sure the bed is clear.

## License

[MIT](LICENSE)
