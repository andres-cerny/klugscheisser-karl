"""Swiss Unihockey: player profiles, per-season league stats and national team totals (checked 2026-11-24).

Source: the public website www.swissunihockey.ch. Its pages load their data blocks as HTML from the render engine
`/renderengine/load_view.php?block_type=…` (GET works; robots.txt only blocks CMS folders such as /tools,
/config). Blocks used (all `&locale=de-CH`):

- `block_type=player_details&player_id=N&template=player_detail` -> club, number, position, year of birth,
  height, weight, licence (labels are empty in the HTML; the order is fixed, as in the API docs)
- `block_type=player_statistics&player_id=N` -> "Spielerstatistik Nationalliga": season, league, club, games,
  goals, assists, points, 2'/5'/10'/match penalties (counts)
- `block_type=national_players&selection=1` -> current men's national team pool (national_player_id, no., pos.)
- `block_type=national_player_statistics&national_player_id=N` -> national team career totals
- `block_type=topscorers_su&game_class=11&league=24&amount=all&season=Y` -> L-UPL scorer list (player_id links)
- `block_type=team_players&team_id=N` -> team roster with player_id, position, year of birth

Public page a human can open (the source link): /de/player-detail?player_id=N.
The REST API v2 (api-v2.swissunihockey.ch) needs a partner key on every endpoint and its robots.txt disallows
/api/, so it is not used. There is no name search: players are found through the L-UPL/NLB scorer lists and the
L-UPL team rosters (players abroad are not found unless `federation_id` "su:<player_id>" is set by hand).
national_player_id and player_id are different number spaces (the national player page links a fixed default
player_id, not the player's own), so national players are matched to league players by name.

Gaps: the league table is per league and season as published (Swiss Unihockey does not split regular season and
playoffs there: stored as phase "regular"); penalty minutes = 2·(2') + 5·(5') + 10·(10'), match penalties are
not counted (no fixed minutes).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.floorball.common import (
    CappedHttp,
    RequestCapReached,
    current_season,
    season_start,
    table_rows,
    text_of,
    to_int,
)
from klugscheisser.db import models as m
from klugscheisser.domain.names import german_transliteration, normalize

log = logging.getLogger(__name__)

BASE = "https://www.swissunihockey.ch"
VIEW = f"{BASE}/renderengine/load_view.php"
PLAYER_URL = BASE + "/de/player-detail?player_id={player_id}"
NATIONAL_PLAYER_URL = BASE + "/de/national-player-detail?national_player_id={national_player_id}"
SOURCE = "swissunihockey"  # SeasonStat.source
STINT_SOURCE = "federation"  # CareerStint.source vocabulary
ID_PREFIX = "su:"  # Athlete.federation_id = "su:<player_id>"
MEN = 11  # game_class of men's leagues
LEAGUES = {24: "L-UPL", 2: "NLB"}  # league ids used by the scorer lists (men)
DETAIL_FIELDS = ("club", "number", "position", "birth_year", "height", "weight", "license")
POSITIONS = {"goalie": "G", "torhüter": "G", "verteidiger": "D", "stürmer": "F", "center": "F"}


@dataclass
class PlayerRef:
    """A player as listed somewhere (scorer list, roster, national pool)."""

    player_id: str | None
    name: str
    team: str | None = None
    team_id: str | None = None
    number: str | None = None
    position: str | None = None
    birth_year: int | None = None
    national_player_id: str | None = None


@dataclass
class PlayerProfile:
    player_id: str
    name: str | None
    club: str | None
    number: str | None
    position: str | None  # as written ("Stürmer")
    birth_year: int | None
    height_cm: int | None
    weight_kg: int | None
    license: str | None  # "Herren Aktive GF L-UPL"
    url: str
    portrait_url: str | None = None

    @property
    def position_code(self) -> str | None:
        return POSITIONS.get((self.position or "").lower())


@dataclass
class SeasonRow:
    season: int  # start year (2026 = 2026/27)
    season_label: str  # "2026/27"
    league: str  # "L-UPL", "NLB", "U21 A", "SML"
    team: str
    games: int | None
    goals: int | None
    assists: int | None
    points: int | None
    pen2: int | None
    pen5: int | None
    pen10: int | None
    match_pen: int | None

    @property
    def penalty_minutes(self) -> int | None:
        counts = (self.pen2, self.pen5, self.pen10)
        if all(c is None for c in counts):
            return None
        return 2 * (self.pen2 or 0) + 5 * (self.pen5 or 0) + 10 * (self.pen10 or 0)

    @property
    def youth(self) -> bool:
        return bool(re.match(r"U\s?\d", self.league))


@dataclass
class NationalTotals:
    national_player_id: str
    selection: str  # "Herren - Nationalmannschaft"
    games: int | None
    goals: int | None
    assists: int | None
    points: int | None
    pen2: int | None
    url: str


@dataclass
class PlayerData:
    profile: PlayerProfile
    seasons: list[SeasonRow] = field(default_factory=list)
    national: list[NationalTotals] = field(default_factory=list)


# --- parsing --------------------------------------------------------------------------------------------------


def _id(href: str | None, name: str) -> str | None:
    hit = re.search(rf"[?&]{name}=(\d+)", href or "")
    return hit.group(1) if hit else None


def _dash(v: str | None) -> str | None:
    v = (v or "").strip()
    return None if v in ("", "-") else v


def parse_player_details(page: str, player_id: str) -> PlayerProfile:
    values = re.findall(r"<td(?![^>]*su-strong)[^>]*>(.*?)</td>", page, re.S)
    vals = [_dash(text_of(v)) for v in values][: len(DETAIL_FIELDS)]
    vals += [None] * (len(DETAIL_FIELDS) - len(vals))
    d = dict(zip(DETAIL_FIELDS, vals, strict=True))
    alt = re.search(r"alt=['\"]([^'\"]+?)\s*\(Portrait\)['\"]", page)
    img = re.search(r"<img src=['\"]([^'\"]+)['\"]", page)
    header = re.search(r"su-value-name['\"]?>([^<]*)<", page)
    name = _dash(alt.group(1) if alt else None) or _dash(header.group(1) if header else None)
    return PlayerProfile(
        player_id=str(player_id),
        name=name,
        club=d["club"],
        number=d["number"],
        position=d["position"],
        birth_year=to_int(d["birth_year"]),
        height_cm=to_int(re.sub(r"\D", "", d["height"] or "")),
        weight_kg=to_int(re.sub(r"\D", "", d["weight"] or "")),
        license=d["license"],
        url=PLAYER_URL.format(player_id=player_id),
        portrait_url=img.group(1) if img and "blob.core.windows.net" in img.group(1) else None,
    )


def parse_player_statistics(page: str) -> tuple[str | None, list[SeasonRow]]:
    """(player name from the header, season rows)."""
    head = re.search(r"Spielerstatistik Nationalliga\s*([^<]*)<", page)
    name = " ".join(head.group(1).split()) if head else None
    rows = []
    for cells in table_rows(page):
        t = [c[0] for c in cells]
        if len(t) < 7 or season_start(t[0]) is None:
            continue
        nums = [to_int(x) for x in t[3:11]] + [None] * 8
        rows.append(
            SeasonRow(
                season=season_start(t[0]),
                season_label=t[0],
                league=t[1],
                team=t[2],
                games=nums[0],
                goals=nums[1],
                assists=nums[2],
                points=nums[3],
                pen2=nums[4],
                pen5=nums[5],
                pen10=nums[6],
                match_pen=nums[7],
            )
        )
    return name or None, rows


def parse_national_statistics(page: str, national_player_id: str) -> list[NationalTotals]:
    out = []
    for cells in table_rows(page):
        t = [c[0] for c in cells]
        if len(t) < 5 or to_int(t[1]) is None:
            continue
        out.append(
            NationalTotals(
                national_player_id=str(national_player_id),
                selection=t[0],
                games=to_int(t[1]),
                goals=to_int(t[2]),
                assists=to_int(t[3]),
                points=to_int(t[4]),
                pen2=to_int(t[5]) if len(t) > 5 else None,
                url=NATIONAL_PLAYER_URL.format(national_player_id=national_player_id),
            )
        )
    return out


def parse_player_list(page: str) -> list[PlayerRef]:
    """Scorer list, team roster or national pool -> PlayerRef (whatever columns the block has)."""
    roster_team = re.search(r"Kaderliste\s*([^<]*)<", page)
    out = []
    for cells in table_rows(page):
        name_cell = next(
            (c for c in cells if _id(c[1], "player_id") or _id(c[1], "national_player_id")), None
        )
        # scorer lists link the rank cell too: take the cell whose text is a name
        named = [c for c in cells if (_id(c[1], "player_id") or _id(c[1], "national_player_id"))]
        named = [c for c in named if c[0] and not c[0].isdigit()]
        if named:
            name_cell = named[0]
        if not name_cell or not name_cell[0]:
            continue
        team_cell = next((c for c in cells if _id(c[1], "team_id")), None)
        texts = [c[0] for c in cells]
        pos = next((x for x in texts if x.lower() in POSITIONS), None)
        year = next((int(x) for x in texts if re.fullmatch(r"(19|20)\d\d", x)), None)
        number = (
            texts[0]
            if re.fullmatch(r"\d{1,3}|-", texts[0] or "") and not _id(cells[0][1], "player_id")
            else None
        )
        out.append(
            PlayerRef(
                player_id=_id(name_cell[1], "player_id"),
                national_player_id=_id(name_cell[1], "national_player_id"),
                name=name_cell[0],
                team=team_cell[0]
                if team_cell
                else (" ".join(roster_team.group(1).split()) if roster_team else None),
                team_id=_id(team_cell[1], "team_id") if team_cell else None,
                number=_dash(number),
                position=pos,
                birth_year=year,
            )
        )
    return out


# --- fetching -------------------------------------------------------------------------------------------------


def _view(http: CappedHttp, **params) -> str:
    return http.text(VIEW, {**params, "locale": "de-CH"})


def fetch_player(http: CappedHttp, player_id: str, *, with_details: bool = True) -> PlayerData:
    """Profile (1 request) and season stats (1 request) of a league player."""
    profile = None
    if with_details:
        profile = parse_player_details(
            _view(http, player_id=player_id, template="player_detail", block_type="player_details"), player_id
        )
    name, rows = parse_player_statistics(_view(http, player_id=player_id, block_type="player_statistics"))
    if profile is None:
        profile = PlayerProfile(
            player_id, name, None, None, None, None, None, None, None, PLAYER_URL.format(player_id=player_id)
        )
    profile.name = profile.name or name
    return PlayerData(profile=profile, seasons=rows)


def fetch_national_pool(http: CappedHttp, selection: int = 1) -> list[PlayerRef]:
    """Current national team pool (selection 1 = men)."""
    return parse_player_list(_view(http, selection=selection, block_type="national_players"))


def fetch_national_totals(http: CappedHttp, national_player_id: str) -> list[NationalTotals]:
    page = _view(http, national_player_id=national_player_id, block_type="national_player_statistics")
    return parse_national_statistics(page, national_player_id)


def fetch_scorers(http: CappedHttp, league: int = 24, season: int | None = None) -> list[PlayerRef]:
    params = {
        "tabs": "off",
        "game_class": MEN,
        "league": league,
        "amount": "all",
        "block_type": "topscorers_su",
    }
    if season:
        params["season"] = season
    return parse_player_list(_view(http, **params))


def fetch_roster(http: CappedHttp, team_id: str) -> list[PlayerRef]:
    return parse_player_list(_view(http, team_id=team_id, block_type="team_players"))


# --- finding players ------------------------------------------------------------------------------------------


def _keys(name: str) -> set[str]:
    n = normalize(name)
    out = {n, normalize(german_transliteration(name))}
    parts = n.split()
    if len(parts) >= 2:
        out.add(" ".join(reversed(parts)))  # "Zaugg Jan"
        out.add(f"{parts[0]} {parts[-1]}")  # "Matteo Nico Steiner" -> "matteo steiner"
    return out


class Directory:
    """Name -> league PlayerRefs, filled lazily: scorer lists first, then L-UPL rosters (for goalies)."""

    def __init__(self, http: CappedHttp, seasons: tuple[int, ...] | None = None) -> None:
        self.http = http
        cur = current_season()
        self.seasons = seasons or (cur, cur - 1)
        self.by_name: dict[str, dict[str, PlayerRef]] = defaultdict(dict)
        self._steps = self._plan()
        self.team_ids: set[str] = set()  # L-UPL teams seen in the L-UPL scorer lists

    def _plan(self):
        for season in self.seasons:
            yield ("scorers", 24, season)
        yield ("scorers", 2, self.seasons[0])
        yield ("rosters", None, None)

    def _add(self, refs: list[PlayerRef], top_league: bool = False) -> None:
        for r in refs:
            if not r.player_id:
                continue
            if r.team_id and top_league:
                self.team_ids.add(r.team_id)
            for k in _keys(r.name):
                old = self.by_name[k].get(r.player_id)
                if old is None or (r.birth_year and not old.birth_year):
                    self.by_name[k][r.player_id] = r

    def _step(self) -> bool:
        try:
            kind, league, season = next(self._steps)
        except StopIteration:
            return False
        if kind == "scorers":
            self._add(
                fetch_scorers(self.http, league, season),
                top_league=league == 24 and season == self.seasons[0],
            )
        else:
            for team_id in sorted(self.team_ids):
                self._add(fetch_roster(self.http, team_id))
        return True

    def find(self, name: str, birth_year: int | None = None) -> PlayerRef | None:
        """The one player of that name (and birth year, when known). Fetches more lists until found or done.
        None when not found or ambiguous."""
        while True:
            hit = self._lookup(name, birth_year)
            if hit is not None:
                return hit
            if not self._step():
                return None

    def _lookup(self, name: str, birth_year: int | None) -> PlayerRef | None:
        cands: dict[str, PlayerRef] = {}
        for k in _keys(name):
            cands.update(self.by_name.get(k, {}))
        if birth_year:
            same = {i: r for i, r in cands.items() if r.birth_year in (None, birth_year)}
            cands = same
        return next(iter(cands.values())) if len(cands) == 1 else None


# --- writing --------------------------------------------------------------------------------------------------

STAT_FIELDS = ("games", "goals", "assists", "points", "penalty_minutes")


def stint_specs(rows: list[SeasonRow], today: date | None = None) -> list[dict]:
    """Runs of consecutive seasons at one club (club / youth separately) -> career stint dicts."""
    cur = current_season(today)
    by_team: dict[tuple[str, str], list[SeasonRow]] = defaultdict(list)
    for r in rows:
        by_team[(r.team, "youth" if r.youth else "club")].append(r)
    out = []
    for (team, kind), rs in by_team.items():
        seasons = sorted({r.season for r in rs})
        runs: list[list[int]] = [[seasons[0]]]
        for s in seasons[1:]:
            if s == runs[-1][-1] + 1:
                runs[-1].append(s)
            else:
                runs.append([s])
        for run in runs:
            leagues = []
            for r in sorted(rs, key=lambda x: x.season):
                if run[0] <= r.season <= run[-1] and r.league not in leagues:
                    leagues.append(r.league)
            first = next(r.season_label for r in rs if r.season == run[0])
            last = next(r.season_label for r in rs if r.season == run[-1])
            span = first if run[0] == run[-1] else f"{first}–{last}"
            out.append(
                {
                    "team_name": team,
                    "kind": kind,
                    "league": ", ".join(leagues),
                    "season_from": run[0],
                    "season_to": None if run[-1] >= cur else run[-1],
                    "quote": f"Spielerstatistik Nationalliga: {span} {', '.join(leagues)} {team}",
                }
            )
    return sorted(out, key=lambda x: (x["season_from"], x["kind"], x["team_name"]))


def write_player(
    session: Session, athlete: m.Athlete, data: PlayerData, *, dry_run: bool = True, today: date | None = None
) -> dict[str, int]:
    """Upsert SeasonStat (source "swissunihockey") and CareerStint (source "federation", this player page only)
    rows of one athlete. Rows of these sources for the same page that are gone at the source are removed."""
    counts = defaultdict(int)
    url = data.profile.url
    now = datetime.now(UTC)
    existing = {
        (r.season, r.league, r.team_name, r.phase): r
        for r in session.scalars(
            select(m.SeasonStat).where(m.SeasonStat.athlete_id == athlete.id, m.SeasonStat.source == SOURCE)
        )
    }
    seen = set()
    for row in data.seasons:
        key = (row.season, row.league[:50], row.team[:200], "regular")
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
                        team_kind="youth" if row.youth else "club",
                        phase="regular",
                        source=SOURCE,
                        source_url=url,
                        fetched_at=now,
                        **values,
                    )
                )
            continue
        changed = {f: v for f, v in values.items() if getattr(old, f) != v}
        if old.source_url != url:
            changed["source_url"] = url
        if changed:
            counts["season_stats_updated"] += 1
            if not dry_run:
                for f, v in changed.items():
                    setattr(old, f, v)
                old.fetched_at = now
    for key, old in existing.items():
        if key not in seen and old.source_url == url:
            counts["season_stats_removed"] += 1
            if not dry_run:
                session.delete(old)

    specs = stint_specs(data.seasons, today)
    old_stints = {
        (st.team_name, st.kind, st.season_from): st
        for st in session.scalars(
            select(m.CareerStint).where(
                m.CareerStint.athlete_id == athlete.id,
                m.CareerStint.source == STINT_SOURCE,
                m.CareerStint.source_url == url,
            )
        )
    }
    resolver = None
    seen_stints = set()
    for spec in specs:
        key = (spec["team_name"][:200], spec["kind"], spec["season_from"])
        seen_stints.add(key)
        old = old_stints.get(key)
        if resolver is None and not dry_run:
            from klugscheisser.facts.teams import TeamResolver

            resolver = TeamResolver(session)
        team_id = (
            resolver.resolve(spec["team_name"], country="SUI", youth=spec["kind"] == "youth")
            if resolver
            else None
        )
        fields = {
            "league": spec["league"][:100] or None,
            "season_to": spec["season_to"],
            "quote": spec["quote"],
            "team_id": team_id,
        }
        if old is None:
            counts["stints_added"] += 1
            if not dry_run:
                session.add(
                    m.CareerStint(
                        athlete_id=athlete.id,
                        team_name=key[0],
                        kind=spec["kind"],
                        season_from=spec["season_from"],
                        role="player",
                        source=STINT_SOURCE,
                        source_url=url,
                        confidence=1.0,
                        status="confirmed",
                        **fields,
                    )
                )
            continue
        changed = {
            k: v for k, v in fields.items() if getattr(old, k) != v and not (k == "team_id" and v is None)
        }
        if changed:
            counts["stints_updated"] += 1
            if not dry_run:
                for k, v in changed.items():
                    setattr(old, k, v)
    for key, old in old_stints.items():
        if key not in seen_stints:
            counts["stints_removed"] += 1
            if not dry_run:
                session.delete(old)
    if not dry_run:
        session.flush()
    return dict(counts)


def _set_profile_fields(athlete: m.Athlete, data: PlayerData, dry_run: bool) -> dict[str, int]:
    """Only fills empty fields: federation_id ("su:<player_id>") and position."""
    counts = defaultdict(int)
    fid = ID_PREFIX + data.profile.player_id
    if not athlete.federation_id:
        counts["federation_ids_set"] += 1
        if not dry_run:
            athlete.federation_id = fid
    if not athlete.position and data.profile.position_code:
        counts["positions_set"] += 1
        if not dry_run:
            athlete.position = data.profile.position_code
    return dict(counts)


def import_for_athletes(
    session: Session,
    athletes: list[m.Athlete],
    *,
    dry_run: bool = True,
    max_requests: int = 200,
    http: CappedHttp | None = None,
    today: date | None = None,
) -> dict:
    """Find each athlete at Swiss Unihockey (federation_id "su:<id>" or by name + birth year) and import profile
    fields, season stats and career stints. Returns counts plus `not_found` / `national_totals` (not stored:
    career totals have no season). Never raises on network errors; stops at the request cap."""
    http = http or CappedHttp(max_requests=max_requests)
    directory = Directory(http)
    counts: dict = defaultdict(int)
    not_found: list[str] = []
    found: dict[int, str] = {}
    try:
        for a in athletes:
            pid = a.federation_id[len(ID_PREFIX) :] if (a.federation_id or "").startswith(ID_PREFIX) else None
            if pid is None:
                ref = directory.find(a.full_name, a.birth_date.year if a.birth_date else None)
                pid = ref.player_id if ref else None
            if pid is None:
                not_found.append(a.full_name)
                continue
            found[a.id] = pid
            try:
                data = fetch_player(http, pid)
            except RequestCapReached:
                raise
            except Exception as exc:  # one bad page must not stop the run
                log.warning("swissunihockey: player %s failed: %s", pid, exc)
                counts["errors"] += 1
                continue
            if a.birth_date and data.profile.birth_year and data.profile.birth_year != a.birth_date.year:
                counts["birth_year_mismatch"] += 1
                not_found.append(f"{a.full_name} (Jahrgang {data.profile.birth_year} passt nicht)")
                continue
            counts["players"] += 1
            for k, v in _set_profile_fields(a, data, dry_run).items():
                counts[k] += v
            for k, v in write_player(session, a, data, dry_run=dry_run, today=today).items():
                counts[k] += v
    except RequestCapReached as exc:
        counts["stopped"] = str(exc)
    except Exception as exc:
        log.warning("swissunihockey: run failed: %s", exc)
        counts["errors"] += 1
    counts["requests"] = http.requests
    counts["not_found"] = not_found
    counts["found"] = found
    return dict(counts)


def national_totals_for(
    http: CappedHttp, names: list[str], selection: int = 1
) -> dict[str, list[NationalTotals]]:
    """National team career totals (games/goals/assists/points) of the current pool players among `names`.
    1 + one request per matched player. Not written to the database (no season)."""
    pool = fetch_national_pool(http, selection)
    wanted = {k: n for n in names for k in _keys(n)}
    out = {}
    for ref in pool:
        hit = next((wanted[k] for k in _keys(ref.name) if k in wanted), None)
        if hit and ref.national_player_id:
            out[hit] = fetch_national_totals(http, ref.national_player_id)
    return out
