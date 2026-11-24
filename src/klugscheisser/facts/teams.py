"""Map team names as written in a source (Wikidata label, Wikipedia sentence) to our Team rows.

Only unambiguous matches link: the whole normalised name must be a team name or an explicit alias
(seeds/team_aliases.yaml) of exactly one team. National teams link by country when the name says it is a
women's national team. Everything else stays unlinked (team_id None) and is compared by `team_key`.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import normalize
from klugscheisser.domain.team_matcher import has_women_context

# words that do not tell two clubs apart ("ZSC Lions Frauen" = "ZSC Lions Women" = "ZSC Lions")
_GENERIC = {
    "women",
    "womens",
    "woman",
    "frauen",
    "damen",
    "dam",
    "ladies",
    "feminin",
    "feminine",
    "femminile",
    "ice",
    "hockey",
    "eishockey",
    # men's projects (floorball, 2026-11-24): "Zug United Herren" = "Zug United"
    "herren",
    "manner",
    "maenner",
    "men",
    "mens",
    "unihockey",
    "floorball",
    "team",
    "the",
}
_NATIONAL = re.compile(
    r"national|nationalmannschaft|nationalteam|nati\b|landslag|maajoukkue|reprezentac|landshold", re.I
)
_YOUTH = re.compile(r"\bu ?-?\d{2}\b|under[- ]?\d{2}|junior|youth|nachwuchs|elite ?a\b", re.I)


def team_key(name: str) -> str:
    """Normalised comparison key of a team name, without generic words."""
    words = [w for w in normalize(name or "").split() if w not in _GENERIC]
    return " ".join(words) or normalize(name or "")


def is_national_name(name: str) -> bool:
    return bool(_NATIONAL.search(name or ""))


def is_youth_name(name: str) -> bool:
    return bool(_YOUTH.search(name or ""))


class TeamResolver:
    def __init__(self, session: Session) -> None:
        from klugscheisser.seed.loader import load_team_aliases
        from klugscheisser.sports import current_spec

        self.session = session
        self.men = current_spec().men  # men's project: national teams link by a men's (not women's) name
        teams = list(session.scalars(select(m.Team)))
        self.national_by_country = {t.country: t.id for t in teams if t.kind == "national" and t.country}
        names: dict[str, set[int]] = {}
        by_key = {t.key: t for t in teams}
        for t in teams:
            if t.kind == "national":
                continue  # "Schweiz" alone is a country, not a team
            for n in filter(None, [t.name, t.short_name if t.short_name and len(t.short_name) > 3 else None]):
                names.setdefault(normalize(n), set()).add(t.id)
        for key, aliases in load_team_aliases(session).items():
            team = by_key.get(key)
            if team is None:
                continue
            for a in aliases:
                women_alias = a.women_specific or has_women_context(normalize(a.alias))
                if team.kind == "national" and (women_alias if self.men else not women_alias):
                    continue  # "Nati" / "Team Sweden" can be the men's team (or, in a men's project, the women's)
                names.setdefault(normalize(a.alias), set()).add(team.id)
        self.names = names

    def resolve(
        self, *labels: str | None, national: bool = False, country: str | None = None, youth: bool = False
    ) -> int | None:
        """Team id for these spellings of one team, or None when not unambiguous."""
        labels_ = [x for x in labels if x]
        if youth or any(is_youth_name(x) for x in labels_):
            return None  # our national team rows are the senior teams
        if national or any(is_national_name(x) for x in labels_):
            women = any(has_women_context(normalize(x)) for x in labels_)
            if women != self.men and country and country in self.national_by_country:
                return self.national_by_country[country]
        for label in labels_:
            ids = self.names.get(normalize(label))
            if ids and len(ids) == 1:
                return next(iter(ids))
        return None
