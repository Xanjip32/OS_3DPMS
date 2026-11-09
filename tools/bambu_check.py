"""Check a Bambu printer's LAN access code without the dashboard.

Usage (on the PC that can reach the printer):
    python tools\\bambu_check.py 192.168.0.6

It asks for the access code (typing is hidden), then tries the status login
(MQTT, port 8883) and the camera (port 6000) and says which one works.
Nothing is saved.
"""
import getpass
import socket
import ssl
import struct
import sys


def tls(host, port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Bambu printers use self-signed certificates
    raw = socket.create_connection((host, port), timeout=8)
    return ctx.wrap_socket(raw, server_hostname=host)


def s16(b):
    return struct.pack(">H", len(b)) + b


def check_mqtt(host, code):
    with tls(host, 8883) as s:
        var = s16(b"MQTT") + bytes([4, 0xC2]) + struct.pack(">H", 60)
        payload = s16(b"os3dpms-check") + s16(b"bblp") + s16(code.encode())
        body = var + payload
        s.sendall(bytes([0x10, len(body)]) + body)
        reply = s.recv(4)
    if len(reply) < 4 or reply[0] != 0x20:
        return "no proper reply (%r)" % reply
    rc = reply[3]
    return {0: "OK, access code accepted",
            4: "REFUSED: wrong user name or access code",
            5: "REFUSED: not authorized (access code, or Developer Mode needed)"}.get(rc, "refused, code %d" % rc)


def check_camera(host, code):
    with tls(host, 6000) as s:
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
        auth += b"bblp".ljust(32, b"\0") + code.encode().ljust(32, b"\0")
        s.sendall(auth)
        try:
            header = s.recv(16)
        except socket.timeout:
            return "connected but no picture within 8 s (LAN Mode Liveview off?)"
    if not header:
        return "REFUSED: printer closed the connection (access code)"
    size = struct.unpack("<I", header[:4].ljust(4, b"\0"))[0]
    return "OK, picture coming (%d bytes)" % size


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else input("Printer IP: ").strip()
    code = getpass.getpass("LAN access code (hidden): ").strip()
    print("Code length: %d characters" % len(code))
    for name, fn in (("Status login (8883)", check_mqtt), ("Camera (6000)", check_camera)):
        try:
            print("%-20s %s" % (name, fn(host, code)))
        except OSError as e:
            print("%-20s cannot connect: %s" % (name, e))


if __name__ == "__main__":
    main()
