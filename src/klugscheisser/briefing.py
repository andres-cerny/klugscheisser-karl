"""v7: project briefing — Fabi's "system prompt" per project, plus a learned part she approves.

- `text`: what the project is for, which people/teams matter, what she wants, tone, what to avoid. She writes
  and edits it on the project page. Every save is a new version (traceable: answers store the version id).
- `learned`: "Was wir gelernt haben". Suggested from her questions and ratings (👎 reasons), never applied
  silently: a suggestion waits until she accepts, edits or rejects it.
- Used where a model writes for her: the answer writer (Claude Sonnet or Gemini; also cross-person answers and fun facts).
  Not used
  when documents are classified or ranked — a preference must never make us drop or hide data.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)
MAX_CHARS = 3000  # text + learned, cut for the prompt
SUGGEST_EVERY = timedelta(days=7)
SUGGEST_MIN_QUESTIONS = 5


def current(s: Session) -> m.ProjectBriefing | None:
    return s.scalar(select(m.ProjectBriefing).order_by(m.ProjectBriefing.id.desc()).limit(1))


def history(s: Session, limit: int = 8) -> list[m.ProjectBriefing]:
    return list(s.scalars(select(m.ProjectBriefing).order_by(m.ProjectBriefing.id.desc()).limit(limit)))


def save(s: Session, text: str | None = None, learned: str | None = None, note: str = "", by: str = "Fabi"):
    """New version; unchanged parts are carried over. Returns the new row (or the current one if nothing
    changed)."""
    cur = current(s)
    new_text = (text if text is not None else (cur.text if cur else "")).strip()
    new_learned = (learned if learned is not None else (cur.learned if cur else "")).strip()
    if cur is not None and cur.text == new_text and cur.learned == new_learned:
        return cur
    row = m.ProjectBriefing(text=new_text, learned=new_learned, note=note or None, created_by=by)
    s.add(row)
    s.flush()
    return row


def prompt_block(s: Session) -> tuple[str, int | None]:
    """The briefing as a block for model prompts, and its version id. Empty when there is none."""
    cur = current(s)
    if cur is None or not (cur.text or cur.learned):
        return "", None
    parts = []
    if cur.text:
        parts.append(cur.text)
    if cur.learned:
        parts.append(f"Was wir aus ihren Fragen und Bewertungen gelernt haben:\n{cur.learned}")
    body = "\n\n".join(parts)[:MAX_CHARS]
    return (
        "\n\nProjekt-Briefing der Moderatorin (beachten bei Auswahl, Gewichtung und Ton; die Regeln oben gehen "
        f"immer vor, das Briefing ist nie eine Quelle):\n{body}"
    ), cur.id


def current_block() -> tuple[str, int | None]:
    """prompt_block for the current project (own short session)."""
    from klugscheisser.db.session import session_scope

    try:
        with session_scope() as s:
            return prompt_block(s)
    except Exception:  # database without the v7 tables: no briefing
        log.warning("briefing unavailable", exc_info=True)
        return "", None


# --- learned part: suggestions from questions and ratings ----------------------------------------------


def open_suggestion(s: Session) -> m.BriefingSuggestion | None:
    return s.scalar(
        select(m.BriefingSuggestion)
        .where(m.BriefingSuggestion.status == "offen")
        .order_by(m.BriefingSuggestion.id.desc())
        .limit(1)
    )


def basis(s: Session, since: datetime) -> dict[str, Any]:
    """Her questions and ratings since `since` (what the suggestion is built from)."""
    questions = list(
        s.scalars(
            select(m.ChatMessage.text)
            .where(m.ChatMessage.role == "user", m.ChatMessage.created_at >= since)
            .order_by(m.ChatMessage.id.desc())
            .limit(200)
        )
    )
    rated = list(
        s.execute(
            select(m.ChatMessage.rating, m.ChatMessage.rating_reason)
            .where(m.ChatMessage.rated_at >= since, m.ChatMessage.rating.is_not(None))
            .limit(200)
        )
    )
    counts = Counter(r for r, _ in rated)
    reasons = [f"{r}: {why}" for r, why in rated if why]
    return {
        "seit": since.date().isoformat(),
        "fragen": questions,
        "bewertungen": dict(counts),
        "gruende": reasons[:60],
    }


SUGGEST_SYSTEM = (
    "Du hilfst, das Briefing einer Sportmoderatorin für ihre Recherche-App zu verbessern. Du bekommst ihre "
    "Fragen der letzten Zeit, ihre Bewertungen der Antworten (gut/okay/schlecht) mit Gründen und das aktuelle "
    "Briefing. Schreibe 2 bis 6 kurze Stichpunkte 'Was wir gelernt haben': welche Themen sie oft fragt, was "
    "sie an Antworten stört, was ihr gefällt. Nur was sich aus den Daten klar ergibt, keine Vermutungen über "
    "sie als Person, keine Namen von Personen ausser wenn sie oft gefragt werden. Deutsch, je Punkt ein Satz, "
    "beginnend mit '- '. Wiederhole nichts, was schon im Briefing steht."
)


def suggest(
    s: Session, settings, llm=None, now: datetime | None = None, days: int = 30
) -> m.BriefingSuggestion | None:
    """Build one suggestion (the text model, llm/text.py). None when there is too little to learn from."""
    now = now or datetime.now(UTC)
    data = basis(s, now - timedelta(days=days))
    if len(data["fragen"]) < SUGGEST_MIN_QUESTIONS and not data["gruende"]:
        return None
    cur = current(s)
    prompt = (
        f"Aktuelles Briefing:\n{(cur.text if cur else '') or '(leer)'}\n\n"
        f"Bisher gelernt:\n{(cur.learned if cur else '') or '(nichts)'}\n\n"
        f"Daten:\n{json.dumps(data, ensure_ascii=False)}"
    )
    text = (llm or _model_call(settings))(SUGGEST_SYSTEM, prompt).strip()
    if not text:
        return None
    row = m.BriefingSuggestion(
        text=text,
        basis={"seit": data["seit"], "fragen": len(data["fragen"]), "bewertungen": data["bewertungen"]},
    )
    s.add(row)
    s.flush()
    return row


def _model_call(settings):
    def call(system: str, prompt: str) -> str:
        from klugscheisser.llm import text as llm_text

        out, _usage = llm_text.generate(settings, system, prompt, role="answer", temperature=0.2)
        return out or ""

    return call


def due(s: Session, now: datetime | None = None) -> bool:
    """Weekly, and only when there are new questions since the last suggestion and none is waiting."""
    now = now or datetime.now(UTC)
    if open_suggestion(s) is not None:
        return False
    last = s.scalar(select(func.max(m.BriefingSuggestion.created_at)))
    if last is not None and now - last < SUGGEST_EVERY:
        return False
    since = last or (now - timedelta(days=30))
    n = s.scalar(
        select(func.count())
        .select_from(m.ChatMessage)
        .where(m.ChatMessage.role == "user", m.ChatMessage.created_at >= since)
    )
    return (n or 0) >= SUGGEST_MIN_QUESTIONS


def decide(s: Session, suggestion: m.BriefingSuggestion, action: str, edited: str | None = None) -> None:
    """accept (append to the learned part, possibly edited) or reject."""
    suggestion.decided_at = datetime.now(UTC)
    if action == "ablehnen":
        suggestion.status = "abgelehnt"
        return
    text = (edited if edited is not None else suggestion.text).strip()
    cur = current(s)
    learned = "\n".join(x for x in [(cur.learned if cur else "").strip(), text] if x)
    save(s, learned=learned, note="Gelerntes übernommen" + (" (bearbeitet)" if edited else ""))
    suggestion.status = "angenommen"
