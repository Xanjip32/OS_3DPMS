"""Bambu Lab printers (P1/A1/X1) over LAN: FTPS upload and MQTT print start.

Needs the LAN access code from the printer screen. Recent firmware may also
need "LAN Only Mode" / "Developer Mode" for third-party tools.
"""
import ftplib
import json
import re
import socket
import ssl
import struct
import time
import uuid
import zipfile

from . import config, tlspin


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
    def s16(b):
        return struct.pack(">H", len(b)) + b

    def remaining(n):
        out = bytearray()
        while True:
            digit = n % 128
            n //= 128
            out.append(digit | (0x80 if n else 0))
            if not n:
                return bytes(out)

    with tlspin.connect(host, 8883, pins) as s:
        var = s16(b"MQTT") + bytes([4, 0xC2]) + struct.pack(">H", 60)
        body = (s16(("os3dpms-" + uuid.uuid4().hex[:8]).encode())
                + s16(b"bblp") + s16(access_code.encode()))
        s.sendall(bytes([0x10]) + remaining(len(var) + len(body)) + var + body)
        ack = _recv_exact(s, 4)
        if ack[0] != 0x20 or ack[3] != 0:
            raise RuntimeError("printer refused MQTT login (code %d)" % ack[3])
        pub = s16(topic.encode()) + payload
        s.sendall(bytes([0x30]) + remaining(len(pub)) + pub)
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
