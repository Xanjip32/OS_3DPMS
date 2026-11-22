"""Fake Moonraker with object discovery, full object status and a JSON-RPC WebSocket.

Used to test the Moonraker adapter without a printer. Objects can be removed (printers without
a bed, sensors...), status deltas pushed over the socket, and connections dropped.
"""
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dashboard import wsclient

OBJECTS = {
    "webhooks": {"state": "ready", "state_message": "Printer is ready"},
    "print_stats": {"state": "printing", "filename": "benchy.gcode", "print_duration": 600.0,
                    "message": "", "info": {"current_layer": 12, "total_layer": 120}},
    "display_status": {"progress": 0.25},
    "virtual_sdcard": {"progress": 0.25},
    "toolhead": {"position": [10.0, 20.0, 3.0, 100.0], "extruder": "extruder"},
    "extruder": {"temperature": 214.8, "target": 215.0},
    "heater_bed": {"temperature": 59.9, "target": 60.0},
    "fan": {"speed": 0.5},
    "motion_report": {"live_position": [11.0, 21.0, 3.0, 101.0]},
    "temperature_sensor chamber": {"temperature": 31.0},
    "filament_switch_sensor runout": {"enabled": True, "filament_detected": True},
    "configfile": {"config": {"huge": "x"}},
}


def read_client_frame(rfile):
    head = rfile.read(2)
    if len(head) < 2:
        return None, None
    op, n = head[0] & 0x0F, head[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(rfile.read(n)))
    return op, data


class MockMoonraker(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, objects=None, websocket=True, api_key=""):
        self.objects = json.loads(json.dumps(objects if objects is not None else OBJECTS))
        self.websocket = websocket
        self.api_key = api_key
        self.requests = []
        self.posts = []
        self.sockets = []
        self.ws_count = 0
        self.metadata = {"benchy.gcode": {"estimated_time": 2400}}
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def printer(self, **extra):
        p = {"id": "sapphire", "name": "Sapphire Pro", "type": "klipper", "host": "127.0.0.1",
             "http_port": self.port, "websocket": True, "allow_remote_control": True, "allow_file_upload": True}
        p.update(extra)
        return p

    def push(self, delta):
        """Send notify_status_update to every open WebSocket and apply it here too."""
        for name, values in delta.items():
            self.objects.setdefault(name, {}).update(values)
        msg = json.dumps({"jsonrpc": "2.0", "method": "notify_status_update", "params": [delta, time.time()]})
        self._broadcast(msg.encode())

    def notify(self, method):
        self._broadcast(json.dumps({"jsonrpc": "2.0", "method": method}).encode())

    def _broadcast(self, data):
        with self.lock:
            for s in list(self.sockets):
                try:
                    s.sendall(wsclient.server_frame(1, data))
                except OSError:
                    self.sockets.remove(s)

    def drop_sockets(self):
        with self.lock:
            for s in self.sockets:
                try:
                    s.shutdown(2)
                except OSError:
                    pass
            self.sockets = []

    def stop(self):
        self.drop_sockets()
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        key = self.server.api_key
        if not key:
            return True
        if self.headers.get("X-Api-Key") == key:
            return True
        return "token=oneshot" in self.path

    def do_GET(self):
        srv = self.server
        srv.requests.append(self.path)
        if not self._authorized():
            return self._json({"error": {"message": "Unauthorized"}}, 401)
        if self.path.startswith("/websocket"):
            return self._websocket()
        if self.path.startswith("/access/oneshot_token"):
            return self._json({"result": "oneshot"})
        if self.path.startswith("/printer/objects/list"):
            return self._json({"result": {"objects": list(srv.objects)}})
        if self.path.startswith("/printer/objects/query"):
            import urllib.parse
            names = [urllib.parse.unquote(x.split("=")[0]) for x in self.path.split("?", 1)[1].split("&")]
            return self._json({"result": {"eventtime": 1.0, "status": {
                n: srv.objects[n] for n in names if n in srv.objects}}})
        if self.path.startswith("/server/files/metadata"):
            import urllib.parse
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("filename", [""])[0]
            if name in srv.metadata:
                return self._json({"result": srv.metadata[name]})
            return self._json({"error": {"message": "not found"}}, 404)
        if self.path.startswith("/server/files/list"):
            return self._json({"result": [{"path": "benchy.gcode", "size": 1234, "modified": 1.0}]})
        if self.path.startswith("/server/history/list"):
            return self._json({"result": {"count": 1, "jobs": [
                {"filename": "cube.gcode", "status": "completed", "start_time": 1.0, "end_time": 2.0,
                 "print_duration": 1.0}]}})
        if self.path.startswith("/server/info"):
            return self._json({"result": {"klippy_state": "ready", "klippy_connected": True,
                                          "moonraker_version": "v0.9"}})
        if self.path.startswith(("/machine/", "/server/history/totals")):
            return self._json({"result": {}})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.posts.append((self.path, body))
        if not self._authorized():
            return self._json({"error": {"message": "Unauthorized"}}, 401)
        self._json({"result": "ok"})

    def _websocket(self):
        srv = self.server
        if not srv.websocket or self.headers.get("Upgrade", "").lower() != "websocket":
            return self._json({"error": "no websocket"}, 404)
        key = self.headers["Sec-WebSocket-Key"]
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsclient.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        sock = self.connection
        with srv.lock:
            srv.sockets.append(sock)
            srv.ws_count += 1
        self.close_connection = True
        while True:
            try:
                op, data = read_client_frame(self.rfile)
            except OSError:
                return
            if op is None or op == 8:
                return
            if op == 9:
                sock.sendall(wsclient.server_frame(0xA, data))
                continue
            if op != 1:
                continue
            msg = json.loads(data)
            method, rid = msg.get("method"), msg.get("id")
            if method == "printer.objects.list":
                result = {"objects": list(srv.objects)}
            elif method == "printer.objects.subscribe":
                names = (msg.get("params") or {}).get("objects") or {}
                result = {"eventtime": 1.0, "status": {n: srv.objects[n] for n in names if n in srv.objects}}
            else:
                result = "ok"
            try:
                sock.sendall(wsclient.server_frame(1, json.dumps({"jsonrpc": "2.0", "id": rid,
                                                                    "result": result}).encode()))
            except OSError:
                return
