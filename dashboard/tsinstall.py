"""Install Tailscale on Windows from inside OS_3DPMS.

Downloads the official MSI from pkgs.tailscale.com over HTTPS, checks that it is
signed by Tailscale Inc. (Windows Authenticode), and runs it silently. Windows shows
one "Allow changes?" (UAC) prompt, because Tailscale installs a system service.
"""
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import tailscale

log = logging.getLogger("os3dpms.tsinstall")

MSI_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest-%s.msi"
ALLOWED_HOST = "https://pkgs.tailscale.com/"
SIGNER_RE = re.compile(r"(^|,\s*)O=\"?Tailscale Inc\.\"?(,|$)")
MAX_BYTES = 300 * 1024 * 1024
UAC_DECLINED = 1223
OK_CODES = (0, 3010)  # 3010 = installed, restart recommended

PS_VERIFY = ("$s = Get-AuthenticodeSignature -LiteralPath $env:OS3DPMS_MSI; "
             "Write-Output ($s.Status.ToString() + '|' + $s.SignerCertificate.Subject)")
PS_INSTALL = ("try { $p = Start-Process -FilePath msiexec.exe -Verb RunAs -Wait -PassThru "
              "-ArgumentList @('/i', ('\"' + $env:OS3DPMS_MSI + '\"'), '/quiet', '/norestart'); "
              "exit $p.ExitCode } catch { exit %d }" % UAC_DECLINED)

BUSY = ("downloading", "verifying", "installing", "starting")

_state = {"state": "idle", "detail": ""}
_lock = threading.Lock()


def supported():
    return os.name == "nt"


def arch():
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    return "amd64"


def state():
    with _lock:
        return dict(_state)


def _set(name, detail=""):
    with _lock:
        _state.update(state=name, detail=detail)
    log.info("tailscale install: %s %s", name, detail)


def signer_ok(output):
    """True when Get-AuthenticodeSignature says Valid and the publisher is Tailscale Inc."""
    status, _, subject = (output or "").strip().partition("|")
    return status == "Valid" and bool(SIGNER_RE.search(subject))


def _powershell(script, msi, timeout):
    env = dict(os.environ, OS3DPMS_MSI=msi)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=env, timeout=timeout, creationflags=tailscale.NO_WINDOW)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def download(url, dest, opener=urllib.request.urlopen):
    with opener(url, timeout=60) as resp:
        final = resp.geturl()
        if not final.startswith(ALLOWED_HOST):
            raise RuntimeError("download was redirected away from pkgs.tailscale.com")
        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise RuntimeError("download is too large")
                f.write(chunk)
    return size


def _worker():
    tmp = tempfile.mkdtemp(prefix="os3dpms-ts-")
    msi = os.path.join(tmp, "tailscale-setup.msi")
    try:
        _set("downloading", "Downloading Tailscale from pkgs.tailscale.com…")
        download(MSI_URL % arch(), msi)

        _set("verifying", "Checking the Tailscale signature…")
        _, out = _powershell(PS_VERIFY, msi, 60)
        if not signer_ok(out):
            raise RuntimeError("the downloaded file is not signed by Tailscale Inc. Nothing was installed.")

        _set("installing", "Click Yes on the Windows permission prompt…")
        code, _ = _powershell(PS_INSTALL, msi, 900)
        if code == UAC_DECLINED:
            raise RuntimeError("Windows permission was declined. Click Install again and choose Yes.")
        if code not in OK_CODES:
            raise RuntimeError("the Tailscale installer stopped with code %d" % code)

        _set("starting", "Starting Tailscale…")
        deadline = time.time() + 60
        while time.time() < deadline:
            if tailscale.find_cli() and tailscale.status().get("state") not in ("Error", "NotInstalled"):
                break
            time.sleep(2)
        _set("done", "Tailscale is installed.")
    except Exception as e:  # report every failure to the setup page
        _set("failed", str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def start(background=True):
    """Begin installing. Returns a dict for the setup page."""
    if not supported():
        return {"ok": False, "error": "Automatic install works on Windows only.",
                "download_url": tailscale.DOWNLOAD_URL}
    if tailscale.find_cli():
        return {"ok": True, "already": True}
    with _lock:
        if _state["state"] in BUSY:
            return {"ok": True, "running": True}
        _state.update(state="downloading", detail="Starting…")
    if background:
        threading.Thread(target=_worker, name="tailscale-install", daemon=True).start()
    else:
        _worker()
    return {"ok": True}
