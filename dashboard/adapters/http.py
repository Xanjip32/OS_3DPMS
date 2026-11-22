"""Small HTTP helpers shared by the HTTP adapters (OctoPrint, PrusaLink).

Standard library only. Requests go to the configured printer only: redirects to another
host are refused, so a printer cannot send the dashboard (or its API key) elsewhere.
Includes HTTP Digest authentication (PrusaLink) computed by hand, so a streamed upload
does not have to be sent twice.
"""
import hashlib
import http.client
import json
import os
import re
import uuid

MAX_BODY = 2_000_000


class HTTPStatusError(OSError):
    def __init__(self, status, body=""):
        super().__init__("HTTP %d" % status)
        self.status = status
        self.body = body


def _parse_challenge(header):
    fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header or ""))
    return fields if (header or "").lower().startswith("digest") else None


def digest_header(challenge, method, uri, user, password, nc=1):
    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    ha1 = hashlib.md5(("%s:%s:%s" % (user, realm, password)).encode()).hexdigest()
    ha2 = hashlib.md5(("%s:%s" % (method, uri)).encode()).hexdigest()
    qop = "auth" if "auth" in (challenge.get("qop") or "").split(",") else ""
    if qop:
        cnonce = uuid.uuid4().hex[:16]
        ncs = "%08x" % nc
        resp = hashlib.md5(("%s:%s:%s:%s:%s:%s" % (ha1, nonce, ncs, cnonce, qop, ha2)).encode()).hexdigest()
        extra = ', qop=%s, nc=%s, cnonce="%s"' % (qop, ncs, cnonce)
    else:
        resp = hashlib.md5(("%s:%s:%s" % (ha1, nonce, ha2)).encode()).hexdigest()
        extra = ""
    out = 'Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s"%s' % (
        user, realm, nonce, uri, resp, extra)
    if challenge.get("opaque"):
        out += ', opaque="%s"' % challenge["opaque"]
    return out


class Client:
    """HTTP client for one printer. `auth` is ("apikey", key) or ("digest", user, password)."""

    def __init__(self, host, port, auth=None, timeout=5):
        self.host, self.port, self.auth, self.timeout = host, port, auth, timeout
        self._challenge = None
        self._nc = 0

    def _headers(self, method, uri, extra=None):
        h = dict(extra or {})
        if self.auth and self.auth[0] == "apikey" and self.auth[1]:
            h["X-Api-Key"] = self.auth[1]
        elif self.auth and self.auth[0] == "digest" and self._challenge:
            self._nc += 1
            h["Authorization"] = digest_header(self._challenge, method, uri, self.auth[1], self.auth[2], self._nc)
        return h

    def request(self, method, uri, body=None, headers=None, timeout=None, stream_path=None, prefix=b"", suffix=b""):
        """(status, headers, body bytes). Follows no redirects. Retries once on a Digest challenge
        (only before any file data is sent: the challenge is fetched first with an empty GET)."""
        for attempt in (1, 2):
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
            try:
                h = self._headers(method, uri, headers)
                if stream_path:
                    size = len(prefix) + os.path.getsize(stream_path) + len(suffix)
                    conn.putrequest(method, uri)
                    for k, v in h.items():
                        conn.putheader(k, v)
                    conn.putheader("Content-Length", str(size))
                    conn.endheaders()
                    conn.send(prefix)
                    with open(stream_path, "rb") as f:
                        while True:
                            chunk = f.read(1 << 20)
                            if not chunk:
                                break
                            conn.send(chunk)
                    conn.send(suffix)
                else:
                    conn.request(method, uri, body=body, headers=h)
                resp = conn.getresponse()
                data = resp.read(MAX_BODY)
                rheaders = {k.lower(): v for k, v in resp.getheaders()}
            finally:
                conn.close()
            if resp.status == 401 and attempt == 1 and self.auth and self.auth[0] == "digest":
                challenge = _parse_challenge(rheaders.get("www-authenticate"))
                if challenge:
                    self._challenge, self._nc = challenge, 0
                    if stream_path:  # do not send the file twice: the caller retries after prime()
                        return resp.status, rheaders, data
                    continue
            return resp.status, rheaders, data
        return resp.status, rheaders, data

    def prime(self, uri="/api/version"):
        """Fetch a Digest challenge before a streamed upload."""
        if self.auth and self.auth[0] == "digest" and self._challenge is None:
            self.request("GET", uri)

    def json(self, method, uri, payload=None, ok=(200, 201, 204), timeout=None):
        body, headers = None, {}
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, _, data = self.request(method, uri, body, headers, timeout)
        if status not in ok:
            raise HTTPStatusError(status, data[:300].decode("utf-8", "replace"))
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:
            raise OSError("answer from %s is not JSON" % self.host) from None


def multipart(fields, file_field, filename):
    """(boundary, prefix bytes, suffix bytes) around a streamed file."""
    boundary = uuid.uuid4().hex
    head = b""
    for k, v in fields.items():
        head += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode()
    head += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, file_field, filename)).encode("utf-8")
    return boundary, head, ("\r\n--%s--\r\n" % boundary).encode()
