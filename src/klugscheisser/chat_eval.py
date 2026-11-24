"""Fixed chat evaluation (2026-10-05): the questions of `tests/fixtures/chat_eval.yaml`, answered with the prosa
style, each checked against what it must (and must not) say. Every run is stored, so the next run shows what
got better and what got worse.

Read-only for the chats (nothing is stored in chat tables, no answer cache). The model calls are paid
(text model + Jev; a full run about $0.05 with Gemini, about $0.65 with Claude Sonnet): the CLI asks for
--yes."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

QUESTIONS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "chat_eval.yaml"
RUNS_DIR = Path("data") / "chat-eval"
NOTHING = re.compile(r"nichts gefunden|keine belegte angabe|nicht belegt", re.I)


def _project_key() -> str | None:
    """None for the default project (ice hockey: the files as before), else its key ("unihockey")."""
    from klugscheisser import projects

    key = projects.current().key
    return None if key == projects.default().key else key


def questions_file() -> Path:
    """The current project's questions: chat_eval_<project>.yaml next to chat_eval.yaml when it exists."""
    key = _project_key()
    own = QUESTIONS.with_name(f"chat_eval_{key}.yaml") if key else None
    return own if own is not None and own.is_file() else QUESTIONS


def project_runs_dir() -> Path:
    """Saved runs per project, so a project is only compared with its own last run."""
    key = _project_key()
    return RUNS_DIR / key if key else RUNS_DIR


def load(path: Path | None = None) -> list[dict]:
    items = yaml.safe_load((path or questions_file()).read_text(encoding="utf-8")) or []
    for it in items:
        it.setdefault("muss", [])
        it.setdefault("darf_nicht", [])
        it.setdefault("art", "antwort")
    return items


def shown_text(result) -> str:
    """Everything the answer shows as statement (sentences, table cells), not the source list."""
    parts = []
    for b in getattr(result, "antwort", None) or []:
        parts += [x.text for x in b.saetze]
        if b.zeilen and b.spalten:
            parts.append(" | ".join(b.spalten))  # the table header is shown too
        parts += [" | ".join(z["zellen"]) for z in b.zeilen]
    if not parts:  # liste style or an engine without prosa: the statements
        parts = [st.aussage for st in getattr(result, "statements", None) or []]
    return "\n".join(parts)


def shape(result) -> str:
    if getattr(result, "rueckfrage", None):
        return "rueckfrage"
    return "antwort" if shown_text(result).strip() else "nichts"


def check(item: dict, result) -> list[str]:
    """Problems of one answer (empty = passed)."""
    text = shown_text(result)
    low = text.lower()
    problems = []
    got = shape(result)
    allowed = str(item["art"]).split("|")  # "antwort|nichts": either is fine (e.g. "is anyone injured?")
    if got not in allowed:
        problems.append(f"Form: {got} statt {item['art']}")
    if got == "antwort":
        for need in item["muss"]:
            if not any(alt.strip().lower() in low for alt in str(need).split("|")):
                problems.append(f"fehlt: {need}")
    for bad in item["darf_nicht"]:
        if str(bad).lower() in low:
            problems.append(f"enthält: {bad}")
    return problems


@dataclass
class Outcome:
    id: str
    group: str
    frage: str
    erwartet: str
    ok: bool
    probleme: list[str] = field(default_factory=list)
    antwort: str = ""
    verstanden: str | None = None
    rueckfrage: str | None = None
    nicht_sicher: int = 0
    kosten_usd: float = 0.0
    sekunden: float = 0.0
    modell: dict = field(default_factory=dict)  # model calls and tokens of this answer (input/cached/output)


def _context(it: dict) -> dict:
    """Page context of a question as the web chat passes it: `person: "Naemi Herzig"` (person page) or
    `team: sui-women` (team page). Resolved in the current database; unknown → no context."""
    if not (it.get("person") or it.get("team")):
        return {}
    from klugscheisser.db import models as m
    from klugscheisser.db import repo
    from klugscheisser.db.session import session_scope

    with session_scope() as s:
        if it.get("person"):
            a = repo.resolve_athlete(s, it["person"])
            return {"person_ids": [a.id]} if a else {}
        t = s.query(m.Team).filter(m.Team.key == it["team"]).one_or_none()
        return {"team_id": t.id} if t else {}


def run(engine, items: list[dict], style: str = "prosa") -> list[Outcome]:
    """Ask every question (follow-ups with the earlier answer as chat history) and check it."""
    done: dict[str, tuple[str, Any]] = {}
    out = []
    for it in items:
        history = None
        if (prev := it.get("nach")) and prev in done:
            pq, pr = done[prev]
            history = [
                {
                    "frage": pq,
                    "person_ids": list(getattr(pr, "person_ids", None) or []),
                    "saetze": shown_text(pr).splitlines()[:10],
                }
            ]
        started = time.monotonic()
        try:
            res = engine.answer(it["frage"], use_cache=False, style=style, history=history, **_context(it))
        except Exception as exc:  # one broken answer must not stop the run
            out.append(
                Outcome(it["id"], it["group"], it["frage"], it.get("erwartet", ""), False, [repr(exc)[:200]])
            )
            continue
        done[it["id"]] = (it["frage"], res)
        problems = check(it, res)
        out.append(
            Outcome(
                id=it["id"],
                group=it["group"],
                frage=it["frage"],
                erwartet=it.get("erwartet", ""),
                ok=not problems,
                probleme=problems,
                antwort=shown_text(res)[:1500],
                verstanden=getattr(res, "scope_hinweis", None),
                rueckfrage=getattr(res, "rueckfrage", None),
                nicht_sicher=len(getattr(res, "nicht_sicher", None) or []),
                kosten_usd=float(getattr(res, "kosten_usd", 0) or 0),
                sekunden=round(time.monotonic() - started, 1),
                modell=dict(getattr(engine, "last_usage", None) or {}),
            )
        )
    return out


def previous_run(runs_dir: Path | None = None) -> dict[str, bool]:
    runs_dir = runs_dir or project_runs_dir()
    files = sorted(runs_dir.glob("*.json"))
    if not files:
        return {}
    return {o["id"]: o["ok"] for o in json.loads(files[-1].read_text(encoding="utf-8"))["ergebnisse"]}


def save(outcomes: list[Outcome], runs_dir: Path | None = None) -> Path:
    runs_dir = runs_dir or project_runs_dir()
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    path.write_text(
        json.dumps(
            {"zeit": datetime.now(UTC).isoformat(), "ergebnisse": [asdict(o) for o in outcomes]},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return path


def report(outcomes: list[Outcome], before: dict[str, bool]) -> str:
    """Plain-text summary: score per group, newly broken / newly fixed, every failure with its problems."""
    lines = []
    ok = sum(o.ok for o in outcomes)
    cost = sum(o.kosten_usd for o in outcomes)
    secs = sorted(o.sekunden for o in outcomes)
    tok = {
        k: sum((o.modell or {}).get(k, 0) for o in outcomes)
        for k in ("calls", "input", "cached", "cache_write", "output")
    }
    lines.append(f"{ok}/{len(outcomes)} richtig · Kosten ${cost:.4f}")
    if secs:
        lines.append(
            f"  Zeit: Mittel {sum(secs) / len(secs):.1f} s, Median {secs[len(secs) // 2]:.1f} s · Modell: "
            f"{tok['calls']} Aufrufe, {tok['input']} Input-Tokens (davon {tok['cached']} aus dem Cache, "
            f"{tok['cache_write']} in den Cache geschrieben), "
            f"{tok['output']} Output-Tokens"
        )
    groups: dict[str, list[Outcome]] = {}
    for o in outcomes:
        groups.setdefault(o.group, []).append(o)
    lines.append("  " + " · ".join(f"{g} {sum(x.ok for x in xs)}/{len(xs)}" for g, xs in groups.items()))
    if before:
        worse = [o.id for o in outcomes if before.get(o.id) and not o.ok]
        better = [o.id for o in outcomes if before.get(o.id) is False and o.ok]
        lines.append(f"  schlechter als letzter Lauf: {', '.join(worse) or 'keine'}")
        lines.append(f"  besser als letzter Lauf: {', '.join(better) or 'keine'}")
    for o in outcomes:
        if not o.ok:
            first = (o.rueckfrage or o.antwort or "—").splitlines()[0][:160]
            lines.append(f"✗ {o.id} {o.frage}\n    {'; '.join(o.probleme)}\n    → {first}")
    return "\n".join(lines)
