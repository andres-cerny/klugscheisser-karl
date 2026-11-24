"""National-team games from the Finnish federation's results service (tulospalvelu.leijonat.fi, keyless JSON).

The service carries every game of the "National Team Women" level (id 133): all Euro Hockey Tour tournaments
(wherever they are played) and the World Championship groups. Requests need the header `accept: application/json`.

- Tournaments ("subseries") of a level and season (season alias 2027 = 2026/27):
  /serie/helpers/getsubseries?season=2027&levelid=133&districtid=0
- Games of one subseries: /helpers/getgames?dwl=0&season=2027&subSerieId={id}&teamid=0&districtid=0
  &gamedays=4&dog=YYYY-MM-DD&levelid=-1
  With a subSerieId and gamedays >= 2 the answer holds every game of the subseries whatever `dog` is
  (checked 2026-10-17 with dog before, inside and after the tournament, and empty); gamedays=0 returns only the
  `dog` day, gamedays=1 nothing. We send gamedays=4.
- Game report (goals with time, scorer, assists, situation; shootout; goalies; arena; spectators):
  /gamereport/getgamereportdata?gameid={id}&season=2027
- Game rosters (line-ups, goalie saves): /game/helpers/getsummary?gameid={id}&season=2027
- Public game page (the source link): /game?season=2027&gameid={id}&lang=en

Times: `GameTime` is Finnish local time (Europe/Helsinki) for every venue, whatever the `TimeZone` field says
(Lahti SWE–SUI 30.9.2026 12:30 = 11:30 Swiss time; Kloten SUI–SWE 20:00 = 19:00 Swiss time; Södertälje
FIN–SUI 15:00 = 14:00 Swiss time; all checked against the SIHF programme).

Never stored: dates of birth (rosters), the scorekeeper's contact data (`UsersInfo`), referees, player ids.
No robots.txt and no terms of use found (checked 2026-10-17). One request per second, project user agent.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import (
    TEAM_DE,
    KnownFinal,
    PoliteHttp,
    season_label,
    season_of,
    slug,
    title_name,
    wants,
)
from klugscheisser.interfaces import GameResult, Participant

log = logging.getLogger(__name__)

API = "https://tulospalvelu.leijonat.fi"
GAME_URL = API + "/game?season={season}&gameid={game_id}&lang=en"
JSON = {"accept": "application/json"}
TZ = ZoneInfo("Europe/Helsinki")  # GameTime is always Finnish local time
SOURCE = "Leijonat"
WOMEN_NATIONAL_TEAM = 133

# "EHT: Lidl Hockey Games 28.-31.10.2026 Södertälje, SWE", "Women's World Championship 6.-16.11.2026 Esbjerg,
# DEN Group A": the last date, then the city and the country code
SUBSERIE = re.compile(
    r"^(?P<title>.*?)\s*(?:\d{1,2}\.(?:\d{1,2}\.?)?\s*-\s*)?(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})"
    r"\s+(?P<city>[^,]+?),\s*(?P<country>[A-Z]{3})\b\s*(?P<rest>.*)$"
)
# goal types of the Finnish feed -> our situation labels
SITUATIONS = {
    "YV": "PP",  # ylivoima
    "YV2": "PP2",
    "AV": "SH",  # alivoima
    "AV2": "SH2",
    "TM": "EN",  # tyhjä maali: empty net
    "IM": "ohne Torhüterin",  # ilman maalivahtia: scored with the own goalie pulled
    "RL": "Penalty",  # rangaistuslaukaus
    "VL": "Penalty-Schiessen",  # voittolaukaus: game-deciding shootout goal
}


def team_name(code: str | None) -> str | None:
    return TEAM_DE.get(code or "", code)


def subserie_info(name: str, season: int, subserie_id: int | None = None) -> dict:
    """Competition and tournament container for a subseries name (stable German names and keys).

    EHT -> competition "Women's Euro Hockey Tour 2026/27, Lahti" + container "eht-2026-lahti";
    World Championship -> competition "IIHF Frauen-WM 2026" + container "wch-2026" (the seeded ones);
    anything else -> "Frauen-Länderspiele 2026/27: <name>" (container only when a city is known)."""
    label = season_label(season)
    mt = SUBSERIE.match(name or "")
    city = mt.group("city").strip() if mt else None
    country = mt.group("country") if mt else None
    year = int(mt.group("year")) if mt else season - 1
    where = f"{city} ({country})" if city and country else city
    if re.search(r"\bEHT\b|euro ?hockey ?tour", name or "", re.I):
        comp = f"Women's Euro Hockey Tour {label}" + (f", {city}" if city else "")
        key = f"eht-{label.replace('/', '-')}" + (f"-{slug(city)}" if city else "")
        tour = (
            {
                "prefix": "eht",
                "key": f"eht-{year}-{slug(city)}",
                "name": f"Women's Euro Hockey Tour, {city}",
                "city": where,
            }
            if city
            else None
        )
        return {"competition": comp, "competition_key": key, "tournament": tour}
    if re.search(r"world championship", name or "", re.I):
        return {
            "competition": f"IIHF Frauen-WM {year}",
            "competition_key": f"wch-{year}",
            "tournament": {
                "prefix": "wch",
                "key": f"wch-{year}",
                "name": f"IIHF Frauen-WM {year}",
                "city": None,
            },
        }
    comp = f"Frauen-Länderspiele {label}: {name}".strip()
    tour = (
        {
            "prefix": "nt",
            "key": f"nt-{year}-{slug(city)}",
            "name": (mt.group("title") or name).strip(),
            "city": where,
        }
        if city
        else None
    )
    return {
        "competition": comp,
        "competition_key": f"leijonat-{season}-{subserie_id}" if subserie_id else f"nt-{slug(comp)}"[:80],
        "tournament": tour,
    }


def _status(code) -> str:
    return {1: "live", 2: "final"}.get(int(code or 0), "scheduled")


def _suffix(finished_type) -> str:
    return {2: " n.V.", 3: " n.P."}.get(int(finished_type or 0), "")


def _start(day: str | None, clock: str | None) -> datetime | None:
    if not day:
        return None
    try:
        t = time.fromisoformat(clock) if clock else time(0, 0)
        return datetime.combine(date.fromisoformat(day), t, tzinfo=TZ)
    except ValueError:
        return None


def parse_games(data: list, season: int, info: dict | None = None) -> list[GameResult]:
    games = []
    for block in data or []:
        for g in block.get("Games") or []:
            if not g.get("GameID"):
                continue
            sub_name = g.get("SubSerieName") or ""
            meta = info or subserie_info(sub_name, season, g.get("SubSerieID"))
            home, away = g.get("HomeTeamAbbrv"), g.get("AwayTeamAbbrv")
            st = _status(g.get("GameStatus"))
            score = None
            if st != "scheduled" and g.get("HomeGoals") is not None:
                score = f"{g['HomeGoals']}:{g['AwayGoals']}" + (
                    _suffix(g.get("FinishedType")) if st == "final" else ""
                )
            ps = (g.get("PeriodSummary") or {}).get("PeriodGoals") or {}
            periods = [f"{h}:{a}" for h, a in zip(ps.get("Home") or [], ps.get("Away") or [], strict=False)]
            tour = meta.get("tournament")
            games.append(
                GameResult(
                    source=SOURCE,
                    sport="icehockey_women",
                    competition=meta["competition"],
                    external_id=str(g["GameID"]),
                    url=GAME_URL.format(season=season, game_id=g["GameID"]),
                    status=st,
                    starts_at=_start(g.get("GameDateDB"), g.get("GameTime")),
                    home=team_name(home),
                    away=team_name(away),
                    score=score,
                    period_scores=periods if st != "scheduled" else [],
                    venue=g.get("RinkName"),
                    raw={
                        "home_code": home,
                        "away_code": away,
                        "subserie_id": g.get("SubSerieID"),
                        "subserie": sub_name,
                        "competition_key": meta.get("competition_key"),
                        "competition_kind": "tournament",
                        "season": season_label(season),
                        "tournament": tour,
                        "city": tour.get("city") if tour else None,
                        "status_raw": g.get("GameStatus"),
                        "finished_type": g.get("FinishedType"),
                        "spectators": g.get("Spectator") or None,
                    },
                )
            )
    return games


def _log_name(value: str | None) -> str | None:
    """'GRILLFORS Tilde' / 'NYLEN-PERSSON Maja' (family name in capitals first) -> 'Tilde Grillfors'."""
    words = (value or "").split()
    if not words:
        return None
    family = [w for w in words if w.isupper() and len(w) > 1]
    given = [w for w in words if w not in family]
    if not family or not given:
        return " ".join(words).strip() or None
    return title_name(" ".join(given), " ".join(family))


def _clock(seconds) -> str | None:
    if seconds in (None, ""):
        return None
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def apply_details(game: GameResult, report: dict | None, summary: dict | None) -> GameResult:
    """Goals (time, scorer, assists, situation), goalies with saves, line-ups and arena of a played game.
    Only names are kept: no dates of birth, no player or contact ids."""
    parts: list[Participant] = []
    goals: list[dict] = []
    home_code, away_code = game.raw.get("home_code"), game.raw.get("away_code")
    team_codes: dict[int, str] = {}
    if report:
        g = (report.get("GamesUpdate") or [{}])[0]
        for side, code in (("HomeTeam", home_code), ("AwayTeam", away_code)):
            t = g.get(side) or {}
            if t.get("Id"):
                team_codes[t["Id"]] = t.get("Name") or code
        logs = sorted(
            (e for e in report.get("GameLogsUpdate") or [] if e.get("Type") == "Goal"),
            key=lambda e: (int(e.get("Period") or 0), int(e.get("GameTime") or 0)),
        )
        for e in logs:
            code = team_codes.get(e.get("TeamId"))
            team = team_name(code)
            sc = f"{e.get('HomeTeamGoals')}:{e.get('AwayTeamGoals')}"
            gtype = (e.get("GoalType") or "").strip().upper()
            shootout = gtype == "VL" or int(e.get("Period") or 0) >= 5
            clock = None if shootout else _clock(e.get("GameTime"))
            sit = SITUATIONS.get(gtype, gtype) if gtype else ""
            if shootout:
                sit = SITUATIONS["VL"]
            scorer = _log_name(e.get("ScorerName"))
            assists = [
                n for n in (_log_name(e.get("FirstAssistName")), _log_name(e.get("SecondAssistName"))) if n
            ]
            detail = ", ".join(x for x in (sc, clock) if x) + (f" ({sit})" if sit else "")
            if scorer:
                parts.append(Participant(scorer, "scorer", team, detail))
            parts += [Participant(n, "assist", team, detail) for n in assists]
            goals.append(
                {"score": sc, "time": clock or sit, "team": code, "scorer": scorer, "assists": assists}
            )
        arena = (g.get("Arena") or "").strip()
        tour = game.raw.get("tournament") or {}
        city = (tour.get("city") or "").split(" (")[0] or None
        if arena:
            game.venue = arena + (f", {city}" if city and city not in arena else "")
        if g.get("Spectators"):
            game.raw["spectators"] = g["Spectators"]
    stats: list[dict] = []
    if summary:
        for side, code in (("HomeTeamGameRoster", home_code), ("AwayTeamGameRoster", away_code)):
            team = team_name(code)
            for p in (summary.get(side) or {}).get("Players") or []:
                name = title_name(p.get("FirstName"), p.get("LastName"))
                if not name:
                    continue
                stats.append(player_line(name, team, p))
                if p.get("RoleAbbrv") == "MV" and p.get("Saves") not in (None, ""):
                    saves, ga = int(p.get("Saves") or 0), int(p.get("GA") or 0)
                    parts.append(
                        Participant(name, "goalie", team, f"{saves} Paraden bei {saves + ga} Schüssen")
                    )
                parts.append(Participant(name, "lineup", team, None))
    game.participants = parts
    game.raw = {**game.raw, "goals": goals, "detail": bool(report)}
    if stats:
        game.raw["player_stats"] = stats
    return game


def _num(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def player_line(name: str, team: str | None, p: dict) -> dict:
    """One player's line of the game roster (2026-10-25, for season totals): time on ice in seconds (0 = dressed,
    did not play), penalty minutes, plus/minus, and for goalies saves and goals against. No ids, no birth dates."""
    line = {
        "name": name,
        "team": team,
        "goalie": p.get("RoleAbbrv") == "MV",
        "toi": _num(p.get("Toi")),
        "pim": _num(p.get("PenMin")),
        "pm": _num(p.get("PlusMinus")),
    }
    if line["goalie"]:
        line["saves"], line["ga"] = _num(p.get("Saves")), _num(p.get("GA"))
    return line


class LeijonatCollector(KnownFinal):
    """Every game of the configured levels and season (scheduled, live, final). The season is small (about 20
    women's national-team games), so finals are listed whatever `since` is; details are fetched once per game."""

    name = "leijonat"
    source_label = SOURCE

    def __init__(
        self,
        season: int | None = None,
        level_ids: list[int] | tuple[int, ...] = (WOMEN_NATIONAL_TEAM,),
        skip: str | None = None,
        delay_s: float = 1.0,
        max_details: int = 20,
        http: PoliteHttp | None = None,
    ) -> None:
        # season alias of today unless given (2026-10 -> 2027 = 2026/27)
        self.season = season or season_of(datetime.now(TZ).date())
        self.level_ids, self.max_details = list(level_ids), max_details
        self.skip = re.compile(skip, re.I) if skip else None
        self.http = http or PoliteHttp(delay_s)
        self.known_final = set()

    def _json(self, path: str, params: dict):
        return self.http.json(API + path, params, headers=JSON)

    def subseries(self, level_id: int) -> list[dict]:
        data = self._json(
            "/serie/helpers/getsubseries", {"season": self.season, "levelid": level_id, "districtid": 0}
        )
        return [x for x in data or [] if x.get("subSerieId")]

    def games(self, subserie_id: int) -> list:
        return self._json(
            "/helpers/getgames",
            {
                "dwl": 0,
                "season": self.season,
                "subSerieId": subserie_id,
                "teamid": 0,
                "districtid": 0,
                "gamedays": 4,
                "dog": f"{self.season - 1}-08-01",
                "levelid": -1,
            },
        )

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        out: list[GameResult] = []
        details = 0
        for level in self.level_ids:
            try:
                subs = self.subseries(level)
            except Exception as exc:
                log.warning("leijonat: subseries of level %s failed: %s", level, exc)
                continue
            for sub in subs:
                name = sub.get("subSerieName") or ""
                if self.skip and self.skip.search(name):
                    continue  # covered by another collector (e.g. the WCh by IIHF)
                try:
                    games = parse_games(
                        self.games(sub["subSerieId"]),
                        self.season,
                        subserie_info(name, self.season, sub["subSerieId"]),
                    )
                except Exception as exc:
                    log.warning("leijonat: games of subseries %s failed: %s", sub.get("subSerieId"), exc)
                    continue
                for game in games:
                    if not wants(teams, game.home, game.away, game.raw["home_code"], game.raw["away_code"]):
                        continue
                    need = game.status == "live" or (
                        game.status == "final" and not self.is_known_final(game.external_id)
                    )
                    if need and details < self.max_details:
                        details += 1
                        try:
                            params = {"gameid": game.external_id, "season": self.season}
                            report = self._json("/gamereport/getgamereportdata", params)
                            summary = self._json("/game/helpers/getsummary", params)
                            apply_details(game, report, summary)
                        except Exception as exc:
                            log.warning("leijonat: game %s failed: %s", game.external_id, exc)
                    out.append(game)
        return out
