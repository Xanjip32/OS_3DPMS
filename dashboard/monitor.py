"""Watch every printer's state and turn changes into events (print finished, failed...)."""
import collections
import datetime
import logging
import threading

from . import klipper, tailscale

log = logging.getLogger("dashboard.monitor")

POLL_SECONDS = 5
OFFLINE_POLLS = 12          # 12 x 5 s = 1 minute without answer
BUSY = ("printing", "paused")

ICONS = {"started": "▶️", "complete": "✅", "error": "❌", "paused": "⏸️",
         "cancelled": "⏹️", "offline": "⚠️", "test": "🔔"}


def transition(old, new):
    """Event kind for a state change, or None."""
    if old is None or old == new:
        return None
    if new == "printing" and old not in BUSY:
        return "started"
    if new == "paused" and old == "printing":
        return "paused"
    if new == "complete" and old in BUSY:
        return "complete"
    if new == "error":
        return "error"
    if new in ("cancelled", "standby") and old in BUSY:
        return "cancelled"
    return None


def _duration(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    h, m = divmod(seconds // 60, 60)
    return " in %dh %02dm" % (h, m) if h else " in %dm" % m


def describe(kind, name, st):
    file = (st or {}).get("file") or "the print"
    pct = int(round(((st or {}).get("progress") or 0) * 100))
    msg = (st or {}).get("message") or ""
    text = {
        "started": "%s started %s" % (name, file),
        "complete": "%s finished %s%s" % (name, file, _duration((st or {}).get("duration"))),
        "error": "%s stopped with an error%s" % (name, (": " + msg) if msg else ""),
        "paused": "%s paused at %d%%%s" % (name, pct, (": " + msg) if msg else ""),
        "cancelled": "%s: print cancelled (%s)" % (name, file),
        "offline": "%s is not answering while it was printing" % name,
        "test": "Test notification from OS_3DPMS",
    }[kind]
    return "%s %s" % (ICONS[kind], text)


class Monitor:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.prev = {}
        self.status = {}
        self.failures = collections.Counter()
        self.events = collections.deque(maxlen=200)
        self.next_id = 1
        self.public_url = ""
        self.stopped = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="monitor").start()
        threading.Thread(target=self._url_loop, daemon=True, name="public-url").start()

    def stop(self):
        self.stopped.set()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("monitor poll failed")
            self.stopped.wait(POLL_SECONDS)

    def _url_loop(self):
        while not self.stopped.is_set():
            try:
                dns = tailscale.status().get("dns_name", "")
                self.public_url = "https://%s/" % dns if dns else ""
            except Exception:
                pass
            self.stopped.wait(600)

    def fetch(self, p):
        if p["type"] == "klipper":
            try:
                return klipper.status(p)
            except Exception:
                return None
        watcher = self.app.bambu_watchers.get(p["id"])
        if watcher is None:
            return {"state": None}  # no serial number: camera only
        return watcher.status()

    def poll_once(self):
        printers = list(self.app.printers.values())
        results = [(p, self.fetch(p)) for p in printers]
        for p, st in results:
            self.update(p, st)

    def update(self, p, st):
        pid = p["id"]
        with self.lock:
            if st is None:
                self.failures[pid] += 1
                self.status[pid] = {"state": "offline"}
                if self.failures[pid] == OFFLINE_POLLS and self.prev.get(pid) in BUSY:
                    self._emit("offline", p, {})
                return
            self.failures[pid] = 0
            self.status[pid] = st
            new = st.get("state")
            if new is None:
                return
            kind = transition(self.prev.get(pid), new)
            self.prev[pid] = new
            if kind:
                self._emit(kind, p, st)

    def _emit(self, kind, p, st, notify=True):
        event = {
            "id": self.next_id,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "printer": p["id"],
            "name": p["name"],
            "kind": kind,
            "text": describe(kind, p["name"], st),
        }
        self.next_id += 1
        self.events.append(event)
        log.info("event: %s", event["text"])
        if notify:
            self.app.notifier.send(event)
        return event

    def test_event(self):
        """Record a test event for the browser; the caller delivers it to phones."""
        with self.lock:
            return self._emit("test", {"id": "", "name": "OS_3DPMS"}, {}, notify=False)

    def events_after(self, after):
        with self.lock:
            return [e for e in self.events if e["id"] > after], self.next_id - 1
