"""SSL (Svenska Superligan, Sweden's top men's league): season stats of every player, checked 2026-11-24.

Source: the league's own website www.ssl.se (a JS app) reads its data from its public JSON API under
`https://www.ssl.se/api/` (no key, no login). www.ssl.se has no robots.txt (the app answers /robots.txt with
its HTML page), so nothing is disallowed. Calls used:

- `statistics-v2/layout-info?statisticsType=player&currentLocale=sv` -> every season/series/game type
  ("ssgt") with its uuid; "SSLHerr" + "regular" = men's regular season, from 2016/17 (2018/19 is missing).
- `statistics-v2/stats-info/players_summary?ssgtUuid=<uuid>&provider=ibis&count=1000` -> one row per player
  and team: GP, G, A, TP, PIM, plus name, uuid, birth date, nationality (ISO-2 or "N/A"), position, team.
- `statistics-v2/stats-info/goalkeepers_summary?…` -> GP, GA, GAA, SVSPerc of the goalkeepers.

So one season costs 2 requests; all seasons ~20. Player page (source link): /athlete-profile/<uuid>.
Not available: playoff stats (the playoff ssgts answer an empty list or HTTP 500), per-game logs (not used).

Not used: stats.innebandy.se (the federation's stats site): its robots.txt disallows AI agents by name
(ClaudeBot, Claude-User, GPTBot, PerplexityBot …); statistik.innebandy.se (old site) does not answer from this
server (timeout after 90 s, 2026-11-24).
"""

from __future__ import annotations

import logging
from datetime import date

from klugscheisser.collectors.floorball.common import CappedHttp, RequestCapReached
from klugscheisser.collectors.floorball.leagues import ISO2_TO_IOC, LeaguePlayer, LeagueRow

log = logging.getLogger(__name__)

API = "https://www.ssl.se/api/"
PLAYER_URL = "https://www.ssl.se/athlete-profile/{uuid}"
SOURCE = "ssl"  # SeasonStat.source
LEAGUE = "SSL"  # SeasonStat.league / CareerStint.league
COUNTRY = "SWE"
SERIES = "SSLHerr"


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _date(v) -> date | None:
    """ISO date; 1 January is a placeholder at the SSL (e.g. "2025-01-01" for a player born 1998): None."""
    try:
        d = date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None
    return None if (d.month, d.day) == (1, 1) else d


def parse_seasons(layout: list | dict) -> list[tuple[int, str, str]]:
    """(season start year, game type code, ssgt uuid) of the men's SSL, newest first."""
    items = layout if isinstance(layout, list) else [layout]
    out = set()
    for item in items:
        ssgts = list(item.get("originalSeasonSeriesGameTypeTeams") or [])
        if item.get("ssgtDefaultValue"):
            ssgts.append(item["ssgtDefaultValue"])
        for x in ssgts:
            if (x.get("series") or {}).get("code") != SERIES:
                continue
            season = _int((x.get("season") or {}).get("code"))
            gtype = (x.get("gameType") or {}).get("code")
            if season and gtype and x.get("ssgtUuid"):
                out.add((season, gtype, x["ssgtUuid"]))
    return sorted(out, reverse=True)


def _stats(data) -> list[dict]:
    if isinstance(data, list):
        data = data[0] if data else {}
    return list((data or {}).get("stats") or [])


def parse_stats(
    data, season: int, players: dict[str, LeaguePlayer], *, goalies: bool = False, phase: str = "regular"
) -> int:
    """Add the rows of one players_summary / goalkeepers_summary answer to `players` (by uuid). Returns rows."""
    n = 0
    for s in _stats(data):
        info = s.get("info") or {}
        uuid, name = info.get("uuid"), (info.get("fullName") or "").strip()
        team = ((info.get("team") or {}).get("name") or "").strip()
        if not uuid or not name or not team:
            continue
        p = players.get(uuid)
        if p is None:
            nat = str(info.get("nationality") or "").strip().upper()
            p = players[uuid] = LeaguePlayer(
                key=uuid,
                name=name,
                url=PLAYER_URL.format(uuid=uuid),
                birth_date=_date(info.get("birthDate")),
                nationality=ISO2_TO_IOC.get(nat),
                position=(info.get("position") or None) if info.get("position") in ("G", "D", "F") else None,
            )
        if goalies:
            p.position = "G"
        row = next((r for r in p.rows if (r.season, r.team, r.phase) == (season, team, phase)), None)
        if row is None:
            row = LeagueRow(season=season, team=team, phase=phase)
            p.rows.append(row)
        if goalies:
            row.games = row.games if row.games is not None else _int(s.get("GP"))
            row.goals_against = _int(s.get("GA"))
            pct = _float(s.get("SVSPerc"))
            row.save_pct = round(pct / 100, 4) if pct is not None and pct > 1 else pct
            gaa = _float(s.get("GAA"))
            row.gaa = gaa if gaa else None  # the API writes 0 when it has no value
        else:
            row.games = _int(s.get("GP"))
            row.goals, row.assists, row.points = _int(s.get("G")), _int(s.get("A")), _int(s.get("TP"))
            row.penalty_minutes = _int(s.get("PIM"))
        n += 1
    return n


def fetch_players(
    http: CappedHttp, seasons: tuple[int, ...] | None = None, today: date | None = None
) -> tuple[list[LeaguePlayer], dict]:
    """All SSL men's players of the regular seasons (all published, or only `seasons`). Returns
    (players, report). Stops quietly at the request cap; one failing season does not stop the others."""
    report: dict = {"saisons": 0, "zeilen": 0}
    layout = http.get(
        API + "statistics-v2/layout-info", params={"statisticsType": "player", "currentLocale": "sv"}
    )
    players: dict[str, LeaguePlayer] = {}
    for season, gtype, uuid in parse_seasons(layout.json()):
        if gtype != "regular" or (seasons and season not in seasons):
            continue
        try:
            for module, goalies in (("players_summary", False), ("goalkeepers_summary", True)):
                resp = http.get(
                    API + f"statistics-v2/stats-info/{module}",
                    params={"ssgtUuid": uuid, "provider": "ibis", "count": 1000},
                )
                report["zeilen"] += parse_stats(resp.json(), season, players, goalies=goalies)
            report["saisons"] += 1
        except RequestCapReached as exc:
            report["gestoppt"] = str(exc)
            break
        except Exception as exc:
            log.warning("ssl: season %s failed: %s", season, exc)
            report.setdefault("fehler", []).append(f"{season}: {str(exc)[:80]}")
    report["spieler"] = len(players)
    return list(players.values()), report
