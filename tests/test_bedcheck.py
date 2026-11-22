"""Bed-clear check (dashboard/bedcheck.py) and the queue's opt-in auto-start (web.AutoStarter)."""
import itertools
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import bedcheck, config, web  # noqa: E402
from tests.mock_printer import MockPrinter  # noqa: E402
from tests.test_dashboard import ServerFixture, free_port  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def jpeg(name):
    with open(os.path.join(DATA, name), "rb") as f:
        return f.read()


def builtin(name):
    return bedcheck.gray_grid(jpeg(name), use_pillow=False)


try:
    import PIL  # noqa: F401
    HAVE_PILLOW = True
except ImportError:
    HAVE_PILLOW = False


class DecoderTests(unittest.TestCase):
    def test_builtin_reader_gives_a_one_eighth_scale_picture(self):
        for name in ("bed_empty.jpg", "bed_empty_gray.jpg", "bed_empty_restart.jpg"):
            w, h, y, cb, cr = builtin(name)
            self.assertEqual((w, h), (40, 30), name)
            self.assertEqual(len(y), 1200)
            self.assertTrue(all(0 <= v <= 255 for v in y + cb + cr))
        self.assertEqual(set(builtin("bed_empty_gray.jpg")[3]), {128.0})   # no color in a gray picture

    def test_builtin_reader_refuses_what_it_cannot_read(self):
        with self.assertRaises(bedcheck.BedCheckError) as e:
            builtin("bed_empty_progressive.jpg")
        self.assertIn("Pillow", str(e.exception))
        for bad in (b"", b"not a jpeg", jpeg("bed_empty.jpg")[:400], b"\xff\xd8\xff\xd9"):
            with self.assertRaises(bedcheck.BedCheckError):
                bedcheck.gray_grid(bad, use_pillow=False)

    @unittest.skipUnless(HAVE_PILLOW, "Pillow is optional")
    def test_pillow_reader_matches_the_builtin_grid(self):
        a, b = builtin("bed_empty.jpg"), bedcheck.gray_grid(jpeg("bed_empty.jpg"), use_pillow=True)
        self.assertEqual(a[:2], b[:2])
        self.assertLess(sum(abs(x - y) for x, y in zip(a[2], b[2])) / len(a[2]), 6)
        self.assertEqual(bedcheck.gray_grid(jpeg("bed_empty_progressive.jpg"), use_pillow=True)[:2], (40, 30))

    def test_backend_falls_back_without_pillow(self):
        with mock.patch.object(bedcheck, "_pillow", return_value=None):
            self.assertEqual(bedcheck.backend(), "builtin")
            self.assertEqual(bedcheck.gray_grid(jpeg("bed_empty.jpg"))[:2], (40, 30))


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.ref = builtin("bed_empty.jpg")

    def test_empty_bed_matches_despite_noise_and_light(self):
        for name in ("bed_empty_2.jpg", "bed_empty_3.jpg", "bed_empty_bright.jpg", "bed_empty_restart.jpg"):
            r = bedcheck.compare(self.ref, builtin(name))
            self.assertTrue(r["clear"], (name, r))

    def test_parts_are_found_by_brightness_or_color(self):
        for name in ("bed_part_red.jpg", "bed_part_white.jpg"):
            r = bedcheck.compare(self.ref, builtin(name))
            self.assertFalse(r["clear"], (name, r))
            self.assertGreater(r["score"], r["threshold"])

    def test_roi_limits_the_check_to_the_bed(self):
        r = bedcheck.compare(self.ref, builtin("bed_part_red.jpg"), roi=[0, 0, 0.4, 1])
        self.assertTrue(r["clear"])          # the part is outside this region
        self.assertEqual(r["grid"], [16, 24])

    def test_unusable_pictures_raise(self):
        with self.assertRaises(bedcheck.BedCheckError):
            bedcheck.compare(self.ref, builtin("bed_dark.jpg"))      # light off
        with self.assertRaises(bedcheck.BedCheckError):
            bedcheck.compare(self.ref, builtin("bed_small.jpg"))     # camera resolution changed

    def test_settings_validation(self):
        p = {}
        bedcheck.validate_printer(p, "x")
        self.assertEqual(p["auto_start"], False)
        self.assertEqual(p["bed_check_roi"], [0.0, 0.0, 1.0, 1.0])
        p = {"bed_check_roi": "0, 0.3, 1, 1", "bed_check_threshold": "2", "auto_start": 1}
        bedcheck.validate_printer(p, "x")
        self.assertEqual((p["bed_check_roi"], p["bed_check_threshold"], p["auto_start"]), ([0, 0.3, 1, 1], 2.0, True))
        for bad in ({"bed_check_roi": [0, 0, 0.01, 1]}, {"bed_check_roi": [0.5, 0, 0.2, 1]},
                    {"bed_check_threshold": 99}, {"bed_check_frames": 0}, {"bed_check_tolerance": "x"}):
            with self.assertRaises(config.ConfigError, msg=bad):
                bedcheck.validate_printer(dict(bad), "x")


class CheckerStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.checker = bedcheck.BedChecker(self.dir)
        self.cfg = dict(bedcheck.DEFAULTS, bed_check_frames=2)

    def test_needs_reference_then_a_seen_part_then_clear_pictures_in_a_row(self):
        c, t = self.checker, iter(range(1, 100))
        st, ready = c.observe("v", jpeg("bed_empty_2.jpg"), next(t), self.cfg)
        self.assertFalse(ready)
        self.assertIn("no empty-bed picture", st["reason"])
        c.set_reference("v", jpeg("bed_empty.jpg"), "alice")
        self.assertEqual(c.reference_info("v")["by"], "alice")
        st, ready = c.observe("v", jpeg("bed_empty_2.jpg"), next(t), self.cfg)
        self.assertFalse(ready)              # clear, but no part was seen since the last print
        self.assertIn("no finished part was seen", st["reason"])
        st, ready = c.observe("v", jpeg("bed_part_red.jpg"), next(t), self.cfg)
        self.assertFalse(ready)
        self.assertTrue(st["seen_occupied"])
        self.assertTrue(c.take_notice("v"))
        self.assertFalse(c.take_notice("v"))  # one notification per wait
        st, ready = c.observe("v", jpeg("bed_empty_2.jpg"), next(t), self.cfg)
        self.assertFalse(ready)
        self.assertEqual(st["passes"], 1)
        st, ready = c.observe("v", jpeg("bed_empty_2.jpg"), next(t), self.cfg)
        self.assertFalse(ready)              # the same picture again: frozen camera
        self.assertIn("not changing", st["reason"])
        self.assertEqual(st["passes"], 0)
        st, ready = c.observe("v", jpeg("bed_empty_3.jpg"), next(t), self.cfg)
        st, ready = c.observe("v", jpeg("bed_empty_4.jpg"), next(t), self.cfg)
        self.assertTrue(ready, st)
        c.reset("v")
        self.assertFalse(c.status("v")["seen_occupied"])

    def test_errors_never_count_as_clear(self):
        c = self.checker
        c.set_reference("v", jpeg("bed_empty.jpg"), "alice")
        cfg = dict(self.cfg, bed_check_frames=1, bed_check_require_removal=False)
        for i, frame in enumerate((None, b"\xff\xd8junk", jpeg("bed_dark.jpg"), jpeg("bed_small.jpg"))):
            st, ready = c.observe("v", frame, 10 + i, cfg)
            self.assertFalse(ready)
            self.assertEqual(st["passes"], 0)
        with mock.patch.object(bedcheck, "compare", side_effect=RuntimeError("boom")), \
                self.assertLogs("dashboard.bedcheck", "ERROR"):
            st, ready = c.observe("v", jpeg("bed_empty_2.jpg"), 999, cfg)
        self.assertFalse(ready)
        self.assertIn("boom", st["reason"])
        st, ready = c.observe("v", jpeg("bed_empty_3.jpg"), 1000, cfg)
        self.assertTrue(ready)               # require_removal off: a clear bed is enough

    def test_reference_must_be_readable_and_bright(self):
        with self.assertRaises(bedcheck.BedCheckError):
            self.checker.set_reference("v", b"\xff\xd8junk", "a")
        with self.assertRaises(bedcheck.BedCheckError):
            self.checker.set_reference("v", jpeg("bed_dark.jpg"), "a")
        with self.assertRaises(bedcheck.BedCheckError):
            self.checker.set_reference("../x", jpeg("bed_empty.jpg"), "a")
        self.assertIsNone(self.checker.reference_info("v"))
        self.checker.set_reference("v", jpeg("bed_empty.jpg"), "a")
        self.checker.clear_reference("v")
        self.assertIsNone(self.checker.reference("v"))


class FakeHub:
    """Camera hub stand-in: hands out the given pictures, each with a newer time."""

    clock = itertools.count(1)

    def __init__(self, frames):
        self.frames = list(frames)
        self.cond = threading.Condition()
        self.frame_time = 0.0
        self.error = None

    def snapshot(self):
        if not self.frames:
            return None
        self.frame_time = float(next(FakeHub.clock))
        return self.frames.pop(0)

    def touch(self):
        pass

    def stop(self):
        pass


class AutoStartTests(unittest.TestCase):
    def setUp(self):
        self.printer = MockPrinter()
        self.addCleanup(self.printer.shutdown)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = os.path.join(self.dir, "config.json")
        config.save(path, {"port": free_port(), "admin_port": free_port(), "printers": [
            {"id": "voron", "name": "Voron", "type": "klipper", "host": "127.0.0.1",
             "http_port": self.printer.port, "auto_start": True, "bed_check_frames": 2}]})
        self.app = web.App(path, self.dir)
        self.app.monitor.stop()              # the test drives the auto-start by hand
        self.addCleanup(self._close)
        self.events = []
        self.status = {"voron": {"state": "complete"}}
        self.app.monitor = mock.Mock(status=self.status, announce=lambda kind, p, st: self.events.append((kind, st)))
        with open(os.path.join(self.app.files_dir, "cube.gcode"), "w") as f:
            f.write("G28\n")
        self.app.queue.append({"id": "j1", "file": "cube.gcode", "printer": "voron", "added": 0, "by": "alice"})
        self.app.bedcheck.set_reference("voron", jpeg("bed_empty.jpg"), "alice")

    def _close(self):
        for h in list(self.app.audit_log.handlers):
            h.close()
            self.app.audit_log.removeHandler(h)
        for fw in self.app.forwarders:
            fw.stop()

    def run_ticks(self, frames):
        self.app.hubs["voron"] = FakeHub(frames)
        for _ in frames:
            self.app.autostarter.tick()

    def audit(self):
        with open(os.path.join(self.dir, "audit.log"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_starts_after_the_part_is_removed_and_the_bed_stays_clear(self):
        self.run_ticks([jpeg("bed_part_red.jpg"), jpeg("bed_part_white.jpg")])
        self.assertEqual(self.app.queue[0]["id"], "j1")
        self.assertEqual([k for k, _ in self.events], ["bed_not_clear"])   # once, not every picture
        job = self.app.queue_view()[0]
        self.assertIn("not clear", job["bed_check"]["reason"])
        self.assertTrue(job["bed_check"]["auto_start"] and job["bed_check"]["has_reference"])
        self.run_ticks([jpeg("bed_empty_2.jpg"), jpeg("bed_empty_3.jpg")])
        self.assertEqual(self.app.queue, [])
        self.assertIn(b'name="print"', self.printer.uploads[-1])
        self.assertEqual(self.events[-1][0], "auto_started")
        entry = [e for e in self.audit() if e["action"] == "queue-auto-start"][-1]
        self.assertEqual((entry["user"], entry["result"], entry["file"]), ("auto-start", "ok", "cube.gcode"))
        self.assertEqual(self.app.history.list("voron")[0]["user"], "auto-start")

    def test_never_starts_without_every_condition(self):
        clear = [jpeg("bed_part_red.jpg"), jpeg("bed_empty_2.jpg"), jpeg("bed_empty_3.jpg"), jpeg("bed_empty_4.jpg")]
        cases = {
            "auto_start off": lambda: self.app.printers["voron"].update(auto_start=False),
            "printer busy": lambda: self.status.update(voron={"state": "printing"}),
            "no reference": lambda: self.app.bedcheck.clear_reference("voron"),
            "job just started": lambda: self.app.queue_started.update(voron=9e12),
        }
        for name, breaker in cases.items():
            with self.subTest(name):
                saved = dict(self.app.printers["voron"])
                breaker()
                self.run_ticks(list(clear))
                self.assertEqual([j["id"] for j in self.app.queue], ["j1"], name)
                self.assertEqual(self.printer.uploads, [])
                self.app.printers["voron"].clear()
                self.app.printers["voron"].update(saved)
                self.status["voron"] = {"state": "complete"}
                self.app.queue_started.clear()
                self.app.bedcheck.set_reference("voron", jpeg("bed_empty.jpg"), "alice")

    def test_no_camera_or_decoder_error_means_no_start(self):
        self.run_ticks([jpeg("bed_part_red.jpg")])
        self.app.hubs["voron"] = FakeHub([])            # camera gone
        for _ in range(3):
            self.app.autostarter.tick()
        with mock.patch.object(bedcheck, "gray_grid", side_effect=bedcheck.BedCheckError("unreadable")):
            self.run_ticks([jpeg("bed_empty_2.jpg"), jpeg("bed_empty_3.jpg")])
        self.assertEqual(len(self.app.queue), 1)
        self.assertIn("unreadable", self.app.queue_view()[0]["bed_check"]["reason"])

    def test_a_refused_start_waits_for_a_person(self):
        self.run_ticks([jpeg("bed_part_red.jpg")])
        with mock.patch.object(self.app, "push_file", return_value=(502, {"ok": False, "error": "printer said no"})):
            self.run_ticks([jpeg("bed_empty_2.jpg"), jpeg("bed_empty_3.jpg"), jpeg("bed_empty_4.jpg")])
        self.assertEqual(len(self.app.queue), 1)
        self.assertEqual(self.events[-1][0], "warning")
        self.assertIn("printer said no", self.app.queue_view()[0]["bed_check"]["reason"])
        self.assertEqual(self.printer.uploads, [])

    def test_a_job_is_started_only_once(self):
        code, _, _ = self.app.start_queued_job("j1", "alice")
        self.assertEqual(code, 200)
        self.assertEqual(self.app.start_queued_job("j1", "bob")[0], 404)
        self.assertEqual(len(self.printer.uploads), 1)


class BedCheckApiTests(ServerFixture):
    def test_save_test_view_and_delete_the_empty_bed_picture(self):
        h = {"X-Dashboard": "1"}
        app = self.app
        self.addCleanup(app.bedcheck.clear_reference, "voron")
        real = app.hubs["voron"]
        # The mock printer's camera sends pictures the bed check cannot read: refused, nothing saved.
        resp, data = self.request("POST", "/api/bedcheck/voron/reference", None, h)
        self.assertEqual(resp.status, 400, data)
        try:
            app.hubs["voron"] = FakeHub([jpeg("bed_empty.jpg"), jpeg("bed_part_red.jpg")])
            resp, data = self.request("POST", "/api/bedcheck/voron/reference", None, h)
            self.assertEqual(resp.status, 200, data)
            resp, data = self.request("POST", "/api/bedcheck/voron/test", None, h)
            self.assertEqual(resp.status, 200, data)
            self.assertFalse(json.loads(data)["result"]["clear"])
        finally:
            app.hubs["voron"] = real
        resp, data = self.request("GET", "/api/bedcheck/voron/reference.jpg")
        self.assertEqual((resp.status, data), (200, jpeg("bed_empty.jpg")))
        listing = json.loads(self.request("GET", "/api/bedcheck")[1])
        voron = next(p for p in listing["printers"] if p["id"] == "voron")
        self.assertTrue(voron["has_reference"])
        self.assertFalse(voron["auto_start"])
        self.assertEqual(self.request("POST", "/api/bedcheck/voron/reference", None)[0].status, 403)  # no CSRF header
        with mock.patch.dict(app.cfg, {"read_only_users": ["alice@example.com"]}):
            self.assertEqual(self.request("POST", "/api/bedcheck/voron/reference/delete", None, h)[0].status, 403)
        self.assertEqual(self.request("POST", "/api/bedcheck/voron/reference/delete", None, h)[0].status, 200)
        self.assertEqual(self.request("GET", "/api/bedcheck/voron/reference.jpg")[0].status, 404)
        self.assertEqual(self.request("POST", "/api/bedcheck/nope/test", None, h)[0].status, 404)


if __name__ == "__main__":
    unittest.main()
