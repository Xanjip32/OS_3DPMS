import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_dashboard  # noqa: E402


class InstallableAppTests(test_dashboard.ServerFixture):
    def test_app_files_served_with_identity_only(self):
        files = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "text/javascript",
                 "/icon-192.png": "image/png", "/icon-512.png": "image/png",
                 "/apple-touch-icon.png": "image/png"}
        for path, ctype in files.items():
            resp, data = self.request("GET", path)
            self.assertEqual(resp.status, 200, path)
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype), path)
            if ctype == "image/png":
                self.assertTrue(data.startswith(b"\x89PNG"), path)
            resp, _ = self.request("GET", path, user=None)
            self.assertEqual(resp.status, 403, path)

    def test_manifest_is_installable(self):
        _, data = self.request("GET", "/manifest.webmanifest")
        m = json.loads(data)
        self.assertEqual(m["display"], "standalone")
        self.assertEqual(m["start_url"], "/")
        sizes = {i["sizes"] for i in m["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        _, html = self.request("GET", "/")
        self.assertIn(b'rel="manifest"', html)
        self.assertIn(b'rel="apple-touch-icon"', html)
