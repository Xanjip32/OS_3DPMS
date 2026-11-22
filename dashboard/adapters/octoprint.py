"""Marlin printers through OctoPrint (the recommended way to add a Marlin printer).

A Marlin printer that is plugged into a Raspberry Pi (or PC) running OctoPrint is added as type
"octoprint" (*Marlin through OctoPrint*), not as a USB printer: OctoPrint owns the serial port.
Target: Anycubic Kossel Linear Plus and other Marlin printers attached to OctoPrint.
OctoPrint is optional for OS_3DPMS as a whole: it is only this adapter's transport.

Status comes from OctoPrint's push channel when it works: the raw WebSocket behind SockJS at
/sockjs/websocket, signed in with a passive /api/login using the API key. OctoPrint sends the
printer state, job, progress and temperatures about once a second. When the WebSocket is off
("websocket": false), refused, or quiet for too long, status is polled over REST as before.
"""
import json
import logging
import socket
import threading
import time
import urllib.parse

from .. import cameras, config, klipper, wsclient
from .base import AdapterError, PrinterAdapter, PrinterBusy
from .http import Client, HTTPStatusError, multipart

log = logging.getLogger("dashboard.adapters.octoprint")

WS_PATH = "/sockjs/websocket"
WS_STALE_S = 15         # no message for this long: use REST until the WebSocket talks again
WS_PING_S = 10
WS_THROTTLE = 2         # OctoPrint sends "current" every 0.5 s x throttle
MAX_BACKOFF_S = 60


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

    def __init__(self, printer, pins=None):
        super().__init__(printer, pins)
        self.stopped = threading.Event()
        self.thread = None
        self.ws = None
        self.ws_connected = False
        self.ws_error = ""
        self.ws_last = 0.0
        self.reconnects = 0
        self.cache = {}        # last "current" from the WebSocket: state, job, progress, temps

    # ------------------------------------------------------------ push (WebSocket)

    def connect(self):
        if self.p.get("websocket", True) and self.thread is None:
            self.thread = threading.Thread(target=self._ws_loop, daemon=True, name="octoprint-%s" % self.p["id"])
            self.thread.start()

    def disconnect(self):
        self.stopped.set()
        ws = self.ws
        if ws is not None:
            ws.close()

    def ws_url(self):
        port = self.p.get("http_port", 80)
        host = "[%s]" % self.p["host"] if ":" in self.p["host"] else self.p["host"]
        return "ws://%s%s%s" % (host, "" if port == 80 else ":%d" % port, WS_PATH)

    def _ws_loop(self):
        backoff = 1.0
        while not self.stopped.is_set():
            started = time.time()
            try:
                self._ws_session()
            except Exception as e:
                self.ws_error = str(e)[:200] or type(e).__name__
                log.debug("octoprint %s websocket: %s", self.p["id"], self.ws_error)
            finally:
                self.ws_connected = False
                ws, self.ws = self.ws, None
                if ws is not None:
                    ws.close()
            if self.stopped.is_set():
                return
            if time.time() - started > 60:
                backoff = 1.0
            self.reconnects += 1
            self.stopped.wait(backoff)
            backoff = min(MAX_BACKOFF_S, backoff * 2)

    def _ws_session(self):
        # A WebSocket cannot send X-Api-Key: a passive login with the key gives a session for "auth".
        try:
            user = self.client(5).json("POST", "/api/login", {"passive": True}) or {}
        except HTTPStatusError as e:
            raise AdapterError("OctoPrint login for the push channel: HTTP %d" % e.status) from None
        name, session = user.get("name"), user.get("session")
        if not name or not session:
            raise AdapterError("OctoPrint login gave no session (is the API key a user's application key?)")
        ws = wsclient.connect(self.ws_url(), timeout=5)
        self.ws = ws
        ws.sock.settimeout(1.0)
        ws.send_text(json.dumps({"auth": "%s:%s" % (name, session)}))
        ws.send_text(json.dumps({"throttle": WS_THROTTLE}))
        pinged = time.time()
        while not self.stopped.is_set():
            now = time.time()
            if now - pinged >= WS_PING_S:
                ws.ping()
                pinged = now
            if self.ws_last and now - self.ws_last > 4 * WS_STALE_S:
                raise TimeoutError("OctoPrint push channel went quiet")
            try:
                raw = ws.recv()
            except socket.timeout:
                continue
            if raw is None:      # pong
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict):
                self._on_message(data)

    def _on_message(self, data):
        if "reauthRequired" in data:
            raise AdapterError("OctoPrint asked to sign in again")
        current = data.get("current") or data.get("history")
        if not isinstance(current, dict):
            if data:
                self.ws_last = time.time()   # connected / event / plugin messages: the channel is alive
            return
        with self._lock:
            for key in ("state", "job", "progress"):
                if isinstance(current.get(key), dict):
                    self.cache[key] = current[key]
            temps = current.get("temps")
            if isinstance(temps, list) and temps and isinstance(temps[-1], dict):
                self.cache["temps"] = temps[-1]   # the newest sample; "current" often has none
        self.ws_last = time.time()
        self.ws_connected = True

    def ws_fresh(self):
        return self.ws_connected and time.time() - self.ws_last < WS_STALE_S and "state" in self.cache

    # ------------------------------------------------------------ monitoring

    def fetch_status(self):
        if self.ws_fresh():
            with self._lock:
                c = json.loads(json.dumps(self.cache))
            temps = {k: v for k, v in (c.get("temps") or {}).items() if k != "time"}
            printer = {"state": c["state"], "temperature": temps}
            if not (c["state"].get("flags") or {}).get("operational") and "offline" in \
                    (c["state"].get("text") or "").lower():
                printer = None
            job = {"job": c.get("job") or {}, "progress": c.get("progress") or {},
                   "state": c["state"].get("text")}
            return self._status(printer, job, "websocket")
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
        return self._status(printer, job, "http")

    @staticmethod
    def _status(printer, job, transport):
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
            "temperatures": temps, "transport": transport,
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
        d.update({"websocket": self.ws_fresh(), "websocket_error": self.ws_error, "reconnects": self.reconnects})
        return d
