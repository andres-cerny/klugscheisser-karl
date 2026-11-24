"""Shared plumbing for results collectors: polite HTTP, name and score helpers.

Every collector identifies itself, fetches sequentially with a small pause, and never raises to the
caller: on errors it logs and returns what it has (possibly []).
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import UTC, datetime

import httpx

from klugscheisser.domain.names import normalize
from klugscheisser.interfaces import GameResult

log = logging.getLogger(__name__)

# German team names as in seeds/teams.yaml, so events link to the seeded national teams (IIHF codes)
TEAM_DE = {
    "SUI": "Schweiz",
    "USA": "USA",
    "CZE": "Tschechien",
    "GER": "Deutschland",
    "AUT": "Österreich",
    "CAN": "Kanada",
    "FIN": "Finnland",
    "SWE": "Schweden",
    "JPN": "Japan",
    "DEN": "Dänemark",
    "SVK": "Slowakei",
    "HUN": "Ungarn",
    "FRA": "Frankreich",
    "NOR": "Norwegen",
    "CHN": "China",
    "ITA": "Italien",
    "NED": "Niederlande",
    "GBR": "Grossbritannien",
    "KOR": "Südkorea",
    "POL": "Polen",
    "LAT": "Lettland",
    "KAZ": "Kasachstan",
    "SLO": "Slowenien",
}
# English country names as sihf.ch writes them -> IIHF code
NATION_CODE = {
    "switzerland": "SUI",
    "usa": "USA",
    "united states": "USA",
    "czechia": "CZE",
    "czech republic": "CZE",
    "germany": "GER",
    "austria": "AUT",
    "canada": "CAN",
    "finland": "FIN",
    "sweden": "SWE",
    "japan": "JPN",
    "denmark": "DEN",
    "slovakia": "SVK",
    "hungary": "HUN",
    "france": "FRA",
    "norway": "NOR",
    "china": "CHN",
    "italy": "ITA",
}

USER_AGENT = "klugscheisser-athlete/0.1 (+https://github.com/andres-cerny/klugscheisser_athlete)"
TIMEOUT_S = 20.0


class PoliteHttp:
    """Sequential GETs with an identified user agent and a pause between requests."""

    def __init__(self, delay_s: float = 1.0, client: httpx.Client | None = None) -> None:
        self.delay_s = delay_s
        self._client = client
        self._last = 0.0
        self.requests = 0

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, follow_redirects=True
            )
        return self._client

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        wait = self._last + self.delay_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.client.get(url, params=params, headers=headers)
        finally:
            self._last = time.monotonic()
            self.requests += 1
        resp.raise_for_status()
        return resp

    def json(self, url: str, params: dict | None = None, headers: dict | None = None):
        return self.get(url, params, headers).json()

    def text(self, url: str, params: dict | None = None) -> str:
        return self.get(url, params).text


class KnownFinal:
    """Mixin: ids of games already stored as final; their details are not fetched again.

    `ingest_results` fills `known_final` before calling `collect()`. `refetch`: ids read again in detail although
    stored as final (the game watch re-checks a fresh final, IIHF corrects game sheets afterwards).
    """

    known_final: set[str]
    refetch: set[str]

    def is_known_final(self, external_id: str) -> bool:
        return external_id in getattr(self, "known_final", set()) and external_id not in getattr(
            self, "refetch", set()
        )


def wants(teams: list[str] | None, *names: str | None) -> bool:
    """True if no team filter is set or one of the game's team names/codes matches a filter entry."""
    if not teams:
        return True
    have = [normalize(n) for n in names if n]
    for t in teams:
        q = normalize(t)
        if q and any(q == h or (len(q) >= 4 and (q in h or h in q) and len(h) >= 4) for h in have):
            return True
    return False


def first_last(last_first: str) -> str:
    """'Rüedi Lisa' -> 'Lisa Rüedi'; 'Thuvik, Hanna' -> 'Hanna Thuvik'. Moves the last token to the front
    when there is no comma (the matcher also tries rotations, so multi-word names still match)."""
    s = " ".join(last_first.split())
    if "," in s:
        last, first = (x.strip() for x in s.split(",", 1))
        return f"{first} {last}".strip()
    parts = s.split()
    return " ".join(parts[-1:] + parts[:-1]) if len(parts) >= 2 else s


def title_name(given: str | None, family: str | None) -> str:
    """IIHF style 'MULLER' + 'Alina' -> 'Alina Muller'."""
    fam = " ".join(w.capitalize() if w.isupper() else w for w in (family or "").replace("-", "- ").split())
    return " ".join(filter(None, [(given or "").strip(), fam.replace("- ", "-")]))


def ot_suffix(periods: int, shootout: bool = False, overtime: bool = False) -> str:
    if shootout or periods >= 5:
        return " n.P."
    if overtime or periods == 4:
        return " n.V."
    return ""


def in_window(game: GameResult, since: datetime | None) -> bool:
    """Scheduled/live games always count; final games only if they started after `since`."""
    if game.status != "final" or since is None or game.starts_at is None:
        return True
    return game.starts_at >= since


def slug(text: str) -> str:
    """'Södertälje' -> 'sodertalje' (keys and ids)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def season_label(season: int) -> str:
    """Season alias of the federations (2027 = 2026/27) -> '2026/27'."""
    return f"{season - 1}/{season % 100:02d}"


def season_of(day) -> int:
    """Season alias for a date: August to July (2026-10-03 -> 2027)."""
    return day.year + 1 if day.month >= 8 else day.year


def utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
