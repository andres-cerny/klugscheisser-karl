"""Read side of the fact layer (used by the answer pipeline): one fact per person, career stints, who played
together. Every row carries `source`, `source_url` and `quote` so each line of an answer can be cited."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.facts.catalog import SENSITIVE_KINDS
from klugscheisser.facts.source_site import item_sites
from klugscheisser.facts.teams import team_key

UNKNOWN_SEASON = "Saison unbekannt"  # a station from an article that names no season (never shown as "?")
STATUS_RANK = {"confirmed": 0, "conflict": 1, "unconfirmed": 2}
OPEN_END = 9999  # season_to None = open / current


@dataclass
class FactRow:
    athlete_id: int
    athlete_name: str
    kind: str
    value: dict
    value_text: str
    status: str
    confidence: float
    source: str
    source_url: str
    quote: str | None
    source_item_id: int | None = None
    vorsicht: bool = False
    site: str | None = (
        None  # citable site + date for web-type sources ("EV Zug, 16.12.2025"), see source_site
    )
    # other values for the same kind when sources disagree (shown next to the chosen one)
    conflicts: list[FactRow] = field(default_factory=list)


@dataclass
class StintRow:
    athlete_id: int
    athlete_name: str
    team_id: int | None
    team_name: str
    kind: str
    league: str | None
    season_from: int | None
    season_to: int | None
    role: str
    status: str
    confidence: float
    source: str
    source_url: str
    quote: str | None
    source_item_id: int | None = None
    ep_team_id: str | None = None  # v12: Elite Prospects team id
    site: str | None = (
        None  # citable site + date for web-type sources ("EV Zug, 16.12.2025"), see source_site
    )

    @property
    def season_known(self) -> bool:
        return self.season_from is not None or self.season_to is not None

    @property
    def seasons(self) -> str:
        """'2019/20–2021/22', 'seit 2019/20', 'Saison unbekannt'."""

        def fmt(y: int) -> str:
            return f"{y}/{(y + 1) % 100:02d}"

        if self.season_from is None:
            return f"bis {fmt(self.season_to)}" if self.season_to is not None else UNKNOWN_SEASON
        if self.season_to is None:
            return f"seit {fmt(self.season_from)}"
        if self.season_to == self.season_from:
            return fmt(self.season_from)
        return f"{fmt(self.season_from)}–{fmt(self.season_to)}"


@dataclass
class Pair:
    a: StintRow
    b: StintRow
    team_name: str
    season_from: int  # first common season
    season_to: int | None  # last common season; None = both open


# v11: for hockey data Elite Prospects wins over Wikidata/Wikipedia/web (life story stays with the others)
EP_FIRST_KINDS = {
    "height_cm",
    "weight_kg",
    "position",
    "shoots",
    "birth_place",
    "current_team",
    "jersey_number",
    "draft",
    "youth_team",
    "contract",
    "agency",
}


# 2026-10-25: official tournament rosters (Hockey Canada) right after Elite Prospects for hockey data, right after
# Wikidata for the rest (birth date, hometown); everything else (web, news, Wikipedia …) after them as before.
_SOURCE_ORDER_HOCKEY = {"eliteprospects": 0, "hockeycanada": 1, "wikidata": 2}
_SOURCE_ORDER = {"wikidata": 0, "hockeycanada": 1}


def _rank(f: m.Fact) -> tuple:
    order = _SOURCE_ORDER_HOCKEY if f.kind in EP_FIRST_KINDS else _SOURCE_ORDER
    return (
        STATUS_RANK.get(f.status, 3),
        order.get(f.source, 3),
        -(f.confidence or 0.0),
        -(f.id or 0),  # newest first
    )


def _fact_row(f: m.Fact, name: str) -> FactRow:
    return FactRow(
        athlete_id=f.athlete_id,
        athlete_name=name,
        kind=f.kind,
        value=f.value or {},
        value_text=f.value_text,
        status=f.status,
        confidence=f.confidence,
        source=f.source,
        source_url=f.source_url,
        quote=f.quote,
        source_item_id=f.source_item_id,
        vorsicht=f.kind in SENSITIVE_KINDS,
    )


def _shown_key(f: m.Fact) -> str:
    from klugscheisser.facts.extract import compare_key

    return compare_key(f.kind, f.value or {}) or f.value_text


def _names(session: Session, ids: list[int]) -> dict[int, str]:
    rows = session.execute(select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(ids)))
    return dict(rows.all())


def facts_for(
    session: Session, athlete_ids: list[int], kind: str, include_unconfirmed: bool = False
) -> list[FactRow]:
    """One row per person (in the order of `athlete_ids`): confirmed before conflict before unconfirmed, then
    Wikidata, then higher support, then newest. Differing values of conflicting sources are in `conflicts`.
    Persons without a value are missing from the result (the caller lists them as "kein Wert")."""
    if not athlete_ids:
        return []
    q = select(m.Fact).where(m.Fact.athlete_id.in_(athlete_ids), m.Fact.kind == kind)
    if not include_unconfirmed:
        q = q.where(m.Fact.status != "unconfirmed")
    by_person: dict[int, list[m.Fact]] = {}
    for f in session.scalars(q):
        by_person.setdefault(f.athlete_id, []).append(f)
    names = _names(session, list(by_person))
    out = []
    for aid in dict.fromkeys(athlete_ids):
        facts = sorted(by_person.get(aid, []), key=_rank)
        if not facts:
            continue
        best = _fact_row(facts[0], names.get(aid, ""))
        if any(f.status == "conflict" for f in facts):
            shown = {_shown_key(facts[0])}
            for f in facts[1:]:
                differs = f.status == "conflict" or (best.status == "conflict" and f.status == "confirmed")
                if differs and _shown_key(f) not in shown:
                    shown.add(_shown_key(f))
                    best.conflicts.append(_fact_row(f, names.get(aid, "")))
        out.append(best)
    item_sites(session, out + [c for r in out for c in r.conflicts])
    return out


def _stint_row(s: m.CareerStint, name: str) -> StintRow:
    return StintRow(
        athlete_id=s.athlete_id,
        athlete_name=name,
        team_id=s.team_id,
        team_name=s.team_name,
        kind=s.kind,
        league=s.league,
        season_from=s.season_from,
        season_to=s.season_to,
        role=s.role,
        status=s.status,
        confidence=s.confidence,
        source=s.source,
        source_url=s.source_url,
        quote=s.quote,
        source_item_id=s.source_item_id,
        ep_team_id=s.ep_team_id,
    )


def stints_for(session: Session, athlete_ids: list[int], include_unconfirmed: bool = False) -> list[StintRow]:
    """All career stints of these persons, by person (input order), then season (unknown last)."""
    if not athlete_ids:
        return []
    q = select(m.CareerStint).where(m.CareerStint.athlete_id.in_(athlete_ids))
    if not include_unconfirmed:
        q = q.where(m.CareerStint.status != "unconfirmed")
    stints = list(session.scalars(q))
    names = _names(session, list({s.athlete_id for s in stints}))
    order = {aid: i for i, aid in enumerate(dict.fromkeys(athlete_ids))}
    stints.sort(
        key=lambda s: (
            order[s.athlete_id],
            s.season_from is None,
            s.season_from or 0,
            s.source != "wikidata",
            s.id,
        )
    )
    rows = [_stint_row(s, names.get(s.athlete_id, "")) for s in stints]
    item_sites(session, rows)
    return rows


def _same_team(a: StintRow, b: StintRow) -> bool:
    if a.ep_team_id and b.ep_team_id:  # v12: Elite Prospects' id is the surest (renames, abbreviations)
        return a.ep_team_id == b.ep_team_id
    if a.team_id is not None and b.team_id is not None:
        return a.team_id == b.team_id
    return team_key(a.team_name) == team_key(b.team_name)


def prefer_ep(stints: list[StintRow]) -> list[StintRow]:
    """v12: for a person with Elite Prospects stations (every season, from–until per EP team) the club/college/
    youth stations of other sources are left out of "played together": they are vaguer ("Frölunda seit 2025",
    no end) and would invent overlaps. National-team stations and persons without EP data stay as they are."""
    with_ep = {x.athlete_id for x in stints if x.source == "eliteprospects"}
    return [
        x
        for x in stints
        if x.athlete_id not in with_ep or x.source == "eliteprospects" or x.kind == "national"
    ]


def played_together(session: Session, ids_a: list[int], ids_b: list[int]) -> list[Pair]:
    """Pairs (one person from each side) with overlapping seasons at the same team: team_id equal, or the
    normalised team name equal when a stint is not linked to a team. Stints without a start season are
    skipped (no overlap can be shown); an open end counts as "until now". One pair per persons and team,
    the best-sourced stints first."""
    stints_a = [s for s in prefer_ep(stints_for(session, ids_a)) if s.season_from is not None]
    stints_b = [s for s in prefer_ep(stints_for(session, ids_b)) if s.season_from is not None]
    best: dict[tuple, Pair] = {}

    def quality(s: StintRow) -> tuple:
        return (STATUS_RANK.get(s.status, 3), s.source != "wikidata", -(s.confidence or 0.0))

    for a in stints_a:
        for b in stints_b:
            if a.athlete_id == b.athlete_id or not _same_team(a, b):
                continue
            start = max(a.season_from, b.season_from)
            end = min(
                OPEN_END if a.season_to is None else a.season_to,
                OPEN_END if b.season_to is None else b.season_to,
            )
            if start > end:
                continue
            pair = Pair(a, b, a.team_name, start, None if end == OPEN_END else end)
            persons = tuple(sorted((a.athlete_id, b.athlete_id)))
            key = (persons, a.team_id or team_key(a.team_name))
            old = best.get(key)
            if old is None or (quality(a), quality(b)) < (quality(old.a), quality(old.b)):
                best[key] = pair
    return sorted(best.values(), key=lambda p: (p.a.athlete_name, p.b.athlete_name, p.season_from))
