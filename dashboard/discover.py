"""Find printers on the local network.

* Bambu Lab printers announce themselves every few seconds (SSDP NOTIFY to UDP port 2021),
  including their serial number, model and firmware. The listener only receives; it never
  answers or sends anything.
* Klipper printers are found by asking each address of this PC's local /24 networks for
  Moonraker's /server/info (port 80 through Mainsail/Fluidd, or Moonraker's own 7125).

Only private (RFC 1918) networks are searched, and only when someone clicks
"Find printers" in setup.
"""
import concurrent.futures
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("dashboard.discover")

BAMBU_PORT = 2021
MAX_PACKET = 4096
SERIAL_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")
# DevModel.bambu.com values. Models marked rtsp stream their camera on port 322 instead of 6000.
BAMBU_MODELS = {
    "C11": ("P1P", False), "C12": ("P1S", False), "N1": ("A1 mini", False), "N2S": ("A1", False),
    "BL-P001": ("X1 Carbon", True), "BL-P002": ("X1", True), "C13": ("X1E", True),
    "O1D": ("H2D", True), "O1S": ("H2S", True), "N7": ("P2S", True),
}


def _clean(text, limit=64):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(text or "")).strip()[:limit]


def parse_bambu(data, ip):
    """Printer details from one Bambu announcement (sent from ip), or None if it is not one."""
    text = data[:MAX_PACKET].decode("utf-8", "replace")
    lines = re.split(r"\r?\n", text)
    if not lines or not lines[0].upper().startswith(("NOTIFY ", "HTTP/1.1 200")):
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if "bambulab" not in (headers.get("nt", "") + headers.get("st", "")).lower():
        return None
    serial = headers.get("usn", "")
    if not SERIAL_RE.match(serial):
        return None
    code = _clean(headers.get("devmodel.bambu.com"), 16)
    model, rtsp = BAMBU_MODELS.get(code, (code or "Bambu Lab", False))
    return {
        "type": "bambu",
        "host": ip,  # where the packet came from, not what it claims
        "serial": serial,
        "name": _clean(headers.get("devname.bambu.com")) or "Bambu %s" % model,
        "model": model,
        "firmware": _clean(headers.get("devversion.bambu.com"), 32),
        "lan_only": headers.get("devconnect.bambu.com", "").lower() == "lan",
        "rtsp_camera": rtsp,
    }


class BambuListener:
    """Collects Bambu announcements while someone needs them.

    listen(seconds) keeps the receive socket open for at least that long. Every printer seen
    is remembered with the time it was last heard, and each callback gets its details.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.found = {}       # ip -> (details, last heard)
        self.callbacks = []
        self.until = 0.0
        self.thread = None
        self.error = None

    def listen(self, seconds):
        with self.lock:
            self.until = max(self.until, time.time() + seconds)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="bambu-discovery")
                self.thread.start()

    def stop(self):
        with self.lock:
            self.until = 0.0

    def printers(self, max_age=900):
        now = time.time()
        with self.lock:
            return [dict(info) for info, seen in self.found.values() if now - seen < max_age]

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Bambu Studio may listen too
            s.bind(("", BAMBU_PORT))
        except OSError:
            s.close()
            raise
        s.settimeout(1)
        return s

    def _run(self):
        try:
            s = self._open()
            self.error = None
        except OSError as e:
            self.error = "cannot listen for Bambu printers on UDP port %d (%s)" % (BAMBU_PORT, e)
            log.info(self.error)
            with self.lock:
                self.thread = None
            return
        try:
            while True:
                with self.lock:
                    if time.time() >= self.until:
                        self.thread = None
                        return
                try:
                    data, addr = s.recvfrom(MAX_PACKET)
                except socket.timeout:
                    continue
                except OSError:  # Windows reports unrelated ICMP errors on UDP sockets
                    time.sleep(0.2)
                    continue
                info = parse_bambu(data, addr[0])
                if not info:
                    continue
                with self.lock:
                    new = addr[0] not in self.found
                    self.found[addr[0]] = (info, time.time())
                    callbacks = list(self.callbacks)
                if new:
                    log.info("found Bambu %s %s at %s", info["model"], info["serial"], addr[0])
                for cb in callbacks:
                    try:
                        cb(info)
                    except Exception:
                        log.exception("Bambu discovery callback failed")
        finally:
            s.close()


# ---------------------------------------------------------------- Klipper

def local_networks(limit=4):
    """This PC's private IPv4 /24 networks, the one with the default route first."""
    addrs = []
    try:  # address of the interface that would carry traffic; nothing is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # documentation address: follows the default route
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        addrs += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    nets = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        if not any(ip in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
            continue  # not a home/office network (for example Tailscale's 100.x addresses)
        net = ipaddress.ip_network("%s/24" % a, strict=False)
        if net not in nets:
            nets.append(net)
    return nets[:limit]


def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()  # error responses hold a socket
        raise
    with r:
        return json.loads(r.read(500_000).decode("utf-8", "replace"))


def _port_open(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _orientation(cam):
    try:
        rotate = int(cam.get("rotation") or 0)
    except (TypeError, ValueError):
        rotate = 0
    return {"rotate": rotate if rotate in (90, 180, 270) else 0,
            "flip_horizontal": bool(cam.get("flip_horizontal")),
            "flip_vertical": bool(cam.get("flip_vertical"))}


def probe_klipper(host, timeout=0.5, ports=(80, 7125)):
    """Details of the Klipper printer at host, or None."""
    for port in ports:
        if not _port_open(host, port, timeout):
            continue
        base = "http://%s/" % host if port == 80 else "http://%s:%d/" % (host, port)
        try:
            info = _get_json(urllib.parse.urljoin(base, "/server/info"), 3)["result"]
            if "klippy_state" not in info:
                continue
        except Exception:
            continue
        found = {"type": "klipper", "host": host, "http_port": port, "name": "",
                 "state": _clean(info.get("klippy_state"), 16), "cameras": 0,
                 "rotate": 0, "flip_horizontal": False, "flip_vertical": False}
        for path, pick in (
                ("/server/database/item?namespace=mainsail&key=general", lambda v: v.get("printername")),
                ("/server/database/item?namespace=fluidd&key=uiSettings",
                 lambda v: (v.get("general") or {}).get("instanceName")),
                ("/printer/info", None)):
            try:
                result = _get_json(urllib.parse.urljoin(base, path), 3)["result"]
                name = pick(result.get("value") or {}) if pick else result.get("hostname")
            except Exception:
                continue
            if name:
                found["name"] = _clean(name)
                break
        try:
            cams = _get_json(urllib.parse.urljoin(base, "/server/webcams/list"), 3)["result"]["webcams"]
            cams = [c for c in cams if isinstance(c, dict) and c.get("enabled", True)]
            found["cameras"] = len(cams)
            if cams:
                found.update(_orientation(cams[0]))
        except Exception:
            pass
        found["name"] = found["name"] or host
        return found
    return None


def find_klipper(networks, workers=128):
    hosts = [str(h) for net in networks for h in net.hosts()]
    with concurrent.futures.ThreadPoolExecutor(min(workers, max(1, len(hosts)))) as pool:
        return [r for r in pool.map(probe_klipper, hosts) if r]


def _ip_key(item):
    try:
        return (item["type"], ipaddress.ip_address(item["host"]))
    except ValueError:
        return (item["type"], ipaddress.ip_address("0.0.0.0"))


def find_printers(listener, seconds=8):
    """Every printer found in about `seconds`: Klipper by asking, Bambu by listening."""
    started = time.time()
    listener.listen(seconds + 2)
    networks = local_networks()
    klipper = find_klipper(networks) if networks else []
    rest = seconds - (time.time() - started)
    if rest > 0:  # Bambu printers announce every few seconds: give them the full time
        time.sleep(rest)
    bambu = listener.printers()
    return {
        "printers": sorted(klipper, key=_ip_key) + sorted(bambu, key=_ip_key),
        "networks": [str(n) for n in networks],
        "bambu_error": listener.error,
    }
