# Remote access

OS_3DPMS has **no custom VPN and opens no ports**. Every listener binds to `127.0.0.1`.
[Tailscale](https://tailscale.com) is the supported way in from outside, and it is optional.

## The three pieces

```
 phone / laptop  ──Tailscale (WireGuard)──►  printer-room PC running OS_3DPMS  ──LAN──►  printers
   (anywhere)          HTTPS *.ts.net             127.0.0.1:8080 dashboard                (never exposed)
                                                  127.0.0.1:8079 Setup (never published)
```

| Where you are | How you reach the dashboard | Printers |
|---|---|---|
| On the printer-room PC | `http://127.0.0.1:8080/` (dashboard), `http://127.0.0.1:8079/setup` | reached directly by the PC |
| Anywhere, with Tailscale | `https://<pc>.<tailnet>.ts.net/` published by `tailscale serve` (Setup → Publish) | reached **through** the PC; printers never get a public or tailnet address |
| Without Tailscale | not reachable (by design) | – |

Printers only ever talk to the OS_3DPMS PC on the local network. Do **not** port-forward printer
ports (80, 7125, 8883, 990, 6000) or OS_3DPMS itself, and never use Tailscale Funnel.

## Who may do what

1. **Tailscale identity** (default `auth.mode: tailscale`): `tailscale serve` adds the signed-in
   account; `allowed_users` restricts who gets in, `read_only_users` may watch but not upload or
   control.
2. **Password login** (`auth.mode: password` or `tailscale+password`): dashboard accounts created
   with `py run.py --add-user NAME --role admin|operator|viewer`. Passwords are PBKDF2-SHA256
   hashed (310 000 iterations), sessions are HttpOnly + SameSite=Strict cookies (Secure on
   `*.ts.net`), at most 5 login attempts per 15 minutes per source and account, and 20 per account in total, then
   a wait of up to 15 minutes.
   `tailscale+password` requires both an allowed Tailscale account **and** a password.
3. **Setup page** (8079) is only for the PC itself and is never published.
4. Changing printers remotely needs an account in *Allowed Tailscale accounts*. Dangerous actions
   (cancel, start) need an explicit confirmation; every control, upload and config change is in
   `audit.log`.

## What happens when something is down

| Outage | Effect |
|---|---|
| Internet down | Dashboard on the PC keeps working; remote access and phone notifications stop until it is back. Printers keep printing. |
| Tailscale down / logged out | Remote access stops. Local dashboard and printers unaffected. |
| OS_3DPMS PC off or app stopped | No dashboard, no notifications, no queue. **Printers keep printing**: OS_3DPMS never streams G-code to a printer (files are uploaded to the printer first, and Marlin serial is monitor-only). |
| One printer offline | Only its tile goes offline; the monitor backs off on it (up to 60 s) and keeps polling the others concurrently. A *reconnected* notification comes when it is back. |
| Printer firmware refuses a command (Bambu authorization) | The refusal is shown; nothing is retried in a loop. |

## Remote server (optional)

If the printers are in one building and you want OS_3DPMS on another machine, run OS_3DPMS on a
small PC **in the printer room** (it needs LAN access to the printers) and reach it through
Tailscale. Running it in the cloud and connecting printers to it is not supported: it would mean
exposing printers or building a VPN, which this project deliberately avoids.
