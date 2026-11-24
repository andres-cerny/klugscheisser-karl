"""v12 Part H: replay every chat question with the old (`liste`) and the new (`prosa`) answer style.

`klugscheisser chat-replay` reads all user questions from `chat_messages` of one or every project database, each
with its chat context (person / team / event page, the earlier turns for follow-ups), answers it again with every
style and writes a side-by-side HTML report: question · old answer · new answer · dropped / unsure · cost, with
Fabi's rating (gut / okay / schlecht + reason) next to the old answer.

Read-only for the chats: questions are read in a session that is rolled back, answers are never stored and the
answer cache is not used (`use_cache=False`). The engine itself still books its model costs in `cost_ledger`.
Without `--yes` the command only counts questions and estimates the cost (no model call); `--fake` uses a fake
engine (no model, no cost) for tests.

Also here: the v12 question set (`tests/fixtures/chat_questions.yaml`) with its expected answer shape, a loader
and a checker the writer / cross parts can run against fake or live results.
"""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.interfaces import AnswerBlock, AnswerResult, AnswerSentence

STYLES = ("liste", "prosa")
STYLE_LABEL = {"liste": "Alt (Liste)", "prosa": "Neu (Prosa)"}
# model calls per answer: one text-model call plus one batch of Jev support checks (translations come on top)
CALLS_PER_ANSWER = 2
FALLBACK_COST_USD = 0.005  # per answer when the project has no stored answer cost yet
PROSA_COST_FACTOR = 1.5  # prosa: same documents in, more text out, one Jev check per sentence
QUESTION_SET = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "chat_questions.yaml"
_BRIEF_RE = re.compile(r"\b(brief|briefing|vorbereitung auf das spiel|matchvorbereitung)\b", re.I)


# --- reading the questions (read-only) -------------------------------------------------------------------


@dataclass
class ReplayQuestion:
    project: str
    chat_id: int
    message_id: int
    question: str
    chat_title: str
    context: str  # name of the person / team / event the chat is about, or ""
    scope: dict[str, Any]  # person_ids / team_id / event_id as the web chat would pass them
    history: list[dict]  # earlier turns of the chat, oldest first (engine shape)
    asked_at: datetime | None = None
    stored: dict | None = None  # the answer stored back then (AnswerResult as JSON)
    rating: str | None = None  # Fabi's rating of that answer: gut | okay | schlecht
    rating_reason: str | None = None


def _skip(question: str) -> bool:
    """Chat messages the web chat does not send to the answer engine (add requests, Spiel-Brief links)."""
    from klugscheisser.adds.flow import parse_add_message

    return bool(parse_add_message(question)) or bool(_BRIEF_RE.search(question))


def collect_questions(
    s: Session, project: str = "", turns: int = 4, limit: int | None = None
) -> list[ReplayQuestion]:
    """Every user question of every chat, oldest first, with the history the web chat had at that moment.

    `limit` keeps the newest N. Nothing is written; the caller should roll the session back anyway.
    """
    from klugscheisser.chat import service as chat_svc
    from klugscheisser.web.views import chat_context

    out: list[ReplayQuestion] = []
    for chat in s.scalars(select(m.Chat).order_by(m.Chat.id)):
        msgs = sorted(chat.messages, key=lambda x: x.id)
        past: list[dict] = []
        for i, msg in enumerate(msgs):
            if msg.role != "user":
                continue
            reply = next((x for x in msgs[i + 1 :] if x.role == "assistant"), None)
            nxt_user = next((x for x in msgs[i + 1 :] if x.role == "user"), None)
            if reply is not None and nxt_user is not None and nxt_user.id < reply.id:
                reply = None  # the answer belongs to a later question
            hist = past[-turns:] if turns > 0 else []
            if not _skip(msg.text):
                out.append(
                    ReplayQuestion(
                        project=project,
                        chat_id=chat.id,
                        message_id=msg.id,
                        question=msg.text,
                        chat_title=chat.title,
                        context=chat_context(chat),
                        scope=chat_svc.scope_for(s, chat, msg.text, hist),
                        history=[dict(h) for h in hist],
                        asked_at=msg.created_at,
                        stored=reply.answer if reply is not None else None,
                        rating=reply.rating if reply is not None else None,
                        rating_reason=reply.rating_reason if reply is not None else None,
                    )
                )
            if reply is not None and reply.answer is not None:  # same shape as chat_svc.history
                past.append(
                    {
                        "frage": msg.text,
                        "antwort": reply.text,
                        "personen": reply.answer.get("personen") or [],
                        "person_ids": reply.answer.get("person_ids") or [],
                    }
                )
    out.sort(key=lambda q: q.message_id)
    return out[-limit:] if limit else out


def average_answer_cost(s: Session) -> float:
    """Mean cost of the answers stored in this project's chats (0 = none yet)."""
    costs = [
        float((a or {}).get("kosten_usd") or 0)
        for a in s.scalars(select(m.ChatMessage.answer).where(m.ChatMessage.role == "assistant"))
    ]
    costs = [c for c in costs if c > 0]
    return sum(costs) / len(costs) if costs else 0.0


@dataclass
class Estimate:
    questions: int
    styles: tuple[str, ...]
    usd: float

    @property
    def answers(self) -> int:
        return self.questions * len(self.styles)

    @property
    def calls(self) -> int:
        return self.answers * CALLS_PER_ANSWER

    def text(self) -> str:
        return (
            f"{self.questions} Fragen × {len(self.styles)} Stile ({', '.join(self.styles)}) = {self.answers} "
            f"Antworten, je etwa {CALLS_PER_ANSWER} Modellaufrufe (Text-Modell + Jev) → ca. {self.calls} Aufrufe, "
            f"geschätzt ${self.usd:.2f}"
        )


def estimate(questions: int, styles: tuple[str, ...], avg_cost: float) -> Estimate:
    per = avg_cost or FALLBACK_COST_USD
    usd = sum(questions * per * (PROSA_COST_FACTOR if st == "prosa" else 1.0) for st in styles)
    return Estimate(questions, styles, round(usd, 4))


# --- answering --------------------------------------------------------------------------------------------


@dataclass
class ReplayAnswer:
    style: str
    result: AnswerResult | None = None
    error: str | None = None
    seconds: float = 0.0

    @property
    def dropped(self) -> int:
        return self.result.verworfen if self.result else 0

    @property
    def unsure(self) -> int:
        r = self.result
        return len(r.unbestaetigt) + len(getattr(r, "nicht_sicher", None) or []) if r else 0

    @property
    def cost(self) -> float:
        return float(self.result.kosten_usd or 0) if self.result else 0.0


@dataclass
class ReplayRow:
    q: ReplayQuestion
    answers: dict[str, ReplayAnswer] = field(default_factory=dict)
    html: dict[str, str] = field(default_factory=dict)  # rendered per style (in the project's context)
    stored_html: str = ""


def _accepts(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def answer_once(engine: Any, q: ReplayQuestion, style: str) -> ReplayAnswer:
    """One answer, never from the cache. An engine without `style=` (before the writer part) answers `liste`
    only; asking it for `prosa` is reported, not faked."""
    kwargs: dict[str, Any] = {
        "person_ids": q.scope.get("person_ids") or None,
        "team_id": q.scope.get("team_id"),
        "event_id": q.scope.get("event_id"),
        "history": q.history or None,
    }
    if _accepts(engine.answer, "use_cache"):
        kwargs["use_cache"] = False
    if _accepts(engine.answer, "style"):
        kwargs["style"] = style
    elif style != "liste":
        return ReplayAnswer(style, error="Die Antwort-Engine kennt diesen Stil noch nicht (style=).")
    started = time.monotonic()
    try:
        res = engine.answer(q.question, **kwargs)
    except Exception as exc:  # one failing question must not stop the replay
        return ReplayAnswer(style, error=f"{type(exc).__name__}: {exc}", seconds=time.monotonic() - started)
    return ReplayAnswer(style, result=res, seconds=round(time.monotonic() - started, 1))


class ReplayFakeEngine:
    """`--fake` and tests: the web app's fake engine (no model, no cost), with `style=`. For `prosa` the quotes
    become one paragraph with numbered sources, so the report shows the new shape."""

    name = "fake"

    def __init__(self, settings: Any = None) -> None:
        from klugscheisser.web.engine import FakeAnswerEngine

        self._fake = FakeAnswerEngine(settings)
        self.calls: list[dict] = []

    def answer(self, question: str, style: str = "liste", use_cache: bool = True, **kw: Any) -> AnswerResult:
        self.calls.append({"frage": question, "style": style, "use_cache": use_cache, **kw})
        kw.pop("web", None)
        res = self._fake.answer(question, **kw)
        res.stil = style
        if style == "prosa":
            res.quellen = list(res.statements)
            res.antwort = [
                AnswerBlock(
                    typ="text",
                    saetze=[
                        AnswerSentence(text=st.aussage, quellen=[i + 1])
                        for i, st in enumerate(res.statements)
                    ],
                )
            ]
        return res


def replay_project(
    project: str,
    engine: Any,
    styles: tuple[str, ...] = STYLES,
    limit: int | None = None,
    echo: Any = None,
) -> list[ReplayRow]:
    """Replay the questions of one project (switches to its database for the run)."""
    from klugscheisser import projects
    from klugscheisser.config import get_settings
    from klugscheisser.db.session import get_engine

    rows: list[ReplayRow] = []
    with projects.use(project):
        with Session(get_engine()) as s:
            questions = collect_questions(s, project, get_settings().chat_history_turns, limit)
            s.rollback()
        for n, q in enumerate(questions, 1):
            row = ReplayRow(q)
            for st in styles:
                row.answers[st] = answer_once(engine, q, st)
                row.html[st] = _answer_box(row.answers[st])
            row.stored_html = _stored_box(q.stored)
            rows.append(row)
            if echo:
                echo(f"[{project}] {n}/{len(questions)} {q.question[:70]}")
    return rows


def ledger_since(project: str, since: datetime) -> float:
    """Model costs booked in the project's ledger since `since` (what the replay really cost)."""
    from klugscheisser import projects
    from klugscheisser.db.session import get_engine

    with projects.use(project), Session(get_engine()) as s:
        return float(
            s.scalar(
                select(func.coalesce(func.sum(m.CostLedger.cost_usd), 0.0)).where(
                    m.CostLedger.created_at >= since
                )
            )
            or 0.0
        )


# --- report -----------------------------------------------------------------------------------------------


def _answer_box(a: ReplayAnswer) -> str:
    from klugscheisser.web.html import e
    from klugscheisser.web.views import answer_html

    if a.error:
        return f"<p class='err'>{e(a.error)}</p>"
    try:
        return answer_html(a.result)
    except Exception as exc:  # the renderer is the writer part's; a bad result must not break the report
        return f"<p class='err'>Darstellung fehlgeschlagen: {e(exc)}</p>"


def _stored_box(stored: dict | None) -> str:
    if not stored:
        return ""
    from klugscheisser.chat.service import result_from_json
    from klugscheisser.web.html import e
    from klugscheisser.web.views import answer_html

    try:
        return answer_html(result_from_json(stored))
    except Exception as exc:
        return f"<p class='small'>Gespeicherte Antwort nicht darstellbar: {e(exc)}</p>"


REPORT_CSS = """
.rp-q{border-top:2px solid var(--ink);margin:28px 0 0;padding-top:10px}
.rp-q h3{font:600 18px/1.3 'Barlow',sans-serif;margin:4px 0}
.rp-cols{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:10px}
.rp-col{border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-width:0}
.rp-col h4{font:700 13px/1.2 'Barlow Condensed',sans-serif;letter-spacing:.08em;text-transform:uppercase;margin:0 0 6px}
.rp-stats{font-size:13px;color:var(--grey);margin:0 0 8px}
.rate{display:inline-block;border-radius:12px;padding:2px 9px;font:600 13px/1.4 'Barlow',sans-serif;background:var(--chip)}
.rate-gut{background:#DDF2E4;color:#1B6B3A}.rate-schlecht{background:#FBE1DF;color:#B3261E}
.rate-okay{background:var(--warn-bg);color:var(--warn-fg)}
.rp-sum td,.rp-sum th{padding:4px 10px;text-align:left;border-bottom:1px solid var(--line)}
"""


def _rating_html(q: ReplayQuestion) -> str:
    from klugscheisser.web.html import e

    if not q.rating:
        return "<p class='small'>Fabis Bewertung: keine</p>"
    reason = f" · «{e(q.rating_reason)}»" if q.rating_reason else ""
    return f"<p class='small'>Fabis Bewertung: <span class='rate rate-{e(q.rating)}'>{e(q.rating)}</span>{reason}</p>"


def _stats(a: ReplayAnswer) -> str:
    if a.result is None:
        return ""
    r = a.result
    parts = [
        f"verworfen {a.dropped}",
        f"nicht sicher {a.unsure}",
        f"Kosten ${a.cost:.4f}",
        f"{a.seconds:.1f} s",
    ]
    if getattr(r, "rueckfrage", None):
        parts.insert(0, "Rückfrage")
    return " · ".join(parts)


def report_html(
    rows: list[ReplayRow], styles: tuple[str, ...], ledger_usd: float | None = None, note: str = ""
) -> str:
    from klugscheisser.web.html import CSS, e

    summary = []
    for st in styles:
        ans = [r.answers[st] for r in rows if st in r.answers]
        ok = [a for a in ans if a.result is not None]
        summary.append(
            f"<tr><th>{e(STYLE_LABEL.get(st, st))}</th><td>{len(ok)}/{len(ans)} beantwortet</td>"
            f"<td>verworfen {sum(a.dropped for a in ok)}</td><td>nicht sicher {sum(a.unsure for a in ok)}</td>"
            f"<td>Rückfragen {sum(1 for a in ok if getattr(a.result, 'rueckfrage', None))}</td>"
            f"<td>Kosten ${sum(a.cost for a in ok):.4f}</td></tr>"
        )
    ratings = {k: sum(1 for r in rows if r.q.rating == k) for k in ("gut", "okay", "schlecht")}
    head = [
        "<h1>Chat-Replay: alte und neue Antworten</h1>",
        f"<p class='meta'>{len(rows)} Fragen · erstellt {datetime.now(UTC).astimezone().strftime('%d.%m.%Y %H:%M')}"
        + (f" · {e(note)}" if note else "")
        + "</p>",
        "<table class='rp-sum'>" + "".join(summary) + "</table>",
        f"<p class='small'>Fabis Bewertungen der alten Antworten: gut {ratings['gut']} · okay {ratings['okay']} · "
        f"schlecht {ratings['schlecht']}"
        + (f" · Kosten laut Kostenbuch seit Start: ${ledger_usd:.4f}" if ledger_usd is not None else "")
        + "</p>",
    ]
    body = []
    for r in rows:
        q = r.q
        where = " · ".join(x for x in (q.project, q.context, f"Chat {q.chat_id}") if x)
        hist = ""
        if q.history:
            hist = (
                f"<details><summary class='small'>Folgefrage: {len(q.history)} frühere Fragen im Chat</summary>"
                "<ul class='lines'>"
                + "".join(f"<li><strong>{e(h['frage'])}</strong> → {e(h['antwort'])}</li>" for h in q.history)
                + "</ul></details>"
            )
        cols = []
        for st in styles:
            a = r.answers.get(st)
            if a is None:
                continue
            extra = ""
            if st == styles[0]:
                extra = _rating_html(q)
                if r.stored_html:
                    extra += f"<details><summary class='small'>Damals gespeicherte Antwort</summary>{r.stored_html}</details>"
            cols.append(
                f"<div class='rp-col'><h4>{e(STYLE_LABEL.get(st, st))}</h4>{extra}"
                f"<p class='rp-stats'>{e(_stats(a))}</p>{r.html.get(st, '')}</div>"
            )
        body.append(
            f"<section class='rp-q'><div class='small'>{e(where)}</div><h3>{e(q.question)}</h3>{hist}"
            f"<div class='rp-cols'>{''.join(cols)}</div></section>"
        )
    if not rows:
        body.append("<p>Keine Fragen gefunden.</p>")
    return (
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Chat-Replay</title>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700"
        "&family=Barlow+Condensed:wght@600;700&display=swap'>"
        f"<style>{CSS}{REPORT_CSS}</style></head><body><main>{''.join(head)}{''.join(body)}</main></body></html>"
    )


# --- v12 question set (expected answer shapes) --------------------------------------------------------------


@dataclass
class ExpectedQuestion:
    """One question of `tests/fixtures/chat_questions.yaml` and the shape its answer must have."""

    id: str
    frage: str
    kanal: str = "web"  # web (prosa) | mcp (line format, unchanged)
    stil: str = "prosa"
    kontext: dict = field(default_factory=dict)  # {"person": name} | {"team": name} | {"event": key}
    folgefrage_von: str | None = None  # id of the question asked before in the same chat
    sprache: str = "de"
    tabelle: bool | None = None  # True: a table is expected; False: none
    rueckfrage: str = "nein"  # nein | erlaubt | erwartet
    quellen_pflicht: bool = True  # every sentence / table row cites a source
    berechnet: bool = False  # a computed source document ("Berechnet aus gespeicherten Daten") is expected
    nur_eigene_quellen: bool = False  # sensitive topic: only the person's own posts / interviews
    nichts_gefunden: bool = False  # "Dazu habe ich nichts gefunden" + closest sources
    enthaelt: list[str] = field(default_factory=list)  # words the answer text should contain (live run)
    max_saetze: int | None = None
    notiz: str = ""


def load_question_set(path: Path | str | None = None) -> list[ExpectedQuestion]:
    raw = yaml.safe_load(Path(path or QUESTION_SET).read_text(encoding="utf-8")) or {}
    items = [ExpectedQuestion(**{k: v for k, v in x.items()}) for x in raw.get("fragen") or []]
    seen: set[str] = set()
    for x in items:
        if x.id in seen:
            raise ValueError(f"{x.id}: doppelte id")
        if x.folgefrage_von and x.folgefrage_von not in seen:
            raise ValueError(f"{x.id}: folgefrage_von {x.folgefrage_von!r} muss vorher stehen")
        seen.add(x.id)
    return items


def answer_text(res: AnswerResult) -> str:
    """All written text of a prosa answer (sentences and table cells); the statements for list answers."""
    parts: list[str] = []
    for b in res.antwort or []:
        b = b if isinstance(b, AnswerBlock) else AnswerBlock(**b)
        parts += [(x.text if isinstance(x, AnswerSentence) else x.get("text", "")) for x in b.saetze]
        parts += [" | ".join(z.get("zellen") or []) for z in b.zeilen]
    if not parts:
        parts = [st.aussage for st in res.statements]
    if res.rueckfrage:
        parts.append(res.rueckfrage)
    return " ".join(p for p in parts if p)


_DE = re.compile(
    r"\b(der|die|das|und|ist|nicht|mit|von|für|eine?|sie|hat|wurde|im|auf|zu|dazu|keine)\b", re.I
)
_EN = re.compile(r"\b(the|and|is|was|with|of|for|she|has|her|who|not|in the|from|born)\b", re.I)
_OWN = re.compile(r"instagram|facebook|\bx @|tiktok|interview|eigene|podcast", re.I)
NOTHING_FOUND = ("nichts gefunden", "keine belegte angabe", "nicht belegt", "keine angabe")


def _sentences(res: AnswerResult) -> list[tuple[AnswerSentence | dict, bool]]:
    out: list[tuple[Any, bool]] = []
    for b in res.antwort or []:
        b = b if isinstance(b, AnswerBlock) else AnswerBlock(**b)
        out += [(x, False) for x in b.saetze]
        out += [(z.get("satz") or {}, True) for z in b.zeilen]
    return out


def _quellen(x: AnswerSentence | dict) -> list[int]:
    return list(x.quellen if isinstance(x, AnswerSentence) else (x.get("quellen") or []))


def check_result(item: ExpectedQuestion, res: AnswerResult, live: bool = False) -> list[str]:
    """Problems of one answer against its expected shape (empty list = fine). `live`: also check the
    expected words (`enthaelt`), which a fake engine cannot produce."""
    probs: list[str] = []
    text = answer_text(res)
    low = text.casefold()
    if item.kanal == "mcp":
        if res.stil != "liste" or res.antwort:
            probs.append("MCP muss das Zeilenformat behalten (stil liste, keine antwort-Blöcke)")
        if not res.statements and not res.luecken:
            probs.append("MCP: weder Aussagen noch Lücken")
        return probs
    if res.stil != item.stil:
        probs.append(f"stil {res.stil!r} statt {item.stil!r}")
    asked_back = bool(res.rueckfrage)
    if item.rueckfrage == "erwartet" and not asked_back:
        probs.append("Rückfrage erwartet")
    if item.rueckfrage == "nein" and asked_back:
        probs.append("unerwartete Rückfrage")
    if asked_back:
        return probs  # a question back has no answer blocks to check
    nothing = any(w in low for w in NOTHING_FOUND) or not text.strip()
    if item.nichts_gefunden:
        if not nothing:
            probs.append("erwartet «Dazu habe ich nichts gefunden»")
        if not res.naechste_quellen:
            probs.append("erwartet die nächsten Quellen (naechste_quellen)")
    elif not item.nur_eigene_quellen and nothing:
        probs.append("keine Antwort")
    if item.stil == "prosa" and not item.nichts_gefunden and not nothing and not res.antwort:
        probs.append("keine antwort-Blöcke")
    has_table = any((b.typ if isinstance(b, AnswerBlock) else b.get("typ")) == "tabelle" for b in res.antwort)
    if item.tabelle is True and not has_table:
        probs.append("Tabelle erwartet")
    if item.tabelle is False and has_table:
        probs.append("keine Tabelle erwartet")
    n_src = len(res.quellen)
    for x, is_row in _sentences(res):
        nums = _quellen(x)
        label = "Tabellenzeile" if is_row else "Satz"
        if item.quellen_pflicht and not nums:
            probs.append(
                f"{label} ohne Quelle: {(x.text if isinstance(x, AnswerSentence) else x.get('text'))!r}"
            )
        if any(not 1 <= k <= n_src for k in nums):
            probs.append(f"{label} verweist auf eine Quelle, die es nicht gibt: {nums}")
    if item.berechnet and not any("berechnet" in (q.quelle or "").casefold() for q in res.quellen):
        probs.append("berechnete Quelle erwartet («Berechnet aus gespeicherten Daten»)")
    if item.nur_eigene_quellen and not nothing:
        foreign = [q.quelle for q in res.quellen if not _OWN.search(q.quelle or "")]
        if foreign:
            probs.append(f"heikles Thema aus fremden Quellen: {foreign[:3]}")
    if item.sprache == "de" and text.strip() and len(_EN.findall(text)) > len(_DE.findall(text)):
        probs.append("Antwort nicht auf Deutsch")
    if item.max_saetze is not None and len(_sentences(res)) > item.max_saetze:
        probs.append(f"mehr als {item.max_saetze} Sätze")
    if live:
        probs += [f"«{w}» fehlt in der Antwort" for w in item.enthaelt if w.casefold() not in low]
    return probs


def resolve_context(s: Session, item: ExpectedQuestion) -> dict[str, Any]:
    """The question's page context as engine arguments in the current database ({} when not found)."""
    from klugscheisser.db import repo

    k = item.kontext or {}
    if name := k.get("person"):
        a = repo.resolve_athlete(s, name)
        return {"person_ids": [a.id]} if a else {}
    if name := k.get("team"):
        t = repo.find_team(s, name)
        return {"team_id": t.id} if t else {}
    if key := k.get("event"):
        ev = s.scalar(select(m.Event).where(m.Event.key == key))
        return {"event_id": ev.id} if ev else {}
    return {}


def run_question_set(
    engine: Any, items: list[ExpectedQuestion] | None = None, live: bool = False
) -> list[tuple[ExpectedQuestion, AnswerResult | None, list[str]]]:
    """Ask every question of the set (current project database) with its expected style; follow-ups get the
    earlier question's answer as history. Returns (item, result, problems)."""
    from klugscheisser.chat.service import answer_summary
    from klugscheisser.db.session import get_engine

    items = items if items is not None else load_question_set()
    done: dict[str, tuple[ExpectedQuestion, AnswerResult]] = {}
    out = []
    for item in items:
        with Session(get_engine()) as s:
            scope = resolve_context(s, item)
        history = None
        if item.folgefrage_von and item.folgefrage_von in done:
            prev_item, prev = done[item.folgefrage_von]
            history = [
                {
                    "frage": prev_item.frage,
                    "antwort": answer_summary(prev),
                    "personen": prev.personen,
                    "person_ids": prev.person_ids,
                }
            ]
            if not scope and prev.person_ids:
                scope = {"person_ids": prev.person_ids}
        q = ReplayQuestion("", 0, 0, item.frage, "", "", scope, history or [])
        a = answer_once(engine, q, item.stil)
        if a.result is None:
            out.append((item, None, [a.error or "keine Antwort"]))
            continue
        done[item.id] = (item, a.result)
        out.append((item, a.result, check_result(item, a.result, live=live)))
    return out
