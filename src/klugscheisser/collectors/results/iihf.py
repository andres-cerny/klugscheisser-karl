"""IIHF: Women's World Championship 2026 (event 773) from realtime.iihf.com (undocumented JSON).

- All games of an event, with their realtime ids, status and score:
  https://realtime.iihf.com/gamestate/GetLatestScoresStateForEvents/{eventId}
  -> this solves the game -> realtime-id mapping (e.g. game 3 SUI-USA = 13547); no seed list needed.
- Game state (scorers, assists, goalies; `null` before the game):
  https://realtime.iihf.com/gamestate/GetLatestState/{realtimeId}
- Public game page (linked, never fetched: www.iihf.com sits behind Cloudflare bot protection):
  https://www.iihf.com/en/events/2026/ww/gamecenter/playbyplay/{siteId}/{gameNumber}-{home}-vs-{away}
  siteId = 75138 + gameNumber for WW 2026 (checked against 8 indexed game-centre URLs, games 1-27).

Past events: the event endpoint answers `[]` once an event is over, but GetLatestState still serves each old
game (with its EventId). A backfill event therefore names its realtime game-id range (`game_ids`), the
collector reads each id once and keeps the games of that event (`klugscheisser results-backfill`). The game page
id on iihf.com is unknown for those events: the link is the realtime game state the data comes from.

Terms: no reuse without written IIHF consent (see docs/sources.md); on for the private PoC only.
Athlete ids in the feed (GG_Athlete_Id) encode the birth date: they are never stored.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import (
    TEAM_DE,
    KnownFinal,
    PoliteHttp,
    in_window,
    ot_suffix,
    title_name,
    wants,
)
from klugscheisser.interfaces import GameResult, Participant

log = logging.getLogger(__name__)

API = "https://realtime.iihf.com/gamestate"
COMPETITION = "IIHF Frauen-WM 2026"
GAME_URL = "https://www.iihf.com/en/events/{path}/gamecenter/playbyplay/{site_id}/{number}-{home}-vs-{away}"

SITUATIONS = {"EQ": "", "PP1": "PP", "PP2": "PP2", "SH1": "SH", "SH2": "SH2", "EN": "EN", "PS": "Penalty"}


def team_name(code: str | None) -> str | None:
    """'SUI' -> 'Schweiz' (TEAM_DE in base.py, shared with the national-team collectors)."""
    return TEAM_DE.get(code or "", code)


def _status(raw: str | None) -> str:
    s = (raw or "").upper()
    if s in ("", "UPCOMING", "SCHEDULED", "NOT STARTED"):
        return "scheduled"
    if "FINAL" in s or "COMPLETED" in s or s in ("GAME OVER", "ENDED"):
        return "final"
    return "live"


STATE_URL = f"{API}/GetLatestState/{{game_id}}"
MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1
    )
}


class IihfEvent:
    """One IIHF event. Current events: `event_id` is enough (event endpoint). Past events (backfill): `game_ids`
    = (first, last) realtime game id and `tz` = local time zone of the venue (the game state has local times)."""

    def __init__(
        self,
        event_id: int = 773,
        path: str = "2026/ww",
        site_id_offset: int | None = 75138,
        competition: str = COMPETITION,
        competition_key: str | None = None,
        game_ids: tuple[int, int] | list[int] | None = None,
        tz: str | None = None,
        tournament: dict | None = None,
    ) -> None:
        self.event_id, self.path, self.site_id_offset, self.competition = (
            event_id,
            path,
            site_id_offset,
            competition,
        )
        self.competition_key = competition_key
        self.game_ids = tuple(game_ids) if game_ids else None
        self.tz = tz
        self.tournament = tournament  # {prefix, key, name, city}: container for sync_tournaments

    @classmethod
    def from_config(cls, cfg: dict) -> IihfEvent:
        return cls(
            event_id=int(cfg["event_id"]),
            path=cfg.get("path", ""),
            site_id_offset=cfg.get("site_id_offset"),
            competition=cfg.get("competition", COMPETITION),
            competition_key=cfg.get("competition_key"),
            game_ids=cfg.get("game_ids"),
            tz=cfg.get("tz"),
            tournament=cfg.get("tournament"),
        )

    def game_url(self, number: int, home: str | None, away: str | None, game_id: str | None = None) -> str:
        if self.site_id_offset is None:  # iihf.com page id unknown (past events): the realtime game state
            return STATE_URL.format(game_id=game_id)
        return GAME_URL.format(
            path=self.path,
            site_id=self.site_id_offset + number,
            number=number,
            home=(home or "tbd").lower(),
            away=(away or "tbd").lower(),
        )

    def extra_raw(self) -> dict:
        out: dict = {}
        if self.competition_key:
            out["competition_key"] = self.competition_key
        if self.tournament:
            out["tournament"] = self.tournament
        return out


def parse_event_games(data: list, event: IihfEvent) -> list[GameResult]:
    games = []
    for g in data or []:
        if not g.get("GameId"):
            continue
        home, away = (g.get("HomeTeam") or {}).get("TeamCode"), (g.get("GuestTeam") or {}).get("TeamCode")
        st = _status(g.get("Status"))
        number = int(g.get("GameNumber") or 0)
        start = g.get("GameDateTimeUTC")
        score = None
        if st != "scheduled":
            score = f"{g['HomeTeam'].get('Points', 0)}:{g['GuestTeam'].get('Points', 0)}"
        games.append(
            GameResult(
                source="IIHF",
                sport="icehockey_women",
                competition=event.competition,
                external_id=str(g["GameId"]),
                url=event.game_url(number, home, away, str(g["GameId"])),
                status=st,
                starts_at=datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None,
                home=team_name(home),
                away=team_name(away),
                score=score,
                venue=g.get("Venue"),
                raw={
                    "home_code": home,
                    "away_code": away,
                    "game_number": number,
                    "phase": g.get("PhaseId"),
                    "group": g.get("Group"),
                    "status_raw": g.get("Status"),
                    **event.extra_raw(),
                },
            )
        )
    return games


def _state_start(state: dict, tz: str | None) -> datetime | None:
    """EventDateTime {Value: '20250409', StartTime: {TimeFormat: '16:00'}} (local time of the venue) -> UTC."""
    dt = state.get("EventDateTime") or {}
    try:
        if dt.get("Value"):
            day = datetime.strptime(str(dt["Value"]), "%Y%m%d")
        else:
            day = datetime(int(dt["Year"]), MONTHS[str(dt["Month"]).upper()[:3]], int(dt["Day"]))
    except (KeyError, ValueError, TypeError):
        return None
    hh, mm = 0, 0
    t = ((dt.get("StartTime") or {}).get("TimeFormat") or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", t):
        hh, mm = (int(x) for x in t.split(":"))
    local = day.replace(hour=hh, minute=mm, tzinfo=ZoneInfo(tz) if tz else UTC)
    return local.astimezone(UTC)


def game_from_state(state: dict | None, event: IihfEvent) -> GameResult | None:
    """A past game from GetLatestState alone (backfill); None when the id belongs to another event."""
    if not state or str(state.get("EventId")) != str(event.event_id) or not state.get("GameId"):
        return None
    home = (state.get("HomeTeam") or {}).get("ShortTeamName")
    away = (state.get("AwayTeam") or {}).get("ShortTeamName")
    number = int(state.get("GameNumber") or 0)
    st = "final" if state.get("IsGameCompleted") else _status(state.get("Status"))
    game = GameResult(
        source="IIHF",
        sport="icehockey_women",
        competition=event.competition,
        external_id=str(state["GameId"]),
        url=event.game_url(number, home, away, str(state["GameId"])),
        status=st,
        starts_at=_state_start(state, event.tz),
        home=team_name(home),
        away=team_name(away),
        raw={
            "home_code": home,
            "away_code": away,
            "game_number": number,
            "status_raw": state.get("Status"),
            **event.extra_raw(),
        },
    )
    return apply_state(game, state)


def _ath_name(a: dict | None) -> str | None:
    if not a:
        return None
    return title_name(a.get("GivenName"), a.get("FamilyName")) or a.get("ReportingName")


def apply_state(game: GameResult, state: dict) -> GameResult:
    """Scorers, assists, goalies and period scores from GetLatestState."""
    periods = [p for p in state.get("Periods") or [] if p.get("PeriodCode") != "TOT"]
    parts: list[Participant] = []
    goals = []
    goalies: dict[tuple[str, str], str] = {}
    # 2026-11-04: everyone the sheet names (scorer, assists, goalies) with team code, number and position, so the
    # game watch can see players who dressed but are not on Karl's tournament roster (jobs/game_rosters.py)
    named: dict[tuple[str, str], dict] = {}

    def seen(a: dict | None, code: str | None, role: str) -> None:
        name = _ath_name(a)
        code = (a or {}).get("NOC_Code") or code
        if not name or not code:
            return
        row = named.setdefault((code, name), {"name": name, "team": code, "roles": []})
        if a.get("Number") not in (None, "") and str(a["Number"]).isdigit():
            row.setdefault("number", int(a["Number"]))
        pos = "G" if role == "goalie" else (a.get("Position") or "").upper()[:1]
        if pos in ("G", "D", "F"):
            row.setdefault("pos", pos)
        if role not in row["roles"]:
            row["roles"].append(role)

    for p in periods:
        for a in p.get("ScoringActions") or []:
            if not a.get("Scorer"):
                continue
            code = a.get("ExecutedByShortTeamName")
            team = team_name(code)
            ns = a.get("NewScore") or {}
            sc = f"{ns.get('Home')}:{ns.get('Away')}"
            sit = SITUATIONS.get(a.get("SituationType") or "", a.get("SituationType") or "")
            detail = f"{sc}, {a.get('TimeOfPlay')}" + (f" ({sit})" if sit else "")
            scorer = _ath_name(a["Scorer"])
            assists = [n for n in (_ath_name(a.get("Assistant1")), _ath_name(a.get("Assistant2"))) if n]
            parts.append(Participant(scorer, "scorer", team, detail))
            parts += [Participant(n, "assist", team, detail) for n in assists]
            seen(a["Scorer"], code, "scorer")
            for k in ("Assistant1", "Assistant2"):
                seen(a.get(k), code, "assist")
            goals.append(
                {
                    "score": sc,
                    "time": a.get("TimeOfPlay"),
                    "team": code,
                    "scorer": scorer,
                    "assists": assists,
                    "period": str(p.get("PeriodCode") or ""),  # "SO"/"GWS" = shootout (game watch)
                }
            )
        for a in p.get("Actions") or []:
            if a.get("Code") == "GOL_KPR_IN" and a.get("GoalTender"):
                code = a.get("ExecutedByShortTeamName") or a["GoalTender"].get("NOC_Code")
                goalies.setdefault((code, _ath_name(a["GoalTender"])), code)
                seen(a["GoalTender"], code, "goalie")
    # no per-goalie stats in this feed (and the SOG_H/SOG_A direction is undocumented): names only
    for code, name in goalies:
        parts.append(Participant(name, "goalie", team_name(code), None))
    score = state.get("CurrentScore") or {}
    game.participants = parts
    game.period_scores = [
        f"{p['Score'].get('Home')}:{p['Score'].get('Away')}" for p in periods if p.get("Score")
    ]
    codes = {str(p.get("PeriodCode") or p.get("Period") or "").upper() for p in periods}
    shootout = bool(codes & {"SO", "GWS", "PS", "PSS"})
    if score.get("Home") is not None:
        game.score = f"{score['Home']}:{score['Away']}{ot_suffix(len(periods), shootout=shootout)}"
    if state.get("IsGameCompleted"):
        game.status = "final"
    venue = (state.get("Venue") or {}).get("Name")
    game.venue = venue or game.venue
    end = ((state.get("EventDateTime") or {}).get("EndTime") or {}).get("TimeFormat")
    game.raw = {
        **game.raw,
        "goals": goals,
        "end_local": end,
        "spectators": state.get("Spectators"),
        "detail": True,
        "sheet_players": list(named.values()),
    }
    return game


class IihfCollector(KnownFinal):
    name = "iihf"
    source_label = "IIHF"

    def __init__(
        self,
        event: IihfEvent | list[IihfEvent] | None = None,
        delay_s: float = 1.0,
        max_details: int = 20,
        http: PoliteHttp | None = None,
    ) -> None:
        events = event if isinstance(event, list) else [event or IihfEvent()]
        self.events = events
        self.event = events[0]
        self.max_details = max_details
        self.http = http or PoliteHttp(delay_s)
        self.known_final = set()

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        out: list[GameResult] = []
        for event in self.events:
            if event.game_ids:
                out += self._collect_past(event, teams)
            else:
                out += self._collect_event(event, since, teams)
        return out

    def _collect_past(self, event: IihfEvent, teams: list[str] | None) -> list[GameResult]:
        """Backfill: every realtime game id of the range once (known final games are skipped without a request)."""
        first, last = event.game_ids
        out = []
        for gid in range(int(first), int(last) + 1):
            if self.is_known_final(str(gid)):
                continue
            try:
                game = game_from_state(self.http.json(STATE_URL.format(game_id=gid)), event)
            except Exception as exc:
                log.warning("iihf: game %s failed: %s", gid, exc)
                continue
            if game is None:
                continue
            if wants(teams, game.home, game.away, game.raw["home_code"], game.raw["away_code"]):
                out.append(game)
        return out

    def _collect_event(
        self, event: IihfEvent, since: datetime | None, teams: list[str] | None
    ) -> list[GameResult]:
        try:
            data = self.http.json(f"{API}/GetLatestScoresStateForEvents/{event.event_id}")
            games = parse_event_games(data, event)
        except Exception as exc:
            log.warning("iihf: event %s failed: %s", event.event_id, exc)
            return []
        out, details = [], 0
        for game in games:
            if not wants(teams, game.home, game.away, game.raw["home_code"], game.raw["away_code"]):
                continue
            if not in_window(game, since):
                continue
            need = game.status == "live" or (
                game.status == "final" and not self.is_known_final(game.external_id)
            )
            if need and details < self.max_details:
                details += 1
                try:
                    state = self.http.json(f"{API}/GetLatestState/{game.external_id}")
                    if state:
                        apply_state(game, state)
                except Exception as exc:
                    log.warning("iihf: game %s failed: %s", game.external_id, exc)
            out.append(game)
        return out
