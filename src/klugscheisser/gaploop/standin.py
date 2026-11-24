"""Stand-in client: Claude Sonnet plays Perplexity and answers questions using only Karl's MCP tools.

No web search: the model only gets Karl's (read-only) tools, so the transcript shows what Karl delivers.
Writes run.json (full transcript: tool calls, args, raw outputs, answer, time, tokens, cost) and report.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from klugscheisser.gaploop.envfile import read_key

MODEL = "claude-sonnet-5-5"
# USD per million tokens (claude-sonnet-5-5): input, output, cache read, cache write (5 min = 1.25 x input)
PRICES = {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50}
DEFAULT_URL = "http://127.0.0.1:8000/mcp/eishockey"
# the loop's branch instance (scripts/loop_instance.sh): own port, copy database klugscheisser_loop
LOOP_URL = "http://127.0.0.1:8020/mcp/eishockey"
LOOP_DATABASE_URL = "postgresql+psycopg:///klugscheisser_loop?host=/var/run/postgresql"
DEFAULT_CAP_USD = 0.50
MAX_TOKENS = 3000
MAX_TURNS = 8
# tools that change data or start paid background jobs: never offered to the stand-in
BLOCKED_TOOLS = frozenset(
    {"hinzufuegen", "hinzufuegen_antwort", "hinzufuegen_status", "refresh", "refresh_status"}
)
HERE = Path(__file__).parent
INSTRUCTIONS = {
    "eishockey": HERE / "instructions_eishockey.txt",
    "allgemein": HERE / "instructions_allgemein.txt",
}

HARNESS_NOTE = (
    "Heute ist {today} (Schweizer Zeit). Du hast KEINE Websuche: nutze ausschliesslich die Tools des Connectors "
    '"Klugscheisser Karl". Was die Tools nicht liefern, kennzeichne als nicht gefunden.'
)


# --- data --------------------------------------------------------------------------------------------------


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    output: str
    duration_s: float
    blocked: bool = False
    error: bool = False


@dataclass
class QuestionRun:
    id: str
    question: str
    notes: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    answer: str = ""
    duration_s: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    turns: int = 0
    stop: str = ""  # end_turn | max_turns | max_tokens | cost_cap | error: …


@dataclass
class Run:
    started_at: str
    model: str
    effort: str
    server: str
    project: str
    questions_file: str
    cap_usd: float
    instructions: str
    questions: list[QuestionRun] = field(default_factory=list)
    cost_usd: float = 0.0
    karl_cost_usd: float | None = None  # cost_ledger delta during the run (may include background jobs)
    finished_at: str = ""


def load_questions(path: Path, only: list[str] | None = None, limit: int | None = None) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    qs = [q for q in data.get("questions", []) if q.get("question")]
    if only:
        qs = [q for q in qs if str(q.get("id")) in only]
    return qs[:limit] if limit else qs


def cost_of(usage: Any) -> tuple[float, dict[str, int]]:
    tok = {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    return sum(tok[k] * PRICES[k] for k in tok) / 1_000_000, tok


def estimate_input_cost(system: str, tools: list[dict], messages: list[Any]) -> float:
    """Upper-bound guess (3 chars per token, all uncached) for the cap check before a call."""
    chars = len(system) + len(json.dumps(tools, ensure_ascii=False))
    for m in messages:
        content = m["content"] if isinstance(m, dict) else m
        chars += len(content) if isinstance(content, str) else len(json.dumps(content, default=str))
    return chars / 3 * PRICES["input"] / 1_000_000


# --- tools backend -----------------------------------------------------------------------------------------


class ToolBackend(Protocol):
    async def list_tools(self) -> list[dict]: ...  # [{name, description, input_schema}]
    async def call(self, name: str, args: dict) -> tuple[str, bool]: ...  # (text, is_error)

    server_instructions: str


class McpBackend:
    """Karl's live MCP server over Streamable HTTP with the bearer token (read from ~/klugscheisser/.env)."""

    def __init__(self, url: str, token: str | None):
        self.url, self.token = url, token
        self.server_instructions = ""
        self._stack = None
        self._session = None

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        self._stack = AsyncExitStack()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        hc = await self._stack.enter_async_context(create_mcp_http_client(headers=headers))
        streams = await self._stack.enter_async_context(streamable_http_client(self.url, http_client=hc))
        self._session = await self._stack.enter_async_context(ClientSession(streams[0], streams[1]))
        init = await self._session.initialize()
        self.server_instructions = getattr(init, "instructions", None) or ""
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def list_tools(self) -> list[dict]:
        res = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in res.tools
        ]

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        res = await self._session.call_tool(name, args, read_timeout_seconds=180)
        parts = [getattr(c, "text", "") for c in getattr(res, "content", []) or []]
        text = "\n".join(p for p in parts if p)
        if not text and getattr(res, "structured_content", None) is not None:
            text = json.dumps(res.structured_content, ensure_ascii=False, default=str)
        return text, bool(getattr(res, "is_error", False))


# --- the loop ----------------------------------------------------------------------------------------------


class CostCapReached(Exception):
    pass


class Budget:
    def __init__(self, cap_usd: float):
        self.cap, self.spent = cap_usd, 0.0

    def check(self, reserve: float) -> None:
        if self.spent + reserve > self.cap:
            raise CostCapReached(
                f"cap ${self.cap:.2f}: spent ${self.spent:.4f}, next call up to ${reserve:.4f}"
            )


def _block_dict(b: Any) -> dict:
    return b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)


async def ask(
    client: Any,
    backend: ToolBackend,
    tools: list[dict],
    system: str,
    q: dict,
    budget: Budget,
    model: str = MODEL,
    effort: str = "medium",
    max_turns: int = MAX_TURNS,
) -> QuestionRun:
    run = QuestionRun(id=str(q.get("id", "")), question=q["question"], notes=q.get("notes"))
    messages: list[dict] = [{"role": "user", "content": q["question"]}]
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    start = time.monotonic()
    try:
        while True:
            if run.turns >= max_turns:
                run.stop = "max_turns"
                break
            budget.check(estimate_input_cost(system, tools, messages) + MAX_TOKENS * PRICES["output"] / 1e6)
            resp = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
            )
            run.turns += 1
            cost, tok = cost_of(resp.usage)
            budget.spent += cost
            run.cost_usd += cost
            for k in totals:
                totals[k] += tok[k]
            messages.append({"role": "assistant", "content": [_block_dict(b) for b in resp.content]})
            uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                run.answer = "\n".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                ).strip()
                run.stop = resp.stop_reason or "end_turn"
                break
            results = []
            for u in uses:
                t0 = time.monotonic()
                args = dict(u.input or {})
                if u.name in BLOCKED_TOOLS:
                    text, err, blocked = (
                        f"Tool '{u.name}' ist im Test-Client gesperrt (schreibt Daten).",
                        True,
                        True,
                    )
                else:
                    blocked = False
                    try:
                        text, err = await backend.call(u.name, args)
                    except Exception as exc:  # tool/network error: show it to the model like a client would
                        text, err = f"Fehler beim Tool-Aufruf: {exc}", True
                run.tool_calls.append(
                    ToolCall(u.name, args, text, round(time.monotonic() - t0, 2), blocked=blocked, error=err)
                )
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": text, "is_error": err})
            messages.append({"role": "user", "content": results})
    except CostCapReached as exc:
        run.stop = f"cost_cap: {exc}"
    except Exception as exc:
        run.stop = f"error: {type(exc).__name__}: {exc}"
    run.duration_s = round(time.monotonic() - start, 2)
    run.tokens = totals
    return run


def build_system(instructions: str, server_instructions: str, today: str) -> str:
    parts = [instructions.strip()]
    if server_instructions:
        parts.append('Beschreibung des Connectors "Klugscheisser Karl":\n' + server_instructions.strip())
    parts.append(HARNESS_NOTE.format(today=today))
    return "\n\n".join(parts)


def offered_tools(all_tools: list[dict]) -> list[dict]:
    return [t for t in all_tools if t["name"] not in BLOCKED_TOOLS]


async def run_questions(
    client: Any,
    backend: ToolBackend,
    questions: list[dict],
    run: Run,
    on_done=None,
) -> Run:
    tools = offered_tools(await backend.list_tools())
    today = datetime.now(UTC).astimezone().strftime("%d.%m.%Y")
    system = build_system(run.instructions, backend.server_instructions, today)
    budget = Budget(run.cap_usd)
    for q in questions:
        if budget.spent >= budget.cap:
            run.questions.append(
                QuestionRun(id=str(q.get("id", "")), question=q["question"], stop="cost_cap: skipped")
            )
            continue
        qr = await ask(client, backend, tools, system, q, budget, model=run.model, effort=run.effort)
        run.questions.append(qr)
        if on_done:
            on_done(qr)
    run.cost_usd = round(budget.spent, 6)
    return run


# --- live DB: Karl's own cost during the run (read-only) ------------------------------------------------------


def karl_ledger_total(database_url: str | None) -> float | None:
    if not database_url:
        return None
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(database_url, connect_args={"options": "-c default_transaction_read_only=on"})
        with eng.connect() as c:
            v = c.execute(text("SELECT coalesce(sum(cost_usd), 0) FROM cost_ledger")).scalar()
        eng.dispose()
        return float(v or 0)
    except Exception:
        return None


def median_time(run: Run) -> float | None:
    times = [q.duration_s for q in run.questions if q.turns]
    return round(statistics.median(times), 2) if times else None


def save(run: Run, out_dir: Path) -> tuple[Path, Path]:
    from klugscheisser.gaploop.report import run_report

    out_dir.mkdir(parents=True, exist_ok=True)
    jp, mp = out_dir / "run.json", out_dir / "report.md"
    jp.write_text(json.dumps(asdict(run), ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(run_report(run), encoding="utf-8")
    return jp, mp


def load_run(path: Path) -> Run:
    d = json.loads(path.read_text(encoding="utf-8"))
    qs = d.pop("questions", [])
    run = Run(**d)
    for q in qs:
        calls = [ToolCall(**c) for c in q.pop("tool_calls", [])]
        run.questions.append(QuestionRun(**q, tool_calls=calls))
    return run


# --- CLI ---------------------------------------------------------------------------------------------------


def resolve_target(
    url: str | None, database_url: str | None, loop: bool, project: str
) -> tuple[str, str | None]:
    """MCP endpoint + database for the ledger delta. Default: live. --loop: the loop instance and its copy DB
    (the loop instance has only the eishockey project)."""
    if loop:
        if project != "eishockey":
            raise SystemExit("--loop: the loop instance serves only the eishockey project")
        return url or LOOP_URL, database_url or LOOP_DATABASE_URL
    url = url or f"{DEFAULT_URL.rsplit('/', 1)[0]}/{project}"
    if database_url:
        return url, database_url
    return url, read_key("DATABASE_URL") if project == "eishockey" and url == DEFAULT_URL else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcp_standin", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="answer the questions through Karl's MCP tools (paid: Anthropic API)")
    r.add_argument("--questions", type=Path, default=Path("tests/fixtures/mcp_gap_questions.yaml"))
    r.add_argument("--only", nargs="*", help="question ids")
    r.add_argument("--limit", type=int)
    r.add_argument("--project", default="eishockey", choices=sorted(INSTRUCTIONS))
    r.add_argument("--url", help=f"MCP endpoint (default {DEFAULT_URL.rsplit('/', 1)[0]}/<project>, live)")
    r.add_argument(
        "--loop",
        action="store_true",
        help=f"use the loop instance: --url {LOOP_URL}, ledger from klugscheisser_loop",
    )
    r.add_argument(
        "--database-url",
        help="database whose cost_ledger is read for Karl's cost (default: live DATABASE_URL; --loop: the copy)",
    )
    r.add_argument(
        "--instructions", type=Path, help="client instructions file (default: live Perplexity text)"
    )
    r.add_argument("--model", default=MODEL)
    r.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    r.add_argument("--max-cost", type=float, default=DEFAULT_CAP_USD, help="hard cap in USD per run")
    r.add_argument("--out", type=Path, help="output folder (default data/gap-runs/<time>-<label>)")
    r.add_argument("--label", default="run")
    c = sub.add_parser("compare", help="two runs side by side (free)")
    c.add_argument("a", type=Path, help="run.json or its folder")
    c.add_argument("b", type=Path)
    c.add_argument("--out", type=Path, help="markdown file (default: print)")
    rep = sub.add_parser("report", help="re-render report.md from a run.json (free)")
    rep.add_argument("run", type=Path)
    args = p.parse_args(argv)

    if args.cmd == "compare":
        from klugscheisser.gaploop.report import compare_report

        def _p(x: Path) -> Path:
            return x / "run.json" if x.is_dir() else x

        md = compare_report(load_run(_p(args.a)), load_run(_p(args.b)))
        if args.out:
            args.out.write_text(md, encoding="utf-8")
            print(args.out)
        else:
            print(md)
        return 0
    if args.cmd == "report":
        path = args.run / "run.json" if args.run.is_dir() else args.run
        print(save(load_run(path), path.parent)[1])
        return 0

    import anyio

    questions = load_questions(args.questions, args.only, args.limit)
    if not questions:
        print("no questions", file=sys.stderr)
        return 2
    url, db_url = resolve_target(args.url, args.database_url, args.loop, args.project)
    instructions = (args.instructions or INSTRUCTIONS[args.project]).read_text(encoding="utf-8")
    now = datetime.now(UTC)
    out = args.out or Path("data/gap-runs") / f"{now.astimezone().strftime('%Y%m%d-%H%M')}-{args.label}"
    run = Run(
        started_at=now.isoformat(timespec="seconds"),
        model=args.model,
        effort=args.effort,
        server=url,
        project=args.project,
        questions_file=str(args.questions),
        cap_usd=args.max_cost,
        instructions=instructions,
    )
    import anthropic

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY via `with-secrets klugscheisser`
    ledger_before = karl_ledger_total(db_url)

    def progress(q: QuestionRun) -> None:
        tools = ", ".join(t.name for t in q.tool_calls) or "-"
        print(f"[{q.id}] {q.duration_s:.1f} s ${q.cost_usd:.4f} tools: {tools} ({q.stop})", flush=True)

    async def go() -> None:
        async with McpBackend(url, read_key("MCP_AUTH_TOKEN")) as backend:
            await run_questions(client, backend, questions, run, on_done=progress)

    anyio.run(go)
    ledger_after = karl_ledger_total(db_url)
    if ledger_before is not None and ledger_after is not None:
        run.karl_cost_usd = round(ledger_after - ledger_before, 4)
    run.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    jp, mp = save(run, out)
    print(f"cost ${run.cost_usd:.4f} (cap ${run.cap_usd:.2f}); median {median_time(run)} s\n{mp}\n{jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
