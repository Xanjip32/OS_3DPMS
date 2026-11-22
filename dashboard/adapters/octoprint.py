"""Marlin printers through OctoPrint's REST API (the recommended way to add Marlin printers).

Target: Anycubic Kossel Linear Plus and other Marlin printers attached to OctoPrint.
OctoPrint is optional for OS_3DPMS as a whole: it is only this adapter's transport.
Status is polled over REST every monitor cycle (OctoPrint's push channel is SockJS, which
would need an extra client; a 5 s poll of one local server is cheap).
"""
import logging
import urllib.parse

from .. import cameras, config, klipper
from .base import AdapterError, PrinterAdapter, PrinterBusy
from .http import Client, HTTPStatusError, multipart

log = logging.getLogger("dashboard.adapters.octoprint")


def state_from(printer, job):
    """Normalized state from /api/printer and /api/job answers (printer None: 409, not connected)."""
    if printer is None:
        return "offline", "OctoPrint is not connected to the printer"
    flags = ((printer or {}).get("state") or {}).get("flags") or {}
    text = ((printer or {}).get("state") or {}).get("text") or (job or {}).get("state") or ""
    completion = ((job or {}).get("progress") or {}).get("completion")
    if flags.get("error") or (flags.get("closedOrError") and "error" in text.lower()):
        return "error", text
    if flags.get("paused") or flags.get("pausing"):
        return "paused", text
    if flags.get("printing") or flags.get("cancelling") or flags.get("finishing"):
        return "printing", text
    if flags.get("operational"):
        if completion is not None and completion >= 100:
            return "complete", ""
        return "standby", ""
    return "offline", text or "OctoPrint is not connected to the printer"


class OctoPrintAdapter(PrinterAdapter):
    protocol = "octoprint"
    manufacturer = "Marlin"
    supports = {"monitor": True, "can_upload": True, "can_start_print": True, "can_pause": True,
                "can_resume": True, "can_cancel": True, "can_list_files": True, "has_camera": True,
                "can_control_temperature": True}

    def client(self, timeout=5):
        return Client(self.p["host"], self.p.get("http_port", 80),
                      ("apikey", config.secret(self.p, "api_key")), timeout)

    def fetch_status(self):
        c = self.client(3)
        try:
            printer = c.json("GET", "/api/printer")
        except HTTPStatusError as e:
            if e.status == 409:  # OctoPrint up, printer not connected
                printer = None
            elif e.status in (401, 403):
                raise AdapterError("OctoPrint refused the API key") from None
            else:
                raise
        job = c.json("GET", "/api/job")
        state, msg = state_from(printer, job)
        temps = {}
        for name, t in ((printer or {}).get("temperature") or {}).items():
            if isinstance(t, dict) and name != "history":
                label = {"tool0": "nozzle", "bed": "bed", "chamber": "chamber"}.get(name, name)
                temps[label] = {"actual": t.get("actual"), "target": t.get("target")}
        prog = (job or {}).get("progress") or {}
        file = (((job or {}).get("job") or {}).get("file") or {}).get("name") or ""
        return {
            "state": state, "file": file, "progress": (prog.get("completion") or 0) / 100.0,
            "duration": prog.get("printTime") or 0,
            "remaining_s": prog.get("printTimeLeft") if state in ("printing", "paused") else None,
            "message": msg if state in ("error", "offline") else "",
            "errors": [msg] if state == "error" and msg else [],
            "temperatures": temps, "transport": "http",
        }

    def file_exists(self, name):
        try:
            self.client().json("GET", "/api/files/local/" + urllib.parse.quote(name))
            return True
        except HTTPStatusError as e:
            return False if e.status == 404 else None
        except OSError:
            return None

    def upload_file(self, path, name, start=False):
        self.require("can_upload")
        if start:
            self.require("can_start_print")
            st = self.get_status()
            if st and st["state"] in ("printing", "paused"):
                raise PrinterBusy("%s is %s" % (self.p["name"], st["state"]))
        if not self.accepts_file(name):
            raise AdapterError("OctoPrint prints %s files" % ", ".join(self.file_extensions))
        fields = {"select": "true", "print": "true"} if start else {}
        boundary, head, tail = multipart(fields, "file", name)
        status, _, body = self.client(120).request(
            "POST", "/api/files/local", headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
            stream_path=path, prefix=head, suffix=tail)
        if status in (401, 403):
            raise AdapterError("OctoPrint refused the API key")
        if status not in (200, 201):
            raise AdapterError("OctoPrint answered %d: %s" % (status, body[:200].decode("utf-8", "replace")))
        return "Uploaded to %s%s" % (self.p["name"], " and print started" if start else "")

    def _job(self, payload):
        try:
            self.client(10).json("POST", "/api/job", payload)
        except HTTPStatusError as e:
            if e.status == 409:
                raise PrinterBusy("OctoPrint refused: the printer is not in a state for this") from None
            raise AdapterError("OctoPrint answered %d" % e.status) from None

    def start_print(self, filename):
        self.require("can_start_print")
        st = self.get_status()
        if st is None or st["state"] in ("printing", "paused", "offline", "error"):
            raise PrinterBusy("%s is %s" % (self.p["name"], st["state"] if st else "not answering"))
        try:
            self.client(10).json("POST", "/api/files/local/" + urllib.parse.quote(filename),
                                 {"command": "select", "print": True})
        except HTTPStatusError as e:
            raise AdapterError("OctoPrint answered %d" % e.status) from None
        return "Print of %s started on %s" % (filename, self.p["name"])

    def pause_print(self):
        self.require("can_pause")
        self._job({"command": "pause", "action": "pause"})
        return "%s paused" % self.p["name"]

    def resume_print(self):
        self.require("can_resume")
        self._job({"command": "pause", "action": "resume"})
        return "%s resumed" % self.p["name"]

    def cancel_print(self):
        self.require("can_cancel")
        self._job({"command": "cancel"})
        return "Print on %s cancelled" % self.p["name"]

    def list_files(self):
        data = self.client().json("GET", "/api/files/local?recursive=true") or {}
        out = []
        for f in data.get("files") or []:
            if f.get("type") == "machinecode":
                out.append({"name": f.get("path") or f.get("name"), "size": f.get("size"), "modified": f.get("date")})
        return out

    def webcam_urls(self):
        p = self.p
        if p.get("webcam_url"):
            return klipper.webcam_urls(p)
        urls = []
        try:
            cam = (self.client(3).json("GET", "/api/settings") or {}).get("webcam") or {}
            urls += [cam[k] for k in ("streamUrl", "snapshotUrl") if isinstance(cam.get(k), str) and cam[k]]
        except Exception as e:
            log.info("octoprint %s webcam settings: %s", p["id"], e)
        urls += ["/webcam/?action=stream", "/webcam/?action=snapshot"]
        base = klipper.base_url(p)
        out = []
        for u in urls:
            u = klipper.on_printer(urllib.parse.urljoin(base, u), p)  # never another host
            if u and u not in out:
                out.append(u)
        return out

    def camera_source(self):
        return lambda hub: cameras.klipper_camera(hub, self.webcam_urls)

    def diagnostics(self):
        d = super().diagnostics()
        try:
            d["version"] = self.client(3).json("GET", "/api/version")
            d["connection"] = ((self.client(3).json("GET", "/api/connection") or {}).get("current") or {})
        except Exception as e:
            d["version_error"] = str(e)[:120]
        return d
