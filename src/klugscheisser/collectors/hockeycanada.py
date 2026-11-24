"""Tournament rosters of IIHF women's events from the Hockey Canada stats site (free profile source, 2026-10-25).

hockeycanada.ca mirrors the rosters of *all* teams of the IIHF women's events Canada plays (WCh, Olympics, U18 WCh):
number, position, shoots/catches, height (feet'inches), weight (lb), birth date, hometown and current club. The
pages are server-rendered (Next.js); the roster sits in the page's `__NEXT_DATA__` JSON
(`props.pageProps.teamDataTournament.TeamRoster_Players / _Goalies`). Per event:

- `…/<event path>/stats/schedule` → the teams of the event (`home`/`away` objects with id, name, abbr). Team ids
  differ per event family (WCh: 179 CAN … 186 SUI; Olympics 2026: 362 CAN, 364 SUI; U18: 131 CAN, 138 SUI).
- `…/<event path>/stats/team-rosters/<team id>` → the roster of one team (empty until the team is entered).

Rules: robots.txt allows these paths (only seasons <= 2012-13, AJAX stats and a few others are disallowed; no
Crawl-delay), identified User-Agent, one request at a time with `HOCKEYCANADA_DELAY_S` (3 s) between requests.
Copyright note on the site ("proprietary materials … may not be reproduced"): only facts (height, birth date …) are
kept, with the roster page as link; photos (assets.leaguestat.com) are never stored or shown.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from klugscheisser.collectors.profiles.http import PoliteClient

log = logging.getLogger(__name__)

SOURCE = "hockeycanada"
BASE = "https://www.hockeycanada.ca/en-ca/team-canada/women"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# team names as the site writes them → IOC code (the site's own abbr is wrong for Austria: "AUS")
COUNTRY_CODES = {
    "canada": "CAN",
    "united states": "USA",
    "finland": "FIN",
    "switzerland": "SUI",
    "czechia": "CZE",
    "czech republic": "CZE",
    "germany": "GER",
    "sweden": "SWE",
    "japan": "JPN",
    "denmark": "DEN",
    "austria": "AUT",
    "hungary": "HUN",
    "norway": "NOR",
    "china": "CHN",
    "italy": "ITA",
    "france": "FRA",
    "slovakia": "SVK",
}
POSITION = {"F": "F", "D": "D", "G": "G", "FORWARD": "F", "DEFENCE": "D", "DEFENSE": "D", "GOALTENDER": "G"}


@dataclass(frozen=True)
class HcEvent:
    key: str  # our event key ("wch-2025"); also the label in facts
    path: str  # below BASE: "national/2024-25/world-championship"
    label: str  # German name for answers
    start: date  # first day of the tournament (data date of the roster)
    end: date
    current: bool = False  # the event Fabi prepares for: re-read nightly until it is over

    @property
    def season(self) -> int:
        """Season start year (2025 = 2025/26); a tournament from August on belongs to that year's season."""
        return self.start.year if self.start.month >= 8 else self.start.year - 1

    def schedule_url(self) -> str:
        return f"{BASE}/{self.path}/stats/schedule"

    def roster_url(self, team_id: str | int) -> str:
        return f"{BASE}/{self.path}/stats/team-rosters/{team_id}"


# Newest first: when a person is on several rosters, the newest value of each field wins.
EVENTS: tuple[HcEvent, ...] = (
    HcEvent(
        "wch-2026",
        "national/2026-27/world-championship",
        "IIHF Frauen-WM 2026",
        date(2026, 11, 6),
        date(2026, 11, 16),
        current=True,
    ),
    HcEvent("oly-2026", "olympics/2026", "Olympische Spiele 2026", date(2026, 2, 5), date(2026, 2, 19)),
    HcEvent(
        "u18-2026",
        "under-18/2025-26/world-championship",
        "U18-Frauen-WM 2026",
        date(2026, 1, 10),
        date(2026, 1, 17),
    ),
    HcEvent(
        "wch-2025",
        "national/2024-25/world-championship",
        "IIHF Frauen-WM 2025",
        date(2025, 4, 9),
        date(2025, 4, 20),
    ),
    HcEvent(
        "u18-2025",
        "under-18/2024-25/world-championship",
        "U18-Frauen-WM 2025",
        date(2025, 1, 8),
        date(2025, 1, 15),
    ),
    HcEvent(
        "wch-2024",
        "national/2023-24/world-championship",
        "IIHF Frauen-WM 2024",
        date(2024, 4, 3),
        date(2024, 4, 14),
    ),
)


def event_by_key(key: str) -> HcEvent:
    for e in EVENTS:
        if e.key == key:
            return e
    raise KeyError(f"unknown Hockey Canada event {key!r}; known: {', '.join(e.key for e in EVENTS)}")


@dataclass
class HcTeam:
    team_id: str
    name: str
    code: str  # IOC


@dataclass
class HcPlayer:
    name: str
    first_name: str | None
    last_name: str | None
    hc_id: str | None
    jersey: int | None
    position: str | None  # F | D | G
    club: str | None
    birth_date: date | None
    height_cm: int | None
    height_raw: str | None  # "5'6"
    weight_kg: int | None
    weight_lb: int | None
    hometown: str | None
    shoots: str | None  # L | R (catches for goalies)


@dataclass
class HcRoster:
    event: HcEvent
    team: HcTeam
    url: str
    players: list[HcPlayer] = field(default_factory=list)


def _next_data(html: str) -> dict:
    hit = NEXT_DATA.search(html or "")
    if not hit:
        raise ValueError("page has no __NEXT_DATA__ block")
    return json.loads(hit.group(1))


def parse_teams(html: str) -> list[HcTeam]:
    """Teams of an event from its schedule page. Placeholders ("TBD", "W26", "L27") have no country and are left
    out; the order is the order of first appearance."""
    data = _next_data(html)
    out: dict[str, HcTeam] = {}

    def walk(o) -> None:
        if isinstance(o, dict):
            for side in ("home", "away"):
                t = o.get(side)
                if isinstance(t, dict) and t.get("id") is not None and t.get("name"):
                    code = COUNTRY_CODES.get(str(t["name"]).strip().lower())
                    if code and str(t["id"]) not in out:
                        out[str(t["id"])] = HcTeam(str(t["id"]), str(t["name"]).strip(), code)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data.get("props") or {})
    return list(out.values())


def feet_inches_cm(raw: str | None) -> int | None:
    """ "5'6" / "5' 6\"" / "5-6" → 168 (rounded); implausible values → None."""
    hit = re.match(r"\s*(\d)\s*['’\-]\s*(\d{1,2})?", raw or "")
    if not hit:
        return None
    feet, inches = int(hit.group(1)), int(hit.group(2) or 0)
    if inches > 11:
        return None
    cm = round((feet * 12 + inches) * 2.54)
    return cm if 140 <= cm <= 205 else None


def lb_kg(raw: str | None) -> tuple[int | None, int | None]:
    """ "139" (lb) → (139, 63); "0" or empty (the site's "unknown") → (None, None)."""
    try:
        lb = int(str(raw or "").strip())
    except ValueError:
        return None, None
    if not 80 <= lb <= 260:
        return None, None
    return lb, round(lb * 0.45359237)


def us_date(raw: str | None) -> date | None:
    """ "05-15-1994" (MM-DD-YYYY) → date(1994, 5, 15)."""
    hit = re.match(r"\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$", raw or "")
    if not hit:
        return None
    try:
        return date(int(hit.group(3)), int(hit.group(1)), int(hit.group(2)))
    except ValueError:
        return None


def _clean(text: str | None) -> str | None:
    text = re.sub(r"\s+", " ", str(text or "")).strip(" ,")
    return text or None


def _player(p: dict) -> HcPlayer | None:
    name = _clean(p.get("Name")) or _clean(f"{p.get('FirstName') or ''} {p.get('LastName') or ''}")
    if not name:
        return None
    info = {str(i.get("Name") or "").strip().lower(): i.get("Value") for i in p.get("Info") or []}
    height_raw = _clean(info.get("height"))
    lb, kg = lb_kg(info.get("weight"))
    hand = str(info.get("catches") or info.get("shoots") or "").strip().upper()[:1]
    pos = POSITION.get(str(p.get("Position") or p.get("PositionDescription") or "").strip().upper())
    try:
        jersey = int(str(p.get("JerseyNumber") or "").strip())
    except ValueError:
        jersey = None
    return HcPlayer(
        name=name,
        first_name=_clean(p.get("FirstName")),
        last_name=_clean(p.get("LastName")),
        hc_id=str(p["Id"]) if p.get("Id") is not None else None,
        jersey=jersey,
        position=pos,
        club=_clean(p.get("CurrentTeam")),
        birth_date=us_date(info.get("date of birth")),
        height_cm=feet_inches_cm(height_raw),
        height_raw=height_raw,
        weight_kg=kg,
        weight_lb=lb,
        hometown=_clean(info.get("hometown")),
        shoots=hand if hand in ("L", "R") else None,
    )


def parse_roster(html: str) -> list[HcPlayer]:
    """Players and goalies of one team-roster page (staff left out). An empty roster (team not entered yet) → []."""
    data = _next_data(html)
    t = ((data.get("props") or {}).get("pageProps") or {}).get("teamDataTournament") or {}
    out = []
    for p in (t.get("TeamRoster_Players") or []) + (t.get("TeamRoster_Goalies") or []):
        if str(p.get("RosterType") or "player").lower() not in ("player", "goalie", ""):
            continue
        if (player := _player(p)) is not None:
            out.append(player)
    return out


class HockeyCanadaClient:
    """Polite reader of the stats pages (robots.txt checked by PoliteClient)."""

    def __init__(self, client: PoliteClient | None = None, delay_s: float = 3.0) -> None:
        self.client = client or PoliteClient(min_delay_s=delay_s)
        self.requests = 0

    def _get(self, url: str) -> str:
        self.requests += 1
        r = self.client.get(url)
        r.raise_for_status()
        return r.text

    def teams(self, event: HcEvent) -> list[HcTeam]:
        return parse_teams(self._get(event.schedule_url()))

    def roster(self, event: HcEvent, team: HcTeam) -> HcRoster:
        url = event.roster_url(team.team_id)
        return HcRoster(event, team, url, parse_roster(self._get(url)))

    def rosters(self, event: HcEvent, codes: set[str] | None = None) -> tuple[list[HcRoster], list[str]]:
        """All team rosters of the event (only teams with an IOC code in `codes` when given). Errors per team are
        returned, not raised, so one bad page does not stop the rest."""
        errors: list[str] = []
        try:
            teams = self.teams(event)
        except Exception as exc:  # network, robots, odd HTML: report and go on
            return [], [f"{event.key}: schedule: {exc}"[:300]]
        out = []
        for team in teams:
            if codes and team.code not in codes:
                continue
            try:
                out.append(self.roster(event, team))
            except Exception as exc:
                errors.append(f"{event.key} {team.code}: {exc}"[:300])
        return out, errors


def roster_label(url: str | None) -> str:
    """'Hockey Canada, Kader U18-Frauen-WM 2025' for a roster link (the tournament says how old the value is)."""
    for e in EVENTS:
        if f"/{e.path}/" in (url or ""):
            return f"Hockey Canada, Kader {e.label}"
    return "Hockey Canada"
