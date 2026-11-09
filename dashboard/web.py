"""HTTP server.

Two listeners, both on 127.0.0.1 only:

* public (default 8080): published to the tailnet with `tailscale serve`.
  Every request must carry the Tailscale identity header that
  `tailscale serve` adds, so only signed-in tailnet users get in.
* admin (default 8079): never published. Setup page for the person sitting
  at this PC (install/login Tailscale, add printers, publish).
"""
import argparse
import atexit
import datetime
import json
import logging
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

from . import __version__, bambu, cameras, cloud, config, discover, klipper, monitor, notify, tailscale, tsinstall
from .forward import Forwarder, local_or_tailnet
from .tlspin import CertificateChanged, PinStore

log = logging.getLogger("dashboard")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
HTML, JS, CSS = "text/html; charset=utf-8", "text/javascript; charset=utf-8", "text/css; charset=utf-8"
PUBLIC_FILES = {
    "/": ("index.html", HTML), "/index.html": ("index.html", HTML),
    "/app.js": ("app.js", JS), "/app.css": ("app.css", CSS),
    "/sw.js": ("sw.js", JS), "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}
ADMIN_FILES = {"/setup": ("setup.html", HTML), "/setup.js": ("setup.js", JS)}
UPLOAD_EXTENSIONS = (".gcode", ".gco", ".g", ".3mf")
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost"}
MAX_JSON_BODY = 64 * 1024
MAX_CONNECTIONS = 100
CSP = ("default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
EDITABLE_FIELDS = ("id", "name", "type", "host", "ui_port", "http_port", "webcam_url", "rotate",
                   "flip_horizontal", "flip_vertical", "serial", "use_ams")


IMPORT_FIELDS = EDITABLE_FIELDS + ("access_code", "api_key")
# Where the older "printer-dashboard" prototype keeps its settings.
LEGACY_CONFIGS = (
    os.path.join(os.path.dirname(ROOT), "printer-dashboard", "config.json"),
    os.path.join(os.path.expanduser("~"), "Desktop", "printer-dashboard", "config.json"),
)


def legacy_printers():
    """(path, printers) from the old printer-dashboard config, or (None, []) when there is none."""
    for path in LEGACY_CONFIGS:
        if not os.path.isfile(path):
            continue
        try:
            raw = config.read_raw(path)
        except (OSError, config.ConfigError):
            continue
        printers = [p for p in raw.get("printers") or [] if isinstance(p, dict)
                    and p.get("type") in ("klipper", "bambu") and isinstance(p.get("host"), str) and p["host"]]
        if printers:
            return path, printers[:32]
    return None, []


def safe_filename(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\-() ]+", "_", name).lstrip(". ")
    return name[:200]


def _normalize_host(value):
    value = (value or "").strip().lower()
    for suffix in (":443", ":80"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value


class App:
    """Shared state: config, cameras, Mainsail forwards, audit log."""

    def __init__(self, config_path, data_dir):
        self.config_path = config_path
        self.data_dir = data_dir
        self.tmp_dir = os.path.join(data_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)
        for leftover in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, leftover))
            except OSError:
                pass
        self.pins = PinStore(os.path.join(data_dir, "pins.json"))
        self.audit_log = _audit_logger(data_dir)
        self.lock = threading.RLock()
        self.cfg = None
        self.printers = {}
        self.hubs = {}
        self.upload_locks = {}
        self.forwarders = []
        self.bambu_watchers = {}
        self.monitor = None
        self.connector = None
        self.notifier = notify.Notifier(self)
        self.bambu_listener = discover.BambuListener()
        self.bambu_listener.callbacks.append(self._bambu_seen)
        self.reload()
        self.monitor = monitor.Monitor(self)
        self.monitor.start()

    def reload(self):
        cfg = config.load(self.config_path)
        with self.lock:
            for fw in self.forwarders:
                fw.stop()
            for hub in self.hubs.values():
                hub.stop()
            for watcher in self.bambu_watchers.values():
                watcher.stop()
            self.bambu_watchers = {}
            for p in cfg["printers"]:
                if p["type"] == "bambu" and p.get("serial"):
                    watcher = bambu.BambuWatcher(p, self.pins)
                    watcher.start()
                    self.bambu_watchers[p["id"]] = watcher
            self.cfg = cfg
            self.printers = {p["id"]: p for p in cfg["printers"]}
            self.hubs = {pid: cameras.CameraHub(pid, self._camera_source(p))
                         for pid, p in self.printers.items()}
            self.upload_locks = {pid: threading.Lock() for pid in self.printers}
            if any(p["type"] == "bambu" and not p.get("serial") for p in cfg["printers"]):
                self.bambu_listener.listen(600)  # learn the serial number from the printer itself
            self.forwarders = []
            for p in cfg["printers"]:
                if p["type"] == "klipper" and p.get("ui_port"):
                    fw = Forwarder(p["ui_port"], p["host"], p.get("http_port", 80))
                    try:
                        fw.start()
                        self.forwarders.append(fw)
                    except OSError as e:
                        log.error("cannot open port %d for %s: %s", p["ui_port"], p["id"], e)

    def _bambu_seen(self, info):
        """A Bambu printer announced itself: fill in a missing serial number and start live status."""
        with self.lock:
            for p in self.cfg["printers"]:
                if p["type"] != "bambu" or p.get("serial") or p["host"] != info["host"]:
                    continue
                p["serial"] = info["serial"]
                if p["id"] not in self.bambu_watchers:
                    watcher = bambu.BambuWatcher(p, self.pins)
                    watcher.start()
                    self.bambu_watchers[p["id"]] = watcher
                log.info("%s: serial number %s found on the network", p["id"], info["serial"])
                try:
                    raw = config.read_raw(self.config_path)
                    for item in raw.get("printers", []):
                        if isinstance(item, dict) and item.get("id") == p["id"] and not item.get("serial"):
                            item["serial"] = info["serial"]
                    config.save(self.config_path, config.validate(raw))
                except (OSError, config.ConfigError) as e:
                    log.warning("could not save the serial number of %s: %s", p["id"], e)

    def _camera_source(self, p):
        if p["type"] == "bambu" and not p.get("webcam_url"):
            return lambda hub: cameras.bambu_camera(hub, p["host"], config.secret(p, "access_code"), self.pins)
        return lambda hub: cameras.klipper_camera(hub, lambda: klipper.webcam_urls(p))

    def publish_mappings(self):
        maps = [(443, self.cfg["port"])]
        maps += [(p["ui_port"], p["ui_port"]) for p in self.cfg["printers"] if p.get("ui_port")]
        return maps

    def audit(self, user, action, **fields):
        entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
                 "user": user, "action": action}
        entry.update(fields)
        self.audit_log.info(json.dumps(entry))

    def setup_config(self):
        """Config for the setup page, without secrets."""
        printers = []
        for p in self.cfg["printers"]:
            item = {k: p[k] for k in EDITABLE_FIELDS if k in p}
            item["has_access_code"] = bool(p.get("access_code") or p.get("access_code_protected"))
            item["has_api_key"] = bool(p.get("api_key") or p.get("api_key_protected"))
            printers.append(item)
        n = self.cfg["notifications"]
        notifications = {
            "events": n["events"], "snapshot": n["snapshot"],
            "telegram_chat_id": n.get("telegram_chat_id", ""),
        }
        for field in config.NOTIFY_SECRETS:
            notifications["has_" + field] = bool(n.get(field) or n.get(field + "_protected"))
        remote = self.cfg["cloud"]
        _, old = legacy_printers()
        return {"allowed_users": self.cfg["allowed_users"], "printers": printers,
                "cloud": {"url": remote.get("url", ""), "has_key": bool(remote)},
                "notifications": notifications, "channels": self.notifier.channels(),
                "legacy": [{"name": str(p.get("name") or p.get("id") or p["host"]), "host": p["host"]}
                           for p in old]}

    def import_legacy(self):
        """Copy the printers from the old printer-dashboard prototype into this config.

        A printer that is already here (same id or same address) is updated with the old
        settings, which are known to have worked; its serial and secrets are kept when the
        old file has none. Returns how many printers were imported.
        """
        _, old = legacy_printers()
        if not old:
            raise config.ConfigError("No printers found in the old printer-dashboard.")
        raw = config.read_raw(self.config_path)
        current = [p for p in raw.get("printers", []) if isinstance(p, dict)]
        for item in old:
            entry = {k: item[k] for k in IMPORT_FIELDS if k in item and item[k] not in (None, "")}
            match = next((c for c in current if c.get("id") == entry.get("id")
                          or str(c.get("host", "")).lower() == str(entry["host"]).lower()), None)
            if match is None:
                current.append(entry)
                continue
            entry["id"] = match.get("id") or entry.get("id")
            for field in config.SECRET_FIELDS:
                if not entry.get(field):
                    for key in (field, field + "_protected"):
                        if match.get(key):
                            entry[key] = match[key]
            for key in ("serial", "use_ams", "webcam_url"):
                if not entry.get(key) and match.get(key):
                    entry[key] = match[key]
            current[current.index(match)] = entry
        # Every printer needs an id, and Klipper printers their own Mainsail port.
        used_ids = set()
        used_ports = {raw.get("port", 8080), raw.get("admin_port", 8079)}
        for p in current:
            pid = re.sub(r"[^a-z0-9]+", "-", str(p.get("id") or p.get("name") or "printer").lower()).strip("-")[:32]
            pid = pid or "printer"
            base, n = pid, 2
            while pid in used_ids:
                pid = "%s-%d" % (base, n)
                n += 1
            used_ids.add(pid)
            p["id"] = pid
            if p.get("type") == "klipper":
                port = p.get("ui_port")
                if not isinstance(port, int) or port in used_ports:
                    port = 8101
                    while port in used_ports:
                        port += 1
                p["ui_port"] = port
                used_ports.add(port)
        raw["printers"] = current
        cfg = config.validate(raw)
        config.protect_secrets(cfg)
        config.save(self.config_path, cfg)
        self.reload()
        return len(old)

    def apply_notifications(self, payload):
        """Save notification settings. Empty secret fields keep the saved value; clear_<field> removes it."""
        if not isinstance(payload, dict):
            raise config.ConfigError("bad request")
        raw = config.read_raw(self.config_path)
        prev = raw.get("notifications") if isinstance(raw.get("notifications"), dict) else {}
        n = {
            "events": payload.get("events") if isinstance(payload.get("events"), dict) else {},
            "snapshot": bool(payload.get("snapshot")),
            "telegram_chat_id": str(payload.get("telegram_chat_id") or "").strip(),
        }
        for field in config.NOTIFY_SECRETS:
            value = str(payload.get(field) or "").strip()
            if payload.get("clear_" + field):
                continue
            if value:
                n[field] = value
            elif prev.get(field + "_protected"):
                n[field + "_protected"] = prev[field + "_protected"]
            elif prev.get(field):
                n[field] = prev[field]
        raw["notifications"] = n
        cfg = config.validate(raw)
        config.protect_secrets(cfg)
        config.save(self.config_path, cfg)
        with self.lock:
            self.cfg = dict(self.cfg, notifications=cfg["notifications"])

    def apply_setup(self, payload):
        if not isinstance(payload, dict) or not {"printers", "allowed_users", "cloud"}.intersection(payload):
            raise config.ConfigError("bad request")
        raw = config.read_raw(self.config_path)
        if "printers" in payload:
            if not isinstance(payload["printers"], list):
                raise config.ConfigError("printers must be a list")
            old = {p.get("id"): p for p in raw.get("printers", []) if isinstance(p, dict)}
            printers = []
            for item in payload["printers"]:
                if not isinstance(item, dict):
                    raise config.ConfigError("bad printer entry")
                p = {k: item[k] for k in EDITABLE_FIELDS if k in item and item[k] not in (None, "")}
                prev = old.get(p.get("id"), {})
                for field in config.SECRET_FIELDS:
                    value = str(item.get(field) or "").strip()
                    if value:
                        p[field] = value
                    elif prev.get(field + "_protected"):
                        p[field + "_protected"] = prev[field + "_protected"]
                    elif prev.get(field):
                        p[field] = prev[field]
                printers.append(p)
            raw["printers"] = printers
        if "allowed_users" in payload:
            raw["allowed_users"] = payload["allowed_users"]
        if "cloud" in payload:
            remote = payload["cloud"]
            if not isinstance(remote, dict):
                raise config.ConfigError("bad cloud settings")
            url = str(remote.get("url") or "").strip().rstrip("/").lower()
            key = str(remote.get("key") or "").strip()
            previous = raw.get("cloud", {})
            raw["cloud"] = previous if url == previous.get("url") and not key else {"url": url, "key": key}
        cfg = config.validate(raw)
        config.protect_secrets(cfg)
        config.save(self.config_path, cfg)
        if "printers" in payload:
            self.reload()
        else:
            self.cfg = dict(self.cfg, allowed_users=cfg["allowed_users"], cloud=cfg["cloud"])


class LimitedServer(ThreadingHTTPServer):
    """Threaded server with a cap on simultaneous connections."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, handler, limit=MAX_CONNECTIONS):
        self.slots = threading.BoundedSemaphore(limit)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "OS_3DPMS"
    sys_version = ""
    timeout = 60  # drop clients that stall (slowloris)
    app = None
    admin = False

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def internal_error(self, error):
        log.exception("request %s %s failed", self.command, urllib.parse.urlsplit(self.path).path)
        try:
            self.send_json(500, {"error": "OS_3DPMS error: %s" % (str(error)[:200] or type(error).__name__)})
        except OSError:
            pass

    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, name, ctype):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def discard_body(self, length, limit=16 * 1024 * 1024):
        """Read and drop a small rejected body so the client sees the error reply
        instead of a connection reset. Large bodies are not drained."""
        if length > limit:
            return
        while length > 0:
            chunk = self.rfile.read(min(1 << 20, length))
            if not chunk:
                return
            length -= len(chunk)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_JSON_BODY:
            raise config.ConfigError("request body missing or too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            raise config.ConfigError("request body is not JSON") from None

    # ------------------------------------------------------------ access control

    def identity(self):
        """Who is asking, or None to refuse."""
        if self.admin:
            # Admin port is never published; also reject DNS-rebinding hosts.
            host = (self.headers.get("Host") or "").lower().rsplit(":", 1)[0]
            return "local-admin" if host in LOCAL_HOSTNAMES else None
        if not local_or_tailnet(self.headers.get("Host")):
            return None  # DNS rebinding: another website pointed its name at this PC
        login = (self.headers.get("Tailscale-User-Login") or "").strip().lower()
        cfg = self.app.cfg
        if not login:
            return None if cfg["require_tailscale_identity"] else "tailnet-device"
        if cfg["allowed_users"] and login not in cfg["allowed_users"]:
            return None
        return login

    def refuse(self):
        login = (self.headers.get("Tailscale-User-Login") or "").strip()
        if self.admin:
            msg = "Open the setup page as http://127.0.0.1:%d/setup on this PC." % self.app.cfg["admin_port"]
        elif login:
            msg = "Tailscale account %s is not allowed on this dashboard." % login
        else:
            msg = "Open this dashboard through Tailscale (https://<this-pc>.<tailnet>.ts.net)."
        self.send_json(403, {"error": msg})

    def same_origin(self):
        """Block cross-site requests (CSRF): custom header + Origin/Fetch-Site checks."""
        if self.headers.get("X-Dashboard") != "1":
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            netloc = urllib.parse.urlsplit(origin).netloc
            if _normalize_host(netloc) != _normalize_host(self.headers.get("Host")):
                return False
        return True

    # ------------------------------------------------------------ routes

    def do_GET(self):
        try:
            user = self.identity()
            if user is None:
                return self.refuse()
            path = urllib.parse.urlsplit(self.path).path
            static = PUBLIC_FILES.get(path) or (ADMIN_FILES.get(path) if self.admin else None)
            if static:
                return self.send_file(*static)
            parts = [x for x in path.split("/") if x]
            app = self.app

            if path == "/api/me":
                return self.send_json(200, {"user": user, "admin": self.admin, "version": __version__,
                                            "max_upload_mb": app.cfg["max_upload_mb"]})
            if path == "/api/printers":
                return self.send_json(200, [{
                    "id": p["id"], "name": p["name"], "type": p["type"], "ui_port": p.get("ui_port"),
                    "rotate": p["rotate"], "flip_horizontal": p["flip_horizontal"],
                    "flip_vertical": p["flip_vertical"],
                } for p in app.cfg["printers"]])
            if len(parts) == 3 and parts[:2] == ["api", "status"]:
                return self.send_status(parts[2])
            if len(parts) == 2 and parts[0] in ("snap", "cam"):
                hub = app.hubs.get(parts[1])
                if hub is None:
                    return self.send_json(404, {"error": "unknown printer"})
                return self.send_snapshot(hub) if parts[0] == "snap" else self.send_stream(hub)
            if path == "/api/events":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try:
                    after = int(query.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                events, last = app.monitor.events_after(after)
                return self.send_json(200, {"events": events[-50:], "last": last})
            if self.admin and path == "/api/setup/status":
                return self.send_setup_status()
            if self.admin and path == "/api/setup/config":
                return self.send_json(200, app.setup_config())
            self.send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # never drop the connection silently: the page shows this message
            self.internal_error(e)

    def do_POST(self):
        try:
            user = self.identity()
            if user is None:
                return self.refuse()
            if not self.same_origin():
                return self.send_json(403, {"error": "cross-site request blocked"})
            path = urllib.parse.urlsplit(self.path).path
            parts = [x for x in path.split("/") if x]
            if len(parts) == 3 and parts[:2] == ["api", "upload"]:
                return self.handle_upload(user, parts[2])
            if self.admin and path == "/api/setup/config":
                try:
                    payload = self.read_json()
                    with self.app.lock:
                        self.app.apply_setup(payload)
                except config.ConfigError as e:
                    return self.send_json(400, {"ok": False, "error": str(e)})
                self.app.audit(user, "config-saved", printers=len(self.app.printers))
                return self.send_json(200, {"ok": True})
            if self.admin and path == "/api/setup/import-old":
                try:
                    with self.app.lock:
                        count = self.app.import_legacy()
                except config.ConfigError as e:
                    return self.send_json(400, {"ok": False, "error": str(e)})
                self.app.audit(user, "config-imported", printers=count)
                return self.send_json(200, {"ok": True, "imported": count})
            if self.admin and len(parts) == 4 and parts[:3] == ["api", "setup", "check-printer"]:
                return self.check_printer(parts[3])
            if self.admin and path == "/api/setup/discover":
                return self.send_discovery()
            if self.admin and path == "/api/setup/notifications":
                try:
                    payload = self.read_json()
                    with self.app.lock:
                        self.app.apply_notifications(payload)
                except config.ConfigError as e:
                    return self.send_json(400, {"ok": False, "error": str(e)})
                self.app.audit(user, "notifications-saved", channels=self.app.notifier.channels())
                return self.send_json(200, {"ok": True, "channels": self.app.notifier.channels()})
            if self.admin and path == "/api/setup/notify-test":
                event = self.app.monitor.test_event()
                return self.send_json(200, {"results": self.app.notifier.deliver(event),
                                            "channels": self.app.notifier.channels()})
            if self.admin and path == "/api/setup/install-tailscale":
                result = tsinstall.start()
                self.app.audit(user, "tailscale-install", result=result)
                return self.send_json(200, result)
            if self.admin and path == "/api/setup/login":
                return self.send_json(200, tailscale.start_login())
            if self.admin and path == "/api/setup/publish":
                results = tailscale.publish(self.app.publish_mappings())
                self.app.audit(user, "publish", results=results)
                return self.send_json(200, {"results": results})
            self.send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # never drop the connection silently: the page shows this message
            self.internal_error(e)

    def do_PUT(self):
        self.send_json(405, {"error": "method not allowed"})

    do_DELETE = do_PATCH = do_PUT

    # ------------------------------------------------------------ handlers

    def send_status(self, pid):
        p = self.app.printers.get(pid)
        if p is None:
            return self.send_json(404, {"error": "unknown printer"})
        # The monitor polls every printer; serve its cached result.
        st = self.app.monitor.status.get(pid) if self.app.monitor else None
        self.send_json(200, st or {"state": None})

    def send_snapshot(self, hub):
        frame = hub.snapshot()
        if frame is None:
            with hub.cond:
                reason = hub.error
            msg = "No picture from the camera"
            if reason:
                msg += ": %s" % str(reason)[:400]
            return self.send_json(503, {"error": msg})
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def check_printer(self, pid):
        with self.app.lock:
            p = self.app.printers.get(pid)
            hub = self.app.hubs.get(pid)
        if p is None or hub is None:
            return self.send_json(404, {"error": "Save this printer before checking it."})

        hub.touch()
        host = p["host"]
        port = 8883 if p["type"] == "bambu" else p["http_port"]
        try:
            if p["type"] == "klipper":
                klipper.status(p)
                printer = "Moonraker responded at %s:%d." % (host, port)
                printer_ok = True
            else:
                printer_ok, printer = self.check_bambu(p)
        except (OSError, ValueError, KeyError, TypeError) as error:
            printer_ok = False
            if isinstance(error, urllib.error.HTTPError):
                detail = "HTTP %d. Check the printer HTTP port." % error.code
                if error.code in (401, 403):
                    detail = "Moonraker denied access. Check the API key or Moonraker trusted clients."
            elif isinstance(error, (ValueError, KeyError, TypeError)):
                detail = "The response is not from the Moonraker API. Check the printer HTTP port."
            else:
                detail = "No connection. Check the address, port and this PC’s access to the printer network."
            printer = "%s:%d. %s" % (host, port, detail)

        camera_ok = hub.snapshot() is not None
        if camera_ok:
            camera = "Camera frame received."
        elif p["type"] == "bambu" and not p.get("webcam_url"):
            camera = "No camera frame. Check the LAN access code and whether this model provides a JPEG feed on port 6000."
        else:
            camera = "No camera frame. Check that the MJPEG or JPEG snapshot URL opens from this PC."
        if not camera_ok:
            with hub.cond:
                reason = hub.error
            if reason:  # the camera reader's own error says what went wrong
                camera += " Details: %s" % str(reason)[:400]
            if p["type"] == "bambu" and not p.get("webcam_url"):
                code = config.secret(p, "access_code")
                if code != code.strip() or any(c.isupper() for c in code):
                    camera += (" The saved access code has capital letters or spaces. Codes are"
                               " case-sensitive: type it exactly as the printer screen shows it.")
        return self.send_json(200, {"printer_ok": printer_ok, "printer": printer,
                                    "camera_ok": camera_ok, "camera": camera})

    def check_bambu(self, p):
        """(ok, text): log in with the saved access code, as live status and remote start do."""
        host = p["host"]
        try:
            ok, text = bambu.check_login(host, config.secret(p, "access_code"), self.app.pins)
        except CertificateChanged as e:
            return False, str(e)
        if not ok:
            return False, "%s:8883 is reachable, but the status login failed: %s." % (host, text)
        if p.get("serial"):
            watcher = self.app.bambu_watchers.get(p["id"])
            live = " Live status is connected." if watcher and watcher.connected else ""
            return True, "Access code accepted (serial %s).%s" % (p["serial"], live)
        self.app.bambu_listener.listen(60)
        return True, ("Access code accepted. The serial number is still missing: OS_3DPMS is listening for "
                      "the printer's announcement to fill it in. You can also enter it under Advanced "
                      "settings (printer screen: Settings > Device).")

    def send_discovery(self):
        result = discover.find_printers(self.app.bambu_listener)
        with self.app.lock:
            known = {(p["type"], p["host"].lower()) for p in self.app.cfg["printers"]}
        for item in result["printers"]:
            item["saved"] = (item["type"], item["host"].lower()) in known
        return self.send_json(200, result)

    def send_stream(self, hub):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        last = 0.0
        while True:
            hub.touch()
            frame, ts = hub.wait_frame(last, 30)
            if frame is None or ts == last:
                return  # camera gone or printer removed
            last = ts
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                             % len(frame) + frame + b"\r\n")

    def send_setup_status(self):
        ts = tailscale.status()
        expected = {port: "http://127.0.0.1:%d" % local for port, local in self.app.publish_mappings()}
        served = tailscale.serve_targets() if ts.get("state") == "Running" else {}
        published = {str(port): served.get(port, "").rstrip("/") == target for port, target in expected.items()}
        dns = ts.get("dns_name", "")
        self.send_json(200, {
            "tailscale": ts,
            "cloud": self.app.connector.status() if self.app.connector else {},
            "install": dict(tsinstall.state(), supported=tsinstall.supported()),
            "published": published,
            "public_url": "https://%s/" % dns if dns else "",
            "printers": len(self.app.printers),
        })

    def handle_upload(self, user, pid):
        app = self.app
        p = app.printers.get(pid)
        if p is None:
            return self.send_json(404, {"ok": False, "error": "unknown printer"})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        name = safe_filename(query.get("name", [""])[0])
        start = query.get("print", ["0"])[0] == "1"
        if not name or not name.lower().endswith(UPLOAD_EXTENSIONS):
            return self.send_json(400, {"ok": False, "error": "only .gcode or .3mf files"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self.send_json(400, {"ok": False, "error": "empty file"})
        if length > app.cfg["max_upload_mb"] * 1024 * 1024:
            self.discard_body(length)
            return self.send_json(413, {"ok": False, "error": "file larger than %d MB" % app.cfg["max_upload_mb"]})
        lock = app.upload_locks.get(pid)
        if lock is None or not lock.acquire(blocking=False):
            return self.send_json(409, {"ok": False, "error": "another upload to this printer is running"})

        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".upload", dir=app.tmp_dir)
            with os.fdopen(fd, "wb") as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    remaining -= len(chunk)
            if p["type"] == "bambu":
                msg = bambu.upload(p, tmp, name, start, app.pins)
            else:
                msg = klipper.upload(p, tmp, name, start)
            app.audit(user, "upload", printer=pid, file=name, bytes=length, start=start, result="ok")
            log.info("%s uploaded %s to %s", user, name, pid)
            self.send_json(200, {"ok": True, "message": msg})
        except Exception as e:
            log.warning("upload to %s failed: %s", pid, e)
            app.audit(user, "upload", printer=pid, file=name, bytes=length, start=start, result="error",
                      error=str(e)[:200])
            self.send_json(502, {"ok": False, "error": str(e)[:300]})
        finally:
            lock.release()
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def make_handler(app, admin):
    return type("AdminHandler" if admin else "PublicHandler", (Handler,), {"app": app, "admin": admin})


def _audit_logger(data_dir):
    logger = logging.getLogger("dashboard.audit.%s" % id(data_dir))
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = RotatingFileHandler(os.path.join(data_dir, "audit.log"), maxBytes=1_000_000,
                                backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(h)
    return logger


def setup_logging(data_dir):
    redirected = sys.stderr is None or sys.stdout is None
    if redirected:  # pythonw: no console
        stream = open(os.path.join(data_dir, "stderr.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(os.path.join(data_dir, "dashboard.log"), maxBytes=1_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if not redirected:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="OS_3DPMS", description="3D printer camera dashboard")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    parser.add_argument("--data-dir", default=ROOT, help="where logs, pins and temp files go")
    parser.add_argument("--open-setup", action="store_true", help="open the setup page in a browser")
    parser.add_argument("--install-tailscale", action="store_true",
                        help="download, verify and install Tailscale (Windows), then exit")
    args = parser.parse_args(argv)

    if args.install_tailscale:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        result = tsinstall.start(background=False)
        if result.get("already"):
            print("Tailscale is already installed.")
            return 0
        st = tsinstall.state()
        print(st["detail"] or result.get("error", ""))
        return 0 if st["state"] == "done" else 1

    os.makedirs(args.data_dir, exist_ok=True)
    setup_logging(args.data_dir)
    if not os.path.exists(args.config):
        config.write_default(args.config)
        log.info("created %s", args.config)
    try:
        app = App(args.config, args.data_dir)
    except config.ConfigError as e:
        log.error("config.json: %s", e)
        return 2

    try:
        public = LimitedServer(("127.0.0.1", app.cfg["port"]), make_handler(app, False))
        admin = LimitedServer(("127.0.0.1", app.cfg["admin_port"]), make_handler(app, True))
    except OSError as e:
        log.error("cannot open port (already running?): %s", e)
        return 1

    pid_file = os.path.join(args.data_dir, "dashboard.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(pid_file) and os.remove(pid_file))

    threading.Thread(target=admin.serve_forever, daemon=True).start()
    app.connector = cloud.Connector(app)
    app.connector.start()
    setup_url = "http://127.0.0.1:%d/setup" % app.cfg["admin_port"]
    log.info("OS_3DPMS %s running. Setup on this PC: %s", __version__, setup_url)
    if args.open_setup:
        webbrowser.open(setup_url)
    try:
        public.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
