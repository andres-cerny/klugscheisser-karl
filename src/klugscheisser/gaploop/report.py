"""Markdown views of stand-in runs, written for an agent (or Ondrej) to read: one run, or two side by side."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from klugscheisser.gaploop.standin import QuestionRun, Run

OUTPUT_TRIM = 1500  # chars of each tool output shown in report.md (run.json keeps everything)


def trim(text: str, n: int = OUTPUT_TRIM) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + f"\n… [{len(text) - n} more chars in run.json]"


def _args(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)


def _fence(text: str) -> str:
    return "````text\n" + text.replace("````", "'''") + "\n````"


def _question_section(q: QuestionRun) -> list[str]:
    out = [f"## {q.id}: {q.question}", ""]
    if q.notes:
        out += [f"_Notes:_ {q.notes}", ""]
    out.append(
        f"- time {q.duration_s:.1f} s · cost ${q.cost_usd:.4f} · turns {q.turns} · stop `{q.stop}` · "
        f"tokens in {q.tokens.get('input', 0)} / out {q.tokens.get('output', 0)} / cache read "
        f"{q.tokens.get('cache_read', 0)}"
    )
    out += ["", f"### Tool calls ({len(q.tool_calls)})", ""]
    if not q.tool_calls:
        out.append("_none_")
    for i, c in enumerate(q.tool_calls, 1):
        flag = " **BLOCKED**" if c.blocked else (" **ERROR**" if c.error else "")
        out += [
            f"{i}. `{c.name}` `{_args(c.args)}` · {c.duration_s:.1f} s · {len(c.output)} chars{flag}",
            "",
            _fence(trim(c.output)),
            "",
        ]
    out += ["### Answer", "", q.answer or "_(no answer)_", "", "---", ""]
    return out


def run_report(run: Run) -> str:
    from klugscheisser.gaploop.standin import median_time

    calls = sum(len(q.tool_calls) for q in run.questions)
    lines = [
        f"# Stand-in run {run.started_at}",
        "",
        f"- model `{run.model}` (effort {run.effort}), server `{run.server}`, project {run.project}",
        f"- questions: {len(run.questions)} from `{run.questions_file}`; tool calls: {calls}",
        f"- stand-in cost ${run.cost_usd:.4f} (cap ${run.cap_usd:.2f}); median answer time {median_time(run)} s",
        "- Karl-side cost (cost_ledger delta during the run, may include background jobs): "
        + (f"${run.karl_cost_usd:.4f}" if run.karl_cost_usd is not None else "n/a"),
        "- no web search: the stand-in only had Karl's read-only tools; full tool outputs are in run.json",
        "",
        "| id | time s | cost $ | tools | stop |",
        "|---|---|---|---|---|",
    ]
    for q in run.questions:
        tools = ", ".join(c.name for c in q.tool_calls) or "-"
        lines.append(f"| {q.id} | {q.duration_s:.1f} | {q.cost_usd:.4f} | {tools} | {q.stop} |")
    lines += ["", "---", ""]
    for q in run.questions:
        lines += _question_section(q)
    return "\n".join(lines)


def _key(q: QuestionRun) -> str:
    return q.id or q.question


def compare_report(a: Run, b: Run) -> str:
    """Both runs' answers per question (matched by id, else question text), with tools, time and cost."""
    by_b = {_key(q): q for q in b.questions}
    keys = [_key(q) for q in a.questions] + [k for k in by_b if k not in {_key(q) for q in a.questions}]
    by_a = {_key(q): q for q in a.questions}
    lines = [
        "# Compare stand-in runs",
        "",
        f"- **A** {a.started_at} · `{a.model}` · cost ${a.cost_usd:.4f}",
        f"- **B** {b.started_at} · `{b.model}` · cost ${b.cost_usd:.4f}",
        "",
        "| id | time A | time B | tools A | tools B | answer chars A | B |",
        "|---|---|---|---|---|---|---|",
    ]

    def cell(q: QuestionRun | None, what: str) -> str:
        if q is None:
            return "–"
        if what == "time":
            return f"{q.duration_s:.1f}"
        if what == "tools":
            return ", ".join(c.name for c in q.tool_calls) or "-"
        return str(len(q.answer))

    for k in keys:
        qa, qb = by_a.get(k), by_b.get(k)
        lines.append(
            f"| {(qa or qb).id} | {cell(qa, 'time')} | {cell(qb, 'time')} | {cell(qa, 'tools')} | "
            f"{cell(qb, 'tools')} | {cell(qa, 'len')} | {cell(qb, 'len')} |"
        )
    lines += ["", "---", ""]
    for k in keys:
        qa, qb = by_a.get(k), by_b.get(k)
        q0 = qa or qb
        lines += [f"## {q0.id}: {q0.question}", ""]
        for label, q in (("A", qa), ("B", qb)):
            if q is None:
                lines += [f"### {label}: not in this run", ""]
                continue
            tools = "; ".join(f"`{c.name}` {_args(c.args)}" for c in q.tool_calls) or "none"
            lines += [
                f"### {label} · {q.duration_s:.1f} s · ${q.cost_usd:.4f} · stop `{q.stop}`",
                "",
                f"Tools: {tools}",
                "",
                q.answer or "_(no answer)_",
                "",
            ]
        lines += ["---", ""]
    return "\n".join(lines)
