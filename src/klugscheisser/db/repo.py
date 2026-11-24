"""Lookups shared by seeding, ingest and the MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import name_variants, normalize

FUZZY_MIN_SIMILARITY = 0.45


@dataclass
class AthleteMatch:
    athlete: m.Athlete
    score: float
    how: str  # exact | fuzzy


def get_sport(session: Session, key: str, name: str | None = None) -> m.Sport:
    sport = session.scalar(select(m.Sport).where(m.Sport.key == key))
    if sport is None:
        sport = m.Sport(key=key, name=name or key)
        session.add(sport)
        session.flush()
    return sport


def add_aliases(session: Session, athlete: m.Athlete, names: list[str], kind: str = "variant") -> None:
    existing = {a.alias_normalized for a in athlete.aliases}
    for raw in names:
        for variant in name_variants(raw):
            if variant not in existing:
                athlete.aliases.append(m.AthleteAlias(alias=raw, alias_normalized=variant, kind=kind))
                existing.add(variant)
    session.flush()


def find_athletes(session: Session, query: str, limit: int = 5) -> list[AthleteMatch]:
    """Exact match on any spelling variant first, then trigram similarity for typos."""
    variants = name_variants(query) | {normalize(query)}
    exact = session.scalars(
        select(m.Athlete).join(m.AthleteAlias).where(m.AthleteAlias.alias_normalized.in_(variants)).distinct()
    ).all()
    if exact:
        return [AthleteMatch(a, 1.0, "exact") for a in exact]
    q = normalize(query)
    sim = func.similarity(m.AthleteAlias.alias_normalized, q)
    rows = session.execute(
        select(m.Athlete, func.max(sim).label("s"))
        .join(m.AthleteAlias)
        .where(text("athlete_aliases.alias_normalized % :q").bindparams(q=q))
        .group_by(m.Athlete.id)
        .order_by(text("s DESC"))
        .limit(limit)
    ).all()
    return [AthleteMatch(a, float(s), "fuzzy") for a, s in rows if s >= FUZZY_MIN_SIMILARITY]


def resolve_athlete(session: Session, query: str | int) -> m.Athlete | None:
    if isinstance(query, int) or (isinstance(query, str) and query.isdigit()):
        return session.get(m.Athlete, int(query))
    matches = find_athletes(session, query, limit=2)
    if not matches:
        return None
    if len(matches) > 1 and matches[0].score - matches[1].score < 0.05:
        return None  # ambiguous: caller should present candidates
    return matches[0].athlete


def upsert_athlete(
    session: Session,
    sport: m.Sport,
    full_name: str,
    *,
    birth_date: date | None = None,
    nationality: str | None = None,
    wikidata_qid: str | None = None,
    **fields,
) -> m.Athlete:
    athlete = None
    if wikidata_qid:
        athlete = session.scalar(select(m.Athlete).where(m.Athlete.wikidata_qid == wikidata_qid))
    if athlete is None:
        for match in find_athletes(session, full_name):
            same_birth = birth_date is None or match.athlete.birth_date in (None, birth_date)
            if match.how == "exact" and same_birth:
                athlete = match.athlete
                break
    if athlete is None:
        athlete = m.Athlete(sport_id=sport.id, full_name=full_name, name_normalized=normalize(full_name))
        session.add(athlete)
    for key, value in {
        "birth_date": birth_date,
        "nationality": nationality,
        "wikidata_qid": wikidata_qid,
        **fields,
    }.items():
        if value is not None and getattr(athlete, key, None) in (None, ""):
            setattr(athlete, key, value)
    session.flush()
    add_aliases(session, athlete, [full_name])
    return athlete


def get_team(session: Session, key: str) -> m.Team | None:
    return session.scalar(select(m.Team).where(m.Team.key == key))


def find_team(session: Session, query: str, fuzzy: bool = True) -> m.Team | None:
    q = normalize(query)
    teams = session.scalars(select(m.Team)).all()
    for team in teams:
        if q in {normalize(team.name), normalize(team.key), normalize(team.short_name or "")}:
            return team
    if not fuzzy or not q:
        return None
    return next((t for t in teams if q in normalize(t.name)), None)


def current_affiliations(session: Session, athlete: m.Athlete) -> list[m.Affiliation]:
    """Newest first: by season label, then valid_from."""
    affs = list(athlete.affiliations)
    affs.sort(key=lambda a: (a.season or "", a.valid_from or date.min), reverse=True)
    return [a for a in affs if a.valid_to is None or a.valid_to >= date.today()]
