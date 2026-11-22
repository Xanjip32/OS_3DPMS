"""Mixed fleet: 10+ simulated printers, offline isolation, capabilities, control API, history."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fleet_fixture import FleetFixture  # noqa: E402


class FleetTests(FleetFixture):
    moonrakers = 10
    offline = 3

    def test_ten_printers_polled_concurrently_offline_ones_isolated(self):
        t0 = time.time()
        self.app.monitor.poll_once()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 4.5, "one round must not add up per-printer timeouts")
        status = self.app.monitor.status
        for i in range(10):
            self.assertEqual(status["mr-%d" % i]["state"], "printing")
            self.assertEqual(status["mr-%d" % i]["temperatures"]["bed"]["actual"], 59.9)
        self.assertEqual(status["kossel"]["state"], "printing")
        for i in range(3):
            self.assertEqual(status["off-%d" % i], {"state": "offline"})

    def test_offline_printer_backs_off(self):
        from dashboard import monitor
        m = self.app.monitor
        for _ in range(monitor.OFFLINE_POLLS + 1):
            m.update(self.app.printers["off-0"], None)
        self.assertGreater(m.next_poll["off-0"], time.time())
        self.assertFalse(m.health("off-0")["online"])

    def test_api_lists_capabilities_and_locations(self):
        resp, data = self.request("GET", "/api/printers")
        printers = {p["id"]: p for p in json.loads(data)}
        self.assertEqual(printers["mr-0"]["protocol"], "moonraker")
        self.assertTrue(printers["mr-0"]["capabilities"]["can_pause"])
        self.assertEqual(printers["kossel"]["protocol"], "octoprint")
        self.assertFalse(printers["p1s"]["capabilities"]["can_upload"])   # Bambu: read-only
        self.assertEqual(printers["p1s"]["manufacturer"], "Bambu Lab")
        self.assertEqual(printers["mr-1"]["location"], "Lab")
        self.assertNotIn("api_key", data.decode())
        self.assertNotIn(self.octo.key, data.decode())   # secrets never reach the browser
        resp, data = self.request("GET", "/api/locations")
        self.assertEqual(json.loads(data)["locations"], ["Lab", "Office"])

    def test_printer_detail_and_diagnostics(self):
        self.app.monitor.poll_once()
        resp, data = self.request("GET", "/api/printers/mr-0")
        self.assertEqual(resp.status, 200)
        detail = json.loads(data)
        self.assertEqual(detail["status"]["state"], "printing")
        self.assertTrue(detail["connection"]["online"])
        resp, data = self.request("GET", "/api/printers/mr-0/diagnostics")
        self.assertEqual(json.loads(data)["server"]["klippy_state"], "ready")
        resp, data = self.request("GET", "/api/printers/mr-0/files")
        self.assertEqual(json.loads(data)["files"][0]["name"], "benchy.gcode")
        resp, data = self.request("GET", "/api/printers/mr-0/temperatures")
        self.assertTrue(json.loads(data)["samples"])
        self.assertEqual(self.request("GET", "/api/printers/nope")[0].status, 404)

    def test_pause_goes_to_the_printer_and_is_audited(self):
        resp, data = self.post("/api/printers/mr-2/pause")
        self.assertEqual(resp.status, 200, data)
        self.assertEqual(self.mr[2].posts[-1][0], "/printer/print/pause")
        with open(os.path.join(self.dir, "audit.log"), encoding="utf-8") as f:
            self.assertIn('"action": "printer-pause"', f.read())

    def test_cancel_needs_confirmation(self):
        before = len(self.mr[3].posts)
        resp, data = self.post("/api/printers/mr-3/cancel")
        self.assertEqual(resp.status, 428)
        resp, data = self.post("/api/printers/mr-3/cancel", {"confirm": "mr-4"})   # wrong printer
        self.assertEqual(resp.status, 428)
        self.assertEqual(len(self.mr[3].posts), before)
        resp, data = self.post("/api/printers/mr-3/cancel", {"confirm": "mr-3"})
        self.assertEqual(resp.status, 200, data)
        self.assertEqual(self.mr[3].posts[-1][0], "/printer/print/cancel")

    def test_control_refused_without_capability(self):
        resp, data = self.post("/api/printers/p1s/pause")
        self.assertEqual(resp.status, 403, data)
        self.assertIn("turned off", json.loads(data)["error"])

    def test_control_needs_csrf_header(self):
        resp, _ = self.request("POST", "/api/printers/mr-0/pause")
        self.assertEqual(resp.status, 403)

    def test_start_while_printing_is_refused(self):
        resp, data = self.post("/api/printers/mr-5/start", {"confirm": "mr-5", "file": "benchy.gcode"})
        self.assertEqual(resp.status, 409, data)

    def test_upload_file_type_is_checked_per_printer(self):
        resp, data = self.post("/api/upload/mr-6?name=plate.3mf&print=0", b"PK")
        self.assertEqual(resp.status, 400)
        self.assertIn(".gcode", json.loads(data)["error"])

    def test_existing_file_is_not_replaced_without_confirmation(self):
        resp, data = self.post("/api/upload/mr-7?name=benchy.gcode&print=0", b"G28\n")
        self.assertEqual(resp.status, 409, data)
        self.assertTrue(json.loads(data)["exists"])
        resp, data = self.post("/api/upload/mr-7?name=benchy.gcode&print=0&overwrite=1", b"G28\n")
        self.assertEqual(resp.status, 200, data)

    def test_job_history_follows_events(self):
        p = self.app.printers["mr-8"]
        m = self.app.monitor
        m.update(p, {"state": "standby"})
        m.update(p, {"state": "printing", "file": "a.gcode"})
        m.update(p, {"state": "complete", "file": "a.gcode"})
        m.update(p, {"state": "printing", "file": "b.gcode"})
        m.update(p, {"state": "error", "file": "b.gcode", "message": "heater"})
        jobs = self.app.history.list("mr-8")
        self.assertEqual([(j["file"], j["status"]) for j in jobs], [("b.gcode", "failed"), ("a.gcode", "completed")])
        self.assertEqual(jobs[0]["error"], "heater")
        resp, data = self.request("GET", "/api/history?printer=mr-8")
        self.assertEqual(len(json.loads(data)["jobs"]), 2)

    def test_reconnected_and_warning_events(self):
        from dashboard import monitor
        p = self.app.printers["mr-9"]
        m = self.app.monitor
        sent = []
        original = self.app.notifier.send
        self.app.notifier.send = sent.append
        try:
            m.update(p, {"state": "printing", "warnings": []})
            for _ in range(monitor.OFFLINE_POLLS):
                m.update(p, None)
            m.update(p, {"state": "printing", "warnings": []})
            m.update(p, {"state": "printing", "warnings": ["Filament not detected (runout)"]})
            m.update(p, {"state": "printing", "warnings": ["Filament not detected (runout)"]})  # once only
        finally:
            self.app.notifier.send = original
        self.assertEqual([e["kind"] for e in sent], ["offline", "reconnected", "warning"])
        self.assertIn("Filament", sent[-1]["text"])


class ReadOnlyUserTests(FleetFixture):
    extra_cfg = {"read_only_users": ["viewer@example.com"]}

    def test_viewer_can_watch_but_not_act(self):
        v = "viewer@example.com"
        self.assertEqual(self.request("GET", "/api/printers", user=v)[0].status, 200)
        me = json.loads(self.request("GET", "/api/me", user=v)[1])
        self.assertFalse(me["can_control"])
        for path, body in (("/api/printers/mr-0/pause", None), ("/api/upload/mr-0?name=a.gcode", b"G28"),
                           ("/api/files?name=a.gcode", b"G28"), ("/api/queue/add?name=a.gcode&printer=mr-0", None)):
            resp, _ = self.post(path, body, user=v)
            self.assertEqual(resp.status, 403, path)
        self.assertEqual(self.mr[0].posts, [])


if __name__ == "__main__":
    unittest.main()
