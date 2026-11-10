"""Outbound connection from one printer host to its Cloudflare relay.

A small status report goes up every few seconds. Camera pictures go up only while someone
watches on the cloud dashboard, over one WebSocket that stays open while they watch. A relay
without that live link (an older Worker) gets one upload per picture instead.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config, tailscale, wsclient
from .notify import USER_AGENT

log = logging.getLogger("dashboard.cloud")
FRAME_LIMIT = 1_000_000
FRAME_SECONDS = 1.0   # pictures per camera: at most one a second (0.5 s when only one camera is watched)
LINK_IDLE = 30        # keep the live link this long after the last viewer left
FRESH = 5             # never send a picture older than this many seconds


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward the host key to a redirect destination


def live_url(link):
    parts = urllib.parse.urlsplit(link)
    scheme = "ws" if parts.scheme == "http" else "wss"
    return urllib.parse.urlunsplit((scheme, parts.netloc, "/connector/live", "", ""))


def packet(printer_id, jpeg):
    """One camera picture on the live link: id length, id, JPEG."""
    name = printer_id.encode("ascii")
    return bytes([len(name)]) + name + jpeg


class CameraLink:
    """Sends the pictures the cloud asks for, while it asks, over one WebSocket."""

    def __init__(self, connector):
        self.connector = connector
        self.lock = threading.Lock()
        self.wanted = []
        self.wanted_at = 0.0
        self.thread = None
        self.live = False
        self.retry_at = 0.0  # set when the relay has no live link: use uploads until then

    def want(self, ids):
        with self.lock:
            self.wanted = list(ids)
            if ids:
                self.wanted_at = time.monotonic()
            idle = self.thread is None or not self.thread.is_alive()
            if ids and idle and time.monotonic() >= self.retry_at:
                self.thread = threading.Thread(target=self._run, daemon=True, name="cloud-cameras")
                self.thread.start()

    def _needed(self):
        with self.lock:
            return bool(self.wanted) or time.monotonic() - self.wanted_at < LINK_IDLE

    def _run(self):
        failures = 0
        while self._needed():
            settings = self.connector.app.cfg.get("cloud") or {}
            key = config.secret(settings, "key")
            if not settings.get("url") or not key:
                return
            try:
                ws = wsclient.WebSocket.connect(live_url(settings["url"]), {
                    "Authorization": "Bearer " + key, "User-Agent": USER_AGENT})
            except wsclient.HandshakeRefused as error:
                if error.status in (404, 426):
                    log.info("cloud relay has no live camera link yet; uploading pictures one by one")
                    with self.lock:
                        self.retry_at = time.monotonic() + 600
                    return
                log.warning("live camera link refused: HTTP %d", error.status)
            except (OSError, ValueError) as error:
                log.warning("live camera link failed: %s", type(error).__name__)
            else:
                failures = 0
                self.live = True
                log.info("live camera link open")
                try:
                    self._stream(ws)
                    log.info("live camera link closed: nobody is watching")
                    return  # want() starts a new link when someone does
                except (OSError, ValueError) as error:
                    log.warning("live camera link dropped: %s %s", type(error).__name__, error)
                except Exception:
                    log.exception("live camera link failed")
                finally:
                    self.live = False
                    ws.close()
            failures += 1
            time.sleep(min(30, 2 ** min(failures, 5)))

    def _stream(self, ws):
        sent = {}           # printer id -> (time of the picture, when it was sent)
        last_out = time.monotonic()
        while self._needed():
            for message in ws.poll(0.1):
                try:
                    cameras = json.loads(message).get("cameras") if isinstance(message, str) else None
                except (ValueError, AttributeError):
                    cameras = None
                if isinstance(cameras, list):
                    self.want([c for c in cameras if isinstance(c, str)][:8])
            with self.lock:
                wanted = list(self.wanted)
            hubs = self.connector.hubs()
            interval = FRAME_SECONDS / 2 if len(wanted) == 1 else FRAME_SECONDS
            for pid in wanted:
                hub = hubs.get(pid)
                if hub is None:
                    continue
                hub.touch()
                with hub.cond:
                    frame, taken = hub.frame, hub.frame_time
                now = time.monotonic()
                last_taken, last_sent = sent.get(pid, (0.0, 0.0))
                if (frame and taken > last_taken and now - last_sent >= interval and time.time() - taken < FRESH
                        and len(frame) <= FRAME_LIMIT and frame.startswith(b"\xff\xd8")):
                    ws.send(packet(pid, frame))
                    sent[pid] = (taken, now)
                    last_out = now
            if time.monotonic() - last_out > 25:  # Cloudflare closes links that stay silent
                ws.ping()
                last_out = time.monotonic()


class Connector:
    def __init__(self, app):
        self.app = app
        self.opener = urllib.request.build_opener(NoRedirect())
        self.session = uuid.uuid4().hex
        self.cursor = app.monitor.events_after(0)[1]
        self.link = None
        self.last_ok = 0
        self.detail = ""
        self.cameras = CameraLink(self)
        self._remote, self._remote_at = ("", set()), -60.0

    def remote(self):
        """(https://<this PC>.ts.net, published ports) when the dashboard is on Tailscale, else ("", set()).
        The cloud page links there; checked once a minute because it runs the Tailscale command."""
        if time.monotonic() - self._remote_at >= 60:
            self._remote_at = time.monotonic()
            try:
                st = tailscale.status()
                ports = set(tailscale.serve_targets()) if st.get("state") == "Running" and st.get("dns_name") else set()
                self._remote = ("https://" + st["dns_name"], ports) if 443 in ports else ("", set())
            except Exception as error:  # the status report must go out anyway
                log.info("Tailscale status for the cloud page unavailable: %s", error)
                self._remote = ("", set())
        return self._remote

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="cloud-connector").start()

    def hubs(self):
        with self.app.lock:
            return {pid: self.app.hubs[pid] for pid in list(self.app.printers)[:32] if pid in self.app.hubs}

    def request(self, link, key, body, camera=None):
        url = link + "/connector"
        # Cloudflare blocks Python's default User-Agent (error 1010).
        headers = {"Authorization": "Bearer " + key, "User-Agent": USER_AGENT}
        if camera is None:
            headers["Content-Type"] = "application/json"
            method = "POST"
        else:
            url += "?" + urllib.parse.urlencode({"camera": camera})
            headers["Content-Type"] = "image/jpeg"
            method = "PUT"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        # Generous: from some countries every request crosses an ocean to reach Cloudflare.
        with self.opener.open(request, timeout=10) as response:
            return json.loads(response.read(4096)) if camera is None else None

    @staticmethod
    def camera_error(hub):
        """Why a camera has no picture right now, for the cloud dashboard ('' when it has one)."""
        with hub.cond:
            if hub.error and time.time() - hub.frame_time > FRESH:
                return str(hub.error)[:300]
        return ""

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
        base, ports = self.remote()
        report = {
            "session": self.session,
            "printers": [
                dict({k: p[k] for k in ("id", "name", "type", "rotate", "flip_horizontal", "flip_vertical")},
                     status={k: (status.get(p["id"]) or {}).get(k) for k in ("state", "progress")},
                     camera_error=self.camera_error(hubs[p["id"]]),
                     # Tailscale addresses: they open only on the owner's own Tailscale devices.
                     remote=base,
                     mainsail="%s:%d/" % (base, p["ui_port"]) if base and p.get("ui_port") in ports else "")
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
        demand = [pid for pid in result.get("cameras", []) if isinstance(pid, str) and pid in hubs][:8]
        self.cameras.want(demand)
        if not self.cameras.live:
            self.upload(link, key, hubs, demand)
        return 3 if demand else 8

    def upload(self, link, key, hubs, demand):
        """One upload per picture: for a relay without the live link, or while that link connects."""
        for pid in demand:
            hub = hubs[pid]
            hub.touch()
            with hub.cond:
                frame = hub.frame if time.time() - hub.frame_time < FRESH and not hub.stopped else None
            if not frame or len(frame) > FRAME_LIMIT or not frame.startswith(b"\xff\xd8"):
                continue
            try:
                self.request(link, key, frame, pid)
            except Exception as error:  # one slow camera must not hold back the others
                log.info("camera picture for %s not uploaded: %s", pid, type(error).__name__)

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
                    log.warning("Cloud relay connection failed: %s %s", type(error).__name__,
                                getattr(error, "code", ""))
                    # A refused key will not fix itself; a slow or lost request is worth retrying soon.
                    delay = 8 if isinstance(error, urllib.error.HTTPError) else 3
            time.sleep(delay if link else 2)

    def status(self):
        return {"connected": self.link == self.app.cfg.get("cloud", {}).get("url")
                and self.last_ok > 0 and time.monotonic() - self.last_ok < 25,
                "detail": self.detail}
