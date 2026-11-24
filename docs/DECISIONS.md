# Decisions taken during the universal-adapter work

The work was a hands-off handover: whenever the brief left a choice open, the safest
well-supported option was taken and recorded here. Each can be revisited.

| # | Decision | Why |
|---|---|---|
| 1 | **Standard library only**; `pyserial` is optional and needed only for `marlin_serial` | The project's selling point is "no packages to install, small attack surface". WebSocket (RFC 6455) and HTTP Digest were small enough to write and test. |
| 2 | **Adapter layer** in `dashboard/adapters/`, wrapping the existing `klipper.py` / `bambu.py` instead of rewriting them | Keeps the battle-tested Bambu MQTT/FTPS/TLS-pinning code and all existing tests; backward compatible. |
| 3 | **Bambu is read-only by default, also for existing configs** (`mode: read_only`) | The brief asks for a non-invasive first connection and "read access never implies write access". This changes behaviour for anyone who used remote start on Bambu: set `"mode": "control"` and `"allow_remote_control": true`. Noted as a breaking change in the CHANGELOG. |
| 4 | Upload/control default **on** for `klipper`, `octoprint`, `prusalink` | Keeps the existing Klipper upload-and-start behaviour; these protocols are designed for remote control. Each printer can turn it off. |
| 5 | **Marlin serial is monitor-only** with a fixed allowlist of read commands | Streaming G-code over serial from a dashboard is fragile (a PC sleep kills the print) and the brief forbids arbitrary G-code. OctoPrint is the recommended way to control a Marlin printer. |
| 6 | **OctoPrint is REST-polled** (5 s), not SockJS | SockJS would need a dependency; 5 s is fine for a dashboard. |
| 7 | **PrusaLink implemented against the documented v1 API but untested on hardware** | No Prusa printer available; covered by a mock and flagged as experimental. |
| 8 | **Moonraker WebSocket with HTTP fallback** | Push updates and lower load; if the socket drops, status keeps coming over HTTP. |
| 9 | **Concurrent monitor** (thread pool, per-printer timeouts, backoff up to 60 s for offline printers) | One offline printer no longer delays all others. |
| 10 | **Password login is optional**; Tailscale identity stays the default | Existing installs keep working unchanged; password mode is for setups that want a second factor or have no Tailscale identity. |
| 11 | **Sessions in memory** | Single-PC app; a restart signing everyone out is acceptable and avoids storing session secrets on disk. |
| 12 | PBKDF2-SHA256 310 000 iterations, minimum 10 characters | OWASP recommendation available in the standard library (no bcrypt/argon2 dependency). |
| 13 | Roles: `admin` / `operator` / `viewer`, plus `read_only_users` for Tailscale accounts | Viewing vs. controlling was the main gap; admin is reserved for future remote settings. |
| 14 | Cancel and start need `{"confirm": "<printer id>"}` (HTTP 428 otherwise); pause/resume do not | Cancel/start are irreversible or move hardware; pause/resume are recoverable. |
| 15 | **No arbitrary G-code and no temperature setting** in any API, even where the protocol could | Brief requirement; smallest blast radius. `can_control_temperature` is reported but not offered. |
| 16 | **Temperature runaway is left to firmware**; OpenPrintDeck surfaces the shutdown / HMS error and notifies | Firmware reacts in milliseconds; a dashboard polling every few seconds must not pretend to be a safety system. |
| 17 | E-mail notifications are configured in `config.json` only | Keeps the Setup page simple; SMTP is an advanced option. TLS verified, plain SMTP only to localhost. |
| 18 | **No custom VPN**; Tailscale stays the only remote path, and stays optional | Proven, audited, already integrated; building a VPN would add risk. |
| 19 | MCP is **design only** (`docs/MCP.md`) | Brief requirement; it should be built on top of the stable HTTP API and roles. |
| 20 | Existing upload of an existing file name returns **409** and needs `overwrite=1` | Silent overwrite of printer files was possible before; the dashboard now asks. |
| 21 | Version not bumped; changes are under *Unreleased* in the CHANGELOG | Releases are cut by the maintainer (tag + version), not by this work. |
| 22 | Work kept on local branch `feature/universal-adapters`, not pushed; the PC clone stays on `main` | The maintainer pushes and merges; the running dashboard on the office PC was not interrupted. |
