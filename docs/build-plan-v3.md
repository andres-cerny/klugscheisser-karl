# Build plan v3: sourced answers (Gemini Flash-Lite) + own app, connector stays

Status: proposal 2026-10-03, not started. Builds on v2 (everything in v2 is live).

## Goal
Answer *unpredictable* questions about athletes ("Wer studiert?", "Wer hatte zuletzt ein Comeback?",
"Gossip vor der WM?") with **every sentence backed by a verbatim quote and a link**, in two front ends:
1. the existing MCP connector (Perplexity / Claude), via a new `frage` tool;
2. an own web app (athlete pages + question box) using the Gemini API (Flash-Lite).

## Principles (agreed)
- **Thin structure, raw material.** Structured only: persons (athletes, coaches, …), teams, events, results,
  person↔document links, link/date/source per document. Everything else = raw documents + Jev labels.
  No fixed facts schema per question type.
- **Jev selects, Gemini reads, the source proves.** Gemini only cites document IDs; links come from our DB;
  quotes must appear verbatim; Jev checks that the quote supports the statement.
- **Per person, on demand.** Scales to 500+ names (skiing, coaches); updates only for persons asked about.
- **Sensitive topics:** relationships and health only as explicit self-statements (own post/interview), per
  person, always ⚠️ Vorsicht; never team-wide lists, never inferred from emojis/photos.
- **Cost:** Gemini 3.1 Flash-Lite by default (~1 ct/question, ~2 ct with web); 3.8 Flash only where Lite fails.

## Architecture
```
sources ─► ingest (Jev labels, Gemini media) ─► Postgres: persons · documents · links · search index
                                                        │
question ─► filters + Jev intent labels + hybrid search ─► Jev rerank (top 10–15 docs)
        ─► [gaps?] Brave search → fetch allowed pages → temporary docs (same pipeline)
        ─► Gemini Flash-Lite: JSON statements {aussage, dok, zitat}
        ─► checks: doc exists · quote verbatim in doc · Jev "quote supports statement" ≥ threshold
        ─► render: Datenstand · ⚠️ Vorsicht first · one statement per line · emoji (meaning) · «quote» · link
        ─► verified web docs saved to the pile (enrichment)
front ends: MCP tool `frage` (Perplexity/Claude)  ·  own web app (athlete pages + question box)
```

## Phases

**P1 — Persons instead of athletes (lead, sequential)**
- `athletes` → persons with `role` (athlete, coach, staff, official) and `sport`; migration keeps data.
- Jev disambiguation at ingest for ambiguous names (choice: which person / none), using sport, team, tagged handles.
- Tests: two namesakes in different sports are not mixed up.

**P2 — More raw material (sub-agent)**
- Document types `profile` (college/club/federation player pages, Wikipedia summary) and `interview`.
- Collectors: Wikipedia (de/en/fr) summary per person; public player profile pages where robots/terms allow
  (college rosters, club sites); interviews via existing news feeds (Jev label `interview`).
- Each document: link, date, source, verbatim text; linked to persons.

**P3 — Answer engine `frage` (lead, core)**
- `answer(question, persons|team|event|None, since?)`: selection (filters, intent labels, hybrid, Jev rerank),
  Gemini Flash-Lite with structured output `{statements:[{aussage, dok, zitat, kategorie}], luecken:[…]}`.
- Checks: document id valid; quote verbatim (normalised whitespace/quotes); Jev noul "Zitat stützt Aussage"
  ≥ threshold (calibrated like Vorsicht); failing → dropped (shown count) or "Unbestätigt" section.
- Sensitive-topic rules enforced in code (relationship/health statements only from `self` sources or interviews,
  per person, Vorsicht).
- Output: same line format as v2 + «quote»; Datenstand first; "Keine Angabe in unseren Quellen" when empty.
- Cost ledger per question (Jev, Gemini, search); daily cap.
- Tests: hallucinated quote dropped, invented doc id dropped, unsupported conclusion ("alum" ≠ "studiert jetzt")
  dropped, sensitive rule enforced, empty answer honest.

**P4 — Web enrichment (sub-agent)**
- `WebSearch` adapter: Brave Search API (`BRAVE_API_KEY`), top 3–5 results; fetch pages only where robots.txt
  and terms allow; otherwise snippet + link marked "nicht geprüft".
- Web pages become temporary documents → same P3 pipeline → lines marked 🌐 (Web).
- Verified web documents stored in the pile (person links, link, date) so the next question needs no search.
- Triggered only when P3 reports gaps or too few verified statements.

**P5 — Connector (lead)**
- MCP tool `frage(question, person?, team?, event?, since?)`, read-only annotation, ready-to-copy text block.
- Update client instructions: use `frage` for open questions, copy lines verbatim.

**P6 — Own web app (sub-agent, after P3 interfaces)**
- Same Python server (Starlette + server-rendered HTML + htmx; no separate frontend stack), mobile first, German.
- Pages: search (person/team/event autocomplete) · person page (Vorsicht box, Instagram cards with emoji label,
  image description and link, news, results, bio, facts from verified statements) · event page (teams, players,
  provisional/confirmed) · question box (`frage`, streamed progress: "suche … prüfe Zitate …") · refresh button
  with status · Datenstand always visible.
- Deterministic pages need no LLM; only the question box uses Gemini.
- Login: Cloudflare Access (email one-time code for Fabi) in front of the app; MCP endpoint keeps its token.

**P7 — Fixed address (lead, needs Ondřej's Cloudflare domain)**
- Named Cloudflare Tunnel on a subdomain (e.g. `athlete.<domain>`), autostart on the Mac (launchd);
  app behind Cloudflare Access, `/mcp` with token. Later the same tunnel moves to a Hetzner VPS.

**P8 — Evaluation and costs (lead)**
- Question log from real use (tool/app calls, no answers stored beyond the cache) → test set of 20–30 questions
  with expected sources (plus Fabi's written examples).
- Report: answer rate, dropped statements, cost per question; compare Flash-Lite vs 3.8 Flash on the test set.

## Parallelisation
P1 → P3 sequential (lead). P2 and P4 sub-agents in parallel with P3 against fixed interfaces
(`Document`, `WebSearch`, `AnswerEngine`). P6 sub-agent once P3's `answer()` contract is fixed. P5, P7, P8 lead.

## Prerequisites from Ondřej
1. Gemini billing switched on (paid tier) + Google budget alert.
2. Brave Search API key (`BRAVE_API_KEY`; $5 free credit/month).
3. A domain on Cloudflare for the fixed address + Cloudflare Access (free for small teams).
4. Fabi's example questions (any time; used in P8).

## Cost estimate (2026-10 prices)
Per question ~1 ct (Flash-Lite) / ~2 ct with web; ~600 questions/month ≈ $7–8. Plus Instagram updates
(~$0.03–0.07 per person per day asked) and media analysis (~$0.01–0.02 per new post). Jev negligible.
