"""Bambu Lab printers (P1S first; P1P, A1, X1 similar) over the local network.

* Status: MQTT over TLS (port 8883, user `bblp`, the LAN access code), read-only by default.
* Camera: JPEG feed on TLS port 6000 (P1/A1). X1/H2D/P2S use RTSP and need a bridge.
* Upload: implicit FTPS (port 990) to the SD card, only with `allow_file_upload`.
* Start / pause / resume / stop: MQTT commands, only with `mode: control` and
  `allow_remote_control`. Read access never implies write access: the printer may still refuse
  (LAN Only Mode / Developer Mode / authorization on newer firmware), and that refusal is shown.

Nothing here changes printer settings (LAN mode, cloud, Developer Mode, network, firmware).
Field names were checked against P1S reports seen in the field; every field is optional
because they differ between models and firmware versions.
"""
import json
import logging

from .. import bambu, cameras, config
from .base import AdapterError, NotSupported, PrinterAdapter, PrinterBusy

log = logging.getLogger("dashboard.adapters.bambu")
CONTROL_STATES = {"pause": ("PAUSE",), "resume": ("RUNNING", "PREPARE"), "stop": ("IDLE", "FINISH", "FAILED")}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fan(v):
    """Bambu fan speeds are strings 0..15."""
    n = _num(v)
    return None if n is None else round(min(15.0, max(0.0, n)) / 15.0, 2)


def enrich(base, r, use_ams=True):
    """Add temperatures, fans, layers, AMS and HMS from a raw report to a legacy status."""
    st = dict(base)
    temps = {}
    for label, actual, target in (("nozzle", "nozzle_temper", "nozzle_target_temper"),
                                  ("bed", "bed_temper", "bed_target_temper"),
                                  ("chamber", "chamber_temper", None)):
        if r.get(actual) is not None:
            temps[label] = {"actual": _num(r.get(actual)), "target": _num(r.get(target)) if target else None}
    st["temperatures"] = temps
    st["fan"] = _fan(r.get("cooling_fan_speed"))
    fans = {k: _fan(r.get(k)) for k in ("big_fan1_speed", "big_fan2_speed", "heatbreak_fan_speed")
            if r.get(k) is not None}
    if fans:
        st["fans"] = fans
    if r.get("total_layer_num") or r.get("layer_num"):
        st["layer"] = {"current": r.get("layer_num"), "total": r.get("total_layer_num")}
    rem = _num(r.get("mc_remaining_time"))
    if rem is not None and st.get("state") in ("printing", "paused"):
        st["remaining_s"] = rem * 60
    errors, warnings = [], []
    if st.get("message"):
        errors.append(st["message"])
    for item in r.get("hms") or []:
        if isinstance(item, dict):
            attr, code = int(item.get("attr") or 0), int(item.get("code") or 0)
            errors.append("HMS %04X-%04X-%04X-%04X" % (attr >> 16, attr & 0xFFFF, code >> 16, code & 0xFFFF))
    ams = r.get("ams") if isinstance(r.get("ams"), dict) else None
    if use_ams and ams and isinstance(ams.get("ams"), list):   # AMS off in setup: ignore it, no warning
        units = []
        for unit in ams["ams"]:
            if not isinstance(unit, dict):
                continue
            trays = []
            for t in unit.get("tray") or []:
                if isinstance(t, dict):
                    trays.append({"slot": t.get("id"), "type": t.get("tray_type") or "",
                                  "color": str(t.get("tray_color") or "")[:8], "remain": t.get("remain")})
            units.append({"id": unit.get("id"), "humidity": unit.get("humidity"), "trays": trays})
        st["ams"] = {"units": units, "tray_now": ams.get("tray_now")}
        if not any(t["type"] for u in units for t in u["trays"]):
            warnings.append("AMS reports no filament loaded")
    st["errors"], st["warnings"] = errors, warnings
    st["transport"] = "mqtt"
    return st


class BambuAdapter(PrinterAdapter):
    protocol = "bambu_mqtt"
    manufacturer = "Bambu Lab"
    file_extensions = (".3mf", ".gcode")
    supports = {"monitor": True, "can_upload": True, "can_start_print": True, "can_pause": True,
                "can_resume": True, "can_cancel": True, "has_camera": True, "has_ams": True}

    def __init__(self, printer, pins=None):
        super().__init__(printer, pins)
        self.watcher = None

    def connect(self):
        if self.p.get("serial") and self.watcher is None:
            self.watcher = bambu.BambuWatcher(self.p, self.pins)
            self.watcher.start()

    def disconnect(self):
        if self.watcher is not None:
            self.watcher.stop()

    def allowed(self, key):
        if self.p.get("mode", "read_only") != "control":
            return False  # read-only mode: no upload or control, whatever else is set
        return super().allowed(key)

    def get_capabilities(self):
        caps = super().get_capabilities()
        if not self.p.get("serial"):  # without the serial number there is no MQTT status or control
            for k in ("monitor", "can_start_print", "can_pause", "can_resume", "can_cancel", "has_ams"):
                caps[k] = False
        caps["has_ams"] = caps["has_ams"] and bool(self.p.get("use_ams"))
        return caps

    def fetch_status(self):
        if self.watcher is None:
            return {"state": None}  # no serial number: camera only
        base = self.watcher.status()
        if base is None:
            self.last_error = self.watcher.error or "no status report"
            return None
        raw, _ = self.watcher.raw_report()
        return enrich(base, raw, use_ams=bool(self.p.get("use_ams")))

    def upload_file(self, path, name, start=False):
        self.require("can_upload")
        if start:
            self.require("can_start_print")
            st = self.get_status()
            if st and st.get("state") in ("printing", "paused"):
                raise PrinterBusy("%s is %s" % (self.p["name"], st["state"]))
        if not self.accepts_file(name):
            raise AdapterError("Bambu printers take .3mf (sliced) or .gcode files")
        return bambu.upload(self.p, path, name.replace(" ", "_"), start, self.pins, self.watcher)

    def start_print(self, filename):
        raise NotSupported("Bambu prints start together with the upload (the file must be sent from here)")

    def _command(self, command):
        if self.watcher is None or not self.watcher.connected:
            raise AdapterError("%s has no live MQTT connection" % self.p["name"])
        seq = bambu._new_seq()
        payload = json.dumps({"print": {"sequence_id": seq, "command": command, "param": ""}}).encode()
        reply, state = self.watcher.request("device/%s/request" % self.p["serial"], payload, seq, command, 10)
        if reply:
            result = str(reply.get("result", "")).lower()
            err = str(reply.get("err_code") or "")
            if result in ("fail", "failed", "error") or (err and err != "0"):
                raise AdapterError("%s refused %s: %s%s" % (
                    self.p["name"], command, reply.get("reason") or "no reason given",
                    " (code %s)" % err if err and err != "0" else ""))
            return
        if state not in CONTROL_STATES[command]:
            raise AdapterError("%s did not confirm %s. Newer firmware may need LAN Only Mode and "
                               "Developer Mode for third-party control." % (self.p["name"], command))

    def pause_print(self):
        self.require("can_pause")
        self._command("pause")
        return "%s paused" % self.p["name"]

    def resume_print(self):
        self.require("can_resume")
        self._command("resume")
        return "%s resumed" % self.p["name"]

    def cancel_print(self):
        self.require("can_cancel")
        self._command("stop")
        return "Print on %s stopped" % self.p["name"]

    def camera_source(self):
        p, pins = self.p, self.pins
        if p.get("webcam_url"):
            from .. import klipper
            return lambda hub: cameras.klipper_camera(hub, lambda: klipper.webcam_urls(p))
        return lambda hub: cameras.bambu_camera(hub, p["host"], config.secret(p, "access_code"), pins,
                                                p.get("camera_port", 6000))

    def diagnostics(self):
        d = super().diagnostics()
        w = self.watcher
        d.update({"mode": self.p.get("mode", "read_only"), "serial_known": bool(self.p.get("serial")),
                  "mqtt_connected": bool(w and w.connected), "mqtt_error": (w.error if w else "") or ""})
        return d
