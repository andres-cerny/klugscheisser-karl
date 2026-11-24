"""Static token auth and request logging as thin ASGI wrappers.

Token accepted as `Authorization: Bearer <token>` (Claude, likely Perplexity "API Key"), `X-API-Key: <token>`
(hedge: Perplexity's header is undocumented), or `?token=<token>` for clients that only take a URL.
OAuth discovery paths answer a plain 404 without auth, so clients don't start an OAuth flow we don't offer.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("klugscheisser.access")
OPEN_PATHS = {"/healthz"}
NOT_FOUND_PREFIX = "/.well-known/"
_TOKEN_IN_QUERY = re.compile(r"(token=)[^&]+")


async def _respond(send: Send, status: int, body: dict, extra_headers: list | None = None) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *(extra_headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.strip().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith(NOT_FOUND_PREFIX):
            await _respond(send, 404, {"error": "not found"})
            return
        if not (path == "/mcp" or path.startswith("/mcp/")):
            await self.app(scope, receive, send)  # web app pages have their own access control
            return
        if path in OPEN_PATHS or scope["method"] == "OPTIONS" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        await _respond(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b"Bearer")])

    def _check(self, candidate: bytes) -> bool:
        candidate = candidate.strip()
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization" and value[:7].lower() == b"bearer ":
                if self._check(value[7:]):
                    return True
            elif name == b"x-api-key" and self._check(value):
                return True
        query = parse_qs(scope.get("query_string", b"").decode())
        return self._check(query.get("token", [""])[0].encode())


class AccessLogMiddleware:
    """One line per request; never logs token values (query token redacted, header names only)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = {"code": 0}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        from klugscheisser import calllog

        ua = next(
            (v.decode(errors="replace") for k, v in scope.get("headers", []) if k == b"user-agent"), None
        )
        ua_token = calllog.request_ua.set(ua)  # for the one-line MCP call log
        try:
            await self.app(scope, receive, capture)
        finally:
            calllog.request_ua.reset(ua_token)
            headers = {k.decode(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
            auth = [h for h in ("authorization", "x-api-key") if h in headers]
            query = _TOKEN_IN_QUERY.sub(r"\1***", scope.get("query_string", b"").decode())
            client = headers.get("cf-connecting-ip") or (scope.get("client") or ("?",))[0]
            log.info(
                "%s %s%s -> %s %.0fms host=%s origin=%s ua=%s auth=%s ip=%s",
                scope["method"],
                scope["path"],
                f"?{query}" if query else "",
                status["code"],
                (time.monotonic() - started) * 1000,
                headers.get("host"),
                headers.get("origin", "-"),
                headers.get("user-agent", "-")[:80],
                ",".join(auth) or ("query" if "token=" in query else "none"),
                client,
            )
