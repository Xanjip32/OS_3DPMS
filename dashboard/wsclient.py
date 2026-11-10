"""A small WebSocket client (RFC 6455): Python's standard library has none.

Used for the live camera link to the cloud dashboard. One thread uses a connection:
it sends with send() and reads what arrived with poll(), so no read is ever cut in half.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import urllib.parse

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 1 << 20


class HandshakeRefused(ConnectionError):
    """The server answered the upgrade with a normal HTTP status (no WebSocket there)."""

    def __init__(self, status):
        super().__init__("server answered HTTP %d instead of opening a WebSocket" % status)
        self.status = status


def _mask(data, key):
    if not data:
        return b""
    n = len(data)
    pad = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(pad, "little")).to_bytes(n, "little")


class WebSocket:
    def __init__(self, sock, buffered=b"", timeout=20):
        self.sock = sock
        self.buf = bytearray(buffered)
        self.timeout = timeout
        self.closed = False

    @classmethod
    def connect(cls, url, headers=None, timeout=20, context=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise ValueError("not a WebSocket address")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        try:
            if parts.scheme == "wss":
                sock = (context or ssl.create_default_context()).wrap_socket(sock, server_hostname=parts.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            host = parts.hostname if parts.port is None else "%s:%d" % (parts.hostname, parts.port)
            lines = ["GET %s HTTP/1.1" % ((parts.path or "/") + ("?" + parts.query if parts.query else "")),
                     "Host: " + host, "Upgrade: websocket", "Connection: Upgrade",
                     "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
            lines += ["%s: %s" % item for item in (headers or {}).items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk or len(head) > 16384:
                    raise ConnectionError("server closed the connection during the WebSocket handshake")
                head += chunk
            head, rest = head.split(b"\r\n\r\n", 1)
            status_line, *fields = head.decode("latin-1").split("\r\n")
            words = status_line.split()
            status = int(words[1]) if len(words) > 1 and words[1].isdigit() else 0
            if status != 101:
                raise HandshakeRefused(status)
            found = {k.strip().lower(): v.strip() for k, v in (f.split(":", 1) for f in fields if ":" in f)}
            expected = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
            if found.get("sec-websocket-accept") != expected:
                raise ConnectionError("server did not confirm the WebSocket handshake")
        except BaseException:
            sock.close()
            raise
        return cls(sock, rest, timeout)

    # ---------------------------------------------------------------- sending
    def _frame(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        key = os.urandom(4)  # clients must mask what they send
        self.sock.sendall(head + key + _mask(payload, key))

    def send(self, data):
        """bytes go as a binary message, str as text."""
        if isinstance(data, str):
            self._frame(1, data.encode("utf-8"))
        else:
            self._frame(2, bytes(data))

    def send_json(self, value):
        self.send(json.dumps(value, separators=(",", ":")))

    def ping(self):
        self._frame(9, b"")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.settimeout(2)
            self._frame(8, struct.pack("!H", 1000))
        except OSError:
            pass
        finally:
            self.sock.close()

    # ---------------------------------------------------------------- receiving
    def poll(self, timeout):
        """Messages that arrive within `timeout` seconds: str (text) or bytes (binary).

        Pings are answered. Raises ConnectionError when the server closed the connection.
        """
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk
            while isinstance(self.sock, ssl.SSLSocket) and self.sock.pending():
                self.buf += self.sock.recv(65536)
        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(self.timeout)
        messages = []
        while True:
            frame = self._parse()
            if frame is None:
                return messages
            opcode, payload = frame
            if opcode == 8:
                self.close()
                raise ConnectionError("server closed the connection")
            if opcode == 9:
                self._frame(10, payload)
            elif opcode == 1:
                messages.append(payload.decode("utf-8", "replace"))
            elif opcode == 2:
                messages.append(payload)

    def _parse(self):
        """One complete frame from the buffer as (opcode, payload), or None if it has not all arrived."""
        buf = self.buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        if b0 & 0x0F == 0 or not b0 & 0x80:
            raise ConnectionError("fragmented WebSocket messages are not supported")
        n, pos = b1 & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, pos = struct.unpack_from("!H", buf, 2)[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, pos = struct.unpack_from("!Q", buf, 2)[0], 10
        if n > MAX_MESSAGE:
            raise ConnectionError("WebSocket message too large")
        key = None
        if b1 & 0x80:
            if len(buf) < pos + 4:
                return None
            key, pos = bytes(buf[pos:pos + 4]), pos + 4
        if len(buf) < pos + n:
            return None
        payload = bytes(buf[pos:pos + n])
        del buf[:pos + n]
        return b0 & 0x0F, _mask(payload, key) if key else payload
