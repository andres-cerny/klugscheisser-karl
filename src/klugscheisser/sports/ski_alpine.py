"""Alpine skiing (v5 P4): women and men as separate sports, like the ice hockey categories."""

from klugscheisser.sports import SportSpec, register

# Wikidata occupation "alpine skier" (verified 2026-10-03; Q4270517 is the generic "skier", which would also
# match cross-country skiers, ski jumpers, ...)
ALPINE_SKIER = "Q4144610"
ALPINE_SKIING = "Q186222"  # sport (P641) of teams

SKI_ALPINE_WOMEN = register(
    SportSpec(
        key="ski_alpine_women",
        name_de="Ski alpin Frauen",
        person_de="Skirennfahrerin",
        wikidata_occupation=ALPINE_SKIER,
        wikidata_sport=ALPINE_SKIING,
    )
)

SKI_ALPINE_MEN = register(
    SportSpec(
        key="ski_alpine_men",
        name_de="Ski alpin Männer",
        person_de="Skirennfahrer",
        wikidata_occupation=ALPINE_SKIER,
        wikidata_sport=ALPINE_SKIING,
        wikidata_gender="Q6581097",  # male
    )
)
