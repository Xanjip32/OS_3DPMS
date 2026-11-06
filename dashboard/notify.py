"""Send events to phones: ntfy, Discord webhook, Telegram bot.

Only links that pass config validation are used (https, known Discord host),
so the dashboard cannot be turned into a tool for calling arbitrary URLs.
"""
import base64
import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
import uuid

from . import __version__, config

log = logging.getLogger("dashboard.notify")

USER_AGENT = "OS_3DPMS/%s (+https://github.com/Xanjip32/OS_3DPMS)" % __version__
MAX_PER_MINUTE = 20
TELEGRAM_API = "https://api.telegram.org"


def _request(url, data, headers, method="POST", timeout=15):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _multipart(fields, file_field, filename, content, ctype="image/jpeg"):
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n' % (boundary, k)).encode()
        out += v.encode("utf-8") + b"\r\n"
    out += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
            % (boundary, file_field, filename, ctype)).encode()
    out += content + ("\r\n--%s--\r\n" % boundary).encode()
    return bytes(out), "multipart/form-data; boundary=" + boundary


def _header_text(text):
    """RFC 2047 so emoji and accents survive in HTTP headers."""
    return "=?UTF-8?B?%s?=" % base64.b64encode(text.encode("utf-8")).decode("ascii")


def send_ntfy(url, token, title, message, click="", image=None, tags=""):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if image:
        headers.update({"Title": _header_text(title), "Message": _header_text(message),
                        "Filename": "camera.jpg"})
        if click:
            headers["Click"] = click
        if tags:
            headers["Tags"] = tags
        return _request(url, image, headers, method="PUT")
    parts = urllib.parse.urlsplit(url)
    body = {"topic": parts.path.strip("/"), "title": title, "message": message}
    if click:
        body["click"] = click
    if tags:
        body["tags"] = tags.split(",")
    headers["Content-Type"] = "application/json"
    return _request("%s://%s/" % (parts.scheme, parts.netloc), json.dumps(body).encode(), headers)


def send_discord(webhook, text, image=None):
    payload = {"content": text, "allowed_mentions": {"parse": []}}
    if image:
        data, ctype = _multipart({"payload_json": json.dumps(payload)}, "files[0]", "camera.jpg", image)
        return _request(webhook, data, {"Content-Type": ctype})
    return _request(webhook, json.dumps(payload).encode(), {"Content-Type": "application/json"})


def send_telegram(token, chat_id, text, image=None, api=TELEGRAM_API):
    if image:
        data, ctype = _multipart({"chat_id": chat_id, "caption": text}, "photo", "camera.jpg", image)
        return _request("%s/bot%s/sendPhoto" % (api, token), data, {"Content-Type": ctype})
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    return _request("%s/bot%s/sendMessage" % (api, token), body, {"Content-Type": "application/json"})


TAGS = {"complete": "white_check_mark", "error": "x", "paused": "pause_button",
        "cancelled": "stop_button", "offline": "warning", "started": "arrow_forward", "test": "bell"}


class Notifier:
    def __init__(self, app):
        self.app = app
        self.queue = queue.Queue(maxsize=50)
        self.sent = []
        threading.Thread(target=self._work, daemon=True, name="notifier").start()

    def channels(self):
        n = self.app.cfg.get("notifications") or {}
        out = []
        if n.get("ntfy_url") or n.get("ntfy_url_protected"):
            out.append("ntfy")
        if n.get("discord_webhook") or n.get("discord_webhook_protected"):
            out.append("discord")
        if (n.get("telegram_token") or n.get("telegram_token_protected")) and n.get("telegram_chat_id"):
            out.append("telegram")
        return out

    def send(self, event):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            log.warning("notification queue full, dropping %s", event["kind"])

    def _work(self):
        while True:
            event = self.queue.get()
            try:
                self.deliver(event)
            except Exception:
                log.exception("notification failed")

    def _rate_ok(self):
        now = time.time()
        self.sent = [t for t in self.sent if now - t < 60]
        if len(self.sent) >= MAX_PER_MINUTE:
            return False
        self.sent.append(now)
        return True

    def deliver(self, event):
        """Send one event to every configured channel. Returns {channel: 'ok' or error}."""
        n = self.app.cfg.get("notifications") or {}
        kind = event["kind"]
        if kind != "test" and not (n.get("events") or {}).get(kind):
            return {}
        channels = self.channels()
        if not channels or not self._rate_ok():
            return {}
        image = self._snapshot(event) if n.get("snapshot") else None
        title = "%s: %s" % (event["name"], kind) if kind != "test" else "OS_3DPMS"
        click = self.app.monitor.public_url if self.app.monitor else ""
        results = {}
        for ch in channels:
            try:
                if ch == "ntfy":
                    send_ntfy(config.secret(n, "ntfy_url"), config.secret(n, "ntfy_token"),
                              title, event["text"], click, image, TAGS.get(kind, ""))
                elif ch == "discord":
                    send_discord(config.secret(n, "discord_webhook"), event["text"], image)
                elif ch == "telegram":
                    send_telegram(config.secret(n, "telegram_token"), n["telegram_chat_id"], event["text"], image)
                results[ch] = "ok"
            except Exception as e:
                results[ch] = str(e)[:200]
                log.warning("%s notification failed: %s", ch, e)
        return results

    def _snapshot(self, event):
        hub = self.app.hubs.get(event.get("printer"))
        if hub is None:
            return None
        hub.touch()
        frame, _ = hub.wait_frame(0, 8)
        return frame
