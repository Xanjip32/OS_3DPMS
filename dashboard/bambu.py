"""Bambu Lab printers (P1/A1/X1) over LAN: FTPS upload and MQTT print start.

Needs the LAN access code from the printer screen. Recent firmware may also
need "LAN Only Mode" / "Developer Mode" for third-party tools.
"""
import ftplib
import json
import logging
import re
import socket
import ssl
import struct
import threading
import time
import uuid
import zipfile

from . import config, tlspin

log = logging.getLogger("dashboard.bambu")


class BambuFTPS(ftplib.FTP_TLS):
    """Implicit FTPS (port 990) with TLS session reuse and certificate pinning."""

    def __init__(self, pins, **kwargs):
        self._sock = None
        self.pins = pins
        super().__init__(context=tlspin.unverified_context(), **kwargs)

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
            self.pins.check(value, self.host, self.port)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size

    def store(self, name, f):
        # storbinary() calls unwrap(), which Bambu printers never answer.
        self.voidcmd("TYPE I")
        conn = self.transfercmd("STOR " + name)
        try:
            while True:
                buf = f.read(65536)
                if not buf:
                    break
                conn.sendall(buf)
        finally:
            conn.close()
        return self.voidresp()


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by printer")
        buf += chunk
    return bytes(buf)


def mqtt_publish(host, access_code, topic, payload, pins):
    """Minimal MQTT 3.1.1 over TLS: CONNECT, PUBLISH (QoS 0), DISCONNECT."""
    with tlspin.connect(host, 8883, pins) as s:
        s.sendall(_connect_packet("os3dpms-" + uuid.uuid4().hex[:8], access_code))
        try:
            ack = _recv_exact(s, 4)
        except ConnectionError:
            raise RuntimeError("printer closed the connection: " + MQTT_REFUSED[5]) from None
        if ack[0] != 0x20 or ack[3] != 0:
            raise RuntimeError("printer refused the print command login: %s"
                               % MQTT_REFUSED.get(ack[3], "code %d" % ack[3]))
        pub = _s16(topic.encode()) + payload
        s.sendall(bytes([0x30]) + _remaining(len(pub)) + pub)
        time.sleep(0.5)
        try:
            s.sendall(b"\xe0\x00")
        except (OSError, socket.error):
            pass


def start_print(p, path, name, pins):
    if name.lower().endswith(".3mf"):
        plate = "Metadata/plate_1.gcode"
        try:
            with zipfile.ZipFile(path) as z:
                plates = sorted(n for n in z.namelist() if re.match(r"Metadata/plate_\d+\.gcode$", n))
                if plates:
                    plate = plates[0]
        except zipfile.BadZipFile:
            pass
        cmd = {"print": {
            "sequence_id": "0", "command": "project_file", "param": plate,
            "url": "file:///sdcard/" + name, "subtask_name": name,
            "bed_type": "auto", "timelapse": False, "bed_leveling": True,
            "flow_cali": False, "vibration_cali": True, "layer_inspect": False,
            "use_ams": bool(p.get("use_ams")), "ams_mapping": [],
            "profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0",
        }}
    else:
        cmd = {"print": {"sequence_id": "0", "command": "gcode_file", "param": "/sdcard/" + name}}
    mqtt_publish(p["host"], config.secret(p, "access_code"),
                 "device/%s/request" % p["serial"], json.dumps(cmd).encode(), pins)


def upload(p, path, name, start, pins):
    ftp = BambuFTPS(pins, timeout=60)
    ftp.connect(p["host"], 990)
    try:
        ftp.login("bblp", config.secret(p, "access_code"))
        ftp.prot_p()
        with open(path, "rb") as f:
            ftp.store(name, f)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    if not start:
        return "Uploaded to %s SD card" % p["name"]
    if not p.get("serial"):
        return "Uploaded to %s SD card. Add its serial number in setup to start prints." % p["name"]
    start_print(p, path, name, pins)
    return "Uploaded to %s and print started" % p["name"]


# ---------------------------------------------------------------- live status over MQTT

STATE_MAP = {"RUNNING": "printing", "PREPARE": "printing", "SLICING": "printing",
             "PAUSE": "paused", "FINISH": "complete", "FAILED": "error", "IDLE": "standby"}
# print_error values that mean "stopped from the printer screen or Bambu Studio", not a failure.
CANCELLED_ERRORS = {0x0300400C, 0x0500400E}
MQTT_REFUSED = {
    1: "the printer does not accept this MQTT version",
    2: "the printer refused the client id",
    3: "the printer's MQTT service is unavailable",
    4: "wrong LAN access code",
    5: "not authorized: wrong LAN access code, or LAN Only Mode / Developer Mode is off "
       "(newer firmware). Read the code again on the printer screen",
}


class LoginRefused(RuntimeError):
    """The printer rejected the LAN access code."""


def error_code(value):
    """Bambu's own notation for a print error, for example 0300-400C."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value)
    return "%04X-%04X" % ((value >> 16) & 0xFFFF, value & 0xFFFF)


def _connect_packet(client_id, access_code):
    var = _s16(b"MQTT") + bytes([4, 0xC2]) + struct.pack(">H", 60)
    body = _s16(client_id.encode()) + _s16(b"bblp") + _s16(access_code.encode("utf-8"))
    return bytes([0x10]) + _remaining(len(var) + len(body)) + var + body


def check_login(host, access_code, pins, timeout=8):
    """(ok, text) for one MQTT login with this access code. Used by the setup check."""
    if not access_code:
        return False, "no LAN access code saved"
    with tlspin.connect(host, 8883, pins, timeout=timeout) as s:
        s.sendall(_connect_packet("os3dpms-check-" + uuid.uuid4().hex[:6], access_code))
        buf = bytearray()
        deadline = time.time() + timeout
        while True:
            pkt = _parse_packet(buf)
            if pkt:
                break
            if time.time() > deadline:
                return False, "printer did not answer the login"
            data = s.recv(4096)
            if not data:
                return False, "printer closed the connection during login (" + MQTT_REFUSED[5] + ")"
            buf += data
        try:
            s.sendall(b"\xe0\x00")  # DISCONNECT
        except OSError:
            pass
    kind, ack = pkt
    if kind >> 4 != 2 or len(ack) < 2:
        return False, "unexpected answer from the printer"
    if ack[1] == 0:
        return True, "access code accepted"
    return False, MQTT_REFUSED.get(ack[1], "login refused (code %d)" % ack[1])


class BambuWatcher(threading.Thread):
    """Keeps an MQTT connection to the printer and the latest print report."""

    PING_SECONDS = 30
    PUSHALL_SECONDS = 600

    def __init__(self, p, pins):
        super().__init__(daemon=True, name="bambu-%s" % p["id"])
        self.p = p
        self.pins = pins
        self.report = {}
        self.lock = threading.Lock()
        self.connected = False
        self.last_report = 0.0
        self.error = None  # why the last status connection failed, for the setup check
        self.stopped = threading.Event()
        self.sock = None
        self.buf = bytearray()

    def stop(self):
        self.stopped.set()
        sock = self.sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def status(self):
        """Same shape as klipper.status(), or None when the printer is unreachable."""
        with self.lock:
            r = dict(self.report)
            fresh = self.connected or time.time() - self.last_report < 60
        if not r or not fresh:
            return None
        state = STATE_MAP.get(str(r.get("gcode_state", "")).upper(), "standby")
        try:
            err = int(r.get("print_error") or 0)
        except (TypeError, ValueError):
            err = 0
        if state == "error" and err in CANCELLED_ERRORS:
            state = "cancelled"
        try:
            progress = min(100.0, max(0.0, float(r.get("mc_percent") or 0))) / 100.0
        except (TypeError, ValueError):
            progress = 0.0
        return {
            "state": state,
            "file": r.get("subtask_name") or r.get("gcode_file") or "",
            "progress": progress,
            "remaining_min": r.get("mc_remaining_time") or 0,
            "message": ("error %s" % error_code(err)) if err and state == "error" else "",
        }

    def run(self):
        while not self.stopped.is_set():
            wait = 10
            try:
                self._session()
            except LoginRefused as e:
                # Wrong access code: retrying often does not help and may make the printer block us.
                self.error = str(e)
                log.info("bambu %s status: %s", self.p["id"], e)
                wait = 120
            except Exception as e:
                self.error = str(e) or type(e).__name__
                log.info("bambu %s status: %s", self.p["id"], self.error)
            self.connected = False
            self.stopped.wait(wait)

    def _send(self, data):
        self.sock.sendall(data)

    def _session(self):
        p = self.p
        topic_report = ("device/%s/report" % p["serial"]).encode()
        topic_request = ("device/%s/request" % p["serial"]).encode()
        self.buf = bytearray()
        with tlspin.connect(p["host"], 8883, self.pins, timeout=15) as s:
            self.sock = s
            self._send(_connect_packet("os3dpms-w-" + uuid.uuid4().hex[:8], config.secret(p, "access_code")))
            try:
                pkt = self._next_packet(s, wait=15)
            except ConnectionError:
                # The printer hangs up instead of answering when it does not accept the code.
                raise LoginRefused("printer closed the status connection during login: %s" % MQTT_REFUSED[5]) from None
            if pkt is None:
                raise ConnectionError("printer did not answer MQTT login")
            kind, ack = pkt
            if kind >> 4 != 2 or len(ack) < 2 or ack[1] != 0:
                code = ack[1] if len(ack) > 1 else -1
                raise LoginRefused("printer refused the status login (code %d): %s" % (
                    code, MQTT_REFUSED.get(code, "unexpected answer")))
            sub = struct.pack(">H", 1) + _s16(topic_report) + b"\x00"
            self._send(bytes([0x82]) + _remaining(len(sub)) + sub)
            self._pushall(topic_request)
            self.connected = True
            self.error = None
            last_ping = last_push = time.time()
            s.settimeout(2)
            while not self.stopped.is_set():
                now = time.time()
                if now - last_ping > self.PING_SECONDS:
                    self._send(b"\xc0\x00")
                    last_ping = now
                if now - last_push > self.PUSHALL_SECONDS:
                    self._pushall(topic_request)
                    last_push = now
                pkt = self._next_packet(s, wait=0)
                if pkt and pkt[0] >> 4 == 3:
                    self._on_publish(pkt[0], pkt[1])

    def _pushall(self, topic):
        msg = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}).encode()
        pub = _s16(topic) + msg
        self._send(bytes([0x30]) + _remaining(len(pub)) + pub)

    def _next_packet(self, s, wait):
        """Return (first byte, body) of the next MQTT packet, or None on a read timeout."""
        deadline = time.time() + wait
        while True:
            pkt = _parse_packet(self.buf)
            if pkt:
                return pkt
            try:
                data = s.recv(65536)
            except socket.timeout:
                if time.time() >= deadline:
                    return None
                continue
            if not data:
                raise ConnectionError("printer closed the connection")
            self.buf += data
            if len(self.buf) > 4_000_000:
                raise ConnectionError("MQTT message too large")

    def _on_publish(self, first, body):
        qos = (first >> 1) & 3
        tlen = struct.unpack(">H", body[:2])[0]
        payload = body[2 + tlen + (2 if qos else 0):]
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        report = data.get("print") if isinstance(data, dict) else None
        if isinstance(report, dict):
            with self.lock:
                self.report.update(report)
                self.last_report = time.time()


def _s16(b):
    return struct.pack(">H", len(b)) + b


def _remaining(n):
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _parse_packet(buf):
    """Pop one complete MQTT packet from buf, or return None if it is incomplete."""
    if len(buf) < 2:
        return None
    length, mult, i = 0, 1, 1
    while True:
        if i >= len(buf):
            return None
        digit = buf[i]
        length += (digit & 0x7F) * mult
        i += 1
        if not digit & 0x80:
            break
        mult *= 128
        if i > 4:
            raise ConnectionError("bad MQTT packet length")
    if len(buf) < i + length:
        return None
    first = buf[0]
    body = bytes(buf[i:i + length])
    del buf[:i + length]
    return first, body
