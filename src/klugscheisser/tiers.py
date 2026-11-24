"""v8: tiers — how deep we look for a person.

- Tier 1: the Swiss national team pool and its staff (Swiss team rosters since `SINCE`, Swiss national-team
  affiliation in a recent season, or a Swiss national-team stint since `STINT_SINCE`). Deep sources.
- Tier 2: players, coaches and staff of the other teams of the next World Championship (same rules for their
  national team). Light sources.
- Tier 3: everyone else.
Recomputed from the data (`recompute`); Fabi's manual choice (`tier_manual`) always wins.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

SINCE = datetime(2026, 1, 1, tzinfo=UTC)  # rosters of tournaments from this date on count
RECENT_SEASONS = ("2025-26", "2026-27")  # national-team affiliations in these seasons count
STINT_SINCE = 2024  # career stint "national" lasting into the 2024/25 season or later
WM_EVENT = "wch-2026"  # eishockey; the current project's tournament: main_event()
HOME = "SUI"


def main_event() -> str:
    from klugscheisser import projects

    return projects.current().main_event or WM_EVENT


def home() -> str:
    from klugscheisser import projects

    return projects.current().home_nation or HOME


LABELS = {1: "Tier 1", 2: "Tier 2"}


def wm_nations(s: Session) -> set[str]:
    rows = s.scalars(
        select(m.Team.country)
        .join(m.EventTeam, m.EventTeam.team_id == m.Team.id)
        .join(m.Event, m.Event.id == m.EventTeam.event_id)
        .where(m.Event.key == main_event(), m.Team.kind == "national")
    )
    return {c for c in rows if c}


def _people_of(s: Session, countries: set[str]) -> set[int]:
    """Persons tied to these national teams: roster entries (players and staff), recent affiliations, and
    players of that nationality with a recent national-team stint."""
    if not countries:
        return set()
    national = select(m.Team.id).where(m.Team.kind == "national", m.Team.country.in_(countries))
    ids = set(
        s.scalars(
            select(m.EventEntry.athlete_id)
            .join(m.Event, m.Event.id == m.EventEntry.event_id)
            .where(m.EventEntry.team_id.in_(national), m.Event.starts_at >= SINCE)
        )
    )
    ids |= set(
        s.scalars(
            select(m.Affiliation.athlete_id).where(
                m.Affiliation.team_id.in_(national), m.Affiliation.season.in_(RECENT_SEASONS)
            )
        )
    )
    ids |= set(
        s.scalars(
            select(m.CareerStint.athlete_id)
            .join(m.Athlete, m.Athlete.id == m.CareerStint.athlete_id)
            .where(
                m.CareerStint.kind == "national",
                m.Athlete.nationality.in_(countries),
                or_(m.CareerStint.season_to.is_(None), m.CareerStint.season_to >= STINT_SINCE),
                m.CareerStint.status != "conflict",
            )
        )
    )
    return ids


def compute(s: Session) -> dict[int, int]:
    """Automatic tier per person id (only tiers 1 and 2 are listed)."""
    tier1 = _people_of(s, {home()})
    tier2 = _people_of(s, wm_nations(s) - {home()}) - tier1
    return {**{i: 2 for i in tier2}, **{i: 1 for i in tier1}}


def recompute(s: Session) -> dict[int, int]:
    """Store the automatic tier for everyone. Returns {tier: number of persons} (effective tiers)."""
    auto = compute(s)
    s.execute(update(m.Athlete).where(m.Athlete.tier != 3).values(tier=3))
    for tier in (1, 2):
        ids = [i for i, t in auto.items() if t == tier]
        if ids:
            s.execute(update(m.Athlete).where(m.Athlete.id.in_(ids)).values(tier=tier))
    s.flush()
    return counts(s)


def effective(a: m.Athlete) -> int:
    return a.tier_manual or a.tier or 3


def counts(s: Session) -> dict[int, int]:
    eff = func.coalesce(m.Athlete.tier_manual, m.Athlete.tier)
    return dict(s.execute(select(eff, func.count()).group_by(eff)).all())


def ids_in(s: Session, tier: int, role: str | None = None) -> list[int]:
    q = select(m.Athlete.id).where(func.coalesce(m.Athlete.tier_manual, m.Athlete.tier) == tier)
    if role:
        q = q.where(m.Athlete.role == role)
    return list(s.scalars(q.order_by(m.Athlete.id)))


def set_manual(s: Session, a: m.Athlete, tier: int | None) -> None:
    """Fabi's choice (1, 2, 3) or None to go back to the automatic tier."""
    a.tier_manual = tier if tier in (1, 2, 3) else None
    s.flush()


def sources_count(s: Session, athlete_id: int) -> int:
    """Stored documents (each with its URL) linked to the person — "Quellen" on the badge."""
    return (
        s.scalar(
            select(func.count(func.distinct(m.ItemAthlete.item_id)))
            .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
            .where(m.ItemAthlete.athlete_id == athlete_id, m.Item.text_purged_at.is_(None))
        )
        or 0
    )


def badge(tier: int, sources: int | None = None) -> str:
    """'Tier 1 · 64 Quellen' (empty for tier 3)."""
    label = LABELS.get(tier)
    if not label:
        return ""
    return f"{label} · {sources} Quellen" if sources is not None else label
