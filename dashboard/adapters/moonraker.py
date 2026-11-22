"""Klipper printers through Moonraker: WebSocket first, HTTP polling as fallback.

Primary target: Two Trees Sapphire Pro with Klipper; works with any Moonraker.
Objects are discovered with printer.objects.list, so a printer without (say) a bed or a
filament sensor simply has no such value. Nothing here changes printer settings.
"""
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse

from .. import cameras, config, klipper, wsclient
from .base import AdapterError, PrinterAdapter, PrinterBusy

log = logging.getLogger("dashboard.adapters.moonraker")

BASE_OBJECTS = ("webhooks", "print_stats", "display_status", "virtual_sdcard", "toolhead", "extruder",
                "heater_bed", "fan", "gcode_move", "motion_report", "idle_timeout", "pause_resume")
PREFIX_OBJECTS = ("extruder", "temperature_sensor ", "heater_generic ", "temperature_fan ",
                  "filament_switch_sensor ", "filament_motion_sensor ")
LEGACY_OBJECTS = ("print_stats", "display_status")  # when the object list cannot be read
HEARTBEAT_S = 20      # ping when the socket is quiet this long
DEAD_S = 50           # reconnect when nothing (not even a pong) arrived this long
MAX_BACKOFF_S = 60
OBJECTS_TTL_S = 300


def wanted_objects(available):
    """Objects worth subscribing to, from the printer's own list."""
    out = []
    for name in available:
        if name in BASE_OBJECTS or name.startswith(PREFIX_OBJECTS):
            out.append(name)
    return out


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize(st, metadata=None):
    """Normalized status from a Moonraker object status dict (missing objects are fine)."""
    wh = st.get("webhooks") or {}
    ps = st.get("print_stats") or {}
    ds = st.get("display_status") or {}
    vsd = st.get("virtual_sdcard") or {}
    th = st.get("toolhead") or {}
    mr = st.get("motion_report") or {}
    state = ps.get("state") or "standby"
    message = ps.get("message") or ""
    errors, warnings = [], []
    if wh.get("state") and wh["state"] != "ready":
        state = "error"
        message = wh.get("state_message") or ("Klipper is %s" % wh["state"])
        errors.append("Klipper %s: %s" % (wh["state"], message.strip()[:200]))
    progress = ds.get("progress")
    if not progress:
        progress = vsd.get("progress") or 0
    duration = _num(ps.get("print_duration"), 0) or 0
    temps = {}
    for name, obj in st.items():
        if not isinstance(obj, dict) or "temperature" not in obj:
            continue
        if name == "heater_bed":
            label = "bed"
        elif name == "extruder":
            label = "nozzle"
        elif name.startswith("extruder"):
            label = "nozzle" + name[len("extruder"):]
        elif " " in name:
            label = name.split(" ", 1)[1]
        else:
            continue
        temps[label] = {"actual": _num(obj.get("temperature")), "target": _num(obj.get("target"))}
    fan = (st.get("fan") or {}).get("speed")
    pos = mr.get("live_position") or th.get("position")
    for name, obj in st.items():
        if name.startswith(("filament_switch_sensor ", "filament_motion_sensor ")) and isinstance(obj, dict):
            if obj.get("enabled", True) and obj.get("filament_detected") is False:
                warnings.append("Filament not detected (%s)" % name.split(" ", 1)[1])
    info = ps.get("info") or {}
    layer = None
    if info.get("total_layer") or info.get("current_layer"):
        layer = {"current": info.get("current_layer"), "total": info.get("total_layer")}
    remaining = None
    est = _num((metadata or {}).get("estimated_time"))
    if state in ("printing", "paused"):
        if est and duration:
            remaining = max(0.0, est - duration)
        elif progress and progress > 0.01 and duration:
            remaining = max(0.0, duration / progress - duration)
    return {
        "state": state, "file": ps.get("filename", "") or "", "progress": progress or 0,
        "duration": duration, "remaining_s": remaining, "message": message,
        "temperatures": temps, "fan": _num(fan), "position": list(pos[:3]) if pos else None,
        "tool": th.get("extruder"), "layer": layer, "errors": errors, "warnings": warnings,
    }


def merge(cache, delta):
    """Merge a notify_status_update delta into the cached object status."""
    for name, values in (delta or {}).items():
        if isinstance(values, dict):
            cache.setdefault(name, {}).update(values)


class MoonrakerAdapter(PrinterAdapter):
    protocol = "moonraker"
    manufacturer = "Klipper"
    supports = {"monitor": True, "can_upload": True, "can_start_print": True, "can_pause": True,
                "can_resume": True, "can_cancel": True, "can_list_files": True, "has_camera": True,
                "has_history": True}

    def __init__(self, printer, pins=None):
        super().__init__(printer, pins)
        self.cache = {}
        self.ws_connected = False
        self.ws_error = ""
        self.ws_since = 0.0
        self.reconnects = 0
        self.objects = None
        self.objects_at = 0.0
        self.metadata = {}
        self.stopped = threading.Event()
        self.thread = None
        self.ws = None
        self._next_id = 0

    # ------------------------------------------------------------ lifecycle

    def connect(self):
        if self.p.get("websocket", True) and self.thread is None:
            self.thread = threading.Thread(target=self._ws_loop, daemon=True, name="moonraker-%s" % self.p["id"])
            self.thread.start()

    def disconnect(self):
        self.stopped.set()
        ws = self.ws
        if ws is not None:
            ws.close()

    # ------------------------------------------------------------ WebSocket

    def ws_url(self):
        port = self.p.get("http_port", 80)
        host = "[%s]" % self.p["host"] if ":" in self.p["host"] else self.p["host"]
        url = "ws://%s%s/websocket" % (host, "" if port == 80 else ":%d" % port)
        if config.secret(self.p, "api_key"):
            # Moonraker: a WebSocket cannot send X-Api-Key, so get a one-shot token over HTTP.
            token = klipper.http_json(self.p, "/access/oneshot_token", timeout=5).get("result", "")
            url += "?token=" + urllib.parse.quote(str(token))
        return url

    def _ws_loop(self):
        backoff = 1.0
        while not self.stopped.is_set():
            started = time.time()
            try:
                self._ws_session()
            except Exception as e:
                self.ws_error = str(e)[:200] or type(e).__name__
                log.debug("moonraker %s websocket: %s", self.p["id"], self.ws_error)
            finally:
                self.ws_connected = False
                ws, self.ws = self.ws, None
                if ws is not None:
                    ws.close()
            if self.stopped.is_set():
                return
            if time.time() - started > 60:
                backoff = 1.0  # it was up for a while: retry quickly
            self.reconnects += 1
            self.stopped.wait(backoff)
            backoff = min(MAX_BACKOFF_S, backoff * 2)

    def _rpc(self, ws, method, params=None, timeout=5.0):
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            msg["params"] = params
        ws.send_text(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except socket.timeout:
                continue
            if raw is None:
                continue
            data = json.loads(raw)
            if data.get("id") == rid:
                if "error" in data:
                    raise AdapterError("Moonraker %s: %s" % (method, (data["error"] or {}).get("message", "error")))
                return data.get("result")
            self._on_message(data)
        raise TimeoutError("Moonraker did not answer %s" % method)

    def _ws_session(self):
        ws = wsclient.connect(self.ws_url(), timeout=5)
        self.ws = ws
        ws.sock.settimeout(1.0)
        try:
            listed = self._rpc(ws, "printer.objects.list")
            objects = wanted_objects((listed or {}).get("objects") or [])
        except AdapterError as e:  # klippy not ready: Moonraker is up, Klipper is not
            objects = []
            with self._lock:
                self.cache = {"webhooks": {"state": "startup", "state_message": str(e)}}
        if objects:
            result = self._rpc(ws, "printer.objects.subscribe", {"objects": {o: None for o in objects}})
            with self._lock:
                self.cache = dict((result or {}).get("status") or {})
                self.objects, self.objects_at = objects, time.time()
        self.ws_connected = True
        self.ws_error = ""
        self.ws_since = time.time()
        last = time.time()
        pinged = False
        while not self.stopped.is_set():
            try:
                raw = ws.recv()
            except socket.timeout:
                quiet = time.time() - last
                if quiet > DEAD_S:
                    raise TimeoutError("no answer from Moonraker for %d s" % quiet) from None
                if quiet > HEARTBEAT_S and not pinged:
                    ws.ping()
                    pinged = True
                continue
            last, pinged = time.time(), False
            if raw is None:  # pong
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if self._on_message(data) == "resubscribe":
                return  # Klipper restarted: reconnect for a fresh object list

    def _on_message(self, data):
        method = data.get("method")
        if method == "notify_status_update":
            params = data.get("params") or []
            if params and isinstance(params[0], dict):
                with self._lock:
                    merge(self.cache, params[0])
        elif method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
            with self._lock:
                self.cache.setdefault("webhooks", {}).update(
                    {"state": "shutdown" if method.endswith("shutdown") else "disconnected",
                     "state_message": "Klipper %s" % method.rsplit("_", 1)[1]})
        elif method == "notify_klippy_ready":
            return "resubscribe"
        return None

    # ------------------------------------------------------------ status

    def _http_objects(self):
        if self.objects is None or time.time() - self.objects_at > OBJECTS_TTL_S:
            try:
                listed = klipper.http_json(self.p, "/printer/objects/list", timeout=3)
                self.objects = wanted_objects(listed["result"]["objects"]) or list(LEGACY_OBJECTS)
            except (KeyError, TypeError, ValueError):
                self.objects = list(LEGACY_OBJECTS)
            self.objects_at = time.time()
        return self.objects

    def _metadata(self, filename):
        if not filename:
            return {}
        if filename not in self.metadata:
            try:
                q = urllib.parse.urlencode({"filename": filename})
                self.metadata[filename] = klipper.http_json(self.p, "/server/files/metadata?" + q,
                                                            timeout=2).get("result") or {}
            except Exception:
                self.metadata[filename] = {}
            if len(self.metadata) > 50:
                self.metadata.pop(next(iter(self.metadata)))
        return self.metadata[filename]

    def fetch_status(self):
        if self.ws_connected:
            with self._lock:
                raw = json.loads(json.dumps(self.cache))
            transport = "websocket"
        else:
            objects = self._http_objects()
            query = "&".join(urllib.parse.quote(o) for o in objects)
            raw = klipper.http_json(self.p, "/printer/objects/query?" + query, timeout=3)["result"]["status"]
            transport = "http"
        st = normalize(raw, self._metadata((raw.get("print_stats") or {}).get("filename")))
        st["transport"] = transport
        return st

    # ------------------------------------------------------------ files and control

    def file_exists(self, name):
        q = urllib.parse.urlencode({"filename": name})
        try:
            klipper.http_json(self.p, "/server/files/metadata?" + q, timeout=3)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            return None
        except Exception:
            return None

    def upload_file(self, path, name, start=False):
        self.require("can_upload")
        if start:
            self.require("can_start_print")
        if not self.accepts_file(name):
            raise AdapterError("%s prints %s files only" % (self.p["name"], ", ".join(self.file_extensions)))
        return klipper.upload(self.p, path, name, start)

    def _post(self, path):
        try:
            return klipper.http_json(self.p, path, timeout=10, method="POST", data=b"")
        except urllib.error.HTTPError as e:
            raise AdapterError("%s refused: HTTP %d" % (self.p["name"], e.code)) from None
        except OSError as e:
            raise AdapterError("%s did not answer: %s" % (self.p["name"], e)) from None

    def start_print(self, filename):
        self.require("can_start_print")
        st = self.get_status()
        if st is None:
            raise AdapterError("%s is not answering" % self.p["name"])
        if st["state"] in ("printing", "paused"):
            raise PrinterBusy("%s is %s" % (self.p["name"], st["state"]))
        self._post("/printer/print/start?" + urllib.parse.urlencode({"filename": filename}))
        return "Print of %s started on %s" % (filename, self.p["name"])

    def pause_print(self):
        self.require("can_pause")
        self._post("/printer/print/pause")
        return "%s paused" % self.p["name"]

    def resume_print(self):
        self.require("can_resume")
        self._post("/printer/print/resume")
        return "%s resumed" % self.p["name"]

    def cancel_print(self):
        self.require("can_cancel")
        self._post("/printer/print/cancel")
        return "Print on %s cancelled" % self.p["name"]

    def list_files(self):
        data = klipper.http_json(self.p, "/server/files/list?root=gcodes", timeout=5).get("result") or []
        return [{"name": f.get("path") or f.get("filename"), "size": f.get("size"),
                 "modified": f.get("modified")} for f in data if isinstance(f, dict)]

    def get_history(self, limit=20):
        data = klipper.http_json(self.p, "/server/history/list?limit=%d&order=desc" % int(limit), timeout=5)
        jobs = (data.get("result") or {}).get("jobs") or []
        return [{"file": j.get("filename"), "status": j.get("status"), "start": j.get("start_time"),
                 "end": j.get("end_time"), "duration": j.get("print_duration")} for j in jobs]

    def camera_source(self):
        p = self.p
        return lambda hub: cameras.klipper_camera(hub, lambda: klipper.webcam_urls(p))

    def diagnostics(self):
        d = super().diagnostics()
        d.update({"websocket": self.ws_connected, "websocket_error": self.ws_error,
                  "reconnects": self.reconnects, "objects": list(self.objects or [])})
        for key, path in (("server", "/server/info"), ("system", "/machine/system_info"),
                          ("proc", "/machine/proc_stats"), ("history_totals", "/server/history/totals"),
                          ("updates", "/machine/update/status")):
            try:
                result = klipper.http_json(self.p, path, timeout=3).get("result")
            except Exception as e:
                result = {"error": str(e)[:120]}
            d[key] = _summary(key, result)
        return d


def _summary(key, result):
    """Small, secret-free extract of Moonraker's diagnostic answers."""
    if not isinstance(result, dict) or "error" in result:
        return result
    if key == "server":
        return {k: result.get(k) for k in ("klippy_state", "klippy_connected", "moonraker_version",
                                           "api_version_string", "components") if k in result}
    if key == "system":
        si = result.get("system_info") or {}
        return {"cpu": (si.get("cpu_info") or {}).get("model"), "distribution":
                (si.get("distribution") or {}).get("name")}
    if key == "proc":
        return {k: result.get(k) for k in ("cpu_temp", "system_uptime", "system_memory") if k in result}
    if key == "history_totals":
        return result.get("job_totals") or result
    if key == "updates":
        info = result.get("version_info") or {}
        return {name: (v or {}).get("version") for name, v in info.items() if isinstance(v, dict)}
    return result
