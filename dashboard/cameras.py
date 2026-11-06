"""Camera readers. One upstream connection per printer, shared by all viewers."""
import logging
import struct
import threading
import time
import urllib.request

from . import tlspin

log = logging.getLogger("dashboard.camera")

IDLE_SECONDS = 20
MAX_FRAME_BYTES = 10_000_000


class CameraHub:
    """Starts on the first viewer request, stops after IDLE_SECONDS without viewers."""

    def __init__(self, printer_id, source):
        self.printer_id = printer_id
        self.source = source
        self.frame = None
        self.frame_time = 0.0
        self.error = None
        self.last_request = 0.0
        self.stopped = False
        self.cond = threading.Condition()
        self.thread = None

    def active(self):
        return not self.stopped and time.time() - self.last_request < IDLE_SECONDS

    def touch(self):
        with self.cond:
            if self.stopped:
                return
            self.last_request = time.time()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()

    def push(self, jpeg):
        with self.cond:
            self.frame = jpeg
            self.frame_time = time.time()
            self.error = None
            self.cond.notify_all()

    def wait_frame(self, newer_than, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.stopped or self.frame_time > newer_than, timeout)
            return self.frame, self.frame_time

    def _run(self):
        while self.active():
            try:
                self.source(self)
            except Exception as e:  # offline camera, wrong URL... retry
                with self.cond:
                    self.error = str(e)
                log.warning("camera %s: %s", self.printer_id, e)
                time.sleep(3)


def read_mjpeg(stream, hub):
    """Cut JPEG frames out of an MJPEG stream by their start/end markers."""
    read = getattr(stream, "read1", None) or stream.read
    buf = bytearray()
    while hub.active():
        chunk = read(65536)
        if not chunk:
            raise ConnectionError("camera stream ended")
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                del buf[:-1]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del buf[:start]
                break
            hub.push(bytes(buf[start:end + 2]))
            del buf[:end + 2]
        if len(buf) > MAX_FRAME_BYTES:
            buf.clear()


def klipper_camera(hub, url):
    with urllib.request.urlopen(url, timeout=10) as r:
        if r.headers.get("Content-Type", "").startswith("image/"):
            hub.push(r.read(MAX_FRAME_BYTES))
            while hub.active():  # snapshot URL: poll it
                time.sleep(0.2)
                with urllib.request.urlopen(url, timeout=10) as s:
                    hub.push(s.read(MAX_FRAME_BYTES))
            return
        read_mjpeg(r, hub)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by printer")
        buf += chunk
    return bytes(buf)


def bambu_camera(hub, host, access_code, pins):
    """P1/A1 camera: TLS port 6000, 80-byte auth packet, then length-prefixed JPEGs."""
    with tlspin.connect(host, 6000, pins) as s:
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
        auth += b"bblp".ljust(32, b"\0")
        auth += access_code.encode().ljust(32, b"\0")
        s.sendall(auth)
        while hub.active():
            header = _recv_exact(s, 16)
            size = struct.unpack("<I", header[:4])[0]
            if size <= 0 or size > MAX_FRAME_BYTES:
                raise ConnectionError("bad frame size %d (check access code)" % size)
            img = _recv_exact(s, size)
            if img[:2] == b"\xff\xd8":
                hub.push(img)
