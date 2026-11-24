"""Per-call notes for the one-line MCP call log.

One line per MCP tool call (logger `klugscheisser.tools`), e.g.

    mcp_call tool=frage dur=2.41s chars=1985 capped=no err=no ua=Perplexity-MCP/1.0 cost=$0.0270 cache=miss
    refresh=läuft:0.0s args=question='Wer trainiert…' person='Lara Stalder'

The HTTP middleware (`mcp_server/auth.AccessLogMiddleware`) puts the caller's user agent into `request_ua`; the
MCP tool wrapper (`mcp_server/server.timed`) opens a `CallNotes` for the call; code deeper down (refresh, answer
cache, cost ledger) adds what it saw with `note()` / `add_cost()`. Outside a tool call these are no-ops. Context
variables follow the call into worker threads of `projects.ContextPool` (the object is shared, so their notes
count too). Only what the logs already hold: tool arguments (public names and questions), the client's product
token, durations and costs. Never tokens, headers or IPs.

Off: `MCP_CALL_LOG=false` (the old `tool … done in …` lines stay either way).
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Any

request_ua: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_ua", default=None)

ARG_MAX_CHARS = 80  # per argument value in the log line
ARGS_MAX_CHARS = 300  # whole args summary


@dataclass
class CallNotes:
    notes: dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0
    cost_rows: int = 0


_current: contextvars.ContextVar[CallNotes | None] = contextvars.ContextVar("call_notes", default=None)


def start() -> tuple[CallNotes, contextvars.Token]:
    notes = CallNotes()
    return notes, _current.set(notes)


def end(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> CallNotes | None:
    return _current.get()


def note(key: str, value: str) -> None:
    if (c := _current.get()) is not None:
        c.notes.setdefault(key, value)  # the first word counts (e.g. the asked person's refresh)


def add_cost(usd: float | None) -> None:
    if (c := _current.get()) is not None:
        c.cost_usd += float(usd or 0.0)
        c.cost_rows += 1


def short_ua(ua: str | None) -> str:
    """'Perplexity-MCP/1.0', 'python-httpx/0.28' or the browser token; no spaces (one log field)."""
    if not ua or not ua.strip():
        return "-"
    first = ua.split()[0]
    m = (
        re.search(r"(HeadlessChrome|Edg|Chrome|Firefox|Safari)/[\d.]+", ua)
        if first.startswith("Mozilla")
        else None
    )
    return (m.group(0) if m else first)[:40]


def _value(v: Any) -> str:
    if isinstance(v, str):
        s = " ".join(v.split())
        return repr(s if len(s) <= ARG_MAX_CHARS else s[: ARG_MAX_CHARS - 1] + "…")
    if isinstance(v, int | float | bool) or v is None:
        return repr(v)
    if isinstance(v, list | tuple):
        return f"[{len(v)}]"
    return type(v).__name__


def summarize_args(args: tuple, kwargs: dict[str, Any], names: list[str] | None = None) -> str:
    """`question='…' person='Lara Stalder'`: positional args named after the function's parameters; None left
    out; long strings cut, lists shown as their length."""
    names = names or []
    parts = [
        f"{names[i] if i < len(names) else f'arg{i}'}={_value(v)}"
        for i, v in enumerate(args)
        if v is not None
    ]
    parts += [f"{k}={_value(v)}" for k, v in kwargs.items() if v is not None]
    out = " ".join(parts)
    return out if len(out) <= ARGS_MAX_CHARS else out[: ARGS_MAX_CHARS - 1] + "…"


def format_line(
    tool: str,
    dur_s: float,
    chars: int,
    capped: bool,
    failed: bool,
    notes: CallNotes | None,
    args: str,
) -> str:
    n = notes or CallNotes()
    cost = f"${n.cost_usd:.4f}" if n.cost_rows else "$0"
    return (
        f"mcp_call tool={tool} dur={dur_s:.2f}s chars={chars} capped={'yes' if capped else 'no'} "
        f"err={'yes' if failed else 'no'} ua={short_ua(request_ua.get())} cost={cost} "
        f"cache={n.notes.get('cache', 'none')} refresh={n.notes.get('refresh', 'none')} args={args or '-'}"
    )
