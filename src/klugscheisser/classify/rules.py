"""Keyword classifier (de/en/fr). Free and offline; the default until Jev or an LLM is configured.

Deliberately cautious on sensitivity: a single health or private-life cue raises the flag,
because a missed "Vorsicht" is worse than an extra one.
"""

from __future__ import annotations

import re

from klugscheisser.classify.base import ClassificationResult, ClassifyInput
from klugscheisser.domain.sport import sport_verdict


def _rx(*words: str) -> re.Pattern:
    return re.compile(r"(?<![a-zäöü])(" + "|".join(words) + r")", re.IGNORECASE)


CUES = {
    "injury": _rx(
        r"verletz",
        r"injur",
        r"krücke",
        r"krücken",
        r"crutch",
        r"gips\b",
        r"\bcast\b",
        r"rollstuhl",
        r"knie",
        r"knee",
        r"kreuzband",
        r"\bacl\b",
        r"\bop\b",
        r"operation",
        r"surgery",
        r"reha",
        r"rehab",
        r"gehirnerschütterung",
        r"concussion",
        r"blessure",
        r"out for",
        r"fällt .{0,20}aus",
        r"ausfall",
        r"gebrochen",
        r"broken",
        r"krank",
        r"illness",
    ),
    "controversy": _rx(
        r"sperre",
        r"gesperrt",
        r"suspend",
        r"suspension",
        r"kritik",
        r"criticism",
        r"protest",
        r"doping",
        r"\bban\b",
        r"skandal",
        r"scandal",
        r"vorwurf",
        r"allegation",
        r"strafe",
        r"disziplinar",
        r"diskriminier",
        r"belästig",
        r"harass",
    ),
    "private_life": _rx(
        r"hochzeit",
        r"wedding",
        r"verlob",
        r"engaged",
        r"engagement",
        r"\bbaby\b",
        r"schwanger",
        r"pregnan",
        r"freund(in)?\b",
        r"boyfriend",
        r"girlfriend",
        r"partnerin",
        r"familie",
        r"family",
        r"urlaub",
        r"vacation",
        r"ferien",
        r"geburtstag",
        r"birthday",
        r"beerdigung",
        r"funeral",
        r"verstorben",
        r"passed away",
        r"trauer",
        r"mental health",
        r"psychisch",
        r"burnout",
    ),
    "sponsor": _rx(
        r"#ad\b",
        r"#werbung",
        r"werbung",
        r"anzeige",
        r"sponsor",
        r"partner(ship)?\b",
        r"zusammen mit @",
        r"in collaboration",
        r"rabatt",
        r"discount",
        r"\bcode\b",
        r"#partner",
    ),
    "team_coach": _rx(
        r"unterschreib",
        r"unterzeichn",
        r"signed",
        r"signs",
        r"signing",
        r"vertrag",
        r"contract",
        r"draft",
        r"transfer",
        r"wechsel",
        r"wechselt",
        r"neu bei",
        r"trainer",
        r"coach",
        r"captain",
        r"kapitän",
        r"captains team",
        r"kader",
        r"roster",
        r"aufgebot",
        r"selektion",
        r"selected",
        r"nominier",
    ),
    "result": _rx(
        r"\d+\s*[:\-]\s*\d+",
        r"sieg",
        r"gewonnen",
        r"\bwin\b",
        r"\bwon\b",
        r"victory",
        r"niederlage",
        r"\bloss\b",
        r"\btor(e)?\b",
        r"\bgoal",
        r"assist",
        r"punkte",
        r"points",
        r"medaille",
        r"medal",
        r"bronze",
        r"silber",
        r"silver",
        r"gold",
        r"meister",
        r"champion",
        r"final",
        r"playoff",
        r"shutout",
        r"overtime",
        r"verlängerung",
        r"penalty",
    ),
    "training": _rx(
        r"training",
        r"practice",
        r"camp",
        r"trainingslager",
        r"vorbereitung",
        r"preseason",
        r"workout",
        r"gym",
        r"saisonstart",
        r"season (just )?started",
        r"erste[sn]? (gemeinsame )?training",
    ),
}

STORYLINE_CUES = {
    "comeback": _rx(
        r"comeback", r"zurück auf dem eis", r"back on (the )?ice", r"nach .{0,30}pause", r"retour"
    ),
    "first_appearance": _rx(
        r"debüt", r"debut", r"erste[sn]? (spiel|einsatz|länderspiel)", r"first (ever )?game"
    ),
    "milestone": _rx(
        r"rekord",
        r"record",
        r"\d{2,4}\.\s*(spiel|tor|punkt)",
        r"all-time",
        r"historisch",
        r"history books",
        r"first[- ]ever",
        r"meilenstein",
        r"milestone",
        r"olympian",
    ),
    "transfer": _rx(
        r"unterschreib",
        r"signed",
        r"signs",
        r"draft",
        r"transfer",
        r"wechsel",
        r"neu bei",
        r"zieht es nach",
        r"selects",
    ),
}
SENSITIVE_EXTRA = _rx(
    r"krank",
    r"spital",
    r"hospital",
    r"trauer",
    r"verstorben",
    r"mental",
    r"psych",
    r"schwanger",
    r"pregnan",
    r"rücktritt",
    r"retire",
)
HEALTH_WORDS = _rx(r"krank", r"spital", r"hospital", r"mental", r"psych")
PREGNANCY_WORDS = _rx(r"schwanger", r"pregnan")
GRIEF_WORDS = _rx(r"trauer", r"verstorben")
RETIRE_WORDS = _rx(r"rücktritt", r"retire")
# Which sport a text is about: domain/sport.py (specific cues; other sports outweigh; generic words like
# Nati / WM / Spiel never decide alone; the URL section only when the text names no sport).

SCORE = re.compile(r"(?<!\d)\d{1,2}:\d{1,2}(?!\d)(?!\s*(Uhr|h\b))")
PRIORITY = ["injury", "controversy", "team_coach", "result", "private_life", "sponsor", "training"]
BASE_NEWS = {
    "injury": 4,
    "controversy": 4,
    "team_coach": 3,
    "result": 3,
    "private_life": 2,
    "sponsor": 2,
    "training": 2,
    "other": 1,
}


MOOD_CUES = [
    (
        "verletzung",
        _rx(
            r"verletz",
            r"injur",
            r"reha",
            r"kreuzband",
            r"\bop\b",
            r"surgery",
            r"krücke",
            r"crutch",
            r"gips\b",
            r"🤕",
            r"🩼",
        ),
    ),
    (
        "trauer",
        _rx(
            r"abschied",
            r"farewell",
            r"goodbye",
            r"come to an end",
            r"rücktritt",
            r"retire",
            r"traurig",
            r"heartbreak",
            r"💔",
            r"😢",
            r"😭",
        ),
    ),
    (
        "liebe",
        _rx(
            r"verlob", r"engaged", r"hochzeit", r"wedding", r"my love", r"freund(in)?\b", r"💍", r"💕", r"😍"
        ),
    ),
    ("aerger", _rx(r"frust", r"ärger", r"unfair", r"skandal", r"😡", r"😠")),
    ("humor", _rx(r"😂", r"🤣", r"lol\b", r"haha")),
    (
        "freude",
        _rx(
            r"sieg",
            r"\bwin\b",
            r"won\b",
            r"stolz",
            r"proud",
            r"medaille",
            r"medal",
            r"🥉",
            r"🥈",
            r"🥇",
            r"🎉",
            r"🔥",
            r"we did it",
            r"champion",
            r"meister",
        ),
    ),
]


class RuleBasedClassifier:
    name = "rules"
    version = "1.0"

    def classify(self, item: ClassifyInput) -> ClassificationResult:
        text = " ".join(filter(None, [item.text, item.image_description]))
        result = self.classify_text(text)
        if item.paid_partnership and result.category in ("other", "training"):
            result.category = "sponsor"
        if item.source_type in ("srf", "official_result"):
            result.relevant_p = max(result.relevant_p, 0.9)
        return result

    def classify_text(self, text: str) -> ClassificationResult:
        result = self._classify_text(text)
        result.topics = {k: 0.7 for k, rx in TOPIC_CUES.items() if rx.search(text)}
        # the extra sensitive words carry their reason (classify.base.vorsicht_reason: no reason, no Vorsicht)
        if HEALTH_WORDS.search(text):
            result.topics["verletzung"] = 0.7
        if PREGNANCY_WORDS.search(text):
            result.topics["beziehung"] = 0.7
        if GRIEF_WORDS.search(text) and result.mood in (None, "neutral"):
            result.mood = "trauer"
        if RETIRE_WORDS.search(text) and result.storyline is None:
            result.storyline = "karriereende"
        return result

    def _classify_text(self, text: str) -> ClassificationResult:
        hits = {cat: len(rx.findall(text)) for cat, rx in CUES.items()}
        hits["result"] += 2 * len(SCORE.findall(text))  # a score line is strong evidence of a game report
        cats = [c for c in PRIORITY if hits[c]]
        # sensitive topics win on any hit; otherwise the strongest signal, ties broken by PRIORITY
        sensitive_hit = next((c for c in ("injury", "controversy") if hits[c]), None)
        category = sensitive_hit or (
            max(cats, key=lambda c: (hits[c], -PRIORITY.index(c))) if cats else "other"
        )
        storyline = next((s for s, rx in STORYLINE_CUES.items() if rx.search(text)), None)
        verdict = sport_verdict(text)
        relevant_p = (
            0.3  # clearly another sport (women's football, skiing …)
            if verdict.is_other
            else 0.85
            if (verdict.looks_hockey() or category in ("result", "team_coach", "injury"))
            else (0.55 if category != "other" else 0.3)
        )
        sensitive = (
            hits["injury"] + hits["controversy"] + hits["private_life"] + len(SENSITIVE_EXTRA.findall(text))
        )
        sensitive_p = min(0.95, 0.2 + 0.25 * sensitive) if sensitive else 0.05
        news = BASE_NEWS[category] + (1 if storyline in ("transfer", "milestone", "comeback") else 0)
        if storyline == "transfer" and category == "team_coach":
            news += 1
        mood = next((name for name, rx in MOOD_CUES if rx.search(text)), "neutral")
        return ClassificationResult(
            mood=mood,
            relevant_p=relevant_p,
            category=category,
            category_p=0.6 if cats else 0.3,
            sensitive_p=round(sensitive_p, 2),
            newsworthiness=max(1, min(5, news)),
            storyline=storyline,
            classifier=self.name,
            version=self.version,
        )


# v5 topic tags without Jev (tests, outages): keywords in de/en/fr
TOPIC_CUES = {
    "studium": re.compile(r"studi|universit|college|hochschule|bachelor|master|ncaa|étudi", re.I),
    "beruf": re.compile(r"\bberuf|arbeitet als|teilzeit|\bjob\b|arbeitgeber|works as", re.I),
    "verletzung": re.compile(r"verletz|injur|operation|kreuzband|gehirnerschütterung|blessure", re.I),
    "comeback": re.compile(r"comeback|rückkehr|zurück auf dem eis", re.I),
    "transfer": re.compile(r"wechsel|transfer|vertrag|unterschreibt|draft|signs", re.I),
    "familie_sport": re.compile(r"schwester|bruder|tochter von|sister|brother", re.I),
    "meilenstein": re.compile(r"rekord|jubiläum|\b100\.|medaille|auszeichnung|mvp|record", re.I),
    "herkunft": re.compile(r"aufgewachsen|heimat|stammt aus|kindheit|grew up", re.I),
    "engagement": re.compile(r"botschafterin|gleichstellung|stiftung|engagiert sich|ambassador", re.I),
}
