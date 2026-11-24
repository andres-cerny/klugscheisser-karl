"""Spending limits for one answer (frage / web chat): a per-question budget and the daily cap.

Every paid call made while an answer is being written goes through here: the text model (llm/text.py, also
translations and the agent planner) and Jev (classify/jev.py). Each call first asks `before_call()` and books its
cost with `charge_llm()` / `charge_jev()` afterwards. The budget is a context variable, so the calls inside an
answer (also in the thread pools, see projects.ContextPool) all see the same one; outside an answer there is none
and nothing is checked here (nightly jobs, fact extraction … have their own caps).

- `ANSWER_MAX_USD` (settings.answer_max_usd): what one answer may cost. Once spent, further paid calls are refused
  (`AnswerBudgetExceeded`); callers degrade instead of failing: fewer persons, Jev → word match, no repair/agent.
  Callers that can choose (group answers, agent) look at `affordable()` / `left()` before a call.
- `LLM_DAILY_COST_CAP_USD`: answer spend of the last 24 h (cost_ledger: the answer model source + Jev). When it is
  used up, no paid call is made at all: the answer comes from stored data without a model, with a short note.

Cost ledger: `finish()` books whatever the call sites did not book themselves (Jev reranking and support checks,
for example), so every paid call of an answer ends up in cost_ledger exactly once.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

JEV_USD_PER_M = 0.042  # TypeSafe Jev, per 1M input tokens (same rate everywhere in the app)
CHARS_PER_TOKEN = 3.5  # rough, for estimates before a call
WRITE_OUTPUT_TOKENS = 1500  # typical writer answer (statements + quotes), for estimates

DAILY_NOTE = (
    "Tageslimit für KI-Antworten erreicht: diese Antwort kommt nur aus gespeicherten Daten, ohne KI-Text "
    "(wieder normal, sobald die Ausgaben der letzten 24 Stunden unter dem Limit sind)."
)
QUESTION_NOTE = "Kostenlimit pro Frage erreicht"


class AnswerBudgetExceeded(Exception):
    """A paid call was refused: the per-question budget or the daily cap is used up."""


@dataclass
class AnswerBudget:
    max_usd: float
    daily_cap_usd: float | None = None
    daily_spent_before: float = 0.0  # cost_ledger answer spend of the last 24 h when the answer started
    llm_usd: float = 0.0
    jev_usd: float = 0.0
    jev_tokens: int = 0
    calls: int = 0
    refused: int = 0
    booked: dict = field(
        default_factory=lambda: {"llm": 0.0, "jev": 0.0}
    )  # what call sites put in cost_ledger
    notes: list[str] = field(default_factory=list)  # what was left out, for the answer's gaps
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def spent(self) -> float:
        return self.llm_usd + self.jev_usd

    @property
    def daily_exhausted(self) -> bool:
        return (
            self.daily_cap_usd is not None
            and self.daily_spent_before + self.spent >= self.daily_cap_usd - 1e-9
        )

    def left(self) -> float:
        """USD this answer may still spend (per-question budget and daily cap)."""
        left = self.max_usd - self.spent
        if self.daily_cap_usd is not None:
            left = min(left, self.daily_cap_usd - self.daily_spent_before - self.spent)
        return max(0.0, left)

    def affordable(self, estimate_usd: float) -> bool:
        return estimate_usd <= self.left()

    def before_call(self, what: str = "llm") -> None:
        """Raise when nothing may be spent any more."""
        if self.daily_exhausted:
            self._refuse(what, "daily")
            raise AnswerBudgetExceeded(f"{what}: daily cap ${self.daily_cap_usd:.2f} used up")
        if self.spent >= self.max_usd - 1e-9:
            self._refuse(what, "question")
            raise AnswerBudgetExceeded(f"{what}: answer budget ${self.max_usd:.3f} used up")

    def _refuse(self, what: str, why: str) -> None:
        with self._lock:
            self.refused += 1
        log.info("answer budget: %s refused (%s), spent $%.4f", what, why, self.spent)

    def charge_llm(self, usd: float) -> None:
        with self._lock:
            self.llm_usd += float(usd or 0.0)
            self.calls += 1

    def charge_jev(self, tokens: int) -> None:
        with self._lock:
            self.jev_tokens += int(tokens or 0)
            self.jev_usd += int(tokens or 0) / 1e6 * JEV_USD_PER_M
            self.calls += 1

    def booked_in_ledger(self, source: str, usd: float) -> None:
        kind = "jev" if source == "jev" else "llm" if source.startswith("llm:") else None
        if kind:
            with self._lock:
                self.booked[kind] += float(usd or 0.0)

    def note(self, text: str) -> None:
        with self._lock:
            if text not in self.notes:
                self.notes.append(text)

    def finish(self, session, settings) -> None:
        """Book in cost_ledger what the call sites did not book (Jev of rerank/support checks, …)."""
        from klugscheisser.ingest import costs
        from klugscheisser.llm import text as llm_text

        llm_rest = round(self.llm_usd - self.booked["llm"], 6)
        jev_rest = round(self.jev_usd - self.booked["jev"], 6)
        if llm_rest > 1e-6:
            costs.record(session, llm_text.cost_source(settings, "answer"), 1, llm_rest, "frage-rest")
        if jev_rest > 1e-7:
            costs.record(session, "jev", self.jev_tokens, jev_rest, "frage")


_current: contextvars.ContextVar[AnswerBudget | None] = contextvars.ContextVar("answer_budget", default=None)


def current() -> AnswerBudget | None:
    return _current.get()


def daily_answer_spend(session, settings) -> float:
    """cost_ledger, last 24 h: the answer model source (llm:<provider>-answer) + Jev."""
    from klugscheisser.ingest import costs
    from klugscheisser.llm import text as llm_text

    return costs.spent_today(session, llm_text.cost_source(settings, "answer")) + costs.spent_today(
        session, "jev"
    )


@contextmanager
def answering(settings):
    """The budget for one answer. Nested answers (an answer inside an answer) share the outer one."""
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    from klugscheisser.db.session import session_scope

    b = AnswerBudget(
        max_usd=float(settings.answer_max_usd), daily_cap_usd=float(settings.llm_daily_cost_cap_usd)
    )
    try:
        with session_scope() as s:
            b.daily_spent_before = daily_answer_spend(s, settings)
    except Exception as exc:  # never block an answer on the ledger query
        log.warning("daily answer spend unknown: %s", str(exc)[:160])
    token = _current.set(b)
    try:
        yield b
    finally:
        _current.reset(token)


def estimate_llm_usd(settings, prompt_chars: int, output_tokens: int = WRITE_OUTPUT_TOKENS) -> float:
    """Rough cost of one text-model call before it is made (fresh input, no cache)."""
    from klugscheisser.llm import text as llm_text

    tokens_in = int(prompt_chars / CHARS_PER_TOKEN)
    return llm_text.cost_usd(settings, {"input": tokens_in, "output": output_tokens})


def check(what: str = "llm") -> None:
    if (b := _current.get()) is not None:
        b.before_call(what)


def charge_llm(settings, usage: dict | None, which: str | None = None) -> None:
    if (b := _current.get()) is not None:
        from klugscheisser.llm import text as llm_text

        b.charge_llm(llm_text.cost_usd(settings, usage, which))


def charge_jev(tokens: int) -> None:
    if (b := _current.get()) is not None:
        b.charge_jev(tokens)
