"""Current season totals (2026-10-25): newer numbers than the one-off Elite Prospects import of 04.10.2026.

Two free sources, both written to `season_stats` next to the EP rows (EP rows are never touched):

- source "sihf": the PostFinance Women's League's own season tables on data.sihf.ch (skaters: GP, G, A, PTS, PIM;
  goalies: GP, GA, GAA, minutes; no saves). Two requests per run. The stored SIHF game sheets are not summed for
  the league: the results feed only ever lists the latest round, so Karl holds 14 of the ~33 games played by
  25.10.2026 (sheets only since 26.09.) and a sum would undercount. They serve as a cross-check (`check_sihf`).
- source "leijonat": national-team totals counted from the stored Leijonat game sheets (Euro Hockey Tour, World
  Championship): GP from the line-ups, G/A from the goals (shootout goals left out), goalies GP/GA/saves. Only
  for teams whose every stored final game of that tournament series has a sheet; no PIM in the sheets.

`fetched_at` of each row is its "Stand": the table's update time (SIHF) or the end of the last game counted.
Team names follow the EP rows of the same players (vote), so EP and current rows describe the same station.

Read side (`stats.queries.prefer_current`): where a current row and an EP row cover the same person, season,
league and phase, the current row wins (unless it has no games played and EP has), the EP row is left out (no
double counting), and the winning row names the EP values it replaces.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)

SOURCE_SIHF = "sihf"
SOURCE_COLLEGE = "college"  # the college's own stats page (Sidearm), NCAA
SOURCE_LEIJONAT = "leijonat"  # also "iihf", "wiki": the platform of the counted sheets is the row's source
SOURCE_SWEHOCKEY = "swehockey"  # SDHL / NDHL tables on stats.swehockey.se
SOURCE_LEIJONAT_LIIGA = "leijonat_liiga"  # Finnish league tables (Auroraliiga) of tulospalvelu.leijonat.fi
SOURCE_JIHF = "jihf"  # Japan women's league, counted from the JIHF game sheets
SIHF_LEAGUE = "SWHL A (W)"  # the league as Elite Prospects writes it (LEAGUE_ALIASES in stats/search.py)
TZ = ZoneInfo("Europe/Zurich")

# competition names of the Leijonat collector -> league as Elite Prospects writes it
SHEET_LEAGUES = (
    (re.compile(r"euro hockey tour", re.I), "EHT (W)"),
    (re.compile(r"division i\b|division 1|div\.? i\b", re.I), "WC D1 (W)"),
    (re.compile(r"world championship|weltmeisterschaft|frauen-wm|\bwm\b", re.I), "WC (W)"),
    (re.compile(r"olympi", re.I), "OG (W)"),
)
# shootout goals are not goals in the stats: Leijonat "Penalty-Schiessen", IIHF "GWS" (game-winning shot)
SHOOTOUT = re.compile(r"Penalty-Schiessen|\bGWS\b")
# platforms whose stored sheets are counted per tournament (national teams). Leijonat has line-ups (GP, PIM);
# IIHF realtime and the Wikipedia game boxes only scorers, assists and goalies: GP stays empty for skaters
SHEET_PLATFORMS = ("leijonat", "iihf", "wiki")
# German team names of the sheets -> EP's English names (only when no EP row of a player gives the name)
NATION_EN = {
    "Schweiz": "Switzerland",
    "Schweden": "Sweden",
    "Finnland": "Finland",
    "Tschechien": "Czechia",
    "Deutschland": "Germany",
    "Kanada": "Canada",
    "USA": "USA",
    "Japan": "Japan",
    "Dänemark": "Denmark",
    "Ungarn": "Hungary",
    "Slowakei": "Slovakia",
    "Frankreich": "France",
    "Norwegen": "Norway",
    "Österreich": "Austria",
    "China": "China",
    "Italien": "Italy",
}
STAT_FIELDS = (
    "games",
    "goals",
    "assists",
    "points",
    "penalty_minutes",
    "plus_minus",
    "goals_against",
    "saves",
    "save_pct",
    "gaa",
    "toi",
    "wins",
    "losses",
    "ties",
    "shutouts",
)


@dataclass
class TotalRow:
    name: str
    team: str
    league: str
    season: int  # start year (2026 = 2026/27)
    phase: str
    team_kind: str
    source: str
    source_url: str
    stand: datetime
    goalie: bool = False
    values: dict = field(default_factory=dict)
    # club league tables of other countries (swehockey, Leijonat leagues): check each name match against the
    # person's known club, position and birth year before storing (`plausible`)
    guard: str | None = None  # "club" (plausible) or "nation" (plausible_nation)
    birth_year: int | None = None  # from the source, only to check the match (never stored)
    birth_date: str | None = None  # ISO date from the source, only to check the match (never stored)
    fill_gap_only: bool = (
        False  # youth backfill: only where Elite Prospects has no row of this season and league
    )


@dataclass
class TotalsReport:
    rows: int = 0
    matched: int = 0
    stored: int = 0
    unmatched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    doubtful: list[str] = field(default_factory=list)  # name found, but club/position/birth year disagree
    unconfirmed: list[str] = field(default_factory=list)  # stored, but Karl knows no club for the person
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def out(self) -> dict:
        return {
            "zeilen": self.rows,
            "personen_erkannt": self.matched,
            "gespeichert": self.stored,
            "pro_quelle": self.per_source,
            "nicht_erkannt": len(self.unmatched),
            "nicht_erkannt_beispiele": self.unmatched[:10],
            "ausgelassen": self.skipped or None,
            "zweifel_nicht_gespeichert": len(self.doubtful),
            "zweifel_beispiele": self.doubtful[:40] or None,
            "ohne_vereinsbeleg_gespeichert": len(self.unconfirmed),
            "ohne_vereinsbeleg_beispiele": self.unconfirmed[:60] or None,
            "fehler": self.errors or None,
        }


# --- SIHF league tables --------------------------------------------------------------------------------------


def sihf_rows(table: list[dict], phase: str, stand: datetime, season_alias: int) -> list[TotalRow]:
    from klugscheisser.collectors.results.sihf import LEAGUE_PAGE

    out = []
    for r in table:
        values = {k: r.get(k) for k in STAT_FIELDS if r.get(k) is not None}
        out.append(
            TotalRow(
                name=r["name"],
                team=r["team"],
                league=SIHF_LEAGUE,
                season=season_alias - 1,
                phase=phase,
                team_kind="club",
                source=SOURCE_SIHF,
                source_url=LEAGUE_PAGE,
                stand=stand,
                goalie=bool(r.get("goalie")),
                values=values,
            )
        )
    return out


# --- national-team totals from the stored game sheets ---------------------------------------------------------


def sheet_league(competition: str | None) -> str | None:
    for rx, league in SHEET_LEAGUES:
        if competition and rx.search(competition):
            return league
    return None


_SHOTS = re.compile(r"(\d+) Paraden bei (\d+) Schüssen")


def _goalie_line(detail: str | None) -> tuple[int, int] | None:
    """'37 Paraden bei 41 Schüssen' -> (saves 37, shots 41)."""
    hit = _SHOTS.search(detail or "")
    return (int(hit.group(1)), int(hit.group(2))) if hit else None


def _season_start(dt: datetime) -> int:
    d = dt.astimezone(TZ).date()
    return d.year if d.month >= 8 else d.year - 1


def sheet_rows(
    s: Session, platform: str = "leijonat", goalie_ids: set[str] | None = None
) -> tuple[list[TotalRow], list[str]]:
    """Per player, team, league and season from the stored sheets of one platform. Returns (rows, skipped).

    `goalie_ids`: normalised names of known goalies (position G in Karl): a dressed backup goalie is in the
    line-up but has not played, so goalies count only the games with a goalie line (shots faced > 0)."""
    from klugscheisser.domain.names import normalize

    goalie_ids = goalie_ids or set()
    items = list(
        s.scalars(select(m.Item).where(m.Item.platform == platform, m.Item.source_type == "official_result"))
    )
    # tournament series (league, season) -> games; final events without a sheet make a team incomplete
    by_series: dict[tuple[str, int], list[m.Item]] = defaultdict(list)
    for it in items:
        raw = it.raw or {}
        league = sheet_league(raw.get("competition"))
        if not league or it.published_at is None or not (raw.get("lineup") or raw.get("participants")):
            continue
        by_series[(league, _season_start(it.published_at))].append(it)
    have_ids = {it.external_id for it in items}
    missing_teams: dict[tuple[str, int], set[str]] = defaultdict(set)
    for ev in s.scalars(
        select(m.Event).where(
            m.Event.kind == "game", m.Event.status == "final", m.Event.key.startswith(f"{platform}-")
        )
    ):
        comp = s.get(m.Competition, ev.competition_id) if ev.competition_id else None
        league = sheet_league(comp.name if comp else None)
        if not league or ev.starts_at is None or ev.key.split("-", 1)[1] in have_ids:
            continue
        for team in (ev.name or "").split(" – "):
            missing_teams[(league, _season_start(ev.starts_at))].add(team.strip())

    rows: list[TotalRow] = []
    skipped: list[str] = []
    for (league, season), games in by_series.items():
        games.sort(key=lambda it: it.published_at)
        acc: dict[tuple[str, str], dict] = {}
        last: dict[str, m.Item] = {}

        def entry(team: str, name: str, acc: dict = acc) -> dict:
            return acc.setdefault(
                (team, name),
                {"games": 0, "goals": 0, "assists": 0, "goalie_games": 0, "lines": 0, "pim": 0, "pm": 0},
            )

        for it in games:
            raw = it.raw or {}
            parts = raw.get("participants") or []
            lines = raw.get("player_stats") or []
            goalie_lines = [(p, _goalie_line(p.get("detail"))) for p in parts if p.get("role") == "goalie"]
            feed_gap = bool(goalie_lines) and sum(g[0] for _, g in goalie_lines if g) == 0
            for team in (*(raw.get("lineup") or {}), raw.get("home"), raw.get("away")):
                if team:
                    last[team] = it
            if not raw.get("lineup") and not lines:  # scorers and goalies only: no games played for skaters
                for p in parts:
                    if p.get("role") == "goalie" and p.get("team") and p.get("name"):
                        a = entry(p["team"], p["name"])
                        a["goalie"] = True
                        a["goalie_games"] += 1
                        a["games"] += 1
                        a["gap"] = True  # no saves in these sheets
                for p in parts:
                    if p.get("team") and p.get("name"):
                        entry(p["team"], p["name"])["no_gp"] = True
            if lines:  # the game roster with time on ice: who really played, PIM, +/-, goalie saves
                with_toi = {ln.get("team") for ln in lines if (ln.get("toi") or 0) > 0}
                for ln in lines:
                    team, name = ln.get("team"), ln.get("name")
                    if not team or not name:
                        continue
                    if ln.get("goalie"):  # some feeds give goalies no time on ice, but saves when they played
                        shots = (ln.get("saves") or 0) + (ln.get("ga") or 0)
                        played = (ln.get("toi") or 0) > 0 or shots > 0
                    else:
                        played = (ln.get("toi") or 0) > 0 if team in with_toi else True
                    if not played:
                        continue
                    a = entry(team, name)
                    a["games"] += 1
                    a["lines"] += 1
                    a["pim"] += ln.get("pim") or 0
                    a["pm"] += ln.get("pm") or 0
                    if ln.get("goalie") or normalize(name) in goalie_ids:
                        a["goalie"] = True
                        a["goalie_games"] += 1
                        gap = feed_gap or sum(x.get("saves") or 0 for x in lines if x.get("goalie")) == 0
                        if ln.get("saves") is None or ln.get("ga") is None or gap:
                            a["gap"] = True
                        else:
                            a["saves"] = a.get("saves", 0) + ln["saves"]
                            a["goals_against"] = a.get("goals_against", 0) + ln["ga"]
            else:  # older sheets: the line-up only (dressed players), goalies by their goalie line
                played_goalies = {(p.get("team"), p["name"]): g for p, g in goalie_lines if g and g[1] > 0}
                for team, names in (raw.get("lineup") or {}).items():
                    for name in names:
                        is_goalie = normalize(name) in goalie_ids or any(
                            p.get("team") == team and p.get("name") == name for p, _ in goalie_lines
                        )
                        if is_goalie and (team, name) not in played_goalies:
                            continue  # dressed backup
                        a = entry(team, name)
                        a["games"] += 1
                        if is_goalie:
                            a["goalie"] = True
                            a["goalie_games"] += 1
                            g = played_goalies[(team, name)]
                            if feed_gap:
                                a["gap"] = True
                            else:
                                a["saves"] = a.get("saves", 0) + g[0]
                                a["goals_against"] = a.get("goals_against", 0) + (g[1] - g[0])
            for p in parts:
                if p.get("role") not in ("scorer", "assist") or SHOOTOUT.search(p.get("detail") or ""):
                    continue
                a = entry(p.get("team"), p["name"])
                if not a["games"] and not a.get("no_gp"):  # scorer missing from the roster: count the game
                    a["games"] = 1
                a["goals" if p["role"] == "scorer" else "assists"] += 1
        for (team, name), a in acc.items():
            if team in missing_teams.get((league, season), set()) or team not in last:
                continue
            it = last[team]
            full_lines = a["lines"] == a["games"]  # PIM and +/- only when every counted game has its line
            if a.get("goalie"):
                if not a["goalie_games"]:
                    continue
                values = {"games": a["goalie_games"]}
                if not a.get("gap") and a.get("saves") is not None:
                    values["saves"], values["goals_against"] = a["saves"], a["goals_against"]
                    shots = a["saves"] + a["goals_against"]
                    if shots:
                        values["save_pct"] = round(a["saves"] / shots, 3)
            else:
                values = {
                    "games": None if a.get("no_gp") else a["games"],
                    "goals": a["goals"],
                    "assists": a["assists"],
                    "points": a["goals"] + a["assists"],
                }
                if full_lines:
                    values["penalty_minutes"], values["plus_minus"] = a["pim"], a["pm"]
            rows.append(
                TotalRow(
                    name=name,
                    team=team,
                    league=league,
                    season=season,
                    phase="regular",
                    team_kind="national",
                    source=platform,
                    source_url=it.url,
                    stand=it.published_at,
                    goalie=bool(a.get("goalie")),
                    values=values,
                )
            )
        for team in sorted(missing_teams.get((league, season), set())):
            if any(t == team for t, _ in acc):
                skipped.append(f"{league} {season}: {team} (Spielbericht fehlt)")
    return rows, skipped


def refetch_leijonat_lines(s: Session, http, limit: int = 40) -> int:
    """Stored Leijonat sheets from before 2026-10-25 lack the per-player lines (time on ice, PIM): fetch the game
    roster once more (one request per game, polite) and add `player_stats` to the stored raw. Returns games done."""
    from urllib.parse import parse_qs, urlparse

    from klugscheisser.collectors.results.leijonat import API, JSON, player_line, title_name

    done = 0
    for it in s.scalars(
        select(m.Item).where(m.Item.platform == "leijonat", m.Item.source_type == "official_result")
    ):
        raw = dict(it.raw or {})
        if raw.get("player_stats") or not raw.get("lineup") or done >= limit:
            continue
        season = (parse_qs(urlparse(it.url).query).get("season") or [None])[0]
        try:
            summary = http.json(
                f"{API}/game/helpers/getsummary", {"gameid": it.external_id, "season": season}, headers=JSON
            )
        except Exception as exc:
            log.warning("leijonat roster %s failed: %s", it.external_id, exc)
            continue
        lines = []
        for side, team in (("HomeTeamGameRoster", raw.get("home")), ("AwayTeamGameRoster", raw.get("away"))):
            for p in (summary.get(side) or {}).get("Players") or []:
                if name := title_name(p.get("FirstName"), p.get("LastName")):
                    lines.append(player_line(name, team, p))
        if lines:
            raw["player_stats"] = lines
            it.raw = raw
            done += 1
    s.flush()
    return done


# --- matching and storing -------------------------------------------------------------------------------------


def match_name(matcher, name: str) -> int | None:
    """One Karl person for a sheet/table name ('Lara Stalder', also the other name order), else None.
    Never guesses between namesakes (matcher without Jev)."""
    from klugscheisser.ingest.results import name_candidates

    parts = name.split()
    tries = name_candidates(name)
    if len(parts) >= 3:  # 'Ivana Maria Wey' -> 'Ivana Wey' (Karl stores the name without middle names)
        tries += name_candidates(f"{parts[0]} {parts[-1]}")
    for candidate in tries:
        found = matcher.from_text(candidate)
        if len(found) == 1:
            return found[0].athlete_id
        if found:
            return None
    return None


def _team_names(
    s: Session, rows: list[TotalRow], ids: dict[int, int]
) -> dict[tuple[str, str, int], tuple[str, str | None]]:
    """(source team, league, season) -> (EP team name, EP team id) by majority of the matched players' EP rows
    (same league, this season or the one before)."""
    votes: dict[tuple[str, str, int], Counter] = defaultdict(Counter)
    for i, r in enumerate(rows):
        aid = ids.get(i)
        if aid is None:
            continue
        ep = s.execute(
            select(m.SeasonStat.team_name, m.SeasonStat.ep_team_id, m.SeasonStat.season).where(
                m.SeasonStat.athlete_id == aid,
                m.SeasonStat.source == "eliteprospects",
                m.SeasonStat.league == r.league,
                m.SeasonStat.season.in_((r.season, r.season - 1)),
                m.SeasonStat.team_name.is_not(None),
            )
        ).all()
        if not ep:
            continue
        newest = max(x.season for x in ep)
        for name, team_id, season in ep:
            if season == newest and not name.endswith("(all)"):
                votes[(r.team, r.league, r.season)][(name, team_id)] += 1
    return {k: c.most_common(1)[0][0] for k, c in votes.items() if c}


# --- plausibility of a name match for club tables of other countries ---------------------------------------

# words that say nothing about which club it is
_CLUB_NOISE = {
    "if",
    "hc",
    "ik",
    "bk",
    "hf",
    "sk",
    "goif",
    "hockey",
    "ishockey",
    "ice",
    "club",
    "klub",
    "ry",
    "naiset",
    "women",
    "womens",
    "dam",
    "damer",
    "team",
    "the",
    "swe",
    "sweden",
    "fin",
    "finland",
    "den",
    "usa",
    "pwhl",
    "wcha",
    "ecac",
    "ahl",
    "he",
    "aha",
    "university",
    "college",
    "of",
    "ev",
    "ec",
    "sc",
    "hk",
    "and",
}


def _club_words(name: str) -> tuple[set[str], str]:
    from klugscheisser.domain.names import normalize

    words = normalize(name).split()
    return {w for w in words if w not in _CLUB_NOISE and len(w) >= 3}, "".join(words)


def same_club(a: str, b: str) -> bool:
    """'Malmö (SWE)' ~ 'IF Malmö Redhawks', 'HV71' ~ 'HV 71', 'Brynäs' ~ 'Brynäs IF'; 'AIK' !~ 'Leksands IF'."""
    from klugscheisser.collectors.results.jihf_stats import team_name as jp_team

    a, b = jp_team(a), jp_team(b)  # Japanese club names as written on JIHF rosters -> English
    wa, ca = _club_words(a)
    wb, cb = _club_words(b)
    if wa & wb:
        return True
    return bool(ca and cb and len(min(ca, cb, key=len)) >= 4 and (ca in cb or cb in ca))


@dataclass
class Evidence:
    clubs: list[str] = field(default_factory=list)  # any club Karl knows for this season (may be stale)
    ep_now: list[tuple[str, str]] = field(
        default_factory=list
    )  # EP rows of this season with games: (league, team)


def _club_evidence(s: Session, ids: list[int], season: int | None) -> dict[int, Evidence]:
    """What Karl knows about these persons' clubs this season, from everything except the guarded tables
    themselves: roster affiliations, roster seeds (`club_text`), season rows, career stations
    (answer/connections.py), plus the Elite Prospects rows of this season (04.10.2026) with games played."""
    out: dict[int, Evidence] = defaultdict(Evidence)
    if not ids:
        return out
    from klugscheisser.answer.connections import current_clubs

    ids = sorted(set(ids))
    try:
        for aid, clubs in current_clubs(s, ids, season).items():
            out[aid].clubs = [c.name for c in clubs]
    except Exception:  # evidence is optional: without it a match is only checked by position and birth year
        log.exception("season totals: club evidence failed")
    for aid, league, team in s.execute(
        select(m.SeasonStat.athlete_id, m.SeasonStat.league, m.SeasonStat.team_name).where(
            m.SeasonStat.athlete_id.in_(ids),
            m.SeasonStat.source == "eliteprospects",
            m.SeasonStat.season == season,
            m.SeasonStat.team_kind.in_(("club", "college")),
            m.SeasonStat.games > 0,
        )
    ):
        out[aid].ep_now.append((league, team or ""))
    return out


def _not_player() -> str:
    """Reason of a dropped row: no player (2026-11-24: "kein Spieler" in men's projects)."""
    from klugscheisser.sports import current_spec

    return "kein Spieler" if current_spec().men else "keine Spielerin"


def plausible(a: m.Athlete | None, r: TotalRow, ev: Evidence | None) -> tuple[str, str]:
    """'yes' (a known club of the person is this team), 'unknown' (nothing confirms it, nothing hard disagrees:
    stored and listed), 'no' (not stored: coach/staff, goalie vs skater, other birth year, or Elite Prospects has
    her playing for another league this season). Older club info (roster seeds, career stations) only confirms:
    players move, so a different old club is no reason to drop a row."""
    ev = ev or Evidence()
    if a is None or (a.role or "athlete") != "athlete":
        return "no", _not_player()
    if a.position and (a.position == "G") != r.goalie:
        return "no", f"Position {a.position}"
    if r.birth_year and a.birth_date and a.birth_date.year != r.birth_year:
        return "no", f"Jahrgang {a.birth_date.year} statt {r.birth_year}"
    if any(same_club(c, r.team) for c in ev.clubs) or any(
        lg == r.league and same_club(tm, r.team) for lg, tm in ev.ep_now
    ):
        return "yes", ""
    other = [f"{tm} ({lg})" for lg, tm in ev.ep_now if lg != r.league]
    if other and not any(lg == r.league for lg, _ in ev.ep_now):
        return "no", "Elite Prospects 04.10.: " + ", ".join(other[:2])
    return "unknown", ("Karl kennt: " + ", ".join(ev.clubs[:2])) if ev.clubs else "kein Verein bekannt"


def swehockey_rows(results) -> list[TotalRow]:
    """SDHL / NDHL rows from the stats.swehockey.se "Players By Team" pages."""
    from klugscheisser.collectors.results.swehockey_stats import TZ as SWE_TZ

    out = []
    for league, table, stand in results:
        stand = stand or datetime.now(SWE_TZ)
        for r in table:
            out.append(
                TotalRow(
                    name=r["name"],
                    team=r["team"],
                    league=league.league,
                    season=_season_start(stand),
                    phase="regular",
                    team_kind="club",
                    source=SOURCE_SWEHOCKEY,
                    source_url=league.table_url,
                    stand=stand,
                    goalie=bool(r.get("goalie")),
                    values={k: v for k, v in r.items() if k in STAT_FIELDS and v is not None},
                    guard="club",
                )
            )
    return out


def leijonat_liiga_rows(league: str, results) -> list[TotalRow]:
    """Auroraliiga rows from the Leijonat league tables (collectors/results/leijonat_stats.py)."""
    out = []
    for url, table, stand in results:
        stand = stand or datetime.now(UTC)
        for r in table:
            out.append(
                TotalRow(
                    name=r["name"],
                    team=r["team"],
                    league=league,
                    season=_season_start(stand),
                    phase="regular",
                    team_kind="club",
                    source=SOURCE_LEIJONAT_LIIGA,
                    source_url=url,
                    stand=stand,
                    goalie=bool(r.get("goalie")),
                    values={k: v for k, v in r.items() if k in STAT_FIELDS and v is not None},
                    guard="club",
                    birth_year=r.get("birth_year"),
                )
            )
    return out


def native_names(s: Session) -> dict[str, str]:
    """Japanese spelling -> Karl's name, from the roster seeds (`name_orig`, e.g. "野呂 里桜" -> "Rio Noro").
    Only persons Karl finds under the seed's romanised name (one person, no namesakes) are in the map."""
    from pathlib import Path

    import yaml

    from klugscheisser.collectors.results.jihf_stats import name_key
    from klugscheisser.domain.matcher import AthleteMatcher
    from klugscheisser.tournament import INFO_DIR

    matcher = AthleteMatcher(s, jev_client=None)
    out: dict[str, str] = {}
    for path in sorted((Path(INFO_DIR).parent / "rosters").glob("*.yaml")):
        seed = yaml.safe_load(path.read_text()) or {}
        for p in seed.get("players") or []:
            if not p.get("name_orig") or not p.get("name"):
                continue
            aid = match_name(matcher, p["name"])
            if aid is not None:
                out[name_key(p["name_orig"])] = s.get(m.Athlete, aid).full_name
    return out


def jihf_rows(s: Session, sheets: dict, report: TotalsReport) -> list[TotalRow]:
    """Japan women's league rows counted from the cached JIHF game sheets (collectors/results/jihf_stats.py).
    Names in Japanese script that no roster seed spells are left out (counted in the report)."""
    from klugscheisser.collectors.results.jihf_stats import LEAGUE, MEET_URL, totals

    if not sheets:
        return []
    native = native_names(s)
    games = sorted(sheets.values(), key=lambda x: x["date"])
    by_season: dict[int, list[dict]] = defaultdict(list)
    for g in games:
        by_season[_season_start(datetime.fromisoformat(g["date"]))].append(g)
    out = []
    for season, sh in by_season.items():
        last = sh[-1]
        stand = datetime.fromisoformat(last["date"])
        meet = re.search(r"meet_id=(\d+)", last.get("url") or "")
        url = MEET_URL.format(meet=meet.group(1)) if meet else last.get("url") or ""
        no_latin = 0
        for r in totals(sh):
            name = native.get(r["key"])
            if not name:
                no_latin += 1
                continue
            out.append(
                TotalRow(
                    name=name,
                    team=r["team"],
                    league=LEAGUE,
                    season=season,
                    phase="regular",
                    team_kind="club",
                    source=SOURCE_JIHF,
                    source_url=url,
                    stand=stand,
                    goalie=bool(r.get("goalie")),
                    values={k: v for k, v in r.items() if k in STAT_FIELDS and v is not None},
                    guard="club",
                )
            )
        report.per_source[f"jihf_{season}_spiele"] = len(sh)
        report.per_source[f"jihf_{season}_ohne_lateinischen_namen"] = no_latin
    return out


def _has_ep_row(s: Session, aid: int, season: int, league: str) -> bool:
    return (
        s.scalar(
            select(m.SeasonStat.id)
            .where(
                m.SeasonStat.athlete_id == aid,
                m.SeasonStat.source == "eliteprospects",
                m.SeasonStat.season == season,
                m.SeasonStat.league == league,
            )
            .limit(1)
        )
        is not None
    )


def nation_code(team: str) -> str:
    """'Switzerland U18' -> 'SUI' (EP's English team names of the national youth teams)."""
    from klugscheisser.collectors.results.base import TEAM_DE

    english = re.sub(r"\s+U\d+$", "", team or "")
    for code, german in TEAM_DE.items():
        if NATION_EN.get(german) == english or code == english:
            return code
    return english


def nation_team(code: str, suffix: str = " U18") -> str:
    """'SUI' -> 'Switzerland U18'."""
    from klugscheisser.collectors.results.base import TEAM_DE

    german = TEAM_DE.get(code, code)
    return f"{NATION_EN.get(german, german)}{suffix}"


def plausible_nation(s: Session, a: m.Athlete | None, r: TotalRow) -> tuple[str, str]:
    """National youth teams: 'yes' when the birth year agrees (both known) or the nation agrees (Karl's
    nationality, else a roster of that nation's team), 'no' on a different birth year / nation / position,
    'no' as well when Karl knows neither (a name alone is not enough for an old youth tournament)."""
    if a is None or (a.role or "athlete") != "athlete":
        return "no", _not_player()
    if a.position and (a.position == "G") != r.goalie:
        return "no", f"Position {a.position}"
    code = nation_code(r.team)
    # the birth year decides: Karl holds some dates as year only (1 January) and a few with day and month swapped
    year = int(r.birth_date[:4]) if r.birth_date and r.birth_date[:4].isdigit() else r.birth_year
    if a.birth_date and year:
        if a.birth_date.year != year:
            return "no", f"Jahrgang {a.birth_date.year} statt {year}"
        return "yes", ""
    nations = (
        {a.nationality}
        if a.nationality
        else set(
            s.scalars(
                select(m.Team.country)
                .join(m.EventEntry, m.EventEntry.team_id == m.Team.id)
                .where(
                    m.EventEntry.athlete_id == a.id, m.Team.kind == "national", m.Team.country.is_not(None)
                )
            )
        )
    )
    if not nations:
        return "no", "Nation unbekannt"
    return ("yes", "") if code in nations else ("no", f"Nation {', '.join(sorted(nations))} statt {code}")


def store(s: Session, rows: list[TotalRow], report: TotalsReport) -> None:
    """Replace the stored rows of each (source, league, season, phase) in `rows` by the new ones."""
    from klugscheisser.domain.matcher import AthleteMatcher

    matcher = AthleteMatcher(s, jev_client=None)
    ids: dict[int, int] = {}
    for i, r in enumerate(rows):
        aid = match_name(matcher, r.name)
        if aid is None:
            report.unmatched.append(f"{r.name} ({r.team})")
        else:
            ids[i] = aid
    report.rows += len(rows)
    report.matched += len(ids)
    names = _team_names(s, rows, ids)
    scopes = {(r.source, r.league, r.season, r.phase) for r in rows}
    for source, league, season, phase in scopes:
        s.execute(
            delete(m.SeasonStat).where(
                m.SeasonStat.source == source,
                m.SeasonStat.league == league,
                m.SeasonStat.season == season,
                m.SeasonStat.phase == phase,
            )
        )
    checks = _club_evidence(
        s, [aid for i, aid in ids.items() if rows[i].guard == "club"], rows[0].season if rows else None
    )
    best: dict[tuple, tuple[TotalRow, int, str, str | None]] = {}
    for i, aid in ids.items():
        r = rows[i]
        if r.fill_gap_only and _has_ep_row(s, aid, r.season, r.league):
            report.per_source["ep_vorhanden"] = report.per_source.get("ep_vorhanden", 0) + 1
            continue
        if r.guard:
            a = s.get(m.Athlete, aid)
            verdict, why = (
                plausible_nation(s, a, r) if r.guard == "nation" else plausible(a, r, checks.get(aid))
            )
            if verdict == "no":
                report.doubtful.append(f"{r.name} ({r.team}, {r.league}): {why}")
                log.info("season totals: %s (%s) not stored: %s", r.name, r.team, why)
                report.matched -= 1
                continue
            if verdict == "unknown":
                report.unconfirmed.append(f"{r.name} ({r.team}, {r.league}): {why}")
        if r.source == SOURCE_COLLEGE and not r.goalie:
            a = s.get(m.Athlete, aid)
            if a is not None and a.position == "G":
                continue  # the newer college layout lists goalies in the skater table too
        team, ep_team_id = names.get((r.team, r.league, r.season)) or (
            NATION_EN.get(r.team, r.team) if r.team_kind == "national" else r.team,
            None,
        )
        key = (aid, r.season, r.league, team, r.phase, r.source)
        old = best.get(key)
        if old is None or (r.values.get("games") or 0) > (old[0].values.get("games") or 0):
            best[key] = (r, aid, team, ep_team_id)
    for r, aid, team, ep_team_id in best.values():
        v = r.values
        s.add(
            m.SeasonStat(
                athlete_id=aid,
                season=r.season,
                league=r.league,
                team_name=team[:200],
                team_kind=r.team_kind,
                ep_team_id=ep_team_id,
                phase=r.phase,
                games=v.get("games"),
                goals=v.get("goals"),
                assists=v.get("assists"),
                points=v.get("points"),
                penalty_minutes=v.get("penalty_minutes"),
                plus_minus=v.get("plus_minus"),
                goals_against=v.get("goals_against"),
                saves=v.get("saves"),
                save_pct=v.get("save_pct"),
                gaa=v.get("gaa"),
                wins=v.get("wins"),
                losses=v.get("losses"),
                ties=v.get("ties"),
                shutouts=v.get("shutouts"),
                toi=v.get("toi"),
                source=r.source,
                source_url=r.source_url,
                fetched_at=r.stand,
            )
        )
        report.per_source[r.source] = report.per_source.get(r.source, 0) + 1
    report.stored += len(best)
    s.flush()


def college_rows(results) -> list[TotalRow]:
    """NCAA rows from the college stats pages (collectors/results/college_stats.py)."""
    out = []
    for site, url, table, last in results:
        stand = last or datetime.now(UTC)
        season = _season_start(stand)
        for r in table:
            out.append(
                TotalRow(
                    name=r["name"],
                    team=site.team,
                    league="NCAA (W)",
                    season=season,
                    phase="regular",
                    team_kind="college",
                    source=SOURCE_COLLEGE,
                    source_url=url,
                    stand=stand,
                    goalie=bool(r.get("goalie")),
                    values={k: v for k, v in r.items() if k in STAT_FIELDS and v is not None},
                )
            )
    return out


def _goalie_names(s: Session) -> set[str]:
    from klugscheisser.domain.names import normalize

    return {normalize(n) for n in s.scalars(select(m.Athlete.full_name).where(m.Athlete.position == "G"))}


def refresh(
    s: Session, settings, fetch: bool = True, http=None, refetch_lines: bool = False, college_client=None
) -> dict:
    """Nightly step `saisonwerte` (and CLI `klugscheisser season-totals`): SIHF tables (when `fetch`) + the
    national-team totals per tournament from the stored sheets (Leijonat, IIHF realtime, Wikipedia game boxes)."""
    report = TotalsReport()
    if fetch and settings.sihf_data_enabled:
        from klugscheisser.collectors.results.base import PoliteHttp
        from klugscheisser.collectors.results.sihf import fetch_season_tables

        try:
            http = http or PoliteHttp(settings.results_request_delay_s)
            table, phase, stand = fetch_season_tables(http, settings.sihf_league_id, settings.sihf_season)
            if table:
                with s.begin_nested():
                    store(
                        s, sihf_rows(table, phase, stand or datetime.now(UTC), settings.sihf_season), report
                    )
        except Exception as exc:  # the old rows stay
            log.warning("season totals: SIHF tables failed: %s", exc)
            report.errors.append(f"sihf: {exc}")
    if fetch and settings.season_totals_college_enabled:
        from klugscheisser.collectors.profiles.college import load_sites
        from klugscheisser.collectors.profiles.http import PoliteClient
        from klugscheisser.collectors.results.college_stats import fetch_college_stats

        try:
            got = fetch_college_stats(college_client or PoliteClient(max_crawl_delay_s=60.0), load_sites())
            report.per_source["college_seiten"] = len(got)
            if got:
                with s.begin_nested():
                    store(s, college_rows(got), report)
        except Exception as exc:
            log.warning("season totals: college stats failed: %s", exc)
            report.errors.append(f"college: {exc}")
    if fetch and settings.season_totals_swehockey_enabled and settings.season_totals_swehockey_leagues:
        from klugscheisser.collectors.results.base import PoliteHttp
        from klugscheisser.collectors.results.swehockey_stats import fetch_league, leagues_from_config

        swe_http = http or PoliteHttp(settings.results_request_delay_s)
        groups: dict[str, list] = defaultdict(list)  # both NDHL groups are one EP league: stored together
        for league in leagues_from_config(settings.season_totals_swehockey_leagues):
            groups[league.league].append(league)
        for name, leagues in groups.items():
            try:  # a failing page keeps that league's old rows (all its groups)
                got = []
                for league in leagues:
                    table, stand = fetch_league(swe_http, league)
                    report.per_source[f"swehockey_{league.schedule_id}"] = len(table)
                    got.append((league, table, stand))
                if any(table for _, table, _ in got):
                    with s.begin_nested():
                        store(s, swehockey_rows(got), report)
            except Exception as exc:
                log.warning("season totals: swehockey %s failed: %s", name, exc)
                report.errors.append(f"swehockey {name}: {exc}")
    if fetch and settings.season_totals_leijonat_leagues_enabled and settings.season_totals_leijonat_leagues:
        from klugscheisser.collectors.results.base import PoliteHttp, season_of
        from klugscheisser.collectors.results.leijonat_stats import LeijonatLeague
        from klugscheisser.collectors.results.leijonat_stats import fetch_league as fetch_fin

        fin_http = http or PoliteHttp(settings.results_request_delay_s)
        alias = settings.leijonat_season or season_of(datetime.now(TZ).date())
        for league in LeijonatLeague.from_config(settings.season_totals_leijonat_leagues):
            try:
                got = fetch_fin(fin_http, league, alias)
                report.per_source[f"leijonat_liiga_{league.level_id}"] = sum(len(t) for _, t, _ in got)
                if any(t for _, t, _ in got):
                    with s.begin_nested():
                        store(s, leijonat_liiga_rows(league.league, got), report)
            except Exception as exc:
                log.warning("season totals: Leijonat league %s failed: %s", league.league, exc)
                report.errors.append(f"leijonat liiga {league.league}: {exc}")
    if fetch and settings.season_totals_jihf_enabled and settings.season_totals_jihf_meets:
        from klugscheisser.collectors.results.base import PoliteHttp
        from klugscheisser.collectors.results.jihf_stats import fetch_sheets

        try:  # finished sheets never change: kept in source_state, only new games are fetched
            state = s.get(m.SourceState, SOURCE_JIHF) or m.SourceState(source=SOURCE_JIHF, state={})
            jp_http = http or PoliteHttp(max(settings.results_request_delay_s, 2.0))
            sheets, n = fetch_sheets(
                jp_http, list(settings.season_totals_jihf_meets), (state.state or {}).get("sheets")
            )
            state.state = {**(state.state or {}), "sheets": sheets}
            s.add(state)
            report.per_source["jihf_anfragen"] = n
            rows = jihf_rows(s, sheets, report)
            if rows:
                with s.begin_nested():
                    store(s, rows, report)
        except Exception as exc:
            log.warning("season totals: JIHF failed: %s", exc)
            report.errors.append(f"jihf: {exc}")
    if settings.leijonat_enabled and refetch_lines:
        from klugscheisser.collectors.results.base import PoliteHttp

        http = http or PoliteHttp(settings.results_request_delay_s)
        report.per_source["leijonat_nachgeladen"] = refetch_leijonat_lines(s, http)
    sheet_totals(s, report)
    return report.out()


def sheet_totals(
    s: Session, report: TotalsReport, platforms: tuple[str, ...] = SHEET_PLATFORMS
) -> TotalsReport:
    """National-team totals per tournament counted from the stored sheets (no requests). Also run by the game
    watch right after a new final game sheet (platform "iihf"), so `stats` shows the game at once."""
    goalies = _goalie_names(s)
    for platform in platforms:
        try:
            rows, skipped = sheet_rows(s, platform, goalies)
            report.skipped += skipped
            if rows:
                with s.begin_nested():
                    store(s, rows, report)
        except Exception as exc:
            log.exception("season totals: %s sheets failed", platform)
            report.errors.append(f"{platform}: {exc}")
    return report


# --- cross-check: SIHF game sheets never exceed the league table ------------------------------------------


def check_sihf(s: Session) -> list[str]:
    """Players whose goals/assists in the stored SIHF game sheets are above the stored SIHF season row: a sign of
    a wrong name match or a broken table. The sheets cover only part of the season, so lower is normal."""
    return check_sheets(s, "sihf", SOURCE_SIHF)


# platform of the stored game sheets -> source of the league table they are checked against
SHEET_CHECKS = {"sihf": SOURCE_SIHF, "sdhl": SOURCE_SWEHOCKEY}


def check_sheets(s: Session, platform: str, source: str) -> list[str]:
    """Cross-check: goals/assists counted from the stored game sheets of `platform` must not exceed the stored
    league-table row of `source` (same person, newest season). Higher = wrong match or broken table."""
    from klugscheisser.domain.matcher import AthleteMatcher

    matcher = AthleteMatcher(s, jev_client=None)
    sums: dict[int, Counter] = defaultdict(Counter)
    for it in s.scalars(
        select(m.Item).where(m.Item.platform == platform, m.Item.source_type == "official_result")
    ):
        raw = it.raw or {}
        for p in raw.get("participants") or []:
            if SHOOTOUT.search(p.get("detail") or ""):
                continue
            if p.get("role") in ("scorer", "assist") and (aid := match_name(matcher, p["name"])):
                sums[aid]["goals" if p["role"] == "scorer" else "assists"] += 1
    out = []
    for aid, c in sums.items():
        row = s.scalar(
            select(m.SeasonStat)
            .where(
                m.SeasonStat.athlete_id == aid,
                m.SeasonStat.source == source,
                m.SeasonStat.phase == "regular",
                m.SeasonStat.goals_against.is_(None),
            )
            .order_by(m.SeasonStat.season.desc())
        )
        if row is None:
            continue
        for f in ("goals", "assists"):
            if c[f] > (getattr(row, f) or 0):
                a = s.get(m.Athlete, aid)
                out.append(f"{a.full_name}: {f} Spielberichte {c[f]} > Tabelle {getattr(row, f)}")
    return out
