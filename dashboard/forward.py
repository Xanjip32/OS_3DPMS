"""Local TCP forward to a printer's Mainsail page.

Listens on 127.0.0.1 only. `tailscale serve` publishes it to the tailnet
over HTTPS, so Mainsail (including its websocket) never touches the LAN
from this PC.
"""
import logging
import os
import re
import socket
import threading

log = logging.getLogger("dashboard.forward")
FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def local_or_tailnet(host_header):
    """True for a Host header a browser sends to this PC itself or to its Tailscale name.

    Anything else is a website that pointed its own name at this PC (DNS rebinding) to use
    the dashboard or Mainsail from a browser here.
    """
    host = (host_header or "").strip().lower()
    if host.count(":") == 1:
        host = host.split(":")[0]
    return host in ("127.0.0.1", "localhost") or host.endswith(".ts.net")


class Forwarder:
    def __init__(self, listen_port, host, port, listen_host="127.0.0.1", max_connections=128):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.host = host
        self.port = port
        self.slots = threading.BoundedSemaphore(max_connections)
        self.sock = None
        self.closed = False

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":  # on Windows SO_REUSEADDR lets two processes share a port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.listen_host, self.listen_port))
            s.listen(64)
        except OSError:
            s.close()
            raise
        s.settimeout(1.0)
        self.sock = s
        threading.Thread(target=self._accept, daemon=True).start()
        log.info("Mainsail %s:%d on %s:%d", self.host, self.port, self.listen_host, self.listen_port)

    def stop(self):
        self.closed = True
        if self.sock:
            self.sock.close()

    def _accept(self):
        while not self.closed:
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            client.settimeout(None)
            threading.Thread(target=self._pipe, args=(client,), daemon=True).start()

    def _pipe(self, client):
        try:
            # A browser connection belongs to one site: checking the first request's Host is enough.
            head = b""
            upstream = None
            try:
                client.settimeout(10)
                while b"\r\n\r\n" not in head and len(head) < 16384:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    head += chunk
                host = re.search(rb"\r\nhost:[ \t]*([^\r\n]*)", head, re.I)
                if not host or not local_or_tailnet(host.group(1).decode("latin-1")):
                    client.sendall(FORBIDDEN)
                    client.close()
                    return
                client.settimeout(None)
                upstream = socket.create_connection((self.host, self.port), timeout=5)
                upstream.settimeout(None)
                upstream.sendall(head)
            except OSError:
                for s in (client, upstream):
                    if s is not None:
                        s.close()
                return

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    for s in (src, dst):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            t = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            t.start()
            pump(client, upstream)
            t.join()
            client.close()
            upstream.close()
        finally:
            self.slots.release()
