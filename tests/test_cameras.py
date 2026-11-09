import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import cameras, config, klipper, web  # noqa: E402
from tests.mock_printer import FRAME  # noqa: E402


class _Camera(BaseHTTPRequestHandler):
    """Printer whose Mainsail camera link uses the printer's own name, and whose
    default camera address is broken (HTTP 500), like some stock Klipper images."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path.startswith("/server/webcams/list"):
            body = json.dumps({"result": {"webcams": [{
                "enabled": True, "service": "mjpegstreamer",
                "stream_url": "http://printer-name.local:%d/cam/stream" % port}]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/cam/stream") and not self.server.broken_stream:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=b")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"--b\r\nContent-Type: image/jpeg\r\n\r\n" + FRAME + b"\r\n")
                    time.sleep(0.1)
            except OSError:
                pass
        elif self.path.startswith("/webcam/?action=snapshot"):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(FRAME)))
            self.end_headers()
            self.wfile.write(FRAME)
        else:
            self.send_error(500, "Server Error")


class CameraServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, broken_stream=False):
        self.broken_stream = broken_stream
        super().__init__(("127.0.0.1", 0), _Camera)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class CameraUrlTests(unittest.TestCase):
    def setUp(self):
        klipper._webcam_cache.clear()

    def test_camera_link_with_printer_name_points_at_printer_ip(self):
        p = {"id": "v", "host": "192.168.0.21"}
        self.assertEqual(klipper.on_printer("http://voron.local:8080/?action=stream", p),
                         "http://192.168.0.21:8080/?action=stream")
        self.assertEqual(klipper.on_printer("http://localhost/webcam/?action=snapshot", p),
                         "http://192.168.0.21/webcam/?action=snapshot")
        self.assertIsNone(klipper.on_printer("file:///etc/passwd", p))

    def test_urls_start_with_mainsail_camera_then_defaults(self):
        srv = CameraServer()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        p = {"id": "v", "host": "127.0.0.1", "http_port": port}
        urls = klipper.webcam_urls(p)
        self.assertEqual(urls[0], "http://127.0.0.1:%d/cam/stream" % port)
        self.assertIn("http://127.0.0.1:%d/webcam/?action=snapshot" % port, urls)
        self.assertTrue(all(u.startswith("http://127.0.0.1:") for u in urls))

    def test_override_is_used_alone(self):
        p = {"id": "v", "host": "10.0.0.5", "webcam_url": "http://10.0.0.9/stream"}
        self.assertEqual(klipper.webcam_urls(p), ["http://10.0.0.9/stream"])

    def run_hub(self, p):
        hub = cameras.CameraHub("v", lambda h: cameras.klipper_camera(h, lambda: klipper.webcam_urls(p)))
        self.addCleanup(hub.stop)
        return hub, hub.snapshot()

    def test_stream_from_mainsail_link(self):
        srv = CameraServer()
        self.addCleanup(srv.shutdown)
        hub, frame = self.run_hub({"id": "v", "host": "127.0.0.1", "http_port": srv.server_address[1]})
        self.assertEqual(frame, FRAME)

    def test_falls_back_when_camera_address_fails(self):
        srv = CameraServer(broken_stream=True)  # Mainsail link and default stream give HTTP 500
        self.addCleanup(srv.shutdown)
        hub, frame = self.run_hub({"id": "v", "host": "127.0.0.1", "http_port": srv.server_address[1]})
        self.assertEqual(frame, FRAME)  # from the snapshot address
        self.assertTrue(hub.good_url.endswith("/webcam/?action=snapshot"))

    def test_error_names_the_address_that_failed(self):
        hub = cameras.CameraHub("v", None)
        hub.last_request = time.time()
        with mock.patch.object(cameras, "_stream_from", side_effect=OSError("HTTP Error 500: Server Error")):
            with self.assertRaises(ConnectionError) as ctx:
                cameras.klipper_camera(hub, ["http://1.2.3.4/a", "http://1.2.3.4/b"])
        self.assertIn("http://1.2.3.4/a", str(ctx.exception))
        self.assertIn("HTTP Error 500", str(ctx.exception))
        self.assertIn("1 other address", str(ctx.exception))


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.legacy = os.path.join(self.dir, "printer-dashboard", "config.json")
        os.makedirs(os.path.dirname(self.legacy))
        with open(self.legacy, "w", encoding="utf-8") as f:
            json.dump({"port": 8080, "printers": [
                {"id": "sapphire-1", "name": "Sapphire-1", "type": "klipper", "host": "192.0.2.23", "ui_port": 8101},
                {"id": "sapphire-pro", "name": "Sapphire Pro", "type": "klipper", "host": "192.0.2.22",
                 "ui_port": 8102, "rotate": 180},
                {"id": "p1s", "name": "Bambu P1S", "type": "bambu", "host": "192.0.2.6",
                 "access_code": "old12345", "serial": ""},
            ]}, f)
        self.cfg_path = os.path.join(self.dir, "config.json")
        config.save(self.cfg_path, {"port": 18080, "admin_port": 18079, "printers": [
            {"id": "sapphire-pro", "name": "sapphire -pro", "type": "klipper", "host": "192.0.2.22", "ui_port": 8101},
            {"id": "bambu-p1s", "name": "P1S", "type": "bambu", "host": "192.0.2.6",
             "access_code": "typo1234", "serial": "01P00A000000001"},
        ]})
        patcher = mock.patch.object(web, "LEGACY_CONFIGS", (self.legacy,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_import_merges_old_printers(self):
        with mock.patch.object(web.App, "reload"):
            app = web.App.__new__(web.App)
            app.config_path = self.cfg_path
            self.assertEqual(app.import_legacy(), 3)
        saved = config.load(self.cfg_path)
        by_host = {p["host"]: p for p in saved["printers"]}
        self.assertEqual(len(saved["printers"]), 3)
        self.assertEqual(by_host["192.0.2.22"]["rotate"], 180)          # old settings win
        self.assertEqual(by_host["192.0.2.22"]["id"], "sapphire-pro")
        p1s = by_host["192.0.2.6"]
        self.assertEqual(p1s["id"], "bambu-p1s")                         # existing id kept
        self.assertEqual(p1s["serial"], "01P00A000000001")              # serial kept
        self.assertEqual(config.secret(p1s, "access_code"), "old12345")  # code that worked before
        ports = [p["ui_port"] for p in saved["printers"] if p["type"] == "klipper"]
        self.assertEqual(len(ports), len(set(ports)))

    def test_setup_lists_old_printers(self):
        path, printers = web.legacy_printers()
        self.assertEqual(path, self.legacy)
        self.assertEqual([p["name"] for p in printers], ["Sapphire-1", "Sapphire Pro", "Bambu P1S"])


if __name__ == "__main__":
    unittest.main()
