"""Klipper printers through Moonraker (the API behind Mainsail/Fluidd)."""
import http.client
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid

from . import config

log = logging.getLogger("dashboard.klipper")

_webcam_cache = {}
_webcam_lock = threading.Lock()
BUSY_STATES = ("printing", "paused")


def base_url(p):
    port = p.get("http_port", 80)
    return "http://%s/" % p["host"] if port == 80 else "http://%s:%d/" % (p["host"], port)


def http_json(p, path, timeout=5):
    req = urllib.request.Request(urllib.parse.urljoin(base_url(p), path))
    key = config.secret(p, "api_key")
    if key:
        req.add_header("X-Api-Key", key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(1_000_000).decode("utf-8"))


def webcam_url(p):
    """Webcam URL as configured in Mainsail, via Moonraker's webcam list."""
    base = base_url(p)
    if p.get("webcam_url"):
        return urllib.parse.urljoin(base, p["webcam_url"])
    with _webcam_lock:
        cached = _webcam_cache.get(p["id"])
        if cached and time.time() - cached[1] < 300 and cached[2] == base:
            return cached[0]
    url = urllib.parse.urljoin(base, "/webcam/?action=stream")
    try:
        cams = http_json(p, "/server/webcams/list").get("result", {}).get("webcams", [])
        for cam in cams:
            if not cam.get("enabled", True):
                continue
            service = cam.get("service", "") or ""
            mjpeg = service.startswith(("mjpegstreamer", "uv4l", "ipstream")) or not service
            path = cam.get("stream_url") if mjpeg else (cam.get("snapshot_url") or cam.get("stream_url"))
            if path:
                candidate = urllib.parse.urljoin(base, path)
                # Only follow URLs on the printer itself.
                if urllib.parse.urlsplit(candidate).hostname == p["host"]:
                    url = candidate
                break
    except Exception as e:
        log.info("webcam list for %s failed (%s), using default URL", p["id"], e)
    with _webcam_lock:
        _webcam_cache[p["id"]] = (url, time.time(), base)
    return url


def status(p):
    data = http_json(p, "/printer/objects/query?print_stats&display_status", timeout=3)
    st = data["result"]["status"]
    ps = st.get("print_stats", {})
    ds = st.get("display_status", {})
    return {
        "state": ps.get("state", "unknown"),
        "file": ps.get("filename", ""),
        "progress": ds.get("progress", 0) or 0,
        "duration": ps.get("print_duration", 0) or 0,
        "message": ps.get("message", "") or "",
    }


def upload(p, path, name, start):
    """Moonraker /server/files/upload (multipart), streamed from disk."""
    note = ""
    if start:
        try:
            state = status(p)["state"]
        except Exception:
            state = "unknown"
        if state in BUSY_STATES:
            start = False
            note = " (print not started: printer is %s)" % state

    boundary = uuid.uuid4().hex

    def field(key, value):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, key, value)).encode()

    head = field("root", "gcodes")
    if start:
        head += field("print", "true")
    head += ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    size = os.path.getsize(path)

    conn = http.client.HTTPConnection(p["host"], p.get("http_port", 80), timeout=120)
    try:
        conn.putrequest("POST", "/server/files/upload")
        conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        conn.putheader("Content-Length", str(len(head) + size + len(tail)))
        key = config.secret(p, "api_key")
        if key:
            conn.putheader("X-Api-Key", key)
        conn.endheaders()
        conn.send(head)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(tail)
        resp = conn.getresponse()
        body = resp.read(100_000).decode("utf-8", "replace")
    finally:
        conn.close()

    if resp.status == 401:
        raise RuntimeError("printer refused the upload (401). Add the dashboard PC to "
                           "trusted_clients in moonraker.conf, or set api_key for this printer")
    if resp.status not in (200, 201):
        raise RuntimeError("printer answered %d" % resp.status)
    if start:
        try:
            started = json.loads(body).get("print_started", True)
        except ValueError:
            started = True
        if not started:
            return "Uploaded to %s, but the print did not start" % p["name"]
        return "Uploaded to %s and print started" % p["name"]
    return "Uploaded to %s%s" % (p["name"], note)
