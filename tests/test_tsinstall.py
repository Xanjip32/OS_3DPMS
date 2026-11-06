import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import tailscale, tsinstall  # noqa: E402
from tests import test_dashboard  # noqa: E402

SIGNED = "Valid|CN=Tailscale Inc., O=Tailscale Inc., L=Toronto, S=Ontario, C=CA"


class FakeResponse(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def reset():
    tsinstall._state.update(state="idle", detail="")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_signer_check(self):
        self.assertTrue(tsinstall.signer_ok(SIGNED))
        self.assertFalse(tsinstall.signer_ok(SIGNED.replace("Valid", "HashMismatch")))
        self.assertFalse(tsinstall.signer_ok("Valid|CN=Evil, O=Not Tailscale Inc., C=US"))
        self.assertFalse(tsinstall.signer_ok("Valid|CN=Tailscale Inc., O=Evil Corp"))
        self.assertFalse(tsinstall.signer_ok(""))

    def test_safe_url(self):
        self.assertTrue(tailscale.safe_url("https://login.tailscale.com/a/abc"))
        self.assertFalse(tailscale.safe_url("http://login.tailscale.com/a/abc"))
        self.assertFalse(tailscale.safe_url("https://login.tailscale.com.evil.example/a"))
        self.assertFalse(tailscale.safe_url("javascript:alert(1)"))

    def test_download_rejects_redirect_elsewhere(self):
        dest = os.path.join(tempfile.mkdtemp(), "dl.msi")
        ok = lambda url, timeout: FakeResponse(b"MSI", "https://pkgs.tailscale.com/stable/x.msi")  # noqa: E731
        self.assertEqual(tsinstall.download("u", dest, opener=ok), 3)
        bad = lambda url, timeout: FakeResponse(b"MSI", "https://evil.example/x.msi")  # noqa: E731
        with self.assertRaises(RuntimeError):
            tsinstall.download("u", dest, opener=bad)

    def run_worker(self, verify_out, install_code):
        calls = []

        def fake_ps(script, msi, timeout):
            calls.append(script)
            if script == tsinstall.PS_VERIFY:
                return 0, verify_out
            return install_code, ""

        with mock.patch.object(tsinstall, "download", return_value=3), \
                mock.patch.object(tsinstall, "_powershell", side_effect=fake_ps), \
                mock.patch.object(tailscale, "find_cli", return_value="tailscale"), \
                mock.patch.object(tailscale, "status", return_value={"state": "NeedsLogin"}):
            tsinstall._worker()
        return calls

    def test_unsigned_file_is_never_run(self):
        calls = self.run_worker("Valid|CN=Someone, O=Someone Else", 0)
        self.assertEqual(calls, [tsinstall.PS_VERIFY])
        self.assertEqual(tsinstall.state()["state"], "failed")
        self.assertIn("not signed", tsinstall.state()["detail"])

    def test_uac_declined(self):
        self.run_worker(SIGNED, tsinstall.UAC_DECLINED)
        self.assertEqual(tsinstall.state()["state"], "failed")
        self.assertIn("declined", tsinstall.state()["detail"])

    def test_success(self):
        calls = self.run_worker(SIGNED, 0)
        self.assertEqual(calls, [tsinstall.PS_VERIFY, tsinstall.PS_INSTALL])
        self.assertEqual(tsinstall.state()["state"], "done")

    def test_start_rules(self):
        with mock.patch.object(tsinstall, "supported", return_value=False):
            self.assertFalse(tsinstall.start()["ok"])
        with mock.patch.object(tsinstall, "supported", return_value=True), \
                mock.patch.object(tailscale, "find_cli", return_value="tailscale"):
            self.assertTrue(tsinstall.start()["already"])
        tsinstall._state["state"] = "installing"
        with mock.patch.object(tsinstall, "supported", return_value=True), \
                mock.patch.object(tailscale, "find_cli", return_value=None):
            self.assertTrue(tsinstall.start()["running"])


class InstallRouteTests(test_dashboard.ServerFixture):
    def test_only_admin_can_install(self):
        headers = {"X-Dashboard": "1", "Content-Type": "application/json"}
        resp, _ = self.request("POST", "/api/setup/install-tailscale", b"{}", headers)
        self.assertEqual(resp.status, 404)        # not on the published port
        resp, _ = self.request("POST", "/api/setup/install-tailscale", b"{}", admin=True)
        self.assertEqual(resp.status, 403)        # CSRF header missing
        with mock.patch.object(tsinstall, "start", return_value={"ok": True}) as start:
            resp, data = self.request("POST", "/api/setup/install-tailscale", b"{}", headers, admin=True)
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(data)["ok"])
        start.assert_called_once()
        resp, data = self.request("GET", "/api/setup/status", admin=True)
        self.assertIn("supported", json.loads(data)["install"])


if __name__ == "__main__":
    unittest.main()
