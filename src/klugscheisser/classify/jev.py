"""Jev (TypeSafe) classifier and reranker.

API: POST {base}/v1/systemone  {"model", "state", "questions": {name: {type, instructions, criteria}}}
Question types: noul (yes/no probability), choice (one of a list), score (ordered levels).
Answers come back calibrated, so thresholds mean the same across questions.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from klugscheisser.classify.base import (
    CATEGORIES,
    MOODS,
    STORYLINES,
    TOPICS,
    ClassificationResult,
    ClassifyInput,
)
from klugscheisser.config import Settings

NEWS_LEVELS = [
    "1: belanglos, Alltagspost ohne Neuigkeit",
    "2: kleine Neuigkeit (Training, Werbung, Stimmung)",
    "3: erwähnenswert (Resultat, Leistung, Teamnews)",
    "4: wichtig (Transfer, Verletzung, Nomination, Rekord)",
    "5: grosse Story (Medaille, Karriereende, schwere Verletzung, Skandal)",
]

QUESTIONS: dict[str, dict] = {
    "relevant": {
        "type": "noul",
        "instructions": "Ist dieser Inhalt für eine Sportmoderatorin relevant, die über diese {person} "
        "berichtet oder sie interviewt?",
        "criteria": {
            "true": "betrifft ihren Sport, ihr Team, ihre Karriere, Gesundheit, öffentliche Rolle, Sponsoren – "
            "oder sie persönlich: Privatleben, Familie, Partnerschaft, Hobbys, Ausbildung, Beruf, Herkunft, "
            "Geschichten über sie (auch Boulevard)",
            "false": "ohne Bezug zur Athletin oder zum Sport (Spam, reine Fremdwerbung, unklarer Bezug)",
        },
    },
    "category": {
        "type": "choice",
        "instructions": "Hauptthema des Inhalts.",
        "criteria": {
            "result": "Spielresultat, Tore, Punkte, Leistung, Medaille, Titel",
            "training": "Training, Vorbereitung, Trainingslager, Saisonstart ohne Resultat",
            "injury": "Verletzung, Krankheit, Operation, Rehabilitation, Gesundheit",
            "team_coach": "Team, Trainer, Transfer, Vertrag, Draft, Nomination, Kader, Captain",
            "sponsor": "Sponsor, Werbung, bezahlte Partnerschaft, Produkt, Rabattcode",
            "private_life": "Privatleben: Familie, Beziehung, Ferien, Geburtstag, Freizeit",
            "controversy": "Kontroverse, Kritik, Sperre, Strafe, Konflikt, Skandal",
            "other": "nichts davon",
        },
    },
    "sensitive": {
        "type": "noul",
        "instructions": "Ist der Inhalt heikel für ein Live-Interview, so dass die Moderatorin vorsichtig "
        "sein muss?",
        "criteria": {
            "true": "Verletzung, Krankheit, psychische Gesundheit, Trauer, Beziehung/Familie, Schwangerschaft, "
            "Kontroverse, Sperre, Konflikt mit Team oder Verband, Karriereende",
            "false": "neutral oder positiv, unproblematisch anzusprechen",
        },
    },
    "newsworthiness": {
        "type": "score",
        "instructions": "Wie neuwertig ist das für eine TV-Sportmoderatorin vor einer Übertragung?",
        "criteria": NEWS_LEVELS,
    },
    "storyline": {
        "type": "choice",
        "instructions": "Passt der Inhalt zu einer dieser Geschichten?",
        "criteria": {
            "comeback": "Rückkehr nach Verletzung oder Pause",
            "first_appearance": "Debüt, erstes Spiel oder erster Einsatz",
            "milestone": "Rekord, Jubiläum (z. B. 100. Länderspiel), historischer Moment",
            "home_game": "Heimspiel oder Rückkehr in die Heimat",
            "transfer": "Vereinswechsel, Vertrag, Draft",
            "karriereende": "Rücktritt, Karriereende, Abschied vom Team oder längere Pause",
            "none": "keine dieser Geschichten",
        },
    },
    "mood": {
        "type": "choice",
        "instructions": "Grundstimmung des Inhalts aus Sicht der Athletin.",
        "criteria": {
            "freude": "Freude, Stolz, Erfolg, Feiern",
            "trauer": "Trauer, Abschied, Wehmut, Enttäuschung",
            "aerger": "Ärger, Frust, Kritik, Wut",
            "verletzung": "Verletzung, Krankheit, Rückschlag, Reha",
            "humor": "Witz, Ironie, lustiger Moment",
            "nachdenklich": "nachdenklich, reflektierend, ernst, persönlich",
            "liebe": "Liebe, Partnerschaft, Familie, romantisches Privatleben",
            "neutral": "sachlich, Information, Werbung ohne Gefühl",
        },
    },
}

# v5: topic tags in the same call (yes/no each, several can be true)
TOPIC_QUESTIONS: dict[str, dict] = {
    f"topic_{key}": {
        "type": "noul",
        "instructions": f"Enthält der Inhalt eine Information zum Thema: {meaning}?",
        "criteria": {
            "true": "der Inhalt sagt dazu etwas Konkretes über die betroffene Person",
            "false": "kein oder nur ein beiläufiger Bezug",
        },
    }
    for key, meaning in TOPICS.items()
}


def project_questions() -> dict[str, dict]:
    """QUESTIONS in the words of the current project (v7)."""
    from klugscheisser.projects import wording

    person = wording().person
    out = {}
    for name, q in QUESTIONS.items():
        q = dict(q)
        q["instructions"] = _men_words(q["instructions"]).replace("{person}", person)
        if isinstance(q.get("criteria"), dict):
            q["criteria"] = {k: _men_words(v) for k, v in q["criteria"].items()}
        out[name] = q
    return out


# 2026-11-24 (floorball project, men): the same questions about a male player; women's projects unchanged
MEN_WORDS = {
    "über diese {person} berichtet oder sie interviewt": "über diesen {person} berichtet oder ihn interviewt",
    "betrifft ihren Sport, ihr Team, ihre Karriere": "betrifft seinen Sport, sein Team, seine Karriere",
    "oder sie persönlich:": "oder ihn persönlich:",
    "Geschichten über sie": "Geschichten über ihn",
    "zur Athletin": "zum Athleten",
    "der Athletin": "des Athleten",
}


def _men_words(text: str) -> str:
    from klugscheisser.sports import current_spec

    if not current_spec().men:
        return text
    for women, men in MEN_WORDS.items():
        text = text.replace(women, men)
    return text


MAX_MAIN_PERSON_QUESTIONS = 8


def main_person_questions(names: tuple[str, ...]) -> dict[str, dict]:
    """Only when several persons are linked: who is the document really about?"""
    if len(names) < 2:
        return {}
    return {
        f"main_{i}": {
            "type": "noul",
            "instructions": f"Ist {name} eine Hauptperson dieses Inhalts (nicht nur in einer Liste oder am Rand "
            "erwähnt)?",
            "criteria": {
                "true": "der Inhalt handelt wesentlich von ihr oder zitiert sie",
                "false": "nur erwähnt, z. B. in einer Aufzählung oder Kaderliste",
            },
        }
        for i, name in enumerate(names[:MAX_MAIN_PERSON_QUESTIONS])
    }


ACCOUNT_KIND_QUESTION = {
    "kind": {
        "type": "choice",
        "instructions": "Was für ein Instagram-Account hat diesen Inhalt veröffentlicht?",
        "criteria": {
            "official": "offizieller Account eines Teams, einer Liga, eines Verbands oder Veranstalters",
            "media": "Medium, Zeitung, Sender, Sportjournalist oder Fotograf mit redaktionellem Inhalt",
            "boulevard": "Boulevard-, Klatsch- oder People-Medium",
            "brand": "Firma, Marke, Sponsor, Shop, Ausrüster",
            "fan": "Fan-Account, Privatperson, Fanseite, Community-Seite",
            "other": "nichts davon oder unklar",
        },
    }
}


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.model = settings.typesafe_model
        self.http = client or httpx.Client(
            base_url=settings.typesafe_base_url,
            timeout=20,
            headers={"Authorization": f"Bearer {settings.typesafe_api_key.get_secret_value()}"},
        )
        self.usage = {"input_tokens": 0}

    def ask(self, state: str, questions: dict[str, dict]) -> dict[str, dict]:
        from klugscheisser.llm import budget

        budget.check("jev")  # inside an answer: per-question budget and daily cap (llm/budget.py)
        r = self.http.post(
            "/v1/systemone", json={"model": self.model, "state": state, "questions": questions}
        )
        if r.status_code >= 400:
            raise JevError(f"Jev HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        for key, value in (data.get("usage") or {}).items():
            if isinstance(value, int | float):
                self.usage[key] = self.usage.get(key, 0) + value
        budget.charge_jev(int((data.get("usage") or {}).get("input_tokens", 0) or 0))
        answers = data.get("answers") or {}
        missing = set(questions) - set(answers)
        if missing:
            raise JevError(f"Jev did not answer: {sorted(missing)}")
        return answers


# --- answer parsing: tolerant to the documented field names (noul/choice/score + probabilities) ---


def noul_probability(answer: dict[str, Any]) -> float:
    probs = answer.get("probabilities")
    if isinstance(probs, dict):
        for key in ("true", True, "yes", "1"):
            if key in probs:
                return float(probs[key])
    if isinstance(answer.get("probability"), int | float):
        return float(answer["probability"])
    value = answer.get("noul", answer.get("answer"))
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    conf = float(answer.get("confidence", 1.0))
    return conf if value is True else 1 - conf


def choice_answer(answer: dict[str, Any], options: list[str]) -> tuple[str, float]:
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and probs:
        best = max(probs, key=lambda k: probs[k])
        return str(best), float(probs[best])
    if isinstance(probs, list) and len(probs) == len(options):
        i = max(range(len(options)), key=lambda j: probs[j])
        return options[i], float(probs[i])
    value = answer.get("choice", answer.get("answer"))
    if isinstance(value, int) and 0 <= value < len(options):
        value = options[value]
    return str(value), float(answer.get("confidence", 0.5))


def score_answer(answer: dict[str, Any], levels: int) -> float:
    """Expected level index 0..levels-1."""
    probs = answer.get("probabilities")
    if isinstance(probs, list) and len(probs) == levels:
        return sum(i * p for i, p in enumerate(probs))
    value = answer.get("score", answer.get("answer"))
    if isinstance(value, int | float):
        return float(value) if value <= levels - 1 else float(value) - 1
    return 0.0


class JevClassifier:
    name = "jev"

    def __init__(self, settings: Settings, client: JevClient | None = None) -> None:
        self.client = client or JevClient(settings)
        self.version = settings.typesafe_model

    def classify(self, item: ClassifyInput) -> ClassificationResult:
        mains = main_person_questions(item.athlete_names)
        a = self.client.ask(item.as_state(), {**project_questions(), **TOPIC_QUESTIONS, **mains})
        category, category_p = choice_answer(a["category"], CATEGORIES)
        storyline, _ = choice_answer(a["storyline"], STORYLINES)
        mood, _ = choice_answer(a["mood"], MOODS)
        level = score_answer(a["newsworthiness"], len(NEWS_LEVELS))
        return ClassificationResult(
            relevant_p=round(noul_probability(a["relevant"]), 3),
            category=category if category in CATEGORIES else "other",
            category_p=round(category_p, 3),
            sensitive_p=round(noul_probability(a["sensitive"]), 3),
            newsworthiness=max(1, min(5, round(level) + 1)),
            storyline=None if storyline in ("none", "None") else storyline,
            mood=mood if mood in MOODS else None,
            classifier=self.name,
            version=self.version,
            topics={
                key: round(noul_probability(a[f"topic_{key}"]), 3) for key in TOPICS if f"topic_{key}" in a
            },
            main_persons={
                name: round(noul_probability(a[f"main_{i}"]), 3)
                for i, name in enumerate(item.athlete_names[:MAX_MAIN_PERSON_QUESTIONS])
                if f"main_{i}" in a
            }
            or None,
        )


READER_CONTEXT = (  # template: reader_context() fills in the project's words
    "Die Nutzerin ist Sportmoderatorin und Journalistin beim Schweizer Fernsehen und berichtet über Schweizer "
    "{sport_topic}. Sie bereitet Sendungen, Kommentare und Interviews vor und braucht überprüfbare Fakten mit "
    "Datum und Quelle: Resultate, Statistiken, Kader, Verletzungen, Transfers, Zitate und Hintergrund zu "
    "{players} beider Teams. Massgebend ist, ob das Dokument die Frage beantwortet; Porträts zählen, wenn die "
    "Frage Hintergrund oder Karriere betrifft. Nur bei sonst gleicher Relevanz: das neuere Dokument vorziehen. "
    "Andere Sportarten und {other_gender_sport} sind nicht relevant, ausser die Frage fragt danach."
)


def reader_context() -> str:
    """READER_CONTEXT of the current project (ice hockey: "… Schweizer Frauen-Eishockey … Männer-Eishockey …")."""
    from klugscheisser.llm.words import fill
    from klugscheisser.projects import wording
    from klugscheisser.sports import current_spec

    other = f"{'Frauen' if current_spec().men else 'Männer'}-{wording().sport_de}"
    return fill(READER_CONTEXT.replace("{other_gender_sport}", other))


class JevReranker:
    """Score question per candidate: how well does the item answer the query."""

    name = "jev"
    LEVELS: ClassVar[list[str]] = [
        "0: kein Bezug",
        "1: entfernter Bezug",
        "2: passt teilweise",
        "3: beantwortet die Frage direkt",
    ]

    def __init__(
        self, settings: Settings, client: JevClient | None = None, reader_context: bool = False
    ) -> None:
        self.client = client or JevClient(settings)
        self.reader_context = reader_context  # search lab 2026-10-25: READER_CONTEXT first in the state

    def score(self, query: str, texts: list[str]) -> list[float]:
        frage = "zur Frage der Nutzerin" if self.reader_context else "zur Frage"
        questions = {
            f"c{i}": {
                "type": "score",
                "instructions": f"Wie gut passt Kandidat {i} {frage}: {query}",
                "criteria": self.LEVELS,
            }
            for i in range(len(texts))
        }
        state = "\n\n".join(f"Kandidat {i}: {t[:800]}" for i, t in enumerate(texts))
        if self.reader_context:
            state = f"Nutzerin: {reader_context()}\n\n{state}"
        answers = self.client.ask(state, questions)
        return [
            score_answer(answers[f"c{i}"], len(self.LEVELS)) / (len(self.LEVELS) - 1)
            for i in range(len(texts))
        ]


class JevPairReranker:
    """Search lab 2026-10-25 (switch JEV_PAIRWISE), as TypeSafe's re-ranking cookbook does it: one request per
    (question, document), the state a JSON object {frage, dokument: {titel, quelle, art, datum, über, text}},
    descriptive levels without number prefixes, requests in parallel (`workers`). Optional: the reader context
    (JEV_READER_CONTEXT, field `nutzerin` of the state) and a calibrated noul "contains evidence that answers the question"
    (`beleg`, used as cut-off by RERANK_NOUL_MIN). A failed request falls back to word overlap for that document."""

    name = "jev-pair"
    pairwise = True
    LEVELS: ClassVar[list[str]] = [
        "kein Bezug zur Frage",
        "entfernter Bezug",
        "passt teilweise",
        "beantwortet die Frage direkt",
    ]

    def __init__(
        self,
        settings: Settings,
        client: JevClient | None = None,
        workers: int | None = None,
        reader_context: bool | None = None,
        evidence: bool | None = None,
    ) -> None:
        if client is None:
            client = JevClient(settings)
            client.http = httpx.Client(
                base_url=settings.typesafe_base_url,
                timeout=20,
                headers=client.http.headers,
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=64),
            )
        self.client = client
        self.workers = workers or settings.jev_workers
        self.reader_context = settings.jev_reader_context if reader_context is None else reader_context
        self.evidence = (
            (settings.rerank_noul_min is not None or self.reader_context) if evidence is None else evidence
        )
        self.errors = 0

    def questions(self) -> dict[str, dict]:
        q = {
            "rel": {
                "type": "score",
                "instructions": "Wie gut passt das Dokument zur Frage der Nutzerin?",
                "criteria": self.LEVELS,
            }
        }
        if self.evidence:
            q["beleg"] = {
                "type": "noul",
                "instructions": "Enthält das Dokument Belege, die die Frage direkt beantworten?",
            }
        return q

    def state(self, query: str, doc) -> dict:
        st = {"frage": query, "dokument": doc if isinstance(doc, dict) else {"text": doc}}
        return {"nutzerin": reader_context(), **st} if self.reader_context else st

    def _one(self, query: str, doc) -> dict[str, float]:
        import time as _time

        state = self.state(query, doc)
        for attempt in range(3):
            try:
                a = self.client.ask(state, self.questions())
                out = {"score": score_answer(a["rel"], len(self.LEVELS)) / (len(self.LEVELS) - 1)}
                if "beleg" in a:
                    out["beleg"] = noul_probability(a["beleg"])
                return out
            except Exception as exc:
                if "429" in str(exc) and attempt < 2:
                    _time.sleep(0.5 * (attempt + 1))
                    continue
                if "budget" in type(exc).__name__.lower():
                    raise
                break
        self.errors += 1
        from klugscheisser.retrieval.search import FakeReranker

        text = doc if isinstance(doc, str) else " ".join(str(v) for v in doc.values())
        return {"score": FakeReranker().score(query, [text])[0], "fallback": 1.0}

    def judge(self, query: str, docs: list) -> list[dict[str, float]]:
        if not docs:
            return []
        from klugscheisser.projects import ContextPool

        with ContextPool(max_workers=min(self.workers, len(docs))) as pool:
            return list(pool.map(lambda d: self._one(query, d), docs))

    def score(self, query: str, texts: list) -> list[float]:
        return [x["score"] for x in self.judge(query, texts)]


def classify_account(
    client: JevClient, handle: str, full_name: str | None, sample_caption: str
) -> tuple[str, float]:
    """Jev decides what kind of account an unknown Instagram account is (fan, brand, media, ...)."""
    state = f"Instagram-Account: @{handle}\nAnzeigename: {full_name or '-'}\nBeispiel-Post: {sample_caption[:800]}"
    answer = client.ask(state, ACCOUNT_KIND_QUESTION)["kind"]
    kind, confidence = choice_answer(answer, list(ACCOUNT_KIND_QUESTION["kind"]["criteria"]))
    return kind, confidence
