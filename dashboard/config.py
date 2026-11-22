"""Load, validate and save config.json. Secrets are encrypted on Windows."""
import copy
import json
import os
import re
import urllib.parse

from . import bedcheck, winsecret

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
SECRET_FIELDS = ("access_code", "api_key", "password")
PRINTER_TYPES = ("klipper", "bambu", "octoprint", "prusalink", "marlin_serial")
TYPE_LABELS = {"klipper": "Klipper (Moonraker)", "bambu": "Bambu Lab (LAN)", "octoprint": "OctoPrint",
               "prusalink": "PrusaLink", "marlin_serial": "Marlin (USB serial)"}
# Printers that talk HTTP to a host and port of their own.
HTTP_TYPES = ("klipper", "octoprint", "prusalink")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
SERIAL_PORT_RE = re.compile(r"^(COM[1-9][0-9]{0,2}|/dev/[A-Za-z0-9_./\-]{1,64})$")
BAUD_RATES = (9600, 19200, 38400, 57600, 115200, 250000, 500000, 1000000)
BAMBU_MODES = ("read_only", "control")
# Remote control (start, pause, resume, cancel) and uploads, when the printer entry does not say.
# Bambu and direct serial start read-only: the brief asks for a non-invasive first connection.
CONTROL_DEFAULT = {"klipper": True, "octoprint": True, "prusalink": True, "bambu": False, "marlin_serial": False}
TEXT_FIELDS = ("location", "manufacturer", "model")

DEFAULTS = {
    "port": 8080,
    "admin_port": 8079,
    "max_upload_mb": 1024,
    "allowed_users": [],
    "require_tailscale_identity": True,
    "printers": [],
    "notifications": {},
    "read_only_users": [],          # Tailscale logins that may view but not upload or control
    "auth": {"mode": "tailscale"},  # see dashboard/auth.py
    "users": [],                    # dashboard accounts for password login (hashes only)
    "api_tokens": [],               # tokens for programs (Orca Slicer plugin): SHA-256 only
}

NOTIFY_EVENTS = ("started", "complete", "error", "paused", "cancelled", "offline", "reconnected", "warning",
                 "auto_started", "bed_not_clear")
NOTIFY_DEFAULT_ON = ("complete", "error", "paused", "offline", "reconnected", "warning", "auto_started",
                     "bed_not_clear")
NOTIFY_SECRETS = ("ntfy_url", "ntfy_token", "discord_webhook", "telegram_token", "smtp_password")
# E-mail is set in config.json only (see docs/NOTIFICATIONS.md); the Setup page keeps these as they are.
EMAIL_FIELDS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_from", "smtp_to")
SMTP_SECURITY = ("starttls", "ssl", "none")
EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NTFY_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(:\d+)?/[A-Za-z0-9_\-]{1,64}$")
DISCORD_RE = re.compile(r"^https://(discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)"
                        r"/api/webhooks/\d+/[A-Za-z0-9_\-]+$")
TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_\-]{30,}$")
TELEGRAM_CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{4,})$")


class ConfigError(ValueError):
    pass


def _port(value, what):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError("%s must be a number" % what) from None
    if not 1024 <= port <= 65535:
        raise ConfigError("%s must be between 1024 and 65535" % what)
    return port


def validate(raw):
    """Return a cleaned copy of raw, or raise ConfigError."""
    try:
        return _validate(raw)
    except (TypeError, ValueError) as e:
        if isinstance(e, ConfigError):
            raise
        raise ConfigError("invalid value: %s" % e) from None


def _validate(raw):
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(copy.deepcopy(raw))

    cfg["port"] = _port(cfg["port"], "port")
    cfg["admin_port"] = _port(cfg["admin_port"], "admin_port")
    if cfg["port"] == cfg["admin_port"]:
        raise ConfigError("port and admin_port must differ")
    try:
        cfg["max_upload_mb"] = max(1, int(cfg["max_upload_mb"]))
    except (TypeError, ValueError):
        raise ConfigError("max_upload_mb must be a number") from None
    cfg["require_tailscale_identity"] = bool(cfg["require_tailscale_identity"])

    users = cfg["allowed_users"]
    if not isinstance(users, list) or not all(isinstance(u, str) for u in users):
        raise ConfigError("allowed_users must be a list of Tailscale login names")
    cfg["allowed_users"] = sorted({u.strip().lower() for u in users if u.strip()})

    if not isinstance(cfg["printers"], list):
        raise ConfigError("printers must be a list")
    used_ports = {cfg["port"], cfg["admin_port"]}
    ids = set()
    printers = []
    for i, item in enumerate(cfg["printers"]):
        if not isinstance(item, dict):
            raise ConfigError("printer %d must be an object" % (i + 1))
        p = dict(item)
        pid = str(p.get("id", ""))
        if not ID_RE.match(pid):
            raise ConfigError("printer %d: id must be lowercase letters, digits or '-'" % (i + 1))
        if pid in ids:
            raise ConfigError("duplicate printer id '%s'" % pid)
        ids.add(pid)
        ptype = p.get("type")
        if ptype not in PRINTER_TYPES:
            raise ConfigError("%s: type must be one of %s" % (pid, ", ".join(PRINTER_TYPES)))
        if ptype == "marlin_serial" and not str(p.get("host", "")).strip():
            p["host"] = "localhost"  # USB printer on this PC: there is no network address
        host = str(p.get("host", "")).strip()
        if not HOST_RE.match(host):
            raise ConfigError("%s: host must be an IP address or hostname" % pid)
        p["host"] = host
        p["name"] = str(p.get("name") or pid).strip()[:64]
        for key in TEXT_FIELDS:
            value = str(p.get(key) or "").strip()[:64]
            if value:
                p[key] = value
            else:
                p.pop(key, None)
        for field in SECRET_FIELDS:
            env = str(p.get(field + "_env") or "").strip()
            if env:
                if not ENV_RE.match(env):
                    raise ConfigError("%s: %s_env must be an environment variable name" % (pid, field))
                p[field + "_env"] = env
            else:
                p.pop(field + "_env", None)

        webcam = str(p.get("webcam_url") or "").strip()
        if webcam:
            url = urllib.parse.urlsplit(urllib.parse.urljoin("http://%s/" % host, webcam))
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ConfigError("%s: camera URL must use HTTP or HTTPS" % pid)
            p["webcam_url"] = webcam
        else:
            p.pop("webcam_url", None)

        if ptype in HTTP_TYPES:
            p["http_port"] = int(p.get("http_port", 80))
            if not 1 <= p["http_port"] <= 65535:
                raise ConfigError("%s: http_port out of range" % pid)
        else:
            p.pop("http_port", None)
        if ptype == "klipper" and p.get("ui_port") not in (None, ""):
            p["ui_port"] = _port(p["ui_port"], "%s ui_port" % pid)
            if p["ui_port"] in used_ports:
                raise ConfigError("%s: ui_port %d is already used" % (pid, p["ui_port"]))
            used_ports.add(p["ui_port"])
        else:
            p.pop("ui_port", None)
        if ptype == "klipper":
            p["websocket"] = bool(p.get("websocket", True))
        if ptype == "octoprint" and not _has_secret(p, "api_key"):
            raise ConfigError("%s: OctoPrint needs an API key (OctoPrint settings > Application Keys)" % pid)
        if ptype == "prusalink":
            if not (_has_secret(p, "password") or _has_secret(p, "api_key")):
                raise ConfigError("%s: PrusaLink needs its password (printer screen: Settings > Network)" % pid)
            p["username"] = str(p.get("username") or "maker").strip()[:64]
        if ptype == "marlin_serial":
            port = str(p.get("serial_port") or "").strip()
            if not SERIAL_PORT_RE.match(port):
                raise ConfigError("%s: serial_port must look like COM3 or /dev/ttyUSB0" % pid)
            p["serial_port"] = port
            baud = int(p.get("baudrate", 115200))
            if baud not in BAUD_RATES:
                raise ConfigError("%s: baudrate must be one of %s" % (pid, ", ".join(map(str, BAUD_RATES))))
            p["baudrate"] = baud
        if ptype == "bambu":
            if not _has_secret(p, "access_code"):
                raise ConfigError("%s: Bambu printers need the LAN access code" % pid)
            serial = str(p.get("serial", "")).strip()
            if serial and not re.match(r"^[A-Za-z0-9]{6,32}$", serial):
                raise ConfigError("%s: serial looks wrong" % pid)
            p["serial"] = serial
            p["use_ams"] = bool(p.get("use_ams", False))
            mode = p.get("mode") or "read_only"
            if mode not in BAMBU_MODES:
                raise ConfigError("%s: mode must be read_only or control" % pid)
            p["mode"] = mode
            for key, default in (("mqtt_port", 8883), ("camera_port", 6000), ("ftps_port", 990)):
                p[key] = int(p.get(key) or default)
                if not 1 <= p[key] <= 65535:
                    raise ConfigError("%s: %s out of range" % (pid, key))
        for key in ("allow_remote_control", "allow_file_upload"):
            p[key] = bool(p.get(key, CONTROL_DEFAULT[ptype]))
        if ptype == "bambu" and p["mode"] == "read_only":
            p["allow_remote_control"] = p["allow_file_upload"] = False

        rotate = int(p.get("rotate", 0) or 0)
        if rotate not in (0, 90, 180, 270):
            raise ConfigError("%s: rotate must be 0, 90, 180 or 270" % pid)
        p["rotate"] = rotate
        p["flip_horizontal"] = bool(p.get("flip_horizontal", False))
        p["flip_vertical"] = bool(p.get("flip_vertical", False))
        bedcheck.validate_printer(p, pid)
        printers.append(p)
    cfg["printers"] = printers
    cfg["notifications"] = _validate_notifications(cfg.get("notifications"))
    _validate_auth(cfg)
    cfg.pop("cloud", None)  # the Cloudflare dashboard was removed in 1.5.0; its old settings are dropped
    return cfg


def _validate_auth(cfg):
    from . import auth
    ro = cfg.get("read_only_users") or []
    if not isinstance(ro, list) or not all(isinstance(u, str) for u in ro):
        raise ConfigError("read_only_users must be a list of login names")
    cfg["read_only_users"] = sorted({u.strip().lower() for u in ro if u.strip()})
    a = cfg.get("auth") if isinstance(cfg.get("auth"), dict) else {}
    mode = a.get("mode") or "tailscale"
    if mode not in auth.MODES:
        raise ConfigError("auth mode must be one of %s" % ", ".join(auth.MODES))
    hours = int(a.get("session_hours") or 12)
    if not 1 <= hours <= 720:
        raise ConfigError("session_hours must be between 1 and 720")
    cfg["auth"] = {"mode": mode, "session_hours": hours}
    users, names = [], set()
    for u in cfg.get("users") or []:
        if not isinstance(u, dict):
            raise ConfigError("users must be a list of objects")
        name = str(u.get("name") or "").strip().lower()
        if not auth.USER_RE.match(name) or name in names:
            raise ConfigError("user name '%s' is invalid or used twice" % name)
        names.add(name)
        if not str(u.get("password_hash") or "").startswith("pbkdf2_sha256$"):
            raise ConfigError("user %s: password_hash missing (use run.py --add-user)" % name)
        role = u.get("role") or "operator"
        if role not in auth.ROLES:
            raise ConfigError("user %s: role must be admin, operator or viewer" % name)
        users.append({"name": name, "password_hash": u["password_hash"], "role": role})
    cfg["users"] = users
    tokens, names = [], set()
    for t in cfg.get("api_tokens") or []:
        if not isinstance(t, dict):
            raise ConfigError("api_tokens must be a list of objects")
        name = str(t.get("name") or "").strip().lower()
        if not auth.TOKEN_NAME_RE.match(name) or name in names:
            raise ConfigError("API token name '%s' is invalid or used twice" % name)
        names.add(name)
        digest = str(t.get("token_sha256") or "").lower()
        if not re.match(r"^[0-9a-f]{64}$", digest):
            raise ConfigError("API token %s: token_sha256 missing (use run.py --add-token)" % name)
        role = t.get("role") or "operator"
        if role not in auth.TOKEN_ROLES:
            raise ConfigError("API token %s: role must be operator or viewer" % name)
        tokens.append({"name": name, "token_sha256": digest, "role": role,
                       "created": str(t.get("created") or "")[:32]})
    cfg["api_tokens"] = tokens
    if "password" in mode and not users:
        raise ConfigError("auth mode %s needs at least one user: run  py run.py --add-user NAME" % mode)


def _validate_notifications(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("notifications must be an object")
    n = dict(raw)
    events = n.get("events") if isinstance(n.get("events"), dict) else {}
    n["events"] = {e: bool(events.get(e, e in NOTIFY_DEFAULT_ON)) for e in NOTIFY_EVENTS}
    n["snapshot"] = bool(n.get("snapshot", False))
    # Plain-text values are new input and must be checked. Encrypted ones were checked when saved.
    checks = (("ntfy_url", NTFY_RE, "ntfy link must look like https://ntfy.sh/your-topic"),
              ("discord_webhook", DISCORD_RE, "Discord webhook must be a https://discord.com/api/webhooks/... link"),
              ("telegram_token", TELEGRAM_TOKEN_RE, "Telegram bot token looks wrong"))
    for key, pattern, msg in checks:
        value = str(n.get(key) or "").strip()
        if value:
            if not pattern.match(value):
                raise ConfigError(msg)
            n[key] = value
        else:
            n.pop(key, None)
    token = str(n.get("ntfy_token") or "").strip()
    if token and not re.match(r"^[A-Za-z0-9_\-]{8,128}$", token):
        raise ConfigError("ntfy access token looks wrong")
    chat = str(n.get("telegram_chat_id") or "").strip()
    if chat and not TELEGRAM_CHAT_RE.match(chat):
        raise ConfigError("Telegram chat id must be a number or @channel")
    n["telegram_chat_id"] = chat
    _validate_email(n)
    return n


def _validate_email(n):
    host = str(n.get("smtp_host") or "").strip()
    if not host:
        for key in EMAIL_FIELDS:
            n.pop(key, None)
        return
    if not HOST_RE.match(host):
        raise ConfigError("smtp_host must be a hostname or IP address")
    security = str(n.get("smtp_security") or "starttls").lower()
    if security not in SMTP_SECURITY:
        raise ConfigError("smtp_security must be starttls, ssl or none")
    if security == "none" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ConfigError("smtp_security none is only allowed for a mail relay on this PC (localhost)")
    try:
        port = int(n.get("smtp_port") or (465 if security == "ssl" else 587))
    except (TypeError, ValueError):
        raise ConfigError("smtp_port must be a number") from None
    if not 1 <= port <= 65535:
        raise ConfigError("smtp_port must be 1-65535")
    to = n.get("smtp_to")
    to = [to] if isinstance(to, str) else (to or [])
    to = [str(t).strip() for t in to if str(t).strip()]
    if not to or len(to) > 10 or not all(EMAIL_RE.match(t) for t in to):
        raise ConfigError("smtp_to must be 1-10 e-mail addresses")
    sender = str(n.get("smtp_from") or n.get("smtp_user") or "").strip()
    if not EMAIL_RE.match(sender):
        raise ConfigError("smtp_from must be an e-mail address")
    env = n.get("smtp_password_env")
    if env and not ENV_RE.match(str(env)):
        raise ConfigError("smtp_password_env must be an environment variable name")
    n.update(smtp_host=host, smtp_port=port, smtp_security=security, smtp_to=to, smtp_from=sender,
             smtp_user=str(n.get("smtp_user") or "").strip())


def protect_secrets(cfg):
    """Move plain-text secrets to their encrypted form. Return True if anything changed."""
    if not winsecret.available():
        return False
    changed = False
    targets = [(p, SECRET_FIELDS) for p in cfg["printers"]]
    targets.append((cfg.get("notifications") or {}, NOTIFY_SECRETS))
    for obj, fields in targets:
        for field in fields:
            value = obj.get(field)
            if value:
                obj[field + "_protected"] = winsecret.protect(str(value))
                del obj[field]
                changed = True
    return changed


def _has_secret(p, field):
    return bool(p.get(field) or p.get(field + "_protected") or p.get(field + "_env"))


def secret(printer, field):
    """Plain-text secret for printer, decrypted on demand.

    Order: plain value, DPAPI-encrypted value, then the environment variable named in
    `<field>_env` (for Linux/macOS or containers, where DPAPI is not available)."""
    env = printer.get(field + "_env")
    if env and not printer.get(field) and not printer.get(field + "_protected"):
        return os.environ.get(env, "")
    if printer.get(field):
        return str(printer[field])
    token = printer.get(field + "_protected")
    if token:
        return winsecret.unprotect(token)
    return ""


def read_raw(path):
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError as e:
            raise ConfigError("config.json is not valid JSON: %s" % e) from None


def save(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    if os.name != "nt":
        os.chmod(path, 0o600)


def load(path):
    raw = read_raw(path)
    cfg = validate(raw)
    if protect_secrets(cfg) or "cloud" in raw:  # also drops the old Cloudflare key from the file
        save(path, cfg)
    return cfg


def write_default(path):
    save(path, {k: DEFAULTS[k] for k in ("port", "admin_port", "allowed_users", "printers")})
