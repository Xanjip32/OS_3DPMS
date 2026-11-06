"""Local TCP forward to a printer's Mainsail page.

Listens on 127.0.0.1 only. `tailscale serve` publishes it to the tailnet
over HTTPS, so Mainsail (including its websocket) never touches the LAN
from this PC.
"""
import logging
import os
import socket
import threading

log = logging.getLogger("dashboard.forward")


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
            try:
                upstream = socket.create_connection((self.host, self.port), timeout=5)
                upstream.settimeout(None)
            except OSError:
                client.close()
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
