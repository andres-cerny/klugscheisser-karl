"""v13: the text model (v17: Claude Sonnet, before: Gemini) reads the question before anything is answered (docs/build-plan-v13-interpreter.md).

It rewrites the question into one of the German templates below — names completed or corrected, pronouns
resolved from the chat, English translated, groups/leagues/seasons spelled the way our rules know them. Our
deterministic code then answers the template from stored data. Gemini only says what is *meant*; the answer
still comes from stored data or checked quotes. Any error → None → the original question goes on as before."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from klugscheisser.llm.words import fill
from klugscheisser.sports import current_spec

log = logging.getLogger(__name__)

ARTEN = [
    "fakt",
    "rangliste",
    "alter",
    "statistik",
    "form",
    "beziehung",
    "liga",
    "transfers",
    "turnier",
    "spiel",
    "trainer",
    "auszeichnungen",
    "dokumente",
    "unklar",
]

SYSTEM = (
    "Du liest Fragen an eine Recherche-App über {sport_topic} (vor allem Schweizer {players}) und schreibst "
    "sie in GENAU EINE der Vorlagen unten um, auf Deutsch. Du beantwortest die Frage NICHT.\n"
    "Regeln:\n"
    "1. Übersetze englische Fragen. Korrigiere Tippfehler in Namen nur, wenn eindeutig (z. B. 'Staldr' → "
    "'Stalder'); erfinde keine Namen.\n"
    "2. Ersetze {pronouns_q}, 'dort', 'in that game' durch die Person bzw. das Spiel aus dem "
    "bisherigen Gespräch.\n"
    "3. Übernimm Zahlen, Saisons, Jahre, Ligen und Turniere aus der Frage (Olympia 2026, WM, Euro Hockey Tour, "
    "SWHL, PWHL, SDHL, NCAA). 'diese Saison' bleibt 'diese Saison'. Füge NIE ein Jahr, eine Saison oder einen "
    "Zeitraum hinzu, der nicht in der Frage oder im Gespräch steht ('bei Olympia' bleibt ohne Jahr = alle "
    "Olympischen Spiele).\n"
    "4. Passt keine Vorlage (Neuigkeiten, Zitate, Aussagen, Verletzungen, Meinungen, Hintergründe), dann art "
    "'dokumente' und frage = die Frage auf Deutsch, Inhalt unverändert, Pronomen aufgelöst.\n"
    "5. Ist die Frage ohne Gespräch unverständlich (abgebrochen, ohne Subjekt), art 'unklar' und frage = die "
    "Originalfrage.\n"
    "Vorlagen (<…> ausfüllen):\n"
    "fakt: 'Wie alt ist <Person>?' · 'Wo ist <Person> geboren?' · 'Wie gross ist <Person>?' · 'Wie schwer ist "
    "<Person>?' · 'Welche Position spielt <Person>?' · 'Mit welcher Hand schiesst <Person>?' · 'Welche "
    "Rückennummer hat <Person>?' · 'Bei welchem Verein spielt <Person> aktuell?' · 'Bis wann läuft der Vertrag "
    "von <Person>?' · 'Juniorenklub von <Person>' · 'Wer hat <Person> gedraftet?' · 'Hat <Person> Geschwister im "
    "{sport_de}?' · 'Wo studiert <Person>?' · 'Welche Sprachen spricht <Person>?' · 'Wie viele "
    "Instagram-Follower hat <Person>?' · 'Wo hat <Person> überall gespielt?'\n"
    "rangliste: 'Wer ist {rank_who} im <Gruppe>?' · 'Wer hat am meisten "
    "Instagram-Follower im <Gruppe>?'\n"
    "alter: 'Wie viele {players} im <Gruppe> sind über|unter <Zahl>?'\n"
    "statistik: 'Wie viele Punkte|Tore|Assists|Spiele hat <Person> diese Saison?' · 'Wie viele "
    "Punkte|Tore|Assists|Spiele hat <Person> in der <Liga> gespielt?' (ganze Karriere) · 'Fangquote von <Person> "
    "bei Olympia [<Jahr>]' · 'Wer hat bei Olympia|der WM [<Jahr>] am meisten Punkte|Tore für die Schweiz?' · '{best_swiss} "
    "nach Punkten pro Spiel in der <Liga> diese Saison' · '<Person> vs <Person> an "
    "Weltmeisterschaften|an Olympia|in der <Liga>'\n"
    "form: 'Form von <Person> in den letzten 5 Spielen' (oder 10)\n"
    "auszeichnungen: 'Welche Auszeichnungen hat <Person> [in der Saison <Saison>] gewonnen?' (Preise, Titel, "
    "Medaillen, Erfolge)\n"
    "beziehung: 'Haben <Person> und <Person> schon im selben Club gespielt?' · {nation_pair} "
    "· 'Wer aus dem <Gruppe> hat zusammen bei <Team> gespielt?' · 'Im "
    "letzten Spiel <Team> gegen <Team>: wie viele {players} der beiden Teams haben schon im selben Club "
    "gespielt?'\n"
    "liga: 'Welche {swiss_players} spielen aktuell in der <Liga>?' · 'Welche {players} aus dem Schweizer "
    "Nationalteam spielen aktuell College-Hockey in den USA?' · 'Welche <Nation-Spieler> spielen aktuell bei Klubs "
    "in <Land>?' (Nation der Spieler und Land der Klubs getrennt lassen, z. B. 'Welche Deutschen spielen aktuell bei "
    "Klubs in der Schweiz?') · 'Welche <Nation-Spieler> spielen im Ausland?' · 'Welche <Nation-Spieler> haben schon "
    "bei Klubs in <Land> gespielt?'\n"
    "transfers: 'Wer hat im <Zeitraum> den Club gewechselt?' (z. B. Sommer 2026, in den letzten Wochen)\n"
    "turnier: 'Wann ist die nächste WM|Olympia|Euro Hockey Tour und wer ist im Kader?'\n"
    "spiel: 'Wie ist das letzte Spiel <Team> gegen <Team> ausgegangen?' · 'Wer hat für <Team> gegen <Team> an "
    "der <Turnier> getroffen?' · 'Wann spielt <Team> das nächste Mal?' · 'Wie ist das letzte Spiel von <Team> "
    "ausgegangen?' · 'Wie viele {players} im letzten Spiel <Team> gegen <Team> sind über <Zahl>?'\n"
    "trainer: 'Wer ist der Trainer von <Team oder Nation>?'\n"
    "[…] = nur wenn in der Frage genannt.\n"
    "Gruppen: 'Schweizer Olympia-Kader', 'Schweizer Nationalteam', 'finnischen Nationalteam' (usw.), "
    "'WM-Kader'. Teams als Name ('Frölunda HC', 'EV Zug') oder Land ('Schweiz', 'Finnland')."
)


def system() -> str:
    """SYSTEM with the current project's words (ice hockey: the text above as written before 2026-11-24)."""
    men = current_spec().men
    text = SYSTEM
    for key, value in {
        "{pronouns_q}": "'er', 'ihm', 'sein', 'he', 'him', 'his'" if men else "'sie', 'ihr', 'her', 'she'",
        "{rank_who}": "der älteste|jüngste|grösste|kleinste Spieler"
        if men
        else "die älteste|jüngste|grösste|kleinste Spielerin",
        "{best_swiss}": "Bester Schweizer" if men else "Beste Schweizerin",
        "{nation_pair}": "'Welche Spieler aus <Nation> haben mit Spielern aus <Nation> im selben Club gespielt?'"
        if men
        else "'Welche <Nation>innen haben mit <Nation>innen im selben Club gespielt?'",
    }.items():
        text = text.replace(key, value)
    return fill(text)


SCHEMA = {
    "type": "object",
    "properties": {
        "art": {"type": "string", "enum": ARTEN},
        "frage": {"type": "string"},
        "personen": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["art", "frage"],
}


@dataclass
class Interpretation:
    art: str
    frage: str  # the template question (German)
    personen: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    @property
    def rules_question(self) -> str | None:
        """The question our rules should read: the template — unless documents/unclear (then the original)."""
        return self.frage if self.art not in ("dokumente", "unklar") and self.frage else None


def _history(history: list[dict] | None) -> str:
    if not history:
        return ""
    lines = ["Bisheriges Gespräch (ältestes zuerst):"]
    for turn in history[-2:]:
        lines.append(f"- Frage: {turn.get('frage', '')}")
        for st in (turn.get("saetze") or [])[:3]:
            lines.append(f"  Antwort: {st}")
    return "\n".join(lines) + "\n"


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text or "").strip().lower()


def changed(original: str, interp: Interpretation | None) -> bool:
    """True when the template says more than a different spelling of the original (for "Gelesen als …")."""
    return bool(interp and interp.rules_question and _norm(interp.rules_question) != _norm(original))


def interpret(ask, question: str, history: list[dict] | None = None, today: date | None = None):
    """`ask(system, prompt, schema) -> (dict, usage)` (the engine's model call). Returns an Interpretation or
    None (any problem: the original question goes on unchanged)."""
    today = today or date.today()
    prompt = f"Heute ist der {today:%d.%m.%Y}.\n{_history(history)}Frage: {question}"
    try:
        out, usage = ask(system(), prompt, SCHEMA)
    except Exception as exc:  # never block an answer because the reading step failed
        log.warning("interpreter failed: %s", str(exc)[:200])
        return None
    art = str((out or {}).get("art") or "")
    frage = str((out or {}).get("frage") or "").strip()
    if art not in ARTEN or not frage or len(frage) > 400:
        return None
    personen = [str(p) for p in (out or {}).get("personen") or [] if str(p).strip()][:10]
    return Interpretation(art=art, frage=frage, personen=personen, usage=usage or {})
