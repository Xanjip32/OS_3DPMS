# OS_3DPMS — Open Source 3D Printer Monitoring System

One web page with live camera feeds from all your 3D printers, open from any
of your devices. Click a camera to open that printer's Mainsail. Drag a
`.gcode` file onto a printer to send it, and optionally start the print.

Works with a mix of printers:

- **Klipper printers** with Mainsail or Fluidd (Moonraker API): Voron, Two Trees Sapphire, RatRig, Creality with Klipper, and others
- **Bambu Lab** P1S / P1P / A1 / X1 in LAN mode

**[Tailscale](https://tailscale.com) is the front door.** The dashboard only
listens on `127.0.0.1` and is published to *your* Tailscale network over
HTTPS. No ports are opened on the office or home network, and nothing is
reachable from the internet.

```
 Your laptop / phone ──Tailscale (HTTPS, WireGuard)──▶ Printer-room PC ──LAN──▶ Printers
  (signed in to your        identity checked on        OS_3DPMS on 127.0.0.1
   Tailscale account)        every request
```

## Features

- Camera grid that fits the screen: 2×2 on a monitor, one row on an ultrawide, a single column on a phone
- Click a camera to open that printer's full Mainsail page, through the same Tailscale door
- Drag and drop G-code upload, with an optional "start print" (skipped when the printer is busy)
- Print status and progress for Klipper and Bambu printers
- Notifications when a print finishes, fails, pauses or a printer goes offline: pop-ups in the
  dashboard, and on your phone through ntfy, Discord or Telegram
- Per-camera rotation and mirroring
- Guided setup page: Tailscale login, add printers, publish — no config files to edit
- Python standard library only: no packages to install, small attack surface

## Quick start (Windows)

1. On the PC that sits next to the printers (it must reach their IPs), download this repository:
   **Code → Download ZIP**, and extract it, for example to `C:\OS_3DPMS`.
2. Double-click **`install.bat`**. It installs Python and Tailscale with `winget` if they are
   missing, sets OS_3DPMS to start at login, and opens the setup page.
3. Follow the setup page (`http://127.0.0.1:8079/setup`):
   1. **Log in to Tailscale.**
   2. **Add your printers** (IP address; Bambu printers also need the LAN access code).
   3. **Publish.** The first time, Tailscale asks you to enable HTTPS for your tailnet (one click).
   4. Copy the link, for example `https://printer-pc.tail1234.ts.net/`.
4. On your own laptop or phone, install Tailscale, sign in with the **same account**, and open the link.

To stop: `stop.bat`. To remove: `uninstall.bat`.

### Linux / macOS

```bash
git clone https://github.com/Xanjip32/OS_3DPMS.git && cd OS_3DPMS
python3 run.py --open-setup
```

Install Tailscale first (`curl -fsSL https://tailscale.com/install.sh | sh`) and run as a
user that may call `tailscale serve` (`sudo tailscale set --operator=$USER`).

## Printer notes

**Klipper / Mainsail.** The camera URL is read from Moonraker's webcam list, so whatever
works in Mainsail works here. If uploads fail with 401, add the printer PC to
`trusted_clients` in `moonraker.conf`, or enter a Moonraker API key on the setup page.

**Bambu Lab.** You need the LAN access code (printer screen: Settings → Network). Uploads
go to the SD card, so a card must be inserted. To start prints remotely, also enter the
serial number (Settings → Device) and send `.gcode.3mf` files exported from Bambu Studio or
OrcaSlicer. The serial number also turns on live status and notifications for the printer. Newer firmware may require *LAN Only Mode* / *Developer Mode* for third-party
tools, and future firmware updates may change this.

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
- Every dashboard request must carry the Tailscale identity header; an optional allow-list narrows it further.
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
