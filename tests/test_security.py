"""Attacks that must fail: DNS rebinding, printer redirects, a swapped Bambu upload connection."""
import hashlib
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import bambu, cameras, forward  # noqa: E402
from dashboard.tlspin import CertificateChanged, PinStore  # noqa: E402
from tests.mock_printer import FRAME  # noqa: E402
from tests.test_dashboard import USER, ServerFixture  # noqa: E402


class RebindingTests(ServerFixture):
    def test_host_names(self):
        for host in ("127.0.0.1:8080", "localhost", "printers.tail1234.ts.net", "PRINTERS.TAIL1234.TS.NET:443"):
            self.assertTrue(forward.local_or_tailnet(host), host)
        for host in ("evil.example", "evil.example:8080", "ts.net.evil.example", "", None, "127.0.0.1.evil.example"):
            self.assertFalse(forward.local_or_tailnet(host), host)

    def test_dashboard_refuses_another_sites_name(self):
        # A website that points evil.example at 127.0.0.1 can set any header, even the Tailscale one.
        resp, _ = self.request("GET", "/api/printers", headers={"Host": "evil.example:8080"}, user=USER)
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("POST", "/api/upload/voron?name=a.gcode&print=1", b"G28",
                               {"Host": "evil.example:8080", "Origin": "http://evil.example:8080",
                                "X-Dashboard": "1", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(len(self.printer.uploads), self.uploads_before)

    def setUp(self):
        self.uploads_before = len(self.printer.uploads)

    def mainsail(self, host):
        with socket.create_connection(("127.0.0.1", self.ui_port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: " + host + b"\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return data
                data += chunk

    def test_mainsail_forward_refuses_another_sites_name(self):
        self.assertTrue(self.mainsail(b"evil.example:%d" % self.ui_port).startswith(b"HTTP/1.1 403"))
        self.assertIn(b"Mainsail", self.mainsail(b"127.0.0.1:%d" % self.ui_port))


class _Redirects(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/image":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(FRAME)))
            self.end_headers()
            self.wfile.write(FRAME)
            return
        self.send_response(302)
        # TEST-NET address: nothing answers there, so following it would hang the test.
        self.send_header("Location", "/image" if self.path == "/here" else "http://192.0.2.1/secret")
        self.send_header("Content-Length", "0")
        self.end_headers()


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Redirects)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def test_redirect_on_the_same_printer_is_followed(self):
        with cameras._open(self.base + "/here") as r:
            self.assertEqual(r.read(), FRAME)

    def test_redirect_to_another_computer_is_refused(self):
        with self.assertRaisesRegex(ConnectionError, "302"):
            cameras._open(self.base + "/away")


class BambuUploadTests(unittest.TestCase):
    def test_upload_connection_must_be_the_pinned_printer(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        pins = PinStore(os.path.join(d, "pins.json"))
        pins.pins["192.0.2.6:990"] = hashlib.sha256(b"printer-cert").hexdigest()
        ftp = bambu.BambuFTPS(pins)
        ftp.host, ftp.port, ftp._prot_p = "192.0.2.6", 990, True
        ftp._sock = mock.Mock()
        impostor = mock.Mock()
        impostor.getpeercert.return_value = b"someone-else"
        with mock.patch.object(bambu.ftplib.FTP, "ntransfercmd", return_value=(mock.Mock(), None)), \
                mock.patch.object(ftp.context, "wrap_socket", return_value=impostor):
            with self.assertRaises(CertificateChanged):
                ftp.ntransfercmd("STOR x.gcode")
            impostor.close.assert_called_once()
            impostor.getpeercert.return_value = b"printer-cert"
            self.assertIs(ftp.ntransfercmd("STOR x.gcode")[0], impostor)


if __name__ == "__main__":
    unittest.main()
