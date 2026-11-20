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
            try:  # the file goes over this connection: it must be the same printer as the login
                self.pins.check(conn, self.host, self.port)
            except Exception:
                conn.close()
                raise
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


class StartRefused(RuntimeError):
    """The printer did not accept or did not confirm the print command."""


# err_code values the printer sends back when Authorization Control blocks a LAN command.
_AUTH_ERRORS = {"84033543", "84033545"}
_AUTH_HELP = ("On the printer screen turn on Settings > LAN Only Mode and then Developer Mode "
              "(newer firmware blocks print start from other apps otherwise). The file is on the "
              "SD card, so you can also start it from the printer screen.")
_STARTED = ("PREPARE", "RUNNING", "SLICING")


def _new_seq():
    return str(int(time.time() * 1000) % 1_000_000_000)


def sliced_plate(path):
    """(gcode entry, filament ids) of the first sliced plate in a .3mf; (None, []) if none."""
    try:
        with zipfile.ZipFile(path) as z:
            plates = sorted((n for n in z.namelist() if re.match(r"Metadata/plate_\d+\.gcode$", n)),
                            key=lambda n: int(re.search(r"(\d+)", n).group(1)))
            if not plates:
                return None, []
            plate = plates[0]
            num = int(re.search(r"(\d+)", plate).group(1))
            filaments = []
            try:
                info = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
                blocks = re.findall(r"<plate>.*?</plate>", info, re.S)
                block = next((b for b in blocks if re.search(r'key="index" value="%d"' % num, b)), info)
                filaments = sorted({int(i) for i in re.findall(r'<filament id="(\d+)"', block)})
            except (KeyError, ValueError):
                pass
            return plate, filaments
    except zipfile.BadZipFile:
        raise ValueError("this .3mf file is damaged (not a zip archive)") from None


def build_print_command(p, path, name, seq):
    if name.lower().endswith(".3mf"):
        plate, filaments = sliced_plate(path)
        if not plate:
            raise ValueError("this .3mf has no sliced plate. In Bambu Studio slice it, then use "
                             "File > Export > Export plate sliced file (.gcode.3mf)")
        use_ams = bool(p.get("use_ams"))
        # Filament n of the project goes to AMS slot n-1, Bambu Studio's default mapping.
        mapping = [max(0, f - 1) for f in filaments] if (use_ams and filaments) else [0]
        return {"print": {
            "sequence_id": seq, "command": "project_file", "param": plate,
            "url": "file:///sdcard/" + name, "subtask_name": name,
            "bed_type": "auto", "timelapse": False, "bed_leveling": True,
            "flow_cali": False, "vibration_cali": True, "layer_inspect": False,
            "use_ams": use_ams, "ams_mapping": mapping,
            "profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0",
        }}
    return {"print": {"sequence_id": seq, "command": "gcode_file", "param": "/sdcard/" + name}}


def check_reply(reply):
    """Raise StartRefused if the printer's answer to a print command is a failure."""
    if not reply:
        return
    result = str(reply.get("result", "")).lower()
    err = str(reply.get("err_code") or "")
    if result in ("fail", "failed", "error") or (err and err != "0"):
        reason = str(reply.get("reason") or "")
        auth = err in _AUTH_ERRORS or "verify" in reason.lower() or "auth" in reason.lower()
        msg = "printer refused to start: %s" % (reason or ("authorization required" if auth else "no reason given"))
        if err and err != "0":
            msg += " (code %s)" % err
        if auth:
            msg += ". " + _AUTH_HELP
        raise StartRefused(msg)


def _match_reply(data, seq, command):
    r = data.get("print") if isinstance(data, dict) else None
    if isinstance(r, dict) and str(r.get("sequence_id")) == seq and r.get("command") == command:
        return r
    return None


def mqtt_request(host, access_code, serial, payload, seq, command, pins, timeout=12):
    """One MQTT session: subscribe to reports, publish the command, wait for the printer's answer.

    Returns (answer dict or None, latest gcode_state)."""
    topic_report = ("device/%s/report" % serial).encode()
    topic_request = ("device/%s/request" % serial).encode()
    buf = bytearray()
    state = ""
    with tlspin.connect(host, 8883, pins, timeout=15) as s:
        s.sendall(_connect_packet("os3dpms-" + uuid.uuid4().hex[:8], access_code))
        try:
            ack = _recv_exact(s, 4)
        except ConnectionError:
            raise RuntimeError("printer closed the connection: " + MQTT_REFUSED[5]) from None
        if ack[0] != 0x20 or ack[3] != 0:
            raise RuntimeError("printer refused the print command login: %s"
                               % MQTT_REFUSED.get(ack[3], "code %d" % ack[3]))
        sub = struct.pack(">H", 1) + _s16(topic_report) + b"\x00"
        s.sendall(bytes([0x82]) + _remaining(len(sub)) + sub)
        pub = _s16(topic_request) + payload
        s.sendall(bytes([0x30]) + _remaining(len(pub)) + pub)
        s.settimeout(2)
        deadline = time.time() + timeout
        reply = None
        while reply is None and time.time() < deadline:
            pkt = _parse_packet(buf)
            if pkt is None:
                try:
                    data = s.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                if len(buf) > 4_000_000:
                    break
                continue
            first, body = pkt
            if first >> 4 != 3:
                continue
            tlen = struct.unpack(">H", body[:2])[0]
            raw = body[2 + tlen + (2 if (first >> 1) & 3 else 0):]
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            reply = _match_reply(data, seq, command)
            r = data.get("print") if isinstance(data, dict) else None
            if isinstance(r, dict) and r.get("gcode_state"):
                state = str(r["gcode_state"]).upper()
                if state in _STARTED:
                    break
        try:
            s.sendall(b"\xe0\x00")
        except OSError:
            pass
    return reply, state


def start_print(p, path, name, pins, watcher=None, timeout=12):
    """Send the print command and wait until the printer confirms it. Raises StartRefused."""
    seq = _new_seq()
    cmd = build_print_command(p, path, name, seq)
    payload = json.dumps(cmd).encode()
    command = cmd["print"]["command"]
    topic = "device/%s/request" % p["serial"]
    if watcher is not None and watcher.connected:
        reply, state = watcher.request(topic, payload, seq, command, timeout)
    else:
        reply, state = mqtt_request(p["host"], config.secret(p, "access_code"), p["serial"],
                                    payload, seq, command, pins, timeout)
    check_reply(reply)
    if reply is None and state not in _STARTED:
        raise StartRefused("the printer did not confirm the print within %d s. %s" % (timeout, _AUTH_HELP))


def upload(p, path, name, start, pins, watcher=None):
    if start and p.get("serial") and name.lower().endswith(".3mf"):
        build_print_command(p, path, name, "0")  # refuse a project that cannot print before uploading
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
    try:
        start_print(p, path, name, pins, watcher)
    except StartRefused as e:
        raise StartRefused("Uploaded to %s SD card, but %s" % (p["name"], e)) from None
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
        self.send_lock = threading.Lock()
        self.replies = {}
        self.reply_cond = threading.Condition()

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
        with self.send_lock:
            self.sock.sendall(data)

    def request(self, topic, payload, seq, command, timeout=12):
        """Publish a command on the open status connection and wait for the printer's answer.

        Returns (answer dict or None, latest gcode_state)."""
        key = (seq, command)
        with self.lock:
            before = str(self.report.get("gcode_state", "")).upper()
        with self.reply_cond:
            self.replies[key] = None
        try:
            pub = _s16(topic.encode()) + payload
            self._send(bytes([0x30]) + _remaining(len(pub)) + pub)
            deadline = time.time() + timeout
            with self.reply_cond:
                while self.replies.get(key) is None:
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    self.reply_cond.wait(min(left, 1))
                    with self.lock:
                        now = str(self.report.get("gcode_state", "")).upper()
                    if now != before and now in _STARTED:
                        break  # some firmware starts without echoing the command
        finally:
            with self.reply_cond:
                reply = self.replies.pop(key, None)
        with self.lock:
            state = str(self.report.get("gcode_state", "")).upper()
        return reply, state

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
        if isinstance(report, dict) and report.get("command") not in (None, "push_status"):
            key = (str(report.get("sequence_id")), report.get("command"))
            with self.reply_cond:
                if key in self.replies:
                    self.replies[key] = report
                    self.reply_cond.notify_all()
            return  # answers to commands are not status reports
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
