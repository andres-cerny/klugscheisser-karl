"""Access control for the web pages (not for /mcp, which keeps its bearer token).

1. WEB_ACCESS_EMAILS set: the email Cloudflare Access puts into `Cf-Access-Authenticated-User-Email` must be in
   the list. Only safe when the app is reachable *only* through the Cloudflare tunnel (binds 127.0.0.1).
2. WEB_PASSWORD set: a signed cookie from /login is accepted too (HMAC-SHA256, stdlib only).
3. Neither set: open (local development).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlparse

from starlette.requests import Request

from klugscheisser.config import Settings

EMAIL_HEADER = "cf-access-authenticated-user-email"
COOKIE = "ks_session"


def _key(settings: Settings) -> bytes:
    secret = settings.web_secret or settings.web_password
    raw = secret.get_secret_value() if secret else ""
    return hashlib.sha256(b"klugscheisser-web:" + raw.encode()).digest()


def sign_session(settings: Settings, now: float | None = None) -> str:
    expires = int((now or time.time()) + settings.web_session_days * 86400)
    sig = hmac.new(_key(settings), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_session(settings: Settings, value: str | None) -> bool:
    if not value or "." not in value or settings.web_password is None:
        return False
    expires, sig = value.split(".", 1)
    good = hmac.new(_key(settings), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires.isdigit() and int(expires) > time.time()


def check_password(settings: Settings, password: str) -> bool:
    if settings.web_password is None:
        return False
    return hmac.compare_digest(password.encode(), settings.web_password.get_secret_value().encode())


# --- login brake: after MAX_FAILS wrong passwords from one address within WINDOW_S, that address waits ---------

MAX_FAILS = 5
WINDOW_S = 15 * 60
_fails: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    """The caller's address. Behind Caddy/cloudflared uvicorn already puts the forwarded client here
    (proxy headers are trusted only from 127.0.0.1)."""
    return request.client.host if request.client else "unknown"


def locked_for(ip: str, now: float | None = None) -> int:
    """Seconds this address still has to wait (0 = may try)."""
    now = now or time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < WINDOW_S]
    _fails[ip] = recent
    if len(recent) < MAX_FAILS:
        return 0
    return int(WINDOW_S - (now - recent[-MAX_FAILS])) + 1


def note_failure(ip: str, now: float | None = None) -> None:
    _fails.setdefault(ip, []).append(now or time.time())
    if len(_fails) > 10_000:  # never grow without bound
        _fails.clear()


def note_success(ip: str) -> None:
    _fails.pop(ip, None)


def allowed_emails(settings: Settings) -> set[str]:
    return {x.strip().lower() for x in settings.web_access_emails or [] if x.strip()}


def decide(settings: Settings, request: Request) -> str:
    """'ok' | 'login' (password form) | 'forbidden'."""
    emails = allowed_emails(settings)
    if not emails and settings.web_password is None:
        return "ok"
    email = (request.headers.get(EMAIL_HEADER) or "").strip().lower()
    if emails and email in emails:
        return "ok"
    if settings.web_password is not None:
        return "ok" if valid_session(settings, request.cookies.get(COOKIE)) else "login"
    return "forbidden"


def same_origin(request: Request) -> bool:
    """Cheap CSRF guard for POSTs: a browser Origin header must match the Host."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    return urlparse(origin).netloc == request.headers.get("host")
