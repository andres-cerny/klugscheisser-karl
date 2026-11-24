from klugscheisser.sports import COUNTRY_QIDS as ALL_COUNTRY_QIDS
from klugscheisser.sports import SportSpec, register

ICEHOCKEY_WOMEN = register(
    SportSpec(
        key="icehockey_women",
        name_de="Eishockey Frauen",
        person_de="Eishockeyspielerin",
        wikidata_occupation="Q11774891",  # ice hockey player
        wikidata_sport="Q41466",  # ice hockey
        positions={
            "goal": "G",
            "torh": "G",
            "gardien": "G",
            "defen": "D",
            "verteid": "D",
            "forward": "F",
            "stürm": "F",
            "sturm": "F",
            "center": "F",
            "centre": "F",
            "wing": "F",
            "flügel": "F",
            # home-language Wikipedia rosters (sv, fi, cs, ...)
            "målvakt": "G",
            "maalivahti": "G",
            "brankář": "G",
            "bramk": "G",
            "portier": "G",
            "back": "D",
            "puolustaja": "D",
            "obránc": "D",
            "obránk": "D",
            "obroń": "D",
            "difens": "D",
            "anfall": "F",
            "hyökkääjä": "F",
            "útoč": "F",
            "napast": "F",
            "attacc": "F",
            "attaqu": "F",
        },
        position_labels_de={"G": "Torhüterin", "D": "Verteidigerin", "F": "Stürmerin"},
    )
)

# kept for older imports; the full table lives in klugscheisser.sports
COUNTRY_QIDS = {
    k: ALL_COUNTRY_QIDS[k] for k in ("SUI", "USA", "CAN", "CZE", "GER", "AUT", "FIN", "SWE", "JPN", "DEN")
}
