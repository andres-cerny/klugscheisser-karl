"""SIHF: PostFinance Women's League results from data.sihf.ch (undocumented JSON used by sihf.ch).

- Results list (latest round): /Statistic/api/cms/cache300?alias=results&searchQuery=42//42&filterQuery=2027/42
- Game detail: /Statistic/api/cms/gameoverview?alias=gameDetail&searchQuery={gameId}
- Public game page: https://www.sihf.ch/de/game-center/game/{gameId}

Never use sihf.ch/umbraco/ paths (robots.txt). Terms: personal, non-commercial use only.
Player records carry a birth year (`ageGroup`); we keep names, jersey numbers and game stats only.
Known gap: the results endpoint returns only the latest round (paging/date filters are ignored),
so upcoming fixtures are not available here.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from klugscheisser.collectors.results.base import KnownFinal, PoliteHttp, first_last, in_window, wants
from klugscheisser.interfaces import GameResult, Participant

log = logging.getLogger(__name__)

API = "https://data.sihf.ch/Statistic/api/cms"
GAME_URL = "https://www.sihf.ch/de/game-center/game/{game_id}"
COMPETITION = "PostFinance Women's League 2026/27"
DECISIONS = {"OT": " n.V.", "SO": " n.P.", "V": " n.V.", "P": " n.P.", "PEN": " n.P."}


def results_params(league_id: int, season: int) -> dict:
    return {
        "alias": "results",
        "searchQuery": f"{league_id}//{league_id}",
        "filterQuery": f"{season}/{league_id}",
        "language": "de",
    }


def _status(st: dict) -> str:
    pct = st.get("percent") or 0
    if pct >= 100 or st.get("name") == "Ende":
        return "final"
    return "live" if pct > 0 else "scheduled"


def parse_results(data: dict, competition: str = COMPETITION) -> list[GameResult]:
    """Rows of the results table -> GameResult without participants."""
    games = []
    for row in data.get("data") or []:
        cells = {c.get("type"): c for c in row if isinstance(c, dict) and c.get("type") != "team"}
        teams = [c for c in row if isinstance(c, dict) and c.get("type") == "team"]
        status = cells.get("gameStatus") or {}
        link = cells.get("linkToGameDetail") or {}
        if not link.get("gameId") or len(teams) != 2 or status.get("canceled"):
            continue
        res = cells.get("result") or {}
        per = cells.get("periods") or {}
        st = _status(status)
        decision = row[7] if len(row) > 7 and isinstance(row[7], str) else ""
        score = None
        if st != "scheduled" and res.get("homeTeam") not in (None, ""):
            score = f"{res['homeTeam']}:{res['awayTeam']}{DECISIONS.get(decision.strip().upper(), '')}"
        periods = [
            f"{h}:{a}" for h, a in zip(per.get("homeTeam") or [], per.get("awayTeam") or [], strict=False)
        ]
        start = status.get("startDateTime")
        games.append(
            GameResult(
                source="SIHF",
                sport="icehockey_women",
                competition=competition,
                external_id=str(link["gameId"]),
                url=GAME_URL.format(game_id=link["gameId"]),
                status=st,
                starts_at=datetime.fromisoformat(start) if start else None,
                home=teams[0]["name"],
                away=teams[1]["name"],
                score=score,
                period_scores=periods if st != "scheduled" else [],
                raw={
                    "home_code": teams[0].get("acronym"),
                    "away_code": teams[1].get("acronym"),
                    "end": status.get("endDateTime"),
                    "status_name": status.get("name"),
                },
            )
        )
    return games


def _ids(node) -> list[int]:
    if isinstance(node, list):
        return [x for x in node if isinstance(x, int)]
    if isinstance(node, dict):
        return [i for v in node.values() for i in _ids(v)]
    return []


def apply_detail(game: GameResult, detail: dict) -> GameResult:
    """Add scorers, assists, goalies and line-up from the game detail."""
    players = {p["id"]: p for p in detail.get("players") or []}
    det = detail.get("details") or {}
    team_names = {
        (det.get("homeTeam") or {}).get("id"): game.home,
        (det.get("awayTeam") or {}).get("id"): game.away,
    }

    def name(pid: int) -> str | None:
        p = players.get(pid)
        return first_last(p["fullName"]) if p else None

    parts: list[Participant] = []
    goals = []
    for period in (detail.get("summary") or {}).get("periods") or []:
        for g in period.get("goals") or []:
            team = team_names.get(g.get("teamId"))
            r = g.get("result") or {}
            sc = f"{r.get('home')}:{r.get('away')}"
            sit = (g.get("situation") or {}).get("code") or ""
            scorer = name(g.get("scorerLicenceNr"))
            assists = [n for n in (name(g.get("assist1LicenceNr")), name(g.get("assist2LicenceNr"))) if n]
            detail_txt = f"{sc}, {g.get('time')}" + (f" ({sit})" if sit and sit != "EQ" else "")
            if scorer:
                parts.append(Participant(scorer, "scorer", team, detail_txt))
            for a in assists:
                parts.append(Participant(a, "assist", team, detail_txt))
            goals.append(
                {"score": sc, "time": g.get("time"), "team": team, "scorer": scorer, "assists": assists}
            )
    for table in detail.get("stats") or []:
        if table.get("title") != "Goalie Stats":
            continue
        team = table.get("description")
        for row in table.get("data") or []:
            _no, pname, ga, played = row[0], row[1], row[2], row[3]
            if played in ("00:00", "", None):
                continue
            parts.append(Participant(first_last(pname), "goalie", team, f"{ga} Gegentore, {played} Min."))
    lineups = detail.get("lineUps") or {}
    for side, team in (("homeTeam", game.home), ("awayTeam", game.away)):
        for pid in _ids(lineups.get(side)):
            if n := name(pid):
                parts.append(Participant(n, "lineup", team))
    venue = det.get("venue") or {}
    game.participants = parts
    game.venue = ", ".join(filter(None, [venue.get("name"), venue.get("city")])) or None
    game.raw = {
        **game.raw,
        "end": detail.get("endDateTime") or game.raw.get("end"),
        "goals": goals,
        "spectators": venue.get("spectators"),
        "detail": True,
    }
    return game


class SihfCollector(KnownFinal):
    name = "sihf"
    source_label = "SIHF"

    def __init__(
        self,
        league_id: int = 42,
        season: int = 2027,
        competition: str = COMPETITION,
        delay_s: float = 1.0,
        max_details: int = 20,
        http: PoliteHttp | None = None,
    ) -> None:
        self.league_id, self.season, self.competition = league_id, season, competition
        self.max_details = max_details
        self.http = http or PoliteHttp(delay_s)
        self.known_final = set()

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        try:
            data = self.http.json(f"{API}/cache300", results_params(self.league_id, self.season))
            games = parse_results(data, self.competition)
        except Exception as exc:
            log.warning("sihf: results failed: %s", exc)
            return []
        out, details = [], 0
        for game in games:
            if not wants(teams, game.home, game.away, game.raw.get("home_code"), game.raw.get("away_code")):
                continue
            if not in_window(game, since):
                continue
            new_final = game.status == "final" and not self.is_known_final(game.external_id)
            if new_final and details < self.max_details:
                details += 1
                try:
                    detail = self.http.json(
                        f"{API}/gameoverview",
                        {"alias": "gameDetail", "searchQuery": game.external_id, "language": "de"},
                    )
                    apply_detail(game, detail)
                except Exception as exc:
                    log.warning("sihf: detail %s failed: %s", game.external_id, exc)
            out.append(game)
        return out


# --- season tables (2026-10-25): the league's own player and goalie totals ------------------------------------
# /Statistic/api/cms/cache300?alias=player|goalkeeper&searchQuery=42//42&filterQuery=2027/42 — the whole
# league in one answer (all teams, regular season by default). Goalies: GP, GA, GAA, minutes; no saves.
LEAGUE_PAGE = "https://www.sihf.ch/de/ligen/postfinance-womens-league/"


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def table_phase(data: dict) -> str:
    """'regular' or 'playoffs' from the table's selected Phase filter."""
    for f in data.get("filters") or []:
        if f.get("alias") != "Phase":
            continue
        sel = str(f.get("selected") or "")
        for e in f.get("entries") or []:
            if str(e.get("alias")) == sel:
                name = (e.get("name") or "").lower()
                return "playoffs" if "playoff" in name or "play-off" in name else "regular"
    return "regular"


def table_stand(data: dict) -> datetime | None:
    """The table's update time ('2026-10-25T20:17:21.1350000+01:00': 7 fraction digits, cut to none)."""
    raw = re.sub(r"\.\d+", "", str(data.get("updateDateTime") or ""), count=1)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.year > 2000 else None


def _cols(data: dict) -> dict[str, int]:
    return {h.get("alias"): i for i, h in enumerate(data.get("header") or [])}


def parse_player_table(data: dict) -> list[dict]:
    """Skater rows: name ('Lara Stalder'), team, games, goals, assists, points, penalty_minutes."""
    c, out = _cols(data), []
    for row in data.get("data") or []:
        team = row[c["team"]] if "team" in c else None
        name = row[c["player"]] if "player" in c else None
        if not name or not isinstance(team, dict) or not team.get("name"):
            continue
        out.append(
            {
                "name": first_last(name),
                "team": team["name"],
                "team_code": team.get("acronym"),
                "games": _int(row[c["gamesPlayed"]]),
                "goals": _int(row[c["goals"]]),
                "assists": _int(row[c["assists"]]),
                "points": _int(row[c["points"]]),
                "penalty_minutes": _int(row[c["pimTotal"]]) if "pimTotal" in c else None,
            }
        )
    return out


def parse_goalie_table(data: dict) -> list[dict]:
    """Goalie rows: games, goals_against, gaa, toi ('535:56'). The league average row is left out."""
    c, out = _cols(data), []
    for row in data.get("data") or []:
        team = row[c["team"]] if "team" in c else None
        name = row[c["player"]] if "player" in c else None
        if not name or not isinstance(team, dict) or team.get("id") == 100000 or name == "Durchschnitt":
            continue
        out.append(
            {
                "name": first_last(name),
                "team": team.get("name"),
                "team_code": team.get("acronym"),
                "goalie": True,
                "games": _int(row[c["gamesPlayed"]]),
                "goals_against": _int(row[c["goalsAgainst"]]),
                "gaa": _float(row[c["goalsAgainstAverage"]]),
                "toi": str(row[c["secondsPlayed"]]) if "secondsPlayed" in c else None,
            }
        )
    return out


def fetch_season_tables(
    http: PoliteHttp, league_id: int = 42, season: int = 2027
) -> tuple[list[dict], str, datetime | None]:
    """Both tables of one league and season (two requests). Returns (rows, phase, stand)."""
    rows: list[dict] = []
    phase, stand = "regular", None
    for alias, parse in (("player", parse_player_table), ("goalkeeper", parse_goalie_table)):
        data = http.json(
            f"{API}/cache300",
            {
                "alias": alias,
                "searchQuery": f"{league_id}//{league_id}",
                "filterQuery": f"{season}/{league_id}",
                "language": "de",
            },
        )
        rows += parse(data)
        phase = table_phase(data)
        if (t := table_stand(data)) and (stand is None or t > stand):
            stand = t
    return rows, phase, stand
