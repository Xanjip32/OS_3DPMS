"""A dashboard App with a mixed fleet of simulated printers, for API-level tests."""
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

from dashboard import config, web
from tests.mock_http_printers import MockOctoPrint
from tests.mock_moonraker import MockMoonraker
from tests.test_dashboard import free_port

USER = "alice@example.com"


def blackhole_port():
    """A local port that refuses connections (printer switched off)."""
    return free_port()


class FleetFixture(unittest.TestCase):
    extra_cfg = {}
    moonrakers = 1
    offline = 0

    @classmethod
    def setUpClass(cls):
        cls.mr = [MockMoonraker(websocket=False) for _ in range(cls.moonrakers)]
        cls.octo = MockOctoPrint()
        cls.dir = tempfile.mkdtemp()
        printers = [m.printer(id="mr-%d" % i, name="Sapphire %d" % i, websocket=False,
                              location="Office" if i % 2 == 0 else "Lab")
                    for i, m in enumerate(cls.mr)]
        printers.append(cls.octo.printer(location="Lab"))
        for i in range(cls.offline):
            printers.append({"id": "off-%d" % i, "name": "Off %d" % i, "type": "klipper", "host": "127.0.0.1",
                             "http_port": blackhole_port(), "websocket": False})
        printers.append({"id": "p1s", "name": "P1S", "type": "bambu", "host": "127.0.0.1",
                         "access_code": "12345678", "location": "Office", "mode": "read_only"})
        cfg = {"port": free_port(), "admin_port": free_port(), "max_upload_mb": 1, "allowed_users": [],
               "printers": printers}
        cfg.update(cls.extra_cfg)
        cls.cfg_path = os.path.join(cls.dir, "config.json")
        config.save(cls.cfg_path, cfg)
        cls.app = web.App(cls.cfg_path, cls.dir)
        cls.app.monitor.stop()  # tests drive poll_once themselves
        cls.public = web.LimitedServer(("127.0.0.1", cls.app.cfg["port"]), web.make_handler(cls.app, False))
        cls.admin = web.LimitedServer(("127.0.0.1", cls.app.cfg["admin_port"]), web.make_handler(cls.app, True))
        for srv in (cls.public, cls.admin):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.public, cls.admin):
            srv.shutdown()
            srv.server_close()
        for hub in cls.app.hubs.values():
            hub.stop()
        cls.app.manager.stop_all()
        for m in cls.mr:
            m.stop()
        cls.octo.stop()
        for h in list(cls.app.audit_log.handlers):
            h.close()
            cls.app.audit_log.removeHandler(h)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def request(self, method, path, body=None, headers=None, admin=False, user=USER, cookie=None):
        port = self.app.cfg["admin_port"] if admin else self.app.cfg["port"]
        h = {"Host": "127.0.0.1:%d" % port if admin else "printers.tail1234.ts.net"}
        if user and not admin:
            h["Tailscale-User-Login"] = user
        if cookie:
            h["Cookie"] = cookie
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, {"X-Dashboard": "1"}, **kw)
