"""Swiss national-team games from the sihf.ch team pages (HTML): friendlies, camps, tournaments, any opponent.

Per team page (default: the women's national team):
- Results: https://www.sihf.ch/de/nationalteams/{path}/resultate-statistiken/ — first page only (the newest 15
  games of all seasons: date, event, place, home, away, result). Further pages and the CSV/PDF exports are
  loaded from /umbraco/…, which robots.txt disallows: never requested.
- Programme: https://www.sihf.ch/de/nationalteams/{path}/programm/ — one block per camp/tournament with its
  games (date, time, "SUI - SWE", place). The column header says "Zeit (MESZ)", but the times are Swiss local
  time all year (WCh SUI–USA 6.11.2026 19:00 = 18:00 UTC, IIHF). Stored as Europe/Zurich.

This is the fallback source: a game that another collector (Leijonat, IIHF) already stored for the same day and
teams is skipped, and when such a collector finds a game first stored from here, it takes the row over
(ingest/results.py). Results have no goals or line-ups, so items carry the score only.
External id: "{code}-{date}-{home}-{away}" with IIHF codes, e.g. "sui-2026-10-03-sui-cze".
Terms (sihf.ch): personal, non-commercial use only (see docs/sources.md).
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import (
    NATION_CODE,
    TEAM_DE,
    KnownFinal,
    PoliteHttp,
    season_label,
    season_of,
    slug,
    wants,
)
from klugscheisser.interfaces import GameResult

log = logging.getLogger(__name__)

BASE = "https://www.sihf.ch/de/nationalteams/{path}/"
TZ = ZoneInfo("Europe/Zurich")
SOURCE = "SIHF"

_ROW = re.compile(r"<tr[^>]*class=\"c-table__row[^\"]*\"[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_BLOCK = re.compile(r"<details[^>]*class=\"c-accordion[^\"]*\"[^>]*>(.*?)</details>", re.S)
_TITLE = re.compile(r"c-accordion__title\">(.*?)</h3>", re.S)
_PAIR = re.compile(r"^\s*([A-Z]{3})\s*[-–]\s*([A-Z]{3})\s*$")
_PLACE = re.compile(r"(?P<city>[^/]+?)\s*/\s*(?P<cc>[A-Z]{3})\b")
_SCORE = re.compile(r"(\d+)\s*:\s*(\d+)\s*(?:\((\w+)\))?")


@dataclass(frozen=True)
class NtTeam:
    path: str = "womens-national-team"  # sihf.ch URL segment of the team page
    team: str = "Schweiz"  # our team name (seeds/teams.yaml) for the Swiss side
    code: str = "SUI"  # code the programme uses for the Swiss side; also the id prefix

    @property
    def results_url(self) -> str:
        return BASE.format(path=self.path) + "resultate-statistiken/"

    @property
    def programme_url(self) -> str:
        return BASE.format(path=self.path) + "programm/"


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(_TAG.sub(" ", fragment)).split())


def _code(name: str) -> str | None:
    n = (name or "").strip()
    if re.fullmatch(r"[A-Z]{3}", n):
        return n
    return NATION_CODE.get(n.lower())


def _side(team: NtTeam, code: str | None, name: str) -> str:
    if code == team.code:
        return team.team
    return TEAM_DE.get(code or "", name)


def _score(text: str) -> str | None:
    mt = _SCORE.search(text or "")
    if not mt:
        return None
    extra = (mt.group(3) or "").upper()
    suffix = " n.P." if extra in ("SO", "PS", "GWS") else " n.V." if extra in ("OT", "SD") else ""
    return f"{mt.group(1)}:{mt.group(2)}{suffix}"


def _day(text: str) -> date | None:
    mt = _DATE.match(text.strip())
    return date(int(mt.group(3)), int(mt.group(2)), int(mt.group(1))) if mt else None


def parse_results(page: str) -> list[dict]:
    """Rows of the results table: {day, event, place, home, away, home_code, away_code, score}."""
    rows = []
    for row in _ROW.findall(page or ""):
        cells = [_text(c) for c in _CELL.findall(row)]
        if len(cells) < 6 or (day := _day(cells[0])) is None:
            continue
        rows.append(
            {
                "day": day,
                "event": cells[1],
                "place": cells[2],
                "home": cells[3],
                "away": cells[4],
                "home_code": _code(cells[3]),
                "away_code": _code(cells[4]),
                "score": _score(cells[5]),
            }
        )
    return rows


def parse_programme(page: str) -> list[dict]:
    """Games of the programme blocks: {day, time, home_code, away_code, place, block}."""
    out = []
    for block in _BLOCK.findall(page or ""):
        title = _text(t.group(1)) if (t := _TITLE.search(block)) else ""
        for row in _ROW.findall(block):
            cells = [_text(c) for c in _CELL.findall(row)]
            if len(cells) < 4 or (day := _day(cells[0])) is None:
                continue
            pair = _PAIR.match(cells[2])
            clock = _TIME.match(cells[1])
            if not pair:
                continue
            out.append(
                {
                    "day": day,
                    "time": time(int(clock.group(1)), int(clock.group(2))) if clock else None,
                    "home_code": pair.group(1),
                    "away_code": pair.group(2),
                    "place": cells[3],
                    "block": title,
                }
            )
    return out


def _context(event: str, block: str, place: str, day: date) -> dict:
    """Competition and tournament container from the event name / programme block title."""
    label = season_label(season_of(day))
    text = f"{block} {event}"
    pm = _PLACE.search(block.split(" in ", 1)[-1] if " in " in block else place) or _PLACE.search(place)
    city = pm.group("city").strip() if pm else (place or None)
    where = f"{city} ({pm.group('cc')})" if pm else city
    if re.search(r"euro ?hockey ?tour|\bEHT\b", text, re.I) and city:
        return {
            "competition": f"Women's Euro Hockey Tour {label}, {city}",
            "competition_key": f"eht-{label.replace('/', '-')}-{slug(city)}",
            "tournament": {
                "prefix": "eht",
                "key": f"eht-{day.year}-{slug(city)}",
                "name": f"Women's Euro Hockey Tour, {city}",
                "city": where,
            },
        }
    if re.search(r"world championship|\bWM\b", text, re.I):
        return {
            "competition": f"IIHF Frauen-WM {day.year}",
            "competition_key": f"wch-{day.year}",
            "tournament": {
                "prefix": "wch",
                "key": f"wch-{day.year}",
                "name": f"IIHF Frauen-WM {day.year}",
                "city": None,
            },
        }
    if re.search(r"olympi", text, re.I):
        return {
            "competition": f"Olympische Winterspiele {day.year} Frauen-Eishockey",
            "competition_key": f"oly-{day.year}",
            "tournament": {"prefix": "oly", "key": f"oly-{day.year}", "name": event, "city": None},
        }
    return {
        "competition": f"Frauen-Länderspiele {label}",
        "competition_key": f"nt-sui-{label.replace('/', '-')}",
        "tournament": None,
        "event_name": event or block or None,
    }


def merge(team: NtTeam, results: list[dict], programme: list[dict]) -> list[GameResult]:
    """One GameResult per game: result rows (score) joined with programme rows (time, block) by day + teams."""
    games: dict[tuple, dict] = {}
    for r in results:
        key = (r["day"], r["home_code"] or slug(r["home"]), r["away_code"] or slug(r["away"]))
        games[key] = {**r}
    for p in programme:
        key = (p["day"], p["home_code"], p["away_code"])
        g = games.setdefault(
            key,
            {
                "day": p["day"],
                "event": "",
                "place": p["place"],
                "home": p["home_code"],
                "away": p["away_code"],
                "home_code": p["home_code"],
                "away_code": p["away_code"],
                "score": None,
            },
        )
        g["time"], g["block"] = p["time"], p["block"]
        g["place"] = g.get("place") or p["place"]
    out = []
    for (day, home_key, away_key), g in sorted(games.items(), key=lambda kv: kv[0][0]):
        if team.code not in (g["home_code"], g["away_code"]):
            continue  # only games of the page's team
        ctx = _context(g.get("event") or "", g.get("block") or "", g.get("place") or "", day)
        starts = datetime.combine(day, g.get("time") or time(0, 0), tzinfo=TZ)
        ext = f"{team.code.lower()}-{day.isoformat()}-{slug(home_key)}-{slug(away_key)}"
        tour = ctx.get("tournament")
        city = (tour or {}).get("city") or (g.get("place") or "").split(" / ")[0] or None
        out.append(
            GameResult(
                source=SOURCE,
                sport="icehockey_women",
                competition=ctx["competition"],
                external_id=ext,
                # one page for all games: the fragment keeps the item URL unique per game
                url=f"{team.results_url}#{ext}",
                status="final" if g["score"] else "scheduled",
                starts_at=starts,
                home=_side(team, g["home_code"], g["home"]),
                away=_side(team, g["away_code"], g["away"]),
                score=g["score"],
                venue=g.get("place") or None,
                raw={
                    "home_code": g["home_code"],
                    "away_code": g["away_code"],
                    "event": g.get("event") or None,
                    "block": g.get("block") or None,
                    "time_known": g.get("time") is not None,
                    "competition_key": ctx["competition_key"],
                    "competition_kind": "tournament" if tour else "series",
                    "season": season_label(season_of(day)),
                    "tournament": tour,
                    "city": city,
                    # a final score is all this source has: enough for a short result item
                    "detail": bool(g["score"]),
                },
            )
        )
    return out


class SihfNtCollector(KnownFinal):
    """Fallback for national-team games no other source has (friendlies, camps); see the module doc."""

    name = "sihfnt"
    source_label = "SIHF Nationalteam"
    fallback = True  # ingest skips its games when another source has them, and lets others take its rows over

    def __init__(
        self,
        teams: list[NtTeam] | None = None,
        delay_s: float = 1.0,
        http: PoliteHttp | None = None,
    ) -> None:
        self.teams = teams or [NtTeam()]
        self.http = http or PoliteHttp(delay_s)
        self.known_final = set()

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        out: list[GameResult] = []
        for team in self.teams:
            results, programme = [], []
            try:
                results = parse_results(self.http.text(team.results_url))
            except Exception as exc:
                log.warning("sihfnt: results of %s failed: %s", team.path, exc)
            try:
                programme = parse_programme(self.http.text(team.programme_url))
            except Exception as exc:
                log.warning("sihfnt: programme of %s failed: %s", team.path, exc)
            for game in merge(team, results, programme):
                if wants(teams, game.home, game.away, game.raw["home_code"], game.raw["away_code"]):
                    out.append(game)
        return out
