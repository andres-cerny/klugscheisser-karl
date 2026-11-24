"""Floorball / Unihockey (2026-11-24): the men's World Championship project "unihockey"."""

from klugscheisser.sports import SportSpec, register

FLOORBALL_PLAYER = "Q23868946"  # Wikidata occupation "floorball player" (checked 2026-11-24)
FLOORBALL = "Q206763"  # sport (P641)
FLOORBALL_SKIP = ("results", "saisonwerte", "kaderwatch", "hockeycanada", "elite_prospects", "gamewatch")

FLOORBALL_MEN = register(
    SportSpec(
        key="floorball_men",
        name_de="Unihockey Männer",
        person_de="Unihockeyspieler",
        wikidata_occupation=FLOORBALL_PLAYER,
        wikidata_sport=FLOORBALL,
        wikidata_gender="Q6581097",  # male
        family="unihockey",
        team_suffix="men",
        men=True,
        nightly_skip=FLOORBALL_SKIP,
        positions={
            "goal": "G",
            "torh": "G",
            "keeper": "G",
            "målvakt": "G",
            "maalivahti": "G",
            "brankář": "G",
            "gardien": "G",
            "portier": "G",
            "defen": "D",
            "verteid": "D",
            "back": "D",
            "puolustaja": "D",
            "obránce": "D",
            "obrán": "D",
            "forward": "F",
            "stürm": "F",
            "sturm": "F",
            "center": "F",
            "centre": "F",
            "wing": "F",
            "flügel": "F",
            "anfall": "F",
            "hyökkääjä": "F",
            "útočník": "F",
            "útoč": "F",
            "attaqu": "F",
        },
        position_labels_de={"G": "Torhüter", "D": "Verteidiger", "F": "Stürmer"},
    )
)

FLOORBALL_WOMEN = register(
    SportSpec(
        key="floorball_women",
        name_de="Unihockey Frauen",
        person_de="Unihockeyspielerin",
        wikidata_occupation=FLOORBALL_PLAYER,
        wikidata_sport=FLOORBALL,
        family="unihockey",
        team_suffix="women",
        nightly_skip=FLOORBALL_SKIP,
        positions=dict(FLOORBALL_MEN.positions),
        position_labels_de={"G": "Torhüterin", "D": "Verteidigerin", "F": "Stürmerin"},
    )
)
