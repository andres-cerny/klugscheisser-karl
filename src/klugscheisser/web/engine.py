"""Answer engine for the question box: the real one (P3; Claude Sonnet or Gemini writes) when available, else
a deterministic fake."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.interfaces import AnswerEngine, AnswerResult, Statement
from klugscheisser.mcp_server.present import datenstand, first_sentence, item_out
from klugscheisser.retrieval.search import Filters, athlete_team_filter, list_items

log = logging.getLogger(__name__)


class FakeAnswerEngine:
    """No LLM: quotes the first sentence of the 3 newest stored items about the person/team/event.

    Every statement is a verbatim quote, so the page shows the real line format with real links.
    """

    name = "fake"

    def __init__(self, settings: Settings | None = None, n: int = 3) -> None:
        self.settings = settings
        self.n = n

    def answer(
        self,
        question: str,
        person_ids: list[int] | None = None,
        team_id: int | None = None,
        event_id: int | None = None,
        since: datetime | None = None,
        web: bool = True,
        history: list[dict] | None = None,
        style: str | None = None,  # v12: ignored, the fake always answers as a list
    ) -> AnswerResult:
        with session_scope() as s:
            f = Filters(athlete_ids=person_ids or None, since=since)
            if team_id and not person_ids:
                team = s.get(m.Team, team_id)
                if team is not None:
                    f = athlete_team_filter(s, team)
                    f.since = since
            elif event_id and not person_ids:
                ids = list(
                    s.scalars(select(m.EventEntry.athlete_id).where(m.EventEntry.event_id == event_id))
                )
                f = Filters(athlete_ids=ids or None, since=since)
            items = [i for i in list_items(s, f, limit=30) if first_sentence(i.text)][: self.n]
            statements = []
            for i in items:
                out = item_out(i)
                sentence = first_sentence(i.text)
                statements.append(
                    Statement(
                        aussage=f"Laut Quelle: {sentence}",
                        zitat=sentence,
                        item_id=i.id,
                        url=i.url,
                        quelle=out["quelle"],
                        quelle_link=out["quelle_link"],
                        zeile_prefix=out["zeile_prefix"],
                        vorsicht=out["vorsicht"],
                    )
                )
            names = sorted({n for i in items for n in item_out(i)["athletinnen"]})
            luecken = [
                "Testmodus ohne KI: Die Frage wurde nicht inhaltlich beantwortet; gezeigt werden die neuesten "
                "belegten Einträge."
            ]
            if not statements:
                luecken.append("Keine Angabe in unseren Quellen.")
            return AnswerResult(
                frage=question,
                datenstand=datenstand(s, f.athlete_ids),
                statements=sorted(statements, key=lambda x: not x.vorsicht),
                luecken=luecken,
                personen=names,
                person_ids=list(f.athlete_ids or []),
            )


def get_answer_engine(settings: Settings) -> AnswerEngine:
    if settings.web_answer_engine == "fake":
        return FakeAnswerEngine(settings)
    try:
        from klugscheisser.answer.engine import build_answer_engine  # P3, built by the lead
    except ImportError:
        log.warning("answer engine not available yet; question box uses FakeAnswerEngine")
        return FakeAnswerEngine(settings)
    return build_answer_engine(settings)
