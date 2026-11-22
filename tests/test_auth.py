"""Password login: hashing, sessions, rate limiting, roles, and the tailscale+password mode."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import auth, config  # noqa: E402
from tests.fleet_fixture import FleetFixture  # noqa: E402

HASH_OP = auth.hash_password("correct horse battery", iterations=1000)
HASH_VIEW = auth.hash_password("viewer password 1", iterations=1000)
USERS = [{"name": "alice", "password_hash": HASH_OP, "role": "operator"},
         {"name": "bob", "password_hash": HASH_VIEW, "role": "viewer"}]


class HashTests(unittest.TestCase):
    def test_hash_and_verify(self):
        h = auth.hash_password("a long password", iterations=1000)
        self.assertTrue(h.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(auth.verify_password("a long password", h))
        self.assertFalse(auth.verify_password("a long passwore", h))
        self.assertFalse(auth.verify_password("x", "garbage"))
        self.assertNotIn("a long password", h)

    def test_short_password_refused(self):
        with self.assertRaises(ValueError):
            auth.hash_password("short")

    def test_rate_limiter(self):
        rl = auth.RateLimiter(3, 60)
        self.assertEqual([rl.allow("k") for _ in range(4)], [True, True, True, False])
        self.assertTrue(rl.allow("other"))
        rl.reset("k")
        self.assertTrue(rl.allow("k"))

    def test_sessions(self):
        s = auth.Sessions(hours=1)
        t = s.create("alice", "operator")
        self.assertEqual(s.get(t), ("alice", "operator"))
        self.assertIsNone(s.get("forged"))
        s.drop(t)
        self.assertIsNone(s.get(t))

    def test_config_validation(self):
        with self.assertRaises(config.ConfigError):
            config.validate({"auth": {"mode": "password"}})          # no users
        with self.assertRaises(config.ConfigError):
            config.validate({"auth": {"mode": "open"}})
        with self.assertRaises(config.ConfigError):
            config.validate({"users": [{"name": "a", "password_hash": "plaintext"}]})
        cfg = config.validate({"auth": {"mode": "password"}, "users": USERS})
        self.assertEqual(cfg["auth"], {"mode": "password", "session_hours": 12})

    def test_default_mode_is_tailscale(self):
        self.assertEqual(config.validate({})["auth"]["mode"], "tailscale")


class PasswordModeTests(FleetFixture):
    extra_cfg = {"auth": {"mode": "password"}, "users": USERS}

    def login(self, user, password, **kw):
        resp, data = self.post("/api/login", {"user": user, "password": password}, user=None, **kw)
        cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
        return resp, data, cookie

    def test_pages_need_a_session(self):
        resp, _ = self.request("GET", "/", user=None)
        self.assertEqual((resp.status, resp.getheader("Location")), (302, "/login"))
        self.assertEqual(self.request("GET", "/login", user=None)[0].status, 200)
        self.assertEqual(self.request("GET", "/api/printers", user=None)[0].status, 401)
        # A Tailscale header alone is not enough in password mode
        self.assertEqual(self.request("GET", "/api/printers")[0].status, 401)

    def test_login_and_logout(self):
        resp, data, cookie = self.login("alice", "correct horse battery")
        self.assertEqual(resp.status, 200, data)
        self.assertIn("HttpOnly", resp.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", resp.getheader("Set-Cookie"))
        self.assertIn("Secure", resp.getheader("Set-Cookie"))   # reached through *.ts.net (HTTPS)
        resp, data = self.request("GET", "/api/me", user=None, cookie=cookie)
        self.assertEqual(json.loads(data)["user"], "alice")
        self.assertTrue(json.loads(data)["can_control"])
        self.post("/api/logout", user=None, cookie=cookie)
        self.assertEqual(self.request("GET", "/api/me", user=None, cookie=cookie)[0].status, 401)

    def test_wrong_password_and_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.login("carol", "nope")[0].status, 401)
        self.assertEqual(self.login("carol", "nope")[0].status, 429)

    def test_login_needs_csrf_header(self):
        resp, _ = self.request("POST", "/api/login", {"user": "alice", "password": "correct horse battery"},
                               user=None)
        self.assertEqual(resp.status, 403)

    def test_viewer_role_cannot_control(self):
        _, _, cookie = self.login("bob", "viewer password 1")
        self.assertEqual(self.request("GET", "/api/printers", user=None, cookie=cookie)[0].status, 200)
        resp, _ = self.post("/api/printers/mr-0/pause", user=None, cookie=cookie)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.mr[0].posts, [])

    def test_forged_cookie(self):
        self.assertEqual(self.request("GET", "/api/me", user=None, cookie="os3dpms_session=abc")[0].status, 401)


class BothModeTests(FleetFixture):
    extra_cfg = {"auth": {"mode": "tailscale+password"}, "users": USERS,
                 "allowed_users": ["alice@example.com"]}

    def test_needs_tailscale_and_password(self):
        resp, data = self.post("/api/login", {"user": "alice", "password": "correct horse battery"},
                               user="mallory@example.com")
        self.assertEqual(resp.status, 403)   # not an allowed Tailscale account: no login page at all
        resp, data = self.post("/api/login", {"user": "alice", "password": "correct horse battery"})
        self.assertEqual(resp.status, 200, data)
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie)[0].status, 200)
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie, user="mallory@example.com")[0].status, 403)


if __name__ == "__main__":
    unittest.main()


class CliTests(unittest.TestCase):
    def test_add_and_remove_user(self):
        import argparse
        import tempfile
        from dashboard import web
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        args = argparse.Namespace(config=path, add_user="Admin", remove_user=None, role="admin")
        self.assertEqual(web.manage_user(args, password="a very long password"), 0)
        users = config.load(path)["users"]
        self.assertEqual((users[0]["name"], users[0]["role"]), ("admin", "admin"))
        self.assertNotIn("a very long password", open(path, encoding="utf-8").read())
        args = argparse.Namespace(config=path, add_user=None, remove_user="admin", role="operator")
        self.assertEqual(web.manage_user(args), 0)
        self.assertEqual(config.load(path)["users"], [])
        self.assertEqual(web.manage_user(argparse.Namespace(config=path, add_user="x", remove_user=None,
                                                            role="viewer"), password="short"), 2)
