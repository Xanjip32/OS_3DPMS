# /// script
# requires-python = ">=3.8"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "OS_3DPMS printer agent"
# description = "Send jobs from Orca Slicer to an OS_3DPMS print farm dashboard: queue them (with the optional bed check and auto-start) or print now, and see live printer status. Needs Orca Slicer with plugin support (nightly) and Preferences > Developer > Use printer agents instead of print hosts."
# author = "OS_3DPMS contributors"
# version = "0.1.0"
# ///
"""OS_3DPMS printer agent for Orca Slicer (beta, untested against a real Orca Slicer build).

Orca Slicer talks to printers through "printer agents". This plugin adds two agents that talk
to an OS_3DPMS dashboard over its HTTP API instead of to a printer:

* "OS_3DPMS (queue)"     agent id ``os3dpms_queue``: Print adds the file to the OS_3DPMS queue
                          of the chosen printer. It starts with the Queue page button, or by
                          itself when that printer has the bed check with auto-start turned on.
* "OS_3DPMS (print now)" agent id ``os3dpms_direct``: Print uploads the file and starts it now
                          (OS_3DPMS refuses when the printer is busy).

Send (upload without printing) uploads the file to the printer with both agents.

Set up the printer preset in Orca Slicer (see docs/ORCA_PLUGIN.md in the OS_3DPMS repository):

* Printer settings > Basic information > Advanced > Printer Agent: one of the agents above.
* Print host: the OS_3DPMS address, for example ``http://127.0.0.1:8079`` on the printer PC itself,
  or ``https://<printer-pc>.<tailnet>.ts.net`` from another computer on your Tailscale network.
  Add ``/<printer-id>`` to pin one OS_3DPMS printer, e.g. ``http://127.0.0.1:8079/voron``.
* API key (printhost_apikey): an OS_3DPMS API token (Setup page > Orca Slicer plugin, or
  ``py run.py --add-token orca``).

Only the Python standard library is used. Network access goes through Orca's plugin audit, so
Orca asks once before the first connection to the OS_3DPMS address.
"""
import http.client
import json
import os
import re
import ssl
import threading
import urllib.parse

import orca

pa = orca.printer_agent

PLUGIN_VERSION = "0.1.0"
POLL_S = 3.0                    # status refresh while connected
TIMEOUT_S = 15
CHUNK = 256 * 1024
DEFAULT_ACCESS_CODE = "88888888"  # what Orca passes when the preset has no API key
DEVICE_PREFIX = "os3dpms:"

OK = 0
FAILED = -1
NOT_SUPPORTED = -7010

# Job progress stages for update_fn, as in Orca's/Bambu's print job dialog (best effort).
STAGE_UPLOAD = 1
STAGE_FINISHED = 6
STAGE_ERROR = 7

# OS_3DPMS state -> Bambu-style gcode_state that Orca's device view understands.
GCODE_STATE = {"standby": "IDLE", "printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH",
               "cancelled": "IDLE", "error": "FAILED", "offline": "IDLE"}


class AgentError(Exception):
    """A problem to report to the user (Orca shows the message), never raised to Orca itself."""


# ---------------------------------------------------------------- OS_3DPMS HTTP API

def parse_print_host(host, port="", use_ssl=False):
    """(base URL, pinned printer id) from the preset's print host.

    "127.0.0.1:8079" -> ("http://127.0.0.1:8079", ""); "https://pc.ts.net/voron" -> ("https://pc.ts.net", "voron").
    Orca strips http:// and https:// itself and passes use_ssl; both forms are accepted.
    """
    host = (host or "").strip()
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            use_ssl = prefix == "https://"
            host = host[len(prefix):]
    host, _, path = host.partition("/")
    pinned = path.strip("/").split("/")[0] if path.strip("/") else ""
    if not host:
        raise AgentError("set the OS_3DPMS address as the printer's print host")
    if port and ":" not in host:
        host = "%s:%s" % (host, port)
    if not use_ssl and host.lower().split(":")[0].endswith(".ts.net"):
        use_ssl = True  # tailscale serve publishes OS_3DPMS over HTTPS only
    return ("https" if use_ssl else "http") + "://" + host, urllib.parse.unquote(pinned)


def safe_name(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\-() ]+", "_", name).lstrip(". ")
    return name[:200] or "orca_job"


class Client:
    """Small OS_3DPMS API client (standard library only)."""

    def __init__(self, base, token, ca_file="", timeout=TIMEOUT_S):
        self.base = base.rstrip("/")
        self.token = "" if token in ("", None, DEFAULT_ACCESS_CODE) else str(token).strip()
        self.ca_file = ca_file or ""
        self.timeout = timeout
        parts = urllib.parse.urlsplit(self.base)
        self.https = parts.scheme == "https"
        self.netloc = parts.netloc

    def _conn(self, timeout=None):
        timeout = timeout or self.timeout
        if self.https:
            ctx = ssl.create_default_context(cafile=self.ca_file or None)
            return http.client.HTTPSConnection(self.netloc, timeout=timeout, context=ctx)
        return http.client.HTTPConnection(self.netloc, timeout=timeout)

    def _headers(self, extra=None):
        h = {"X-Dashboard": "1", "User-Agent": "OS_3DPMS-Orca-agent/%s" % PLUGIN_VERSION,
             "Accept": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        h.update(extra or {})
        return h

    @staticmethod
    def _answer(resp):
        raw = resp.read()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            data = {"error": raw[:200].decode("utf-8", "replace")}
        if resp.status >= 400:
            msg = data.get("error") if isinstance(data, dict) else ""
            if resp.status == 401:
                msg = msg or "OS_3DPMS refused the API token"
            raise AgentError("OS_3DPMS answered %d: %s" % (resp.status, msg or resp.reason))
        return data

    def request(self, method, path, body=None, timeout=None):
        conn = self._conn(timeout)
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            headers = self._headers({"Content-Type": "application/json"} if data is not None else None)
            conn.request(method, path, body=data, headers=headers)
            return self._answer(conn.getresponse())
        except (OSError, http.client.HTTPException) as e:
            raise AgentError("cannot reach OS_3DPMS at %s: %s" % (self.base, e)) from None
        finally:
            conn.close()

    def me(self):
        return self.request("GET", "/api/me", timeout=5)

    def printers(self):
        return self.request("GET", "/api/plugin/printers").get("printers") or []

    def control(self, printer, action):
        body = {"confirm": printer} if action in ("cancel", "start") else {}
        return self.request("POST", "/api/printers/%s/%s" % (urllib.parse.quote(printer), action), body)

    def upload(self, printer, path, name, action, progress=None, cancelled=None):
        """Send a file: action queue (add to the OS_3DPMS queue), send (upload) or print (start now)."""
        size = os.path.getsize(path)
        query = urllib.parse.urlencode({"printer": printer, "name": name, "action": action, "overwrite": "1"})
        conn = self._conn(max(self.timeout, 120))
        try:
            conn.putrequest("POST", "/api/plugin/jobs?" + query)
            for k, v in self._headers({"Content-Type": "application/octet-stream",
                                       "Content-Length": str(size)}).items():
                conn.putheader(k, v)
            conn.endheaders()
            sent = 0
            with open(path, "rb") as f:
                while True:
                    if cancelled is not None and cancelled():
                        raise AgentError("cancelled")
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    if progress is not None:
                        progress(int(sent * 100 / max(1, size)))
            return self._answer(conn.getresponse())
        except (OSError, http.client.HTTPException) as e:
            raise AgentError("upload to OS_3DPMS failed: %s" % e) from None
        finally:
            conn.close()


def bambu_status(printer):
    """OS_3DPMS printer entry -> Bambu-style push_status JSON (Orca's device view reads this format)."""
    st = printer.get("status") or {}
    temps = st.get("temperatures") or {}
    nozzle, bed = temps.get("nozzle") or {}, temps.get("bed") or {}
    remaining = st.get("remaining_s")
    out = {
        "command": "push_status", "msg": 0, "sequence_id": "0",
        "gcode_state": GCODE_STATE.get(st.get("state") or "offline", "IDLE"),
        "mc_percent": int(round((st.get("progress") or 0) * 100)),
        "mc_remaining_time": int(round(remaining / 60.0)) if remaining else 0,
        "subtask_name": st.get("file") or "", "gcode_file": st.get("file") or "",
        "print_error": 0,
    }
    for key, value in (("nozzle_temper", nozzle.get("actual")), ("nozzle_target_temper", nozzle.get("target")),
                       ("bed_temper", bed.get("actual")), ("bed_target_temper", bed.get("target"))):
        if isinstance(value, (int, float)):
            out[key] = float(value)
    layer = st.get("layer") or {}
    if layer.get("current") is not None:
        out["layer_num"] = layer.get("current")
        out["total_layer_num"] = layer.get("total") or 0
    return json.dumps({"print": out})


# ---------------------------------------------------------------- the agents

class OS3DPMSAgentBase(pa.PrinterAgentBase):
    """Shared behavior. Subclasses set AGENT_ID, AGENT_NAME and PRINT_ACTION."""

    AGENT_ID = ""
    AGENT_NAME = ""
    PRINT_ACTION = "queue"

    def __init__(self):
        super().__init__()
        self.callbacks = {}
        self.selected = ""
        self.client = None
        self.pinned = ""
        self.devices = {}          # printer id -> last OS_3DPMS printer entry
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    # ------------------------------------------------------------ identity

    def get_name(self):
        return self.AGENT_NAME

    def get_agent_info(self):
        return pa.AgentInfo(self.AGENT_ID, self.AGENT_NAME, PLUGIN_VERSION,
                            "Send jobs to an OS_3DPMS dashboard (%s)" % self.PRINT_ACTION)

    # ------------------------------------------------------------ host callbacks (any may be None)

    def _store(self, key, fn):
        self.callbacks[key] = fn
        return OK

    def set_on_ssdp_msg_fn(self, fn): return self._store("ssdp", fn)
    def set_on_local_connect_fn(self, fn): return self._store("local_connect", fn)
    def set_on_local_message_fn(self, fn): return self._store("local_message", fn)
    def set_on_message_fn(self, fn): return self._store("message", fn)
    def set_on_user_message_fn(self, fn): return self._store("user_message", fn)
    def set_on_printer_connected_fn(self, fn): return self._store("printer_connected", fn)
    def set_on_subscribe_failure_fn(self, fn): return self._store("subscribe_failure", fn)
    def set_server_callback(self, fn): return self._store("server_error", fn)
    def set_queue_on_main_fn(self, fn): return self._store("queue_on_main", fn)

    def _emit(self, key, *args):
        fn = self.callbacks.get(key)
        if fn is not None:
            try:
                fn(*args)
            except Exception:
                pass

    # ------------------------------------------------------------ devices

    @staticmethod
    def device_id(pid):
        return DEVICE_PREFIX + pid

    def _announce(self, printers):
        host = urllib.parse.urlsplit(self.client.base).netloc if self.client else ""
        for p in printers:
            self._emit("ssdp", json.dumps({
                "dev_id": self.device_id(p["id"]), "dev_name": "%s (OS_3DPMS)" % p.get("name", p["id"]),
                "dev_ip": host, "dev_type": p.get("model") or p.get("protocol") or "",
                "dev_signal": "", "connect_type": "lan", "bind_state": "free",
                "connection_name": "OS_3DPMS"}))

    def _refresh(self):
        printers = self.client.printers()
        with self.lock:
            self.devices = {p["id"]: p for p in printers}
        return printers

    def resolve(self, *candidates):
        """OS_3DPMS printer id for a job: the device Orca names, else the pinned one, the selected
        one, or the only printer. Raises AgentError when it cannot tell."""
        with self.lock:
            known = dict(self.devices)
        for cand in candidates + (self.pinned, self.selected):
            cand = (cand or "").strip()
            if cand.startswith(DEVICE_PREFIX):
                cand = cand[len(DEVICE_PREFIX):]
            if cand and (cand in known or (not known and cand == self.pinned)):
                return cand
        if len(known) == 1:
            return next(iter(known))
        raise AgentError("choose an OS_3DPMS printer in the Device tab, or add /<printer-id> to the print host"
                         " (OS_3DPMS printers: %s)" % (", ".join(sorted(known)) or "none"))

    def start_discovery(self, start, sending):
        if not start:
            return True
        if self.client is not None:
            try:
                self._announce(self._refresh())
            except AgentError:
                pass
        return True

    def get_user_selected_machine(self):
        return self.selected

    def set_user_selected_machine(self, dev_id):
        self.selected = dev_id or ""
        return OK

    # ------------------------------------------------------------ connection

    def connect_printer(self, params):
        """Called on Orca's UI thread: one quick check (so Orca's network prompt appears here), then
        a background thread keeps the status current."""
        dev_id = getattr(params, "dev_id", "") or ""
        try:
            base, pinned = parse_print_host(getattr(params, "host", ""), getattr(params, "port", ""),
                                            bool(getattr(params, "use_ssl", False)))
            client = Client(base, getattr(params, "password", ""), getattr(params, "ca_file", ""))
            client.me()
        except AgentError as e:
            self._emit("local_connect", 1, dev_id, str(e))
            return FAILED
        self.disconnect_printer()
        with self.lock:
            self.client, self.pinned = client, pinned
        try:
            self._announce(self._refresh())
        except AgentError:
            pass
        self._emit("local_connect", 0, dev_id, "")
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, args=(self.stop,), daemon=True, name="os3dpms-agent")
        self.thread.start()
        return OK

    def disconnect_printer(self):
        self.stop.set()
        return OK

    def _poll(self, stop):
        lost = False
        while not stop.wait(POLL_S):
            try:
                printers = self._refresh()
            except AgentError as e:
                if not lost:
                    self._emit("local_connect", 2, self.selected, str(e))
                lost = True
                continue
            if lost:
                self._emit("local_connect", 0, self.selected, "")
                lost = False
            for p in printers:
                self._emit("local_message", self.device_id(p["id"]), bambu_status(p))

    def on_unload(self):
        self.stop.set()

    # ------------------------------------------------------------ commands from Orca's device view

    def send_message_to_printer(self, dev_id, json_str, qos, flag):
        try:
            cmd = (json.loads(json_str or "{}").get("print") or {}).get("command")
        except (ValueError, AttributeError):
            return FAILED
        action = {"pause": "pause", "resume": "resume", "stop": "cancel"}.get(cmd)
        if action is None or self.client is None:
            return NOT_SUPPORTED
        try:
            self.client.control(self.resolve(dev_id), action)
            return OK
        except AgentError:
            return FAILED

    def send_message(self, dev_id, json_str, qos, flag):
        return self.send_message_to_printer(dev_id, json_str, qos, flag)

    # ------------------------------------------------------------ jobs (Orca job threads)

    def _client_for(self, params):
        if self.client is not None:
            return self.client
        host = getattr(params, "dev_ip", "") or ""
        base, pinned = parse_print_host(host, "", False)
        if pinned and not self.pinned:
            self.pinned = pinned
        return Client(base, getattr(params, "password", ""))

    def _job(self, params, update_fn, cancel_fn, action):
        def report(stage, code, msg):
            if update_fn is not None:
                try:
                    update_fn(stage, code, msg)
                except Exception:
                    pass
        try:
            client = self._client_for(params)
            if getattr(params, "project_name", "") == "verify_job":
                client.me()          # Orca's connection check: no file is sent
                return OK
            if not self.devices:
                self.client = self.client or client
                self._refresh()
            pid = self.resolve(getattr(params, "dev_id", ""))
            src = getattr(params, "filename", "")
            if not src or not os.path.isfile(src):
                raise AgentError("Orca did not pass a file to send")
            ext = os.path.splitext(src)[1].lower()
            if src.lower().endswith(".gcode.3mf"):
                ext = ".3mf"
            title = getattr(params, "task_name", "") or getattr(params, "project_name", "") or \
                os.path.splitext(os.path.basename(src))[0]
            title = re.sub(r"(\.gcode)?(\.3mf|\.gcode)$", "", title, flags=re.I)
            name = safe_name(title + ext)
            report(STAGE_UPLOAD, 0, "Sending %s to OS_3DPMS" % name)
            reply = client.upload(pid, src, name, action,
                                  progress=lambda pct: report(STAGE_UPLOAD, pct, "Sending %s" % name),
                                  cancelled=cancel_fn)
            report(STAGE_FINISHED, 0, reply.get("message") or "Sent to OS_3DPMS")
            return OK
        except AgentError as e:
            report(STAGE_ERROR, FAILED, str(e))
            return FAILED

    def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
        return self._job(params, update_fn, cancel_fn, "send")

    def start_local_print(self, params, update_fn, cancel_fn):
        return self._job(params, update_fn, cancel_fn, self.PRINT_ACTION)

    def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
        return self._job(params, update_fn, cancel_fn, self.PRINT_ACTION)

    def start_print(self, params, update_fn, cancel_fn, wait_fn):
        return self._job(params, update_fn, cancel_fn, self.PRINT_ACTION)

    def start_sdcard_print(self, params, update_fn, cancel_fn):
        return NOT_SUPPORTED

    # ------------------------------------------------------------ binding: nothing to do for OS_3DPMS

    def bind_detect(self, dev_ip, sec_link, detect):
        detect.dev_id = self.device_id(self.pinned) if self.pinned else (dev_ip or "")
        detect.dev_name = "OS_3DPMS"
        detect.connect_type = "lan"
        detect.bind_state = "free"
        return OK

    def check_cert(self): return OK
    def ping_bind(self, ping_code): return OK
    def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn): return OK
    def unbind(self, dev_id): return OK
    def request_bind_ticket(self): return (OK, "")


class OS3DPMSQueueAgent(OS3DPMSAgentBase):
    AGENT_ID = "os3dpms_queue"
    AGENT_NAME = "OS_3DPMS (queue)"
    PRINT_ACTION = "queue"


class OS3DPMSDirectAgent(OS3DPMSAgentBase):
    AGENT_ID = "os3dpms_direct"
    AGENT_NAME = "OS_3DPMS (print now)"
    PRINT_ACTION = "print"


@orca.plugin
class OS3DPMSPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(OS3DPMSQueueAgent)
        orca.register_capability(OS3DPMSDirectAgent)
