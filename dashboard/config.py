"""Load, validate and save config.json. Secrets are encrypted on Windows."""
import copy
import json
import os
import re

from . import winsecret

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
SECRET_FIELDS = ("access_code", "api_key")
PRINTER_TYPES = ("klipper", "bambu")

DEFAULTS = {
    "port": 8080,
    "admin_port": 8079,
    "max_upload_mb": 1024,
    "allowed_users": [],
    "require_tailscale_identity": True,
    "printers": [],
    "notifications": {},
}

NOTIFY_EVENTS = ("started", "complete", "error", "paused", "cancelled", "offline")
NOTIFY_DEFAULT_ON = ("complete", "error", "paused", "offline")
NOTIFY_SECRETS = ("ntfy_url", "ntfy_token", "discord_webhook", "telegram_token")
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
        if p.get("type") not in PRINTER_TYPES:
            raise ConfigError("%s: type must be 'klipper' or 'bambu'" % pid)
        host = str(p.get("host", "")).strip()
        if not HOST_RE.match(host):
            raise ConfigError("%s: host must be an IP address or hostname" % pid)
        p["host"] = host
        p["name"] = str(p.get("name") or pid).strip()[:64]

        if p["type"] == "klipper":
            p["http_port"] = int(p.get("http_port", 80))
            if not 1 <= p["http_port"] <= 65535:
                raise ConfigError("%s: http_port out of range" % pid)
            if p.get("ui_port") not in (None, ""):
                p["ui_port"] = _port(p["ui_port"], "%s ui_port" % pid)
                if p["ui_port"] in used_ports:
                    raise ConfigError("%s: ui_port %d is already used" % (pid, p["ui_port"]))
                used_ports.add(p["ui_port"])
            else:
                p.pop("ui_port", None)
        else:
            p.pop("ui_port", None)
            if not (p.get("access_code") or p.get("access_code_protected")):
                raise ConfigError("%s: Bambu printers need the LAN access code" % pid)
            serial = str(p.get("serial", "")).strip()
            if serial and not re.match(r"^[A-Za-z0-9]{6,32}$", serial):
                raise ConfigError("%s: serial looks wrong" % pid)
            p["serial"] = serial
            p["use_ams"] = bool(p.get("use_ams", False))

        rotate = int(p.get("rotate", 0) or 0)
        if rotate not in (0, 90, 180, 270):
            raise ConfigError("%s: rotate must be 0, 90, 180 or 270" % pid)
        p["rotate"] = rotate
        p["flip_horizontal"] = bool(p.get("flip_horizontal", False))
        p["flip_vertical"] = bool(p.get("flip_vertical", False))
        printers.append(p)
    cfg["printers"] = printers
    cfg["notifications"] = _validate_notifications(cfg.get("notifications"))
    return cfg


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
    return n


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


def secret(printer, field):
    """Plain-text secret for printer, decrypted on demand."""
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
    cfg = validate(read_raw(path))
    if protect_secrets(cfg):
        save(path, cfg)
    return cfg


def write_default(path):
    save(path, {k: DEFAULTS[k] for k in ("port", "admin_port", "allowed_users", "printers")})
