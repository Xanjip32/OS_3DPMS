import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import adapters  # noqa: E402
from dashboard.adapters import moonraker  # noqa: E402
from tests.mock_moonraker import OBJECTS, MockMoonraker  # noqa: E402


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class NormalizeTests(unittest.TestCase):
    def test_full_status(self):
        st = moonraker.normalize(OBJECTS, {"estimated_time": 2400})
        self.assertEqual(st["state"], "printing")
        self.assertEqual(st["temperatures"]["nozzle"], {"actual": 214.8, "target": 215.0})
        self.assertEqual(st["temperatures"]["bed"]["target"], 60.0)
        self.assertEqual(st["temperatures"]["chamber"]["actual"], 31.0)
        self.assertEqual(st["position"], [11.0, 21.0, 3.0])
        self.assertEqual(st["layer"], {"current": 12, "total": 120})
        self.assertEqual(st["remaining_s"], 1800)
        self.assertEqual(st["fan"], 0.5)
        self.assertEqual(st["warnings"], [])

    def test_missing_objects_are_fine(self):
        st = moonraker.normalize({"print_stats": {"state": "standby"}})
        self.assertEqual(st["state"], "standby")
        self.assertEqual(st["temperatures"], {})
        self.assertIsNone(st["position"])

    def test_klippy_not_ready_is_an_error(self):
        st = moonraker.normalize({"webhooks": {"state": "shutdown", "state_message": "MCU 'mcu' shutdown"},
                                  "print_stats": {"state": "printing"}})
        self.assertEqual(st["state"], "error")
        self.assertIn("shutdown", st["errors"][0])

    def test_filament_runout_warning(self):
        st = moonraker.normalize({"filament_switch_sensor runout": {"enabled": True, "filament_detected": False}})
        self.assertEqual(st["warnings"], ["Filament not detected (runout)"])

    def test_delta_merge(self):
        cache = {"extruder": {"temperature": 20.0, "target": 0.0}}
        moonraker.merge(cache, {"extruder": {"target": 200.0}, "fan": {"speed": 1.0}})
        self.assertEqual(cache, {"extruder": {"temperature": 20.0, "target": 200.0}, "fan": {"speed": 1.0}})

    def test_wanted_objects_skips_heavy_ones(self):
        wanted = moonraker.wanted_objects(list(OBJECTS))
        self.assertIn("temperature_sensor chamber", wanted)
        self.assertNotIn("configfile", wanted)


class MoonrakerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.srv = MockMoonraker()
        self.addCleanup(self.srv.stop)

    def adapter(self, **extra):
        a = adapters.create(self.srv.printer(**extra))
        self.addCleanup(a.disconnect)
        return a

    def test_http_fallback_with_object_discovery(self):
        a = self.adapter(websocket=False)
        st = a.get_status()
        self.assertEqual(st["state"], "printing")
        self.assertEqual(st["transport"], "http")
        self.assertEqual(st["temperatures"]["nozzle"]["actual"], 214.8)
        self.assertTrue(any(r.startswith("/printer/objects/list") for r in self.srv.requests))
        query = [r for r in self.srv.requests if r.startswith("/printer/objects/query")][0]
        self.assertNotIn("configfile", query)

    def test_websocket_subscription_and_deltas(self):
        a = self.adapter()
        a.connect()
        self.assertTrue(wait_for(lambda: a.ws_connected))
        st = a.get_status()
        self.assertEqual(st["transport"], "websocket")
        self.assertEqual(st["progress"], 0.25)
        polls = len([r for r in self.srv.requests if r.startswith("/printer/objects/query")])
        self.srv.push({"display_status": {"progress": 0.5}, "extruder": {"temperature": 216.0}})
        self.assertTrue(wait_for(lambda: a.get_status()["progress"] == 0.5))
        st = a.get_status()
        self.assertEqual(st["temperatures"]["nozzle"], {"actual": 216.0, "target": 215.0})  # merged
        self.assertEqual(len([r for r in self.srv.requests if r.startswith("/printer/objects/query")]), polls,
                         "no HTTP polling while the WebSocket is up")

    def test_reconnects_after_the_socket_drops(self):
        a = self.adapter()
        a.connect()
        self.assertTrue(wait_for(lambda: a.ws_connected))
        self.srv.drop_sockets()
        self.assertTrue(wait_for(lambda: not a.ws_connected, 3))
        self.assertEqual(a.get_status()["transport"], "http")  # polling fallback meanwhile
        self.assertTrue(wait_for(lambda: a.ws_connected and self.srv.ws_count >= 2, 8))
        self.assertGreaterEqual(a.reconnects, 1)

    def test_klippy_shutdown_notification(self):
        a = self.adapter()
        a.connect()
        self.assertTrue(wait_for(lambda: a.ws_connected))
        self.srv.notify("notify_klippy_shutdown")
        self.assertTrue(wait_for(lambda: a.get_status()["state"] == "error"))

    def test_api_key_uses_oneshot_token_for_websocket(self):
        self.srv.api_key = "k" * 32
        a = self.adapter(api_key="k" * 32)
        a.connect()
        self.assertTrue(wait_for(lambda: a.ws_connected))
        self.assertTrue(any(r.startswith("/websocket?token=oneshot") for r in self.srv.requests))

    def test_printer_offline(self):
        self.srv.stop()
        a = self.adapter(websocket=False)
        self.assertIsNone(a.get_status())
        self.assertTrue(a.last_error)

    def test_controls_post_to_moonraker(self):
        a = self.adapter(websocket=False)
        a.pause_print()
        a.resume_print()
        a.cancel_print()
        self.assertEqual([p for p, _ in self.srv.posts],
                         ["/printer/print/pause", "/printer/print/resume", "/printer/print/cancel"])

    def test_start_refused_while_printing(self):
        a = self.adapter(websocket=False)
        with self.assertRaises(adapters.PrinterBusy):
            a.start_print("benchy.gcode")
        self.assertEqual(self.srv.posts, [])

    def test_control_disabled_by_owner(self):
        a = self.adapter(websocket=False, allow_remote_control=False)
        self.assertFalse(a.get_capabilities()["can_pause"])
        with self.assertRaises(adapters.NotAuthorized):
            a.cancel_print()
        self.assertEqual(self.srv.posts, [])

    def test_files_history_and_exists(self):
        a = self.adapter(websocket=False)
        self.assertEqual(a.list_files()[0]["name"], "benchy.gcode")
        self.assertEqual(a.get_history()[0]["status"], "completed")
        self.assertTrue(a.file_exists("benchy.gcode"))
        self.assertFalse(a.file_exists("other.gcode"))
        d = a.diagnostics()
        self.assertEqual(d["server"]["moonraker_version"], "v0.9")

    def test_rejects_3mf(self):
        a = self.adapter(websocket=False)
        self.assertFalse(a.accepts_file("plate.3mf"))


if __name__ == "__main__":
    unittest.main()
