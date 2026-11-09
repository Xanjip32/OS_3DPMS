"""Require a login for Mainsail/Fluidd on your Klipper printers, without breaking OS_3DPMS.

Usage (on the printer PC, while the printers are idle):
    python tools\\secure_printers.py           secure every Klipper printer in config.json
    python tools\\secure_printers.py --undo    put each printer's old settings back

For each printer it:
 1. saves the printer's Moonraker API key in OS_3DPMS (encrypted), so status, cameras
    and uploads keep working,
 2. creates your login (asked once, typing hidden) unless that user already exists,
 3. trusts only the printer itself (KlipperScreen, OctoEverywhere) instead of the whole
    network, keeping the old file as moonraker.conf.before-login,
 4. restarts Moonraker, but never during a print.

Afterwards Mainsail asks for the login everywhere: on the Wi-Fi, through the dashboard's
Mainsail button and over Tailscale. Restart OS_3DPMS (stop.bat, then start) to use the keys.
"""
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dashboard import config, klipper  # noqa: E402

CONFIG = os.path.join(ROOT, "config.json")
BACKUP = "moonraker.conf.before-login"
LOCAL_ONLY = ("127.0.0.0/8", "::1/128")


def restrict_trusted(text):
    """moonraker.conf with trusted_clients limited to the printer itself, or None if it has
    no [authorization] section to change."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == "[authorization]"), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    new = ["trusted_clients:"] + ["    " + ip for ip in LOCAL_ONLY]
    for i in range(start + 1, end):
        if re.match(r"trusted_clients\s*[:=]", lines[i]):
            j = i + 1  # the value continues on indented (or blank) lines
            while j < end and (not lines[j].strip() or lines[j][0] in " \t"):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1  # keep the blank line before the next option
            lines[i:j] = new
            break
    else:
        lines[start + 1:start + 1] = new
    return "\n".join(lines) + "\n"


def call(p, path, key=None, method="GET", body=None, ctype="application/json"):
    req = urllib.request.Request(klipper.base_url(p).rstrip("/") + path, data=body, method=method)
    if key:
        req.add_header("X-Api-Key", key)
    if body is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(2_000_000)
    return json.loads(data.decode("utf-8")) if "json" in r.headers.get("Content-Type", "") else data


def read_config_file(p, key, name):
    try:
        return call(p, "/server/files/config/" + name, key).decode("utf-8")
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 404:
            return None
        raise


def write_config_file(p, key, name, text):
    boundary = uuid.uuid4().hex
    body = ("--%s\r\nContent-Disposition: form-data; name=\"root\"\r\n\r\nconfig\r\n"
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: text/plain\r\n\r\n" % (boundary, boundary, name)).encode() \
        + text.encode("utf-8") + ("\r\n--%s--\r\n" % boundary).encode()
    call(p, "/server/files/upload", key, "POST", body, "multipart/form-data; boundary=" + boundary)


def restart_when_idle(p, key):
    state = call(p, "/printer/objects/query?print_stats", key)["result"]["status"]["print_stats"]["state"]
    if state in ("printing", "paused"):
        print("   It is %s: restart Moonraker after the print (Mainsail: Machine > Services)." % state)
        return False
    call(p, "/server/restart", key, "POST", b"{}")
    return True


def check_login_required(p):
    """Wait up to 60 s for Moonraker to come back, then report whether it now asks for a login."""
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(4)
        try:
            call(p, "/printer/objects/query?print_stats")
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (401, 403):
                print("   Done: Mainsail now asks for the login.")
                return
        except OSError:
            pass  # still restarting
    print("   WARNING: could not confirm the login is required. Check it in Mainsail.")


def save_key(pid, key):
    raw = config.read_raw(CONFIG)
    for item in raw["printers"]:
        if item.get("id") == pid:
            item.pop("api_key_protected", None)
            item["api_key"] = key
    cfg = config.validate(raw)
    config.protect_secrets(cfg)
    config.save(CONFIG, cfg)


def secure(p, username, password):
    key = config.secret(p, "api_key")
    if not key:
        key = call(p, "/access/api_key")["result"]  # allowed while this PC is still trusted
        save_key(p["id"], key)
        print("   Saved its API key in OS_3DPMS.")
    users = [u.get("username") for u in call(p, "/access/users/list", key)["result"]["users"]]
    if username in users:
        print("   Login '%s' already exists: kept its password." % username)
    else:
        call(p, "/access/user", key, "POST", json.dumps({"username": username, "password": password}).encode())
        print("   Created login '%s'." % username)
    text = read_config_file(p, key, "moonraker.conf")
    new = restrict_trusted(text)
    if new is None:
        print("   moonraker.conf has no [authorization] section: not changed.")
        return
    if new != text:
        if read_config_file(p, key, BACKUP) is None:
            write_config_file(p, key, BACKUP, text)
        write_config_file(p, key, "moonraker.conf", new)
        print("   Only the printer itself is trusted now (old file: %s)." % BACKUP)
    if restart_when_idle(p, key):
        check_login_required(p)


def undo(p):
    key = config.secret(p, "api_key")
    old = read_config_file(p, key, BACKUP)
    if old is None:
        print("   No %s on this printer: nothing to undo." % BACKUP)
        return
    write_config_file(p, key, "moonraker.conf", old)
    print("   Old moonraker.conf restored.")
    restart_when_idle(p, key)


def main():
    printers = [p for p in config.load(CONFIG)["printers"] if p["type"] == "klipper"]
    if not printers:
        print("No Klipper printers in config.json.")
        return 1
    if "--undo" in sys.argv[1:]:
        for p in printers:
            print("%s (%s)" % (p["name"], p["host"]))
            undo(p)
        return 0
    print("Printers: " + ", ".join("%s (%s)" % (p["name"], p["host"]) for p in printers))
    try:
        username = input("Login name for Mainsail [admin]: ").strip() or "admin"
        password = getpass.getpass("Password (typing is hidden): ")
        again = getpass.getpass("Same password again: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Nothing changed.")
        return 1
    if len(password) < 8 or password != again:
        print("Passwords differ or are shorter than 8 characters. Nothing changed.")
        return 1
    failed = 0
    for p in printers:
        print("%s (%s)" % (p["name"], p["host"]))
        try:
            secure(p, username, password)
        except (OSError, ValueError, KeyError) as e:
            failed += 1
            print("   FAILED: %s. This printer was left as it was where possible." % e)
    print("\nRestart OS_3DPMS so it uses the saved API keys (stop.bat, then start).")
    print("Slicers that upload straight to a printer now need its Moonraker API key;")
    print("or send files with the dashboard's Send file button instead.")
    print("To undo: python tools\\secure_printers.py --undo")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
