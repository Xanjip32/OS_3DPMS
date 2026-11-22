# Orca Slicer plugin (printer agent)

OS_3DPMS works on its own: the dashboard, the queue and remote access with Tailscale need no
slicer plugin. The plugin is an **optional extra** for people who slice in Orca Slicer and want a
*Print* button that sends straight to OS_3DPMS. Both live in this repository:

| Part | Where | Who installs it |
|------|-------|-----------------|
| OS_3DPMS dashboard | `run.py`, `dashboard/`, `web/` | the printer PC (Windows zip / Python) |
| Orca Slicer printer agent | [`orca_plugin/os3dpms_printer_agent_any.py`](../orca_plugin/os3dpms_printer_agent_any.py) | each computer that runs Orca Slicer |

> **Status: beta, not yet tested inside a real Orca Slicer build.** It is written against Orca
> Slicer's published Printer Agent API reference and tested here with a stand-in for Orca's
> embedded `orca` module (`tests/test_orca_plugin.py`) against a running OS_3DPMS. Expect rough
> edges in the Device tab (status mapping) until someone tries it in Orca. Please report what you see.

## Requirements

- **Orca Slicer with plugin support.** Python plugins are a beta feature that currently needs a
  current **Orca Slicer Nightly** build. The stable release (2.5.2 at the time of writing) has no
  plugin support and cannot load this file.
- **Preferences > Developer > Experimental Features > "Use printer agents instead of print
  hosts"** turned on. While it is off, Orca keeps the classic print-host upload for non-Bambu
  printers and never routes print jobs to a plugin agent.
- An OS_3DPMS **API token** (below). No extra Python packages: the plugin uses only the standard
  library that ships inside Orca.

## 1. Make an API token in OS_3DPMS

On the printer PC, either:

- Setup page (`http://127.0.0.1:8079/setup`) > **Orca Slicer plugin (API tokens)** > name it
  (for example `orca-laptop`), choose **operator** (may send and start jobs) or **viewer** (status
  only) > **Create**; or
- `py run.py --add-token orca-laptop` (add `--role viewer` for status only).

The token (`os3_...`) is shown **once**. Only its SHA-256 hash is stored in `config.json`
(`api_tokens`). Remove it with the Setup page or `py run.py --remove-token orca-laptop`; it stops
working at once.

## 2. Install the plugin in Orca Slicer

Either:

- **Side-load:** Orca Slicer > Plugins > the **Browse plugins** dropdown > **Install local
  plugin** > choose `os3dpms_printer_agent_any.py`. Orca copies it into
  `data_dir()/orca_plugins/<plugin folder>/`.
- **Orca Cloud Plugin Hub:** once the plugin is published there (see *Publishing* below),
  subscribe to it in Orca Slicer > Plugins > Plugin Hub with your Orca Cloud account.

Activate it in the Plugins dialog. It registers two printer agents:

| Agent (Printer Agent dropdown) | ID | What *Print* does |
|---|---|---|
| OS_3DPMS (queue) | `os3dpms_queue` | adds the file to OS_3DPMS **Files** and the printer's **queue**. It starts with the Queue page button, or by itself when the bed check with auto-start is on ([BED_CHECK.md](BED_CHECK.md)) |
| OS_3DPMS (print now) | `os3dpms_direct` | uploads to the printer and starts it now. OS_3DPMS refuses if the printer is busy |

*Send* (upload without printing) uploads to the printer with either agent.

## 3. Set up the printer preset

Printer settings (advanced mode):

- **Basic information > Advanced > Printer Agent:** one of the two agents.
- **Print host:**
  - on the printer PC itself: `http://127.0.0.1:8079` (the setup port is local only) or
    `http://127.0.0.1:8080`;
  - from another computer: the Tailscale address `https://<printer-pc>.<tailnet>.ts.net`. The
    dashboard is published only through `tailscale serve`, so the other computer has to be on
    your tailnet ([REMOTE_ACCESS.md](REMOTE_ACCESS.md)). A `*.ts.net` address always uses HTTPS.
  - To tie the preset to one OS_3DPMS printer add `/<printer id>`, e.g.
    `https://farm.tail1234.ts.net/voron`. Without it, Orca's Device tab lists every OS_3DPMS
    printer and you pick one before printing.
- **API key:** the `os3_...` token.

The first time it connects, Orca's plugin permission prompt asks whether the plugin may open a
network connection to that address. Allow it. A reinstall or update asks again.

## How it talks to OS_3DPMS

All calls use `Authorization: Bearer <token>`:

| Call | Used for |
|------|----------|
| `GET /api/me` | connection check (also Orca's *verify job*) |
| `GET /api/plugin/printers` | printer list, status, queue and bed check every 3 s while connected |
| `POST /api/plugin/jobs?printer=<id>&name=<file>&action=queue\|send\|print` | the G-code body. `queue` stores it in Files (an identical file keeps its name, a different one gets `name (2).gcode`) and appends a queue job; `send`/`print` upload to the printer |
| `POST /api/printers/<id>/control` | pause / resume / cancel from Orca's Device tab |

Security rules for tokens:

- A token works on the dashboard port from this PC or from your tailnet, and on the setup port
  from this PC only. It never opens the Setup page API or config changes.
- The Host check (DNS rebinding) and the Tailscale allow-list still apply: a request that carries
  a Tailscale login that is not allowed is refused even with a valid token.
- Token requests skip the browser CSRF header check (a web page cannot attach an
  `Authorization` header to a cross-site request).
- Wrong tokens are audited (`wrong-token`) and rate-limited (10 per 15 minutes per Tailscale login, or for this PC);
  uploads and control actions are audited as `token:<name>`.
- Roles: `operator` may upload and control, `viewer` only reads. Bambu printers in read-only
  mode and the per-printer `allow_file_upload` / `allow_remote_control` switches still apply.

## Limits

- **Untested in real Orca Slicer** (see Status). Orca's printer agent API is new and may change.
- Orca's Device tab is built for Bambu printers. The plugin translates OS_3DPMS status into the
  Bambu-style `{"print": {...}}` message (state, progress, time left, temperatures, layer, file).
  Anything else in that tab (AMS, fans, camera, HMS) stays empty. Use the OS_3DPMS dashboard for
  the camera and details.
- Only pause, resume and cancel are passed through. Raw G-code and other commands are refused
  (OS_3DPMS has no endpoint that runs arbitrary G-code).
- No printer discovery on the network: printers come from the OS_3DPMS address in the preset.
- Large files are sent in one request. The dashboard's `max_upload_mb` applies.

## Publishing to Orca Cloud (maintainers)

[`.github/workflows/publish-orcacloud.yml`](../.github/workflows/publish-orcacloud.yml) uploads the
plugin file with GitHub OIDC (no secrets). It is **off** until you:

1. Create the plugin on <https://cloud.orcaslicer.com> (Plugins > Shared > Create), then Edit
   plugin > GitHub publishing > connect `Xanjip32/OS_3DPMS`.
2. Set the repository variable `ORCACLOUD_PUBLISH` = `true`.
3. Bump `version` in the PEP 723 header **and** `PLUGIN_VERSION` in the plugin, then publish a
   GitHub release tagged `orca-plugin-v<version>` (e.g. `orca-plugin-v0.1.1`). The release notes
   become the changelog.

Dashboard releases (`v*` tags) never publish the plugin, and the plugin tag does not build the
dashboard zip.
