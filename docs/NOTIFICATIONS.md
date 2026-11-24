# Notifications

Events (choose them in Setup → Phone notifications):

| Event | When | Default |
|---|---|---|
| `started` | a print starts | off |
| `complete` | a print finishes | on |
| `error` | a print fails or the printer goes into an error state (for example Klipper shutdown) | on |
| `paused` | a print pauses (also filament runout on printers that pause for it) | on |
| `cancelled` | a print is cancelled | off |
| `offline` | a printer goes offline while printing | on |
| `reconnected` | an offline printer is reachable again | on |
| `warning` | the printer reports a new warning or error code while not in an error state (Bambu HMS codes, filament / AMS warnings); each one once | on |

Temperature runaway is detected and handled by the printer firmware (Klipper `verify_heater`,
Marlin thermal protection, Bambu HMS); OpenPrintDeck shows the resulting shutdown / error and notifies.

Channels: dashboard pop-ups, **ntfy**, **Discord**, **Telegram** (Setup page) and **e-mail**
(config file only). At most 20 messages a minute.

## E-mail (SMTP)

Add to `"notifications"` in `config.json` and restart OpenPrintDeck (Setup keeps these values when you
save other notification settings):

```json
"notifications": {
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "smtp_security": "starttls",
  "smtp_user": "printers@example.com",
  "smtp_password": "app-password-here",
  "smtp_from": "printers@example.com",
  "smtp_to": ["me@example.com"]
}
```

- `smtp_security`: `starttls` (port 587, default), `ssl` (port 465), or `none` only for a relay on
  this PC (`localhost`). Certificates are verified.
- `smtp_password` is encrypted with DPAPI on the next start (Windows); on Linux/macOS use
  `"smtp_password_env": "SMTP_PASSWORD"`. Use an app password, not your main password.
- `smtp_to`: up to 10 addresses. Use Setup → **Send test** to check.
