"""Live camera link to the cloud dashboard: the WebSocket client and what the PC sends over it."""
import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from dashboard import cameras, cloud, wsclient

JPEG = b"\xff\xd8" + b"picture" * 50 + b"\xff\xd9"


class FakeRelay:
    """A WebSocket server on 127.0.0.1 with just enough of RFC 6455 to stand in for Cloudflare."""

    def __init__(self, status=101, greeting=None):
        self.status = status
        self.greeting = greeting      # text sent right after the handshake
        self.request = b""
        self.received = []            # (opcode, payload) from the client
        self.conn = None
        self.ready = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()
        if self.conn:
            self.conn.close()

    def send(self, opcode, payload):
        n = len(payload)
        head = struct.pack("!BB", 0x80 | opcode, n) if n < 126 else struct.pack("!BBH", 0x80 | opcode, 126, n)
        self.conn.sendall(head + payload)

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self):
        try:
            self.conn, _ = self.sock.accept()
        except OSError:
            return
        while b"\r\n\r\n" not in self.request:
            self.request += self.conn.recv(4096)
        if self.status != 101:
            self.conn.sendall(b"HTTP/1.1 %d No\r\nContent-Length: 0\r\n\r\n" % self.status)
            self.conn.close()
            return
        key = [line.split(b":", 1)[1].strip() for line in self.request.split(b"\r\n")
               if line.lower().startswith(b"sec-websocket-key:")][0]
        accept = base64.b64encode(hashlib.sha1(key + wsclient.GUID).digest())
        self.conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        if self.greeting:
            self.send(1, self.greeting.encode())
        self.ready.set()
        try:
            while True:
                b0, b1 = self._recv_exact(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._recv_exact(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._recv_exact(8))[0]
                assert b1 & 0x80, "client frames must be masked"
                key = self._recv_exact(4)
                self.received.append((b0 & 0x0F, wsclient._mask(self._recv_exact(n), key)))
        except (ConnectionError, OSError):
            pass

    def wait_for(self, check, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if check():
                return True
            time.sleep(0.02)
        return False


class WebSocketClientTests(unittest.TestCase):
    def test_handshake_messages_ping_and_close(self):
        relay = FakeRelay()
        self.addCleanup(relay.close)
        ws = wsclient.WebSocket.connect("ws://127.0.0.1:%d/connector/live" % relay.port,
                                        {"Authorization": "Bearer k"}, timeout=5)
        self.assertIn(b"GET /connector/live HTTP/1.1", relay.request)
        self.assertIn(b"Authorization: Bearer k", relay.request)
        big = bytes(range(256)) * 300   # longer than 65535: the 8-byte length form
        ws.send(big)
        ws.send_json({"hello": 1})
        relay.ready.wait(5)
        relay.send(1, b'{"cameras":["voron"]}')
        relay.send(9, b"are you there")
        messages = []
        self.assertTrue(relay.wait_for(lambda: messages.extend(ws.poll(0.05)) or messages))
        self.assertEqual(messages, ['{"cameras":["voron"]}'])
        self.assertTrue(relay.wait_for(lambda: len(relay.received) >= 3))
        self.assertEqual(relay.received[0], (2, big))
        self.assertEqual(relay.received[1], (1, b'{"hello":1}'))
        self.assertEqual(relay.received[2], (10, b"are you there"))   # ping answered with pong
        relay.send(8, b"")
        with self.assertRaises(ConnectionError):
            for _ in range(100):
                ws.poll(0.05)

    def test_refused_upgrade_reports_the_http_status(self):
        relay = FakeRelay(status=404)
        self.addCleanup(relay.close)
        with self.assertRaises(wsclient.HandshakeRefused) as caught:
            wsclient.WebSocket.connect("ws://127.0.0.1:%d/connector/live" % relay.port, timeout=5)
        self.assertEqual(caught.exception.status, 404)

    def test_a_frame_split_across_reads_is_kept_whole(self):
        ws = wsclient.WebSocket(mock.MagicMock())
        frame = struct.pack("!BBH", 0x82, 126, 300) + b"x" * 300
        ws.buf += frame[:150]
        self.assertIsNone(ws._parse())
        ws.buf += frame[150:]
        self.assertEqual(ws._parse(), (2, b"x" * 300))


class FakeApp:
    def __init__(self, link):
        self.lock = threading.Lock()
        self.cfg = {"cloud": {"url": link, "key": "k" * 43}, "notifications": {"events": {}}}
        hub = cameras.CameraHub("voron", None)
        hub.touch = mock.Mock()
        hub.frame, hub.frame_time = JPEG, time.time()
        self.hubs = {"voron": hub}
        self.printers = {"voron": {"id": "voron", "name": "Voron", "type": "klipper", "rotate": 0,
                                   "flip_horizontal": False, "flip_vertical": False}}
        self.monitor = mock.MagicMock()
        self.monitor.events_after.return_value = ([], 0)
        self.monitor.status = {}


class CameraLinkTests(unittest.TestCase):
    def test_sends_the_pictures_the_cloud_asks_for(self):
        relay = FakeRelay(greeting='{"cameras":["voron"]}')
        self.addCleanup(relay.close)
        app = FakeApp("http://127.0.0.1:%d" % relay.port)
        connector = cloud.Connector(app)
        connector.cameras.want(["voron"])
        self.assertTrue(relay.wait_for(lambda: any(op == 2 for op, _ in relay.received)), relay.received)
        opcode, payload = next(item for item in relay.received if item[0] == 2)
        self.assertEqual(payload, b"\x05voron" + JPEG)
        self.assertIn(b"GET /connector/live", relay.request)
        self.assertIn(b"Authorization: Bearer " + b"k" * 43, relay.request)
        self.assertTrue(connector.cameras.live)
        app.hubs["voron"].touch.assert_called()
        relay.send(1, b'{"cameras":[]}')   # nobody watches any more: the link closes after LINK_IDLE
        with mock.patch.object(cloud, "LINK_IDLE", 0):
            self.assertTrue(relay.wait_for(lambda: not connector.cameras.live))

    def test_old_relay_without_live_link_gets_uploads(self):
        relay = FakeRelay(status=404)
        self.addCleanup(relay.close)
        app = FakeApp("http://127.0.0.1:%d" % relay.port)
        connector = cloud.Connector(app)
        connector.cameras.want(["voron"])
        self.assertTrue(relay.wait_for(lambda: connector.cameras.retry_at > 0))
        connector.cameras.thread.join(5)
        self.assertFalse(connector.cameras.live)
        with mock.patch.object(connector, "request", return_value={"cameras": ["voron"]}) as send:
            connector.sync(app.cfg["cloud"])
        self.assertEqual(send.call_args_list[-1].args[2:], (JPEG, "voron"))

    def test_one_failing_upload_does_not_stop_the_others(self):
        app = FakeApp("https://printers.account.workers.dev")
        second = cameras.CameraHub("p1s", None)
        second.touch = mock.Mock()
        second.frame, second.frame_time = JPEG, time.time()
        app.hubs["p1s"] = second
        connector = cloud.Connector(app)
        calls = []

        def request(link, key, body, camera=None):
            calls.append(camera)
            if camera == "voron":
                raise TimeoutError
        with mock.patch.object(connector, "request", side_effect=request):
            connector.upload("link", "key", app.hubs, ["voron", "p1s"])
        self.assertEqual(calls, ["voron", "p1s"])

    def test_report_explains_a_camera_without_pictures(self):
        app = FakeApp("https://printers.account.workers.dev")
        hub = app.hubs["voron"]
        hub.frame_time = time.time() - 60
        hub.error = "the printer's camera service answered HTTP 500"
        connector = cloud.Connector(app)
        with mock.patch.object(connector, "request", return_value={"cameras": []}) as send, \
                mock.patch.object(connector.cameras, "want"):
            connector.sync(app.cfg["cloud"])
        report = json.loads(send.call_args.args[2])
        self.assertEqual(report["printers"][0]["camera_error"], hub.error)
        hub.frame_time = time.time()
        self.assertEqual(cloud.Connector.camera_error(hub), "")


if __name__ == "__main__":
    unittest.main()
