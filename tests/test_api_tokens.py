"""API tokens (Authorization: Bearer) and the endpoints for programs such as the Orca Slicer plugin."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import auth, config, web  # noqa: E402
from tests.test_dashboard import ServerFixture  # noqa: E402


class TokenUnitTests(unittest.TestCase):
    def test_tokens_are_stored_as_hashes_and_found_in_constant_time(self):
        token = auth.new_token()
        self.assertTrue(token.startswith("os3_") and len(token) > 40)
        entry = {"name": "orca", "token_sha256": auth.token_hash(token), "role": "operator"}
        self.assertIs(auth.find_token([entry], token), entry)
        for wrong in ("", token + "x", "os3_" + "a" * 43, "x" * 300):
            self.assertIsNone(auth.find_token([entry], wrong))

    def test_config_validation(self):
        base = {"printers": []}
        ok = config.validate(dict(base, api_tokens=[{"name": "Orca", "token_sha256": "A" * 64}]))
        self.assertEqual(ok["api_tokens"][0]["name"], "orca")
        self.assertEqual(ok["api_tokens"][0]["role"], "operator")
        for bad in ([{"name": "x", "token_sha256": "short"}], [{"name": "bad name", "token_sha256": "a" * 64}],
                    [{"name": "x", "token_sha256": "a" * 64, "role": "admin"}], ["x"]):
            with self.assertRaises(config.ConfigError):
                config.validate(dict(base, api_tokens=bad))

    def test_cli_add_and_remove(self):
        import shutil
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config.json")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(web.main(["--config", path, "--add-token", "orca", "--role", "viewer"]), 0)
        token = [w for w in out.getvalue().split() if w.startswith("os3_")][0]
        saved = config.read_raw(path)["api_tokens"]
        self.assertEqual((saved[0]["name"], saved[0]["role"]), ("orca", "viewer"))
        with open(path, encoding="utf-8") as f:
            self.assertNotIn(token, f.read())                # only the hash is on disk
        with redirect_stdout(io.StringIO()):
            self.assertEqual(web.main(["--config", path, "--remove-token", "orca"]), 0)
            self.assertEqual(web.main(["--config", path, "--remove-token", "orca"]), 2)
        self.assertEqual(config.read_raw(path)["api_tokens"], [])


class TokenApiTests(ServerFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = cls.app.change_token(add="orca")
        cls.viewer = cls.app.change_token(add="watcher", role="viewer")

    def setUp(self):
        self.app.token_failures.hits.clear()

    def api(self, method, path, token=None, body=None, admin=False, headers=None):
        h = {"Authorization": "Bearer " + (token or self.token)}
        h.update(headers or {})
        return self.request(method, path, body, h, admin=admin, user=None)

    def test_token_signs_in_without_tailscale_identity_or_csrf_header(self):
        resp, data = self.api("GET", "/api/me")
        self.assertEqual(resp.status, 200, data)
        me = json.loads(data)
        self.assertEqual((me["user"], me["token"], me["role"], me["can_control"]), ("token:orca", True, "operator", True))
        self.assertFalse(me["can_edit"])
        self.assertEqual(self.api("GET", "/api/me", admin=True)[0].status, 200)

    def test_wrong_token_is_refused_audited_and_rate_limited(self):
        resp, data = self.api("GET", "/api/me", token="os3_wrong")
        self.assertEqual(resp.status, 401)
        self.assertIn("API token", json.loads(data)["error"])
        for _ in range(12):
            self.api("GET", "/api/me", token="os3_wrong")
        resp, data = self.api("GET", "/api/me")                    # even the right token waits now
        self.assertEqual(resp.status, 401)
        self.assertIn("Too many", json.loads(data)["error"])
        with open(os.path.join(self.dir, "audit.log"), encoding="utf-8") as f:
            self.assertIn("wrong-token", f.read())

    def test_token_cannot_bypass_host_or_tailscale_checks(self):
        self.assertEqual(self.api("GET", "/api/me", headers={"Host": "evil.example"})[0].status, 403)
        with mock.patch.dict(self.app.cfg, {"allowed_users": ["bob@example.com"]}):
            resp, _ = self.api("GET", "/api/me", headers={"Tailscale-User-Login": "mallory@example.com"})
            self.assertEqual(resp.status, 403)                      # a refused Tailscale login stays refused
        self.assertEqual(self.api("GET", "/api/me", admin=True, headers={"Host": "evil.example:1"})[0].status, 403)

    def test_viewer_token_reads_but_cannot_send(self):
        resp, data = self.api("GET", "/api/plugin/printers", token=self.viewer)
        self.assertEqual(resp.status, 200)
        resp, _ = self.api("POST", "/api/plugin/jobs?printer=voron&name=a.gcode", token=self.viewer, body=b"G28\n")
        self.assertEqual(resp.status, 403)

    def test_plugin_printers_lists_status_queue_and_bed_check(self):
        data = json.loads(self.api("GET", "/api/plugin/printers")[1])
        voron = next(p for p in data["printers"] if p["id"] == "voron")
        self.assertIn("state", voron["status"])
        self.assertEqual(voron["queue"], [])
        self.assertIn("auto_start", voron["bed_check"])
        self.assertTrue(data["version"])

    def test_plugin_job_queue_send_and_print(self):
        app = self.app
        resp, data = self.api("POST", "/api/plugin/jobs?printer=voron&name=part.gcode", body=b"G28\nG1 X1\n")
        self.assertEqual(resp.status, 200, data)
        first = json.loads(data)
        self.assertEqual(first["file"], "part.gcode")
        # The same file again keeps its name; a different file with that name gets a new one.
        same = json.loads(self.api("POST", "/api/plugin/jobs?printer=voron&name=part.gcode", body=b"G28\nG1 X1\n")[1])
        other = json.loads(self.api("POST", "/api/plugin/jobs?printer=voron&name=part.gcode", body=b"G28\nG1 X2\n")[1])
        self.assertEqual((same["file"], other["file"]), ("part.gcode", "part (2).gcode"))
        for name in ("part.gcode", "part (2).gcode"):
            self.addCleanup(os.remove, os.path.join(app.files_dir, name))
        queued = [j for j in app.queue if j["by"] == "token:orca"]
        self.assertEqual([j["file"] for j in queued], ["part.gcode", "part.gcode", "part (2).gcode"])
        with app.lock:
            app.queue = [j for j in app.queue if j["by"] != "token:orca"]
            app.save_queue()
        before = len(self.printer.uploads)
        resp, data = self.api("POST", "/api/plugin/jobs?printer=voron&name=now.gcode&action=print&overwrite=1",
                              body=b"G28\n")
        self.assertEqual(resp.status, 200, data)
        self.assertIn(b'name="print"', self.printer.uploads[-1])
        self.assertEqual(len(self.printer.uploads), before + 1)
        resp, _ = self.api("POST", "/api/plugin/jobs?printer=voron&name=x.exe", body=b"MZ")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.api("POST", "/api/plugin/jobs?printer=nope&name=a.gcode", body=b"G28")[0].status, 404)
        self.assertEqual(self.api("POST", "/api/plugin/jobs?printer=voron&name=a.gcode&action=x", body=b"G")[0].status, 400)

    def test_setup_page_manages_tokens_on_this_pc_only(self):
        h = {"X-Dashboard": "1", "Content-Type": "application/json"}
        resp, data = self.request("POST", "/api/setup/tokens", json.dumps({"name": "laptop"}).encode(), h, admin=True)
        self.assertEqual(resp.status, 200, data)
        token = json.loads(data)["token"]
        self.assertEqual(self.api("GET", "/api/me", token=token)[0].status, 200)
        names = [t["name"] for t in json.loads(self.request("GET", "/api/setup/tokens", admin=True)[1])["tokens"]]
        self.assertIn("laptop", names)
        self.assertEqual(self.request("GET", "/api/setup/tokens")[0].status, 404)       # not on the public port
        self.assertEqual(self.api("POST", "/api/setup/tokens", body=b'{"name":"x"}')[0].status, 404)
        for method, path in (("GET", "/api/setup/config"), ("GET", "/api/setup/tokens"),
                             ("POST", "/api/setup/tokens"), ("POST", "/api/setup/config")):
            resp, _ = self.api(method, path, admin=True, body=b'{"name":"x"}' if method == "POST" else None)
            self.assertEqual(resp.status, 404, path)       # a token never opens the Setup page API
        resp, _ = self.request("POST", "/api/setup/tokens/delete", json.dumps({"name": "laptop"}).encode(), h, admin=True)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.api("GET", "/api/me", token=token)[0].status, 401)
        resp, data = self.request("POST", "/api/setup/tokens", json.dumps({"name": "Bad Name!"}).encode(), h, admin=True)
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
