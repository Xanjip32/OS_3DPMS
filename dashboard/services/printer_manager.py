"""Owns one adapter per configured printer: creation, start, stop and lookups.

The web layer, the monitor and the queue go through here instead of branching on the
printer type, so a new protocol only needs a new adapter class.
"""
import logging
import threading
from typing import Dict, List, Optional

from .. import adapters
from ..adapters.base import PrinterAdapter

log = logging.getLogger("dashboard.printers")


class PrinterManager:
    def __init__(self, pins=None):
        self.pins = pins
        self.lock = threading.RLock()
        self.adapters: Dict[str, PrinterAdapter] = {}

    def load(self, printers: List[dict]) -> None:
        """Replace all adapters with ones for this (validated) printer list."""
        with self.lock:
            self.stop_all()
            new = {}
            for p in printers:
                try:
                    a = adapters.create(p, self.pins)
                    a.connect()
                except Exception as e:  # one bad printer must not stop the others
                    log.error("printer %s: adapter failed to start: %s", p.get("id"), e)
                    continue
                new[p["id"]] = a
            self.adapters = new

    def stop_all(self) -> None:
        with self.lock:
            for a in self.adapters.values():
                try:
                    a.disconnect()
                except Exception:
                    log.exception("stopping adapter %s", a.p.get("id"))
            self.adapters = {}

    def get(self, pid: str) -> Optional[PrinterAdapter]:
        return self.adapters.get(pid)

    def infos(self) -> List[dict]:
        return [a.info() for a in list(self.adapters.values())]

    def locations(self) -> List[str]:
        return sorted({a.p.get("location") or "" for a in self.adapters.values()} - {""})

    def bambu_watchers(self) -> dict:
        return {pid: a.watcher for pid, a in self.adapters.items() if getattr(a, "watcher", None) is not None}
