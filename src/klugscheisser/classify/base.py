from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CATEGORIES = ["result", "training", "injury", "team_coach", "sponsor", "private_life", "controversy", "other"]
STORYLINES = ["comeback", "first_appearance", "milestone", "home_game", "transfer", "karriereende", "none"]
MOODS = ["freude", "trauer", "aerger", "verletzung", "humor", "nachdenklich", "liebe", "neutral"]
VORSICHT_CATEGORIES = {"injury", "controversy", "private_life"}

# v5: topic tags for cross-person questions ("Wer studiert?"), several per document
TOPICS = {
    "studium": "Studium, Ausbildung, Universität, College, Hochschule, Abschluss",
    "beruf": "Beruf oder Job neben dem Sport, Arbeitgeber, Arbeit, Teilzeitjob",
    "verletzung": "Verletzung, Krankheit, Operation, Ausfall, Rehabilitation",
    "comeback": "Rückkehr nach Verletzung, Pause oder Rücktritt",
    "transfer": "Vereinswechsel, neuer Vertrag, Draft, Vertragsverlängerung",
    "familie_sport": "Familienmitglieder im Sport (Schwester, Bruder, Eltern, Partner im Sport)",
    "meilenstein": "Rekord, Jubiläum, erstes/100. Spiel, Auszeichnung, Medaille",
    "beziehung": "Beziehung, Partnerin/Partner, Hochzeit, Verlobung (nur wenn die Person es selbst sagt)",
    "herkunft": "Herkunft, Heimatort, Kindheit, erster Verein, wie sie zum Sport kam",
    "engagement": "soziales Engagement, Botschafterin, Gleichstellung, Nachwuchsförderung, Stiftung",
}
TOPIC_DE = {
    "studium": "Studium",
    "beruf": "Beruf",
    "verletzung": "Verletzung",
    "comeback": "Comeback",
    "transfer": "Transfer",
    "familie_sport": "Familie im Sport",
    "meilenstein": "Meilenstein",
    "beziehung": "Beziehung",
    "herkunft": "Herkunft",
    "engagement": "Engagement",
}
TOPIC_MIN_P = 0.5  # a document "has" a topic from this probability

CATEGORY_DE = {
    "result": "Resultat",
    "training": "Training",
    "injury": "Verletzung/Gesundheit",
    "team_coach": "Team/Trainer/Transfer",
    "sponsor": "Sponsor/Werbung",
    "private_life": "Privatleben",
    "controversy": "Kontroverse",
    "other": "Anderes",
}
STORYLINE_DE = {
    "comeback": "Comeback",
    "first_appearance": "Debüt",
    "milestone": "Meilenstein",
    "home_game": "Heimspiel",
    "transfer": "Transfer/Wechsel",
    "karriereende": "Karriereende/Pause",
}

# mood -> (emoji, German meaning); neutral has no emoji
MOOD_EMOJI = {
    "freude": ("🎉", "Freude"),
    "trauer": ("😢", "Enttäuschung/Abschied"),
    "aerger": ("😠", "Ärger/Frust"),
    "verletzung": ("🤕", "Verletzung/Rückschlag"),
    "humor": ("😂", "Humor"),
    "nachdenklich": ("🤔", "nachdenklich"),
    "liebe": ("❤️", "Liebe/Privatleben"),
}

# source kind -> (emoji, German meaning)
SOURCE_KIND_EMOJI = {
    "self": ("📸", "von ihr selbst"),
    "official": ("✅", "offiziell"),
    "srf": ("📺", "SRF"),
    "public": ("📺", "öffentlich-rechtlich"),
    "media": ("📰", "Medienbericht"),
    "boulevard": ("🍿", "Gossip"),
    "fan": ("💬", "Fan-Account"),
    "brand": ("💸", "Werbung"),
    "result": ("🏒", "Resultat"),
    "web": ("🌐", "Web"),
    "profile": ("🪪", "Profil"),
    "other": ("🔗", "andere Quelle"),
}


def vorsicht_reason(c) -> str | None:
    """Why an item is "⚠️ Vorsicht", in a few German words; None = no stated reason (gap loop round 2: Jev flagged
    "Still thinking about this one." as sensitive with category "Anderes" – a flag without a reason is dropped).

    Works on a stored Classification and on a ClassificationResult (category, mood, storyline, topics)."""
    if c is None:
        return None
    topics = getattr(c, "topics", None) or {}

    def topic(key: str) -> bool:
        return (topics.get(key) or 0) >= TOPIC_MIN_P

    category, mood, storyline = c.category, getattr(c, "mood", None), getattr(c, "storyline", None)
    if category == "injury" or mood == "verletzung" or topic("verletzung"):
        return "Verletzung"
    if category == "controversy":
        return "Kontroverse"
    if storyline == "karriereende":
        return "Karriereende/Pause"
    if category == "private_life" or topic("beziehung"):
        return "Privatleben"
    if mood == "trauer" and category != "result":  # a lost game is no reason for caution, a loss in life is
        return "Trauer/Abschied"
    return None


def is_vorsicht(c) -> bool:
    """The Vorsicht flag as shown: flagged by the classifier *and* with a stated reason."""
    return bool(c is not None and c.vorsicht and vorsicht_reason(c))


@dataclass
class ClassificationResult:
    relevant_p: float
    category: str
    sensitive_p: float
    newsworthiness: int  # 1-5
    category_p: float | None = None
    storyline: str | None = None
    mood: str | None = None
    classifier: str = ""
    version: str = ""
    relevant: bool = True
    vorsicht: bool = False
    topics: dict[str, float] | None = None  # topic -> probability (v5)
    main_persons: dict[str, float] | None = None  # athlete name -> probability "Hauptperson" (v5)


@dataclass
class ClassifyInput:
    """What the classifier sees about one item."""

    text: str
    source_type: str
    author: str | None = None
    athlete_names: tuple[str, ...] = ()
    paid_partnership: bool = False
    image_description: str | None = None

    def as_state(self) -> str:
        who = ", ".join(self.athlete_names) or "unbekannt"
        lines = [
            f"Quelle: {self.source_type}" + (f" von @{self.author}" if self.author else ""),
            f"{_affected()}: {who} ({_topic()})",
        ]
        if self.paid_partnership:
            lines.append("Als bezahlte Partnerschaft markiert.")
        lines.append(f"Text: {self.text[:3000]}")
        if self.image_description:
            lines.append(f"Bild: {self.image_description}")
        return "\n".join(lines)


class Classifier(Protocol):
    name: str
    version: str

    def classify(self, item: ClassifyInput) -> ClassificationResult: ...


def _affected() -> str:
    from klugscheisser.sports import current_spec

    return "Betroffene Athleten" if current_spec().men else "Betroffene Athletin(nen)"


def _topic() -> str:
    from klugscheisser.projects import wording

    return wording().topic
