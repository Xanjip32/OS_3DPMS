"""Camera readers. One upstream connection per printer, shared by all viewers."""
import logging
import socket
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
        self.trying_url = None  # camera URL being tried (Klipper)
        self.good_url = None    # last camera URL that gave a picture

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
            if self.trying_url:
                self.good_url = self.trying_url
            self.frame = jpeg
            self.frame_time = time.time()
            self.error = None
            self.cond.notify_all()

    def wait_frame(self, newer_than, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.stopped or self.frame_time > newer_than, timeout)
            return self.frame, self.frame_time

    def snapshot(self):
        self.touch()
        cutoff = time.time() - 3
        frame, ts = self.wait_frame(cutoff, 8)
        return frame if ts > cutoff and not self.stopped else None

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


def _stream_from(hub, url):
    """Show pictures from one camera URL (MJPEG stream or JPEG snapshot) while viewers remain."""
    with urllib.request.urlopen(url, timeout=10) as r:
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype.startswith("image/"):
            first = r.read(MAX_FRAME_BYTES)
            if first[:2] != b"\xff\xd8":
                raise ConnectionError("not a JPEG picture")
            hub.push(first)
            while hub.active():  # snapshot URL: poll it
                time.sleep(0.2)
                with urllib.request.urlopen(url, timeout=10) as s:
                    hub.push(s.read(MAX_FRAME_BYTES))
            return
        if ctype and not ctype.startswith(("multipart/", "application/octet-stream", "video/")):
            raise ConnectionError("not a camera stream (%s)" % ctype.split(";")[0])
        read_mjpeg(r, hub)


def klipper_camera(hub, urls):
    """Try each camera URL until one gives pictures; the one that worked is tried first next time.

    `urls` is a list, or a function that returns one (it may ask the printer, so it runs here,
    in the camera thread).
    """
    if callable(urls):
        urls = urls()
    if hub.good_url in urls:
        urls = [hub.good_url] + [u for u in urls if u != hub.good_url]
    failures = []
    for url in urls:
        if not hub.active():
            return
        before = hub.frame_time
        hub.trying_url = url
        try:
            _stream_from(hub, url)
            return
        except Exception as e:
            if hub.frame_time > before:  # it worked, then stopped: start over (this URL goes first)
                raise
            failures.append((url, e))
        finally:
            hub.trying_url = None
    if not failures:
        raise ConnectionError("no camera address to try")
    url, error = failures[0]
    more = " (and %d other addresses)" % (len(failures) - 1) if len(failures) > 1 else ""
    raise ConnectionError("no picture from %s: %s%s" % (url, error, more))


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
        first = True
        while hub.active():
            try:
                header = _recv_exact(s, 16)
            except socket.timeout:
                if not first:
                    raise
                raise ConnectionError("printer accepted the camera connection but sent no picture. "
                                      "Check the LAN access code and turn on LAN Mode Liveview "
                                      "on the printer") from None
            except ConnectionError:
                if not first:
                    raise
                raise ConnectionError("printer closed the camera connection before the first picture. "
                                      "Either the LAN access code is wrong, or another app is already "
                                      "watching this camera (old printer-dashboard, Bambu Studio or "
                                      "Bambu Handy): P1 and A1 printers allow one camera viewer") from None
            first = False
            size = struct.unpack("<I", header[:4])[0]
            if size <= 0 or size > MAX_FRAME_BYTES:
                raise ConnectionError("bad frame size %d (check access code)" % size)
            img = _recv_exact(s, size)
            if img[:2] == b"\xff\xd8":
                hub.push(img)
