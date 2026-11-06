"""TLS for Bambu printers: self-signed certificates, pinned on first use.

Bambu printers ship self-signed certificates, so normal verification is not
possible. Instead the certificate fingerprint is saved the first time and
every later connection must present the same one. This stops another device
on the network from impersonating the printer.
"""
import hashlib
import json
import logging
import os
import socket
import ssl
import threading

log = logging.getLogger("dashboard.tls")


class CertificateChanged(ssl.SSLError):
    pass


def unverified_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PinStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.pins = json.load(f)
        except (OSError, ValueError):
            self.pins = {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.pins, f, indent=2)
        os.replace(tmp, self.path)

    def check(self, tls_sock, host, port):
        der = tls_sock.getpeercert(binary_form=True)
        if not der:
            raise CertificateChanged("printer %s sent no certificate" % host)
        fingerprint = hashlib.sha256(der).hexdigest()
        key = "%s:%d" % (host, port)
        with self.lock:
            known = self.pins.get(key)
            if known is None:
                self.pins[key] = fingerprint
                self._save()
                log.info("pinned certificate for %s", key)
            elif known != fingerprint:
                raise CertificateChanged(
                    "certificate of %s changed. If the printer was reset or replaced, "
                    "delete '%s' from pins.json" % (key, key))


def connect(host, port, pins, timeout=10):
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        sock = unverified_context().wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    try:
        pins.check(sock, host, port)
    except Exception:
        sock.close()
        raise
    return sock
