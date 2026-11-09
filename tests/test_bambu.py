"""Bambu Lab camera, status and login handling, against a scripted fake printer socket."""
import os
import struct
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import bambu, cameras  # noqa: E402
from tests.mock_printer import FRAME  # noqa: E402


class FakeSocket:
    """Plays back `chunks` from recv(); b"" means the printer hung up."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > n:
            self.chunks.insert(0, chunk[n:])
            chunk = chunk[:n]
        return chunk

    def settimeout(self, t):
        pass

    def close(self):
        pass


def frame_packet(data):
    return struct.pack("<IIII", len(data), 0, 0, 0) + data


class BambuCameraTests(unittest.TestCase):
    def hub(self, frames_wanted=1):
        hub = cameras.CameraHub("p1s", None)
        hub.last_request = time.time()
        pushed = []

        def push(jpeg):
            pushed.append(jpeg)
            if len(pushed) >= frames_wanted:
                hub.stopped = True  # ends the reader loop
        hub.push = push
        return hub, pushed

    def run_camera(self, sock, hub, code="12345678"):
        with mock.patch.object(cameras.tlspin, "connect", return_value=sock):
            cameras.bambu_camera(hub, "192.0.2.6", code, pins=None)

    def test_sends_auth_and_reads_frames(self):
        sock = FakeSocket([frame_packet(FRAME)])
        hub, pushed = self.hub()
        self.run_camera(sock, hub)
        self.assertEqual(pushed, [FRAME])
        self.assertEqual(len(sock.sent), 80)
        self.assertEqual(sock.sent[16:20], b"bblp")
        self.assertEqual(sock.sent[48:56], b"12345678")

    def test_empty_frame_headers_are_skipped(self):
        # Bambu printers send headers with size 0 between pictures; that is not an error.
        sock = FakeSocket([frame_packet(b""), frame_packet(b""), frame_packet(FRAME)])
        hub, pushed = self.hub()
        self.run_camera(sock, hub)
        self.assertEqual(pushed, [FRAME])

    def test_hang_up_before_first_picture_explains_access_code(self):
        hub, _ = self.hub()
        with self.assertRaises(ConnectionError) as ctx:
            self.run_camera(FakeSocket([]), hub)
        self.assertIn("access code", str(ctx.exception))
        self.assertIn("one camera viewer", str(ctx.exception))

    def test_missing_access_code(self):
        hub, _ = self.hub()
        with self.assertRaisesRegex(ConnectionError, "no LAN access code"):
            self.run_camera(FakeSocket([]), hub, code="")

    def test_rtsp_model_is_named(self):
        hub, _ = self.hub()
        with mock.patch.object(cameras.tlspin, "connect", side_effect=ConnectionRefusedError), \
                mock.patch.object(cameras, "_rtsp_only", return_value=True):
            with self.assertRaisesRegex(ConnectionError, "RTSP"):
                cameras.bambu_camera(hub, "192.0.2.6", "12345678", pins=None)

    def test_oversized_frame_is_rejected(self):
        sock = FakeSocket([struct.pack("<IIII", cameras.MAX_FRAME_BYTES + 1, 0, 0, 0)])
        hub, _ = self.hub()
        with self.assertRaisesRegex(ConnectionError, "bad frame size"):
            self.run_camera(sock, hub)


class BambuStatusTests(unittest.TestCase):
    def watcher(self, report):
        w = bambu.BambuWatcher({"id": "p1s", "host": "192.0.2.6", "serial": "01P00A000000001"}, pins=None)
        w.report = report
        w.connected = True
        return w

    def test_printing(self):
        st = self.watcher({"gcode_state": "RUNNING", "mc_percent": 42, "subtask_name": "benchy",
                           "mc_remaining_time": 17}).status()
        self.assertEqual((st["state"], st["progress"], st["file"], st["remaining_min"]),
                         ("printing", 0.42, "benchy", 17))

    def test_cancel_from_printer_is_not_an_error(self):
        st = self.watcher({"gcode_state": "FAILED", "print_error": 0x0300400C}).status()
        self.assertEqual(st["state"], "cancelled")
        self.assertEqual(st["message"], "")

    def test_failure_shows_bambu_error_code(self):
        st = self.watcher({"gcode_state": "FAILED", "print_error": 0x07008011}).status()
        self.assertEqual(st["state"], "error")
        self.assertEqual(st["message"], "error 0700-8011")

    def test_bad_progress_value(self):
        self.assertEqual(self.watcher({"gcode_state": "RUNNING", "mc_percent": "x"}).status()["progress"], 0)

    def test_no_report_means_unknown(self):
        w = self.watcher({})
        self.assertIsNone(w.status())

    def test_hang_up_during_login_is_a_refused_code(self):
        w = bambu.BambuWatcher({"id": "p1s", "host": "192.0.2.6", "serial": "01P00A000000001",
                                "access_code": "wrong123"}, pins=None)
        with mock.patch.object(bambu.tlspin, "connect", return_value=FakeSocket([])):
            with self.assertRaises(bambu.LoginRefused) as ctx:
                w._session()
        self.assertIn("access code", str(ctx.exception))


class BambuLoginTests(unittest.TestCase):
    def check(self, answer):
        sock = FakeSocket(answer)
        with mock.patch.object(bambu.tlspin, "connect", return_value=sock):
            return bambu.check_login("192.0.2.6", "12345678", pins=None), sock

    def test_accepted(self):
        (ok, text), sock = self.check([b"\x20\x02\x00\x00"])
        self.assertTrue(ok)
        self.assertEqual(sock.sent[0], 0x10)          # CONNECT
        self.assertIn(b"bblp", sock.sent)
        self.assertTrue(sock.sent.endswith(b"\xe0\x00"))  # DISCONNECT

    def test_wrong_code(self):
        (ok, text), _ = self.check([b"\x20\x02\x00\x05"])
        self.assertFalse(ok)
        self.assertIn("access code", text)

    def test_hang_up(self):
        (ok, text), _ = self.check([])
        self.assertFalse(ok)
        self.assertIn("closed", text)

    def test_error_code_format(self):
        self.assertEqual(bambu.error_code(50348044), "0300-400C")
        self.assertEqual(bambu.error_code(None), "None")


if __name__ == "__main__":
    unittest.main()
