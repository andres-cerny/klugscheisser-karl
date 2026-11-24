"""Compare MCP `search`: OLD path (vectors + full text, RRF, Jev on the top 24) vs NEW shared path
(retrieval/docs.py: Postgres candidates, Jev scores up to SEARCH_JEV_MAX_DOCS, no vectors).

Runs against a COPY database only (default klugscheisser_loop), in read-only transactions; never the live
databases. Person updates (freshen) are switched off.

  # free, mechanical: both paths with the word-overlap scorer, old path without vectors (full text only)
  uv run python scripts/search_compare.py --out /tmp/x
  # paid (real Jev for both, Gemini query vectors for both paths): needs TYPESAFE_API_KEY + GEMINI_API_KEY
  # in the environment (e.g. `with-secrets <file> -- …`); stops before a query once --max-usd is reached
  with-secrets klugscheisser -- uv run python scripts/search_compare.py --yes --max-usd 1 --out /tmp/x

Queries: the 18 `search` calls the Sonnet stand-in made in the MCP gap loop runs (2026-10-17..21) plus
free-text queries without a person (QUERIES below), or --queries FILE (yaml list of {query, athlete?, team?,
since?, source_type?, limit?}).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from functools import partial
from pathlib import Path

LIVE_DBS = {"klugscheisser", "klugscheisser_allgemein"}
JEV_USD_PER_M = 0.042
GEMINI_EMBED_USD_PER_M = 0.15  # gemini-embedding-001 list price per M input tokens (query vectors)

# gap-loop stand-in calls (data/gap-runs/*/run.json, tool `search`)
GAP_LOOP = [
    {"query": "Aufgebot Södertälje Euro Hockey Tour Schweiz Nationalteam Kader", "since": "2026-10-01"},
    {"query": "Frauen-WM November Verlegung IIHF PWHL Kalender"},
    {"query": "Headcoach Trainerstaff Nationaltrainer Frauen-Nati Assistenztrainer"},
    {"query": "Justin Krueger Assistent Nati Frauen Staff Torhütertrainer"},
    {"query": "Lahti Euro Hockey Tour Schweiz Frauen"},
    {"query": "Länderspiel Tschechien Schweiz Resultat Euro Hockey Tour", "since": "2026-08-01"},
    {"query": "Österreich Frauen Nationalteam Resultat Länderspiel", "team": "Österreich"},
    {
        "query": "Österreich Schweiz Frauen Eishockey",
        "team": "Schweiz",
        "source_type": "official_result",
        "limit": 8,
    },
    {"query": "Schweiz Deutschland Österreich Frauen Eishockey Spiel Resultat", "limit": 8},
    {
        "query": "Schweiz Frauen Nationalteam Resultat Tschechien",
        "source_type": "official_result",
        "limit": 10,
    },
    {"query": "Schweiz Österreich Frauen Eishockey Testspiel"},
    {"query": "Schweiz USA Frauen Länderspiel Resultat", "limit": 8},
    {"query": "Tschechien Schweiz Penaltyschiessen Lahti Tore Torhüterin", "since": "2026-10-02"},
    {"query": "Tschechien Women's Euro Hockey Tour Lahti Resultat", "team": "Tschechien"},
    {"query": "USA Frauen Länderspiel Resultat Kanada Rivalry Series", "team": "USA"},
    {
        "query": "USA Women's National Team game result 2026 Oktober",
        "team": "USA",
        "since": "2026-08-01",
        "source_type": "official_result",
    },
    {"query": "USA women's national team game result Rivalry Series", "since": "2026-08-01"},
    {"query": "verletzt Verletzung Ausfall Nati Frauen", "since": "2026-09-15"},
]
# free text without person/team (where vectors used to matter most), incl. English / synonyms / labels
FREE_TEXT = [
    {"query": "Kreuzbandriss"},
    {"query": "knee injury season over"},
    {"query": "Rücktritt Karriereende Nationalspielerin"},
    {"query": "Wechsel nach Schweden SDHL neuer Verein"},
    {"query": "PWHL Draft Schweizerin"},
    {"query": "Olympia Bronze Milano Cortina"},
    {"query": "Studium College NCAA Hockey"},
    {"query": "Sponsor Werbung Partnerschaft"},
    {"query": "Torhüterin Shutout"},
    {"query": "Hochzeit verlobt"},
    {"query": "goalie of the year award"},
    {"query": "Frauen-WM Dänemark Herning Spielplan"},
]
PERSON = [
    {"query": "Verletzung", "athlete": "Alina Müller"},
    {"query": "Tore Saisonstart", "athlete": "Lara Stalder"},
    {"query": "Comeback", "team": "Schweiz"},
]
QUERIES = GAP_LOOP + FREE_TEXT + PERSON


class Recorder:
    """Wraps the scorer: counts calls, texts, characters, Jev input tokens (paid) and their time."""

    def __init__(self, make_base):
        self.make_base = make_base
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.calls = self.texts = self.chars = self.tokens = 0

    def score(self, query, texts):
        base = self.make_base()
        out = base.score(query, texts)
        tokens = int(getattr(getattr(base, "client", None), "usage", {}).get("input_tokens", 0) or 0)
        with self.lock:
            self.calls += 1
            self.texts += len(texts)
            self.chars += sum(len(t[:800]) for t in texts) + len(query) * len(texts)
            self.tokens += tokens
        return out

    name = "recorder"


def est_tokens(rec: Recorder) -> int:
    """Without Jev: ~3.5 characters per token, plus ~45 tokens per candidate for the per-candidate question."""
    return int(rec.chars / 3.5 + rec.texts * 45)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="klugscheisser_loop")
    ap.add_argument("--out", required=True, help="output folder (json + markdown)")
    ap.add_argument("--yes", action="store_true", help="paid: real Jev (+ Gemini query vectors for OLD)")
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--queries", help="yaml file with a list of queries")
    ap.add_argument("--only", help="comma list of query numbers (1-based)")
    args = ap.parse_args()
    if args.db in LIVE_DBS:
        print(f"refusing live database {args.db}", file=sys.stderr)
        return 2
    os.environ["DATABASE_URL"] = f"postgresql+psycopg:///{args.db}?host=/var/run/postgresql"
    os.environ["PROJECTS_FILE"] = ""
    os.environ["AUTO_REFRESH"] = "false"
    # never any collector or writer key; Jev + Gemini (query vectors) only in paid mode
    blank = ["ANTHROPIC_API_KEY", "APIFY_TOKEN", "BRAVE_API_KEY", "X_BEARER_TOKEN", "XAI_API_KEY"]
    if not args.yes:  # free mode: no key reaches any client
        blank += ["TYPESAFE_API_KEY", "GEMINI_API_KEY"]
    for key in blank:
        os.environ[key] = ""

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    from klugscheisser.config import Settings
    from klugscheisser.mcp_server import tools as t
    from klugscheisser.retrieval.search import FakeReranker

    queries = QUERIES
    if args.queries:
        import yaml

        queries = yaml.safe_load(Path(args.queries).read_text())
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        numbered = [(i, q) for i, q in enumerate(queries, 1) if i in keep]
    else:
        numbered = list(enumerate(queries, 1))

    base = Settings(_env_file=None, auto_refresh=False)
    if args.yes:
        if not (base.features["jev"] and base.features["gemini"]):
            print("--yes needs TYPESAFE_API_KEY and GEMINI_API_KEY in the environment", file=sys.stderr)
            return 2
        from klugscheisser.classify.jev import JevReranker

        rec = Recorder(lambda: JevReranker(base))
        gem = {"embedding_provider": "gemini"}  # both paths: Gemini query vectors (stored vectors are Gemini)
        old_settings = base.model_copy(update={"mcp_search_embeddings": True, **gem})
        new_settings = base.model_copy(
            update={"mcp_search_embeddings": False, "retrieval_vector_candidates": True, **gem}
        )
    else:
        rec = Recorder(FakeReranker)
        # free: no paid query vector, so neither path has vector hits (the copy holds Gemini vectors only)
        old_settings = base.model_copy(update={"mcp_search_embeddings": True})
        new_settings = base.model_copy(
            update={"mcp_search_embeddings": False, "retrieval_vector_candidates": False}
        )
        t.get_embedder = lambda _s: None
    new_settings = new_settings.model_copy(update={"retrieval_embeddings": False})

    t.freshen = lambda s, athletes: {"status": "harness: keine Aktualisierung"}
    t.get_settings = lambda: base
    orig_runner = t._search_runner
    t._search_runner = partial(orig_runner, reranker=rec)

    engine = create_engine(os.environ["DATABASE_URL"])

    @event.listens_for(engine, "connect")
    def _read_only(dbapi_conn, _rec):
        with dbapi_conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        dbapi_conn.commit()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, spent = [], 0.0
    for n, q in numbered:
        if args.yes and spent >= args.max_usd:
            print(f"stop: {spent:.4f} USD >= --max-usd {args.max_usd}")
            break
        row = {"n": n, **q}
        for label, st in (("old", old_settings), ("new", new_settings)):
            rec.reset()
            t0 = time.perf_counter()
            with Session(engine) as s:
                try:
                    res = t.search_tool(s, st, **q)
                    err = None
                except Exception as exc:  # e.g. unknown team name: recorded, the run goes on
                    res, err = {"treffer": []}, f"{type(exc).__name__}: {str(exc)[:200]}"
                s.rollback()
            ms = round((time.perf_counter() - t0) * 1000)
            tokens = rec.tokens if args.yes else est_tokens(rec)
            usd = tokens / 1e6 * JEV_USD_PER_M
            if args.yes:
                spent += usd + GEMINI_EMBED_USD_PER_M * 20 / 1e6  # one query vector per path
            row[label] = {
                "ms": ms,
                "error": err,
                "erweitert": bool(res.get("erweitert")),
                "jev_calls": rec.calls,
                "jev_docs": rec.texts,
                "jev_tokens": tokens,
                "jev_usd": round(usd, 5),
                "hits": [
                    {
                        "url": h.get("url"),
                        "titel": (h.get("titel") or h.get("zusammenfassung") or "")[:90],
                        "datum": h.get("datum"),
                        "label": bool((h.get("_scores") or {}).get("label_match")),
                        "gefunden": (h.get("_scores") or {}).get("gefunden"),
                    }
                    for h in res.get("treffer") or []
                ],
            }
        o, w = ({h["url"] for h in row[x]["hits"]} for x in ("old", "new"))
        row["overlap"] = len(o & w)
        rows.append(row)
        print(
            f"{n:2d} old {len(o):2d} hits {row['old']['jev_docs']:3d} docs {row['old']['ms']:5d} ms | "
            f"new {len(w):2d} hits {row['new']['jev_docs']:3d} docs {row['new']['ms']:5d} ms | "
            f"same {row['overlap']:2d} | {q['query'][:60]}"
        )
    (out_dir / "compare.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    (out_dir / "compare.md").write_text(render(rows, args.yes, spent))
    tot = {x: sum(r[x]["jev_tokens"] for r in rows) for x in ("old", "new")}
    print(
        f"\n{len(rows)} queries; Jev tokens old {tot['old']} new {tot['new']} "
        f"({'measured' if args.yes else 'estimated'}); "
        f"USD old {tot['old'] / 1e6 * JEV_USD_PER_M:.4f} new {tot['new'] / 1e6 * JEV_USD_PER_M:.4f}"
        + (f"; spent ~{spent:.4f}" if args.yes else "")
    )
    print(f"written: {out_dir / 'compare.md'}")
    return 0


def render(rows, paid: bool, spent: float) -> str:
    mode = (
        "PAID (real Jev, Gemini query vectors for both)"
        if paid
        else "FREE (word-overlap scorer, OLD without vectors)"
    )
    lines = [f"# MCP search: old vs new ({mode})", ""]
    lines.append("| # | query | filters | old hits | new hits | same | old docs/ms | new docs/ms |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        filt = ", ".join(f"{k}={r[k]}" for k in ("athlete", "team", "since", "source_type") if r.get(k))
        lines.append(
            f"| {r['n']} | {r['query']} | {filt} | {len(r['old']['hits'])} | {len(r['new']['hits'])} | "
            f"{r['overlap']} | {r['old']['jev_docs']}/{r['old']['ms']} | {r['new']['jev_docs']}/{r['new']['ms']} |"
        )
    for r in rows:
        lines += ["", f"## {r['n']}. {r['query']}"]
        for x in ("old", "new"):
            d = r[x]
            lines.append(
                f"**{x}** ({d['jev_docs']} docs scored, {d['ms']} ms{', widened' if d['erweitert'] else ''})"
                + (f" ERROR {d['error']}" if d["error"] else "")
            )
            lines += [
                f"- {h['datum']} {'[label] ' if h['label'] else ''}"
                f"{'(' + '/'.join(h['gefunden']) + ') ' if h.get('gefunden') else ''}{h['titel']} <{h['url']}>"
                for h in d["hits"]
            ]
    if paid:
        lines += ["", f"Spent ~{spent:.4f} USD."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
