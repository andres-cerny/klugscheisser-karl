"""Tournament rosters from the game sheets (2026-11-04, game watch): players who dressed but are not on Karl's list.

Why: late additions and injury replacements during the WCh appear on the IIHF game sheet before (or instead of) a
new federation list. Fabi must be able to see them right after the game.

Called by the game watch after every new or changed final sheet (`jobs/gamewatch.py`, switch
GAME_WATCH_SHEET_ROSTERS). Database only, no request: it reads the stored sheet of the game.

- Who dressed: every player the sheet names for a team. The IIHF realtime feed names scorers, assists and goalies
  (with number and position, `raw["sheet_players"]`); a feed with full line-ups (`raw["lineup"]`, e.g. Leijonat)
  adds the rest. A goal or an assist proves she played.
- On the list already: exact name (any spelling variant, "MULLER Alina" = Alina Müller) or a clear fuzzy hit among
  that team's listed players. Then nothing happens.
- Not on the list: added to the team's tournament roster as `confirmed`, source "IIHF-Spielbericht: <game>
  (<date>)" + the game's IIHF link. Identity: an existing athlete with that exact name and the team's nation (or
  no nation) — one only, else nothing is added and the case is logged as unclear; no hit at all = a new person
  (nation = the team's country, position from the sheet), as the roster loader does.
- Not added (logged as unclear, for a person to check): a name listed for another team of the tournament, a jersey
  number that Karl's list gives to someone else, more than MAX_ADDS new names for one team in one game (sheet or
  matching problem, not a late addition).
- Never removed: a listed player who did not dress is only logged ("nicht eingesetzt"), and only when the sheet
  has full line-ups. Entries added here are kept when a later official list is loaded without them
  (seed/loader.load_roster_data, GAME_SHEET_SOURCE).
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.labels import TZ
from klugscheisser.seed.loader import GAME_SHEET_SOURCE
from klugscheisser.sports import current_sport_key
from klugscheisser.staff import is_staff_entry

log = logging.getLogger(__name__)

STATE = "game_rosters"  # source_state row: per game key what the sheet changed or showed
MAX_ADDS = 4  # more new names than this for one team in one game: not applied, logged as unclear
FUZZY_SAME = 0.6  # a fuzzy name hit among the team's listed players at least this close is the same player


def tournament_of(s: Session, game: m.Event, team_id: int) -> m.Event | None:
    """The tournament event (non-game, dates include the game day ±1) that has this team."""
    day = game.starts_at.astimezone(TZ).date()
    for c in s.scalars(
        select(m.Event).where(m.Event.kind != "game", m.Event.starts_at.is_not(None)).order_by(m.Event.id)
    ):
        first = c.starts_at.astimezone(TZ).date()
        last = (c.ends_at or c.starts_at).astimezone(TZ).date()
        inside = first - timedelta(days=1) <= day <= last + timedelta(days=1)
        if inside and s.get(m.EventTeam, (c.id, team_id)) is not None:
            return c
    return None


def _code_of(team: m.Team) -> str:
    return (team.country or team.key.split("-")[0]).upper()


def dressed(item: m.Item, code: str, team_de: str | None) -> tuple[list[dict], bool]:
    """(players the sheet names for the team, True when the sheet has the full line-up of that team)."""
    raw = item.raw or {}
    rows: dict[str, dict] = {}
    for p in raw.get("sheet_players") or []:
        if p.get("team") == code and p.get("name"):
            rows.setdefault(p["name"], dict(p))
    lineup = (raw.get("lineup") or {}).get(team_de or "", []) or (raw.get("lineup") or {}).get(code, [])
    for name in lineup:
        rows.setdefault(name, {"name": name, "team": code, "roles": ["lineup"]})
    return list(rows.values()), bool(lineup)


def _listed(s: Session, name: str, entries: list[m.EventEntry]) -> m.EventEntry | None:
    from klugscheisser.db.repo import find_athletes

    by_id = {e.athlete_id: e for e in entries}
    hits = find_athletes(s, name, limit=8)
    for h in hits:
        if h.athlete.id in by_id and (h.how == "exact" or h.score >= FUZZY_SAME):
            return by_id[h.athlete.id]
    return None


def _on_other_team(s: Session, tour: m.Event, team: m.Team, name: str) -> bool:
    """A person of this exact name is listed for another team of the tournament (namesake or a data error)."""
    from klugscheisser.db.repo import find_athletes

    ids = [h.athlete.id for h in find_athletes(s, name, limit=8) if h.how == "exact"]
    if not ids:
        return False
    hit = s.scalar(
        select(m.EventEntry.id)
        .where(
            m.EventEntry.event_id == tour.id,
            m.EventEntry.athlete_id.in_(ids),
            m.EventEntry.team_id != team.id,
        )
        .limit(1)
    )
    return hit is not None


def _identity(s: Session, name: str, nation: str | None) -> tuple[m.Athlete | None, str | None]:
    """(existing athlete, None) | (None, None) = new person | (None, reason) = unclear."""
    from klugscheisser.db.repo import find_athletes

    exact = [h.athlete for h in find_athletes(s, name, limit=8) if h.how == "exact"]
    if not exact:
        return None, None
    same = [a for a in exact if (a.nationality or "").upper() == (nation or "").upper()]
    open_ = [a for a in exact if not a.nationality]
    if len(same) == 1:
        return same[0], None
    if not same and len(open_) == 1:
        return open_[0], None
    if not same and not open_:
        return None, None  # only namesakes of other nations: a new person
    return None, f"{len(same) or len(open_)} Personen dieses Namens in Karl"


def _role(p: dict, athlete: m.Athlete) -> str | None:
    pos = p.get("pos") or (athlete.position or "").upper()[:1]
    return pos if pos in ("G", "D", "F") else None


def _state(s: Session) -> dict:
    row = s.get(m.SourceState, STATE)
    return copy.deepcopy(row.state or {}) if row is not None else {}


def _save(s: Session, state: dict) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    row = s.get(m.SourceState, STATE)
    if row is None:
        row = m.SourceState(source=STATE, state={})
        s.add(row)
    row.state = state
    flag_modified(row, "state")
    s.flush()


def reconcile_game(s: Session, game: m.Event, item: m.Item, today: date | None = None) -> dict[str, Any]:
    """Compare the stored sheet of one final game with both teams' tournament rosters; add who dressed but is
    missing (see module doc). Idempotent: a player added once is on the list the next time."""
    from klugscheisser.collectors.results.base import TEAM_DE
    from klugscheisser.db import repo
    from klugscheisser.seed.loader import _national_affiliation

    day = game.starts_at.astimezone(TZ).date() if game.starts_at else (today or datetime.now(UTC).date())
    url = item.url
    source = f"{GAME_SHEET_SOURCE}: {game.name} ({day:%d.%m.%Y})"
    report: dict[str, Any] = {}
    for team_id in (game.home_team_id, game.away_team_id):
        team = s.get(m.Team, team_id) if team_id else None
        if team is None:
            continue
        code = _code_of(team)
        out: dict[str, Any] = {"dazu": [], "unklar": []}
        report[code] = out
        tour = tournament_of(s, game, team.id)
        if tour is None:
            out["status"] = "kein Turnier mit diesem Team"
            continue
        entries = [
            e
            for e in s.scalars(
                select(m.EventEntry).where(m.EventEntry.event_id == tour.id, m.EventEntry.team_id == team.id)
            )
            if not is_staff_entry(e.role)
        ]
        if not entries:
            out["status"] = "kein Kader gespeichert"
            continue
        players, full = dressed(item, code, TEAM_DE.get(code))
        seen_ids: set[int] = set()
        missing: list[dict] = []
        for p in players:
            e = _listed(s, p["name"], entries)
            if e is not None:
                seen_ids.add(e.athlete_id)
            else:
                missing.append(p)
        numbers = {e.jersey_number: e for e in entries if e.jersey_number is not None}
        todo: list[tuple[dict, m.Athlete | None]] = []
        for p in missing:
            if p.get("number") in numbers:
                holder = numbers[p["number"]].athlete.full_name
                out["unklar"].append(f"{p['name']} (Nr. {p['number']}): Nummer hat laut Kader {holder}")
                continue
            if _on_other_team(s, tour, team, p["name"]):
                out["unklar"].append(f"{p['name']}: steht im Kader eines anderen Teams")
                continue
            athlete, why = _identity(s, p["name"], team.country)
            if why:
                out["unklar"].append(f"{p['name']}: {why}")
                continue
            todo.append((p, athlete))
        if len(todo) > MAX_ADDS:
            out["unklar"].append(
                f"{len(todo)} neue Namen in einem Spiel ({', '.join(p['name'] for p, _ in todo)}): nicht "
                "übernommen, bitte prüfen"
            )
            todo = []
        season = f"{tour.starts_at.year}-{str(tour.starts_at.year + 1)[2:]}" if tour.starts_at else None
        sport = repo.get_sport(s, current_sport_key())  # 2026-11-24: the project's sport
        for p, athlete in todo:
            if athlete is None:
                athlete = m.Athlete(
                    sport_id=sport.id,
                    full_name=p["name"],
                    name_normalized=repo.normalize(p["name"]),
                    nationality=team.country,
                    position=p.get("pos"),
                )
                s.add(athlete)
                s.flush()
                repo.add_aliases(s, athlete, [p["name"]])
                new_person = True
            else:
                new_person = False
            _national_affiliation(s, athlete, team, {"season": season, "source": source, "source_url": url})
            s.add(
                m.EventEntry(
                    event_id=tour.id,
                    athlete_id=athlete.id,
                    team_id=team.id,
                    role=_role(p, athlete),
                    jersey_number=p.get("number"),
                    list_type="confirmed",
                    source=source,
                    source_url=url,
                )
            )
            s.flush()
            seen_ids.add(athlete.id)
            out["dazu"].append(
                p["name"]
                + (f" (Nr. {p['number']})" if p.get("number") else "")
                + (" – neue Person" if new_person else "")
            )
        if full:
            out["nicht_eingesetzt"] = sorted(
                e.athlete.full_name for e in entries if e.athlete_id not in seen_ids
            )
        else:
            out["nicht_eingesetzt"] = None  # sheet without line-up: who did not dress is unknown
        for line in out["dazu"]:
            log.warning("game rosters %s %s: added from the game sheet: %s", game.key, code, line)
        for line in out["unklar"]:
            log.warning("game rosters %s %s: unclear, not added: %s", game.key, code, line)
        if out["nicht_eingesetzt"]:
            log.info(
                "game rosters %s %s: not dressed: %s", game.key, code, ", ".join(out["nicht_eingesetzt"])
            )
    state = _state(s)
    state[game.key] = {"am": datetime.now(UTC).isoformat(), "quelle": url, **report}
    _save(s, state)
    return report


def reconcile_finals(s: Session, keys: list[str]) -> dict[str, Any]:
    """`reconcile_game` for the given game keys that have a stored final IIHF sheet."""
    out: dict[str, Any] = {}
    for key in keys:
        game = s.scalar(select(m.Event).where(m.Event.key == key))
        if game is None or game.status != "final" or not key.startswith("iihf-"):
            continue
        item = s.scalar(
            select(m.Item).where(
                m.Item.platform == "iihf",
                m.Item.source_type == "official_result",
                m.Item.external_id == key.split("-", 1)[1],
            )
        )
        if item is None:
            continue
        try:
            with s.begin_nested():
                out[key] = reconcile_game(s, game, item)
        except Exception as exc:  # never stops the game watch
            log.exception("game rosters %s failed", key)
            out[key] = {"fehler": str(exc)}
    return out


def added_lines(s: Session, tour: m.Event, team: m.Team) -> list[str]:
    """'Name (Nr. 27, F): laut IIHF-Spielbericht: SUI – USA (06.11.2026), <url>' for the tournament answer."""
    rows = s.scalars(
        select(m.EventEntry).where(
            m.EventEntry.event_id == tour.id,
            m.EventEntry.team_id == team.id,
            m.EventEntry.source.like(f"{GAME_SHEET_SOURCE}%"),
        )
    )
    out = []
    for e in rows:
        bits = ", ".join(x for x in (f"Nr. {e.jersey_number}" if e.jersey_number else None, e.role) if x)
        out.append(
            f"{e.athlete.full_name}{f' ({bits})' if bits else ''}: nachgemeldet, laut {e.source}, {e.source_url}"
        )
    return out


__all__ = ["MAX_ADDS", "added_lines", "dressed", "reconcile_finals", "reconcile_game", "tournament_of"]
