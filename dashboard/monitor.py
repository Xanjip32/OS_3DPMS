"""Watch every printer's state and turn changes into events (print finished, failed...).

Printers are polled concurrently through their adapters, each with its own timeout, so one
offline printer never delays the others. A printer that stays offline is asked less often
(exponential backoff up to MAX_BACKOFF_S). Adapters with a live connection (Moonraker
WebSocket, Bambu MQTT, serial auto-report) answer from their cache without network traffic.
"""
import collections
import concurrent.futures
import datetime
import logging
import threading
import time

from . import klipper, tailscale

log = logging.getLogger("dashboard.monitor")

POLL_SECONDS = 5
OFFLINE_POLLS = 12          # 12 x 5 s = 1 minute without answer
MAX_BACKOFF_S = 60          # an offline printer is still asked at least once a minute
FETCH_WAIT_S = 4.0          # poll_once waits this long for answers; slow ones land when they arrive
TEMP_SAMPLES = 360          # temperature history per printer (30 minutes at 5 s)
BUSY = ("printing", "paused")

ICONS = {"started": "▶️", "complete": "✅", "error": "❌", "paused": "⏸️",
         "cancelled": "⏹️", "offline": "⚠️", "test": "🔔", "reconnected": "🔌", "warning": "⚠️"}


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
        "reconnected": "%s is answering again" % name,
        "warning": "%s: %s" % (name, msg or "warning"),
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
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="poll")
        self.inflight = {}           # printer id -> future of a fetch still running
        self.next_poll = {}          # printer id -> earliest time for the next fetch (backoff)
        self.last_ok = {}            # printer id -> time of the last answer
        self.offline_reported = set()
        self.alerts = {}             # printer id -> warnings/errors already announced
        self.temps = collections.defaultdict(lambda: collections.deque(maxlen=TEMP_SAMPLES))

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="monitor").start()
        threading.Thread(target=self._url_loop, daemon=True, name="public-url").start()

    def stop(self):
        self.stopped.set()
        self.pool.shutdown(wait=False)

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
        adapter = self.app.adapter(p["id"]) if hasattr(self.app, "adapter") else None
        if adapter is not None:
            return adapter.get_status()
        if p["type"] == "klipper":
            try:
                return klipper.status(p)
            except Exception:
                return None
        watcher = self.app.bambu_watchers.get(p["id"])
        if watcher is None:
            return {"state": None}  # no serial number: camera only
        return watcher.status()

    def poll_once(self, wait=FETCH_WAIT_S):
        """Ask every printer that is due, all at once; apply each answer as it arrives."""
        now = time.time()
        started = []
        for p in list(self.app.printers.values()):
            pid = p["id"]
            running = self.inflight.get(pid)
            if running is not None and not running.done():
                continue  # still waiting for a slow printer: never pile up requests
            if now < self.next_poll.get(pid, 0):
                continue  # offline for a while: backing off
            try:
                future = self.pool.submit(self.fetch, p)
            except RuntimeError:  # pool shut down
                return
            self.inflight[pid] = future
            future.add_done_callback(lambda f, p=p: self._apply(p, f))
            started.append(future)
        if started:
            concurrent.futures.wait(started, timeout=wait)

    def _apply(self, p, future):
        try:
            st = future.result()
        except Exception as e:
            log.debug("status of %s failed: %s", p["id"], e)
            st = None
        if self.app.printers.get(p["id"]) is not p:
            return  # printer removed or edited meanwhile
        self.update(p, st)

    def health(self, pid):
        """Connection health for the detail page."""
        with self.lock:
            last = self.last_ok.get(pid)
            return {"online": self.failures[pid] == 0 and last is not None,
                    "last_seen": int(last) if last else None, "failed_polls": self.failures[pid],
                    "next_poll_in": max(0, int(self.next_poll.get(pid, 0) - time.time()))}

    def temperature_history(self, pid):
        with self.lock:
            return list(self.temps.get(pid, []))

    def update(self, p, st):
        pid = p["id"]
        with self.lock:
            if st is None:
                self.failures[pid] += 1
                self.status[pid] = {"state": "offline"}
                if self.failures[pid] == OFFLINE_POLLS and self.prev.get(pid) in BUSY:
                    self._emit("offline", p, {})
                    self.offline_reported.add(pid)
                extra = self.failures[pid] - OFFLINE_POLLS
                if extra >= 0:
                    self.next_poll[pid] = time.time() + min(MAX_BACKOFF_S, POLL_SECONDS * 2 ** min(extra, 6))
                return
            self.failures[pid] = 0
            self.next_poll.pop(pid, None)
            self.last_ok[pid] = time.time()
            if pid in self.offline_reported:
                self.offline_reported.discard(pid)
                self._emit("reconnected", p, st)
            self.status[pid] = st
            temps = st.get("temperatures") or {}
            if temps:
                self.temps[pid].append([int(time.time())] + [
                    temps.get(k, {}).get("actual") for k in ("nozzle", "bed")])
            self._alerts(p, st)
            new = st.get("state")
            if new is None:
                return
            kind = transition(self.prev.get(pid), new)
            self.prev[pid] = new
            if kind:
                self._emit(kind, p, st)

    def _alerts(self, p, st):
        """Announce new warnings (filament, AMS) and printer errors (HMS, Klipper) once each."""
        current = set(st.get("warnings") or [])
        if st.get("state") != "error":  # an error state has its own event
            current |= set(st.get("errors") or [])
        known = self.alerts.get(p["id"])
        self.alerts[p["id"]] = current
        if known is None:
            return  # first answer after start: do not announce what was already there
        for text in sorted(current - known)[:3]:
            self._emit("warning", p, {"message": text})

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
        history = getattr(self.app, "history", None)
        if history is not None and p.get("id"):
            try:
                history.on_event(kind, p["id"], (st or {}).get("file") or "", (st or {}).get("message") or "",
                                 started_elsewhere=True)
            except Exception:
                log.exception("job history update failed")
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
