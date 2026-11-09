"""Camera readers. One upstream connection per printer, shared by all viewers."""
import logging
import socket
import struct
import threading
import time
import urllib.error

from . import klipper, tlspin

log = logging.getLogger("dashboard.camera")

IDLE_SECONDS = 20
MAX_FRAME_BYTES = 10_000_000
RETRY_SECONDS = (3, 5, 10, 20, 30)  # wait between attempts after 1, 2, 3... failures in a row


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
                self.error = None  # an old error may no longer be true: try again first
                self.thread = threading.Thread(target=self._run, daemon=True, name="camera-%s" % self.printer_id)
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
        """Latest picture (at most 3 s old), or None.

        Waits up to 8 s for the first picture. When the reader already knows the camera is
        failing it answers within about a second instead, so a broken camera does not hold a
        browser connection that the working cameras need.
        """
        self.touch()
        cutoff = time.time() - 3
        with self.cond:
            failing = self.error is not None and self.frame_time <= cutoff
        frame, ts = self.wait_frame(cutoff, 1 if failing else 8)
        return frame if ts > cutoff and not self.stopped else None

    def _run(self):
        failures = 0
        while self.active():
            before = self.frame_time
            try:
                self.source(self)
                failures = 0
            except Exception as e:  # offline camera, wrong URL... retry, more slowly each time
                if self.frame_time > before:
                    failures = 0  # it worked for a while: the camera is not broken
                with self.cond:
                    self.error = str(e) or type(e).__name__
                log.warning("camera %s: %s", self.printer_id, self.error)
                delay = RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)]
                failures += 1
                with self.cond:
                    self.cond.wait_for(lambda: self.stopped, delay)


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


def _open(url):
    """urlopen that closes error responses (they hold a socket) and explains them."""
    try:
        return klipper.opener.open(url, timeout=10)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code >= 500:
            raise ConnectionError(
                "the printer's camera service answered HTTP %d (%s): it is running but has no "
                "picture. Check the camera cable, then restart crowsnest on the printer "
                "(Mainsail: Machine > Services)" % (e.code, e.reason)) from None
        if e.code == 404:
            raise ConnectionError("HTTP 404: no camera at this address") from None
        raise ConnectionError("HTTP %d %s" % (e.code, e.reason)) from None


def _stream_from(hub, url):
    """Show pictures from one camera URL (MJPEG stream or JPEG snapshot) while viewers remain."""
    with _open(url) as r:
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype.startswith("image/"):
            first = r.read(MAX_FRAME_BYTES)
            if first[:2] != b"\xff\xd8":
                raise ConnectionError("not a JPEG picture")
            hub.push(first)
            while hub.active():  # snapshot URL: poll it
                time.sleep(0.2)
                with _open(url) as s:
                    data = s.read(MAX_FRAME_BYTES)
                if data[:2] == b"\xff\xd8":
                    hub.push(data)
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


BAMBU_WRONG_CODE = ("printer closed the camera connection before the first picture. Either the LAN "
                    "access code is wrong (it changes when LAN mode is turned off and on, so read it "
                    "again on the printer screen), or another app is already watching this camera "
                    "(Bambu Studio, Bambu Handy or another viewer): P1 and A1 printers allow one "
                    "camera viewer at a time")
BAMBU_NO_PICTURE = ("printer accepted the camera connection but sent no picture. Turn on "
                    "LAN Mode Liveview on the printer screen and check the LAN access code")


def _rtsp_only(host):
    """True when the printer has the RTSP camera port (X1, H2D, P2S) instead of port 6000."""
    try:
        with socket.create_connection((host, 322), timeout=2):
            return True
    except OSError:
        return False


def bambu_camera(hub, host, access_code, pins):
    """P1/A1 camera: TLS port 6000, 80-byte auth packet, then JPEGs with a 16-byte header.

    The header starts with the picture size (little endian). Headers with size 0 are
    keep-alives: skip them, like Bambu's own clients do.
    """
    if not access_code:
        raise ConnectionError("no LAN access code saved for this printer")
    try:
        s = tlspin.connect(host, 6000, pins)
    except ConnectionRefusedError:
        if _rtsp_only(host):
            raise ConnectionError("this printer streams its camera over RTSP (port 322; X1, H2D and P2S "
                                  "models). Run a camera bridge such as go2rtc on this PC and enter its "
                                  "MJPEG or JPEG address as Camera URL override") from None
        raise ConnectionError("printer refused the camera port 6000. Turn on LAN Mode Liveview "
                              "on the printer screen") from None
    with s:
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
        auth += b"bblp".ljust(32, b"\0")
        auth += access_code.encode("utf-8")[:32].ljust(32, b"\0")
        s.sendall(auth)
        first = True
        empty = 0
        while hub.active():
            try:
                header = _recv_exact(s, 16)
            except socket.timeout:
                if not first:
                    raise ConnectionError("camera stopped sending pictures") from None
                raise ConnectionError(BAMBU_NO_PICTURE) from None
            except ConnectionError:
                if not first:
                    raise
                raise ConnectionError(BAMBU_WRONG_CODE) from None
            size = struct.unpack("<I", header[:4])[0]
            if size == 0:
                empty += 1
                if empty > 1000:
                    raise ConnectionError("camera sends only empty frames")
                continue
            if size > MAX_FRAME_BYTES:
                raise ConnectionError("bad frame size %d from the printer camera" % size)
            img = _recv_exact(s, size)
            if img[:2] == b"\xff\xd8":
                first = False
                empty = 0
                hub.push(img)
