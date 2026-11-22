"""Minimal WebSocket client (RFC 6455), standard library only.

Enough for Moonraker's JSON-RPC: text frames, ping/pong, close, fragmented messages.
No compression extensions. Used for ws:// on the local network; wss:// works with an
ssl context passed in.
"""
import base64
import hashlib
import os
import socket
import struct
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 8 * 1024 * 1024
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(ConnectionError):
    pass


class WebSocket:
    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self.buf = bytearray(leftover)
        self.closed = False

    # ------------------------------------------------------------ sending

    def _send_frame(self, opcode, payload=b""):
        if self.closed:
            raise WebSocketError("websocket closed")
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self, data=b"hb"):
        self._send_frame(OP_PING, data)

    def close(self):
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
            except OSError:
                pass
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ receiving

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)  # socket.timeout propagates to the caller
            if not chunk:
                self.closed = True
                raise WebSocketError("connection closed by the printer")
            self.buf += chunk

    def _read_frame(self):
        self._fill(2)
        b0, b1 = self.buf[0], self.buf[1]
        n = b1 & 0x7F
        pos = 2
        if n == 126:
            self._fill(4)
            n = struct.unpack(">H", bytes(self.buf[2:4]))[0]
            pos = 4
        elif n == 127:
            self._fill(10)
            n = struct.unpack(">Q", bytes(self.buf[2:10]))[0]
            pos = 10
        if n > MAX_MESSAGE:
            raise WebSocketError("message too large")
        masked = b1 & 0x80
        mask = b""
        if masked:
            self._fill(pos + 4)
            mask = bytes(self.buf[pos:pos + 4])
            pos += 4
        self._fill(pos + n)
        payload = bytes(self.buf[pos:pos + n])
        del self.buf[:pos + n]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(b0 & 0x80), b0 & 0x0F, payload

    def recv(self):
        """Next text/binary message as str/bytes. Answers pings; returns None for a pong.
        Raises socket.timeout when nothing arrives in the socket timeout."""
        parts, kind = [], None
        while True:
            fin, op, payload = self._read_frame()
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if op == OP_PONG:
                if not parts:
                    return None
                continue
            if op == OP_CLOSE:
                self.closed = True
                raise WebSocketError("printer closed the websocket")
            if op in (OP_TEXT, OP_BIN):
                kind, parts = op, [payload]
            elif op == OP_CONT and kind is not None:
                parts.append(payload)
            if sum(len(p) for p in parts) > MAX_MESSAGE:
                raise WebSocketError("message too large")
            if fin and kind is not None:
                data = b"".join(parts)
                return data.decode("utf-8", "replace") if kind == OP_TEXT else data


def connect(url, timeout=5.0, headers=None, ssl_context=None):
    """Open a WebSocket. Raises OSError / WebSocketError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise WebSocketError("not a ws:// URL")
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    sock = socket.create_connection((parts.hostname, port), timeout=timeout)
    try:
        if parts.scheme == "wss":
            sock = ssl_context.wrap_socket(sock, server_hostname=parts.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        host = parts.hostname if parts.port is None else "%s:%d" % (parts.hostname, parts.port)
        lines = ["GET %s HTTP/1.1" % path, "Host: %s" % host, "Upgrade: websocket",
                 "Connection: Upgrade", "Sec-WebSocket-Key: %s" % key, "Sec-WebSocket-Version: 13"]
        for k, v in (headers or {}).items():
            lines.append("%s: %s" % (k, v))
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        resp = bytearray()
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("no websocket handshake answer")
            resp += chunk
            if len(resp) > 65536:
                raise WebSocketError("handshake answer too large")
        head, _, rest = bytes(resp).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise WebSocketError("websocket refused: %s" % lines[0][:80])
        fields = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if fields.get("sec-websocket-accept") != expect:
            raise WebSocketError("bad websocket handshake")
        return WebSocket(sock, rest)
    except BaseException:
        sock.close()
        raise


def accept_key(key):
    """Server side helper (tests): the Sec-WebSocket-Accept value for a client key."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def server_frame(opcode, payload):
    """Server side helper (tests): one unmasked frame."""
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | opcode, n])
    elif n < 65536:
        head = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
    return head + payload


__all__ = ["connect", "WebSocket", "WebSocketError", "socket"]
