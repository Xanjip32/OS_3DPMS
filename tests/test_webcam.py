"""Webcam: frame cap for remote viewers, kept-alive snapshot polling, MJPEG Content-Length, idle stop."""
import http.client
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import cameras, config  # noqa: E402
from tests.test_dashboard import USER, ServerFixture  # noqa: E402

# A JPEG with an embedded thumbnail (its own start and end markers), as many cameras send in EXIF.
THUMB = b"\xff\xd8thumb\xff\xd9"
FRAME = b"\xff\xd8\xff\xe1" + THUMB + b"main-picture-data" * 20 + b"\xff\xd9"


class FakeHub:
    def __init__(self, limit):
        self.frames = []
        self.limit = limit

    def active(self):
        return len(self.frames) < self.limit

    def push(self, jpeg):
        self.frames.append(jpeg)


class Chunks:
    """A stream that hands out data in small pieces, like a slow network."""

    def __init__(self, data, size):
        self.data, self.size, self.pos = data, size, 0

    def read(self, n):
        piece = self.data[self.pos:self.pos + min(n, self.size)]
        self.pos += len(piece)
        return piece


def part(frame, length=True, value=None):
    head = b"--frame\r\nContent-Type: image/jpeg\r\n"
    if length:
        head += b"Content-Length: %d\r\n" % (len(frame) if value is None else value)
    return head + b"\r\n" + frame + b"\r\n"


class MjpegTests(unittest.TestCase):
    def read(self, data, limit, size=65536):
        hub = FakeHub(limit)
        try:
            cameras.read_mjpeg(Chunks(data, size), hub)
        except ConnectionError:
            pass     # stream ended
        return hub.frames

    def test_content_length_keeps_a_picture_with_a_thumbnail_whole(self):
        for size in (7, 100, 65536):
            self.assertEqual(self.read(part(FRAME) * 3, 3, size), [FRAME] * 3, size)

    def test_without_content_length_the_markers_are_used(self):
        plain = b"\xff\xd8plain-picture\xff\xd9"
        self.assertEqual(self.read(part(plain, length=False) * 2, 2, 5), [plain] * 2)
        # (the old marker rule: a thumbnail cuts the picture short, as before this change)
        self.assertEqual(self.read(part(FRAME, length=False), 1)[0], FRAME[:FRAME.index(b"\xff\xd9") + 2])

    def test_a_wrong_content_length_falls_back_to_the_markers(self):
        plain = b"\xff\xd8plain-picture\xff\xd9"
        self.assertEqual(self.read(part(plain, value=len(plain) - 3) + part(plain), 2), [plain] * 2)
        self.assertEqual(self.read(part(plain, value=0) + part(plain), 2), [plain] * 2)
        self.assertEqual(self.read(part(plain, value=99999999999) + part(plain), 2), [plain] * 2)

    def test_headers_in_an_earlier_chunk_are_kept(self):
        data = part(FRAME) + b"x" * 70000 + part(FRAME)      # junk between parts larger than a read
        self.assertEqual(self.read(data, 2, 65536), [FRAME, FRAME])


class _Snapshots(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.server.connections += 1

    def do_GET(self):
        self.server.requests += 1
        if self.server.close_every and self.server.requests % self.server.close_every == 0:
            self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(FRAME)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(FRAME)


class SnapshotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, close_every=0):
        self.connections = self.requests = 0
        self.close_every = close_every
        super().__init__(("127.0.0.1", 0), _Snapshots)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def url(self):
        return "http://127.0.0.1:%d/snapshot.jpg" % self.server_address[1]


class SnapshotPollingTests(unittest.TestCase):
    def run_for(self, server, seconds, interval=0.05):
        hub = cameras.CameraHub("cam", lambda h: cameras._stream_from(h, server.url()), snapshot_interval=interval)
        hub.touch()
        time.sleep(seconds)
        hub.stop()
        hub.thread.join(5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return hub

    def test_one_connection_is_kept_for_the_pictures(self):
        server = SnapshotServer()
        hub = self.run_for(server, 0.8)
        self.assertGreater(server.requests, 5)
        self.assertLessEqual(server.connections, 2)          # the first request (urllib) + one kept alive
        self.assertEqual(hub.frame, FRAME)

    def test_the_interval_is_configurable(self):
        server = SnapshotServer()
        self.run_for(server, 1.0, interval=0.5)
        self.assertLessEqual(server.requests, 4)

    def test_a_camera_that_closes_the_connection_still_works(self):
        server = SnapshotServer(close_every=2)
        self.run_for(server, 0.8)
        self.assertGreater(server.requests, 5)
        self.assertGreater(server.connections, 2)

    def test_a_refused_keep_alive_falls_back_to_a_plain_request(self):
        poller = cameras._SnapshotPoller("http://127.0.0.1:1/x.jpg")
        with mock.patch.object(cameras, "_open") as fallback:
            fallback.return_value.__enter__.return_value.read.return_value = FRAME
            self.assertEqual(poller.get(), FRAME)
        fallback.assert_called_once()


class IdleTests(unittest.TestCase):
    def test_idle_seconds_and_always_on(self):
        hub = cameras.CameraHub("a", None, idle_seconds=5)
        hub.last_request = time.time() - 6
        self.assertFalse(hub.active())
        hub.idle_seconds = 10
        self.assertTrue(hub.active())
        on = cameras.CameraHub("b", None, always_on=True)
        self.assertTrue(on.active())
        on.stop()
        self.assertFalse(on.active())

    def test_camera_config(self):
        base = {"printers": [{"id": "v", "type": "klipper", "host": "127.0.0.1"}]}
        cfg = config.validate(base)
        self.assertEqual(cfg["camera"], {"remote_max_fps": 5, "snapshot_interval_s": 0.2, "idle_seconds": 20})
        self.assertFalse(cfg["printers"][0]["camera_always_on"])
        cfg = config.validate(dict(base, camera={"remote_max_fps": 12, "idle_seconds": 120}))
        self.assertEqual((cfg["camera"]["remote_max_fps"], cfg["camera"]["idle_seconds"]), (12, 120))
        for bad in ({"remote_max_fps": 0}, {"remote_max_fps": 31}, {"snapshot_interval_s": "x"},
                    {"idle_seconds": 1}, {"remote_max_fps": True}, []):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.validate(dict(base, camera=bad))


class StreamCapTests(ServerFixture):
    def count_frames(self, admin, seconds=1.2):
        port = self.app.cfg["admin_port"] if admin else self.app.cfg["port"]
        h = {"Host": "127.0.0.1:%d" % port} if admin else {"Host": "printers.tail1234.ts.net",
                                                            "Tailscale-User-Login": USER}
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/cam/voron", headers=h)
        resp = conn.getresponse()
        data = b""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            data += resp.read1(65536)
        conn.close()
        return data.count(b"--frame\r\n")

    def test_remote_viewers_get_at_most_remote_max_fps(self):
        old = self.app.hubs["voron"]
        stop = threading.Event()

        def fast(hub):        # a camera with 50 pictures a second
            n = 0
            while hub.active() and not stop.is_set():
                n += 1
                hub.push(b"\xff\xd8%d\xff\xd9" % n)
                time.sleep(0.02)

        self.app.hubs["voron"] = cameras.CameraHub("voron", fast)
        self.addCleanup(self.app.hubs.__setitem__, "voron", old)
        self.addCleanup(self.app.hubs["voron"].stop)
        self.addCleanup(stop.set)
        with mock.patch.dict(self.app.cfg["camera"], {"remote_max_fps": 4}):
            remote = self.count_frames(admin=False)
        local = self.count_frames(admin=True)
        self.assertGreaterEqual(remote, 2)
        self.assertLessEqual(remote, 7)            # 4 a second over 1.2 s, plus the first one
        self.assertGreater(local, 20)              # the PC itself gets every picture


if __name__ == "__main__":
    unittest.main()
