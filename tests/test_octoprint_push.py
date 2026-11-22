"""OctoPrint push channel (raw WebSocket at /sockjs/websocket) with REST polling as fallback."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import adapters, config  # noqa: E402
from dashboard.adapters import octoprint  # noqa: E402
from tests.mock_http_printers import MockOctoPrint  # noqa: E402


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class OctoPrintPushTests(unittest.TestCase):
    def start(self, push=True, **extra):
        self.srv = MockOctoPrint(websocket=push)
        self.addCleanup(self.srv.stop)
        adapter = adapters.create(self.srv.printer(**extra))
        adapter.connect()
        self.addCleanup(adapter.disconnect)
        return adapter

    def test_status_comes_over_the_websocket(self):
        a = self.start()
        self.assertTrue(wait_for(a.ws_fresh), a.ws_error)
        self.assertEqual(self.srv.ws_auth, ["op:sess-1"])      # passive login session, never the API key
        rest_calls = len([c for c in self.srv.calls if c[1] in ("/api/printer", "/api/job")])
        st = a.get_status()
        self.assertEqual((st["transport"], st["state"], st["file"]), ("websocket", "printing", "ws-cube.gcode"))
        self.assertAlmostEqual(st["progress"], 0.425)
        # temperatures come from the first (history) message; later "current" ones have none
        self.assertEqual(st["temperatures"]["nozzle"], {"actual": 210.0, "target": 210.0})
        self.assertEqual(len([c for c in self.srv.calls if c[1] in ("/api/printer", "/api/job")]), rest_calls)
        self.srv.flags.update(printing=False, paused=True)
        self.assertTrue(wait_for(lambda: a.get_status()["state"] == "paused"))
        self.assertTrue(a.diagnostics()["websocket"])

    def test_no_websocket_falls_back_to_polling(self):
        a = self.start(push=False)
        st = a.get_status()
        self.assertEqual(st["transport"], "http")
        self.assertEqual(st["file"], "cube.gcode")
        self.assertTrue(wait_for(lambda: a.ws_error != ""))
        self.assertIn("login", a.ws_error)

    def test_websocket_off_in_config(self):
        a = self.start(push=True, websocket=False)
        self.assertIsNone(a.thread)                    # no push thread at all
        self.assertEqual(a.get_status()["transport"], "http")
        self.assertEqual(self.srv.ws_count, 0)
        self.assertTrue(config.validate({"printers": [MockOctoPrint.printer(self.srv)]})["printers"][0]["websocket"])

    def test_a_quiet_websocket_falls_back_to_polling(self):
        a = self.start()
        self.assertTrue(wait_for(a.ws_fresh))
        self.srv.ws_silent = True
        with mock.patch.object(octoprint, "WS_STALE_S", 0.3):
            self.assertTrue(wait_for(lambda: not a.ws_fresh(), 3))
            self.assertEqual(a.get_status()["transport"], "http")
            self.srv.ws_silent = False
            self.assertTrue(wait_for(a.ws_fresh, 3))
            self.assertEqual(a.get_status()["transport"], "websocket")

    def test_reconnects_after_the_connection_drops(self):
        a = self.start()
        self.assertTrue(wait_for(a.ws_fresh))
        a.ws.sock.close()                      # the network drops it
        self.assertTrue(wait_for(lambda: self.srv.ws_count >= 2, 8))
        self.assertTrue(wait_for(a.ws_fresh, 5))
        self.assertGreaterEqual(a.reconnects, 1)

    def test_offline_printer_over_the_websocket(self):
        a = self.start()
        self.srv.flags.update(operational=False, printing=False, closedOrError=True)
        with mock.patch.object(self.srv, "current", lambda: {
                "state": {"text": "Offline", "flags": self.srv.flags}, "job": {}, "progress": {}, "temps": []}):
            self.assertTrue(wait_for(lambda: a.ws_fresh() and a.get_status()["state"] == "offline"))
        self.assertIn("not connected", a.get_status()["message"])

    def test_reauth_request_ends_the_session(self):
        srv = MockOctoPrint()
        self.addCleanup(srv.stop)
        a = adapters.create(srv.printer())
        with self.assertRaises(octoprint.AdapterError):
            a._on_message({"reauthRequired": {"reason": "logout"}})
        a._on_message({"connected": {}})
        self.assertFalse(a.ws_fresh())                 # no state yet: never trusted


if __name__ == "__main__":
    unittest.main()
