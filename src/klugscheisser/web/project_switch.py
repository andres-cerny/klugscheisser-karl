"""v7: which project a request belongs to.

- `/p/<key>/…` (web) — the prefix is removed, the project is remembered in a cookie, so links inside the app
  (`/person/12`) keep working without the prefix. Shareable links can always carry the prefix.
- `/mcp/<key>` (Perplexity connector per project) — rewritten to `/mcp` for the one MCP endpoint.
- anything else — the remembered project (cookie), else the default project.

2026-10-19: every root-relative app link in an HTML response (`href`, `action`, `src`, `hx-get`, `hx-post`, …)
gets the project prefix of the request (`/person/12` → `/p/eishockey/person/12`). Views keep writing plain
`/person/12`; the prefix is added here in one place. Without it, inner links resolved through the cookie only,
so a page opened in one project led into the other one (404) after the other project was used in between.
"""

from __future__ import annotations

import re
from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from klugscheisser import projects

COOKIE = "ks_project"

# URL attributes of app links; values starting with one "/" (not "//", not already "/p/…", not the paths that
# belong to no project: login/logout, MCP, health check, static files).
_LINK = re.compile(
    rb"""(\b(?:href|action|src|formaction|poster|hx-get|hx-post|hx-put|hx-patch|hx-delete|hx-push-url)=)(["'])"""
    rb"""/(?!/|p/|mcp(?:[/?"']|$)|healthz\b|static/|login\b|logout\b)"""
)


def prefix_links(body: bytes, key: str) -> bytes:
    """Put `/p/<key>` in front of every root-relative app link of an HTML document or fragment."""
    pre = b"/p/" + key.encode() + b"/"
    return _LINK.sub(lambda m: m.group(1) + m.group(2) + pre, body)


def _is_html(message: Message) -> bool:
    for name, value in message.get("headers") or []:
        if name.lower() == b"content-type":
            return value.lower().startswith(b"text/html")
    return False


def _cookie(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:
                return None
            if COOKIE in jar:
                return jar[COOKIE].value
    return None


async def _not_found(send: Send) -> None:
    body = b"Unbekanntes Projekt"
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ProjectMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path") or "/"
        remember = None
        key = None
        parts = path.split("/", 3)  # "", "p", key, rest
        if len(parts) >= 3 and parts[1] in ("p", "mcp") and parts[2]:
            key = parts[2]
            if projects.get(key) is None:
                if parts[1] == "p":
                    await _not_found(send)
                    return
                key = None  # /mcp/<something else>: leave untouched
            else:
                rest = "/" + parts[3] if len(parts) > 3 else ""
                new = (rest or "/") if parts[1] == "p" else "/mcp" + rest
                scope = {**scope, "path": new, "raw_path": new.encode()}
                remember = key if parts[1] == "p" else None
        if key is None:
            remembered = _cookie(scope)
            key = remembered if projects.get(remembered) else projects.default().key
        token = projects.set_current(key)

        held: dict[str, Message] = {}  # the response start of an HTML page, sent with the rewritten body
        chunks: list[bytes] = []

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                if remember:
                    headers = list(message.get("headers") or [])
                    cookie = f"{COOKIE}={remember}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly"
                    headers.append((b"set-cookie", cookie.encode()))
                    message = {**message, "headers": headers}
                if _is_html(message):
                    held["start"] = message
                    return
            elif message["type"] == "http.response.body" and "start" in held:
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                body = prefix_links(b"".join(chunks), key)
                start = held.pop("start")
                headers = [(n, v) for n, v in start.get("headers") or [] if n.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                message = {"type": "http.response.body", "body": body}
            await send(message)

        try:
            await self.app(scope, receive, send_with_cookie)
        finally:
            projects._current.reset(token)
