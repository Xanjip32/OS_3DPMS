"""Fake OctoPrint and PrusaLink servers for adapter tests."""
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Base(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler):
        self.calls = []
        self.uploads = []
        super().__init__(("127.0.0.1", 0), handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""


# ---------------------------------------------------------------- OctoPrint

class MockOctoPrint(_Base):
    def __init__(self, key="op-key"):
        self.key = key
        self.flags = {"operational": True, "printing": True, "paused": False, "ready": False, "error": False}
        self.connected = True
        self.completion = 42.5
        self.files = {"cube.gcode"}
        super().__init__(_OctoHandler)

    def printer(self, **extra):
        p = {"id": "kossel", "name": "Kossel", "type": "octoprint", "host": "127.0.0.1", "http_port": self.port,
             "api_key": self.key, "allow_remote_control": True, "allow_file_upload": True}
        p.update(extra)
        return p


class _OctoHandler(_Handler):
    def _auth(self):
        if self.headers.get("X-Api-Key") != self.server.key:
            self._json({"error": "Invalid API key"}, 403)
            return False
        return True

    def do_GET(self):
        srv = self.server
        srv.calls.append(("GET", self.path))
        if not self._auth():
            return
        if self.path == "/api/printer":
            if not srv.connected:
                return self._json({"error": "Printer is not operational"}, 409)
            return self._json({"state": {"text": "Printing", "flags": srv.flags},
                               "temperature": {"tool0": {"actual": 199.5, "target": 200.0},
                                               "bed": {"actual": 59.0, "target": 60.0}}})
        if self.path == "/api/job":
            return self._json({"job": {"file": {"name": "cube.gcode"}, "estimatedPrintTime": 3000},
                               "progress": {"completion": srv.completion, "printTime": 1200, "printTimeLeft": 1800},
                               "state": "Printing"})
        if self.path.startswith("/api/files/local/"):
            name = self.path.rsplit("/", 1)[1]
            return self._json({"name": name}) if name in srv.files else self._json({"error": "nf"}, 404)
        if self.path.startswith("/api/files/local"):
            return self._json({"files": [{"name": f, "path": f, "type": "machinecode", "size": 1} for f in srv.files]})
        if self.path == "/api/settings":
            return self._json({"webcam": {"streamUrl": "/webcam/?action=stream"}})
        if self.path in ("/api/version", "/api/connection"):
            return self._json({"server": "1.10.0", "current": {"state": "Printing"}})
        self._json({"error": "nf"}, 404)

    def do_POST(self):
        srv = self.server
        body = self._body()
        srv.calls.append(("POST", self.path, body))
        if not self._auth():
            return
        if self.path == "/api/files/local":
            srv.uploads.append(body)
            return self._json({"done": True}, 201)
        if self.path == "/api/job":
            cmd = json.loads(body)
            if cmd.get("command") == "pause" and not (srv.flags["printing"] or srv.flags["paused"]):
                return self._json({"error": "not printing"}, 409)
            return self._json(None, 204)
        if self.path.startswith("/api/files/local/"):
            return self._json(None, 204)
        self._json({"error": "nf"}, 404)


# ---------------------------------------------------------------- PrusaLink (Digest auth)

REALM, NONCE = "Printer API", "abc123"


class MockPrusaLink(_Base):
    def __init__(self, password="pl-password"):
        self.password = password
        self.state = "PRINTING"
        self.files = {"part.bgcode"}
        super().__init__(_PrusaHandler)

    def printer(self, **extra):
        p = {"id": "mk4", "name": "MK4", "type": "prusalink", "host": "127.0.0.1", "http_port": self.port,
             "username": "maker", "password": self.password, "allow_remote_control": True,
             "allow_file_upload": True}
        p.update(extra)
        return p


class _PrusaHandler(_Handler):
    def _auth(self, method):
        h = self.headers.get("Authorization") or ""
        f = dict(re.findall(r'(\w+)="?([^",]*)"?', h))
        ok = False
        if h.startswith("Digest") and f.get("username") == "maker":
            ha1 = hashlib.md5(("maker:%s:%s" % (REALM, self.server.password)).encode()).hexdigest()
            ha2 = hashlib.md5(("%s:%s" % (method, f.get("uri"))).encode()).hexdigest()
            expect = hashlib.md5(("%s:%s:%s:%s:%s:%s" % (ha1, NONCE, f.get("nc"), f.get("cnonce"),
                                                         f.get("qop"), ha2)).encode()).hexdigest()
            ok = expect == f.get("response")
        if not ok:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Digest realm="%s", nonce="%s", qop="auth"' % (REALM, NONCE))
            self.send_header("Content-Length", "0")
            self.end_headers()
        return ok

    def do_GET(self):
        srv = self.server
        srv.calls.append(("GET", self.path))
        if not self._auth("GET"):
            return
        if self.path == "/api/v1/status":
            return self._json({"printer": {"state": srv.state, "temp_nozzle": 215.1, "target_nozzle": 215,
                                           "temp_bed": 60.2, "target_bed": 60, "axis_x": 1, "axis_y": 2,
                                           "axis_z": 0.4, "fan_print": 80},
                               "job": {"id": 7, "progress": 30, "time_remaining": 900, "time_printing": 400}})
        if self.path == "/api/v1/job":
            return self._json({"id": 7, "file": {"name": "PART~1.BGC", "display_name": "part.bgcode"}})
        if self.path == "/api/version":
            return self._json({"api": "2.0.0", "server": "2.1.2"})
        self._json({"error": "nf"}, 404)

    def do_HEAD(self):
        if not self._auth("HEAD"):
            return
        name = self.path.rsplit("/", 1)[1]
        self.send_response(200 if name in self.server.files else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        srv = self.server
        body = self._body()
        srv.calls.append(("PUT", self.path, dict(self.headers), body))
        if not self._auth("PUT"):
            return
        if self.path.startswith("/api/v1/files/usb/"):
            name = self.path.rsplit("/", 1)[1]
            if name in srv.files and self.headers.get("Overwrite-File") == "?0":
                return self._json({"title": "Conflict"}, 409)
            srv.uploads.append(body)
            srv.files.add(name)
            return self._json(None, 201)
        return self._json(None, 204)

    def do_DELETE(self):
        self.server.calls.append(("DELETE", self.path))
        if not self._auth("DELETE"):
            return
        self._json(None, 204)

    def do_POST(self):
        self.server.calls.append(("POST", self.path))
        if not self._auth("POST"):
            return
        self._json(None, 204)
