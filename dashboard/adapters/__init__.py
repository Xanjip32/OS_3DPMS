"""Printer adapters: one class per protocol behind a common interface (see base.py)."""
from .bambu import BambuAdapter
from .base import (CAPABILITY_KEYS, AdapterError, NotAuthorized, NotSupported, PrinterAdapter,
                   PrinterBusy, empty_status)
from .marlin_serial import MarlinSerialAdapter
from .moonraker import MoonrakerAdapter
from .octoprint import OctoPrintAdapter
from .prusalink import PrusaLinkAdapter

# config.json "type" -> adapter class
REGISTRY = {
    "klipper": MoonrakerAdapter,
    "bambu": BambuAdapter,
    "octoprint": OctoPrintAdapter,
    "prusalink": PrusaLinkAdapter,
    "marlin_serial": MarlinSerialAdapter,
}


def create(printer, pins=None):
    """Adapter for a validated printer entry from config.json."""
    return REGISTRY[printer["type"]](printer, pins)


__all__ = ["create", "REGISTRY", "PrinterAdapter", "AdapterError", "NotSupported", "NotAuthorized",
           "PrinterBusy", "CAPABILITY_KEYS", "empty_status", "BambuAdapter", "MoonrakerAdapter",
           "OctoPrintAdapter", "PrusaLinkAdapter", "MarlinSerialAdapter"]
