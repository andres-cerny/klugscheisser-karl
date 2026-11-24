"""Club league season totals from the Finnish federation's results service (tulospalvelu.leijonat.fi, keyless JSON).

The same service Karl uses for the national-team sheets (collectors/results/leijonat.py) also serves each league's
own player tables (what the public "Pelaajat" / "Maalivahdit" tabs of the series page show):

- Subseries of a level: /serie/helpers/getsubseries?season=2027&levelid=73&districtid=0  (73 = Auroraliiga)
- Series info incl. the game days with status (2 = final, for the Stand): /serie/helpers/getsubserie
  ?season=2027&subSerieId={id}&teamid=0
- Skaters: /helpers/getplayers?season=2027&type=0&teamid=0&nop=0&subSerieId={id} (GP, G, A, PTS, PIM, +/-, time
  on ice; `TeamStats` splits a player who changed teams)
- Goalies: /helpers/getgoalkeepers?...&gamesratio=0&levelid=73 (games dressed / played, GA, saves, SV%, shutouts)

Header `accept: application/json`. Four requests per league and night. No robots.txt, no terms found
(checked 2026-10-17 and 2026-10-28). Dates of birth in the answer are used only to check a name match (never
stored, like the sheets collector).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import PoliteHttp, title_name

log = logging.getLogger(__name__)

API = "https://tulospalvelu.leijonat.fi"
JSON = {"accept": "application/json"}
TZ = ZoneInfo("Europe/Helsinki")
PUBLIC_URL = API + "/serie?season={season}&lid={level}&ssid={subserie}&lang=en"
FILTERS = {
    "filters[Games]": "",
    "filters[Strength]": "",
    "filters[Rookies]": 0,
    "filters[Total]": 0,
    "filters[Period]": "",
    "filters[SortOrder]": "DESC",
}
# team abbreviations of the feed -> the club names Elite Prospects uses (only a fallback: EP rows of the same
# players decide the name by vote in stats/current.py)
TEAM_NAMES = {
    "K-Espoo": "Kiekko-Espoo",
    "Team Kuortane": "Team Kuortane",
    "Kärpät": "Kärpät",
    "Pelicans": "Pelicans",
}


@dataclass(frozen=True)
class LeijonatLeague:
    level_id: int
    league: str  # as Elite Prospects writes it

    @classmethod
    def from_config(cls, items: list[dict]) -> list[LeijonatLeague]:
        return [cls(int(x["level_id"]), x["league"]) for x in items]


def _birth_year(p: dict) -> int | None:
    y = p.get("Yob") or (p.get("DateOfBirth") or "")[:4]
    return int(y) if str(y).isdigit() else None


def skater_rows(players: list[dict]) -> list[dict]:
    out = []
    for p in players:
        if (p.get("RoleAbbrv") or "") == "MV":  # goalies: from the goalie table
            continue
        name = title_name(p.get("FirstName"), p.get("LastName"))
        if not name:
            continue
        for t in p.get("TeamStats") or [p]:  # one row per team when a player changed teams
            out.append(
                {
                    "name": name,
                    "team": TEAM_NAMES.get(t.get("TeamAbbrv"), t.get("TeamAbbrv")),
                    "goalie": False,
                    "birth_year": _birth_year(p),
                    "games": t.get("PlayerGames"),
                    "goals": t.get("PlayerGoals"),
                    "assists": t.get("PlayerAssists"),
                    "points": t.get("PlayerPoints"),
                    "penalty_minutes": t.get("PlayerPenaltyMin"),
                    "plus_minus": t.get("PlayerPlusMinus"),
                }
            )
    return out


def goalie_rows(goalies: list[dict]) -> list[dict]:
    out = []
    for g in goalies:
        played = g.get("GoaliePlayedGames")
        name = title_name(g.get("FirstName"), g.get("LastName"))
        if not played or not name:
            continue
        ga, sv = g.get("GoalieGoalsAgainst"), g.get("GoalieSaves")
        row = {
            "name": name,
            "team": TEAM_NAMES.get(g.get("TeamAbbrv"), g.get("TeamAbbrv")),
            "goalie": True,
            "birth_year": _birth_year(g),
            "games": played,
            "goals_against": ga,
            "saves": sv,
            "shutouts": g.get("GoalieZeroGames"),
            "wins": g.get("GoalieWinGames"),
            "losses": g.get("GoalieLossGames"),
        }
        if ga is not None and sv is not None and ga + sv:
            row["save_pct"] = round(sv / (ga + sv), 3)
        if g.get("GoalieTimeOnIce"):  # seconds; without time on ice the feed's GAA is 0 and wrong
            row["gaa"] = round(ga * 3600 / g["GoalieTimeOnIce"], 2) if ga is not None else None
        out.append(row)
    return out


def stand(info: dict) -> datetime | None:
    """End of the last final game day (status 2) of the series."""
    days = [
        d.get("gameDate") for d in info.get("gameDays") or [] if d.get("status") == 2 and d.get("gameDate")
    ]
    if not days:
        return None
    return datetime.combine(date.fromisoformat(max(days)), time(23, 0), tzinfo=TZ)


def fetch_league(
    http: PoliteHttp, league: LeijonatLeague, season: int
) -> list[tuple[str, list[dict], datetime | None]]:
    """[(public url, rows, stand)] per subseries of the level (season alias 2027 = 2026/27). Raises on errors."""
    subs = http.json(
        API + "/serie/helpers/getsubseries",
        {"season": season, "levelid": league.level_id, "districtid": 0},
        headers=JSON,
    )
    out = []
    for sub in subs or []:
        sid = sub.get("subSerieId")
        if not sid:
            continue
        info = http.json(
            API + "/serie/helpers/getsubserie",
            {"season": season, "subSerieId": sid, "teamid": 0},
            headers=JSON,
        )
        if not (info or {}).get("playerStatistics"):
            continue
        base = {"season": season, "type": 0, "teamid": 0, "nop": 0, "subSerieId": sid, **FILTERS}
        players = http.json(
            API + "/helpers/getplayers", {**base, "filters[SortedBy]": "PlayerPoints"}, headers=JSON
        )
        rows = skater_rows((players or {}).get("Players") or [])
        if info.get("goalieStatistics"):
            goalies = http.json(
                API + "/helpers/getgoalkeepers",
                {
                    **base,
                    "gamesratio": 0,
                    "levelid": league.level_id,
                    "filters[SortedBy]": "GoalieSavesPerc",
                    "filters[PlayerName]": "",
                },
                headers=JSON,
            )
            rows += goalie_rows((goalies or {}).get("Players") or [])
        url = PUBLIC_URL.format(season=season, level=league.level_id, subserie=sid)
        out.append((url, rows, stand(info)))
    return out
