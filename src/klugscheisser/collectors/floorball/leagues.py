"""Shared part of the foreign league adapters (SSL Sweden: `ssl_se`, F-Liiga Finland: `fliiga`), 2026-11-24.

A league adapter returns `LeaguePlayer`s (one per league player id, all seasons it found). This module matches
them to our athletes by name (+ birth year when both sides know it), writes their season rows (`SeasonStat`,
source = the adapter's SOURCE) and career stints (source "league", one run of consecutive seasons per club),
fills empty profile fields (position, birth date), and ends outdated open club stints of other sources
("newest dated source wins", see `close_outdated_open_stints`).

Nothing is deleted: rows of a source are only added or updated.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.floorball.common import current_season
from klugscheisser.collectors.floorball.swissunihockey import _keys as name_keys
from klugscheisser.db import models as m
from klugscheisser.domain.names import normalize
from klugscheisser.facts.teams import team_key

STINT_SOURCE = "league"  # CareerStint.source of the league adapters
# structured season-by-season sources: a later station there ends an open station of any other source
STRUCTURED = ("federation", "league", "eliteprospects")
ISO2_TO_IOC = {
    "SE": "SWE",
    "FI": "FIN",
    "CH": "SUI",
    "DE": "GER",
    "CZ": "CZE",
    "NO": "NOR",
    "DK": "DEN",
    "LV": "LAT",
    "SK": "SVK",
    "EE": "EST",
    "SI": "SLO",
    "CA": "CAN",
    "JP": "JPN",
    "SG": "SGP",
    "TH": "THA",
    "PH": "PHI",
    "AT": "AUT",
    "NL": "NED",
    "PL": "POL",
    "US": "USA",
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
)


@dataclass
class LeagueRow:
    """One season of one player at one club in one phase, as the league publishes it."""

    season: int  # start year: 2026 = 2026/27
    team: str
    phase: str = "regular"  # regular | playoffs
    games: int | None = None
    goals: int | None = None
    assists: int | None = None
    points: int | None = None
    penalty_minutes: int | None = None
    plus_minus: int | None = None
    goals_against: int | None = None
    saves: int | None = None
    save_pct: float | None = None  # 0.8603
    gaa: float | None = None

    @property
    def season_label(self) -> str:
        return f"{self.season}/{(self.season + 1) % 100:02d}"


@dataclass
class LeaguePlayer:
    key: str  # the league's own player id (SSL uuid, F-Liiga slug)
    name: str
    url: str  # public page of the player (source link)
    birth_date: date | None = None
    birth_year: int | None = None
    nationality: str | None = None  # IOC code, None = not published
    position: str | None = None  # G | D | F
    team: str | None = None  # club of the running season, as published
    rows: list[LeagueRow] = field(default_factory=list)

    @property
    def year(self) -> int | None:
        return self.birth_date.year if self.birth_date else self.birth_year


class Index:
    """Name key -> league players (by league id)."""

    def __init__(self, players: list[LeaguePlayer]) -> None:
        self.by_key: dict[str, dict[str, LeaguePlayer]] = defaultdict(dict)
        for p in players:
            for k in name_keys(p.name):
                self.by_key[k][p.key] = p

    def candidates(self, name: str) -> list[LeaguePlayer]:
        out: dict[str, LeaguePlayer] = {}
        for k in name_keys(name):
            out.update(self.by_key.get(k, {}))
        return list(out.values())

    def match(
        self, athlete: m.Athlete, clubs: set[str] | None = None
    ) -> tuple[LeaguePlayer | None, str | None]:
        """(player, None) when exactly one league player fits; (None, reason) otherwise.
        Fits = same name; birth year equal when both are known; when the athlete has no birth date, a published
        nationality must equal the athlete's, unless the league player played for one of `clubs` (the athlete's
        club on the federation's tournament roster: Danes and Norwegians at Swedish clubs are often listed with
        nationality "SE")."""
        cands = self.candidates(athlete.full_name)
        if not cands:
            return None, None
        if clubs:
            at_club = [p for p in cands if any(_same_club(r.team, c) for r in p.rows for c in clubs)]
            if len(at_club) == 1 and (
                not athlete.birth_date or at_club[0].year in (None, athlete.birth_date.year)
            ):
                return at_club[0], None
        year = athlete.birth_date.year if athlete.birth_date else None
        if year:
            cands = [p for p in cands if p.year in (None, year)]
            if not cands:
                return None, "Jahrgang passt nicht"
            exact = [p for p in cands if p.year == year]
            if len(exact) == 1:
                return exact[0], None
        elif athlete.nationality:
            cands = [p for p in cands if p.nationality in (None, athlete.nationality)]
            if not cands:
                return None, "Nationalität passt nicht"
        if len(cands) > 1:
            return None, f"mehrdeutig ({len(cands)} Spieler)"
        return cands[0], None


def stint_specs(rows: list[LeagueRow], league: str, today: date | None = None) -> list[dict]:
    """Runs of consecutive seasons at one club -> career stint dicts (regular season and playoffs together)."""
    cur = current_season(today)
    by_team: dict[str, set[int]] = defaultdict(set)
    for r in rows:
        by_team[r.team].add(r.season)
    out = []
    for team, seasons_ in by_team.items():
        seasons = sorted(seasons_)
        runs: list[list[int]] = [[seasons[0]]]
        for s in seasons[1:]:
            if s == runs[-1][-1] + 1:
                runs[-1].append(s)
            else:
                runs.append([s])
        for run in runs:
            first = LeagueRow(run[0], team).season_label
            last = LeagueRow(run[-1], team).season_label
            span = first if run[0] == run[-1] else f"{first}–{last}"
            out.append(
                {
                    "team_name": team,
                    "season_from": run[0],
                    "season_to": None if run[-1] >= cur else run[-1],
                    "quote": f"{league}-Statistik: {span} {team}",
                }
            )
    return sorted(out, key=lambda x: (x["season_from"], x["team_name"]))


def write_player(
    session: Session,
    athlete: m.Athlete,
    player: LeaguePlayer,
    *,
    source: str,
    league: str,
    country: str,
    dry_run: bool = True,
    today: date | None = None,
) -> dict[str, int]:
    """Add/update SeasonStat rows (source `source`) and CareerStint rows (source "league", this player page)."""
    counts: dict[str, int] = defaultdict(int)
    now = datetime.now(UTC)
    existing = {
        (r.season, r.league, r.team_name, r.phase): r
        for r in session.scalars(
            select(m.SeasonStat).where(m.SeasonStat.athlete_id == athlete.id, m.SeasonStat.source == source)
        )
    }
    seen = set()
    for row in player.rows:
        key = (row.season, league[:50], row.team[:200], row.phase)
        if key in seen:
            continue
        seen.add(key)
        values = {f: getattr(row, f) for f in STAT_FIELDS}
        old = existing.get(key)
        if old is None:
            counts["season_stats_added"] += 1
            if not dry_run:
                session.add(
                    m.SeasonStat(
                        athlete_id=athlete.id,
                        season=key[0],
                        league=key[1],
                        team_name=key[2],
                        team_kind="club",
                        phase=row.phase,
                        source=source,
                        source_url=player.url,
                        fetched_at=now,
                        **values,
                    )
                )
            continue
        changed = {f: v for f, v in values.items() if getattr(old, f) != v}
        if old.source_url != player.url:
            changed["source_url"] = player.url
        if changed:
            counts["season_stats_updated"] += 1
            if not dry_run:
                for f, v in changed.items():
                    setattr(old, f, v)
                old.fetched_at = now

    old_stints = {
        (st.team_name, st.season_from): st
        for st in session.scalars(
            select(m.CareerStint).where(
                m.CareerStint.athlete_id == athlete.id,
                m.CareerStint.source == STINT_SOURCE,
                m.CareerStint.source_url == player.url,
            )
        )
    }
    resolver = None
    for spec in stint_specs(player.rows, league, today):
        key = (spec["team_name"][:200], spec["season_from"])
        if resolver is None and not dry_run:
            from klugscheisser.facts.teams import TeamResolver

            resolver = TeamResolver(session)
        team_id = resolver.resolve(spec["team_name"], country=country) if resolver else None
        fields = {"league": league, "season_to": spec["season_to"], "quote": spec["quote"]}
        if team_id is not None:
            fields["team_id"] = team_id
        old = old_stints.get(key)
        if old is None:
            counts["stints_added"] += 1
            if not dry_run:
                session.add(
                    m.CareerStint(
                        athlete_id=athlete.id,
                        team_name=key[0],
                        kind="club",
                        season_from=key[1],
                        role="player",
                        source=STINT_SOURCE,
                        source_url=player.url,
                        confidence=1.0,
                        status="confirmed",
                        **fields,
                    )
                )
            continue
        changed = {k: v for k, v in fields.items() if getattr(old, k) != v}
        if changed:
            counts["stints_updated"] += 1
            if not dry_run:
                for k, v in changed.items():
                    setattr(old, k, v)
    for k, v in _profile_fields(athlete, player, dry_run).items():
        counts[k] += v
    if not dry_run:
        session.flush()
    return dict(counts)


def _profile_fields(athlete: m.Athlete, player: LeaguePlayer, dry_run: bool) -> dict[str, int]:
    """Only fills empty fields. A published birth date of 1 January is not used (often a placeholder)."""
    counts: dict[str, int] = {}
    if not athlete.position and player.position in ("G", "D", "F"):
        counts["positions_set"] = 1
        if not dry_run:
            athlete.position = player.position
    bd = player.birth_date
    if athlete.birth_date is None and bd and (bd.month, bd.day) != (1, 1):
        counts["birth_dates_set"] = 1
        if not dry_run:
            athlete.birth_date = bd
    return counts


def import_players(
    session: Session,
    athletes: list[m.Athlete],
    players: list[LeaguePlayer],
    *,
    source: str,
    league: str,
    country: str,
    dry_run: bool = True,
    today: date | None = None,
    clubs: dict[str, set[str]] | None = None,
) -> dict:
    """Match `athletes` to the league's `players` and write what matched (`clubs`: normalised name -> roster
    clubs, see `roster_clubs`). Returns counts, `matched`
    (athlete name -> (league team of the running season or None, page URL)) and `skipped` (name: reason)."""
    index = Index(players)
    clubs = clubs or {}
    counts: dict = defaultdict(int)
    matched: dict[str, tuple[str | None, str]] = {}
    skipped: list[str] = []
    cur = current_season(today)
    for a in athletes:
        p, why = index.match(a, clubs.get(normalize(a.full_name)))
        if p is None:
            if why:
                skipped.append(f"{a.full_name}: {why}")
            continue
        counts["players"] += 1
        teams_now = [r.team for r in p.rows if r.season == cur]
        matched[a.full_name] = (teams_now[-1] if teams_now else None, p.url)
        for k, v in write_player(
            session, a, p, source=source, league=league, country=country, dry_run=dry_run, today=today
        ).items():
            counts[k] += v
    counts["matched"] = matched
    counts["skipped"] = skipped
    return dict(counts)


# --- newest dated source wins ---------------------------------------------------------------------------------


def roster_clubs(seeds_dir) -> dict[str, set[str]]:
    """Normalised player name -> clubs written on the project's tournament rosters (seeds `rosters/*.yaml`,
    any entry with `name` and `club`)."""
    from pathlib import Path

    import yaml

    out: dict[str, set[str]] = defaultdict(set)

    def walk(x) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("name"), str) and isinstance(x.get("club"), str) and x["club"].strip():
                out[normalize(x["name"])].add(x["club"].strip())
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for path in sorted(Path(seeds_dir or ".").glob("rosters/*.yaml")):
        try:
            walk(yaml.safe_load(path.read_text(encoding="utf-8")))
        except Exception:  # one broken file must not stop the import
            continue
    return dict(out)


# club-type words of floorball club names ("Nykvarns IF" = "Nykvarns IBF", "Växjö IBK" = "Växjö Vipers IBK")
_CLUB_WORDS = {
    "if", "ibf", "ibk", "ik", "ibs", "ic", "fbc", "ais", "sk", "sb", "fc", "fbk", "sbs", "ry",
    "floorball", "innebandy", "innebandyklubb", "salibandy", "unihockey", "uhc", "hc", "sv", "team", "club",
}  # fmt: skip


def _core(name: str) -> set[str]:
    words = set(team_key(name).split())
    return (words - _CLUB_WORDS) or words


def _same_club(a: str, b: str) -> bool:
    ca, cb = _core(a), _core(b)
    return bool(ca and cb) and (ca <= cb or cb <= ca)


def close_outdated_open_stints(
    session: Session, athlete_ids: list[int], *, dry_run: bool = True
) -> list[str]:
    """An open club station ("seit 2019 bei Mullsjö", from an old article) is outdated when a structured
    season-by-season source (federation / league tables, Elite Prospects) shows the person at another club from
    a later season on. Such a station gets `season_to` = the season before that later station (the quote and
    source stay, nothing is deleted). Returns one line per change."""
    if not athlete_ids:
        return []
    stints = list(
        session.scalars(
            select(m.CareerStint).where(
                m.CareerStint.athlete_id.in_(athlete_ids),
                m.CareerStint.kind == "club",
                m.CareerStint.season_from.is_not(None),
                m.CareerStint.status != "unconfirmed",
            )
        )
    )
    by_athlete: dict[int, list[m.CareerStint]] = defaultdict(list)
    for st in stints:
        by_athlete[st.athlete_id].append(st)
    names = {
        aid: name
        for aid, name in session.execute(
            select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(list(by_athlete)))
        )
    }
    out = []
    for aid, sts in by_athlete.items():
        structured = [x for x in sts if x.source in STRUCTURED]
        for st in sts:
            if st.season_to is not None or st.source in STRUCTURED:
                continue  # a structured source's own open station is its current season: not ours to end
            later = [
                x
                for x in structured
                if x.season_from > st.season_from and not _same_club(x.team_name, st.team_name)
            ]
            if not later:
                continue
            nxt = min(later, key=lambda x: (x.season_from, x.id))
            end = nxt.season_from - 1
            out.append(
                f"{names.get(aid, aid)}: {st.team_name} (seit {st.season_from}, {st.source}) endet {end}"
                f" — später {nxt.team_name} ab {nxt.season_from} ({nxt.source})"
            )
            if not dry_run:
                st.season_to = end
    if not dry_run:
        session.flush()
    return out
