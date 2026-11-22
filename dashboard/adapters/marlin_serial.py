"""Marlin printers over USB serial, directly (optional; OctoPrint is the recommended route).

Read-only monitoring: firmware info (M115), temperatures (M105 or M155 auto-report),
position (M114), SD print progress (M27), print time (M31), endstops (M119) and the
current settings (M503, read once). It never sends commands that move, heat, start,
pause or stop anything: `send()` refuses every G-code outside READ_COMMANDS.

Safety rules from the brief:
* Direct serial is an explicit choice (`type: marlin_serial` with a `serial_port`).
* One owner per port: the port is opened exclusively, and a second adapter in this
  process for the same port is refused. If another program (OctoPrint, a slicer) holds
  the port, the open fails and we say so instead of fighting for it.
* No unnecessary reset: DTR/RTS are kept low when opening. Some boards (Arduino-style
  USB chips, e.g. the Kossel's Trigorilla) still reset on any port open; connect the
  first time while no SD print runs (docs/ADAPTERS.md).
* Needs `pyserial` only when this printer type is used (`pip install pyserial`).
"""
import logging
import re
import threading
import time

from .base import AdapterError, PrinterAdapter

log = logging.getLogger("dashboard.adapters.marlin_serial")

READ_COMMANDS = ("M115", "M105", "M114", "M119", "M27", "M503", "M155", "M154", "M31")
TEMP_RE = re.compile(r"\b([TBC]\d?):\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)")
POS_RE = re.compile(r"X:(-?\d+(?:\.\d+)?)\s*Y:(-?\d+(?:\.\d+)?)\s*Z:(-?\d+(?:\.\d+)?)")
SD_RE = re.compile(r"SD printing byte (\d+)/(\d+)")
TIME_RE = re.compile(r"Print time:\s*(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?")
_owners = {}
_owners_lock = threading.Lock()


def parse_m115(lines):
    """(firmware dict, capability dict) from the M115 answer."""
    fw, caps = {}, {}
    for line in lines:
        if "FIRMWARE_NAME:" in line:
            for key in ("FIRMWARE_NAME", "SOURCE_CODE_URL", "PROTOCOL_VERSION", "MACHINE_TYPE",
                        "EXTRUDER_COUNT", "UUID"):
                m = re.search(r"%s:(.*?)(?=\s+[A-Z_]+:|$)" % key, line)
                if m:
                    fw[key.lower()] = m.group(1).strip()
        m = re.match(r"\s*Cap:([A-Z0-9_]+):([01])", line)
        if m:
            caps[m.group(1)] = m.group(2) == "1"
    return fw, caps


def parse_temps(line):
    temps = {}
    for name, actual, target in TEMP_RE.findall(line):
        label = {"T": "nozzle", "B": "bed", "C": "chamber"}.get(name, "nozzle" + name[1:])
        if name == "T0" and "nozzle" in temps:
            continue
        temps[label] = {"actual": float(actual), "target": float(target)}
    return temps


def parse_print_time(line):
    m = TIME_RE.search(line)
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def open_pyserial(port, baudrate):
    try:
        import serial  # optional dependency, only for this printer type
    except ImportError:
        raise AdapterError("direct serial needs pyserial: run  python -m pip install pyserial") from None
    s = serial.Serial()
    s.port, s.baudrate, s.timeout, s.write_timeout = port, baudrate, 0.5, 2
    s.dtr = False  # keep DTR/RTS low: avoids the reset-on-open of many boards where possible
    s.rts = False
    try:
        s.exclusive = True  # POSIX lock; Windows COM ports are exclusive anyway
    except (AttributeError, ValueError):
        pass
    try:
        s.open()
    except Exception as e:
        raise AdapterError("cannot open %s (in use by another program such as OctoPrint or a slicer?): %s"
                           % (port, e)) from None
    return s


class MarlinSerialAdapter(PrinterAdapter):
    protocol = "marlin_serial"
    manufacturer = "Marlin"
    supports = {"monitor": True}   # read-only by design: no upload or print control over raw serial
    POLL_S = 3.0
    STALE_S = 20.0

    def __init__(self, printer, pins=None, opener=None):
        super().__init__(printer, pins)
        self.opener = opener or open_pyserial
        self.port = None
        self.stopped = threading.Event()
        self.thread = None
        self.firmware, self.fw_caps = {}, {}
        self.unsupported = set()
        self.temps, self.position, self.sd, self.print_time = {}, None, None, None
        self.endstops, self.settings = {}, []
        self.last_line = 0.0
        self.io_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def connect(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="marlin-%s" % self.p["id"])
            self.thread.start()

    def disconnect(self):
        self.stopped.set()
        self._close()

    def _claim(self):
        key = self.p["serial_port"].lower()
        with _owners_lock:
            owner = _owners.get(key)
            if owner is not None and owner is not self:
                raise AdapterError("%s is already used by printer %s" % (self.p["serial_port"], owner.p["id"]))
            _owners[key] = self

    def _release(self):
        with _owners_lock:
            if _owners.get(self.p["serial_port"].lower()) is self:
                del _owners[self.p["serial_port"].lower()]

    def _close(self):
        port, self.port = self.port, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        self._release()

    def open(self):
        self._claim()
        try:
            self.port = self.opener(self.p["serial_port"], self.p.get("baudrate", 115200))
        except Exception:
            self._release()
            raise
        time.sleep(0.1)
        self._drain()
        lines = self.send("M115", timeout=5)
        self.firmware, self.fw_caps = parse_m115(lines)
        if not self.firmware and not self.fw_caps:
            raise AdapterError("no Marlin answer to M115 on %s (wrong port or baud rate?)" % self.p["serial_port"])
        if self.fw_caps.get("AUTOREPORT_TEMP"):
            self.send("M155 S2")   # report temperatures every 2 s without being asked
        if self.fw_caps.get("AUTOREPORT_POS"):
            self.send("M154 S5")
        try:
            self.settings = [ln for ln in self.send("M503", timeout=5) if ln.strip()][:120]
        except AdapterError:
            self.settings = []

    def _loop(self):
        backoff = 2.0
        while not self.stopped.is_set():
            try:
                if self.port is None:
                    self.open()
                    backoff = 2.0
                    self.last_error = ""
                self.poll()
                self.stopped.wait(self.POLL_S)
            except Exception as e:
                self.last_error = str(e)[:200] or type(e).__name__
                log.info("marlin %s: %s", self.p["id"], self.last_error)
                self._close()
                self.stopped.wait(backoff)
                backoff = min(60.0, backoff * 2)

    # ------------------------------------------------------------ serial I/O

    def _readline(self):
        raw = self.port.readline()
        if not raw:
            return None
        line = raw.decode("ascii", "replace").strip()
        if line:
            self.last_line = time.time()
            self._observe(line)
        return line

    def _drain(self):
        for _ in range(50):
            if self._readline() is None:
                return

    def _observe(self, line):
        """Auto-reports can arrive between answers: parse every line we see."""
        if TEMP_RE.search(line) and ("T:" in line or "B:" in line):
            temps = parse_temps(line)
            if temps:
                self.temps = temps
        m = POS_RE.search(line)
        if m and (line.startswith("X:") or "Count" in line):
            self.position = [float(m.group(1)), float(m.group(2)), float(m.group(3))]
        m = SD_RE.search(line)
        if m:
            self.sd = (int(m.group(1)), int(m.group(2)))
        elif "Not SD printing" in line:
            self.sd = None
        if "Print time" in line:
            self.print_time = parse_print_time(line)

    def send(self, command, timeout=3.0):
        """Send one read-only command and return the lines up to its `ok`."""
        word = command.split()[0].upper()
        if word not in READ_COMMANDS:
            raise AdapterError("refused: %s is not a read-only command" % word)
        if word in self.unsupported:
            raise AdapterError("%s is not supported by this firmware" % word)
        if self.port is None:
            raise AdapterError("serial port not open")
        with self.io_lock:
            self.port.write((command + "\n").encode("ascii"))
            lines, deadline = [], time.time() + timeout
            while time.time() < deadline:
                line = self._readline()
                if line is None:
                    continue
                if line.startswith("ok"):
                    return lines + [line]
                if "Unknown command" in line:
                    self.unsupported.add(word)
                lines.append(line)
                if len(lines) > 500:
                    break
        raise AdapterError("no answer to %s within %.0f s" % (word, timeout))

    def poll(self):
        if not self.fw_caps.get("AUTOREPORT_TEMP"):
            self.send("M105")
        if "M27" not in self.unsupported:
            self.send("M27")
        if not self.fw_caps.get("AUTOREPORT_POS") and "M114" not in self.unsupported:
            self.send("M114")
        if self.sd and "M31" not in self.unsupported:
            self.send("M31")

    # ------------------------------------------------------------ adapter interface

    def fetch_status(self):
        if self.port is None or time.time() - self.last_line > self.STALE_S:
            if self.last_error:
                raise AdapterError(self.last_error)
            return None
        printing = self.sd is not None and self.sd[1] > 0
        return {
            "state": "printing" if printing else "standby",
            "file": "(SD card)" if printing else "",
            "progress": (self.sd[0] / self.sd[1]) if printing else 0,
            "duration": self.print_time or 0,
            "temperatures": dict(self.temps), "position": self.position, "transport": "serial",
        }

    def query_endstops(self):
        lines = self.send("M119")
        self.endstops = dict(ln.split(":", 1) for ln in lines if ":" in ln and not ln.startswith("ok"))
        return self.endstops

    def diagnostics(self):
        d = super().diagnostics()
        d.update({"serial_port": self.p["serial_port"], "baudrate": self.p.get("baudrate", 115200),
                  "firmware": self.firmware, "firmware_capabilities": self.fw_caps,
                  "unsupported_commands": sorted(self.unsupported), "settings_m503": self.settings[:60]})
        return d
