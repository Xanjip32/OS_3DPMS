"""Print job history: what was started where, by whom, and how it ended.

The per-printer queue itself stays in web.App (queue.json, unchanged format). This keeps
the record of started jobs in history.json next to config.json, and closes each job when
the monitor reports complete / error / cancelled for that printer.
"""
import datetime
import json
import logging
import os
import threading
import uuid

log = logging.getLogger("dashboard.jobs")
MAX_JOBS = 500
END_KINDS = {"complete": "completed", "error": "failed", "cancelled": "cancelled"}


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class JobHistory:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.jobs = self._load()

    def _load(self):
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                jobs = json.load(f).get("jobs") or []
            return [j for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), str)][-MAX_JOBS:]
        except (OSError, ValueError, AttributeError) as e:
            log.warning("job history not readable (%s); starting a new one", e)
            return []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"jobs": self.jobs[-MAX_JOBS:]}, f, indent=1)
        os.replace(tmp, self.path)

    def started(self, printer, file, user, source="upload"):
        """Record a print that was just started from OS_3DPMS. Returns its id."""
        with self.lock:
            for j in self.jobs:  # a new start on the same printer ends any job still marked running
                if j["printer"] == printer and j["status"] == "printing":
                    j.update(status="unknown", ended=_now(), error="superseded by a new print")
            job = {"id": uuid.uuid4().hex[:12], "printer": printer, "file": file, "user": user,
                   "source": source, "status": "printing", "started": _now(), "ended": None, "error": ""}
            self.jobs.append(job)
            self.jobs = self.jobs[-MAX_JOBS:]
            self._save()
            return job["id"]

    def on_event(self, kind, printer, file="", message="", started_elsewhere=False):
        """Close (or, for prints started on the printer itself, open) a job from a monitor event."""
        with self.lock:
            if kind == "started" and started_elsewhere:
                if any(j["printer"] == printer and j["status"] == "printing" for j in self.jobs):
                    return
                self.jobs.append({"id": uuid.uuid4().hex[:12], "printer": printer, "file": file, "user": "",
                                  "source": "printer", "status": "printing", "started": _now(), "ended": None,
                                  "error": ""})
                self._save()
                return
            status = END_KINDS.get(kind)
            if not status:
                return
            for j in reversed(self.jobs):
                if j["printer"] == printer and j["status"] == "printing":
                    j.update(status=status, ended=_now(), error=message if status == "failed" else "")
                    self._save()
                    return

    def list(self, printer=None, limit=100):
        with self.lock:
            jobs = [dict(j) for j in self.jobs if printer is None or j["printer"] == printer]
        return list(reversed(jobs))[:limit]

    def running(self, printer):
        with self.lock:
            return any(j["printer"] == printer and j["status"] == "printing" for j in self.jobs)
