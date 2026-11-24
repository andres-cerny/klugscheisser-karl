"""v4: add persons or whole teams on demand. Contract between the resolver (proposal) and the loader (apply).

Flow: Fabi's input → resolver builds a `Proposal` (concrete description + sources + cost estimate) →
Fabi answers ✅ Ja / ❌ Nein / ✏️ Präzisieren → on Ja the loader creates the persons and loads their data in the
background, capped at `add_cost_cap_chf` per action.

Rules for resolvers:
- Every fact in a proposal carries a source (`SourceRef`); facts only Fabi gave are marked as such.
- A person is only matched to a Wikidata/Wikipedia/web entry when Jev says it fits Fabi's details (namesakes!).
- Instagram handles are never verified here: Fabi's handle becomes a candidate with evidence
  "von Fabi angegeben" and goes through the tiered handle verification (handles/service.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Protocol

from sqlalchemy.orm import Session

from klugscheisser.config import Settings


@dataclass
class SourceRef:
    label: str  # "Wikidata Q123", "Wikipedia (de)", "SDHL-Kader", "von Fabi angegeben"
    url: str | None = None


@dataclass
class ProposedPerson:
    name: str
    role: str = "athlete"  # athlete | coach | staff
    function_de: str | None = None  # "Cheftrainerin", "Assistenztrainer", "Materialwart" (coaches/staff)
    sport_key: str | None = None  # registered SportSpec key, or a new one (generic sport layer)
    sport_name_de: str | None = None  # "Eishockey Frauen", "Ski alpin Frauen" (needed for a new sport)
    person_de: str | None = None  # "Eishockeyspielerin", "Skirennfahrerin"
    nationality: str | None = None  # IOC alpha-3, "SUI", "SWE"
    birth_date: date | None = None
    team: str | None = None  # current club / team as stated by a source
    position: str | None = None  # G | D | F for ice hockey, free text otherwise
    jersey_number: int | None = None
    wikidata_qid: str | None = None
    wikipedia_url: str | None = None
    eliteprospects_id: str | None = None
    instagram: str | None = None  # handle without @
    instagram_source: str | None = None  # "fabi" | "wikidata"
    instagram_check: str | None = (
        None  # German, e.g. "Profilname stimmt, Bio nennt Eishockey" / "nicht geprüft"
    )
    sources: list[SourceRef] = field(default_factory=list)
    existing_id: int | None = None  # already in our database (then only missing data is added)
    fit: float | None = None  # Jev probability that the matched entry is the person Fabi means
    event_team_id: int | None = None  # v5 event preparation: the team this person is entered for


@dataclass
class ProposedTeam:
    name: str
    kind: str = "national"  # national | club
    country: str | None = None  # IOC alpha-3
    league: str | None = None
    sport_key: str | None = None
    wikidata_qid: str | None = None
    existing_id: int | None = None
    roster_source: SourceRef | None = None  # where the roster comes from


@dataclass
class Proposal:
    kind: str  # "person" | "team"
    found: bool  # False: nothing reliable found; summary explains, Ja is not offered
    summary: str  # German, concrete: "Hanna Olsson, schwedische Eishockeyspielerin, geb. 1999, Frölunda …"
    persons: list[ProposedPerson] = field(default_factory=list)
    team: ProposedTeam | None = None
    alternatives: list[str] = field(default_factory=list)  # other candidates seen (namesakes), short German
    notes: list[str] = field(default_factory=list)  # German warnings, e.g. "Handle nur von Fabi angegeben"
    cost_estimate_chf: float = 0.0
    resolver_cost_usd: float = 0.0  # what building the proposal itself cost (Brave, Apify profile, Jev)

    def to_json(self) -> dict[str, Any]:
        return asdict(self, dict_factory=_json_dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Proposal:
        persons = [
            ProposedPerson(
                **{
                    **p,
                    "birth_date": date.fromisoformat(p["birth_date"]) if p.get("birth_date") else None,
                    "sources": [SourceRef(**x) for x in p.get("sources", [])],
                }
            )
            for p in data.get("persons", [])
        ]
        team = None
        if t := data.get("team"):
            rs = t.get("roster_source")
            team = ProposedTeam(**{**t, "roster_source": SourceRef(**rs) if rs else None})
        rest = {k: v for k, v in data.items() if k not in ("persons", "team")}
        return cls(**rest, persons=persons, team=team)


def _json_dict(items: list[tuple[str, Any]]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in items}


# --- cost estimate (shared by resolvers and the loader) ---------------------------------------------------

# first Instagram load per handle: own posts + tagged posts over instagram_first_run_days, plus the profile
FIRST_LOAD_RESULTS_PER_HANDLE = 40
BRAVE_REQUESTS_PER_PERSON = 0  # web search runs only when a question needs it


def estimate_load_cost_chf(settings: Settings, persons: int, with_instagram: int) -> float:
    usd = with_instagram * (FIRST_LOAD_RESULTS_PER_HANDLE + 1) * settings.apify_cost_per_post_usd
    usd += persons * BRAVE_REQUESTS_PER_PERSON * settings.brave_cost_per_request_usd
    return round(usd * settings.usd_to_chf, 2)


class PersonResolver(Protocol):
    def propose(
        self,
        session: Session,
        name: str,
        instagram: str | None = None,
        details: str = "",
        refine: str | None = None,
        previous: Proposal | None = None,
    ) -> Proposal:
        """`refine`: Fabi's correction after ✏️ Präzisieren; `previous`: the proposal she corrected."""
        ...


class TeamResolver(Protocol):
    def propose(
        self,
        session: Session,
        request: str,
        refine: str | None = None,
        previous: Proposal | None = None,
        include_staff: bool = True,
    ) -> Proposal:
        """`request`: free text like "Füge das schwedische Frauen-Nationalteam hinzu"."""
        ...
