"""Career history for players without Elite Prospects data (2026-11-01).

Players added after the one-off EP import (04.10.2026), or without an EP id then, have only article stations
without seasons and current-season stats. This module:

- picks the players that matter first (`scope_ids`): who played in the stored games of the last days (all teams,
  incl. national teams) and the WCh 2026 rosters, then tiers;
- audits how thin their history is (`audit`, CLI `history-gaps`, read-only, free);
- backfills past seasons of the Swiss women's leagues from the SIHF's own season tables (`sihf_backfill`, CLI
  `sihf-history`, dry run by default) and derives career stations WITH seasons from them;
- lists the Elite Prospects pages still missing (`ep_url_list`), for a one-off fetch from Ondrej's Mac.

SIHF season tables (data.sihf.ch, keyless JSON, the same service as the current-season table): the old
`cache300?…&filterQuery=<season>/<league>` always answers the CURRENT season. Past seasons come from
`/statistic/api/cms/table?alias=player|goalkeeper&searchQuery=1//<league>&filterBy=Season[,Phase]
&filterQuery=<end year>[/<phase id>]`: Season = END year (2025 = 2024/25); without Phase the table sums every phase
of the season (but counts a "Playoff Overall" summary phase twice, so it is never used), with a phase id only that
phase. Seasons 2014/15 onwards (older ones silently answer the current season: the `filters[].selected` echo is
checked; an unknown phase id answers the first phase plus the phase list). Karl sums phase tables: playoffs =
the "Playoff Overall" table when the season has one, else the playoff rounds; everything else (regular season,
Masterround, ranking/placement, play-in, pre-playoffs, playout, league qualification, promotion games) -> "regular"
(`phase_kind`, `plan_phases`). Goalies come from the goalie table only (it also has their G/A/PIM).
Leagues: 42 SWHL A (PostFinance Women's League), 43 SWHL B, 44/45/46 SWHL C groups (2014/15-2016/17 only).
Rows go to `season_stats` (source "sihf", team = SIHF's team name) only for persons without Elite Prospects data,
after a namesake check (`check_person`); stored rows with equal values stay untouched, nothing is deleted.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)

TABLE_API = "https://data.sihf.ch/statistic/api/cms/table"
# league id -> (league as Elite Prospects writes it, public league page)
SIHF_LEAGUES = {
    42: ("SWHL A (W)", "https://www.sihf.ch/de/ligen/postfinance-womens-league/"),
    43: ("SWHL B (W)", "https://www.sihf.ch/de/ligen/frauenligen/"),
    44: (
        "SWHL C (W)",
        "https://www.sihf.ch/de/ligen/frauenligen/",
    ),  # SWHL C groups (OS, ZS, SR), 2014/15-2016/17
    45: ("SWHL C (W)", "https://www.sihf.ch/de/ligen/frauenligen/"),
    46: ("SWHL C (W)", "https://www.sihf.ch/de/ligen/frauenligen/"),
}
# leagues SIHF serves only for some seasons (END years; checked 01.11.2026): no request for the others
SIHF_LEAGUE_SEASONS = {44: {2015, 2016, 2017}, 45: {2015, 2016, 2017}, 46: {2015, 2016, 2017}}
FIRST_SEASON = 2014  # 2014/15: the oldest season the tables serve
SOURCE = "sihf"
STINT_SOURCE = "sihf"
WOMEN = "icehockey_women"  # the SIHF women's leagues: only persons of this sport are matched


def season_start(d: date) -> int:
    """Season (start year) of a date: from August the new season (2026-11-01 -> 2026)."""
    return d.year if d.month >= 8 else d.year - 1


# --- scope ---------------------------------------------------------------------------------------------------


def recent_game_ids(s: Session, days: int = 7, now: datetime | None = None) -> set[int]:
    """Persons named in a stored game (official result: line-ups, scorers, goalies) of the last `days` days."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    return set(
        s.scalars(
            select(m.ItemAthlete.athlete_id)
            .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
            .where(m.Item.source_type == "official_result", m.Item.published_at >= since)
        )
    )


def event_ids(s: Session, key: str) -> set[int]:
    return set(
        s.scalars(
            select(m.EventEntry.athlete_id)
            .join(m.Event, m.Event.id == m.EventEntry.event_id)
            .where(m.Event.key == key)
        )
    )


def women_sport_id(s: Session) -> int | None:
    return s.scalar(select(m.Sport.id).where(m.Sport.key == WOMEN))


def scope_ids(
    s: Session, scopes: list[str], tiers: list[int] | None = None, days: int = 7
) -> dict[int, list[str]]:
    """Athlete (role athlete) -> why she is in scope, in order of priority: the scopes as given
    ("recent-games", "wch-2026" or "event:<key>"), then the tiers. Ordered dict: first scope first."""
    out: dict[int, list[str]] = {}
    groups: list[tuple[str, set[int]]] = []
    for sc in scopes:
        sc = sc.strip()
        if not sc:
            continue
        if sc == "recent-games":
            groups.append((sc, recent_game_ids(s, days)))
        else:
            groups.append((sc, event_ids(s, sc.removeprefix("event:"))))
    women = women_sport_id(s)
    for t in tiers or []:
        q = select(m.Athlete.id).where(m.Athlete.tier == t, m.Athlete.sport_id == women)
        groups.append((f"tier{t}", set(s.scalars(q))))
    athletes = set(s.scalars(select(m.Athlete.id).where(m.Athlete.role == "athlete")))
    for label, ids in groups:
        for aid in sorted(ids & athletes):
            out.setdefault(aid, []).append(label)
    return out


# --- audit ---------------------------------------------------------------------------------------------------


@dataclass
class Gap:
    athlete_id: int
    name: str
    tier: int
    nation: str | None
    ep_id: str | None
    has_ep: bool
    stints: int
    stints_no_season: int
    stints_with_season: int
    past_stat_seasons: list[int]
    scopes: list[str] = field(default_factory=list)

    @property
    def flags(self) -> list[str]:
        out = []
        if not self.ep_id:
            out.append("keine_ep_id")
        elif not self.has_ep:
            out.append("ep_id_nie_geladen")
        if self.stints_no_season:
            out.append("stationen_ohne_saison")
        if not self.past_stat_seasons:
            out.append("keine_statistik_vor_dieser_saison")
        return out

    @property
    def thin(self) -> bool:
        """Thin history: no EP data and (no stats of earlier seasons or no station with seasons). Stations without
        seasons next to a seasoned list are no gap (the person page shows them as sources / further details)."""
        return not self.has_ep and (not self.past_stat_seasons or not self.stints_with_season)

    def line(self) -> str:
        seasons = (
            f"{min(self.past_stat_seasons)}–{max(self.past_stat_seasons)} ({len(self.past_stat_seasons)} Saisons)"
            if self.past_stat_seasons
            else "keine"
        )
        return (
            f"{self.athlete_id:5} {self.name} ({self.nation or '?'}, Tier {self.tier}) "
            f"[{', '.join(self.scopes)}] EP-ID {self.ep_id or '-'}; Stationen {self.stints} "
            f"(ohne Saison {self.stints_no_season}); frühere Statistik: {seasons}; {', '.join(self.flags) or 'ok'}"
        )


def audit(s: Session, scope: dict[int, list[str]], today: date | None = None) -> list[Gap]:
    current = season_start(today or datetime.now(UTC).date())
    ids = list(scope)
    if not ids:
        return []
    athletes = {a.id: a for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids)))}
    has_ep = set(s.scalars(select(m.EpProfile.athlete_id).where(m.EpProfile.athlete_id.in_(ids))))
    stints: dict[int, list[m.CareerStint]] = defaultdict(list)
    for st in s.scalars(
        select(m.CareerStint).where(m.CareerStint.athlete_id.in_(ids), m.CareerStint.status != "unconfirmed")
    ):
        stints[st.athlete_id].append(st)
    seasons: dict[int, set[int]] = defaultdict(set)
    for aid, season in s.execute(
        select(m.SeasonStat.athlete_id, m.SeasonStat.season).where(
            m.SeasonStat.athlete_id.in_(ids), m.SeasonStat.season < current
        )
    ):
        seasons[aid].add(season)
    out = []
    for aid in ids:
        a = athletes.get(aid)
        if a is None:
            continue
        sts = stints.get(aid, [])
        no_season = sum(1 for x in sts if x.season_from is None and x.season_to is None)
        out.append(
            Gap(
                athlete_id=aid,
                name=a.full_name,
                tier=a.tier,
                nation=a.nationality,
                ep_id=a.eliteprospects_id,
                has_ep=aid in has_ep,
                stints=len(sts),
                stints_no_season=no_season,
                stints_with_season=len(sts) - no_season,
                past_stat_seasons=sorted(seasons.get(aid, set())),
                scopes=scope[aid],
            )
        )
    return out


def summary(gaps: list[Gap]) -> dict:
    def count(rows: list[Gap]) -> dict:
        return {
            "personen": len(rows),
            "mit_ep_daten": sum(g.has_ep for g in rows),
            "ohne_ep_daten": sum(not g.has_ep for g in rows),
            "ohne_ep_id": sum(not g.ep_id for g in rows),
            "ep_id_nie_geladen": sum(bool(g.ep_id) and not g.has_ep for g in rows),
            "stationen_ohne_saison": sum(g.stints_no_season > 0 for g in rows),
            "keine_statistik_vor_dieser_saison": sum(not g.past_stat_seasons for g in rows),
            "ohne_ep_mit_frueherer_statistik": sum(not g.has_ep and bool(g.past_stat_seasons) for g in rows),
            "ohne_ep_mit_stationen_mit_saison": sum(not g.has_ep and g.stints_with_season > 0 for g in rows),
            "duenne_geschichte": sum(g.thin for g in rows),
        }

    def nations(rows: list[Gap]) -> dict:
        out: dict[str, str] = {}
        for nation in sorted({g.nation or "?" for g in rows}):
            rs = [g for g in rows if (g.nation or "?") == nation and not g.has_ep]
            if rs:
                with_past = sum(bool(g.past_stat_seasons) for g in rs)
                out[nation] = (
                    f"{len(rs)} ohne EP, {with_past} mit früherer Statistik, {sum(g.thin for g in rs)} dünn"
                )
        return out

    labels = list(dict.fromkeys(sc for g in gaps for sc in g.scopes))
    out = {"alle": count(gaps), **{lb: count([g for g in gaps if lb in g.scopes]) for lb in labels}}
    for lb in labels:
        if lb != "recent-games" and not lb.startswith("tier"):  # events: by nation (WCh rosters)
            out[f"{lb} nach Nation"] = nations([g for g in gaps if lb in g.scopes])
    return out


# --- SIHF past seasons -------------------------------------------------------------------------------------


def _selected(data: dict, alias: str) -> str | None:
    for f in data.get("filters") or []:
        if f.get("alias") == alias:
            return str(f.get("selected") or "")
    return None


def _phases(data: dict) -> list[tuple[str, str]]:
    for f in data.get("filters") or []:
        if f.get("alias") == "Phase":
            return [(str(e.get("alias")), str(e.get("name") or "").strip()) for e in f.get("entries") or []]
    return []


def table_url(alias: str, league_id: int, end_year: int, phase: str | None = None) -> str:
    """The table's own address (citable: the JSON the numbers come from), in the form of the 01.11.2026 load."""
    if phase is None:
        return (
            f"{TABLE_API}?alias={alias}&searchQuery=1//{league_id}&filterBy=Season"
            f"&filterQuery={end_year}&language=de"
        )
    return (
        f"{TABLE_API}?alias={alias}&searchQuery=1//{league_id}&language=de&filterBy=Season,Phase"
        f"&filterQuery={end_year}/{phase}"
    )


def _get(http, alias: str, league_id: int, end_year: int, phase: str) -> dict | None:
    """One phase table; None when SIHF answers another season (unknown values are no error there: an old season
    silently answers the current one). An unknown phase answers the first phase: checked by the caller."""
    params = {
        "alias": alias,
        "searchQuery": f"1//{league_id}",
        "filterBy": "Season,Phase",
        "filterQuery": f"{end_year}/{phase}",
        "language": "de",
    }
    data = http.json(TABLE_API, params)
    if _selected(data, "Season") != str(end_year):
        return None
    width = len(data.get("header") or [])  # an empty table has one row "Keine Spieler gefunden!"
    return {**data, "data": [r for r in data.get("data") or [] if isinstance(r, list) and len(r) >= width]}


def phase_kind(name: str) -> str:
    """'overall' for a playoff summary phase ("Playoff Overall"), 'playoffs' for playoff rounds (quarter/semi-final,
    final, bronze game / Kleiner Final), 'regular' for everything else (regular season, Phase 1, Masterround,
    ranking/placement rounds, play-in, pre-playoffs, playout, league qualification, promotion games): the
    convention of the 01.11.2026 load of five Swiss players."""
    n = name.lower()
    if "overall" in n:
        return "overall"
    if re.search(
        r"pre-?playoff|play-?out|play-?in|qualifi|aufstieg|abstieg|relegation|ranking|platzierung|master|regular|phase",
        n,
    ):
        return "regular"
    if re.search(r"playoff|final|bronce|bronze|medal", n):
        return "playoffs"
    return "regular"


def plan_phases(phases: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    """Which phase tables make "regular" and "playoffs". Playoffs = the "Playoff Overall" table when the season has
    one (2018/19 SWHL B, 2025/26 SWHL A: there the round tables repeat each other, e.g. Final = Overall), else the
    sum of the round tables. SIHF's no-phase season total is never used (it counts Overall and the rounds)."""
    kinds = [(pid, name, phase_kind(name)) for pid, name in phases]
    overall = [(pid, name) for pid, name, k in kinds if k == "overall"]
    return {
        "regular": [(pid, name) for pid, name, k in kinds if k == "regular"],
        "playoffs": overall[:1] or [(pid, name) for pid, name, k in kinds if k == "playoffs"],
    }


def _toi_s(v) -> int | None:
    hit = re.fullmatch(r"(\d+):(\d{2})", str(v or "").strip())
    return int(hit.group(1)) * 60 + int(hit.group(2)) if hit else None


def parse_goalies(data: dict) -> list[dict]:
    """Goalie rows of a phase table: games, goals against, GAA, seconds played, and the goalie's own goals,
    assists and penalty minutes (the goalie table has them; goalies are not in the skater table)."""
    from klugscheisser.collectors.results.sihf import _cols, _int, parse_goalie_table

    c = _cols(data)
    extra = {}
    for row in data.get("data") or []:
        team = row[c["team"]] if "team" in c else None
        name = row[c["player"]] if "player" in c else None
        if name and isinstance(team, dict):
            extra[(name, team.get("name"))] = {
                "goals": _int(row[c["goals"]]) if "goals" in c else None,
                "assists": _int(row[c["assists"]]) if "assists" in c else None,
                "penalty_minutes": _int(row[c["penaltyInMinutes"]]) if "penaltyInMinutes" in c else None,
            }
    out = []
    from klugscheisser.collectors.results.sihf import first_last

    by_name = {(first_last(n), t): v for (n, t), v in extra.items()}
    for r in parse_goalie_table(data):
        r.update(by_name.get((r["name"], r["team"]), {}))
        r["seconds"] = _toi_s(r.get("toi"))
        out.append(r)
    return out


def merge_phase(skaters: list[dict], goalies: list[dict]) -> list[dict]:
    """One phase: a goalie's row comes from the goalie table (if the skater table lists her too, that row is
    dropped: her games would count twice)."""
    keep = {(g["name"], g["team"]): g for g in goalies}
    return list(keep.values()) + [r for r in skaters if (r["name"], r["team"]) not in keep]


SUM_KEYS = ("games", "goals", "assists", "points", "penalty_minutes", "goals_against", "seconds")


def sum_phases(phase_rows: list[tuple[str, list[dict]]]) -> list[dict]:
    """Rows of several phase tables summed per (name, team); `url` = the first phase table she is in. A goalie's
    GAA is SIHF's own when one phase counts, else goals against per 60 minutes of the summed time."""
    acc: dict[tuple[str, str], dict] = {}
    for url, rows in phase_rows:
        for r in rows:
            key = (r["name"], r["team"])
            a = acc.get(key)
            if a is None:
                a = acc[key] = {"name": r["name"], "team": r["team"], "goalie": bool(r.get("goalie"))}
                a["url"] = r.get("url") or url
                a["_parts"] = 0
                a["_gaa"] = None
            a["_parts"] += 1
            a["_gaa"] = r.get("gaa")
            for k in SUM_KEYS:
                if r.get(k) is not None:
                    a[k] = (a.get(k) or 0) + r[k]
    out = []
    for a in acc.values():
        if not a.get("games"):
            continue
        if a["goalie"]:
            if a["_parts"] == 1 and a["_gaa"] is not None:
                a["gaa"] = a["_gaa"]
            elif a.get("seconds") and a.get("goals_against") is not None:
                a["gaa"] = round(a["goals_against"] * 3600 / a["seconds"], 2)
        if a.get("goals") is not None and a.get("assists") is not None:
            a["points"] = a["goals"] + a["assists"]
        out.append({k: v for k, v in a.items() if not k.startswith("_")})
    return out


def fetch_league_season(http, league_id: int, end_year: int) -> dict | None:
    """{"regular": [rows], "playoffs": [rows], "phases": [...]} of one league and season (SIHF END year), summed
    over the phase tables, or None when the season is not served. 1 + 2 x phases requests."""
    from klugscheisser.collectors.results.sihf import parse_player_table

    first = _get(
        http, "player", league_id, end_year, "x"
    )  # unknown phase: answers the first phase + the list
    if first is None:
        return None
    phases = _phases(first)
    plan = plan_phases(phases)
    cache = {("player", _selected(first, "Phase")): first}
    out: dict = {"regular": [], "playoffs": [], "phases": []}
    for kind in ("regular", "playoffs"):
        parts = []
        for pid, name in plan[kind]:
            out["phases"].append(f"{name} ({pid}) -> {kind}")
            tables = {}
            for alias in ("player", "goalkeeper"):
                data = cache.get((alias, pid)) or _get(http, alias, league_id, end_year, pid)
                if data is None or _selected(data, "Phase") != pid:
                    return None  # a missing part would undercount: leave the whole season out
                tables[alias] = data
            rows = merge_phase(parse_player_table(tables["player"]), parse_goalies(tables["goalkeeper"]))
            for r in rows:
                r["url"] = table_url("goalkeeper" if r.get("goalie") else "player", league_id, end_year, pid)
            parts.append((None, rows))
        summed = sum_phases(parts)
        out[kind] = summed
    return out


# --- matching: who is she? ------------------------------------------------------------------------------------


def _loose(name: str) -> str:
    """team_key without spaces and one-letter rests ("EVZ Women's Team" -> "evz", "EV Zug" -> "evzug")."""
    from klugscheisser.facts.teams import team_key

    return "".join(w for w in team_key(name or "").split() if len(w) > 1)


def same_team(a: str, b: str) -> bool:
    k1, k2 = _loose(a), _loose(b)
    return len(k1) >= 3 and len(k2) >= 3 and (k1 == k2 or k1 in k2 or k2 in k1)


SWISS_LEAGUE = re.compile(r"swhl|swiss|schweiz|sui|postfinance|women'?s league", re.I)


@dataclass
class Candidate:
    """One SIHF row matched to a Karl person."""

    athlete_id: int
    league: str
    league_id: int
    season: int  # start year
    phase: str
    row: dict


@dataclass
class HistoryReport:
    rows: int = 0
    matched: int = 0
    unmatched: int = 0
    with_ep: int = 0  # matched persons with Elite Prospects data: nothing written (EP is their history)
    stored: int = 0
    updated: int = 0
    unchanged: int = 0
    persons: set[int] = field(default_factory=set)
    doubtful: dict[int, str] = field(default_factory=dict)  # person -> why nothing was stored
    season_doubts: list[str] = field(default_factory=list)  # single seasons left out
    extra_rows: list[str] = field(default_factory=list)  # stored sihf rows this run would not write

    def out(self, s: Session) -> dict:
        names = {
            a.id: a.full_name for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(self.doubtful)))
        }
        return {
            "zeilen": self.rows,
            "zeilen_erkannt": self.matched,
            "nicht_erkannt": self.unmatched,
            "zeilen_mit_ep_daten_ausgelassen": self.with_ep,
            "personen_mit_neuer_geschichte": len(self.persons),
            "neu": self.stored,
            "geaendert": self.updated,
            "unveraendert": self.unchanged,
            "fraglich_nicht_gespeichert": len(self.doubtful),
            "fraglich": [f"{aid} {names.get(aid, '?')}: {why}" for aid, why in self.doubtful.items()] or None,
            "saisons_ausgelassen": self.season_doubts or None,
            "gespeichert_aber_nicht_mehr_gefunden": self.extra_rows or None,
        }


def _evidence(s: Session, aids: list[int]) -> dict[int, list[str]]:
    """Teams Karl knows for each person from other sources: career stations, season rows (not sihf history),
    current-team facts. Used to confirm that a SIHF namesake row is hers."""
    out: dict[int, list[str]] = defaultdict(list)
    if not aids:
        return out
    for aid, team in s.execute(
        select(m.CareerStint.athlete_id, m.CareerStint.team_name).where(
            m.CareerStint.athlete_id.in_(aids), m.CareerStint.source != STINT_SOURCE
        )
    ):
        out[aid].append(team)
    for aid, team in s.execute(
        select(m.SeasonStat.athlete_id, m.SeasonStat.team_name).where(
            m.SeasonStat.athlete_id.in_(aids), m.SeasonStat.source != SOURCE
        )
    ):
        out[aid].append(team)
    for aid, value in s.execute(
        select(m.Fact.athlete_id, m.Fact.value).where(
            m.Fact.athlete_id.in_(aids), m.Fact.kind == "current_team"
        )
    ):
        if isinstance(value, dict):
            out[aid].append(str(value.get("team") or value.get("text") or ""))
    return out


def _other_leagues(s: Session, aids: list[int]) -> dict[tuple[int, int], list[tuple[str, int]]]:
    """(person, season) -> [(league, games)] of season rows of other sources outside Switzerland."""
    out: dict[tuple[int, int], list[tuple[str, int]]] = defaultdict(list)
    if not aids:
        return out
    for aid, season, league, games, kind in s.execute(
        select(
            m.SeasonStat.athlete_id,
            m.SeasonStat.season,
            m.SeasonStat.league,
            m.SeasonStat.games,
            m.SeasonStat.team_kind,
        ).where(
            m.SeasonStat.athlete_id.in_(aids), m.SeasonStat.source != SOURCE, m.SeasonStat.phase == "regular"
        )
    ):
        if kind == "club" and not SWISS_LEAGUE.search(league or ""):
            out[(aid, season)].append((league, games or 0))
    return out


def _has_ep_data(s: Session, aids: list[int]) -> set[int]:
    if not aids:
        return set()
    return set(s.scalars(select(m.EpProfile.athlete_id).where(m.EpProfile.athlete_id.in_(aids)))) | set(
        s.scalars(
            select(m.SeasonStat.athlete_id).where(
                m.SeasonStat.athlete_id.in_(aids), m.SeasonStat.source == "eliteprospects"
            )
        )
    )


# words of a club name that say nothing alone; city words where national teams also play (a "Bern" in an article
# about an Austrian player may be the friendly in Bern) only count together with the club prefix ("SC Bern")
CLUB_FILLER = {
    "ladies",
    "lady",
    "hockey",
    "club",
    "team",
    "girls",
    "dames",
    "damen",
    "feminin",
    "the",
    "lions",
    "frauen",
    "women",
    "womens",
    "femminile",
    "de",
}
STRONG_PREFIX = {"hcl", "lhc", "evz", "zsc", "gck", "sct"}  # club abbreviations that name the club alone
CLUB_PREFIX = {"hc", "sc", "ev", "ec", "ehc", "cp", "hcl", "lhc", "evz", "zsc", "gck", "sct"}
VENUE_CITIES = {
    "bern",
    "zurich",
    "lausanne",
    "fribourg",
    "biel",
    "geneve",
    "genf",
    "basel",
    "luzern",
    "lugano",
}


def club_patterns(team: str) -> list[re.Pattern]:
    """Ways an article names this club: the name without filler words, or a distinctive single word
    ("Ambri", "Davos", "Weinfelden"; "ZSC"), but a venue city only with its prefix ("SC Bern", "HC Lugano")."""
    from klugscheisser.domain.names import normalize

    words = [w for w in normalize(team).replace("-", " ").split() if w not in CLUB_FILLER]
    out = []
    if len(words) > 1 or (words and words[0] not in VENUE_CITIES | CLUB_PREFIX):
        out.append(re.compile(r"\b" + r"\W+".join(map(re.escape, words)) + r"\b"))
    for i, w in enumerate(words):
        if w in STRONG_PREFIX or (w not in CLUB_PREFIX and len(w) >= 3 and w not in VENUE_CITIES):
            out.append(re.compile(rf"\b{re.escape(w)}\b"))
        elif w in VENUE_CITIES:
            pre = sorted({p for p in words[:i] if p in CLUB_PREFIX} | {"hc", "sc", "ev", "ladies team"})
            out.append(re.compile(rf"\b({'|'.join(map(re.escape, pre))})\W+{re.escape(w)}\b"))
            out.append(re.compile(rf"\b{re.escape(w)}\W+(ladies|women|frauen|damen)\b"))
    return out


def text_evidence(s: Session, athlete_id: int, teams: set[str], limit: int = 400) -> str | None:
    """A stored document about her (linked item) that names one of these clubs: the club found, else None."""
    from klugscheisser.domain.names import normalize

    pats = {team: club_patterns(team) for team in teams}
    rows = s.execute(
        select(m.Item.title, m.Item.text)
        .join(m.ItemAthlete, m.ItemAthlete.item_id == m.Item.id)
        .where(m.ItemAthlete.athlete_id == athlete_id)
        .order_by(m.Item.published_at.desc().nulls_last())
        .limit(limit)
    )
    for title, text in rows:
        body = normalize(f"{title or ''} {(text or '')[:20000]}")
        for team, ps in pats.items():
            if any(p.search(body) for p in ps):
                return team
    return None


def check_person(
    a: m.Athlete,
    cands: list[Candidate],
    known_teams: list[str],
    women: int | None = None,
    s: Session | None = None,
) -> str | None:
    """Why these SIHF rows may NOT be hers (None: store). Exact name is given; also needed:
    - a player (role athlete) of ice hockey;
    - an age that fits (11 to 45 in every season) when Karl knows her birth year;
    - goalie rows only for goalies and skater rows only for skaters, when Karl knows her position;
    - the club: one of her SIHF teams is a team Karl already knows for her (career, season rows, current team),
      or she is Swiss (a Swiss player of the same exact name in the Swiss women's leagues), or a stored document
      about her names one of these clubs (`text_evidence`). A foreign player without such a club link is a
      namesake risk: reported, not stored."""
    if a.role != "athlete":
        return f"Rolle {a.role}"
    if women is not None and a.sport_id != women:
        return "nicht Frauen-Eishockey"
    if a.birth_date:
        ages = sorted({c.season - a.birth_date.year for c in cands})
        if ages[0] < 11 or ages[-1] > 45:
            return f"Alter passt nicht (Jahrgang {a.birth_date.year}, Saisons {min(c.season for c in cands)}–{max(c.season for c in cands)})"
    if a.position:
        g = {bool(c.row.get("goalie")) for c in cands}
        if (a.position == "G") not in g or len(g) > 1:
            return f"Position {a.position}, SIHF {'Torhüterin' if True in g else 'Feldspielerin'}"
    teams = {c.row["team"] for c in cands}
    if any(same_team(t, k) for t in teams for k in known_teams if k):
        return None
    if a.nationality == "SUI":
        return None
    if s is not None and text_evidence(s, a.id, teams):
        return None
    return (
        f"kein bekannter Verein passt ({', '.join(sorted(teams))}; Nation {a.nationality or '?'})"
        if known_teams
        else f"Karl kennt keinen Verein, Nation {a.nationality or '?'} (SIHF: {', '.join(sorted(teams))})"
    )


def season_conflict(cands: list[Candidate], other: list[tuple[str, int]]) -> str | None:
    """A season she played fully abroad by another source (>= 12 games) cannot also be a full SIHF season."""
    sihf_games = sum(c.row.get("games") or 0 for c in cands if c.phase == "regular")
    big = [(lg, g) for lg, g in other if g >= 12]
    if sihf_games >= 12 and big:
        return f"{big[0][0]} {big[0][1]} Spiele"
    return None


def merge_groups(fetched: list[tuple[int, int, dict]]) -> list[tuple[int, int, dict]]:
    """League ids with the same league name in one season (the SWHL C groups 44/45/46, whose final round lists
    teams of several groups) -> one table per phase, rows of the same player and team summed."""
    groups: dict[tuple[str, int], list[tuple[int, dict]]] = defaultdict(list)
    for league_id, end_year, data in fetched:
        groups[(SIHF_LEAGUES[league_id][0], end_year)].append((league_id, data))
    out = []
    for (_league, end_year), parts in groups.items():
        if len(parts) == 1:
            out.append((parts[0][0], end_year, parts[0][1]))
            continue
        merged = {
            phase: sum_phases([(None, d[phase]) for _, d in parts]) for phase in ("regular", "playoffs")
        }
        out.append((parts[0][0], end_year, merged))
    return out


VALUE_KEYS = ("games", "goals", "assists", "points", "penalty_minutes", "goals_against", "gaa")


def store_history(
    s: Session, fetched: list[tuple[int, int, dict]], report: HistoryReport, stand: datetime
) -> None:
    """Upsert the matched rows (unique key person, season, league, team, phase, source). Rows already stored with
    the same values stay untouched (the 01.11.2026 load of five players); nothing is ever deleted: rows this run
    would not write are listed (`extra_rows`)."""
    from klugscheisser.domain.matcher import AthleteMatcher
    from klugscheisser.stats.current import match_name

    matcher = AthleteMatcher(s, jev_client=None)
    cache: dict[str, int | None] = {}
    per: dict[int, list[Candidate]] = defaultdict(list)
    for league_id, end_year, data in merge_groups(fetched):
        league = SIHF_LEAGUES[league_id][0]
        for phase in ("regular", "playoffs"):
            for r in data[phase]:
                report.rows += 1
                if r["name"] not in cache:
                    cache[r["name"]] = match_name(matcher, r["name"])
                aid = cache[r["name"]]
                if aid is None:
                    report.unmatched += 1
                    continue
                report.matched += 1
                per[aid].append(Candidate(aid, league, league_id, end_year - 1, phase, r))
    aids = list(per)
    with_ep = _has_ep_data(s, aids)
    known = _evidence(s, aids)
    abroad = _other_leagues(s, aids)
    athletes = {a.id: a for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(aids)))}
    women = women_sport_id(s)
    existing = {
        (r.athlete_id, r.season, r.league, r.team_name, r.phase): r
        for r in s.scalars(
            select(m.SeasonStat).where(m.SeasonStat.athlete_id.in_(aids), m.SeasonStat.source == SOURCE)
        )
    }
    written: set[tuple] = set()
    for aid, cands in per.items():
        if aid in with_ep:
            report.with_ep += len(cands)
            continue
        why = check_person(athletes[aid], cands, known.get(aid, []), women, s)
        if why:
            report.doubtful[aid] = why
            continue
        by_season: dict[int, list[Candidate]] = defaultdict(list)
        for c in cands:
            by_season[c.season].append(c)
        for season, cs in sorted(by_season.items()):
            clash = season_conflict(cs, abroad.get((aid, season), []))
            if clash:
                report.season_doubts.append(
                    f"{aid} {athletes[aid].full_name} {_season_label(season)}: SIHF "
                    f"{', '.join(sorted({c.row['team'] for c in cs}))}, aber {clash}"
                )
                continue
            for c in cs:
                values = {k: c.row.get(k) for k in VALUE_KEYS}
                if not c.row.get("goalie"):
                    values["goals_against"] = values["gaa"] = None
                key = (aid, c.season, c.league, c.row["team"][:200], c.phase)
                if key in written:  # two SIHF names for one person and team: keep the first, say so
                    report.season_doubts.append(
                        f"{aid} {athletes[aid].full_name} {_season_label(season)} {c.row['team']}: "
                        f"zweiter SIHF-Name {c.row['name']} nicht gespeichert"
                    )
                    continue
                written.add(key)
                old = existing.get(key)
                if old is not None:
                    if all(getattr(old, k) == values[k] for k in VALUE_KEYS):
                        report.unchanged += 1
                        continue
                    for k, v in values.items():
                        setattr(old, k, v)
                    old.source_url, old.fetched_at = c.row["url"], stand
                    report.updated += 1
                else:
                    s.add(
                        m.SeasonStat(
                            athlete_id=aid,
                            season=c.season,
                            league=c.league,
                            team_name=c.row["team"][:200],
                            team_kind="club",
                            phase=c.phase,
                            source=SOURCE,
                            source_url=c.row["url"],
                            fetched_at=stand,
                            **values,
                        )
                    )
                    report.stored += 1
                report.persons.add(aid)
    fetched_keys = {(SIHF_LEAGUES[lg][0], end - 1) for lg, end, _ in fetched}
    for key, r in existing.items():
        if (r.league, r.season) in fetched_keys and key not in written:
            report.extra_rows.append(
                f"{r.athlete_id} {_season_label(r.season)} {r.league} {r.team_name} {r.phase}"
            )
    s.flush()


# --- career stations with seasons from the stored SIHF rows --------------------------------------------------


@dataclass
class StintSpec:
    team: str
    league: str
    season_from: int
    season_to: int | None
    source_url: str


def _league_id(url: str | None) -> int | None:
    hit = re.search(r"searchQuery=1//(\d+)", url or "")
    return int(hit.group(1)) if hit else None


def stint_specs(rows: list[m.SeasonStat], current: int) -> list[StintSpec]:
    """Consecutive past seasons at the same team and league -> one station. It stays open (season_to None) when it
    reaches last season and the current-season SIHF table has her at the same club in the same league; a
    current-season club that continues no station starts a new open one."""
    past = [r for r in rows if r.season < current and r.team_name]
    now = [r for r in rows if r.season >= current and r.team_name]
    by_team: dict[tuple[str, str], dict[int, m.SeasonStat]] = defaultdict(dict)
    for r in sorted(past, key=lambda x: (x.season, x.phase != "regular")):
        by_team[(r.team_name, r.league)].setdefault(r.season, r)
    out: list[StintSpec] = []
    for (team, league), seasons in by_team.items():
        years = sorted(seasons)
        start = prev = years[0]
        for y in [*years[1:], None]:
            if y is not None and y == prev + 1:
                prev = y
                continue
            first = seasons[start]
            lg = _league_id(first.source_url)
            alias = "goalkeeper" if "alias=goalkeeper" in (first.source_url or "") else "player"
            url = table_url(alias, lg, start + 1) if lg else first.source_url
            out.append(StintSpec(team, league, start, prev, url))
            if y is not None:
                start = prev = y
    for r in now:
        cont = [
            sp
            for sp in out
            if sp.season_to == current - 1 and sp.league == r.league and same_team(sp.team, r.team_name)
        ]
        if cont:
            for sp in cont:
                sp.season_to = None
        elif not any(
            sp.season_to is None and sp.league == r.league and same_team(sp.team, r.team_name) for sp in out
        ):
            out.append(StintSpec(r.team_name, r.league, r.season, None, r.source_url))
    return sorted(out, key=lambda x: (x.season_from, x.team))


def _season_label(y: int) -> str:
    return f"{y}/{(y + 1) % 100:02d}"


def derive_stints(s: Session, athlete_ids: set[int] | None = None, today: date | None = None) -> dict:
    """Career stations (source "sihf", with seasons) from the stored SIHF season rows, for persons without Elite
    Prospects stations (EP's career is the list where it exists). Idempotent: stations already stored the same
    way stay untouched; this source's stations that no longer follow from the rows are removed."""
    current = season_start(today or datetime.now(UTC).date())
    q = select(m.SeasonStat).where(m.SeasonStat.source == SOURCE)
    if athlete_ids is not None:
        q = q.where(m.SeasonStat.athlete_id.in_(athlete_ids))
    per: dict[int, list[m.SeasonStat]] = defaultdict(list)
    for r in s.scalars(q):
        per[r.athlete_id].append(r)
    with_ep = set(
        s.scalars(
            select(m.CareerStint.athlete_id).where(
                m.CareerStint.athlete_id.in_(list(per)), m.CareerStint.source == "eliteprospects"
            )
        )
    )
    counts = {
        "personen": 0,
        "neu": 0,
        "geaendert": 0,
        "unveraendert": 0,
        "entfernt": 0,
        "uebersprungen_ep": 0,
    }
    for aid, rows in per.items():
        if aid in with_ep:
            counts["uebersprungen_ep"] += 1
            continue
        if not any(r.season < current for r in rows):
            continue  # current season only: the nightly table, no history to derive
        old = {
            (st.team_name, st.league, st.season_from): st
            for st in s.scalars(
                select(m.CareerStint).where(
                    m.CareerStint.athlete_id == aid, m.CareerStint.source == STINT_SOURCE
                )
            )
        }
        keep = set()
        for sp in stint_specs(rows, current):
            key = (sp.team[:200], sp.league, sp.season_from)
            keep.add(key)
            st = old.get(key)
            if st is not None:
                if st.season_to == sp.season_to:
                    counts["unveraendert"] += 1
                else:
                    st.season_to = sp.season_to
                    counts["geaendert"] += 1
                continue
            s.add(
                m.CareerStint(
                    athlete_id=aid,
                    team_name=sp.team[:200],
                    kind="club",
                    league=sp.league,
                    season_from=sp.season_from,
                    season_to=sp.season_to,
                    role="player",
                    source=STINT_SOURCE,
                    source_url=sp.source_url,
                    confidence=0.95,
                    status="confirmed",
                )
            )
            counts["neu"] += 1
        for key, st in old.items():
            if key not in keep:
                s.delete(st)
                counts["entfernt"] += 1
        counts["personen"] += 1
    s.flush()
    return counts


def sihf_backfill(
    s: Session,
    http,
    store: bool = False,
    seasons: list[int] | None = None,
    leagues: list[int] | None = None,
    scope: dict[int, list[str]] | None = None,
    today: date | None = None,
) -> dict:
    """Past seasons (start years) of the SIHF women's leagues -> season_stats + stations, for players without
    Elite Prospects data. Dry run unless `store` (everything is rolled back). `scope` only chooses whose
    before/after is reported; every matched person without EP data gets her rows."""
    today = today or datetime.now(UTC).date()
    current = season_start(today)
    seasons = [y for y in (seasons or range(FIRST_SEASON, current)) if FIRST_SEASON <= y < current]
    leagues = leagues or list(SIHF_LEAGUES)
    report = HistoryReport()
    before = summary(audit(s, scope, today)) if scope else None
    fetched, missing, errors = [], [], []
    for league_id in leagues:
        for y in seasons:
            if league_id in SIHF_LEAGUE_SEASONS and y + 1 not in SIHF_LEAGUE_SEASONS[league_id]:
                continue
            try:
                data = fetch_league_season(http, league_id, y + 1)
            except Exception as exc:
                log.warning("sihf history %s %s failed: %s", league_id, y, exc)
                errors.append(f"{league_id}/{y}: {exc}")
                continue
            if data is None:
                missing.append(f"{SIHF_LEAGUES[league_id][0]} ({league_id}) {_season_label(y)}")
                continue
            fetched.append((league_id, y + 1, data))
    store_history(s, fetched, report, datetime.now(UTC))
    stints = derive_stints(s, today=today)
    after = summary(audit(s, scope, today)) if scope else None
    out = {
        "tabellen": len(fetched),
        "nicht_geliefert": missing or None,
        "fehler": errors or None,
        "anfragen": getattr(http, "requests", None),
        **report.out(s),
        "stationen": stints,
        "vorher": before,
        "nachher": after,
    }
    if not store:
        s.rollback()
        out["dry_run"] = "nichts gespeichert (mit --yes speichern)"
    return out


# --- Elite Prospects pages still missing (for a one-off fetch from Ondrej's Mac) -----------------------------


def ep_url_list(s: Session, gaps: list[Gap]) -> list[dict]:
    """Persons without EP data, in scope order: the player page when the id is known, else a search link."""
    from urllib.parse import quote_plus

    out = []
    for g in gaps:
        if g.has_ep:
            continue
        if g.ep_id:
            url = f"https://www.eliteprospects.com/player/{g.ep_id}"
        else:
            url = f"https://www.eliteprospects.com/search/player?q={quote_plus(g.name)}"
        out.append(
            {"athlete_id": g.athlete_id, "name": g.name, "ep_id": g.ep_id, "url": url, "scopes": g.scopes}
        )
    return out
