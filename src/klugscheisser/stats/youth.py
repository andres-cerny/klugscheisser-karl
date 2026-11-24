"""Youth history backfill (2026-10-28, one-off, can re-run): IIHF U18 Women's World Championship (top division)
per player and tournament, for the persons Elite Prospects has no row of that tournament for.

Source: the Finnish federation's results service (tulospalvelu.leijonat.fi, keyless JSON), level 134 "National
Team Women U18". It carries every top-division U18 WCh since 2017 (2017 CZE … 2026 Cape Breton) with all teams,
split into parts (Group A, Group B, Playoffs, Relegation/Placement): Karl sums the parts per player. Same endpoints
as the league tables (collectors/results/leijonat_stats.py). About 9 requests per tournament, ~90 in all, one every 2 s (the service answers 429 when
asked faster for long).
Not covered: Division I/II (Finland always plays the top division) and the 2015/2016 tournaments.

Rows: league "WJC-18 (W)", team "<Nation> U18" (both as Elite Prospects writes them), team_kind "national",
season = start year of the season (U18 WCh January 2025 -> 2024), source "leijonat_u18". A person is matched by
name and then checked by birth date (the feed's date of birth is used for that only, never stored) or nation.

CLI: `klugscheisser youth-backfill` (dry run, prints coverage) / `--yes` (stores).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)

SOURCE = "leijonat_u18"
LEAGUE = "WJC-18 (W)"
LEVEL = 134
SEASONS = range(2017, 2027)  # Leijonat season aliases (2017 = 2016/17)
WCH = re.compile(r"\bW+18 World Championship", re.I)
MAX_GAMES = (
    7  # a U18 WCh team plays at most 6 games (group, quarter-final, semi-final, final): more = double count
)
_END = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _stand(name: str) -> datetime | None:
    """End date of the tournament from the part's name ('… 4.-12.1.2025 Vantaa …')."""
    hits = _END.findall(name or "")
    if hits:
        d, mo, y = (int(x) for x in hits[-1])
        return datetime(y, mo, d, 20, 0, tzinfo=UTC)
    return None


def _main_part(parts: list[tuple[int, str]]) -> int:
    """The part linked as the source: Group A when named, else the lowest id."""
    return next((sid for sid, n in parts if re.search(r"group a\b", n, re.I)), min(sid for sid, _ in parts))


def _tournament_end(http, alias: int, parts: list[tuple[int, str]]) -> datetime:
    """Last day of the tournament: from the parts' names, else the last final game day of the parts (one request
    per part; 2021/22 was played in June 2022 and its names carry no date), else 30 June of the season."""
    from klugscheisser.collectors.results.leijonat_stats import API, stand

    named = [d for _, n in parts if (d := _stand(n))]
    if named:
        return max(named)
    days = []
    for sid, _ in parts:
        try:
            info = _retry(
                http, API + "/serie/helpers/getsubserie", {"season": alias, "subSerieId": sid, "teamid": 0}
            )
            if d := stand(info or {}):
                days.append(d)
        except Exception as exc:
            log.warning("youth: game days %s/%s failed: %s", alias, sid, exc)
    return max(days) if days else datetime(alias, 6, 30, 20, 0, tzinfo=UTC)


def _retry(http, url: str, params: dict, tries: int = 3):
    """The service sometimes answers 500 or times out under load: wait and try again (at most `tries` times)."""
    import time

    from klugscheisser.collectors.results.leijonat_stats import JSON

    for n in range(tries):
        try:
            return http.json(url, params, headers=JSON)
        except Exception as exc:
            if n == tries - 1:
                raise
            busy = getattr(getattr(exc, "response", None), "status_code", None) == 429
            time.sleep((60 if busy else 5) * (n + 1))  # 429 "too many requests": back off for a minute
    return None


def collect(http, seasons=SEASONS, skipped: list | None = None) -> list[dict]:
    """[{alias, parts: [(id, name)], players: {(team, name): row}, url, stand}] per U18 WCh. Season aliases whose
    sums look wrong are added to `skipped`."""
    skipped = skipped if skipped is not None else []
    from klugscheisser.collectors.results.leijonat_stats import API, FILTERS, PUBLIC_URL

    out = []
    for alias in seasons:
        try:
            subs = _retry(
                http,
                API + "/serie/helpers/getsubseries",
                {"season": alias, "levelid": LEVEL, "districtid": 0},
            )
        except Exception as exc:
            log.warning("youth: seasons %s failed: %s", alias, exc)
            continue
        parts = [
            (x["subSerieId"], x.get("subSerieName") or "")
            for x in subs or []
            if WCH.search(x.get("subSerieName") or "")
        ]
        if not parts:
            continue
        acc: dict[tuple[str, str], dict] = {}
        seen: set[tuple] = set()
        no_goalies = False
        for sid, _name in parts:
            base = {"season": alias, "type": 0, "teamid": 0, "nop": 0, "subSerieId": sid, **FILTERS}
            try:
                skaters = _retry(
                    http, API + "/helpers/getplayers", {**base, "filters[SortedBy]": "PlayerPoints"}
                )
            except Exception as exc:
                log.warning("youth: part %s/%s failed: %s", alias, sid, exc)
                acc = {}
                break  # a tournament with a missing part would undercount: skip it whole
            try:
                goalies = _retry(
                    http,
                    API + "/helpers/getgoalkeepers",
                    {
                        **base,
                        "gamesratio": 0,
                        "levelid": LEVEL,
                        "filters[SortedBy]": "GoalieSavesPerc",
                        "filters[PlayerName]": "",
                    },
                )
            except Exception as exc:  # skaters stay; that tournament's goalies are left out entirely
                log.warning("youth: goalies %s/%s failed: %s", alias, sid, exc)
                goalies, no_goalies = None, True
            sk = (skaters or {}).get("Players") or []
            mark = tuple(sorted((p.get("PlayerID"), p.get("PlayerGames"), p.get("PlayerPoints")) for p in sk))
            if mark in seen:  # older seasons: every part answers with the whole tournament; count it once
                continue
            seen.add(mark)
            add_part(acc, sk, (goalies or {}).get("Players") or [])
        if acc and max(a["games"] for a in acc.values()) > MAX_GAMES:
            log.warning("youth: %s sums to more than %s games per player: left out", alias, MAX_GAMES)
            skipped.append(alias)
            acc = {}
        if no_goalies:
            acc = {k: v for k, v in acc.items() if not v["goalie"]}
        if acc:
            out.append(
                {
                    "alias": alias,
                    "parts": parts,
                    "players": acc,
                    "url": PUBLIC_URL.format(season=alias, level=LEVEL, subserie=_main_part(parts)),
                    "stand": _tournament_end(http, alias, parts),
                    "no_goalies": no_goalies,
                }
            )
    return out


def _placeholder(p: dict) -> bool:
    """The feed's stand-in for an unknown scorer ('MISSING MISSING' in 2017/18 and 2021/22)."""
    return "MISSING" in {(p.get("FirstName") or "").upper(), (p.get("LastName") or "").upper()}


def add_part(acc: dict, skaters: list[dict], goalies: list[dict]) -> None:
    """Add one tournament part to the per-player sums (skaters by games/goals/assists/PIM, goalies by games
    played, goals against, saves, shutouts)."""
    from klugscheisser.collectors.results.base import title_name

    for p in skaters:
        if (p.get("RoleAbbrv") or "") == "MV":
            continue
        name = title_name(p.get("FirstName"), p.get("LastName"))
        if not name or not p.get("TeamAbbrv") or _placeholder(p):
            continue
        a = acc.setdefault(
            (p["TeamAbbrv"], name),
            {
                "name": name,
                "code": p["TeamAbbrv"],
                "goalie": False,
                "dob": p.get("DateOfBirth"),
                "yob": p.get("Yob"),
                "games": 0,
                "goals": 0,
                "assists": 0,
                "penalty_minutes": 0,
            },
        )
        if a["goalie"]:
            continue
        for k, f in (
            ("games", "PlayerGames"),
            ("goals", "PlayerGoals"),
            ("assists", "PlayerAssists"),
            ("penalty_minutes", "PlayerPenaltyMin"),
        ):
            a[k] += p.get(f) or 0
    for g in goalies:
        name = title_name(g.get("FirstName"), g.get("LastName"))
        if not name or not g.get("TeamAbbrv") or not g.get("GoaliePlayedGames") or _placeholder(g):
            continue
        a = acc.get((g["TeamAbbrv"], name))
        if a is None or not a["goalie"]:
            a = acc[(g["TeamAbbrv"], name)] = {
                "name": name,
                "code": g["TeamAbbrv"],
                "goalie": True,
                "dob": None,
                "yob": g.get("Yob"),
                "games": 0,
                "goals_against": 0,
                "saves": 0,
                "shutouts": 0,
            }
        a["games"] += g.get("GoaliePlayedGames") or 0
        a["goals_against"] += g.get("GoalieGoalsAgainst") or 0
        a["saves"] += g.get("GoalieSaves") or 0
        a["shutouts"] += g.get("GoalieZeroGames") or 0


def rows(tournaments: list[dict]) -> list:
    from klugscheisser.stats.current import STAT_FIELDS, TotalRow, nation_team

    out = []
    for t in tournaments:
        for a in t["players"].values():
            values = {k: v for k, v in a.items() if k in STAT_FIELDS}
            if a["goalie"]:
                shots = a["saves"] + a["goals_against"]
                if shots:
                    values["save_pct"] = round(a["saves"] / shots, 3)
            else:
                values["points"] = a["goals"] + a["assists"]
            out.append(
                TotalRow(
                    name=a["name"],
                    team=nation_team(a["code"]),
                    league=LEAGUE,
                    season=t["alias"] - 1,
                    phase="regular",
                    team_kind="national",
                    source=SOURCE,
                    source_url=t["url"],
                    stand=t["stand"],
                    goalie=a["goalie"],
                    values=values,
                    guard="nation",
                    birth_date=a.get("dob"),
                    birth_year=int(a["yob"]) if str(a.get("yob") or "").isdigit() else None,
                    fill_gap_only=True,
                )
            )
    return out


def coverage(s: Session, event_key: str | None = None) -> dict:
    """WCh roster players with youth history: EP youth/junior rows or U18 WCh rows (EP or this backfill)."""
    from klugscheisser import projects

    event_key = event_key or projects.current().main_event  # "wch-2026" in the ice hockey project
    roster = set(
        s.scalars(
            select(m.EventEntry.athlete_id)
            .join(m.Event, m.Event.id == m.EventEntry.event_id)
            .where(m.Event.key == event_key)
        )
    )

    def who(*where) -> set[int]:
        return set(
            s.scalars(select(m.SeasonStat.athlete_id).where(m.SeasonStat.athlete_id.in_(roster), *where))
        )

    u18 = m.SeasonStat.league.like("WJC-18%")
    ep = m.SeasonStat.source == "eliteprospects"
    no_ep = roster - who(ep)
    with_u18 = who(u18)
    return {
        "kader": len(roster),
        "mit_u18_wm": len(with_u18),
        "mit_u18_wm_von_elite_prospects": len(who(u18, ep)),
        "mit_u18_wm_aus_diesem_backfill": len(who(m.SeasonStat.source == SOURCE)),
        "mit_jugend_oder_u18": len(who((m.SeasonStat.team_kind == "youth") | u18)),
        "ohne_ep_daten": len(no_ep),
        "ohne_ep_daten_mit_u18_wm": len(no_ep & with_u18),
    }


def backfill(s: Session, http, store: bool = False, seasons=SEASONS) -> dict:
    from klugscheisser.stats.current import TotalsReport
    from klugscheisser.stats.current import store as store_rows

    before = coverage(s)
    skipped: list[int] = []
    tournaments = collect(http, seasons=seasons, skipped=skipped)
    report = TotalsReport()
    all_rows = rows(tournaments)
    if all_rows:
        with s.begin_nested():
            store_rows(s, all_rows, report)
    after = coverage(s)
    out = {
        "turniere": [
            f"{t['alias'] - 1}/{t['alias'] % 100:02d} ({len(t['parts'])} Teile, {len(t['players'])} Personen"
            + (", ohne Torhüterinnen: Quelle antwortet nicht)" if t.get("no_goalies") else ")")
            for t in tournaments
        ],
        "ausgelassen_wegen_summen": [f"{a - 1}/{a % 100:02d}" for a in skipped] or None,
        "anfragen": getattr(http, "requests", None),
        "vorher": before,
        "nachher": after,
        **report.out(),
    }
    if not store:
        s.rollback()
        out["dry_run"] = "nichts gespeichert (mit --yes speichern)"
    return out
