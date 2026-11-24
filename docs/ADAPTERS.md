# <img src="../assets/openprintdeck-icon.svg" alt="OpenPrintDeck" width="36" height="36" align="absmiddle"> Printer adapters

Every printer in `config.json` is handled by an **adapter** (`dashboard/adapters/`). The rest of
OpenPrintDeck (monitor, dashboard, uploads, queue, notifications) only talks to the adapter interface,
so a new protocol is one new file plus a line in `adapters/__init__.py`.

| `type` | Adapter | Protocol | Live status | Upload | Start / pause / resume / cancel | Files | Camera | History |
|---|---|---|---|---|---|---|---|---|
| `klipper` | `MoonrakerAdapter` | Moonraker HTTP + WebSocket (JSON-RPC) | push (WebSocket), HTTP fallback | yes | yes | yes | Moonraker webcam list | yes |
| `octoprint` | `OctoPrintAdapter` | OctoPrint REST + push WebSocket, API key | pushed (~1 s), polled every 5 s as fallback | yes (`.gcode`) | yes | yes | OctoPrint webcam settings | – |
| `prusalink` | `PrusaLinkAdapter` | PrusaLink v1 REST, HTTP Digest | polled every 5 s | yes (`.gcode`, `.bgcode`) | yes | – | – (use `webcam_url`) | – |
| `bambu` | `BambuAdapter` | MQTT over TLS 8883, FTPS 990, JPEG camera 6000 | push (MQTT) | only with `mode: control` + `allow_file_upload` | only with `mode: control` + `allow_remote_control` | – | P1/A1 built-in | – |
| `marlin_serial` | `MarlinSerialAdapter` | USB serial, read-only G-code allowlist | polled every 3 s | **never** | **never** | – | `webcam_url` only | – |

## The interface (`adapters/base.py`)

```text
PrinterAdapter(printer_dict, pins)
  supports                       what the protocol can do (CAPABILITY_KEYS)
  get_capabilities()             supports AND the printer's own allow_* switches = what may be offered
  connect() / disconnect()       background connections (WebSocket, MQTT, serial reader)
  fetch_status() / get_status()  normalized status (below); None or "offline" instead of raising
  get_temperature() / get_progress()
  upload_file(path, name, start) / start_print(name) / pause_print() / resume_print() / cancel_print()
  list_files() / get_history() / diagnostics() / camera_source() / info() / accepts_file(name)
```

Errors are typed: `NotSupported` (protocol cannot), `NotAuthorized` (the printer's
`allow_*` switch or Bambu read-only mode refuses), `PrinterBusy`, `AdapterError` (anything the
printer said). The web layer maps them to 400 / 403 / 409 / 502.

Capability keys: `monitor`, `can_upload`, `can_start_print`, `can_pause`, `can_resume`,
`can_cancel`, `can_control_temperature` (reported, never offered in the UI), `can_list_files`,
`has_camera`, `has_ams`, `has_history`. The dashboard shows a button only when the capability is
true, and the server checks it again on every request.

### Normalized status

```json
{"state": "printing", "message": "", "file": "benchy.gcode", "progress": 0.42,
 "duration": 1520, "remaining_s": 1860, "remaining_min": 31,
 "temperatures": {"extruder": {"actual": 215.0, "target": 215.0}, "bed": {"actual": 60.1, "target": 60.0}},
 "fan": 0.5, "position": [11.0, 21.0, 3.0], "tool": "extruder",
 "layer": {"current": 12, "total": 120}, "ams": null,
 "errors": [], "warnings": [], "transport": "websocket"}
```

`state` is one of `standby printing paused complete cancelled error offline`. Missing values are
`null` / absent, never guessed.

## Configuration per type

Common fields: `id`, `name`, `type`, `host`, `location`, `manufacturer`, `model`, `webcam_url`,
`rotate`, `flip_horizontal`, `flip_vertical`, `allow_file_upload`, `allow_remote_control`.
Secrets (`api_key`, `access_code`, `password`) are DPAPI-encrypted on Windows; on Linux/macOS use
`<field>_env` to read them from an environment variable instead of the file, for example
`"api_key_env": "OCTOPRINT_KEY"`.

| Type | Extra fields |
|---|---|
| `klipper` | `http_port` (Mainsail/Fluidd/Moonraker port, default 80), `ui_port`, `api_key` (optional) |
| `octoprint` | `http_port` (default 80), `api_key` (Settings → Application Keys), `websocket` (default `true`: live status over OctoPrint's push channel) |
| `prusalink` | `http_port` (default 80), `username` (default `maker`), `password` (printer screen → Settings → Network) |
| `bambu` | `access_code`, `serial`, `use_ams`, `mode` (`read_only` default / `control`), `mqtt_port` 8883, `camera_port` 6000, `ftps_port` 990 |
| `marlin_serial` | `serial_port` (`COM3`, `/dev/ttyUSB0`), `baudrate` (115200 / 250000 …). Needs `pip install pyserial` |

Defaults for `allow_file_upload` / `allow_remote_control`: **on** for `klipper`, `octoprint`,
`prusalink` (keeps the existing Klipper behaviour), **off** for `bambu` and `marlin_serial`.

## Protocol notes

**Moonraker.** On start the adapter calls `printer.objects.list`, subscribes to the objects it
understands (`print_stats`, `display_status`, `virtual_sdcard`, `extruder*`, `heater_bed`,
`temperature_sensor *`, `fan`, `toolhead`, `webhooks`, `idle_timeout`, …; never `configfile` or
`bed_mesh`, which are large) and merges every `notify_status_update` delta into its copy. Heartbeat
every 20 s; no message for 50 s means the socket is dead; reconnect with backoff up to 60 s. While
the WebSocket is down, status comes from HTTP. With an API key the WebSocket uses a oneshot token
(`/access/oneshot_token`), so the key is never put in a URL. Klippy `shutdown`/`error` is shown as
`error` with Klipper's message (this was shown as standby before).

**OctoPrint (and Marlin through OctoPrint).** A Marlin printer that is plugged into a Raspberry
Pi or PC running OctoPrint is added as type `octoprint` (*Marlin through OctoPrint*), with the
OctoPrint address and API key, **not** as `marlin_serial`: OctoPrint owns the USB port, and this
gives uploads, start/pause/resume/cancel and the webcam. `marlin_serial` is only for a printer
plugged straight into the OpenPrintDeck PC.

Status is pushed: the adapter signs in with a passive `POST /api/login` using the API key (a
WebSocket cannot send `X-Api-Key`), opens OctoPrint's raw SockJS WebSocket at
`/sockjs/websocket` (standard library client, no extra package), sends `{"auth": "user:session"}`
and `{"throttle": 2}`, and reads the `history`/`current` messages (state, job, progress,
temperatures; temperatures are kept from the last message that had them). REST polling of
`/api/printer` and `/api/job` every monitor cycle is used whenever the WebSocket is off
(`"websocket": false`), refused (for example an old global API key that cannot log in), or sent
nothing for 15 s; it reconnects with backoff up to 60 s and answers `reauthRequired` by signing in
again. The device diagnostics show `websocket`, `websocket_error` and `reconnects`.
"Printer not connected" (409, or *Offline* over the WebSocket) is shown as offline. Webcam
URLs from OctoPrint's settings are used only when they point at the OctoPrint host.
**The push channel is tested against a fake OctoPrint only**, not yet against a real one; if it
misbehaves, set `"websocket": false` for that printer to get the old polling.

**PrusaLink.** Digest authentication is computed by hand (stdlib), uploads use `PUT
/api/v1/files/usb/<name>` with `Overwrite-File: ?0` (an existing file is never replaced unless you
confirm overwrite), pause/resume/cancel use `/api/v1/job/<id>`. **Not yet tested on a real Prusa.**

**Bambu.** See [BAMBU.md](BAMBU.md).

**Marlin serial.** Monitor-only by design. The adapter opens the port exclusively with DTR/RTS
held low (most boards then do not reset), sends only read commands from a fixed allowlist (`M115` firmware info, `M105`
temperatures, `M114` position, `M119` endstops, `M27` SD progress, `M31` print time, `M503`
settings report, `M155`/`M154` auto-report) and refuses everything else. No arbitrary G-code
endpoint exists anywhere in OpenPrintDeck. If another program (OctoPrint, Pronterface, Cura, a slicer)
has the port open, the adapter reports offline instead of fighting for it.

## Safe first connection (Sapphire Pro, Kossel, Bambu P1S)

Do these with the printer **idle and the bed clear**. Nothing below starts a print or changes a
printer setting.

1. **Two Trees Sapphire Pro (Klipper).** Add it as `klipper` with `"allow_remote_control": false`
   and `"allow_file_upload": false`. Check on the dashboard: state, nozzle/bed temperatures, the
   Details dialog (Diagnostics shows the subscribed objects and `transport: websocket`). Pull the
   network cable or stop Moonraker for a minute: the tile goes offline, then you get *reconnected*.
   Only then turn the two switches on. Test Pause/Resume on a short print you started yourself in
   Mainsail.
2. **Anycubic Kossel (Marlin).** Preferred: run it through OctoPrint and add it as `octoprint`
   with both switches off, then on (same checks). Direct USB: add it as `marlin_serial`, close
   every other program using the COM port first, and **never connect while another host is
   printing over that cable**: a board that resets on connect would stop that print.
3. **Bambu P1S.** Add it as `bambu` with the default `"mode": "read_only"`. Check status,
   temperatures, AMS and the camera. The printer is never sent a command in this mode. Only if you
   want remote pause/resume/stop or uploads: set `"mode": "control"` and the matching `allow_*`.

## Adding an adapter

1. Subclass `PrinterAdapter`, set `supports`, implement `fetch_status()` and the
   operations you support (raise `NotSupported` for the rest; the base class does by default).
2. Register it in `dashboard/adapters/__init__.py` `REGISTRY` and add the type to
   `config.PRINTER_TYPES`, `TYPE_LABELS`, `CONTROL_DEFAULT` and its validation.
3. Add a mock server in `tests/` and tests for status parsing, offline handling and refusals.

## Testing without printers

`tests/mock_moonraker.py` (HTTP + WebSocket), `tests/mock_http_printers.py` (OctoPrint,
PrusaLink) and `tests/fleet_fixture.py` (10 simulated Moonraker printers, an OctoPrint printer, 3
offline printers and a Bambu entry in `tests/test_fleet.py`) are used by the tests and can be started by hand for UI work.
