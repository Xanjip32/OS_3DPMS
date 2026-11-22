"""OctoPrint (Marlin) and PrusaLink adapters against fake servers."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import adapters  # noqa: E402
from dashboard.adapters import octoprint  # noqa: E402
from tests.mock_http_printers import MockOctoPrint, MockPrusaLink  # noqa: E402


def gcode_file(test, body=b"G28\nG1 X10\n"):
    fd, path = tempfile.mkstemp(suffix=".gcode")
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    test.addCleanup(os.remove, path)
    return path


class OctoPrintTests(unittest.TestCase):
    def setUp(self):
        self.srv = MockOctoPrint()
        self.addCleanup(self.srv.stop)

    def test_status_and_temperatures(self):
        st = adapters.create(self.srv.printer()).get_status()
        self.assertEqual(st["state"], "printing")
        self.assertAlmostEqual(st["progress"], 0.425)
        self.assertEqual(st["temperatures"]["nozzle"], {"actual": 199.5, "target": 200.0})
        self.assertEqual(st["remaining_s"], 1800)
        self.assertEqual(st["file"], "cube.gcode")

    def test_state_mapping(self):
        self.assertEqual(octoprint.state_from({"state": {"flags": {"operational": True}}},
                                              {"progress": {"completion": 100}})[0], "complete")
        self.assertEqual(octoprint.state_from({"state": {"flags": {"operational": True}}},
                                              {"progress": {"completion": 37}})[0], "standby")
        self.assertEqual(octoprint.state_from({"state": {"flags": {"paused": True}}}, {})[0], "paused")
        self.assertEqual(octoprint.state_from({"state": {"text": "Error: thermal runaway",
                                                         "flags": {"error": True}}}, {})[0], "error")

    def test_printer_not_connected_to_octoprint(self):
        self.srv.connected = False
        st = adapters.create(self.srv.printer()).get_status()
        self.assertEqual(st["state"], "offline")
        self.assertIn("not connected", st["message"])

    def test_wrong_api_key(self):
        a = adapters.create(self.srv.printer(api_key="wrong"))
        self.assertIsNone(a.get_status())
        self.assertIn("API key", a.last_error)

    def test_upload_and_start(self):
        a = adapters.create(self.srv.printer())
        self.srv.flags.update(printing=False, operational=True)
        msg = a.upload_file(gcode_file(self), "part.gcode", start=True)
        self.assertIn("print started", msg)
        self.assertIn(b'name="print"', self.srv.uploads[-1])
        self.assertIn(b"G1 X10", self.srv.uploads[-1])

    def test_upload_with_start_refused_while_printing(self):
        a = adapters.create(self.srv.printer())
        with self.assertRaises(adapters.PrinterBusy):
            a.upload_file(gcode_file(self), "part.gcode", start=True)
        self.assertEqual(self.srv.uploads, [])

    def test_controls(self):
        a = adapters.create(self.srv.printer())
        a.pause_print()
        a.resume_print()
        a.cancel_print()
        bodies = [c[2] for c in self.srv.calls if c[0] == "POST" and c[1] == "/api/job"]
        self.assertEqual(bodies, [b'{"command": "pause", "action": "pause"}',
                                  b'{"command": "pause", "action": "resume"}', b'{"command": "cancel"}'])

    def test_controls_off(self):
        a = adapters.create(self.srv.printer(allow_remote_control=False, allow_file_upload=False))
        caps = a.get_capabilities()
        self.assertFalse(caps["can_cancel"] or caps["can_upload"])
        with self.assertRaises(adapters.NotAuthorized):
            a.cancel_print()
        with self.assertRaises(adapters.NotAuthorized):
            a.upload_file(gcode_file(self), "x.gcode")
        self.assertFalse([c for c in self.srv.calls if c[0] == "POST"])

    def test_file_exists_and_list(self):
        a = adapters.create(self.srv.printer())
        self.assertTrue(a.file_exists("cube.gcode"))
        self.assertFalse(a.file_exists("nope.gcode"))
        self.assertEqual(a.list_files()[0]["name"], "cube.gcode")

    def test_webcam_stays_on_the_printer(self):
        a = adapters.create(self.srv.printer())
        urls = a.webcam_urls()
        self.assertTrue(urls[0].startswith("http://127.0.0.1:%d/webcam" % self.srv.port))


class PrusaLinkTests(unittest.TestCase):
    def setUp(self):
        self.srv = MockPrusaLink()
        self.addCleanup(self.srv.stop)

    def test_digest_login_and_status(self):
        st = adapters.create(self.srv.printer()).get_status()
        self.assertEqual(st["state"], "printing")
        self.assertEqual(st["file"], "part.bgcode")
        self.assertEqual(st["temperatures"]["bed"], {"actual": 60.2, "target": 60})
        self.assertAlmostEqual(st["progress"], 0.3)
        self.assertEqual(st["fan"], 0.8)

    def test_wrong_password(self):
        a = adapters.create(self.srv.printer(password="nope"))
        self.assertIsNone(a.get_status())
        self.assertIn("password", a.last_error)

    def test_upload_does_not_overwrite(self):
        a = adapters.create(self.srv.printer())
        self.srv.state = "IDLE"
        path = gcode_file(self)
        self.assertIn("Uploaded", a.upload_file(path, "new.gcode"))
        self.assertEqual(self.srv.uploads, [b"G28\nG1 X10\n"])  # body sent once, after the challenge
        with self.assertRaises(adapters.AdapterError):
            a.upload_file(path, "new.gcode")
        self.assertTrue(a.file_exists("new.gcode"))
        self.assertFalse(a.file_exists("other.gcode"))

    def test_pause_resume_stop_use_the_job_id(self):
        a = adapters.create(self.srv.printer())
        a.pause_print()
        a.resume_print()
        a.cancel_print()
        calls = [(c[0], c[1]) for c in self.srv.calls if c[0] in ("PUT", "DELETE")]
        self.assertEqual(calls, [("PUT", "/api/v1/job/7/pause"), ("PUT", "/api/v1/job/7/resume"),
                                 ("DELETE", "/api/v1/job/7")])

    def test_states(self):
        a = adapters.create(self.srv.printer())
        for raw, state in (("IDLE", "standby"), ("FINISHED", "complete"), ("ATTENTION", "paused"),
                           ("ERROR", "error"), ("STOPPED", "cancelled")):
            self.srv.state = raw
            self.assertEqual(a.get_status()["state"], state, raw)


if __name__ == "__main__":
    unittest.main()
