"""Fake Moonraker/Mainsail printer for tests and UI previews."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRAME = b"\xff\xd8" + b"\x00fake-jpeg-data" * 50 + b"\xff\xd9"


class MockPrinter(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0):
        self.uploads = []
        self.state = "standby"
        super().__init__(("127.0.0.1", port), _Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/server/webcams/list"):
            self._json({"result": {"webcams": [
                {"enabled": True, "service": "mjpegstreamer", "stream_url": "/webcam/?action=stream"}]}})
        elif self.path.startswith("/webcam"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=b")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"--b\r\nContent-Type: image/jpeg\r\n\r\n" + FRAME + b"\r\n")
                    time.sleep(0.2)
            except OSError:
                pass
        elif self.path.startswith("/printer/objects/query"):
            self._json({"result": {"status": {
                "print_stats": {"state": self.server.state, "filename": "part.gcode"},
                "display_status": {"progress": 0.42}}}})
        else:
            body = b"<title>Mainsail</title>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.uploads.append(body)
        self._json({"result": {"item": {"path": "x"}}, "print_started": b'name="print"' in body}, 201)


if __name__ == "__main__":
    import sys
    printer = MockPrinter(int(sys.argv[1]) if len(sys.argv) > 1 else 9001)
    print("mock printer on port", printer.port)
    while True:
        time.sleep(3600)
