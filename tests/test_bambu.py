"""Bambu Lab camera, status and login handling, against a scripted fake printer socket."""
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile
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


SLICE_INFO = """<?xml version="1.0"?><config><plate>
<metadata key="index" value="2"/>
<filament id="1" type="PLA"/><filament id="3" type="PETG"/>
</plate></config>"""


def make_3mf(entries):
    fd, path = tempfile.mkstemp(suffix=".3mf")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


class SendSink:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)


class PrintCommandTest(unittest.TestCase):
    P = {"name": "P1S", "serial": "01P00A000000001", "use_ams": True}

    def test_sliced_3mf_uses_its_plate_and_ams_mapping(self):
        path = make_3mf({"Metadata/plate_2.gcode": "G28", "Metadata/slice_info.config": SLICE_INFO})
        try:
            cmd = bambu.build_print_command(self.P, path, "part.gcode.3mf", "42")["print"]
        finally:
            os.remove(path)
        self.assertEqual(cmd["param"], "Metadata/plate_2.gcode")
        self.assertEqual(cmd["ams_mapping"], [0, 2])
        self.assertEqual(cmd["sequence_id"], "42")
        self.assertTrue(cmd["use_ams"])

    def test_unsliced_3mf_is_refused_before_upload(self):
        path = make_3mf({"3D/3dmodel.model": "<model/>"})
        try:
            with self.assertRaises(ValueError) as cm:
                bambu.upload(self.P, path, "project.3mf", True, pins=None)
        finally:
            os.remove(path)
        self.assertIn("Export plate sliced file", str(cm.exception))

    def test_gcode_command(self):
        cmd = bambu.build_print_command(self.P, "x", "a.gcode", "7")["print"]
        self.assertEqual(cmd, {"sequence_id": "7", "command": "gcode_file", "param": "/sdcard/a.gcode"})

    def test_authorization_refusal_explains_developer_mode(self):
        with self.assertRaises(bambu.StartRefused) as cm:
            bambu.check_reply({"command": "project_file", "result": "fail",
                               "reason": "mqtt message verify failed", "err_code": 84033543})
        self.assertIn("Developer Mode", str(cm.exception))
        bambu.check_reply({"command": "project_file", "result": "success"})


class WatcherRequestTest(unittest.TestCase):
    def watcher(self):
        w = bambu.BambuWatcher({"id": "p1s", "serial": "S1", "host": "x"}, pins=None)
        w.sock = SendSink()
        w.connected = True
        w.report = {"gcode_state": "IDLE"}
        return w

    def publish_later(self, w, obj, delay=0.2):
        def go():
            time.sleep(delay)
            w._on_publish(0x30, bambu._s16(b"device/S1/report") + json.dumps(obj).encode())
        threading.Thread(target=go, daemon=True).start()

    def test_answer_is_matched_and_kept_out_of_the_status(self):
        w = self.watcher()
        self.publish_later(w, {"print": {"command": "project_file", "sequence_id": "9",
                                         "result": "fail", "reason": "nope"}})
        reply, _state = w.request("device/S1/request", b"{}", "9", "project_file", timeout=3)
        self.assertEqual(reply["result"], "fail")
        self.assertNotIn("result", w.report)
        self.assertTrue(w.sock.sent)

    def test_printer_starting_counts_as_confirmed(self):
        w = self.watcher()
        self.publish_later(w, {"print": {"command": "push_status", "gcode_state": "PREPARE"}})
        t = time.time()
        reply, state = w.request("device/S1/request", b"{}", "10", "project_file", timeout=5)
        self.assertIsNone(reply)
        self.assertEqual(state, "PREPARE")
        self.assertLess(time.time() - t, 3)

    def test_no_answer_is_not_reported_as_started(self):
        w = self.watcher()
        with self.assertRaises(bambu.StartRefused) as cm:
            bambu.start_print({"name": "P1S", "serial": "S1", "host": "x"}, "x", "a.gcode", None,
                              watcher=w, timeout=1)
        self.assertIn("did not confirm", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
