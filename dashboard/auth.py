"""Optional password login for the dashboard: hashing, sessions, login rate limiting.

Tailscale identity stays the default way in (auth mode "tailscale"). Modes:

* "tailscale"           - the Tailscale-User-Login header from `tailscale serve` (unchanged)
* "password"            - a dashboard account and password (session cookie)
* "tailscale+password"  - both: a Tailscale account that is allowed AND a dashboard login

Passwords are stored only as PBKDF2-SHA256 hashes (standard library). Sessions live in
memory: restarting OpenPrintDeck signs everyone out, which is acceptable for a single PC.
"""
import hashlib
import hmac
import re
import secrets
import threading
import time

ITERATIONS = 310_000
ROLES = ("admin", "operator", "viewer")
USER_RE = re.compile(r"^[a-z0-9._@\-]{1,64}$")
COOKIE = "os3dpms_session"
MODES = ("tailscale", "password", "tailscale+password")
# API tokens: for programs such as the Orca Slicer plugin (Authorization: Bearer <token>).
TOKEN_ROLES = ("operator", "viewer")
TOKEN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{0,31}$")
TOKEN_PREFIX = "os3_"


def hash_password(password, iterations=ITERATIONS, salt=None):
    if not isinstance(password, str) or len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt, digest.hex())


def verify_password(password, stored):
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt.encode(), int(iterations))
    except (ValueError, AttributeError, TypeError):
        return False
    return hmac.compare_digest(calc.hex(), digest)


def new_token():
    """A new API token. Only its SHA-256 is stored; the token itself is shown once."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def find_token(tokens, token):
    """The api_tokens entry for a presented token, or None. Compares every entry in constant time."""
    if not token or len(token) > 256:
        return None
    digest = token_hash(token)
    match = None
    for entry in tokens or []:
        if hmac.compare_digest(str(entry.get("token_sha256", "")), digest):
            match = entry
    return match


class Sessions:
    def __init__(self, hours=12):
        self.ttl = hours * 3600
        self.lock = threading.Lock()
        self.items = {}  # token -> (user, role, expires)

    def create(self, user, role):
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.items = {t: v for t, v in self.items.items() if v[2] > now}
            self.items[token] = (user, role, now + self.ttl)
        return token

    def get(self, token):
        if not token:
            return None
        with self.lock:
            item = self.items.get(token)
            if item is None or item[2] < time.time():
                self.items.pop(token, None)
                return None
            return item[0], item[1]

    def drop(self, token):
        with self.lock:
            self.items.pop(token, None)

    def drop_user(self, user):
        with self.lock:
            self.items = {t: v for t, v in self.items.items() if v[0] != user}


class RateLimiter:
    """At most `limit` events per `window` seconds per key (login attempts, control actions)."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.lock = threading.Lock()
        self.hits = {}

    def allow(self, key):
        now = time.time()
        with self.lock:
            recent = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            recent.append(now)
            self.hits[key] = recent
            if len(self.hits) > 10000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
            return True

    def count(self, key):
        """Events for key in the current window (without adding one)."""
        now = time.time()
        with self.lock:
            return len([t for t in self.hits.get(key, []) if now - t < self.window])

    def reset(self, key):
        with self.lock:
            self.hits.pop(key, None)


def cookie_value(header, name=COOKIE):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""
