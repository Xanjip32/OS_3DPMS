"""The common interface every printer protocol implements.

An adapter talks to one printer. The dashboard, monitor, queue and (later) MCP tools use
only this interface, never protocol details. Each adapter reports what it can do through
`get_capabilities()`: what the protocol supports, narrowed by what the owner allowed in
config.json (`allow_remote_control`, `allow_file_upload`, Bambu `mode`). The dashboard hides
controls whose capability is false, and every control method checks again (`require`).
"""
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("dashboard.adapters")

CAPABILITY_KEYS = (
    "monitor",                  # live status
    "can_upload",               # send a file to the printer
    "can_start_print",
    "can_pause",
    "can_resume",
    "can_cancel",
    "can_control_temperature",  # never exposed in the dashboard yet; listed for completeness
    "can_list_files",
    "has_camera",
    "has_ams",
    "has_history",
)
CONTROL_CAPS = ("can_start_print", "can_pause", "can_resume", "can_cancel")

# Normalized printer states (Klipper's names, which the dashboard always used).
STATES = ("standby", "printing", "paused", "complete", "cancelled", "error", "offline")


class AdapterError(RuntimeError):
    """The printer refused or failed an operation. The message is shown to the user."""


class NotSupported(AdapterError):
    """This printer or protocol cannot do that."""


class NotAuthorized(AdapterError):
    """The protocol could do it, but the owner has not allowed it for this printer."""


class PrinterBusy(AdapterError):
    """The printer is printing or paused, so the operation is refused."""


def empty_status(state: Optional[str] = None) -> dict:
    """A status with every normalized key. Unknown values stay None / empty."""
    return {
        "state": state,           # one of STATES, or None when the printer gives no state
        "file": "",
        "progress": 0.0,          # 0..1
        "duration": 0,            # seconds printed so far
        "remaining_s": None,      # estimate, when the printer gives one
        "message": "",            # printer's own message or error text
        "temperatures": {},       # name -> {"actual": float, "target": float|None}
        "fan": None,              # part cooling fan 0..1
        "position": None,         # [x, y, z]
        "tool": None,             # active extruder name
        "layer": None,            # {"current": int, "total": int}
        "ams": None,              # Bambu AMS summary
        "errors": [],             # list of short error strings (HMS, klippy state, ...)
        "warnings": [],           # e.g. filament runout
        "transport": "",          # websocket / http / mqtt / serial
    }


def finish_status(st: dict) -> dict:
    """Fill derived and legacy keys so older dashboard code keeps working."""
    out = empty_status()
    out.update({k: v for k, v in st.items() if v is not None or k in ("state",)})
    rem = out.get("remaining_s")
    if rem is not None:
        out["remaining_min"] = int(round(rem / 60.0))
    try:
        out["progress"] = min(1.0, max(0.0, float(out.get("progress") or 0)))
    except (TypeError, ValueError):
        out["progress"] = 0.0
    return out


class PrinterAdapter:
    """Base class. Subclasses override what their protocol supports."""

    protocol = "base"
    manufacturer = ""
    file_extensions = (".gcode", ".gco", ".g")
    supports = {}  # capability -> True for what the protocol can do; see CAPABILITY_KEYS

    def __init__(self, printer: dict, pins=None):
        self.p = printer
        self.pins = pins
        self.last_seen = 0.0
        self.last_error = ""
        self.connected = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        """Start background connections (WebSocket, MQTT...). Must not block for long."""

    def disconnect(self) -> None:
        """Stop background connections."""

    # ------------------------------------------------------------ monitoring

    def fetch_status(self) -> Optional[dict]:
        """Protocol-specific status, or None when the printer is unreachable. Subclasses."""
        raise NotImplementedError

    def get_status(self) -> Optional[dict]:
        """Normalized status (see empty_status), or None when the printer does not answer."""
        try:
            st = self.fetch_status()
        except Exception as e:  # network errors are normal: the printer may be off
            self.last_error = str(e)[:300] or type(e).__name__
            self.connected = False
            return None
        if st is None:
            self.connected = False
            return None
        self.connected = True
        self.last_seen = time.time()
        if st.get("state") is None and set(st) == {"state"}:
            return st  # e.g. Bambu without serial number: camera only, no status
        return finish_status(st)

    def get_temperature(self) -> Dict[str, dict]:
        st = self.get_status() or {}
        return st.get("temperatures") or {}

    def get_progress(self) -> dict:
        st = self.get_status() or {}
        return {k: st.get(k) for k in ("state", "file", "progress", "duration", "remaining_s", "layer")}

    # ------------------------------------------------------------ capabilities

    def allowed(self, key: str) -> bool:
        """Owner's permission from config.json (validated with per-protocol defaults)."""
        return bool(self.p.get(key, False))

    def get_capabilities(self) -> Dict[str, bool]:
        caps = {k: bool(self.supports.get(k, False)) for k in CAPABILITY_KEYS}
        if not self.allowed("allow_remote_control"):
            for k in CONTROL_CAPS + ("can_control_temperature",):
                caps[k] = False
        if not self.allowed("allow_file_upload"):
            caps["can_upload"] = False
        if self.p.get("webcam_url"):
            caps["has_camera"] = True
        return caps

    def require(self, cap: str) -> None:
        if not self.supports.get(cap):
            raise NotSupported("%s (%s) cannot do this: %s" % (self.p["name"], self.protocol, cap))
        if not self.get_capabilities().get(cap):
            what = "uploads" if cap == "can_upload" else "remote control"
            raise NotAuthorized("%s is turned off for %s. Allow it in Devices > Edit if you want it."
                                % (what.capitalize(), self.p["name"]))

    def accepts_file(self, name: str) -> bool:
        return name.lower().endswith(tuple(self.file_extensions))

    # ------------------------------------------------------------ files and control

    def upload_file(self, path: str, name: str, start: bool = False) -> str:
        raise NotSupported("%s cannot receive files" % self.p["name"])

    def start_print(self, filename: str) -> str:
        raise NotSupported("%s cannot start prints remotely" % self.p["name"])

    def pause_print(self) -> str:
        raise NotSupported("%s cannot pause remotely" % self.p["name"])

    def resume_print(self) -> str:
        raise NotSupported("%s cannot resume remotely" % self.p["name"])

    def cancel_print(self) -> str:
        raise NotSupported("%s cannot cancel remotely" % self.p["name"])

    def list_files(self) -> List[dict]:
        raise NotSupported("%s cannot list its files" % self.p["name"])

    def get_history(self, limit: int = 20) -> List[dict]:
        return []

    # ------------------------------------------------------------ camera and diagnostics

    def camera_source(self) -> Optional[Callable]:
        """Function(hub) that feeds camera frames into a CameraHub, or None.
        Default: the Camera URL override (any HTTP MJPEG stream or JPEG snapshot)."""
        if not self.p.get("webcam_url"):
            return None
        from .. import cameras, klipper
        p = self.p
        return lambda hub: cameras.klipper_camera(hub, lambda: klipper.webcam_urls(p))

    def diagnostics(self) -> dict:
        return {
            "protocol": self.protocol,
            "connected": self.connected,
            "last_seen": int(self.last_seen) or None,
            "last_error": self.last_error,
        }

    def info(self) -> dict:
        """Registration record (no secrets) for the API and the dashboard."""
        p = self.p
        return {
            "id": p["id"], "name": p["name"], "protocol": self.protocol,
            "manufacturer": p.get("manufacturer") or self.manufacturer,
            "model": p.get("model", ""), "location": p.get("location", ""),
            "host": p["host"], "capabilities": self.get_capabilities(),
            "file_extensions": list(self.file_extensions),
            "connected": self.connected, "last_seen": int(self.last_seen) or None,
        }
