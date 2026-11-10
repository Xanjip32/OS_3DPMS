import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import cameras, cloud, config, web, winsecret  # noqa: E402
from dashboard.tlspin import CertificateChanged, PinStore  # noqa: E402
from tests.mock_printer import MockPrinter  # noqa: E402

USER = "alice@example.com"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerFixture(unittest.TestCase):
    """Starts a fake printer and both dashboard listeners. Has no tests of its own."""

    @classmethod
    def setUpClass(cls):
        cls.printer = MockPrinter()
        cls.dir = tempfile.mkdtemp()
        cls.ui_port = free_port()
        cls.cfg_path = os.path.join(cls.dir, "config.json")
        config.save(cls.cfg_path, {
            "port": free_port(), "admin_port": free_port(), "max_upload_mb": 1,
            "allowed_users": [],
            "printers": [
                {"id": "voron", "name": "Voron", "type": "klipper", "host": "127.0.0.1",
                 "http_port": cls.printer.port, "ui_port": cls.ui_port},
                {"id": "p1s", "name": "P1S", "type": "bambu", "host": "127.0.0.1",
                 "access_code": "secret123"},
            ],
        })
        cls.app = web.App(cls.cfg_path, cls.dir)
        cls.public = web.LimitedServer(("127.0.0.1", cls.app.cfg["port"]), web.make_handler(cls.app, False))
        cls.admin = web.LimitedServer(("127.0.0.1", cls.app.cfg["admin_port"]), web.make_handler(cls.app, True))
        for srv in (cls.public, cls.admin):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.public, cls.admin):
            srv.shutdown()
            srv.server_close()
        for fw in cls.app.forwarders:
            fw.stop()
        for hub in cls.app.hubs.values():
            hub.stop()
        cls.app.monitor.stop()
        cls.printer.shutdown()
        logging_handlers = [h for h in cls.app.audit_log.handlers]
        for h in logging_handlers:
            h.close()
            cls.app.audit_log.removeHandler(h)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def request(self, method, path, body=None, headers=None, admin=False, user=USER):
        port = self.app.cfg["admin_port"] if admin else self.app.cfg["port"]
        h = {"Host": "127.0.0.1:%d" % port if admin else "printers.tail1234.ts.net"}
        if user and not admin:
            h["Tailscale-User-Login"] = user
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def upload(self, name, body=b"G28\n", extra=None, **kw):
        headers = {"X-Dashboard": "1", "Content-Type": "application/octet-stream"}
        headers.update(extra or {})
        return self.request("POST", "/api/upload/voron?name=%s&print=0" % name, body, headers, **kw)



class DashboardTest(ServerFixture):
    # ---------------------------------------------------------------- access

    def test_public_requires_tailscale_identity(self):
        resp, data = self.request("GET", "/api/printers", user=None)
        self.assertEqual(resp.status, 403)
        self.assertIn("Tailscale", json.loads(data)["error"])

    def test_public_with_identity(self):
        resp, data = self.request("GET", "/api/printers")
        self.assertEqual(resp.status, 200)
        text = data.decode()
        self.assertIn("Voron", text)
        self.assertNotIn("secret123", text)
        self.assertNotIn("access_code", text)

    def test_allowed_users(self):
        self.app.cfg["allowed_users"] = ["bob@example.com"]
        try:
            resp, _ = self.request("GET", "/api/printers")
            self.assertEqual(resp.status, 403)
            resp, _ = self.request("GET", "/api/printers", user="Bob@Example.com")
            self.assertEqual(resp.status, 200)
        finally:
            self.app.cfg["allowed_users"] = []

    def test_setup_not_on_public_port(self):
        for path in ("/setup", "/api/setup/status", "/api/setup/config"):
            resp, _ = self.request("GET", path)
            self.assertEqual(resp.status, 404, path)

    def test_admin_rejects_dns_rebinding(self):
        resp, _ = self.request("GET", "/api/setup/config", admin=True, headers={"Host": "evil.example:8079"})
        self.assertEqual(resp.status, 403)

    def test_admin_config_hides_secrets(self):
        resp, data = self.request("GET", "/api/setup/config", admin=True)
        self.assertEqual(resp.status, 200)
        cfg = json.loads(data)
        p1s = [p for p in cfg["printers"] if p["id"] == "p1s"][0]
        self.assertTrue(p1s["has_access_code"])
        self.assertNotIn("secret123", data.decode())

    def test_security_headers(self):
        resp, _ = self.request("GET", "/")
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    # ---------------------------------------------------------------- CSRF

    def test_upload_requires_custom_header(self):
        resp, _ = self.request("POST", "/api/upload/voron?name=a.gcode", b"G28", {"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 403)

    def test_upload_rejects_foreign_origin(self):
        resp, _ = self.upload("a.gcode", extra={"Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)

    def test_upload_rejects_cross_site_fetch(self):
        resp, _ = self.upload("a.gcode", extra={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(resp.status, 403)

    # ---------------------------------------------------------------- uploads

    def test_upload_ok_and_audited(self):
        before = len(self.printer.uploads)
        resp, data = self.upload("part.gcode", extra={"Origin": "https://printers.tail1234.ts.net"})
        self.assertEqual(resp.status, 200, data)
        self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(len(self.printer.uploads), before + 1)
        for h in self.app.audit_log.handlers:
            h.flush()
        with open(os.path.join(self.dir, "audit.log"), encoding="utf-8") as f:
            self.assertIn(USER, f.read())

    def test_upload_path_traversal_is_sanitized(self):
        resp, _ = self.upload("..%2F..%2Fevil.gcode")
        self.assertEqual(resp.status, 200)
        body = self.printer.uploads[-1]
        self.assertIn(b'filename="evil.gcode"', body)
        self.assertNotIn(b"../", body)

    def test_upload_wrong_extension(self):
        resp, _ = self.upload("run.exe")
        self.assertEqual(resp.status, 400)

    def test_upload_too_large(self):
        resp, _ = self.upload("big.gcode", body=b"x" * (1024 * 1024 + 1))
        self.assertEqual(resp.status, 413)

    def test_start_print_skipped_when_busy(self):
        self.printer.state = "printing"
        try:
            headers = {"X-Dashboard": "1"}
            resp, data = self.request("POST", "/api/upload/voron?name=b.gcode&print=1", b"G28", headers)
            self.assertEqual(resp.status, 200)
            self.assertIn("not started", json.loads(data)["message"])
            self.assertNotIn(b'name="print"', self.printer.uploads[-1])
        finally:
            self.printer.state = "standby"

    # ---------------------------------------------------------------- file library

    def test_file_library_save_send_delete(self):
        h = {"X-Dashboard": "1"}
        resp, data = self.request("POST", "/api/files?name=..%2Fpart.gcode", b"G28\n", h)
        self.assertEqual(resp.status, 200, data)
        resp, data = self.request("GET", "/api/files")
        listing = json.loads(data)
        self.assertEqual([f["name"] for f in listing["files"]], ["part.gcode"])
        self.assertGreater(listing["free"], 0)
        before = len(self.printer.uploads)
        resp, data = self.request("POST", "/api/files/send?name=part.gcode&printer=voron&print=0", None, h)
        self.assertEqual(resp.status, 200, data)
        self.assertEqual(len(self.printer.uploads), before + 1)
        self.assertIn(b'filename="part.gcode"', self.printer.uploads[-1])
        for path in ("/api/files/send?name=part.gcode&printer=nope", "/api/files/send?name=gone.gcode&printer=voron",
                     "/api/files/delete?name=config.json"):   # only library G-code, never other files here
            self.assertEqual(self.request("POST", path, None, h)[0].status, 404, path)
        self.assertEqual(self.request("POST", "/api/files?name=run.exe", b"x", h)[0].status, 400)
        resp, _ = self.request("POST", "/api/files?name=a.gcode", b"x", {**h, "Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)
        resp, data = self.request("POST", "/api/files/delete?name=part.gcode", None, h)
        self.assertEqual(resp.status, 200, data)
        self.assertEqual(os.listdir(self.app.files_dir), [])
        self.assertTrue(os.path.isfile(self.cfg_path))

    # ---------------------------------------------------------------- print queue

    def test_print_queue_runs_jobs_in_order_on_a_free_printer(self):
        h = {"X-Dashboard": "1"}
        app = self.app
        self.request("POST", "/api/files?name=cube.gcode", b"G28\n", h)
        self.addCleanup(os.remove, os.path.join(app.files_dir, "cube.gcode"))
        for _ in range(2):
            resp, data = self.request("POST", "/api/queue/add?name=cube.gcode&printer=voron", None, h)
            self.assertEqual(resp.status, 200, data)
        self.assertEqual(self.request("POST", "/api/queue/add?name=gone.gcode&printer=voron", None, h)[0].status, 404)
        self.assertEqual(self.request("POST", "/api/queue/add?name=cube.gcode&printer=nope", None, h)[0].status, 404)
        status = {"voron": {"state": "printing"}}
        with mock.patch.object(app, "monitor", mock.Mock(status=status)):
            jobs = json.loads(self.request("GET", "/api/queue")[1])["jobs"]
            self.assertEqual([j["waiting"] for j in jobs], ["printer is printing", "the job before it goes first"])
            self.assertEqual(self.request("POST", "/api/queue/start?id=" + jobs[0]["id"], None, h)[0].status, 409)
            status["voron"] = {"state": "complete"}
            resp, data = self.request("POST", "/api/queue/start?id=" + jobs[0]["id"], None, h)
            self.assertEqual(resp.status, 200, data)
            self.assertIn(b'name="print"', self.printer.uploads[-1])
            jobs = json.loads(self.request("GET", "/api/queue")[1])["jobs"]
            self.assertEqual([j["waiting"] for j in jobs], ["a job was just started there"])
        self.assertEqual(app.load_queue(), app.queue)   # kept on disk
        self.assertEqual(self.request("POST", "/api/queue/remove?id=" + jobs[0]["id"], None, h)[0].status, 200)
        self.assertEqual(app.load_queue(), [])
        with open(app.queue_path, "w") as f:
            f.write("{not json")
        self.assertEqual(app.load_queue(), [])          # a damaged queue is set aside, not lost
        self.assertTrue(os.path.isfile(app.queue_path + ".bad"))

    # ---------------------------------------------------------------- cameras, status, forward

    def test_snapshot(self):
        resp, data = self.request("GET", "/snap/voron")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"))

    def test_check_printer_reports_moonraker_and_camera(self):
        path = "/api/setup/check-printer/voron"
        headers = {"X-Dashboard": "1"}
        resp, _ = self.request("POST", path, b"{}", headers)
        self.assertEqual(resp.status, 404)
        resp, _ = self.request("POST", path, b"{}", admin=True)
        self.assertEqual(resp.status, 403)
        resp, data = self.request("POST", path, b"{}", headers, admin=True)
        self.assertEqual(resp.status, 200, data)
        result = json.loads(data)
        self.assertTrue(result["printer_ok"])
        self.assertTrue(result["camera_ok"])
        self.assertIn(str(self.printer.port), result["printer"])

        with mock.patch("dashboard.web.klipper.status", side_effect=TimeoutError):
            resp, data = self.request("POST", path, b"{}", headers, admin=True)
        self.assertEqual(resp.status, 200, data)
        result = json.loads(data)
        self.assertFalse(result["printer_ok"])
        self.assertIn("No connection", result["printer"])
        self.assertTrue(result["camera_ok"])

    def test_status(self):
        self.app.monitor.poll_once()
        resp, data = self.request("GET", "/api/status/voron")
        self.assertEqual(json.loads(data)["progress"], 0.42)

    def test_mainsail_forward_is_local_only(self):
        fw = self.app.forwarders[0]
        self.assertEqual(fw.sock.getsockname()[0], "127.0.0.1")
        conn = http.client.HTTPConnection("127.0.0.1", self.ui_port, timeout=5)
        conn.request("GET", "/")
        self.assertIn(b"Mainsail", conn.getresponse().read())
        conn.close()

    # ---------------------------------------------------------------- setup save

    def test_cloud_settings_keep_the_connection_key_private(self):
        path = "/api/setup/config"
        headers = {"X-Dashboard": "1"}
        url = "https://printers.account.workers.dev"
        key = "a" * 43
        try:
            payload = {"cloud": {"url": url, "key": key}}
            resp, data = self.request("POST", path, json.dumps(payload), headers, admin=True)
            self.assertEqual(resp.status, 200, data)
            resp, data = self.request("GET", path, admin=True)
            self.assertEqual(json.loads(data)["cloud"], {"url": url, "has_key": True})
            self.assertNotIn(key.encode(), data)
            resp, data = self.request("POST", path, json.dumps({"cloud": {"url": url}}), headers, admin=True)
            self.assertEqual(resp.status, 200, data)
            self.assertEqual(config.secret(self.app.cfg["cloud"], "key"), key)
        finally:
            self.app.apply_setup({"cloud": {"url": ""}})

    def test_cloud_report_excludes_printer_addresses_and_credentials(self):
        connector = cloud.Connector(self.app)
        with mock.patch.object(connector, "request", return_value={"cameras": []}) as send:
            connector.sync({"url": "https://printers.account.workers.dev", "key": "a" * 43})
        report = json.loads(send.call_args.args[2])
        self.assertEqual({p["id"] for p in report["printers"]}, {"voron", "p1s"})
        for printer in report["printers"]:
            self.assertNotIn("host", printer)
            self.assertNotIn("access_code", printer)
            self.assertNotIn("api_key", printer)

    def test_setup_saves_allowed_accounts_separately(self):
        path = "/api/setup/config"
        headers = {"X-Dashboard": "1"}
        hubs = self.app.hubs
        try:
            resp, data = self.request("POST", path, json.dumps({"allowed_users": [" Alice@example.com "]}),
                                      headers, admin=True)
            self.assertEqual(resp.status, 200, data)
            self.assertEqual(config.read_raw(self.cfg_path)["allowed_users"], [USER])
            self.assertIs(self.app.hubs, hubs)
            self.assertEqual(config.secret(self.app.printers["p1s"], "access_code"), "secret123")
            resp, _ = self.request("GET", "/api/printers", user="other@example.com")
            self.assertEqual(resp.status, 403)
            resp, _ = self.request("POST", path, '{"allowed_users": "invalid"}', headers, admin=True)
            self.assertEqual(resp.status, 400)
            self.assertEqual(self.app.cfg["allowed_users"], [USER])
        finally:
            self.app.apply_setup({"allowed_users": []})

    def test_setup_save_keeps_secret(self):
        resp, data = self.request("GET", "/api/setup/config", admin=True)
        cfg = json.loads(data)
        for p in cfg["printers"]:
            p.pop("has_access_code", None)
            p.pop("has_api_key", None)
            if p["id"] == "voron":
                p["rotate"] = 180
        body = json.dumps(cfg).encode()
        resp, data = self.request("POST", "/api/setup/config", body,
                                  {"X-Dashboard": "1", "Content-Type": "application/json"}, admin=True)
        self.assertEqual(resp.status, 200, data)
        p1s = self.app.printers["p1s"]
        self.assertEqual(config.secret(p1s, "access_code"), "secret123")
        self.assertEqual(self.app.printers["voron"]["rotate"], 180)
        with open(self.cfg_path, encoding="utf-8") as f:
            on_disk = f.read()
        if winsecret.available():
            self.assertNotIn("secret123", on_disk)

    def test_setup_rejects_bad_config(self):
        body = json.dumps({"printers": [{"id": "x", "type": "klipper", "host": "bad host!"}]}).encode()
        resp, _ = self.request("POST", "/api/setup/config", body, {"X-Dashboard": "1"}, admin=True)
        self.assertEqual(resp.status, 400)


class UnitTests(unittest.TestCase):
    def test_snapshot_rejects_stale_frame(self):
        hub = cameras.CameraHub("voron", None)
        hub.frame = b"old frame"
        hub.frame_time = time.time() - 10
        with mock.patch.object(hub, "touch"), mock.patch.object(hub.cond, "wait_for"):
            self.assertIsNone(hub.snapshot())

    def test_validate_rejects_non_http_camera_override(self):
        with self.assertRaisesRegex(config.ConfigError, "camera URL"):
            config.validate({"printers": [{"id": "voron", "type": "klipper", "host": "192.168.0.23",
                                          "webcam_url": "file:///local-file"}]})

    def test_safe_filename(self):
        self.assertEqual(web.safe_filename("../../a b.gcode"), "a b.gcode")
        self.assertEqual(web.safe_filename('x";rm.gcode'), "x_rm.gcode")
        self.assertEqual(web.safe_filename(".hidden.gcode"), "hidden.gcode")

    def test_validate_rejects_duplicate_ports(self):
        with self.assertRaises(config.ConfigError):
            config.validate({"printers": [
                {"id": "a", "type": "klipper", "host": "1.2.3.4", "ui_port": 8101},
                {"id": "b", "type": "klipper", "host": "1.2.3.5", "ui_port": 8101}]})

    def test_validate_bad_number_is_config_error(self):
        with self.assertRaises(config.ConfigError):
            config.validate({"printers": [{"id": "a", "type": "klipper", "host": "1.2.3.4", "rotate": "abc"}]})

    def test_validate_requires_bambu_code(self):
        with self.assertRaises(config.ConfigError):
            config.validate({"printers": [{"id": "a", "type": "bambu", "host": "1.2.3.4"}]})

    @unittest.skipUnless(winsecret.available(), "Windows only")
    def test_dpapi_roundtrip(self):
        token = winsecret.protect("hello")
        self.assertNotIn("hello", token)
        self.assertEqual(winsecret.unprotect(token), "hello")

    def test_cloud_requests_name_the_app(self):
        # Cloudflare answers 403 (error 1010) to Python's default User-Agent.
        connector = cloud.Connector.__new__(cloud.Connector)
        connector.opener = mock.MagicMock()
        connector.opener.open.return_value.__enter__.return_value.read.return_value = b"{}"
        connector.request("https://printers.account.workers.dev", "k" * 43, b"{}")
        sent = connector.opener.open.call_args.args[0]
        self.assertTrue(sent.get_header("User-agent").startswith("OS_3DPMS/"))

    def test_certificate_pinning(self):
        class FakeSock:
            def __init__(self, der):
                self.der = der

            def getpeercert(self, binary_form=False):
                return self.der

        d = tempfile.mkdtemp()
        try:
            pins = PinStore(os.path.join(d, "pins.json"))
            pins.check(FakeSock(b"cert-a"), "10.0.0.5", 990)
            pins.check(FakeSock(b"cert-a"), "10.0.0.5", 990)
            with self.assertRaises(CertificateChanged):
                pins.check(FakeSock(b"cert-b"), "10.0.0.5", 990)
            self.assertIn("10.0.0.5:990", PinStore(os.path.join(d, "pins.json")).pins)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
