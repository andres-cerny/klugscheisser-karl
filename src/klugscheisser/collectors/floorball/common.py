"""Shared bits of the floorball adapters: polite HTTP with a request cap, HTML table rows, season helpers."""

from __future__ import annotations

import html
import re
from datetime import date

import httpx

from klugscheisser.collectors.results.base import PoliteHttp


class RequestCapReached(RuntimeError):
    """The adapter has used its request budget; callers stop and report what they have."""


class CappedHttp(PoliteHttp):
    """`PoliteHttp` (identified user agent, pause between requests) that refuses to go over `max_requests`."""

    def __init__(
        self, delay_s: float = 1.5, max_requests: int = 200, client: httpx.Client | None = None
    ) -> None:
        super().__init__(delay_s, client)
        self.max_requests = max_requests

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        if self.requests >= self.max_requests:
            raise RequestCapReached(f"request cap {self.max_requests} reached")
        return super().get(url, params, headers)


_TAG = re.compile(r"<[^>]+>")
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_HREF = re.compile(r"href=['\"]?([^'\" >]+)", re.I)


def text_of(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def table_rows(page: str) -> list[list[tuple[str, str | None]]]:
    """Body rows of every table in `page`: per cell (text, first href or None). Header rows (<th>) are skipped."""
    out = []
    for row in _ROW.findall(page):
        if "<td" not in row.lower():
            continue
        cells = []
        for cell in _CELL.findall(row):
            href = _HREF.search(cell)
            cells.append((text_of(cell), html.unescape(href.group(1)) if href else None))
        out.append(cells)
    return out


def to_int(value: str | None) -> int | None:
    """'12' -> 12; '', '-', None -> None (no data, never 0)."""
    v = (value or "").strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else None


def season_start(label: str) -> int | None:
    """'2026/27' -> 2026; '2026' -> 2026."""
    hit = re.match(r"\s*(\d{4})", label or "")
    return int(hit.group(1)) if hit else None


def current_season(today: date | None = None) -> int:
    """Start year of the running floorball season (seasons start in August)."""
    d = today or date.today()
    return d.year if d.month >= 8 else d.year - 1
