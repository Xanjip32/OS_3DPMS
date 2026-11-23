"""Bambu adapter: read-only mode, capabilities, telemetry parsing, refused control."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import adapters, config  # noqa: E402
from dashboard.adapters import bambu as ab  # noqa: E402

REPORT = {"gcode_state": "RUNNING", "mc_percent": 37, "mc_remaining_time": 42, "subtask_name": "dome",
          "nozzle_temper": 219.6, "nozzle_target_temper": 220, "bed_temper": 54.9, "bed_target_temper": 55,
          "chamber_temper": 28, "cooling_fan_speed": "15", "big_fan1_speed": "0", "layer_num": 31,
          "total_layer_num": 210, "hms": [{"attr": 0x03000200, "code": 0x00010001}],
          "ams": {"tray_now": "1", "ams": [{"id": "0", "humidity": "4", "tray": [
              {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80}, {"id": "1"}]}]}}


def printer(**extra):
    p = {"id": "p1s", "name": "P1S", "type": "bambu", "host": "192.0.2.6", "access_code": "12345678",
         "serial": "01P00A000000001", "use_ams": True}
    p.update(extra)
    return config.validate({"printers": [p]})["printers"][0]


class FakeWatcher:
    def __init__(self, report, reply=None, connected=True):
        self.report, self.reply, self.connected, self.error = dict(report), reply, connected, None
        self.sent = []
        self.last_report = time.time()

    def raw_report(self):
        return dict(self.report), True

    def status(self):
        from dashboard.bambu import BambuWatcher
        w = BambuWatcher.__new__(BambuWatcher)
        import threading
        w.lock, w.report, w.connected, w.last_report = threading.Lock(), self.report, True, time.time()
        return BambuWatcher.status(w)

    def request(self, topic, payload, seq, command, timeout=12):
        self.sent.append((topic, payload))
        return self.reply, "RUNNING"

    def stop(self):
        pass


class BambuConfigTests(unittest.TestCase):
    def test_read_only_by_default(self):
        p = printer()
        self.assertEqual(p["mode"], "read_only")
        self.assertFalse(p["allow_remote_control"] or p["allow_file_upload"])
        self.assertEqual((p["mqtt_port"], p["camera_port"], p["ftps_port"]), (8883, 6000, 990))

    def test_read_only_mode_overrides_allow_flags(self):
        p = printer(allow_remote_control=True, allow_file_upload=True)
        self.assertFalse(p["allow_remote_control"] or p["allow_file_upload"])

    def test_access_code_from_environment(self):
        p = printer(access_code="", access_code_env="OS3DPMS_TEST_P1S_CODE")
        os.environ["OS3DPMS_TEST_P1S_CODE"] = "87654321"
        self.addCleanup(os.environ.pop, "OS3DPMS_TEST_P1S_CODE")
        self.assertEqual(config.secret(p, "access_code"), "87654321")


class BambuAdapterTests(unittest.TestCase):
    def adapter(self, report=REPORT, reply=None, **extra):
        a = adapters.create(printer(**extra))
        a.watcher = FakeWatcher(report, reply)
        return a

    def test_telemetry(self):
        st = self.adapter().get_status()
        self.assertEqual(st["state"], "printing")
        self.assertEqual(st["temperatures"]["nozzle"], {"actual": 219.6, "target": 220.0})
        self.assertEqual(st["temperatures"]["chamber"]["actual"], 28.0)
        self.assertEqual(st["fan"], 1.0)
        self.assertEqual(st["layer"], {"current": 31, "total": 210})
        self.assertEqual(st["remaining_s"], 42 * 60)
        self.assertEqual(st["ams"]["units"][0]["trays"][0]["type"], "PLA")
        self.assertEqual(st["errors"], ["HMS 0300-0200-0001-0001"])
        self.assertEqual(st["transport"], "mqtt")

    def test_ams_off_hides_ams_and_its_no_filament_warning(self):
        empty = dict(REPORT, ams={"tray_now": "255", "ams": [{"id": "0", "tray": [{"id": "0"}]}]})
        on = self.adapter(empty, use_ams=True, mode="control").get_status()
        self.assertIn("AMS reports no filament loaded", on["warnings"])
        off = self.adapter(empty, use_ams=False, mode="control").get_status()   # no AMS connected
        self.assertEqual(off["warnings"], [])
        self.assertIsNone(off["ams"])

    def test_missing_fields(self):
        st = self.adapter({"gcode_state": "IDLE"}).get_status()
        self.assertEqual(st["state"], "standby")
        self.assertEqual(st["temperatures"], {})
        self.assertIsNone(st["layer"])
        self.assertIsNone(st["ams"])

    def test_read_only_refuses_everything(self):
        a = self.adapter()
        caps = a.get_capabilities()
        self.assertTrue(caps["monitor"] and caps["has_camera"])
        self.assertFalse(any(caps[k] for k in ("can_upload", "can_start_print", "can_pause", "can_cancel")))
        for call in (a.pause_print, a.resume_print, a.cancel_print):
            with self.assertRaises(adapters.NotAuthorized):
                call()
        with self.assertRaises(adapters.NotAuthorized):
            a.upload_file(__file__, "x.gcode")
        self.assertEqual(a.watcher.sent, [])  # nothing was sent to the printer

    def test_control_mode_needs_allow_flag_too(self):
        a = self.adapter(mode="control")
        self.assertFalse(a.get_capabilities()["can_pause"])
        with self.assertRaises(adapters.NotAuthorized):
            a.pause_print()

    def test_rejected_command_is_reported(self):
        a = self.adapter(reply={"result": "fail", "reason": "verify failed", "err_code": 84033545},
                         mode="control", allow_remote_control=True)
        with self.assertRaises(adapters.AdapterError) as cm:
            a.pause_print()
        self.assertIn("verify failed", str(cm.exception))
        self.assertIn(b'"command": "pause"', a.watcher.sent[0][1])

    def test_unconfirmed_command_is_not_success(self):
        a = self.adapter(mode="control", allow_remote_control=True)  # no reply; state stays RUNNING
        with self.assertRaises(adapters.AdapterError):
            a.pause_print()

    def test_without_serial_only_camera(self):
        a = adapters.create(printer(serial=""))
        caps = a.get_capabilities()
        self.assertFalse(caps["monitor"])
        self.assertTrue(caps["has_camera"])
        self.assertEqual(a.get_status(), {"state": None})

    def test_enrich_is_safe_with_garbage(self):
        st = ab.enrich({"state": "printing"}, {"nozzle_temper": "x", "hms": ["bad"], "ams": {"ams": [None]}})
        self.assertEqual(st["temperatures"]["nozzle"]["actual"], None)


if __name__ == "__main__":
    unittest.main()
