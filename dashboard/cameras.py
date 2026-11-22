"""Camera readers. One upstream connection per printer, shared by all viewers."""
import base64
import http.client
import logging
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse

from . import klipper, tlspin

log = logging.getLogger("dashboard.camera")

IDLE_SECONDS = 20               # default; config.json "camera": {"idle_seconds": ...}
SNAPSHOT_INTERVAL = 0.2         # default wait between snapshot-URL pictures ("snapshot_interval_s")
MAX_FRAME_BYTES = 10_000_000
RETRY_SECONDS = (3, 5, 10, 20, 30)  # wait between attempts after 1, 2, 3... failures in a row


class CameraHub:
    """Starts on the first viewer request, stops after idle_seconds without viewers.

    always_on keeps the camera connection open without viewers (camera_always_on), so the first
    picture is instant and the bed check always has a fresh picture; it costs camera and network
    load all the time."""

    def __init__(self, printer_id, source, idle_seconds=IDLE_SECONDS, always_on=False,
                 snapshot_interval=SNAPSHOT_INTERVAL):
        self.printer_id = printer_id
        self.source = source
        self.idle_seconds = idle_seconds
        self.always_on = always_on
        self.snapshot_interval = snapshot_interval
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
        return not self.stopped and (self.always_on or time.time() - self.last_request < self.idle_seconds)

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

    def latest(self):
        with self.cond:
            return self.frame, self.frame_time

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


CONTENT_LENGTH_RE = re.compile(rb"(?im)^content-length[ \t]*:[ \t]*(\d{1,9})[ \t]*\r?$")
HEADER_KEEP = 4096   # bytes before a picture kept for its part headers


def read_mjpeg(stream, hub):
    """Cut JPEG frames out of an MJPEG stream.

    When the part headers give a Content-Length, exactly that many bytes are the picture (so a
    thumbnail inside the JPEG, which has its own end marker, cannot cut it short). Without one,
    or when it does not fit, the picture runs from its start marker to the next end marker."""
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
                del buf[:-HEADER_KEEP]
                break
            length = _part_length(buf, start)
            if length:
                if len(buf) < start + length:
                    if start > HEADER_KEEP:
                        del buf[:start - HEADER_KEEP]
                    break
                frame = bytes(buf[start:start + length])
                if frame.endswith(b"\xff\xd9"):
                    hub.push(frame)
                    del buf[:start + length]
                    continue
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start > HEADER_KEEP:
                    del buf[:start - HEADER_KEEP]
                break
            hub.push(bytes(buf[start:end + 2]))
            del buf[:end + 2]
        if len(buf) > MAX_FRAME_BYTES:
            buf.clear()


def _part_length(buf, start):
    """Content-Length from the part headers just before the picture at start, or 0."""
    head = bytes(buf[max(0, start - HEADER_KEEP):start])
    cut = head.rfind(b"\r\n\r\n")
    if cut < 0 or head[cut + 4:].strip():
        return 0       # the picture does not follow the headers directly: not a reliable length
    head = head[:cut + 2]
    found = CONTENT_LENGTH_RE.findall(head[max(0, head.rfind(b"--")):] if b"--" in head else head)
    if not found:
        return 0
    length = int(found[-1])
    return length if 4 <= length <= MAX_FRAME_BYTES else 0


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


class _SnapshotPoller:
    """Fetch the same snapshot URL again and again over one kept-alive HTTP connection.

    Falls back to a new connection (with the usual redirect rules) when the camera closes it or
    answers anything but a JPEG."""

    def __init__(self, url):
        parts = urllib.parse.urlsplit(url)
        self.url = url
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port
        self.path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        self.auth = None
        if parts.username is not None:
            cred = "%s:%s" % (urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""))
            self.auth = "Basic " + base64.b64encode(cred.encode()).decode()
        self.conn = None
        self.reused = 0      # pictures fetched on an already open connection (for tests)

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _keepalive(self):
        if self.scheme not in ("http", "https"):
            return None
        fresh = self.conn is None
        if fresh:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            self.conn = cls(self.host, self.port, timeout=10)
        headers = {"Connection": "keep-alive", "Accept": "image/jpeg"}
        if self.auth:
            headers["Authorization"] = self.auth
        self.conn.request("GET", self.path, headers=headers)
        r = self.conn.getresponse()
        data = r.read(MAX_FRAME_BYTES + 1)
        if r.status != 200 or data[:2] != b"\xff\xd8" or len(data) > MAX_FRAME_BYTES:
            self.close()
            return None
        if r.will_close:
            self.close()
        elif not fresh:
            self.reused += 1
        return data

    def get(self):
        for _ in range(2):          # a kept-alive connection the camera has closed: once more, new
            try:
                data = self._keepalive()
                if data is not None:
                    return data
                break
            except (OSError, http.client.HTTPException):
                self.close()
        with _open(self.url) as s:   # plain request: redirects, error messages
            return s.read(MAX_FRAME_BYTES)


def _stream_from(hub, url):
    """Show pictures from one camera URL (MJPEG stream or JPEG snapshot) while viewers remain."""
    with _open(url) as r:
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype.startswith("image/"):
            first = r.read(MAX_FRAME_BYTES)
            if first[:2] != b"\xff\xd8":
                raise ConnectionError("not a JPEG picture")
            hub.push(first)
            poller = _SnapshotPoller(r.geturl() or url)
            try:
                while hub.active():  # snapshot URL: poll it at snapshot_interval_s
                    due = time.monotonic() + hub.snapshot_interval
                    data = poller.get()
                    if data[:2] == b"\xff\xd8":
                        hub.push(data)
                    with hub.cond:    # wait out the rest of the interval (a stop ends it at once)
                        hub.cond.wait_for(lambda: hub.stopped, max(0.0, due - time.monotonic()))
            finally:
                poller.close()
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


def bambu_camera(hub, host, access_code, pins, port=6000):
    """P1/A1 camera: TLS port 6000, 80-byte auth packet, then JPEGs with a 16-byte header.

    The header starts with the picture size (little endian). Headers with size 0 are
    keep-alives: skip them, like Bambu's own clients do.
    """
    if not access_code:
        raise ConnectionError("no LAN access code saved for this printer")
    try:
        s = tlspin.connect(host, port, pins)
    except ConnectionRefusedError:
        if _rtsp_only(host):
            raise ConnectionError("this printer streams its camera over RTSP (port 322; X1, H2D and P2S "
                                  "models). Run a camera bridge such as go2rtc on this PC and enter its "
                                  "MJPEG or JPEG address as Camera URL override") from None
        raise ConnectionError("printer refused the camera port %d. Turn on" % port + " LAN Mode Liveview "
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
