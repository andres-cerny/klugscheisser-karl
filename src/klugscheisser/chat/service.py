"""v4: server-side chats with follow-ups, and the recent-searches log. Shared across devices (one user)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.interfaces import AnswerBlock, AnswerResult, AnswerSentence, Statement

ANSWER_STYLES = ("liste", "prosa")


def create_chat(
    s: Session,
    title: str,
    athlete_id: int | None = None,
    team_id: int | None = None,
    event_id: int | None = None,
) -> m.Chat:
    chat = m.Chat(title=title_of(title), athlete_id=athlete_id, team_id=team_id, event_id=event_id)
    s.add(chat)
    s.flush()
    return chat


def title_of(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def answer_style(s: Session, settings) -> str:
    """v12: web chat answer style. Project setting `web_answer_style` (project database) wins over
    `settings.web_answer_style` (env / projects.yaml); anything unknown → "liste". MCP always uses "liste"."""
    row = s.get(m.ProjectSetting, "web_answer_style")
    for value in (row.value if row is not None else None, getattr(settings, "web_answer_style", None)):
        if value in ANSWER_STYLES:
            return value
    return "liste"


def answer_sentences(data: dict[str, Any], limit: int = 10) -> list[str]:
    """v12: the sentences of a stored prosa answer (table rows as "Spalte: Zelle, …"), for follow-ups."""
    out: list[str] = []
    for block in data.get("antwort") or []:
        if block.get("typ") == "tabelle":
            cols = list(block.get("spalten") or [])
            for row in block.get("zeilen") or []:
                cells = row.get("zellen") or []
                cols_ = cols + [""] * len(cells)
                out.append(
                    ", ".join(f"{c}: {v}" if c else v for c, v in zip(cols_, cells, strict=False) if v)
                )
        else:
            out.extend(x.get("text", "") for x in block.get("saetze") or [])
    return [x for x in out if x][:limit]


def answer_summary(res: AnswerResult, limit: int = 6) -> str:
    """Short plain text of an answer, used as context for follow-ups (never as a source)."""
    if getattr(res, "stil", "liste") == "prosa":
        parts = answer_sentences(asdict(res), limit) or ([res.rueckfrage] if res.rueckfrage else [])
        return " ".join(parts) or "Dazu habe ich nichts gefunden."
    parts = [st.aussage for st in res.statements[:limit]]
    if not parts:
        parts.append("Keine Angabe in unseren Quellen.")
    return " ".join(parts)


def add_question(s: Session, chat: m.Chat, question: str) -> m.ChatMessage:
    msg = m.ChatMessage(chat_id=chat.id, role="user", text=question)
    s.add(msg)
    chat.updated_at = func.now()
    s.flush()
    return msg


def add_answer(s: Session, chat: m.Chat, res: AnswerResult) -> m.ChatMessage:
    msg = m.ChatMessage(
        chat_id=chat.id,
        role="assistant",
        text=answer_summary(res),
        answer=asdict(res),
        briefing_id=getattr(res, "briefing_id", None),
    )
    s.add(msg)
    chat.updated_at = func.now()
    s.flush()
    return msg


def add_note(s: Session, chat: m.Chat, text: str, add_request_id: int | None = None) -> m.ChatMessage:
    """Assistant message that is not an answer, e.g. an add-person proposal."""
    msg = m.ChatMessage(chat_id=chat.id, role="assistant", text=text, add_request_id=add_request_id)
    s.add(msg)
    chat.updated_at = func.now()
    s.flush()
    return msg


def result_from_json(data: dict[str, Any]) -> AnswerResult:
    def stmts(key: str) -> list[Statement]:
        return [Statement(**x) for x in data.get(key) or []]

    def sentence(x: dict) -> AnswerSentence:
        return AnswerSentence(**x)

    def block(x: dict) -> AnswerBlock:
        return AnswerBlock(
            typ=x.get("typ", "text"),
            saetze=[sentence(y) for y in x.get("saetze") or []],
            spalten=list(x.get("spalten") or []),
            zeilen=[
                {"zellen": list(z.get("zellen") or []), "satz": sentence(z["satz"])}
                for z in x.get("zeilen") or []
            ],
        )

    nested = ("statements", "unbestaetigt", "quellen", "antwort", "nicht_sicher")
    rest = {k: v for k, v in data.items() if k not in nested}
    return AnswerResult(
        **rest,
        statements=stmts("statements"),
        unbestaetigt=stmts("unbestaetigt"),
        quellen=stmts("quellen"),
        antwort=[block(x) for x in data.get("antwort") or []],
        nicht_sicher=[sentence(x) for x in data.get("nicht_sicher") or []],
    )


def history(chat: m.Chat, turns: int) -> list[dict]:
    """Last `turns` question/answer pairs, oldest first, in the shape the answer engine expects."""
    out: list[dict] = []
    question: str | None = None
    for msg in chat.messages:
        if msg.role == "user":
            question = msg.text
        elif question is not None and msg.answer is not None:
            out.append(
                {
                    "frage": question,
                    "antwort": msg.text,
                    "personen": msg.answer.get("personen") or [],
                    "person_ids": msg.answer.get("person_ids") or [],
                    "saetze": answer_sentences(msg.answer),  # v12: the previous prosa answer's sentences
                }
            )
            question = None
    return out[-turns:] if turns > 0 else []


def scope_for(s: Session, chat: m.Chat, question: str, past: list[dict]) -> dict[str, Any]:
    """Who a chat question is about.

    A person named in the question wins ("und Lara Stalder?"); otherwise the follow-up keeps the previous
    turn's persons ("was hat sie dazu gesagt?"); otherwise the chat's own context (person/team/event page).
    """
    from klugscheisser.domain.matcher import AthleteMatcher

    named = list(dict.fromkeys(link.athlete_id for link in AthleteMatcher(s).from_text(question)))
    if named:
        return {"person_ids": named}
    if past and past[-1].get("person_ids"):
        return {"person_ids": past[-1]["person_ids"]}
    if chat.athlete_id:
        return {"person_ids": [chat.athlete_id]}
    return {"team_id": chat.team_id, "event_id": chat.event_id}


def recent_chats(
    s: Session,
    limit: int = 8,
    athlete_id: int | None = None,
    team_id: int | None = None,
    event_id: int | None = None,
) -> list[m.Chat]:
    q = select(m.Chat).order_by(m.Chat.updated_at.desc(), m.Chat.id.desc()).limit(limit)
    if athlete_id is not None:
        q = q.where(m.Chat.athlete_id == athlete_id)
    if team_id is not None:  # v9: the team / event page shows its latest chat next to the roster
        q = q.where(m.Chat.team_id == team_id)
    if event_id is not None:
        q = q.where(m.Chat.event_id == event_id)
    return list(s.scalars(q))


def log_search(s: Session, query: str, result_ref: str | None = None, label: str | None = None) -> None:
    query = " ".join((query or "").split())[:300]
    if not query:
        return
    s.add(m.SearchLog(query=query, result_ref=result_ref, result_label=label))
    s.flush()


def recent_searches(s: Session, limit: int = 8) -> list[m.SearchLog]:
    """Newest first, one row per query (the newest one, preferring rows that opened a result)."""
    rows = s.scalars(
        select(m.SearchLog).order_by(m.SearchLog.created_at.desc(), m.SearchLog.id.desc()).limit(200)
    )
    seen: dict[str, m.SearchLog] = {}
    for row in rows:
        key = (row.result_ref or row.query.casefold()).casefold()
        if key not in seen:
            seen[key] = row
        if len(seen) >= limit:
            break
    return list(seen.values())
