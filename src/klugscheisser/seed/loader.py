"""Load curated YAML seeds and Wikidata players into the database (idempotent)."""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import staff
from klugscheisser.collectors.wikidata import BulkCount, WikidataCollector, WikidataPlayer, split_active
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.names import normalize
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.sports import COUNTRY_QIDS, citizenship_qids, current_spec, get_sport, national_team_key

SEEDS_DIR = Path("seeds")
log = logging.getLogger(__name__)


@dataclass
class SeedStats:
    teams: int = 0
    events: int = 0
    athletes: int = 0
    entries: int = 0
    handles: int = 0


def team_key(value: str) -> str:
    """A team in a seed: its key ("sui-women", "zsc-lions") or a nation code ("SUI" -> the project's national
    team key, "sui-men" in the floorball project)."""
    v = str(value).strip()
    return national_team_key(v) if re.fullmatch(r"[A-Za-z]{3}", v) else v


def _dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


NATIONAL_TEAM = re.compile(r"national|nationalmannschaft|nationalteam|landslag|maajoukkue|reprezentace", re.I)
WOMEN_WORD = re.compile(r"women|frauen|damen|dam|nais|žen|femmin|fémin", re.I)


def _team_by_name(
    session: Session,
    sport: m.Sport,
    name: str,
    league: str | None,
    country: str | None,
    default_country="SUI",
):
    team = repo.find_team(session, name, fuzzy=False)
    if team is None and NATIONAL_TEAM.search(name) and country:
        # "Switzerland women's national ice hockey team" is our national team, not a new club
        women = bool(WOMEN_WORD.search(name))
        # 2026-11-24: in a men's sport (floorball_men) the national team is the one without a women's word
        ours = (not women) if sport.key.endswith("_men") else (women or sport.key.endswith("_women"))
        team = (
            session.scalar(
                select(m.Team).where(
                    m.Team.kind == "national", m.Team.country == country, m.Team.sport_id == sport.id
                )
            )
            if ours
            else None
        )
    if team is None:
        team = m.Team(
            sport_id=sport.id,
            key=normalize(name).replace(" ", "-")[:80],
            name=name,
            kind="club",
            country=country or default_country,
            league=league,
        )
        session.add(team)
        session.flush()
    return team


def load_base(session: Session, path: Path = SEEDS_DIR / "teams.yaml") -> SeedStats:
    data = yaml.safe_load(path.read_text())
    spec = get_sport(data["sport"])
    sport = repo.get_sport(session, spec.key, spec.name_de)
    stats = SeedStats()
    comps: dict[str, m.Competition] = {}
    for c in data.get("competitions", []):
        comp = session.scalar(select(m.Competition).where(m.Competition.key == c["key"]))
        if comp is None:
            comp = m.Competition(sport_id=sport.id, key=c["key"], name=c["name"], kind=c["kind"])
            session.add(comp)
        for field in ("name", "kind", "season", "country", "source_url"):
            if field in c:
                setattr(comp, field, c[field])
        comps[c["key"]] = comp
    for t in data.get("teams", []):
        team = repo.get_team(session, t["key"])
        if team is None:
            team = m.Team(sport_id=sport.id, key=t["key"], name=t["name"])
            session.add(team)
            stats.teams += 1
        for field in ("name", "short_name", "kind", "country", "league", "instagram_handle", "website"):
            if field in t:
                setattr(team, field, t[field])
    session.flush()
    for e in data.get("events", []):
        event = session.scalar(select(m.Event).where(m.Event.key == e["key"]))
        if event is None:
            event = m.Event(key=e["key"], name=e["name"], kind="game", status="scheduled")
            session.add(event)
            stats.events += 1
        # only what the seed states: values the results pipeline / event prep filled later (competition link,
        # end date, final status, venue …) stay when the seed leaves them out
        event.name = e["name"]
        if e.get("kind"):
            event.kind = e["kind"]
        if e.get("competition") in comps:
            event.competition_id = comps[e["competition"]].id
        for field in ("starts_at", "ends_at"):
            if e.get(field):
                setattr(event, field, _dt(e[field]))
        for field in ("city", "venue", "status", "source_url"):
            if e.get(field):
                setattr(event, field, e[field])
        session.flush()
        for group, keys in (e.get("teams") or {}).items():
            for key in keys:
                team = repo.get_team(session, key)
                link = session.get(m.EventTeam, (event.id, team.id))
                if link is None:
                    session.add(m.EventTeam(event_id=event.id, team_id=team.id, group_name=group))
    session.flush()
    return stats


def _club_by_alias(session: Session, name: str) -> m.Team | None:
    """Club named in a roster ("EV Zug") -> our team via seeds/team_aliases.yaml ("EVZ Women's Team")."""
    q = normalize(name)
    for key, aliases in load_team_aliases(session).items():
        if any(normalize(a.alias) == q for a in aliases if not a.ambiguous):
            return repo.get_team(session, key)
    return None


def _national_affiliation(session: Session, athlete: m.Athlete, team: m.Team, data: dict) -> None:
    if team.kind != "national" or not data.get("season"):
        return
    exists = session.scalar(
        select(m.Affiliation).where(
            m.Affiliation.athlete_id == athlete.id,
            m.Affiliation.team_id == team.id,
            m.Affiliation.season == data["season"],
        )
    )
    if exists is None:
        session.add(
            m.Affiliation(
                athlete_id=athlete.id,
                team_id=team.id,
                season=data["season"],
                source=data.get("source"),
                source_url=data.get("source_url"),
            )
        )


def _latest_number(session: Session, athlete_id: int, team_id: int) -> int | None:
    return session.scalar(
        select(m.EventEntry.jersey_number)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .where(
            m.EventEntry.athlete_id == athlete_id,
            m.EventEntry.team_id == team_id,
            m.EventEntry.jersey_number.is_not(None),
        )
        .order_by(m.Event.starts_at.desc().nulls_last())
        .limit(1)
    )


def _captain_fact(
    session: Session, athlete: m.Athlete, mark: str, event: m.Event, team: m.Team, url: str | None
) -> None:
    text = f"{'Captain' if mark == 'C' else 'Assistant-Captain'} ({team.name}, {event.name})"
    exists = session.scalar(
        select(m.Fact.id).where(
            m.Fact.athlete_id == athlete.id, m.Fact.kind == "captaincy", m.Fact.value_text == text
        )
    )
    if exists is None and url:
        session.add(
            m.Fact(
                athlete_id=athlete.id,
                kind="captaincy",
                value={"text": text, "mark": mark, "team": team.name, "event": event.name},
                value_text=text,
                source="roster",
                source_url=url,
                extractor="roster",
            )
        )


WATCH_SOURCE = "Kaderwatch"  # source prefix of rosters loaded by jobs/roster_watch.py (official lists)
# 2026-11-04: source prefix of players added from a game sheet (jobs/game_rosters.py, late additions): a later list
# without them never removes them (only a person does)
GAME_SHEET_SOURCE = "IIHF-Spielbericht"
INJURY_WORDS = re.compile(r"verletz|injur|krank|ill\b|loukkaan|skad|zraněn", re.I)


def _watch_confirmed(session: Session, event: m.Event, team: m.Team) -> bool:
    """True when the roster watcher already stored the official list for this team: a provisional seed must not
    replace it."""
    return (
        session.scalar(
            select(m.EventEntry.id)
            .where(
                m.EventEntry.event_id == event.id,
                m.EventEntry.team_id == team.id,
                m.EventEntry.list_type == "confirmed",
                m.EventEntry.source.like(f"{WATCH_SOURCE}%"),
            )
            .limit(1)
        )
        is not None
    )


def _seed_newer_than_watch(session: Session, event: m.Event, team: m.Team, data: dict) -> bool:
    """A confirmed seed dated after the watcher's last official list for this team replaces it (e.g. a hand-checked
    correction); provisional seeds never do."""
    if data.get("list_type", "provisional") != "confirmed" or not data.get("source_date"):
        return False
    row = session.get(m.SourceState, "roster_watch")
    applied = (((row.state or {}) if row is not None else {}).get("applied") or {}).get(
        team.country or ""
    ) or {}
    seen = applied.get("date")
    return bool(seen) and str(data["source_date"]) > str(seen)


def _absence_fact(session: Session, athlete: m.Athlete, p: dict, event: m.Event, data: dict) -> None:
    """An injury named in a roster announcement becomes an `injury` fact (always Vorsicht) with the quote; other
    absences (no reason given) stay in the roster seed only (the tournament answer lists them)."""
    url = p.get("source_url") or data.get("source_url")
    reason = p.get("reason") or ""
    if not url or not INJURY_WORDS.search(f"{reason} {p.get('quote') or ''}"):
        return
    text = f"{reason} (fehlt im Aufgebot: {event.name})"
    exists = session.scalar(
        select(m.Fact.id).where(
            m.Fact.athlete_id == athlete.id, m.Fact.kind == "injury", m.Fact.value_text == text
        )
    )
    if exists is None:
        session.add(
            m.Fact(
                athlete_id=athlete.id,
                kind="injury",
                value={"text": text, "date": data.get("source_date"), "status": reason},
                value_text=text,
                source="roster",
                source_url=url,
                quote=p.get("quote"),
                extractor="roster",
            )
        )


def load_roster(session: Session, path: Path) -> SeedStats:
    """One roster seed: players (number, position, birth date, club, captain) and staff (head coach …).
    `based_on: <event>` without players copies that event's roster (provisional lists); `also_called_up`
    lists persons who were named but are not on the final list (national-team affiliation only)."""
    return load_roster_data(session, yaml.safe_load(path.read_text()), path.name)


def load_roster_data(session: Session, data: dict, name: str = "roster") -> SeedStats:
    """`load_roster` for an already parsed roster (also used by jobs/roster_watch.py for official lists)."""
    stats = SeedStats()
    spec = current_spec()
    sport = repo.get_sport(session, spec.key, spec.name_de)
    event = session.scalar(select(m.Event).where(m.Event.key == data["event"]))
    team = repo.get_team(session, team_key(data["team"]))
    if event is None or team is None:
        log.warning("roster %s: unknown event or team", name)
        return stats
    if session.get(m.EventTeam, (event.id, team.id)) is None:
        session.add(m.EventTeam(event_id=event.id, team_id=team.id))
    if _watch_confirmed(session, event, team) and not _seed_newer_than_watch(session, event, team, data):
        # 2026-10-28: also a confirmed seed: the watcher's official list (or its later replacement) stays
        log.info("roster %s: official roster already loaded by the roster watcher, seed skipped", name)
        return stats
    source_url = data.get("source_url")
    players = data.get("players") or []
    if data.get("based_on") and not players:
        base = session.scalar(select(m.Event).where(m.Event.key == data["based_on"]))
        base_entries = session.scalars(
            select(m.EventEntry).where(m.EventEntry.event_id == base.id, m.EventEntry.team_id == team.id)
        ).all()
        players = [
            {
                "athlete_id": be.athlete_id,
                "pos": be.role,
                "number": be.jersey_number or _latest_number(session, be.athlete_id, team.id),
            }
            for be in base_entries
            if not staff.is_staff_entry(be.role)
        ]
        if not data.get("staff"):  # v19: the provisional list also takes the base list's staff
            data = {
                **data,
                "staff": [
                    {"athlete_id": be.athlete_id, "role": be.role, "source_url": be.source_url}
                    for be in base_entries
                    if staff.is_staff_entry(be.role)
                ],
            }
    national = team.country if team.kind == "national" else None

    loaded: set[int] = set()

    def person(p: dict, nationality: str | None, **fields) -> m.Athlete:
        if "athlete_id" in p:
            return session.get(m.Athlete, p["athlete_id"])
        birth = date.fromisoformat(str(p["birth"])) if p.get("birth") else None
        stats.athletes += 1
        return repo.upsert_athlete(
            session,
            sport,
            p["name"],
            birth_date=birth,
            nationality=nationality,
            wikidata_qid=p.get("qid"),
            **fields,
        )

    def entry_for(athlete: m.Athlete) -> m.EventEntry:
        entry = session.scalar(
            select(m.EventEntry).where(
                m.EventEntry.event_id == event.id, m.EventEntry.athlete_id == athlete.id
            )
        )
        if entry is None:
            entry = m.EventEntry(event_id=event.id, athlete_id=athlete.id)
            session.add(entry)
            stats.entries += 1
        loaded.add(athlete.id)
        entry.team_id = team.id
        entry.list_type = data.get("list_type", "provisional")
        entry.source = data.get("source")
        entry.source_url = source_url
        return entry

    for p in players:
        athlete = person(p, p.get("nationality") or national, position=p.get("pos"))
        if p.get("club"):
            club = _club_by_alias(session, p["club"]) or _team_by_name(
                session, sport, p["club"], p.get("league"), p.get("country")
            )
            aff = session.scalar(
                select(m.Affiliation).where(
                    m.Affiliation.athlete_id == athlete.id,
                    m.Affiliation.team_id == club.id,
                    m.Affiliation.season == data.get("season"),
                )
            )
            if aff is None:
                session.add(
                    m.Affiliation(
                        athlete_id=athlete.id,
                        team_id=club.id,
                        season=data.get("season"),
                        source=data.get("source"),
                        source_url=source_url,
                    )
                )
        _national_affiliation(session, athlete, team, data)
        entry = entry_for(athlete)
        entry.role = p.get("pos")
        entry.jersey_number = p.get("number")
        if p.get("captain") in ("C", "A") and event.name:
            _captain_fact(session, athlete, p["captain"], event, team, source_url)
    seen = staff.as_date(data.get("source_date"))
    for p in data.get("staff") or []:
        # staff: nationality only when the source gives it (a coach often comes from another country)
        kind = "coach" if staff.is_coach(p.get("role")) else "staff"
        athlete = person(p, p.get("nationality"), role=kind)
        if athlete.role == "athlete":  # a former player now coaching: the person stays one row
            athlete.role = kind
        _national_affiliation(session, athlete, team, data)
        entry = entry_for(athlete)
        entry.role = (p.get("role") or "Staff")[:30]  # column width; the seed keeps the full title
        entry.jersey_number = None
        if p.get("source_url"):  # v19: staff from another document than the players (SIHF roster PDF)
            entry.source_url = p["source_url"]
        if p.get("source"):  # v19: staff kept from an earlier list (roster watcher) keeps its own source
            entry.source = p["source"]
        entry.list_type = p.get("list_type") or entry.list_type
        # v19: every staff line is also evidence for the team's staff history (staff.py)
        staff.record(
            session,
            team,
            athlete,
            p.get("role"),
            source=entry.source,
            source_url=entry.source_url,
            seen=seen,
        )
    for p in data.get("also_called_up") or []:
        _national_affiliation(session, person(p, national, position=p.get("pos")), team, data)
    for p in data.get("absent") or []:  # named as injured/absent in the announcement: not on the list
        athlete = person(p, national, position=p.get("pos"))
        _national_affiliation(session, athlete, team, data)
        _absence_fact(session, athlete, p, event, data)
    # the seed is the roster: entries of this team that are no longer on it are removed (2026-10-28: and kept in
    # the roster history, so "nicht mehr im Kader" can be said with the list that replaced them)
    removed = [
        old
        for old in session.scalars(
            select(m.EventEntry).where(m.EventEntry.event_id == event.id, m.EventEntry.team_id == team.id)
        )
        if old.athlete_id not in loaded and not (old.source or "").startswith(GAME_SHEET_SOURCE)
    ]
    record_removed(session, event, team, removed, data)
    for old in removed:
        session.delete(old)
    session.flush()
    return stats


HISTORY = "roster_history"  # source_state row: entries removed from an event roster, per "<event>/<team>"


def roster_history(session: Session, event: m.Event, team: m.Team) -> list[dict]:
    row = session.get(m.SourceState, HISTORY)
    return list(((row.state or {}) if row is not None else {}).get(f"{event.key}/{team.key}", []))


def record_removed(
    session: Session, event: m.Event, team: m.Team, removed: list[m.EventEntry], data: dict
) -> None:
    """Remember roster entries that a new list removed (who, with which list, replaced by which source and when).
    Athletes are never deleted; only their event entry goes."""
    if not removed:
        return
    import copy

    from sqlalchemy.orm.attributes import flag_modified

    row = session.get(m.SourceState, HISTORY)
    if row is None:
        row = m.SourceState(source=HISTORY, state={})
        session.add(row)
    state = copy.deepcopy(row.state or {})
    items = state.setdefault(f"{event.key}/{team.key}", [])
    known = {(i.get("athlete_id"), i.get("replaced_by")) for i in items}
    for e in removed:
        if (e.athlete_id, data.get("source")) in known:
            continue
        items.append(
            {
                "athlete_id": e.athlete_id,
                "name": e.athlete.full_name if e.athlete else None,
                "role": e.role,
                "number": e.jersey_number,
                "list_type": e.list_type,
                "source": e.source,
                "source_url": e.source_url,
                "removed": date.today().isoformat(),
                "replaced_by": data.get("source"),
                "replaced_by_url": data.get("source_url"),
                "replaced_by_date": str(data["source_date"]) if data.get("source_date") else None,
                "new_list_type": data.get("list_type", "provisional"),
            }
        )
    state[f"{event.key}/{team.key}"] = items[-300:]
    row.state = state
    flag_modified(row, "state")


def load_all_seeds(session: Session, seeds_dir: Path = SEEDS_DIR) -> SeedStats:
    from klugscheisser.domain.source_kind import load_accounts

    total = load_base(session, seeds_dir / "teams.yaml")
    if (seeds_dir / "accounts.yaml").exists():
        load_accounts(session, seeds_dir / "accounts.yaml")
    rosters = sorted(
        (seeds_dir / "rosters").glob("*.yaml"),
        key=lambda p: bool(yaml.safe_load(p.read_text()).get("based_on")),
    )
    for path in rosters:
        s = load_roster(session, path)
        total.athletes += s.athletes
        total.entries += s.entries
    for path in sorted((seeds_dir / "events").glob("*.yaml")):
        total.events += load_event_info(session, path)
    # v19: staff appointments with dates (seeds/staff), then staff lines of every stored roster
    staff.load_seeds(session, seeds_dir / "staff")
    staff.sync_from_entries(session)
    return total


def load_event_info(session: Session, path: Path) -> int:
    """seeds/events/<event>.yaml: the tournament's games become `game` events. Keys are the ones the results
    collector uses ("iihf-<GameId>"), so the results pipeline later updates the same rows with scores; a game the
    collector already stored as live/final keeps its status and score. Returns the number of new games."""
    from klugscheisser.collectors.results.iihf import COMPETITION, IihfEvent, team_name
    from klugscheisser.tournament import NATION_DE, _read

    data = _read(str(path.resolve()), path.stat().st_mtime)  # also the floorball research format, normalized
    event = session.scalar(select(m.Event).where(m.Event.key == data.get("event")))
    if event is None:
        log.warning("event info %s: unknown event %s", path.name, data.get("event"))
        return 0
    comp_id = event.competition_id
    if comp_id is None:
        comp_id = session.scalar(select(m.Competition.id).where(m.Competition.name == COMPETITION))
    iihf = IihfEvent()  # game-centre links of WW 2026 (the event this collector is configured for)
    new = 0
    for g in data.get("games") or []:
        ev = session.scalar(select(m.Event).where(m.Event.key == g["key"]))
        if ev is None:
            ev = m.Event(key=g["key"], kind="game", name="", status="scheduled")
            session.add(ev)
            new += 1
        home, away = (
            repo.get_team(session, team_key(g["home"])),
            repo.get_team(session, team_key(g["away"])),
        )
        ev.kind, ev.competition_id = "game", ev.competition_id or comp_id
        ev.name = ev.name or " – ".join(
            n if (n := team_name(c)) != c else NATION_DE.get(c, c) for c in (g["home"], g["away"])
        )
        ev.starts_at = ev.starts_at or _dt(g["utc"].replace("Z", "+00:00"))
        ev.venue = ev.venue or g.get("venue")
        ev.city = ev.city or g.get("city") or _city(g.get("venue"))
        ev.home_team_id = ev.home_team_id or (home.id if home else None)
        ev.away_team_id = ev.away_team_id or (away.id if away else None)
        # 2026-11-24: other sports name their own source per game or in the file (floorball: the IFF)
        ev.source = ev.source or (g.get("source") or data.get("games_source") or "IIHF")[:30]
        ev.source_url = (
            ev.source_url
            or g.get("url")
            or (
                iihf.game_url(int(g["nr"]), g["home"], g["away"])
                if g.get("nr") and str(g["key"]).startswith("iihf-")
                else event.source_url
            )
        )
    session.flush()
    return new


def _city(venue: str | None) -> str | None:
    for city in ("Herning", "Esbjerg"):
        if venue and city in venue:
            return city
    return None


@dataclass(frozen=True)
class TeamAlias:
    alias: str
    ambiguous: bool = False  # city / short name: only with an ice hockey context
    women_specific: bool = False  # national alias that names the women's team without a women's word


@functools.lru_cache(maxsize=4)
def _read_team_aliases(path: str, mtime: float) -> dict[str, tuple[TeamAlias, ...]]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    out: dict[str, tuple[TeamAlias, ...]] = {}
    for key, entries in (data.get("teams") or {}).items():
        aliases = []
        for e in entries or []:
            if isinstance(e, str):
                aliases.append(TeamAlias(e))
            else:
                aliases.append(
                    TeamAlias(
                        str(e["alias"]),
                        bool(e.get("ambiguous", False)),
                        bool(e.get("women_specific", False)),
                    )
                )
        out[key] = tuple(aliases)
    return out


def load_team_aliases(
    session: Session | None = None, path: Path | None = None
) -> dict[str, tuple[TeamAlias, ...]]:
    """Team key -> explicit aliases from YAML (cached per file version; nothing is stored in the DB).
    Default file: the current project's `<seeds_dir>/team_aliases.yaml` (seeds/team_aliases.yaml for eishockey).

    With a session, team keys that are not in the DB are logged and dropped.
    """
    if path is None:
        from klugscheisser import projects

        path = Path(projects.current().seeds_dir) / "team_aliases.yaml"
    if not path.exists():
        return {}
    aliases = _read_team_aliases(str(path.resolve()), path.stat().st_mtime)
    if session is None:
        return dict(aliases)
    known = set(session.scalars(select(m.Team.key).where(m.Team.key.in_(list(aliases)))))
    for key in sorted(set(aliases) - known):
        log.warning("team_aliases.yaml: unknown team key %s", key)
    return {k: v for k, v in aliases.items() if k in known}


def _namesake_elsewhere(session: Session, sport: m.Sport, p: WikidataPlayer) -> bool:
    """True when the only exact-name persons in the DB are someone else: another QID, another sport or
    another birth date. Then the Wikidata person is created as a new person instead of being merged."""
    if session.scalar(select(m.Athlete).where(m.Athlete.wikidata_qid == p.qid)) is not None:
        return False
    exact = [x.athlete for x in repo.find_athletes(session, p.name) if x.how == "exact"]
    fits = [
        a
        for a in exact
        if a.wikidata_qid in (None, p.qid)
        and a.sport_id == sport.id
        and (p.birth_date is None or a.birth_date in (None, p.birth_date))
    ]
    return bool(exact) and not fits


def load_wikidata_players(
    session: Session, players: list[WikidataPlayer], sport_key: str = "icehockey_women"
) -> SeedStats:
    """Persons from Wikidata with their handles (evidence "wikidata") and current memberships. A person is
    merged with an existing one by QID, or by exact name in the same sport without a conflicting QID or birth
    date; a namesake (other QID, sport or birth date) becomes a separate person."""
    stats = SeedStats()
    spec = get_sport(sport_key)
    sport = repo.get_sport(session, spec.key, spec.name_de)
    for p in players:
        fields = {
            "birth_date": p.birth_date,
            "nationality": p.nationality,
            "wikidata_qid": p.qid,
            "eliteprospects_id": p.eliteprospects_id,
            "position": p.position,
            "first_name": p.given_name,
            "last_name": p.family_name,
        }
        if _namesake_elsewhere(session, sport, p):
            athlete = m.Athlete(
                sport_id=sport.id, full_name=p.name, name_normalized=normalize(p.name), **fields
            )
            session.add(athlete)
            session.flush()
            repo.add_aliases(session, athlete, [p.name])
        else:
            athlete = repo.upsert_athlete(session, sport, p.name, **fields)
        stats.athletes += 1
        repo.add_aliases(session, athlete, p.aliases)
        for platform, handles in (("instagram", p.instagram), ("x", p.x)):
            for handle in handles:
                add_handle_candidate(
                    session,
                    athlete,
                    platform,
                    handle,
                    evidence={"kind": "wikidata", "detail": f"Wikidata {p.qid}", "url": p.url},
                )
                stats.handles += 1
        for t in p.teams:
            if t.end is not None or not t.team_label or t.team_label == t.team_qid:
                continue  # only open memberships with a name are useful as "current" hints
            club = _team_by_name(session, sport, t.team_label, None, _iso3(t.country), default_country=None)
            exists = session.scalar(
                select(m.Affiliation).where(
                    m.Affiliation.athlete_id == athlete.id, m.Affiliation.team_id == club.id
                )
            )
            if exists is None:
                session.add(
                    m.Affiliation(
                        athlete_id=athlete.id,
                        team_id=club.id,
                        valid_from=t.start,
                        source="Wikidata",
                        source_url=p.url,
                    )
                )
    session.flush()
    return stats


def seed_wikidata_bulk(
    session: Session | None,
    sport_key: str,
    nations: list[str],
    collector: WikidataCollector | None = None,
    dry_run: bool = False,
    today: date | None = None,
    min_birth_year: int = 1980,
    retired_years: int = 3,
) -> tuple[list[BulkCount], SeedStats]:
    """v5 P4: active athletes of a sport for several nations from Wikidata (free), one query per nation.

    Active = born in/after ``min_birth_year`` (or no birth date) and no career end (P2032) longer ago than
    ``retired_years``. ``dry_run`` only counts (no session needed)."""
    spec = get_sport(sport_key)
    collector = collector or WikidataCollector()
    today = today or date.today()
    counts: list[BulkCount] = []
    total = SeedStats()
    for nation in nations:
        code = nation.strip().upper()
        if code not in COUNTRY_QIDS:
            raise ValueError(f"unknown nation {code!r} (known: {', '.join(sorted(COUNTRY_QIDS))})")
        found = collector.collect_bulk(spec, code, citizenship_qids(code), min_birth_year=min_birth_year)
        active, count = split_active(found, code, today, min_birth_year, retired_years)
        counts.append(count)
        if not dry_run and session is not None:
            stats = load_wikidata_players(session, active, sport_key=spec.key)
            total.athletes += stats.athletes
            total.handles += stats.handles
    return counts, total


_ISO = {
    "CHE": "SUI",
    "USA": "USA",
    "CAN": "CAN",
    "SWE": "SWE",
    "DEU": "GER",
    "AUT": "AUT",
    "FIN": "FIN",
    "CZE": "CZE",
    "JPN": "JPN",
    "DNK": "DEN",
    "ITA": "ITA",
    "RUS": "RUS",
    "FRA": "FRA",
    "NOR": "NOR",
    "SVN": "SLO",
    "HRV": "CRO",
    "NLD": "NED",
    "SVK": "SVK",
    "POL": "POL",
}


def _iso3(country: str | None) -> str | None:
    return _ISO.get(country, country) if country else None
