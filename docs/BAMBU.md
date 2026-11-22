# Bambu Lab printers

OS_3DPMS talks to Bambu printers **only on your local network** (MQTT over TLS on 8883, the JPEG
camera on 6000, FTPS on 990), with the LAN access code. It never uses the Bambu cloud, never logs
in to a Bambu account and **never changes a printer setting** (LAN mode, cloud binding, Developer
Mode, network, firmware, LEDs, speed, fans or temperatures).

## Modes in OS_3DPMS

| `mode` | What OS_3DPMS does |
|---|---|
| `read_only` (**default**, also for printers saved by older versions) | Subscribes to the printer's reports: state, progress, file, temperatures, fans, layers, AMS, HMS errors, remaining time, camera. Sends **no** command. `allow_file_upload` and `allow_remote_control` are forced off. |
| `control` | Additionally allows, each behind its own switch: uploads to the SD card (`allow_file_upload`), and start / pause / resume / stop (`allow_remote_control`). Stop and start need a confirmation in the dashboard and in the API. |

```json
{"id": "p1s", "name": "Bambu P1S", "type": "bambu", "host": "192.168.1.6",
 "access_code": "12345678", "serial": "01P00A000000000",
 "mode": "read_only", "location": "Office"}
```

Read access never implies write access. In `control` mode the printer may still refuse a command
(see below); OS_3DPMS waits for the printer's answer and shows its reason instead of claiming
success.

## The printer's own settings (you change these on the printer, not OS_3DPMS)

| Printer setting | Effect on OS_3DPMS |
|---|---|
| **Cloud mode** (LAN Only off, bound to Bambu Handy/Studio) | Older firmware: status, camera and commands work on the LAN with the access code. Newer firmware (2025+ "authorization" update): status and camera usually work, but third-party commands and uploads are refused unless Developer Mode is on. |
| **LAN Only Mode** | Cloud is off; the printer is reachable only on the LAN. Status and camera work with the access code. Required before Developer Mode. |
| **Developer Mode** (inside LAN Only Mode, newer firmware) | Third-party tools may send print, pause, resume and stop commands again. Bambu Handy and the cloud do not work in this mode. |
| **Authorization** (newer firmware without Developer Mode) | Commands must be signed by Bambu's software. OS_3DPMS does not try to work around this: it shows the refusal. |
| LAN access code | Changes when LAN mode is toggled. Read it again from the printer screen if the connection stops working. |

Bambu changes this behaviour between firmware versions; always check the current release notes.

## Safe test procedure (P1S)

1. Printer idle, bed clear. Add the printer with `mode: read_only` (the default).
2. Check: tile shows state and temperatures, Details shows AMS trays and HMS codes, camera works
   (close Bambu Studio's / Handy's camera view first: P1/A1 allow one viewer).
3. Diagnostics in the Details dialog: MQTT connected, report age, certificate pinned.
4. Only if you want control: set `mode: control` and `allow_remote_control: true`, then test
   **Pause** and **Resume** on a short print you started from Bambu Studio. If the printer
   refuses, the dashboard says so (turn on LAN Only Mode + Developer Mode, or keep read-only).

## Security notes

- The certificate is pinned on first connection (`pins.json`); a different certificate is refused.
- The access code is DPAPI-encrypted on Windows (`access_code_env` on Linux/macOS) and never sent
  to the browser.
- Uploads re-check the FTPS data channel certificate against the pin.
- Ports (`mqtt_port`, `camera_port`, `ftps_port`) are configurable only for bridges and testing;
  they are always on the printer's own host.
