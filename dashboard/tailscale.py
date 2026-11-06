"""Talk to the local Tailscale client: status, login and `tailscale serve`."""
import json
import os
import re
import shutil
import subprocess
import threading
import time

URL_RE = re.compile(r"https://[^\s\"'<>]+")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DOWNLOAD_URL = "https://tailscale.com/download"

CANDIDATES = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
]


def find_cli():
    found = shutil.which("tailscale")
    if found:
        return found
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _run(args, timeout=15):
    """Run the CLI. Returns (exit code, output, timed_out)."""
    cli = find_cli()
    if not cli:
        return None, "Tailscale is not installed", False
    proc = subprocess.Popen([cli] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        out, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        timed_out = True
    return proc.returncode, out.decode("utf-8", "replace"), timed_out


def status():
    if not find_cli():
        return {"installed": False, "state": "NotInstalled", "download_url": DOWNLOAD_URL}
    code, out, _ = _run(["status", "--json"], 10)
    try:
        data = json.loads(out)
    except ValueError:
        return {"installed": True, "state": "Error", "detail": out.strip()[:300]}
    me = data.get("Self") or {}
    user = ""
    uid = me.get("UserID")
    if uid is not None:
        user = ((data.get("User") or {}).get(str(uid)) or {}).get("LoginName", "")
    return {
        "installed": True,
        "state": data.get("BackendState", "Unknown"),
        "dns_name": (me.get("DNSName") or "").rstrip("."),
        "auth_url": data.get("AuthURL") or "",
        "user": user,
        "version": data.get("Version", ""),
    }


_login_lock = threading.Lock()


def start_login(wait=20):
    """Start `tailscale login` and return the sign-in URL to open."""
    with _login_lock:
        st = status()
        if not st["installed"]:
            return {"ok": False, "error": "Tailscale is not installed"}
        if st["state"] == "Running":
            return {"ok": True, "already": True}
        if st.get("auth_url"):
            return {"ok": True, "auth_url": st["auth_url"]}

        proc = subprocess.Popen([find_cli(), "login"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
        found = {"url": ""}

        def reader():
            for raw in proc.stdout:
                m = URL_RE.search(raw.decode("utf-8", "replace"))
                if m and "tailscale.com" in m.group(0) and not found["url"]:
                    found["url"] = m.group(0)

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + wait
        while time.time() < deadline and not found["url"]:
            st = status()
            if st["state"] == "Running":
                return {"ok": True, "already": True}
            if st.get("auth_url"):
                found["url"] = st["auth_url"]
                break
            if proc.poll() is not None and not found["url"]:
                time.sleep(0.5)
                break
            time.sleep(0.5)
        if found["url"]:
            return {"ok": True, "auth_url": found["url"]}
        return {"ok": False, "error": "Tailscale did not give a sign-in link. Open the Tailscale app and log in there."}


def serve_targets():
    """Map of published HTTPS port -> local target, from `tailscale serve status`."""
    code, out, _ = _run(["serve", "status", "--json"], 10)
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return {}
    targets = {}
    for hostport, web in (data.get("Web") or {}).items():
        try:
            port = int(hostport.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        handler = ((web or {}).get("Handlers") or {}).get("/") or {}
        targets[port] = handler.get("Proxy", "")
    return targets


def publish(mappings):
    """Publish each (https_port, local_port) on the tailnet. Stops at the first step that needs the user."""
    results = []
    for https_port, local_port in mappings:
        code, out, timed_out = _run(
            ["serve", "--bg", "--https=%d" % https_port, "http://127.0.0.1:%d" % local_port], 20)
        if code == 0 and not timed_out:
            results.append({"port": https_port, "ok": True})
            continue
        action = ""
        for url in URL_RE.findall(out):
            if "login.tailscale.com" in url:
                action = url
                break
        results.append({"port": https_port, "ok": False, "action_url": action,
                        "detail": out.strip()[-300:]})
        break
    if os.name == "nt":
        _run(["set", "--unattended=true"], 10)  # keep working when nobody is logged in
    return results
