"""Answer engine: open questions answered from our documents, every statement with a verbatim quote and link.

Flow: scope (persons) → candidate documents (items + Instagram bios) → Jev rerank → the text model writes
(v17: Claude Sonnet; Gemini Flash-Lite with LLM_PROVIDER=gemini) statements that cite document ids and verbatim quotes → checks (document exists, quote verbatim, Jev says the
quote supports the statement, sensitive-topic rule) → optional web enrichment for gaps → AnswerResult.
The model never produces links: they come from our database.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.classify.base import MOOD_EMOJI, SOURCE_KIND_EMOJI, is_vorsicht
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.domain.labels import date_de, local, source_label
from klugscheisser.handles.service import usable_handles
from klugscheisser.ingest import costs
from klugscheisser.interfaces import AnswerBlock, AnswerResult, AnswerSentence, Statement
from klugscheisser.llm.text import add_usage
from klugscheisser.llm.words import fill, position_labels_de
from klugscheisser.projects import wording
from klugscheisser.retrieval import docs as _docs
from klugscheisser.sports import current_spec, home_team_key

log = logging.getLogger(__name__)

MAX_CANDIDATES = 150
TOP_DOCS = 15
DOC_CHARS = 1500
# relationships / health: allowed from every source (Ondřej, 07.10.2026), always marked Vorsicht and attributed
# in the sentence, so Fabi sees who says it and decides
SENSITIVE_KINDS = {"beziehung", "gesundheit"}

SYSTEM = (
    "Du beantwortest Fragen einer Sportmoderatorin ausschliesslich anhand der nummerierten Dokumente.\n"
    "Regeln:\n"
    "1. Jede Aussage stützt sich auf GENAU EIN Dokument (ID wie D3) und enthält ein wörtliches Zitat daraus: "
    "exakt kopiert, 3 bis 30 Wörter, keine Auslassungen, keine Übersetzung.\n"
    "2. Die Aussage sagt nicht mehr als das Zitat. Keine Vermutungen, keine Schlussfolgerungen "
    "(z. B. 'alum' heisst ehemalig, nicht aktuell).\n"
    "3. Beziehungen/Liebesleben und Gesundheit: aus jeder Quelle erlaubt, wenn das Dokument es ausdrücklich "
    "sagt; kategorie entsprechend setzen und die Quelle im Satz nennen ('Laut Blick …', '{Pronoun} schreibt auf "
    "Instagram …'). Nie aus Emojis oder Fotos ableiten.\n"
    "4. Eine Aussage pro Fakt, kurz, deutsch. Lieber weniger Aussagen als unbelegte.\n"
    "5. 'luecken': nur Teile DIESER Frage, die die Dokumente nicht beantworten (kurz). Keine allgemeinen "
    "Hinweise und keine Themen, nach denen nicht gefragt wurde.\n"
    "6. Aussagen nur über Personen, die beim Dokument unter 'über:' stehen (oder über das dort genannte "
    "Team). Andere Personen im Text (z. B. {other_team_players}) weglassen.\n"
    "7. 'Bisheriges Gespräch' dient nur dazu, Rückfragen zu verstehen ('{pronoun}', 'und X?', 'dazu'). "
    "Es ist keine Quelle: Aussagen nur aus den Dokumenten."
)


def prompt_words(system: str) -> str:
    """A system prompt with the current project's words (ice hockey: exactly the texts above)."""
    men = current_spec().men
    for key, value in {
        "{Pronoun}": "Er" if men else "Sie",
        "{other_team_players}": "Spielerinnen eines Frauenteams" if men else "Spieler eines Männerteams",
        "{the_oldest}": "der Älteste" if men else "die Älteste",
        "{the_youngest}": "der Jüngste" if men else "die Jüngste",
        "{her_en}": "his" if men else "her",
        "{of_the_coach}": "des Trainers" if men else "der Trainerin",
        "{forward}": position_labels_de().get("F", "Stürmerin"),
    }.items():
        system = system.replace(key, value)
    return fill(system)


SCHEMA = {
    "type": "object",
    "properties": {
        "statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "aussage": {"type": "string"},
                    "dok": {"type": "string"},
                    "zitat": {"type": "string"},
                    "kategorie": {"type": "string", "enum": ["beziehung", "gesundheit", "anderes"]},
                },
                "required": ["aussage", "dok", "zitat", "kategorie"],
            },
        },
        "luecken": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["statements", "luecken"],
}

# v12 prosa (web chat): Gemini writes the answer, every sentence / table row cites our documents with verbatim
# quotes; the checks below decide what is shown. Bump PROSA_VERSION when the prompt or schema changes (cache key).
PROSA_VERSION = "prosa-9"
SYSTEM_PROSA = (
    "Du beantwortest Fragen einer Sportmoderatorin ausschliesslich anhand der nummerierten Dokumente. "
    "Du schreibst eine Antwort in ganzen Sätzen, keine Liste von Belegen.\n"
    "Regeln:\n"
    "1. Jeder Satz und jede Tabellenzeile stützt sich auf 1 oder 2 Dokumente (ID wie D3) und nennt pro Dokument "
    "ein wörtliches Zitat daraus: exakt kopiert, 3 bis 30 Wörter, keine Auslassungen, keine Übersetzung.\n"
    "2. Ein Satz sagt nicht mehr als seine Zitate. Keine Vermutungen, keine Schlussfolgerungen "
    "(z. B. 'alum' heisst ehemalig, nicht aktuell). Ranglisten wie '{the_oldest}' nur, wenn ein Dokument sie "
    "ausdrücklich enthält (z. B. eine berechnete, sortierte Liste).\n"
    "3. Beziehungen/Liebesleben und Gesundheit: aus jeder Quelle erlaubt, wenn das Dokument es ausdrücklich "
    "sagt; kategorie entsprechend setzen und die Quelle im Satz nennen ('Laut Blick …', '{Pronoun} schreibt auf "
    "Instagram …'). Nie aus Emojis oder Fotos ableiten.\n"
    "4. Der erste Satz beantwortet die Frage direkt und nennt die Person bzw. das Team mit Namen. Danach so kurz, wie die Frage es erlaubt. Du wählst die "
    "Form: ein paar Sätze, ein längerer Text oder eine Tabelle (Ranglisten, Kader, Vergleiche, 'wer sind alle "
    "…'). In einer Tabelle hat jede Zeile ihre eigenen Belege. Schreib immer deutsch, auch wenn die Frage "
    "englisch ist; die Zitate bleiben in der Originalsprache.\n"
    "5. Sicherheit in Worten: Stützen zwei verschiedene Quellen einen Satz, schreib ihn als einfache Aussage. "
    "Nur eine Quelle: nenne sie ('Laut SRF …'). Dokument mit VORSICHT (Boulevard, Fan-Account): 'Blick "
    "berichtet, dass …'. Enthalten die Dokumente gar nichts zur Frage oder zu ihrem Thema, lass 'antwort' "
    "leer; enthalten sie Verwandtes, schreib das (siehe 6).\n"
    "6. 'luecken': nur Teile DIESER Frage, die die Dokumente nicht beantworten (kurz). Beantworten die "
    "Dokumente die Frage nur teilweise, schreib im Text die belegten Fakten, die am nächsten dran sind (z. B. "
    "statt einer fehlenden Aussage {of_the_coach}: was {pronoun} laut den Dokumenten getan hat), und in 'luecken', "
    "was fehlt. Schreib NIE einen Satz darüber, was die Dokumente nicht enthalten ('Die Dokumente enthalten "
    "keine …', 'Es gibt keine Angaben …'): das gehört nur in 'luecken'.\n"
    "7. Aussagen nur über Personen, die beim Dokument unter 'über:' stehen (oder über das dort genannte "
    "Team). Andere Personen im Text (z. B. {other_team_players}) weglassen.\n"
    "8. 'Bisheriges Gespräch' dient nur dazu, Rückfragen zu verstehen ('{pronoun}', 'und {the_youngest}?', 'and {her_en} "
    "club?'). Es ist keine Quelle: Sätze nur aus den Dokumenten.\n"
    "9. 'rueckfrage': nur wenn die Frage zu unklar ist (abgebrochen, ohne klares Subjekt, oder mehrere Spiele "
    "bzw. Anlässe passen): eine kurze Gegenfrage, 'antwort' dann leer. Sonst weglassen.\n"
    "10. 'naechste_fragen': bis zu 3 kurze Anschlussfragen, die diese Dokumente beantworten können.\n"
    "11. Keine Dokument-IDs (D3) in Sätzen, Tabellenzellen oder Fragen: die Belege stehen nur in 'belege'.\n"
    "14. Keine Schlüsse über das hinaus, was ein Dokument wörtlich sagt: aus gemeinsamen Toren wird nicht 'gleiche "
    "Linie', aus '{forward}' nicht 'Center'. Schreib dann die Belege selbst (z. B. 'Enzler und Stalder bereiteten "
    "sich im Spiel X gegenseitig Tore vor') und in 'luecken', was offen bleibt ('ob sie in derselben Linie "
    "spielen, ist nicht belegt').\n"
    "16. Übernimm Einschränkungen aus dem Dokument in Satz oder Spaltentitel ('unvollständig', 'nur erfasste "
    "Turniere', 'provisorisch', 'laut …', 'Stand …'): eine Zahl ohne ihre Einschränkung ist eine falsche Angabe.\n"
    "15. Fragt die Frage genauer, als die Dokumente es sagen (Flügel oder Center, aber gespeichert ist nur "
    "'{forward}'), dann antworte mit dem Gespeicherten ('X ist als {forward} geführt') und nenne in 'luecken', "
    "was fehlt — nicht 'antwort' leer lassen.\n"
    "13. Gib Werte vollständig wieder, wie sie im Dokument stehen: mit Jahr, Runde, Nummer, Saison, Einheit "
    "(z. B. 'Boston Fleet, PWHL-Draft 2023, 1. Runde, Nr. 3', nicht nur 'Boston Fleet').\n"
    "12. Enthält ein berechnetes Dokument eine Liste (Paare, Rangliste, Kader, Werte pro Person), dann: erster "
    "Satz = die Antwort bzw. die Zahl aus dem Dokument ('Ergebnis: …'); danach die Liste VOLLSTÄNDIG als "
    "Tabelle, eine Zeile pro Eintrag, Beleg = genau dieser Eintrag aus dem Dokument; zum Schluss ein Satz, für "
    "wen nichts gespeichert ist (falls das Dokument es nennt). Nie 'zahlreiche' oder 'mehrere' schreiben, "
    "wenn das Dokument die Zahl oder die Namen nennt."
)
_BELEGE = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "dok": {"type": "string"},
            "zitat": {"type": "string"},
            "kategorie": {"type": "string", "enum": ["beziehung", "gesundheit", "anderes"]},
        },
        "required": ["dok", "zitat", "kategorie"],
    },
}
SCHEMA_PROSA = {
    "type": "object",
    "properties": {
        "antwort": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "typ": {"type": "string", "enum": ["text", "tabelle"]},
                    "saetze": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"satz": {"type": "string"}, "belege": _BELEGE},
                            "required": ["satz", "belege"],
                        },
                    },
                    "spalten": {"type": "array", "items": {"type": "string"}},
                    "zeilen": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "zellen": {"type": "array", "items": {"type": "string"}},
                                "belege": _BELEGE,
                            },
                            "required": ["zellen", "belege"],
                        },
                    },
                },
                "required": ["typ"],
            },
        },
        "luecken": {"type": "array", "items": {"type": "string"}},
        "rueckfrage": {"type": "string"},
        "naechste_fragen": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["antwort", "luecken"],
}

# v16: when every sentence of an answer fails Jev, the writer had concluded more than its quotes say (a video
# «X on Glice Hockey» → «X hat auf Kunsteis trainiert»). One short call rewrites those sentences to say only what
# the quote says and what kind of source it is; Jev checks them again. Fabi then sees what we have.
SYSTEM_REPAIR = (
    "Du korrigierst Sätze einer Antwort für eine Sportjournalistin. Jeder Satz behauptet mehr, als sein Zitat "
    "belegt. Schreibe jeden Satz neu, sodass er nur noch sagt, was im Zitat und seinem Umfeld steht, und nenne, "
    "was die Quelle ist (z. B. «Ein Video mit dem Titel «…» zeigt …», «Laut Wikipedia …»). Keine "
    "Schlussfolgerungen, keine Angaben, die nicht im Zitat stehen, keine Wertung. Übernimm nichts aus der Frage "
    "(Verein, Liga, Ort, Zeit), was Zitat und Umfeld nicht selbst nennen: lass es weg. Ein Satz auf Deutsch, Namen "
    "ausgeschrieben. Trägt das Zitat zur Frage nichts bei: satz leer lassen."
)
SCHEMA_REPAIR = {
    "type": "object",
    "properties": {
        "saetze": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"nr": {"type": "integer"}, "satz": {"type": "string"}},
                "required": ["nr", "satz"],
            },
        }
    },
    "required": ["saetze"],
}


@dataclass
class Doc:
    key: str  # "D1"
    text: str
    meta: str  # shown to the model: source, date, who
    url: str
    quelle: str
    zeile_prefix: str
    item_id: int | None = None
    self_authored: bool = False  # the person's own post / bio / interview
    vorsicht: bool = False
    web: bool = False
    score: float = 0.0
    extra: dict = field(default_factory=dict)
    # v12: a document computed from stored data (e.g. "Olympia-Kader nach Geburtsdatum"); `basis` lists the
    # underlying sources [{"quelle", "url"}] shown under "Berechnet aus gespeicherten Daten"
    computed: bool = False
    basis: list[dict] = field(default_factory=list)


# --- quote check ---------------------------------------------------------------------------------------

_DOC_REF = re.compile(r"\bD\d+\b")  # internal document ids must never reach the user
_QUOTES = re.compile(r"[\"'«»„“”‚‘’`´]")
_SPACE = re.compile(r"\s+")


def norm_quote(text: str) -> str:
    t = _QUOTES.sub("", text).replace("…", "...").replace("[...]", "...")
    return _SPACE.sub(" ", t).strip(" .,;:!?-–—").casefold()


def quote_in(quote: str, document: str) -> bool:
    q = norm_quote(quote)
    return len(q.split()) >= 2 and q in norm_quote(document)


# --- documents -----------------------------------------------------------------------------------------


def _prefix(kind: str | None, mood: str | None, platform: str | None = None) -> str:
    emoji, meaning = SOURCE_KIND_EMOJI.get(kind or "other", SOURCE_KIND_EMOJI["other"])
    if platform == "x":
        emoji = "𝕏"
    out = f"{emoji} ({meaning})"
    if mood in MOOD_EMOJI:
        out += f" {MOOD_EMOJI[mood][0]} ({MOOD_EMOJI[mood][1]})"
    return out


def item_doc(item: m.Item, terms: list[str] | None = None) -> Doc:
    """A stored item as a document for Jev and the writer. Long texts: the start, and with `terms` (the search
    words) the part where they occur, not only the first DOC_CHARS characters."""
    c = item.classification
    text = " ".join(
        filter(None, [item.title, item.text, item.image_description and f"[Bild: {item.image_description}]"])
    )
    names = ", ".join(
        sorted(
            ia.athlete.full_name + (" (nur erwähnt)" if ia.main is False else "")
            for ia in item.athletes
            if ia.athlete
        )
    )
    if not names and item.teams:
        names = "Team " + ", ".join(sorted(it.team.name for it in item.teams if it.team))
    label = source_label(item.platform, item.source_name, item.published_at)
    kind = item.source_kind or ("web" if item.source_type == "web" else None)
    return Doc(
        key="",
        text=passage(text, terms, DOC_CHARS) if terms else text[:DOC_CHARS],
        meta=f"{label} · Art: {SOURCE_KIND_EMOJI.get(kind or 'other', ('', kind or 'andere'))[1]} · über: {names or '-'}",
        url=item.url,
        quelle=label,
        zeile_prefix=_prefix(kind, c.mood if c else None, item.platform),
        item_id=item.id,
        self_authored=item.source_type in ("athlete_post", "interview"),
        vorsicht=is_vorsicht(c),
        web=item.source_type == "web",
        extra={"published_at": item.published_at},
    )


# a sentence about what the documents do NOT say (belongs in "Was die Quellen nicht sagen", never shown as fact)
_ABSENCE = re.compile(
    r"\b(die |den )?(dokumente|quellen|unterlagen|daten)\b.{0,40}\b(enthalten|nennen|erwähnen|sagen|zeigen|"
    r"liefern|geben)\b.{0,20}\b(kein|keine|keinen|nicht|nichts)\b|\b(es gibt|liegen) (keine|nicht) "
    r"(angaben|informationen|aussagen|hinweise)|"
    r"\bin den (vorliegenden |gespeicherten |verfügbaren )?(dokumenten|quellen|daten)\b.{0,30}\bnicht\b.{0,15}"
    r"\b(aufgeführt|erwähnt|enthalten|vorhanden|genannt|zu finden|gefunden|belegt)",
    re.I,
)


# v13 interpreter readings, kept a few hours: the same question (with the same chat turn before it) is not read
# again. The answer cache already covers repeated questions without history; this helps follow-ups and
# re-asked questions after new data.
_READINGS: dict[str, tuple[float, object]] = {}
READING_TTL_S = 6 * 3600


def _unknown_persons(s: Session, interp, athletes: list[m.Athlete]) -> list[str]:
    """Persons the question names (interpreter) that we have nowhere: not in the database (also with a typo)
    and in no stored document. Only when the question found nobody at all; any trace → [] (search as usual)."""
    if interp is None or not interp.personen or athletes:
        return []
    from sqlalchemy import or_

    from klugscheisser.db import repo
    from klugscheisser.domain.matcher import AthleteMatcher

    matcher = AthleteMatcher(s)
    for name in interp.personen:
        if (
            repo.resolve_athlete(s, name) is not None
            or matcher.from_text(name)
            or matcher.from_text_fuzzy(name)
        ):
            return []
        surname = name.split()[-1]
        if len(surname) >= 4 and s.scalar(
            select(m.Item.id)
            .where(
                or_(
                    m.Item.title.ilike(f"%{name}%"),
                    m.Item.text.ilike(f"%{name}%"),
                    m.Item.text.ilike(f"%{surname}%"),
                )
            )
            .limit(1)
        ):
            return []
    return list(interp.personen)


def _no_data_result(question: str, names: list[str], update_note: str | None) -> AnswerResult:
    who = ", ".join(names)
    text = (
        f"Zu {who} haben wir keine Daten: die Person ist weder in unserer Datenbank noch in einem gespeicherten "
        "Dokument. Falls der Name anders geschrieben wird, bitte so fragen — oder mit «Füge … hinzu» "
        "vorschlagen lassen."
    )
    return AnswerResult(
        frage=question,
        datenstand="",
        statements=[],
        luecken=[f"keine gespeicherten Daten zu {who}"],
        aktualisierung=update_note,
        scope_hinweis=f"Person nicht gefunden: {who}",
        durchsucht="Datenbank und gespeicherte Dokumente: kein Treffer zum Namen",
        stil="prosa",
        antwort=[AnswerBlock(typ="text", saetze=[AnswerSentence(text, [], None, "sicher")])],
    )


# v16: a record ("Punkterekord", "Allzeit-Leaderin", "all-time") is a milestone that articles report; our season
# rows only add up points, so these questions go to the documents (S4: the career table hid "168 Punkte, Rekord")
RECORD = re.compile(r"rekord|bestmarke|allzeit|all[- ]time|\brecords?\b", re.I)


def _rules_answer_directly(s: Session, question: str, athletes: list[m.Athlete]) -> bool:
    """True when the rules alone give a finished answer from stored data for this wording: a stats table or a
    computed direct answer (facts, Länderspiele, teammates, coach, form, games …)."""
    from klugscheisser.answer import cross

    try:
        if not RECORD.search(question) and _stats_step(s, question, athletes, None) is not None:
            return True
        pi = cross.prosa_inputs(s, question, athletes, None)
    except Exception:  # never let the probe break an answer
        return False
    return bool(pi and pi.docs and pi.docs[0].computed and pi.docs[0].extra.get("direct"))


def _cached_reading(question: str, history: list[dict] | None, read):
    import hashlib
    from datetime import date

    from klugscheisser.domain.names import normalize

    last = history[-1] if history else {}
    raw = json.dumps(
        [
            normalize(question),
            last.get("frage", ""),
            (last.get("saetze") or [])[:3],
            date.today().isoformat(),
        ],
        ensure_ascii=False,
    )
    key = hashlib.sha256(raw.encode()).hexdigest()
    now = time.monotonic()
    hit = _READINGS.get(key)
    if hit and now - hit[0] < READING_TTL_S:
        return hit[1]
    value = read()
    if value is not None:
        from dataclasses import replace

        if len(_READINGS) > 1000:
            _READINGS.clear()
        _READINGS[key] = (now, replace(value, usage={}))  # served again it costs nothing
    return value


def _after(doc: Doc, until: datetime) -> bool:
    """v12: the document was published after the end of a game's date window."""
    published = doc.extra.get("published_at")
    if published is None or until is None:
        return False
    if published.tzinfo is None and until.tzinfo is not None:
        published = published.replace(tzinfo=until.tzinfo)
    elif until.tzinfo is None and published.tzinfo is not None:
        until = until.replace(tzinfo=published.tzinfo)
    return published > until


def bio_docs(athletes: list[m.Athlete]) -> list[Doc]:
    out = []
    for a in athletes:
        for h in usable_handles(a):
            bio = (h.profile or {}).get("biography")
            if not bio:
                continue
            fetched = (h.profile or {}).get("fetched_at")
            when = date_de(datetime.fromisoformat(fetched)) if fetched else "?"
            label = f"[Instagram-Bio @{h.handle}, Stand {when}]"
            out.append(
                Doc(
                    key="",
                    text=bio,
                    meta=f"{label} · Bio von {a.full_name} (von ihr selbst)",
                    url=f"https://www.instagram.com/{h.handle}/",
                    quelle=label,
                    zeile_prefix=_prefix("self", None),
                    self_authored=True,
                )
            )
    return out


EVENT_CUES = [
    (re.compile(r"olympi", re.I), "oly-"),
    (re.compile(r"\b(wm|weltmeisterschaft|world championships?|wch)\b", re.I), "wch-"),
]


def event_cues() -> list[tuple]:
    """EVENT_CUES of the current project: "WM" = the project's main tournament ("wfc-2026" → "wfc-")."""
    from klugscheisser import projects

    prefix = projects.current().main_event.split("-", 1)[0] + "-"
    return [(rx, prefix if p == "wch-" else p) for rx, p in EVENT_CUES]


SWISS_TEAM = re.compile(r"\b(nati|swiss|schweiz\w*|suisse|svizzera)\b", re.I)
GENERIC_TEAM = re.compile(r"\b(team|mannschaft|kader|roster|squad|nationalteam|national team)\b", re.I)


def scope_from_text(s: Session, question: str) -> tuple[list[int], str | None]:
    """Team/event named in the question ("Olympia-Team", "die Nati", "the team") → their persons + a note."""
    for rx, prefix in event_cues():
        if rx.search(question):
            ev = s.scalar(
                select(m.Event)
                .where(m.Event.key.startswith(prefix), m.Event.kind != "game")
                .order_by(m.Event.starts_at.desc().nulls_last())
            )
            if ev is not None:
                # v8: all teams' rosters are stored; "das Olympia-Team" means the Swiss players
                swiss = select(m.Team.id).where(m.Team.key == home_team_key())
                q = select(m.EventEntry.athlete_id).where(
                    m.EventEntry.event_id == ev.id,
                    m.EventEntry.team_id.in_(swiss),
                    m.EventEntry.role.in_(("G", "D", "F")) | m.EventEntry.role.is_(None),
                )
                ids = list(s.scalars(q))
                if ids:
                    return ids, f"{ev.name}: Schweizer Kader ({len(ids)} Personen)"
    if SWISS_TEAM.search(question) or GENERIC_TEAM.search(question):
        from klugscheisser.retrieval.search import athlete_team_filter

        team = s.scalar(select(m.Team).where(m.Team.key == home_team_key()))
        if team is not None:
            ids = athlete_team_filter(s, team).athlete_ids or []
            note = f"Schweizer {wording().team_de}"
            if not SWISS_TEAM.search(question):
                note += " (angenommen; bei einem anderen Team bitte das Team nennen)"
            return ids, f"{note} ({len(ids)} Personen)"
    return [], None


def scope_athletes(
    s: Session, question: str, person_ids, team_id, event_id
) -> tuple[list[m.Athlete], str | None]:
    from klugscheisser.domain.matcher import AthleteMatcher
    from klugscheisser.retrieval.search import athlete_team_filter

    ids: list[int] = list(person_ids or [])
    note = None
    if ids:
        # 24.10.: frage(person='Luisa Waser', team='Schweiz') read 56 persons' 600 documents (9.8 s): a named
        # person is the scope; team/event only describe her
        return [s.get(m.Athlete, i) for i in dict.fromkeys(ids)], note
    if team_id:
        ids += athlete_team_filter(s, s.get(m.Team, team_id)).athlete_ids or []
    if event_id:
        ids += list(s.scalars(select(m.EventEntry.athlete_id).where(m.EventEntry.event_id == event_id)))
    if not ids:
        ids = [link.athlete_id for link in AthleteMatcher(s).from_text(question)]
    if not ids:
        ids, note = scope_from_text(s, question)
    if not ids:  # v12: a name with a typo ("Lara Staldr") — said in the note, so Fabi sees how it was read
        fuzzy = AthleteMatcher(s).from_text_fuzzy(question)
        if fuzzy:
            ids = [link.athlete_id for link, _ in fuzzy]
            names = ", ".join(s.get(m.Athlete, i).full_name for i in ids)
            note = f"Name verstanden als {names}"
    return [s.get(m.Athlete, i) for i in dict.fromkeys(ids)], note


# questions about stored person data (age, followers, numbers …) are answered from fact documents
STRUCTURED = re.compile(
    r"follower|älteste|jüngste|oldest|youngest|\balt\b|\balter\b|\bage\b|geboren|born|jahrgang|"
    r"nummer|number|trikot|jersey|position|verteidig|stürm|torhüt|goalie|defen|forward",
    re.I,
)
MAX_FACT_PERSONS = 60
MAX_CROSS_STATEMENTS = 80  # statements of one answer across persons (each is translated and sent)


def fact_docs(s: Session, athletes: list[m.Athlete]) -> list[Doc]:
    """Stored person data as quotable documents, each with its own source link (Wikidata, roster, Instagram
    profile). Deterministic text — no model writes these."""
    from klugscheisser.domain.labels import TZ

    today = datetime.now(TZ).date()
    out: list[Doc] = []
    labels = position_labels_de()
    for a in athletes[:MAX_FACT_PERSONS]:
        bits = []
        if a.birth_date:
            age = (
                today.year
                - a.birth_date.year
                - ((today.month, today.day) < (a.birth_date.month, a.birth_date.day))
            )
            bits.append(f"geboren am {a.birth_date:%d.%m.%Y} ({age} Jahre)")
        if a.position:
            bits.append(f"Position {labels.get(a.position, a.position)}")
        if a.nationality:
            bits.append(f"Nation {a.nationality}")
        if bits and a.wikidata_qid:
            label = f"[Wikidata {a.wikidata_qid}]"
            out.append(
                Doc(
                    key="",
                    text=f"{a.full_name}: {', '.join(bits)}.",
                    meta=f"{label} · Stammdaten · über: {a.full_name}",
                    url=f"https://www.wikidata.org/wiki/{a.wikidata_qid}",
                    quelle=label,
                    zeile_prefix=_prefix("profile", None),
                )
            )
        for en in s.scalars(select(m.EventEntry).where(m.EventEntry.athlete_id == a.id)):
            ev = s.get(m.Event, en.event_id)
            text = f"{a.full_name} steht im Kader {ev.name}" + (
                f" mit der Nummer {en.jersey_number}" if en.jersey_number else ""
            )
            label = f"[Kader {ev.name}{', ' + en.source if en.source else ''}]"
            out.append(
                Doc(
                    key="",
                    text=text + ".",
                    meta=f"{label} · Kader · über: {a.full_name}",
                    url=en.source_url or ev.source_url or "",
                    quelle=label,
                    zeile_prefix=_prefix("official", None),
                )
            )
        for h in usable_handles(a):
            p = h.profile or {}
            if p.get("followers") is None:
                continue
            when = date_de(datetime.fromisoformat(p["fetched_at"])) if p.get("fetched_at") else "?"
            label = f"[Instagram-Profil @{h.handle}, Stand {when}]"
            out.append(
                Doc(
                    key="",
                    text=f"Instagram @{h.handle} von {a.full_name}: {p['followers']} Follower.",
                    meta=f"{label} · Profilzahlen · über: {a.full_name}",
                    url=f"https://www.instagram.com/{h.handle}/",
                    quelle=label,
                    zeile_prefix=_prefix("self", None),
                )
            )
        out += _profile_fact_docs(s, a) if len(athletes) <= PROFILE_FACT_PERSONS else []
    return [d for d in out if d.url]


# v13 safety net: the profile fields of a few named persons (one document per source), so a question no rule
# understands ("Wo hat sie als Kind gespielt?") still finds "Juniorenklub: EHC Winterthur"
PROFILE_FACT_PERSONS = 3
PROFILE_FACT_KINDS = (
    "current_team",
    "jersey_number",
    "position",
    "shoots",
    "height_cm",
    "weight_kg",
    "birth_place",
    "youth_team",
    "contract",
    "draft",
    "agency",
    "college",
    "studies",
    "languages",
    "hometown",
    "nickname",
    "appearances",  # v14: Länderspiele, debut, milestones for the agent's data package
    "national_team_debut",
    "milestone",
)


def _profile_fact_docs(s: Session, a: m.Athlete) -> list[Doc]:
    from klugscheisser.facts.catalog import FACT_KINDS
    from klugscheisser.facts.source_site import cite_label, item_info, needs_site, row_site

    facts = s.scalars(
        select(m.Fact).where(
            m.Fact.athlete_id == a.id, m.Fact.kind.in_(PROFILE_FACT_KINDS), m.Fact.status == "confirmed"
        )
    ).all()
    info = item_info(s, {f.source_item_id for f in facts if needs_site(f.source)})
    by_source: dict[tuple[str, str], list[str]] = {}
    for f in facts:
        label = FACT_KINDS.get(f.kind, {}).get("de", f.kind)
        # web-type sources by site + date ("EV Zug, 16.12.2025"), never a bare "Web"
        name = cite_label(f.source, f.source_url, row_site(f, info), SOURCE_LABELS)
        by_source.setdefault((name, f.source_url), []).append(f"{label}: {f.value_text}")
    out = []
    for (name, url), bits in by_source.items():
        label = f"[{name}, Profil {a.full_name}]"
        out.append(
            Doc(
                key="",
                text=f"{a.full_name}: " + "; ".join(dict.fromkeys(bits)) + ".",
                meta=f"{label} · Profildaten · über: {a.full_name}",
                url=url or "",
                quelle=label,
                zeile_prefix=_prefix("profile", None),
            )
        )
    ep = s.get(m.EpProfile, a.id)
    form = ((ep.data or {}).get("form") or {}) if ep else {}
    if ep and form:
        from klugscheisser.answer.lookups import _form_line

        parts = [
            f"letzte {n} Spiele: {_form_line(form[k])}"
            for n, k in ((5, "last5"), (10, "last10"))
            if form.get(k)
        ]
        if parts:
            label = f"[Elite Prospects, Form {a.full_name}, Stand {ep.fetched_at:%d.%m.%Y}]"
            out.append(
                Doc(
                    key="",
                    text=f"{a.full_name}, Form: " + "; ".join(parts) + ".",
                    meta=f"{label} · Form · über: {a.full_name}",
                    url=ep.url,
                    quelle=label,
                    zeile_prefix=_prefix("profile", None),
                )
            )
    return out


STATUS_INJURY_DAYS = 365  # an injury report older than this is history, not status
INJURY_Q = re.compile(
    r"verletz|injur|ausfall|ausgefallen|operiert|operation|gesund|fit\b|fitness|blessé", re.I
)


def status_docs(s: Session, athletes: list[m.Athlete], docs: list[Doc] | None = None) -> list[Doc]:
    """Gap loop round 2: injury reports have no end ("Waser needs surgery (19.09.)" although she scored on 14.10.).
    For every person with an injury report of the last year one computed document: the report and what happened
    since (roster nominations, games with points from stored game reports). Also notes the status in the meta
    line of the injury documents in `docs` (the writer sees "seither: …" next to the old report)."""
    from klugscheisser.answer.cross import _basis, _computed_doc
    from klugscheisser.person_status import statuses

    today = datetime.now(UTC).date()
    out: list[Doc] = []
    states = statuses(s, athletes[:MAX_FACT_PERSONS])
    for st in states.values():
        inj = st.injury
        if inj is None or (today - inj.day).days > STATUS_INJURY_DAYS:
            continue
        after = st.since()
        head = f"{st.name}, aktueller Stand ({today:%d.%m.%Y}): letzte Verletzungsmeldung {inj.day:%d.%m.%Y} ({inj.text})."
        if after:
            body = (
                head
                + " Seither: "
                + "; ".join(x.text for x in after)
                + ". Die Verletzungsmeldung ist damit überholt: sie ist seither wieder nominiert bzw. im Einsatz."
            )
        else:
            body = head + " Seither weder ein Aufgebot noch ein Spiel in unseren Daten: Fitness offen."
        basis = _basis([(inj.quelle, inj.url), *((x.quelle, x.url) for x in after)])
        doc = _computed_doc(body, "Aktueller Stand", [st.name], basis)
        doc.vorsicht = not after  # an open injury stays Vorsicht; a resolved one is a plain status
        out.append(doc)
    ids = [d.item_id for d in docs or [] if d.item_id]
    if ids and states:
        injured = s.execute(
            select(m.ItemAthlete.item_id, m.ItemAthlete.athlete_id)
            .join(m.Classification, m.Classification.item_id == m.ItemAthlete.item_id)
            .where(
                m.ItemAthlete.item_id.in_(ids),
                m.ItemAthlete.athlete_id.in_(list(states)),
                m.Classification.category == "injury",
            )
        ).all()
        notes: dict[int, list[str]] = {}
        for item_id, aid in injured:
            d = next(d for d in docs if d.item_id == item_id)
            when = d.extra.get("published_at")
            day = local(when).date() if when else None
            if day and (since := states[aid].since_text(day)):
                notes.setdefault(item_id, []).append(f"{states[aid].name} {since}")
        for d in docs:
            if d.item_id in notes:
                d.meta += " · Stand heute: " + "; ".join(notes[d.item_id])
    return out


SOURCE_LABELS = {
    "eliteprospects": "Elite Prospects",
    "hockeycanada": "Hockey Canada",
    "wikidata": "Wikidata",
    "wikipedia": "Wikipedia",
    "roster": "Kader",
    "sihf": "sihf.ch",
}


def candidate_items(
    s: Session, settings: Settings, question: str, athletes: list[m.Athlete], since: datetime | None
) -> list[m.Item]:
    """Chat candidates on the shared path (retrieval/docs.candidate_items, also used by MCP search)."""
    from dataclasses import replace

    from klugscheisser.mcp_server.tools import detect_intent
    from klugscheisser.retrieval.search import Filters

    ids = [a.id for a in athletes] or None
    # no person in scope: only documents about someone we know (no men's-hockey or golf pages)
    f = Filters(athlete_ids=ids, since=since, linked_only=not ids)
    # off: Jev reads all of the persons' documents instead of the newest 150 plus vector hits
    cap = MAX_CANDIDATES if settings.retrieval_embeddings else settings.answer_jev_max_docs
    extra: list[_docs.Extra] = []
    # reports about her teams that do not name her ("Nati verliert in Finnland"), ranked by Jev
    if ids and (team_ids := person_team_ids(s, athletes)):
        extra.append(_docs.Extra(Filters(team_ids=team_ids, since=since), 40, matched=False))
    if intent := detect_intent(question):
        extra.append(_docs.Extra(replace(f, **intent), 40))
    # v5: cross-person questions ("Wer studiert?") — every document Jev tagged with the topic, complete
    if topics := question_topics(question):
        extra.append(_docs.Extra(replace(f, topics=topics), MAX_CANDIDATES, why="topic"))
    embedder = _docs.query_embedder(settings)
    items, _ = _docs.candidate_items(
        s, question, f, cap=cap, newest=cap if ids else 0, extra=extra, fts_k=40, embedder=embedder
    )
    return items


QUESTION_TOPIC_CUES = {
    "studium": re.compile(r"studi|universit|college|hochschule|ausbildung|abschluss|ncaa", re.I),
    "beruf": re.compile(
        r"beruf|arbeite|\bjob|neben dem (eishockey|unihockey|sport|skifahren)|teilzeit", re.I
    ),
    "verletzung": re.compile(
        r"verletz|injur|ausfall|ausgefallen|operiert|gesund|kreuzband|meniskus|gehirnersch|concussion|"
        r"\b(acl|lca)\b|fraktur|fractur",
        re.I,
    ),
    "comeback": re.compile(r"comeback|rückkehr|zurück", re.I),
    "transfer": re.compile(r"wechsel|transfer|vertrag|draft|neuer verein|neues team", re.I),
    "familie_sport": re.compile(r"schwester|bruder|familie|eltern|vater|mutter|geschwister", re.I),
    "meilenstein": re.compile(r"rekord|meilenstein|jubiläum|medaille|auszeichnung|mvp", re.I),
    "beziehung": re.compile(r"beziehung|freund(in)?\b|partner|verheiratet|hochzeit|verlobt", re.I),
    "herkunft": re.compile(r"herkunft|woher|aufgewachsen|heimat|kindheit|stammt", re.I),
    "engagement": re.compile(r"engagement|engagiert|botschafter|gleichstellung|stiftung", re.I),
}


def question_topics(question: str) -> list[str]:
    return [t for t, rx in QUESTION_TOPIC_CUES.items() if rx.search(question)]


def person_team_ids(s: Session, athletes: list[m.Athlete]) -> list[int]:
    """Current club and national-team ids of the persons (affiliations without end + event rosters)."""
    from klugscheisser.db import repo

    if len(athletes) > 5:  # a team/event scope already covers its teams through the players
        return []
    ids: set[int] = set()
    for a in athletes:
        ids.update(af.team_id for af in repo.current_affiliations(s, a))
        ids.update(
            t for t in s.scalars(select(m.EventEntry.team_id).where(m.EventEntry.athlete_id == a.id)) if t
        )
    return sorted(ids)


# moved to retrieval/docs.py (one path for chat and MCP search); kept importable from here
search_terms = _docs.search_terms
passage = _docs.passage


def rerank(settings: Settings, question: str, docs: list[Doc], terms: list[str] | None = None) -> list[Doc]:
    """Jev scores each document against the question; long documents are read at the passage with the most
    search words (`terms`, default: the question's words). Shared with MCP search: retrieval/docs.score_texts."""
    if not docs:
        return []
    terms = search_terms(question) if terms is None else terms
    scores = _docs.score_texts(
        settings,
        question,
        # the reranker reads 800 characters (chunk mode): meta + a passage that fits, no silent cut of its end
        [f"{d.meta}\n{passage(d.text, terms, 800 - min(len(d.meta) + 1, 200))}" for d in docs],
        fallback_texts=[f"{d.meta} {d.text}" for d in docs],
    )
    for d, sc in zip(docs, scores, strict=False):
        d.score = sc
    return sorted(docs, key=lambda d: d.score, reverse=True)


def fold_stories(s: Session, settings: Settings, docs: list[Doc]) -> list[Doc]:
    """CHAT_GROUP_STORIES (default off): documents of one story (retrieval/stories.py) become one, so the writer
    reads more distinct stories in its TOP_DOCS; the folded ones are in Doc.extra['auch']. Off: unchanged."""
    if not settings.chat_group_stories or len(docs) < 2:
        return docs
    from klugscheisser.retrieval import stories

    by_id = {i.id: i for i in _docs._load(s, [d.item_id for d in docs if d.item_id])}
    try:
        groups = stories.group(s, settings, [by_id.get(d.item_id) for d in docs], [d.score for d in docs])
    except Exception as exc:  # never fail an answer over grouping
        log.warning("chat story groups skipped: %s", str(exc)[:160])
        return docs
    out = []
    for g in groups:
        main = docs[g[0]]
        if len(g) > 1:
            main.extra["auch"] = [stories.also_entry(by_id[docs[i].item_id]) for i in g[1:]]
        out.append(main)
    return out


def _stats_step(s: Session, question: str, athletes: list[m.Athlete], history: list[dict] | None):
    """Stats from the season rows (no model). A follow-up ("Und wie viele Punkte hat sie diese Saison?")
    first takes the persons of the previous answer (stats.search.try_stats_with_history), else as before."""
    from klugscheisser.answer import cross
    from klugscheisser.stats import search

    if history and (cr := search.try_stats_with_history(s, question, athletes, history)) is not None:
        return cr
    return cross.try_stats(s, question, athletes)


def history_block(history: list[dict] | None, prosa: bool = False) -> str:
    """Previous chat turns for follow-ups. Context only: the model must not cite it.
    v12 prosa: the previous answer's own sentences (`saetze`, from chat.service.history) instead of the short
    summary, and the persons it was about (at most 12 names, not a whole roster)."""
    if not history:
        return ""
    lines = ["Bisheriges Gespräch (nur Kontext, keine Quelle):"]
    for turn in history:
        lines.append(f"- Frage: {turn['frage']}")
        if turn.get("personen"):
            names = list(turn["personen"])
            more = f" (+{len(names) - 12} weitere)" if prosa and len(names) > 12 else ""
            lines.append(f"  Personen: {', '.join(names[:12] if prosa else names)}{more}")
        if prosa and turn.get("saetze"):
            lines.append("  Antwort:")
            lines.extend(f"    - {x[:300]}" for x in turn["saetze"][:10])
        elif turn.get("antwort"):
            lines.append(f"  Antwort (Kurzfassung): {turn['antwort'][:600]}")
    return "\n".join(lines) + "\n\n"


def quote_context(quote: str, text: str, width: int = 350) -> str:
    """The text around the quote (the quote is verbatim, so its first words can be found)."""
    low = text.casefold()
    words = norm_quote(quote).split()
    for n in (6, 4, 2):
        probe = " ".join(words[:n])
        if probe and (pos := low.find(probe)) >= 0:
            return text[max(0, pos - width) : pos + len(quote) + width]
    return text[: 2 * width]


def SUPPORT_QUESTION(i: int) -> dict:
    return {
        "type": "noul",
        "instructions": (
            f"Paar {i}: Stützt das Zitat – gelesen im Textausschnitt und mit Quelle/Datum – die Aussage? "
            "Pronomen und Bezüge dürfen aus dem Ausschnitt und der Quelle aufgelöst werden. "
            "Nein, wenn die Aussage etwas Neues behauptet (andere Zeit wie 'aktuell' statt damals, anderer "
            "Verein, andere Person, Wertung, die nicht im Text steht)."
        ),
        "criteria": {
            "true": "die Aussage folgt aus Zitat und Kontext",
            "false": "die Aussage geht über Zitat und Kontext hinaus oder widerspricht ihnen",
        },
    }


# --- v12 prosa: checks and confidence wording ----------------------------------------------------------

# "[D3]", "(D1, D2)": dropped from the text; a bare "D3" becomes the source name (ids never reach the user)
_DOC_BRACKETS = re.compile(r"\s*[\[(]\s*D\d+(?:\s*[,;/]\s*D\d+)*\s*[\])]")
# the sentence already says where it comes from ("Laut SRF …", "Blick berichtet …", "sagt sie")
_ATTRIBUTION = re.compile(
    r"\b(laut|gemäss|gemäß|zufolge|nach angaben|berichte\w*|schreib\w*|schrieb\w*|sagt\w*|sagte\w*|erzähl\w*|"
    r"gepostet|postet\w*|meldet\w*|meldete\w*|teilt\w* mit|according)\b",
    re.I,
)


@dataclass
class _Claim:
    """One sentence or table row of the model's answer, with its checked evidence (doc, quote, kategorie)."""

    block: int
    text: str
    belege: list[tuple[Doc, str, str]]
    cells: list[str] = field(default_factory=list)
    cols: list[str] = field(default_factory=list)
    support: float | None = None
    ok: bool = False
    nums: list[int] = field(default_factory=list)

    def support_item(self) -> tuple[str, str, Doc]:
        """(quote, statement, doc) for Jev. A row reads "Spalte: Zelle | …"; two documents → one pair with both
        excerpts as context."""
        text = self.text
        if self.cells:
            cols = self.cols + [""] * len(self.cells)
            text = " | ".join(f"{c}: {v}" if c else v for c, v in zip(cols, self.cells, strict=False) if v)
        if len(self.belege) == 1:
            d, z, _ = self.belege[0]
            return z, text, d
        ctx = "\n…\n".join(f"[{d.quelle.strip('[]')}] {quote_context(z, d.text)}" for d, z, _ in self.belege)
        combined = Doc(
            key="",
            text=ctx,
            meta=" + ".join(d.meta for d, _, _ in self.belege),
            url="",
            quelle="",
            zeile_prefix="",
            extra={"context": ctx},
        )
        return " / ".join(z for _, z, _ in self.belege), text, combined


@dataclass
class ProsaWritten:
    antwort: list[AnswerBlock] = field(default_factory=list)
    quellen: list[Statement] = field(default_factory=list)  # all numbered sources
    cited: list[Statement] = field(default_factory=list)  # … of the shown answer (AnswerResult.statements)
    unsure_only: list[Statement] = field(default_factory=list)  # … only of "nicht sicher belegt" sentences
    nicht_sicher: list[AnswerSentence] = field(default_factory=list)
    luecken: list[str] = field(default_factory=list)
    rueckfrage: str | None = None
    naechste_fragen: list[str] = field(default_factory=list)
    dropped: int = 0
    usage: dict = field(default_factory=dict)


def source_name(d: Doc) -> str:
    """'[SRF, 03.10.]' → 'SRF'; '[Instagram-Bio @x, Stand 03.10.]' → 'Instagram-Bio @x'; '[Wikidata Q1]' →
    'Wikidata' (for "Laut SRF: …")."""
    label = (d.quelle or "").strip("[]").strip()
    while True:  # drop date / data-age / v14 data-package parts ("Profil X", "Form X", "Statistik X")
        head, _, tail = label.rpartition(", ")
        if head and (
            tail[:1].isdigit()
            or tail.startswith(("Stand", "Profil ", "Form ", "Statistik "))
            or tail == "ohne Datum"
        ):
            label = head
            continue
        break
    return re.sub(r"\s+Q\d+$", "", label) or "einer Quelle"


def _clean(text: str, by_key: dict[str, Doc]) -> str:
    t = _DOC_BRACKETS.sub("", text)
    t = _DOC_REF.sub(
        lambda mt: source_name(by_key[mt.group(0)]) if mt.group(0) in by_key else "eine Quelle", t
    )
    return _SPACE.sub(" ", t).strip()


def _doc_flags(d: Doc) -> str:
    """What the prosa writer needs to word its confidence (Part E)."""
    out = ""
    if d.vorsicht:
        out += " · VORSICHT (Boulevard/unsichere Quelle)"
    if d.computed:
        out += " · berechnet aus gespeicherten Daten"
    return out


def _checked_belege(raw, by_key: dict[str, Doc]) -> list[tuple[Doc, str, str]] | None:
    """All evidence of one sentence must pass (same rules as `_write`), else the sentence is dropped."""
    out = []
    for b in raw or []:
        if not isinstance(b, dict):
            return None
        d = by_key.get(str(b.get("dok", "")).strip())
        zitat, kat = str(b.get("zitat", "")).strip(), b.get("kategorie") or "anderes"
        if d is None or not quote_in(zitat, d.text):
            return None
        out.append((d, zitat, kat))
    return out or None


def _source(d: Doc, zitat: str, kat: str, text: str, support: float | None) -> Statement:
    url = d.url or next((b.get("url") for b in d.basis if b.get("url")), "")
    label = d.quelle.strip("[]") or "Quelle"
    return Statement(
        aussage=text,
        zitat=zitat,
        item_id=d.item_id,
        url=url,
        quelle=d.quelle or f"[{label}]",
        quelle_link=f"[{label}]({url})",
        zeile_prefix=d.zeile_prefix,
        vorsicht=d.vorsicht or kat in SENSITIVE_KINDS,
        web=d.web,
        support=support,
        berechnet=d.computed,
        basis=[dict(b) for b in d.basis],
    )


def _tone(belege: list[tuple[Doc, str, str]]) -> str:
    """Part E, from the real checks: Vorsicht source (or relationship/health) → vorsicht; computed from stored
    data or ≥2 distinct sources → sicher; else eine_quelle. (Low Jev support never gets here: nicht_sicher.)"""
    if any(d.vorsicht or kat in SENSITIVE_KINDS for d, _, kat in belege):
        return "vorsicht"
    if any(d.computed for d, _, _ in belege) or len({d.url or d.quelle for d, _, _ in belege}) >= 2:
        return "sicher"
    return "eine_quelle"


def _worded(text: str, ton: str, d: Doc) -> str:
    """Downgrade a plain statement the checks don't back as plain: one source → "Laut SRF: …", Vorsicht
    source → "Blick berichtet: …" (her own post → "Laut Instagram @x: …"). Kept when already attributed."""
    if ton not in ("eine_quelle", "vorsicht"):
        return text
    name = source_name(d)
    if _ATTRIBUTION.search(text) or name.casefold() in text.casefold():
        return text
    if ton == "vorsicht" and not d.self_authored:
        return f"{name} berichtet: {text}"
    return f"Laut {name}: {text}"


# --- the engine ----------------------------------------------------------------------------------------


class GeminiAnswerEngine:
    def __init__(
        self,
        settings: Settings,
        llm=None,
        support_checker=None,
        web_search=None,
        identity_checker=None,
        interpreter: bool | None = None,
        agent: bool | None = None,
        jev_judge=None,
    ) -> None:
        self.settings = settings
        # v13: the model reads the question first (answer/interpret.py). None = on with the real model when
        # `answer_interpreter` is set; tests with an injected llm switch it on explicitly
        self._interpreter = interpreter
        self._agent = agent  # v14: second stage; None = on with the real models when `answer_agent` is set
        self._jev_judge = jev_judge  # injectable for tests: callable(state, question) -> probability
        self._llm = llm  # injectable for tests: callable(system, prompt, schema) -> (dict, usage dict)
        self._support = support_checker  # injectable: callable(pairs) -> list[float]
        self._web = web_search
        self._identity = identity_checker  # injectable: callable(athlete, title, text) -> bool

    def _use_agent(self) -> bool:
        if self._agent is not None:
            return self._agent
        return self._llm is None and self.settings.answer_agent and self._use_interpreter()

    def _use_interpreter(self) -> bool:
        if self._interpreter is not None:
            return self._interpreter
        from klugscheisser.llm import text as llm_text

        return bool(
            self._llm is None and self.settings.answer_interpreter and llm_text.available(self.settings)
        )

    # model call --------------------------------------------------------------------------------------
    def _ask_model(self, prompt: str, system: str = SYSTEM, schema: dict = SCHEMA) -> tuple[dict, dict]:
        from klugscheisser.briefing import current_block

        # the project's words (2026-11-24) + v7: Fabi's project briefing (rules above always win)
        system = prompt_words(system) + current_block()[0]
        if self._llm is not None:
            from klugscheisser.llm import budget as llm_budget

            llm_budget.check("llm")  # the real model checks this in llm/text.py
            out, usage = self._llm(system, prompt, schema)
            llm_budget.charge_llm(self.settings, usage)
            return out, usage
        from klugscheisser.llm import text as llm_text

        out, usage = llm_text.generate(
            self.settings, system, prompt, schema=schema, role="answer", temperature=0.1
        )
        self._tally(usage)
        return out, usage

    def _tally(self, usage: dict) -> None:
        """Per-answer model usage (calls, input/cached/output tokens) for measuring; reset in `answer()`."""
        t = getattr(self, "last_usage", None)
        if t is None:
            t = self.last_usage = {"calls": 0, "input": 0, "cached": 0, "cache_write": 0, "output": 0}
        t["calls"] += 1
        for k in ("input", "cached", "cache_write", "output"):
            t[k] = t.get(k, 0) + int(usage.get(k, 0) or 0)

    def _support_scores(self, items: list[tuple[str, str, Doc]]) -> list[float | None]:
        """Jev: does the quote, read in its document (source, date, who, surrounding text), support the
        statement? Without the context, true statements like "Sie hat … entwickelt" from a brand's post that
        tags her fail (calibrated 2026-10-03 on 30 real cases, see tests/test_v5_answer.py)."""
        pairs = [(z, a) for z, a, _ in items]
        if self._support is not None:
            return self._support(pairs)
        if not pairs or not self.settings.features["jev"]:
            return [None] * len(pairs)
        from klugscheisser.classify.jev import JevClient, noul_probability

        client = JevClient(self.settings)
        # v12: a prosa sentence citing two documents brings its own context (both excerpts) in extra["context"]
        state = "\n\n".join(
            f"Paar {i}:\nQuelle: {d.meta}\nTextausschnitt: …{d.extra.get('context') or quote_context(z, d.text)}…\n"
            f"Zitat: «{z}»\nAussage: {a}"
            for i, (z, a, d) in enumerate(items)
        )
        questions = {f"p{i}": SUPPORT_QUESTION(i) for i in range(len(items))}
        try:
            answers = client.ask(state, questions)
            self._jev_tokens = client.usage.get("input_tokens", 0)
            return [noul_probability(answers[f"p{i}"]) for i in range(len(pairs))]
        except Exception as exc:
            log.warning("support check failed: %s", exc)
            return [None] * len(pairs)

    def _write(
        self, question: str, docs: list[Doc], who: list[str], history: list[dict] | None = None
    ) -> tuple[list[Statement], list[Statement], list[str], int, dict]:
        for i, d in enumerate(docs, 1):
            d.key = f"D{i}"
        listing = "\n\n".join(f"[{d.key}] {d.meta}\n{d.text}" for d in docs)
        prompt = (
            f"{history_block(history)}Frage: {question}\nPersonen im Fokus: {', '.join(who) or '-'}\n\n"
            f"Dokumente:\n{listing}"
        )
        from klugscheisser.llm.budget import AnswerBudgetExceeded

        try:
            out, usage = self._ask_model(prompt)
        except AnswerBudgetExceeded:  # daily cap / question budget used up: the best documents, no model
            return extractive_statements(docs), [], [], 0, {}
        by_key = {d.key: d for d in docs}
        candidates, dropped = [], 0
        for st in out.get("statements", []):
            d = by_key.get(str(st.get("dok", "")).strip())
            zitat, aussage = str(st.get("zitat", "")).strip(), str(st.get("aussage", "")).strip()
            if d is None or not aussage or not quote_in(zitat, d.text):
                dropped += 1
                continue
            candidates.append((d, aussage, zitat, st.get("kategorie")))
        scores = self._support_scores([(z, a, d) for d, a, z, _ in candidates])
        ok, unsure = [], []
        for (d, aussage, zitat, kat), sc in zip(candidates, scores, strict=False):
            stmt = Statement(
                aussage=aussage,
                zitat=zitat,
                item_id=d.item_id,
                url=d.url,
                quelle=d.quelle,
                quelle_link=f"[{d.quelle.strip('[]')}]({d.url})",
                zeile_prefix=d.zeile_prefix,
                vorsicht=d.vorsicht or kat in SENSITIVE_KINDS,
                web=d.web,
                support=sc,
            )
            (unsure if sc is not None and sc < self.settings.answer_support_threshold else ok).append(stmt)
        gaps = [
            _DOC_REF.sub(lambda mt: by_key[mt.group(0)].quelle if mt.group(0) in by_key else "eine Quelle", g)
            for g in out.get("luecken", [])
        ]
        return ok, unsure, gaps, dropped, usage

    # public ------------------------------------------------------------------------------------------
    def answer(
        self,
        question: str,
        person_ids: list[int] | None = None,
        team_id: int | None = None,
        event_id: int | None = None,
        since: datetime | None = None,
        web: bool = True,
        history: list[dict] | None = None,
        use_cache: bool = True,
        style: str | None = None,
    ) -> AnswerResult:
        """`style`: "liste" (default; MCP and v5–v11 web chat) or "prosa" (v12 web chat: written answer with
        numbered sources). The web chat passes the project's `web_answer_style`.

        2026-10-19: every answer runs inside a budget (llm/budget.py): `answer_max_usd` per question and the daily
        cap `llm_daily_cost_cap_usd`, checked before every paid call (model, Jev)."""
        from klugscheisser.llm import budget as llm_budget

        with llm_budget.answering(self.settings):
            return self._answer(
                question, person_ids, team_id, event_id, since, web, history, use_cache, style
            )

    def _finish_budget(self, result: AnswerResult) -> bool:
        """Books what the steps did not book, sets the answer's total cost and says what the limits left out.
        True when the answer was cut by a limit (then it is not cached)."""
        from klugscheisser.llm import budget as llm_budget

        b = llm_budget.current()
        if b is None:
            return False
        with session_scope() as s:
            b.finish(s, self.settings)
        if b.spent:
            result.kosten_usd = round(b.spent, 5)
        notes = list(b.notes)
        if b.refused:
            cap = b.daily_cap_usd
            if cap is not None and b.daily_spent_before + b.spent >= cap:
                notes.append(llm_budget.DAILY_NOTE)
            else:
                notes.append(
                    f"{llm_budget.QUESTION_NOTE} (${b.max_usd:.2f}): {b.refused} weitere bezahlte Schritte "
                    "ausgelassen (Dokumente nach Stichworten statt KI sortiert)."
                )
        for n in notes:
            if n not in result.luecken:
                result.luecken.append(n)
        if b.spent or b.refused:
            log.info(
                "answer budget: $%.4f (model $%.4f, Jev $%.4f, %d calls, %d refused), max $%.2f",
                b.spent,
                b.llm_usd,
                b.jev_usd,
                b.calls,
                b.refused,
                b.max_usd,
            )
        return bool(b.refused)

    def _answer(
        self,
        question: str,
        person_ids: list[int] | None,
        team_id: int | None,
        event_id: int | None,
        since: datetime | None,
        web: bool,
        history: list[dict] | None,
        use_cache: bool,
        style: str | None,
    ) -> AnswerResult:
        from klugscheisser.answer import cache
        from klugscheisser.briefing import current_block
        from klugscheisser.mcp_server.present import datenstand

        prosa = style == "prosa"
        briefing_id = current_block()[1]
        cache_key = None
        if use_cache and not history:
            cache_key = cache.key(
                question,
                person_ids,
                team_id,
                event_id,
                since,
                web,
                briefing_id,
                style="prosa" if prosa else "liste",
                version=PROSA_VERSION if prosa else None,
            )
            if (hit := cache.get(cache_key)) is not None:
                return hit

        started = time.monotonic()
        timings: dict[str, float] = {}

        def lap(step: str) -> None:
            timings[step] = round(time.monotonic() - started - sum(timings.values()), 2)

        update_note = None
        self.last_usage = {"calls": 0, "input": 0, "cached": 0, "cache_write": 0, "output": 0}
        interp = None
        direct = False
        if prosa and self._use_interpreter() and not history:
            # fast path (07.10.): when our rules already answer the original question from data (stats, age,
            # Länderspiele, teammates …), Gemini does not need to read it first (saves ~1 s)
            with session_scope() as s:
                a0, _ = scope_athletes(s, question, person_ids, team_id, event_id)
                direct = _rules_answer_directly(s, question, a0)
            lap("schnellweg")
        if prosa and self._use_interpreter() and not direct:  # v13: what is meant, as a question template
            from klugscheisser.answer.interpret import interpret

            interp = _cached_reading(
                question,
                history,
                lambda: interpret(lambda sy, pr, sc: self._ask_model(pr, sy, sc), question, history),
            )
            lap("lesen")
        rules_q = (interp.rules_question if interp else None) or question
        with session_scope() as s:
            if interp is not None and interp.usage:
                self._record_cost(s, interp.usage, note="frage-lesen")
            athletes, scope_note = scope_athletes(s, rules_q, person_ids, team_id, event_id)
            if athletes:
                from klugscheisser.ingest.profiles import ensure_profiles

                ensure_profiles(s, self.settings, athletes)  # Wikipedia for persons never looked up
            if athletes and len(athletes) <= 5 and self.settings.auto_refresh:
                from klugscheisser.jobs.freshness import ensure_fresh

                # never wait in a question: the update runs in the background, the answer uses what we have
                fr = ensure_fresh(s, self.settings, athletes, wait_s=0, fast_sources_now=False)
                if fr.status == "läuft":
                    update_note = (
                        "Tägliche Aktualisierung läuft im Hintergrund; diese Antwort nutzt den Stand davor."
                    )
            lap("scope")
            from klugscheisser.answer import cross

            cr = None
            eg = None
            if not prosa:
                # v11: stats questions are computed from the season rows (also as follow-ups, no model call)
                cr = _stats_step(s, question, athletes, history)
                if cr is None and not history and not person_ids:
                    # 2026-10-19: game questions about a tournament: its stored games first (answer/event_games.py)
                    from klugscheisser.answer import event_games

                    eg = event_games.find(s, question, event_id, team_id)
                if cr is None and eg is None and not history:
                    cr = cross.try_answer(self, s, question, athletes, scope_note, since, bool(person_ids))
            unknown = (
                _unknown_persons(s, interp, athletes)
                if prosa and not (person_ids or team_id or event_id)
                else []
            )
            if prosa:  # v12 web chat: written answer (ask-back / stats / cross documents / search → writer)
                if unknown:  # 07.10.: a person we have nowhere — say so at once instead of searching 15 s
                    lap("unbekannt")
                    result = _no_data_result(question, unknown, update_note)
                else:
                    result = self._answer_prosa(
                        s,
                        question,
                        athletes,
                        scope_note,
                        since,
                        history,
                        update_note,
                        lap,
                        rules_q=rules_q,
                        event_id=None if person_ids else event_id,
                        team_id=team_id,
                    )
                    if result.rueckfrage and not result.antwort:
                        cache_key = None  # an ask-back is cheap and depends on stored games: never cached
                    if (
                        self._use_agent() and _budget_left() >= AGENT_MIN_USD
                    ):  # v14: the fast answer is not enough
                        from klugscheisser.answer import agent

                        gate = agent.AgentRun()
                        if agent.needs_agent(self.settings, question, result, gate, self._jev_judge):
                            lap("pruefung-stufe1")
                            deeper = self._agent_answer(
                                s, question, rules_q, athletes, history, update_note, gate
                            )
                            lap("agent")
                            if deeper is not None and deeper.antwort:
                                result = deeper
                from klugscheisser.answer.interpret import changed

                if changed(question, interp):  # v13: Fabi sees how the question was read
                    read = f"Gelesen als: «{rules_q}»"
                    result.scope_hinweis = (
                        f"{read} · {result.scope_hinweis}" if result.scope_hinweis else read
                    )
            elif cr is not None:  # v6: question across people → facts / relations / evidence per person
                ok, unsure, gaps, dropped, usage, cross_note, durchsucht, ids = cr
                lap("quer")
                cost = self._record_cost(s, usage)
                ok.sort(key=lambda st: not st.vorsicht)
                if (
                    len(ok) > MAX_CROSS_STATEMENTS
                ):  # 24.10.: 562 stations → 33 s of quote translation, 140k chars
                    gaps = [
                        *gaps,
                        f"{len(ok) - MAX_CROSS_STATEMENTS} weitere Angaben nicht gezeigt (zu viele); enger fragen "
                        "(eine Person, «aktuell», ein Team).",
                    ]
                    ok = ok[:MAX_CROSS_STATEMENTS]
                translate_quotes(s, self.settings, ok + unsure)
                names = list(s.scalars(select(m.Athlete.full_name).where(m.Athlete.id.in_(ids))))
                result = AnswerResult(
                    frage=question,
                    datenstand=datenstand(s, ids or None),
                    statements=ok,
                    unbestaetigt=unsure,
                    luecken=gaps,
                    verworfen=dropped,
                    kosten_usd=cost,
                    personen=names,
                    person_ids=ids,
                    aktualisierung=update_note,
                    scope_hinweis=cross_note or scope_note,
                    durchsucht=durchsucht,
                )
            else:
                # a follow-up ("und was sagt sie dazu?") is searched together with the previous question
                search_q = f"{history[-1]['frage']} {question}" if history else question
                terms = search_terms(search_q)
                if eg is not None:  # the tournament's games: overview, official results, match reports
                    docs = top = list(eg.docs)
                    lap("spiele")
                else:
                    docs = [
                        item_doc(i, terms)
                        for i in candidate_items(s, self.settings, search_q, athletes, since)
                    ]
                    lap("kandidaten")
                    docs = rerank(self.settings, search_q, docs)
                    lap("rerank")
                    top = fold_stories(s, self.settings, [d for d in docs if d.score > 0])[
                        :TOP_DOCS
                    ] + bio_docs(athletes)
                    if 0 < len(athletes) <= 3 or INJURY_Q.search(search_q):
                        top += status_docs(s, athletes, top)  # injury reports with what happened since
                    if STRUCTURED.search(
                        f"{search_q}"
                    ):  # age, followers, numbers: all persons' facts, not ranked
                        top += fact_docs(s, athletes)
                who = [a.full_name for a in athletes]
                ok, unsure, gaps, dropped, usage = (
                    self._write(question, top, who, history) if top else ([], [], [question], 0, {})
                )
                lap("antwort+pruefung")
                self._unchecked: list[dict] = []
                web = web and self.settings.answer_web_search  # v5: off by default (Perplexity does the web)
                if web and (len(ok) < 2 or gaps) and self._web_available():
                    w_ok, w_unsure, gaps, w_dropped, w_usage = self._web_enrich(
                        s, question, athletes, gaps, history
                    )
                    ok, unsure, dropped = ok + w_ok, unsure + w_unsure, dropped + w_dropped
                    usage = add_usage(add_usage({}, usage), w_usage)
                cost = self._record_cost(s, usage)
                ok.sort(key=lambda st: not st.vorsicht)  # Vorsicht first
                translate_quotes(s, self.settings, ok + unsure)
                result = AnswerResult(
                    frage=question,
                    datenstand=datenstand(s, [a.id for a in athletes] or None),
                    statements=ok,
                    unbestaetigt=unsure,
                    luecken=gaps,
                    verworfen=dropped,
                    kosten_usd=cost,
                    personen=who,
                    person_ids=[a.id for a in athletes],
                    aktualisierung=update_note,
                    scope_hinweis=eg.note if eg is not None else scope_note,
                    durchsucht=eg.searched
                    if eg is not None
                    else f"{len(athletes)} Personen · {len(docs)} Dokumente geprüft"
                    if athletes
                    else f"{len(docs)} Dokumente geprüft (keine bestimmte Person erkannt)",
                    naechste_quellen=[]
                    if ok
                    else [
                        {
                            "quelle": d.quelle,
                            "url": d.url,
                            "zeile_prefix": d.zeile_prefix,
                            "titel": d.text[:140],
                        }
                        for d in docs[:3]
                        if d.score > 0
                    ],
                )
        lap("web+rest")
        result.zeiten = timings
        result.briefing_id = briefing_id
        if self._finish_budget(result):
            cache_key = None  # cut by a limit: the next ask (budget back) gets the full answer
        if cache_key is not None:
            cache.put(cache_key, question, result)
        log.info(
            "frage answered in %.1fs (%s): %d ok, %d unsure, %d dropped, $%.4f",
            time.monotonic() - started,
            " ".join(f"{k}={v}s" for k, v in timings.items()),
            len(result.statements),
            len(result.unbestaetigt),
            result.verworfen,
            result.kosten_usd,
        )
        return result

    # v12 prosa answers (web chat) ----------------------------------------------------------------------
    def _answer_prosa(
        self,
        s: Session,
        question: str,
        athletes: list[m.Athlete],
        scope_note: str | None,
        since: datetime | None,
        history: list[dict] | None,
        update_note: str | None,
        lap,
        rules_q: str | None = None,
        event_id: int | None = None,
        team_id: int | None = None,
    ) -> AnswerResult:
        """Ask-back (app, no model) → stats (computed, no model) → cross documents (facts / relations / a game,
        computed documents first) or the usual document search → Gemini writes, every sentence checked.
        v13: `rules_q` is the interpreter's template question: it drives the rules (ask-back, stats, computed
        answers); the original question goes to the document search and the writer."""
        from klugscheisser.answer import cross
        from klugscheisser.mcp_server.present import datenstand

        rq = rules_q or question
        ids = [a.id for a in athletes]
        names = [a.full_name for a in athletes]
        ab = cross.ask_back(s, rq, athletes, history)
        if ab is not None:  # Part D/F: too unclear, several games match, scope too wide
            return AnswerResult(
                frage=question,
                datenstand=datenstand(s, ids or None),
                statements=[],
                personen=names,
                person_ids=ids,
                aktualisierung=update_note,
                scope_hinweis=scope_note,
                stil="prosa",
                rueckfrage=ab.text,
                rueckfrage_vorschlaege=list(ab.vorschlaege)[:3],
            )
        if (
            not RECORD.search(f"{question} {rq}")
            and (cr := _stats_step(s, rq, athletes, history)) is not None
        ):
            lap("statistik")
            return self._stats_prosa(s, question, cr, scope_note, update_note)
        pi = cross.prosa_inputs(s, rq, athletes, history)
        if pi is not None:  # Part B/F: the scope the cross step used (a recognised game → its players)
            scope_note = pi.scope_note or scope_note
            if pi.athlete_ids:
                wanted = list(dict.fromkeys(pi.athlete_ids))
                found = {a.id: a for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(wanted)))}
                athletes = [found[i] for i in wanted if i in found]
                ids, names = [a.id for a in athletes], [a.full_name for a in athletes]
            since = pi.since or since
        if pi is not None and pi.docs and pi.docs[0].computed and pi.docs[0].extra.get("direct"):
            lap("quer")  # "wer hat mit wem zusammengespielt": count, full list, unknowns — from our data
            return self._direct_prosa(s, question, pi.docs[0], ids, names, scope_note, update_note)
        eg = None
        if not (pi is not None and pi.docs) and not history:
            from klugscheisser.answer import event_games  # 2026-10-19: game questions about a tournament

            eg = event_games.find(s, rq, event_id, team_id)
        if pi is not None and pi.docs:  # computed documents + per-person sources, also for follow-ups
            docs = list(pi.docs)
            top, closest = docs, docs[:3]
            durchsucht = f"{len(ids)} Personen · {len(docs)} Dokumente aus gespeicherten Daten"
            lap("quer")
        elif eg is not None:  # the tournament's games: overview, official results, match reports
            docs = top = list(eg.docs)
            closest = docs[:3]
            durchsucht, scope_note = eg.searched, eg.note
            lap("spiele")
        else:  # usual document search (with a recognised game: its players and the game's date window)
            search_q = f"{history[-1]['frage']} {question}" if history else question
            if rq != question:
                search_q = f"{search_q} {rq}"  # v13: the template adds the resolved names and terms
            terms = search_terms(search_q)
            docs = [item_doc(i, terms) for i in candidate_items(s, self.settings, search_q, athletes, since)]
            if pi is not None and pi.until is not None:
                docs = [d for d in docs if not _after(d, pi.until)]
            lap("kandidaten")
            docs = rerank(self.settings, search_q, docs)
            lap("rerank")
            found = fold_stories(s, self.settings, [d for d in docs if d.score > 0])[:TOP_DOCS] + bio_docs(
                athletes
            )
            top: list[Doc] = []  # page context first (same for every question on the page), then the hits
            if STRUCTURED.search(search_q) or 0 < len(athletes) <= 3:
                # v13 safety net: for one to three named persons their stored data always goes along
                # (youth club, contract, draft …), so a phrasing no rule knows still finds it
                top += fact_docs(s, athletes)
            elif len(athletes) > 3:
                # v14 context by page: a group (team/event page, "die Nati") gets one table per field
                # (Länderspiele, Team, Position …), so list questions are answered from our data
                from klugscheisser.answer.agent import group_tables

                top += group_tables(s, athletes)
            top += found
            if 0 < len(athletes) <= 3 or INJURY_Q.search(search_q):
                top += status_docs(s, athletes, found)  # injury reports with what happened since
            closest = [d for d in docs[:3] if d.score > 0]
            durchsucht = (
                f"{len(athletes)} Personen · {len(docs)} Dokumente geprüft"
                if athletes
                else f"{len(docs)} Dokumente geprüft (keine bestimmte Person erkannt)"
            )
        asked = question if rq == question else f"{question}\n(Gemeint ist: {rq})"
        out = self._write_prosa(asked, top, names, history) if top else ProsaWritten()
        lap("antwort+pruefung")
        cost = self._record_cost(s, out.usage)
        translate_quotes(s, self.settings, out.quellen)
        return AnswerResult(
            frage=question,
            datenstand=datenstand(s, ids or None),
            statements=out.cited,
            unbestaetigt=out.unsure_only,
            luecken=out.luecken,
            verworfen=out.dropped,
            kosten_usd=cost,
            personen=names,
            person_ids=ids,
            aktualisierung=update_note,
            scope_hinweis=scope_note,
            durchsucht=durchsucht,
            # Part E: nothing found → "Dazu habe ich nichts gefunden." + the closest documents we have
            naechste_quellen=[]
            if out.antwort
            else [
                {"quelle": d.quelle, "url": d.url, "zeile_prefix": d.zeile_prefix, "titel": d.text[:140]}
                for d in closest
                if d.url
            ],
            stil="prosa",
            antwort=out.antwort,
            quellen=out.quellen,
            nicht_sicher=out.nicht_sicher,
            rueckfrage=None if out.antwort else out.rueckfrage,
            naechste_fragen=out.naechste_fragen if out.antwort else [],
        )

    def _direct_prosa(
        self, s: Session, question: str, doc: Doc, ids, names, scope_note, update_note
    ) -> AnswerResult:
        """An answer built from a computed document without a model: the result sentence, the complete list as
        a table, and for whom nothing is stored. One numbered source: the computed document (+ its basis)."""
        from klugscheisser.mcp_server.present import datenstand

        d = doc.extra["direct"]
        ton = "vorsicht" if doc.vorsicht else "sicher"  # e.g. family in the sport (a sensitive kind)
        src = _source(doc, d["summary"], "anderes", d["summary"], None)
        blocks = [AnswerBlock(typ="text", saetze=[AnswerSentence(d["summary"] + ".", [1], None, ton)])]
        if d["rows"]:
            # each row: its cells, then the line as in the document (rows of 3 or more items)
            blocks.append(
                AnswerBlock(
                    typ="tabelle",
                    spalten=d.get("spalten") or [wording().players, "Gemeinsames Team (Saison)"],
                    zeilen=[
                        {
                            "zellen": list(row[:-1]),
                            "satz": AnswerSentence(f"{row[0]}: {', '.join(row[1:-1])}", [1], None, ton),
                        }
                        for row in d["rows"]
                    ],
                )
            )
        if d.get("unknown"):
            blocks.append(
                AnswerBlock(typ="text", saetze=[AnswerSentence(d["unknown"] + ".", [1], None, ton)])
            )
        return AnswerResult(
            frage=question,
            datenstand=datenstand(s, ids or None),
            statements=[src],
            personen=names,
            person_ids=ids,
            aktualisierung=update_note,
            scope_hinweis=scope_note,
            durchsucht=f"{len(ids)} Personen · berechnet aus gespeicherten Daten",
            stil="prosa",
            antwort=blocks,
            quellen=[src],
        )

    def _agent_answer(
        self, s, question, rules_q, athletes, history, update_note, gate
    ) -> AnswerResult | None:
        """v14 stage 2: plan → tools → Jev sorts / judges → the usual writer with all checks."""
        from klugscheisser.answer import agent
        from klugscheisser.mcp_server.present import datenstand

        run = agent.collect(
            self,
            s,
            question,
            rules_q,
            athletes,
            lambda sy, pr, sc: self._ask_model(pr, sy, sc),
            self._jev_judge,
        )
        run.jev_tokens += gate.jev_tokens
        if not run.docs:
            return None
        names = [a.full_name for a in athletes]
        asked = question if rules_q == question else f"{question}\n(Gemeint ist: {rules_q})"
        asked += (
            "\n(Vertiefte Suche: Teilantworten sind erwünscht — schreib, was die Dokumente zur Frage sagen, und "
            "in 'luecken', was fehlt. Lass 'antwort' nur leer, wenn kein Dokument etwas Verwandtes enthält.)"
        )
        out = self._write_prosa(asked, run.docs, names, history)
        cost = self._record_cost(s, out.usage, note="agent") + self._record_cost(
            s, run.llm_usage, note="agent"
        )
        if run.jev_tokens:
            jev_usd = run.jev_tokens / 1e6 * agent.JEV_USD_PER_M
            costs.record(s, "jev", run.jev_tokens, jev_usd, "agent")
            cost += jev_usd
        translate_quotes(s, self.settings, out.quellen)
        ids = [a.id for a in athletes]
        return AnswerResult(
            frage=question,
            datenstand=datenstand(s, ids or None),
            statements=out.cited,
            unbestaetigt=out.unsure_only,
            luecken=out.luecken,
            verworfen=out.dropped,
            kosten_usd=round(cost, 5),
            personen=names,
            person_ids=ids,
            aktualisierung=update_note,
            scope_hinweis="Vertiefte Suche",
            durchsucht=(
                f"Vertiefte Suche: {run.rounds} Runde{'n' if run.rounds != 1 else ''}, {len(run.searches)} Suchen "
                f"({', '.join(run.searches[:4])}), Werkzeuge: {', '.join(run.tools)} · {len(run.docs)} Dokumente"
            ),
            stil="prosa",
            antwort=out.antwort,
            quellen=out.quellen,
            nicht_sicher=out.nicht_sicher,
            naechste_fragen=out.naechste_fragen if out.antwort else [],
        )

    def _stats_prosa(self, s: Session, question: str, cr, scope_note, update_note) -> AnswerResult:
        """v11 stats rows (computed from Elite Prospects, never written by a model) as a prosa answer: one row
        → a sentence, several → a table; each row its own numbered source."""
        from klugscheisser.mcp_server.present import datenstand

        ok, _unsure, gaps, dropped, _usage, note, durchsucht, ids = cr
        translate_quotes(s, self.settings, ok)
        rows = [
            AnswerSentence(text=st.aussage, quellen=[i], support=st.support) for i, st in enumerate(ok, 1)
        ]
        if len(rows) > 1:
            blocks = [
                AnswerBlock(
                    typ="tabelle",
                    spalten=["Statistik"],
                    zeilen=[{"zellen": [r.text], "satz": r} for r in rows],
                )
            ]
        else:
            blocks = [AnswerBlock(typ="text", saetze=rows)] if rows else []
        by_id = dict(s.execute(select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(ids))).all())
        return AnswerResult(
            frage=question,
            datenstand=datenstand(s, ids or None),
            statements=ok,
            luecken=gaps,
            verworfen=dropped,
            personen=[by_id[i] for i in ids if i in by_id],
            person_ids=[i for i in ids if i in by_id],
            aktualisierung=update_note,
            scope_hinweis=note or scope_note,
            durchsucht=durchsucht,
            stil="prosa",
            antwort=blocks,
            quellen=list(ok),
        )

    def _write_prosa(
        self, question: str, docs: list[Doc], who: list[str], history: list[dict] | None = None
    ) -> ProsaWritten:
        """The model writes the answer (SYSTEM_PROSA); every sentence and table row is checked like a liste
        statement: each cited document exists, each quote is verbatim (`quote_in`), relationships/health only
        from the person herself — else dropped (`verworfen`). Then Jev: does the quote support the sentence?
        Below `answer_support_threshold_prosa` → not shown as fact (`nicht_sicher`, quotes in the sources)."""
        for i, d in enumerate(docs, 1):
            d.key = f"D{i}"
        listing = "\n\n".join(f"[{d.key}] {d.meta}{_doc_flags(d)}\n{d.text}" for d in docs)
        # (measured 07.10.: documents first for Gemini's prefix cache saved ~1 % of the cost but the answers got
        # less reliable — the question stays first)
        prompt = (
            f"{history_block(history, prosa=True)}Frage: {question}\nPersonen im Fokus: {', '.join(who) or '-'}"
            f"\n\nDokumente:\n{listing}"
        )
        from klugscheisser.llm.budget import AnswerBudgetExceeded

        try:
            out, usage = self._ask_model(prompt, SYSTEM_PROSA, SCHEMA_PROSA)
        except AnswerBudgetExceeded:  # daily cap / question budget used up: the best documents, no model
            return extractive_prosa(docs)
        by_key = {d.key: d for d in docs}
        gaps_from_text: list[str] = []
        res = ProsaWritten(usage=usage)
        claims: list[_Claim] = []
        shapes: list[tuple[str, list[str]]] = []  # per model block: (typ, columns)
        for raw in out.get("antwort") or []:
            if not isinstance(raw, dict):
                continue
            bi = len(shapes)
            if raw.get("typ") == "tabelle":
                cols = [_clean(str(c), by_key) for c in raw.get("spalten") or []]
                shapes.append(("tabelle", cols))
                for row in raw.get("zeilen") or []:
                    cells = [_clean(str(c), by_key) for c in (row or {}).get("zellen") or []]
                    belege = _checked_belege((row or {}).get("belege"), by_key)
                    if belege is None or not any(cells):
                        res.dropped += 1
                        continue
                    claims.append(_Claim(bi, " | ".join(cells), belege, cells=cells, cols=cols))
            else:
                shapes.append(("text", []))
                for st in raw.get("saetze") or []:
                    text = _clean(str((st or {}).get("satz", "")), by_key)
                    if text and _ABSENCE.search(text):  # "Die Dokumente enthalten keine …" is a gap, no fact
                        gaps_from_text.append(text)
                        continue
                    belege = _checked_belege((st or {}).get("belege"), by_key)
                    if belege is None or not text:
                        res.dropped += 1
                        continue
                    claims.append(_Claim(bi, text, belege))
        scores = self._support_scores([c.support_item() for c in claims])
        threshold = self.settings.answer_support_threshold_prosa
        computed_threshold = self.settings.answer_support_threshold_computed
        for c, sc in zip(claims, scores, strict=False):
            c.support = sc
            # a sentence resting only on our own computed documents (counts, sorted lists) gets a lower bar:
            # the quote is our calculation, Jev only checks that the sentence says no more than it
            only_computed = bool(c.belege) and all(d.computed for d, _z, _k in c.belege)
            c.ok = sc is None or sc >= (computed_threshold if only_computed else threshold)
        if claims and not any(c.ok for c in claims):
            self._repair_claims(question, claims, by_key, res)

        # sources numbered in first-citation order: the shown answer first, then the unsure sentences
        index: dict[tuple[str, str], int] = {}

        def numbers(c: _Claim) -> list[int]:
            nums = []
            for d, z, kat in c.belege:
                k = (d.key, norm_quote(z))
                if k not in index:
                    res.quellen.append(_source(d, z, kat, c.text, c.support))
                    index[k] = len(res.quellen)
                nums.append(index[k])
            return list(dict.fromkeys(nums))

        for c in claims:
            if c.ok:
                c.nums = numbers(c)
        n_cited = len(res.quellen)
        for c in claims:
            if not c.ok:
                res.nicht_sicher.append(AnswerSentence(c.text, numbers(c), c.support, "unsicher"))
        res.cited, res.unsure_only = res.quellen[:n_cited], res.quellen[n_cited:]

        for bi, (typ, cols) in enumerate(shapes):
            mine = [c for c in claims if c.block == bi and c.ok]
            if not mine:
                continue  # a block whose sentences all failed is not shown
            if typ == "tabelle":
                width = max([len(cols)] + [len(c.cells) for c in mine])
                rows = [
                    {
                        "zellen": c.cells + [""] * (width - len(c.cells)),
                        "satz": AnswerSentence(c.text, c.nums, c.support, _tone(c.belege)),
                    }
                    for c in mine
                ]
                res.antwort.append(
                    AnswerBlock(typ="tabelle", spalten=cols + [""] * (width - len(cols)), zeilen=rows)
                )
            else:
                saetze = []
                for c in mine:
                    ton = _tone(c.belege)
                    saetze.append(
                        AnswerSentence(_worded(c.text, ton, c.belege[0][0]), c.nums, c.support, ton)
                    )
                res.antwort.append(AnswerBlock(typ="text", saetze=saetze))
        res.luecken = [g for g in (_clean(str(x), by_key) for x in out.get("luecken") or []) if g]
        if gaps_from_text and not res.luecken:
            res.luecken = gaps_from_text[:2]
        res.rueckfrage = _clean(str(out.get("rueckfrage") or ""), by_key) or None
        res.naechste_fragen = [
            q[:160] for q in (_clean(str(x), by_key) for x in out.get("naechste_fragen") or []) if q
        ][:3]
        return res

    def _repair_claims(
        self, question: str, claims: list[_Claim], by_key: dict[str, Doc], res: ProsaWritten
    ) -> None:
        """v16 (SYSTEM_REPAIR): the failed sentences (at most 4, no table rows) rewritten to say only what
        their quotes say, with the same quotes; a rewrite that Jev now supports replaces the sentence."""
        weak = [c for c in claims if not c.cells and c.belege][:4]
        if not weak:
            return
        parts = []
        for i, c in enumerate(weak, 1):
            ev = "\n".join(
                f"  Zitat aus {d.meta}: «{z}»\n  Umfeld: {quote_context(z, d.text, 200)}"
                for d, z, _ in c.belege
            )
            parts.append(f"{i}. Satz: {c.text}\n{ev}")
        prompt = f"Frage: {question}\n\nSätze, die mehr behaupten als ihre Zitate:\n\n" + "\n\n".join(parts)
        try:
            out, usage = self._ask_model(prompt, SYSTEM_REPAIR, SCHEMA_REPAIR)
        except Exception as exc:
            log.warning("prosa repair failed: %s", exc)
            return
        add_usage(res.usage, usage)
        fixed: list[tuple[_Claim, str]] = []
        for x in (out or {}).get("saetze") or []:
            nr = x.get("nr") if isinstance(x, dict) else None
            text = _clean(str((x or {}).get("satz") or ""), by_key) if isinstance(x, dict) else ""
            if isinstance(nr, int) and 1 <= nr <= len(weak) and text and not _ABSENCE.search(text):
                fixed.append((weak[nr - 1], text))
        if not fixed:
            return
        scores = self._support_scores([_Claim(0, text, c.belege).support_item() for c, text in fixed])
        for (c, text), sc in zip(fixed, scores, strict=False):
            if sc is None or sc >= self.settings.answer_support_threshold_prosa:
                c.text, c.support, c.ok = text, sc, True

    # web enrichment (P4) -------------------------------------------------------------------------------
    def _web_available(self) -> bool:
        if self._web is not None:
            return True
        try:
            from klugscheisser.web_search import (
                brave,  # noqa: F401
                web_search_enabled,
            )

            return web_search_enabled(self.settings)
        except ImportError:
            return False

    def _web_enrich(
        self, s: Session, question: str, athletes: list[m.Athlete], gaps: list[str], history=None
    ):
        from klugscheisser.ingest.runner import build_context

        try:
            if self._web is None:
                from klugscheisser.web_search.brave import BraveSearch

                self._web = BraveSearch(self.settings, session=s)
            from klugscheisser.web_search.store import store_web_page
        except ImportError:
            return [], [], gaps, 0, {}
        names = " ".join(a.full_name for a in athletes[:3])
        sport_word = athletes[0].sport.name if athletes and athletes[0].sport else "Sport"
        asked = f"{history[-1]['frage']} {question}" if history else question
        query = f"{names} {sport_word} {asked}".strip()
        ctx = build_context(s, self.settings)
        docs = []
        for result in self._web.search(query, limit=self.settings.answer_web_results):
            page = self._web.fetch(result.url)
            if not page.allowed or not page.text:
                self._unchecked.append(
                    {"titel": result.title, "url": result.url, "hinweis": page.note or "nicht lesbar"}
                )
                continue
            about = [a for a in athletes if self._is_about(a, page.title, page.text)]
            if athletes and not about:
                continue  # namesake or unrelated page: never quote it for this person
            item = store_web_page(ctx, page, [a.id for a in about], query)
            if item is not None:
                docs.append(item_doc(item))
        if not docs:
            return [], [], gaps, 0, {}
        return self._write(question, docs, [a.full_name for a in athletes], history)

    def _is_about(self, athlete: m.Athlete, title: str, text: str) -> bool:
        """Identity check for web pages: is this page about THIS person (not a namesake)?"""
        if self._identity is not None:
            return self._identity(athlete, title, text)
        from klugscheisser.identity import is_about

        return is_about(self.settings, athlete, title, text)

    def _record_cost(self, s: Session, usage: dict, note: str = "frage") -> float:
        """Books the model usage of one step in cost_ledger. Jev and anything a step did not book is booked at the
        end of the answer from its budget (llm/budget.py), and `kosten_usd` is the budget's total."""
        from klugscheisser.llm import text as llm_text

        model_usd = llm_text.cost_usd(self.settings, usage)
        jev = getattr(self, "_jev_tokens", 0) / 1e6 * 0.042
        if model_usd:
            costs.record(s, llm_text.cost_source(self.settings, "answer"), 1, model_usd, note)
        return round(model_usd + jev, 5)


# --- without a model (daily cap / question budget used up) --------------------------------------------------

EXTRACT_DOCS = 5
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _excerpt(text: str, width: int = 260) -> str:
    """The start of a document, verbatim, cut at a sentence end (or a word) near `width` characters."""
    text = _SPACE.sub(" ", text or "").strip()
    if len(text) <= width:
        return text
    cut = [mt.start() for mt in _SENTENCE_END.finditer(text[: width + 1]) if mt.start() > 60]
    end = cut[-1] if cut else text.rfind(" ", 0, width)
    return text[: end if end > 0 else width].strip()


def _extract_docs(docs: list[Doc], n: int = EXTRACT_DOCS) -> list[Doc]:
    """Computed documents first (our own data), then the best ranked ones that have a source link."""
    usable = [d for d in docs if (d.url or d.basis) and d.text.strip()]
    return sorted(usable, key=lambda d: not d.computed)[:n]


def _quoted(d: Doc) -> tuple[str, str]:
    """(statement, quote) without a model: the quote is the document's start (computed documents: longer); the
    statement only names the source and the start, so nothing is claimed beyond the quote."""
    z = _excerpt(d.text, 600 if d.computed else 260)
    title = _excerpt(d.text, 120)
    return f"Gespeichert ({source_name(d)}): {title}", z


def extractive_statements(docs: list[Doc], n: int = EXTRACT_DOCS) -> list[Statement]:
    """liste answer without a model: the start of the best documents, quoted verbatim, each with its source."""
    out = []
    for d in _extract_docs(docs, n):
        aussage, z = _quoted(d)
        out.append(_source(d, z, "anderes", aussage, None))
    return out


def extractive_prosa(docs: list[Doc], n: int = EXTRACT_DOCS) -> ProsaWritten:
    """prosa answer without a model: one attributed sentence per best document ("Laut SRF: …")."""
    res = ProsaWritten()
    saetze = []
    for d in _extract_docs(docs, n):
        z = _excerpt(d.text, 600 if d.computed else 260)
        res.quellen.append(_source(d, z, "anderes", z, None))
        ton = "sicher" if d.computed else "vorsicht" if d.vorsicht else "eine_quelle"
        saetze.append(AnswerSentence(_worded(z, ton, d), [len(res.quellen)], None, ton))
    if saetze:
        res.antwort.append(AnswerBlock(typ="text", saetze=saetze))
    res.cited = list(res.quellen)
    return res


AGENT_MIN_USD = 0.01  # the agent only starts when the answer budget has at least this much left


def _budget_left() -> float:
    from klugscheisser.llm import budget as llm_budget

    b = llm_budget.current()
    return float("inf") if b is None else b.left()


def translate_quotes(s: Session, settings: Settings, statements: list[Statement]) -> None:
    """v8: quotes from documents that are neither German nor English get a German translation (cached)."""
    from klugscheisser import translate

    langs = {}
    for st in statements:
        item = s.get(m.Item, st.item_id) if st.item_id else None
        langs[id(st)] = (translate.item_lang(item) if item else None) or translate.lang_of_url(st.url)
    pairs = [(st.zitat, langs[id(st)]) for st in statements if translate.needs(langs[id(st)])]
    if not pairs:
        return
    try:
        translate.ensure(s, settings, pairs)
    except Exception as exc:  # an answer never fails because of a translation
        log.warning("quote translation failed: %s", exc)
    for st in statements:
        if translate.needs(langs[id(st)]):
            st.zitat_de = translate.cached(s, st.zitat)


def build_answer_engine(settings: Settings) -> GeminiAnswerEngine:
    return GeminiAnswerEngine(settings)


LlmAnswerEngine = (
    GeminiAnswerEngine  # v17: the writer is Claude Sonnet or Gemini (llm/text.py); old name kept
)


def answer_to_dict(result: AnswerResult) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(result)
