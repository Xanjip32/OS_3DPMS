"""Outbound connection from one printer host to its Cloudflare relay."""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
import uuid

from . import config

log = logging.getLogger("dashboard.cloud")
FRAME_LIMIT = 1_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward the host key to a redirect destination


class Connector:
    def __init__(self, app):
        self.app = app
        self.opener = urllib.request.build_opener(NoRedirect())
        self.session = uuid.uuid4().hex
        self.cursor = app.monitor.events_after(0)[1]
        self.link = None
        self.last_ok = 0
        self.detail = ""

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="cloud-connector").start()

    def request(self, link, key, body, camera=None):
        url = link + "/connector"
        headers = {"Authorization": "Bearer " + key}
        if camera is None:
            headers["Content-Type"] = "application/json"
            method = "POST"
        else:
            url += "?" + urllib.parse.urlencode({"camera": camera})
            headers["Content-Type"] = "image/jpeg"
            method = "PUT"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=4) as response:
            return json.loads(response.read(4096)) if camera is None else None

    def sync(self, settings):
        app = self.app
        link = settings["url"]
        key = config.secret(settings, "key")
        events, last = app.monitor.events_after(self.cursor)
        with app.lock:
            printers = list(app.printers.values())[:32]
            hubs = {p["id"]: app.hubs[p["id"]] for p in printers}
        status = app.monitor.status
        event_settings = app.cfg["notifications"]["events"]
        report = {
            "session": self.session,
            "printers": [
                dict({k: p[k] for k in ("id", "name", "type", "rotate", "flip_horizontal", "flip_vertical")},
                     status={k: (status.get(p["id"]) or {}).get(k) for k in ("state", "progress")})
                for p in printers
            ],
            "events": [
                {k: e[k] for k in ("id", "name", "text")}
                for e in events[-20:] if e["kind"] == "test" or event_settings.get(e["kind"])
            ],
        }
        result = self.request(link, key, json.dumps(report).encode())
        self.cursor = last
        self.last_ok = time.monotonic()
        self.detail = ""
        demand = [pid for pid in result.get("cameras", []) if pid in hubs][:8]
        for pid in demand:
            hub = hubs[pid]
            hub.touch()
            with hub.cond:
                frame = hub.frame if time.time() - hub.frame_time < 5 and not hub.stopped else None
            if frame and len(frame) <= FRAME_LIMIT and frame.startswith(b"\xff\xd8"):
                self.request(link, key, frame, pid)
        return 3 if demand else 8

    def run(self):
        while True:
            settings = self.app.cfg.get("cloud") or {}
            link = settings.get("url")
            if link != self.link:
                self.link = link
                self.last_ok = 0
                self.detail = ""
                self.cursor = self.app.monitor.events_after(0)[1]
            delay = 8
            if link:
                try:
                    delay = self.sync(settings)
                except Exception as error:
                    self.detail = "Check the dashboard address and connection key."
                    log.warning("Cloud relay connection failed: %s", type(error).__name__)
            time.sleep(delay if link else 2)

    def status(self):
        return {"connected": self.link == self.app.cfg.get("cloud", {}).get("url")
                and self.last_ok > 0 and time.monotonic() - self.last_ok < 25,
                "detail": self.detail}
