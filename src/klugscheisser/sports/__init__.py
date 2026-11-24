"""Pluggable sport layer. Each sport defines its Wikidata selectors, positions and categories.

A new sport is a module with `SportSpec`s that register themselves (see `sports/ski_alpine.py`), imported in
`get_sport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SportSpec:
    key: str
    name_de: str
    wikidata_occupation: str  # QID of the occupation, e.g. ice hockey player
    wikidata_gender: str = "Q6581072"  # female
    wikidata_sport: str | None = None  # QID of the sport (P641), e.g. ice hockey Q41466
    person_de: str = "Sportlerin"  # "Eishockeyspielerin", "Skirennfahrerin" (used in identity questions)
    positions: dict[str, str] = field(default_factory=dict)  # keyword in position label -> code
    position_labels_de: dict[str, str] = field(default_factory=dict)
    storylines: tuple[str, ...] = ("comeback", "first_appearance", "milestone", "home_game", "transfer")
    # 2026-11-24 (floorball project): what the rest of the code needs to know about the sport
    family: str = "hockey"  # sport check family (domain/sport.py): hockey | unihockey | ski
    team_suffix: str = "women"  # national-team keys "<ioc>-<suffix>" ("sui-women", "sui-men")
    men: bool = False  # men's sport (German wording: Spieler, not Spielerin)
    # nightly steps / workers that read ice hockey sources (IIHF/SIHF results, Leijonat totals, kaderwatch, Hockey
    # Canada, Elite Prospects, IIHF game watch): skipped for other sports (jobs/nightly.py, jobs/gamewatch.py)
    nightly_skip: tuple[str, ...] = ()

    def position_code(self, label: str | None) -> str | None:
        if not label:
            return None
        low = label.lower()
        for keyword, code in self.positions.items():
            if keyword in low:
                return code
        return None


_REGISTRY: dict[str, SportSpec] = {}


def register(spec: SportSpec) -> SportSpec:
    _REGISTRY[spec.key] = spec
    return spec


def get_sport(key: str) -> SportSpec:
    from klugscheisser.sports import floorball, icehockey, ski_alpine  # noqa: F401  (register themselves)

    return _REGISTRY[key]


DEFAULT_SPORT = "icehockey_women"


def current_sport_key() -> str:
    """Sport of the current project; projects without a sport (Allgemein) keep the ice hockey defaults."""
    from klugscheisser import projects

    return projects.current().sport or DEFAULT_SPORT


def current_spec() -> SportSpec:
    try:
        return get_sport(current_sport_key())
    except KeyError:
        return get_sport(DEFAULT_SPORT)


def team_suffix() -> str:
    return current_spec().team_suffix


def national_team_key(ioc: str) -> str:
    """Key of a nation's team in the current project: "sui-women" (ice hockey), "sui-men" (floorball)."""
    return f"{ioc.lower()}-{team_suffix()}"


def home_team_key() -> str:
    """The project's home national team (Switzerland)."""
    from klugscheisser import projects

    return national_team_key(projects.current().home_nation)


# Wikidata QIDs of countries (IOC code -> QID) for seeding by nationality (P27)
COUNTRY_QIDS = {
    "SUI": "Q39",
    "USA": "Q30",
    "CAN": "Q16",
    "CZE": "Q213",
    "GER": "Q183",
    "AUT": "Q40",
    "FIN": "Q33",
    "SWE": "Q34",
    "JPN": "Q17",
    "DEN": "Q35",
    "NOR": "Q20",
    "ITA": "Q38",
    "FRA": "Q142",
    "SLO": "Q215",
    "CRO": "Q224",
    "SVK": "Q214",
    "POL": "Q36",
    "LIE": "Q347",
    "GBR": "Q145",
    "ESP": "Q29",
    "AND": "Q228",
    "BEL": "Q31",
    "NED": "Q55",
    "HUN": "Q28",
}
# citizenship (P27) is often the kingdom, not the country: Danish players have "Kingdom of Denmark" (Q756617)
EXTRA_CITIZENSHIP_QIDS = {"DEN": ("Q756617",), "NED": ("Q29999",)}


def citizenship_qids(ioc: str) -> tuple[str, ...]:
    """All Wikidata QIDs that mean this nationality in P27 (KeyError for unknown codes)."""
    return (COUNTRY_QIDS[ioc], *EXTRA_CITIZENSHIP_QIDS.get(ioc, ()))
