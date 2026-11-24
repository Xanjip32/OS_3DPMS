import json
import os
import sys
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import bambu, config, monitor, notify  # noqa: E402
from tests import test_dashboard  # noqa: E402


class Capture:
    """Tiny HTTP server that records requests, standing in for ntfy/Discord/Telegram."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_PUT = _handle

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TransitionTests(unittest.TestCase):
    def test_rules(self):
        t = monitor.transition
        self.assertIsNone(t(None, "printing"))          # first sighting is only a baseline
        self.assertEqual(t("standby", "printing"), "started")
        self.assertEqual(t("printing", "paused"), "paused")
        self.assertIsNone(t("paused", "printing"))      # resume is not an alert
        self.assertEqual(t("printing", "complete"), "complete")
        self.assertEqual(t("printing", "error"), "error")
        self.assertEqual(t("paused", "cancelled"), "cancelled")
        self.assertIsNone(t("complete", "standby"))

    def test_text(self):
        text = monitor.describe("complete", "Voron", {"file": "body.gcode", "duration": 3 * 3600 + 29 * 60})
        self.assertIn("Voron finished body.gcode in 3h 29m", text)


class NotificationTests(test_dashboard.ServerFixture):
    def test_print_events_reach_api(self):
        self.printer.state = "standby"
        self.app.monitor.poll_once()
        _, before = self.app.monitor.events_after(0)
        try:
            self.printer.state = "printing"
            self.app.monitor.poll_once()
            self.printer.state = "complete"
            self.app.monitor.poll_once()
        finally:
            self.printer.state = "standby"
            self.app.monitor.poll_once()
        resp, data = self.request("GET", "/api/events?after=%d" % before)
        self.assertEqual(resp.status, 200)
        kinds = [e["kind"] for e in json.loads(data)["events"] if e["printer"] == "voron"]
        self.assertEqual(kinds[:2], ["started", "complete"])
        resp, _ = self.request("GET", "/api/events", user=None)
        self.assertEqual(resp.status, 403)

    def test_offline_only_while_printing(self):
        m = monitor.Monitor(self.app)
        p = {"id": "x", "name": "X"}
        sent = []
        original = self.app.notifier.send
        self.app.notifier.send = sent.append
        try:
            m.update(p, {"state": "standby"})
            for _ in range(monitor.OFFLINE_POLLS + 2):
                m.update(p, None)
            self.assertEqual(sent, [])
            m.update(p, {"state": "printing"})
            for _ in range(monitor.OFFLINE_POLLS + 2):
                m.update(p, None)
            self.assertEqual([e["kind"] for e in sent], ["started", "offline"])
        finally:
            self.app.notifier.send = original

    def test_deliver_to_ntfy_and_filter(self):
        cap = Capture()
        saved = self.app.cfg
        try:
            n = config.validate({"notifications": {}})["notifications"]
            n["ntfy_url"] = cap.url + "/my-topic"   # test only: skips the https rule
            self.app.cfg = dict(saved, notifications=n)
            event = {"id": 1, "printer": "voron", "name": "Voron", "kind": "complete",
                     "text": "Voron finished a.gcode"}
            self.assertEqual(self.app.notifier.deliver(event), {"ntfy": "ok"})
            method, path, _, body = cap.requests[-1]
            self.assertEqual((method, path), ("POST", "/"))
            self.assertEqual(json.loads(body)["topic"], "my-topic")
            count = len(cap.requests)               # "started" is off by default
            self.assertEqual(self.app.notifier.deliver(dict(event, kind="started")), {})
            self.assertEqual(len(cap.requests), count)
        finally:
            self.app.cfg = saved
            cap.close()

    def test_channel_payloads(self):
        cap = Capture()
        try:
            notify.send_ntfy(cap.url + "/t", "tok12345", "Voron", "✅ done", image=b"\xff\xd8x\xff\xd9")
            method, _, headers, _ = cap.requests[-1]
            self.assertEqual(method, "PUT")
            self.assertEqual(headers["Authorization"], "Bearer tok12345")
            self.assertTrue(headers["Message"].startswith("=?UTF-8?B?"))
            notify.send_discord(cap.url + "/hook", "hello @everyone")
            self.assertEqual(json.loads(cap.requests[-1][3])["allowed_mentions"], {"parse": []})
            notify.send_telegram("123:abc", "42", "hi", api=cap.url)
            self.assertEqual(cap.requests[-1][1], "/bot123:abc/sendMessage")
            notify.send_telegram("123:abc", "42", "hi", image=b"img", api=cap.url)
            self.assertEqual(cap.requests[-1][1], "/bot123:abc/sendPhoto")
        finally:
            cap.close()

    def test_settings_saved_and_hidden(self):
        hook = "https://discord.com/api/webhooks/123456/abcDEF-ghi_jkl"
        headers = {"X-Dashboard": "1", "Content-Type": "application/json"}

        def post(payload):
            return self.request("POST", "/api/setup/notifications", json.dumps(payload).encode(),
                                headers, admin=True)

        try:
            resp, data = post({"events": {"complete": True, "started": True}, "discord_webhook": hook})
            self.assertEqual(resp.status, 200, data)
            self.assertEqual(json.loads(data)["channels"], ["discord"])
            _, data = self.request("GET", "/api/setup/config", admin=True)
            n = json.loads(data)["notifications"]
            self.assertTrue(n["has_discord_webhook"])
            self.assertTrue(n["events"]["started"])
            self.assertNotIn("abcDEF", data.decode())
            post({"events": {}})                     # empty field keeps the saved webhook
            self.assertEqual(self.app.notifier.channels(), ["discord"])
            resp, _ = post({"discord_webhook": "https://evil.example/x"})
            self.assertEqual(resp.status, 400)
        finally:
            post({"clear_discord_webhook": True})
        self.assertEqual(self.app.notifier.channels(), [])

    def test_email_settings_survive_setup_save(self):
        headers = {"X-Dashboard": "1", "Content-Type": "application/json"}
        raw = config.read_raw(self.app.config_path)
        raw["notifications"] = dict(EmailTests.SMTP)
        config.save(self.app.config_path, config.validate(raw))
        try:
            resp, data = self.request("POST", "/api/setup/notifications", json.dumps({"events": {}}).encode(),
                                      headers, admin=True)
            self.assertEqual(resp.status, 200, data)
            self.assertIn("email", json.loads(data)["channels"])
            n = config.read_raw(self.app.config_path)["notifications"]
            self.assertEqual(n["smtp_host"], "smtp.example.com")
            self.assertTrue(n.get("smtp_password") or n.get("smtp_password_protected"))
        finally:
            raw = config.read_raw(self.app.config_path)
            raw["notifications"] = {}
            config.save(self.app.config_path, config.validate(raw))
            self.request("POST", "/api/setup/notifications", b'{"events": {}}', headers, admin=True)
        self.assertEqual(self.app.notifier.channels(), [])

    def test_url_validation(self):
        bad = [{"ntfy_url": "http://ntfy.sh/topic"},
               {"ntfy_url": "https://ntfy.sh/a/b?x=1"},
               {"discord_webhook": "https://evil.example/api/webhooks/1/x"},
               {"telegram_token": "nope"},
               {"telegram_chat_id": "abc def"}]
        for n in bad:
            with self.assertRaises(config.ConfigError, msg=str(n)):
                config.validate({"notifications": n})
        ok = config.validate({"notifications": {"ntfy_url": "https://ntfy.sh/os3dpms-abc123"}})
        self.assertEqual(ok["notifications"]["ntfy_url"], "https://ntfy.sh/os3dpms-abc123")


class EmailTests(unittest.TestCase):
    SMTP = {"smtp_host": "smtp.example.com", "smtp_user": "bot@example.com", "smtp_password": "app-password",
            "smtp_to": "me@example.com"}

    def test_validation(self):
        n = config.validate({"notifications": dict(self.SMTP)})["notifications"]
        self.assertEqual((n["smtp_port"], n["smtp_security"], n["smtp_from"], n["smtp_to"]),
                         (587, "starttls", "bot@example.com", ["me@example.com"]))
        bad = [dict(self.SMTP, smtp_security="none"),        # plain SMTP only to localhost
               dict(self.SMTP, smtp_to="not-an-address"),
               dict(self.SMTP, smtp_to=[]),
               dict(self.SMTP, smtp_port=70000),
               dict(self.SMTP, smtp_host="bad host!")]
        for b in bad:
            with self.assertRaises(config.ConfigError, msg=str(b)):
                config.validate({"notifications": b})
        local = config.validate({"notifications": dict(self.SMTP, smtp_host="localhost", smtp_security="none",
                                                       smtp_port=25)})
        self.assertEqual(local["notifications"]["smtp_security"], "none")

    def test_send_uses_tls_and_login(self):
        n = config.validate({"notifications": dict(self.SMTP)})["notifications"]
        with mock.patch("smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value = smtp.return_value
            notify.send_email(n, "app-password", "[OpenPrintDeck] Voron: complete", "done", image=b"\xff\xd8x")
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=20)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("bot@example.com", "app-password")
        msg = server.send_message.call_args[0][0]
        self.assertEqual(msg["To"], "me@example.com")
        self.assertEqual(len(list(msg.iter_attachments())), 1)


class BambuStatusTests(unittest.TestCase):
    def test_mqtt_report_parsing(self):
        topic = b"device/X/report"
        payload = json.dumps({"print": {"gcode_state": "RUNNING", "mc_percent": 37,
                                        "subtask_name": "dome"}}).encode()
        body = bambu._s16(topic) + payload
        buf = bytearray(bytes([0x30]) + bambu._remaining(len(body)) + body + b"\x30")
        first, got = bambu._parse_packet(buf)
        self.assertEqual(first, 0x30)
        self.assertEqual(buf, bytearray(b"\x30"))   # the incomplete next packet stays buffered
        self.assertIsNone(bambu._parse_packet(buf))

        w = bambu.BambuWatcher({"id": "p1s", "host": "127.0.0.1", "serial": "X"}, None)
        w._on_publish(first, got)
        st = w.status()
        self.assertEqual(st["state"], "printing")
        self.assertAlmostEqual(st["progress"], 0.37)
        self.assertEqual(st["file"], "dome")
        w._on_publish(first, bambu._s16(topic) + json.dumps({"print": {"gcode_state": "FINISH"}}).encode())
        self.assertEqual(w.status()["state"], "complete")
        self.assertEqual(w.status()["file"], "dome")   # partial reports are merged


if __name__ == "__main__":
    unittest.main()
