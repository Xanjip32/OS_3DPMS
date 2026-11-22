"""Prusa printers (MK4, MK3.9, MINI, XL, Core One) through PrusaLink's local API v1.

Written against PrusaLink's published OpenAPI description (`/api/v1/...`, HTTP Digest login
with user `maker` and the password from the printer screen, or an API key on older builds).
Not yet tested on hardware: see docs/ADAPTERS.md for the safe test procedure.
"""
import logging
import urllib.parse

from .. import config
from .base import AdapterError, PrinterAdapter, PrinterBusy
from .http import Client, HTTPStatusError

log = logging.getLogger("dashboard.adapters.prusalink")

STATE_MAP = {"IDLE": "standby", "READY": "standby", "BUSY": "standby", "PRINTING": "printing",
             "PAUSED": "paused", "FINISHED": "complete", "STOPPED": "cancelled", "ERROR": "error",
             "ATTENTION": "paused"}


class PrusaLinkAdapter(PrinterAdapter):
    protocol = "prusalink"
    manufacturer = "Prusa"
    file_extensions = (".gcode", ".bgcode")
    supports = {"monitor": True, "can_upload": True, "can_start_print": True, "can_pause": True,
                "can_resume": True, "can_cancel": True}
    storage = "usb"

    def client(self, timeout=5):
        password = config.secret(self.p, "password")
        auth = ("digest", self.p.get("username") or "maker", password) if password else \
            ("apikey", config.secret(self.p, "api_key"))
        if getattr(self, "_client", None) is None or self._client.auth != auth:
            self._client = Client(self.p["host"], self.p.get("http_port", 80), auth, timeout)
        self._client.timeout = timeout
        return self._client

    def fetch_status(self):
        try:
            data = self.client(3).json("GET", "/api/v1/status") or {}
        except HTTPStatusError as e:
            if e.status == 401:
                raise AdapterError("PrusaLink refused the password") from None
            raise
        pr, job = data.get("printer") or {}, data.get("job") or {}
        raw = str(pr.get("state") or "").upper()
        state = STATE_MAP.get(raw, "standby")
        temps = {}
        if pr.get("temp_nozzle") is not None:
            temps["nozzle"] = {"actual": pr.get("temp_nozzle"), "target": pr.get("target_nozzle")}
        if pr.get("temp_bed") is not None:
            temps["bed"] = {"actual": pr.get("temp_bed"), "target": pr.get("target_bed")}
        pos = [pr.get("axis_x"), pr.get("axis_y"), pr.get("axis_z")]
        file = ""
        if job.get("id") and state in ("printing", "paused", "complete", "cancelled"):
            try:
                info = self.client(3).json("GET", "/api/v1/job") or {}
                file = (info.get("file") or {}).get("display_name") or (info.get("file") or {}).get("name") or ""
            except Exception:
                pass
        self.job_id = job.get("id")
        fan = pr.get("fan_print")
        return {
            "state": state, "file": file, "progress": (job.get("progress") or 0) / 100.0,
            "duration": job.get("time_printing") or 0, "remaining_s": job.get("time_remaining"),
            "message": "Printer needs attention" if raw == "ATTENTION" else ("Printer error" if raw == "ERROR" else ""),
            "errors": ["Printer error"] if raw == "ERROR" else [],
            "warnings": ["Printer needs attention on its screen"] if raw == "ATTENTION" else [],
            "temperatures": temps, "position": pos if None not in pos else None,
            "fan": (fan / 100.0) if isinstance(fan, (int, float)) and fan > 1 else fan, "transport": "http",
        }

    def _path(self, name):
        return "/api/v1/files/%s/%s" % (self.storage, urllib.parse.quote(name))

    def file_exists(self, name):
        try:
            status, _, _ = self.client().request("HEAD", self._path(name))
            if status == 401:
                status, _, _ = self.client().request("HEAD", self._path(name))
            return {200: True, 404: False}.get(status)
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
            raise AdapterError("PrusaLink takes %s files" % ", ".join(self.file_extensions))
        c = self.client(300)
        c.prime("/api/v1/status")
        headers = {"Content-Type": "application/octet-stream", "Overwrite-File": "?0",
                   "Print-After-Upload": "?1" if start else "?0"}
        status, _, body = c.request("PUT", self._path(name), headers=headers, stream_path=path)
        if status == 409:
            raise AdapterError("%s already has a file named %s" % (self.p["name"], name))
        if status in (401, 403):
            raise AdapterError("PrusaLink refused the password")
        if status not in (200, 201, 204):
            raise AdapterError("PrusaLink answered %d: %s" % (status, body[:200].decode("utf-8", "replace")))
        return "Uploaded to %s%s" % (self.p["name"], " and print started" if start else "")

    def start_print(self, filename):
        self.require("can_start_print")
        st = self.get_status()
        if st is None or st["state"] in ("printing", "paused", "error"):
            raise PrinterBusy("%s is %s" % (self.p["name"], st["state"] if st else "not answering"))
        self._call("POST", self._path(filename))
        return "Print of %s started on %s" % (filename, self.p["name"])

    def _call(self, method, uri):
        try:
            self.client(10).json(method, uri)
        except HTTPStatusError as e:
            if e.status == 409:
                raise PrinterBusy("PrusaLink refused: the printer is not in a state for this") from None
            raise AdapterError("PrusaLink answered %d" % e.status) from None

    def _job_uri(self, action=""):
        if self.get_status() is None or not getattr(self, "job_id", None):
            raise AdapterError("%s has no running job" % self.p["name"])
        return "/api/v1/job/%s%s" % (int(self.job_id), action)

    def pause_print(self):
        self.require("can_pause")
        self._call("PUT", self._job_uri("/pause"))
        return "%s paused" % self.p["name"]

    def resume_print(self):
        self.require("can_resume")
        self._call("PUT", self._job_uri("/resume"))
        return "%s resumed" % self.p["name"]

    def cancel_print(self):
        self.require("can_cancel")
        self._call("DELETE", self._job_uri())
        return "Print on %s stopped" % self.p["name"]

    def diagnostics(self):
        d = super().diagnostics()
        try:
            d["version"] = self.client(3).json("GET", "/api/version")
        except Exception as e:
            d["version_error"] = str(e)[:120]
        return d
