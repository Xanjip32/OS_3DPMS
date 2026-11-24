# MCP server (design only, not implemented)

A future [Model Context Protocol](https://modelcontextprotocol.io) server would let an AI
assistant read the printer fleet and, under strict limits, act on it. Nothing in this document
is implemented yet; it records the design so it can be built safely later.

## Principles

1. **Read by default.** The server starts with read tools only. Write tools must be turned on
   per installation (`mcp.allow_actions`), and every printer's own `allow_remote_control` still
   applies on top.
2. **Same rules as a person.** The MCP server is a client of the existing HTTP API with its own
   dashboard account (`--add-user mcp --role viewer` or `operator`), so roles, CSRF, rate limits,
   confirmations and `audit.log` apply unchanged. It never talks to printers directly.
3. **Local only.** stdio transport on the OpenPrintDeck PC (or over Tailscale to the same API); no
   public listener.
4. **No raw G-code, no settings.** There is no tool for arbitrary G-code, temperature changes,
   firmware, network or Bambu settings, and none will be added.

## Tools

| Tool | Kind | Maps to |
|---|---|---|
| `list_printers(location?)` | read | `GET /api/printers` (+ capabilities) |
| `get_printer_status(id)` | read | `GET /api/printers/<id>` |
| `get_temperatures(id)` | read | `GET /api/printers/<id>/temperatures` |
| `get_job_history(id?, limit)` | read | `GET /api/history`, `/api/printers/<id>/history` |
| `list_locations()` | read | `GET /api/locations` |
| `list_files()` / `list_queue()` | read | `GET /api/files`, `GET /api/queue` |
| `pause_print(id)` / `resume_print(id)` | action | `POST /api/printers/<id>/pause|resume` |
| `cancel_print(id, confirm_id)` | dangerous | `POST /api/printers/<id>/cancel` with `{"confirm": id}`; MCP client must show a human confirmation |
| `queue_job(id, file)` | action | `POST /api/queue/add` (never starts the print: a person presses *Bed is clear, start*) |

Starting a print from MCP is deliberately **not** offered: a person must confirm the bed is clear.

## Resources and prompts

- Resource `printer://<id>/status` (subscribable, backed by the monitor's events).
- Resource `fleet://summary`: counts by state and location.
- Prompt `daily-report`: summarise finished, failed and running jobs per location.

## Open questions before building

- Token storage for the MCP account (DPAPI like other secrets).
- Whether `pause_print` should also require confirmation (it is reversible; proposed: no).
- Event streaming: Server-Sent Events from the dashboard would avoid polling.
