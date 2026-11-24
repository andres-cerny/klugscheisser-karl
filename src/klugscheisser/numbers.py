"""v8: club jersey numbers for tier 1/2 persons, from the club's Wikipedia roster (the same reader as "Team
hinzufügen"). Free (Wikidata + Wikipedia). A club roster counts only when at least one of our known players of
that club is on it (so a men's team or a namesake club never sets numbers). Stored on the club affiliation and
as a sourced `jersey_number` fact ("Nummer 7 (EVZ Women's Team)"), which the person card shows next to the
national-team number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import name_variants

log = logging.getLogger(__name__)
SEASON = 2026  # start year of the current season (2026/27)


@dataclass
class NumbersReport:
    clubs: int = 0
    clubs_with_roster: int = 0
    numbers: int = 0
    skipped: list[str] = field(default_factory=list)


def current_clubs(s: Session, athlete_ids: list[int]) -> dict[int, set[int]]:
    """club team id -> our persons (of `athlete_ids`) whose newest club affiliation is that club."""
    out: dict[int, set[int]] = {}
    for aid in athlete_ids:
        a = s.get(m.Athlete, aid)
        clubs = [x for x in a.affiliations if x.team.kind == "club" and x.valid_to is None]
        clubs.sort(key=lambda x: x.season or "", reverse=True)
        if clubs:
            out.setdefault(clubs[0].team_id, set()).add(aid)
    return out


def _same(name: str, a: m.Athlete) -> bool:
    ours = {x.alias_normalized for x in a.aliases} | name_variants(a.full_name)
    return bool(name_variants(name) & ours)


def club_numbers(s: Session, settings, athlete_ids: list[int], resolver=None, echo=print) -> NumbersReport:
    from klugscheisser.adds.team import TeamRosterResolver

    resolver = resolver or TeamRosterResolver(settings, llm=None)  # rules only: no paid interpretation
    report = NumbersReport()
    for team_id, ours in current_clubs(s, athlete_ids).items():
        club = s.get(m.Team, team_id)
        report.clubs += 1
        try:
            proposal = resolver.propose(s, f"{club.name} women's ice hockey", include_staff=False)
        except Exception as exc:  # one club must not stop the others
            report.skipped.append(f"{club.name}: {exc}")
            continue
        src = proposal.team.roster_source if proposal.team else None
        rows = [p for p in proposal.persons if p.jersey_number]
        persons = [s.get(m.Athlete, i) for i in ours]
        hits = [(p, a) for p in rows for a in persons if _same(p.name, a)]
        if not proposal.found or src is None or not hits:
            report.skipped.append(f"{club.name}: kein passender Kader ({proposal.summary[:80]})")
            continue
        report.clubs_with_roster += 1
        for p, a in hits:
            aff = s.scalar(
                select(m.Affiliation)
                .where(m.Affiliation.athlete_id == a.id, m.Affiliation.team_id == club.id)
                .order_by(m.Affiliation.season.desc().nulls_last())
                .limit(1)
            )
            if aff is not None:
                aff.jersey_number = p.jersey_number
            text = f"Nummer {p.jersey_number} ({club.name})"
            exists = s.scalar(
                select(m.Fact.id).where(
                    m.Fact.athlete_id == a.id, m.Fact.kind == "jersey_number", m.Fact.value_text == text
                )
            )
            if exists is None:
                s.add(
                    m.Fact(
                        athlete_id=a.id,
                        kind="jersey_number",
                        value={"number": p.jersey_number, "team": club.name},
                        value_text=text,
                        season=SEASON,
                        source="wikipedia",
                        source_url=src.url,
                        extractor="club-roster",
                    )
                )
                report.numbers += 1
        s.flush()
        echo(f"{club.name}: {len(hits)} Nummern ({src.label})")
    return report
