# Karl MCP retrieval research (2026-10-25)

Status: complete (Q1-Q6, recommendations, test plan)

## Q1. Delivering results to Perplexity / MCP clients

### What is documented about Perplexity as an MCP client (checked 2026-10-25)
- Help center "Adding Custom Remote Connectors" (no date on page; https://www.perplexity.ai/help-center/en/articles/13915507-adding-custom-remote-connectors):
  Streamable HTTP or SSE, HTTPS, auth None/API key/OAuth, CF-Access headers. **Nothing** about result display,
  size limits, timeouts, structuredContent, resources or citations. Disclaimer: Perplexity "cannot verify third-party
  MCP servers or guarantee answer quality".
- "Troubleshooting connectors" (https://www.perplexity.ai/help-center/en/articles/2026091802-troubleshooting-connectors):
  manifest cached until disconnect/reconnect; connectors are NOT called when sources are Web-only/Academic/Social;
  some surfaces need per-chat opt-in. No size limit or timeout values.
- Agent API connectors doc (https://docs.perplexity.ai/docs/agent-api/tools/connectors): **connector tools use deferred
  discovery by default**: the model gets the connector namespace, searches it and loads only the schemas it needs.
  Tool errors are passed in-band to the model. No structuredContent, size or citation rules. If the consumer product
  uses the same harness (likely but unverified), tool **names + descriptions decide whether a tool is ever loaded**;
  16 tools with overlapping names hurt discovery.
- Karl's own observation (docs/perplexity.md, 2026-10-03): UA `Perplexity-MCP/1.0`, Bearer key, AWS IPs; a question
  without "Eishockey" was answered from web search, not the connector.
- Unknown / must be measured on our logs: per-call timeout, max result size, whether structuredContent is read at all,
  whether URLs inside tool text become Perplexity citations (Perplexity marketing says connectors give "cited answers",
  https://enterprise-prod.perplexity.ai/enterprise/app-connectors, but no spec). Treat Perplexity as "reads `content`
  text only" until proven otherwise.

### Spec facts (MCP 2025-11-25, https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- Result `content` items: text, image, audio, `resource_link`, embedded `resource`; all can carry `annotations`
  (`audience`, `priority` 0-1, `lastModified`). `structuredContent` + optional `outputSchema`; "a tool that returns
  structured content SHOULD also return the serialized JSON in a TextContent block" (backwards compatibility).
- `isError: true` results with actionable text are meant for the model to self-correct.
- Tools have `title`, `annotations` (readOnlyHint etc.), `icons`. Clients must treat annotations as untrusted hints;
  no evidence any LLM client feeds `priority`/`lastModified` to the model. So: put dates/relevance **in the text**.

### Evidence from other clients
- OpenAI (https://developers.openai.com/api/docs/mcp, fetched 2026-10-25): connector/deep-research standard is two tools,
  `search(query)` -> `{results:[{id,title,url}]}` and `fetch(id)` -> `{id,title,text,url,metadata}`, sent as
  structuredContent AND JSON text. "ChatGPT creates citation metadata only when `url` is a non-empty string."
  -> lesson: clients cite what has a clean per-item URL.
- OpenAI forum (2026-06-08..07-19, https://community.openai.com/t/tool-response-truncation-on-mcp-connector-responses-that-previously-worked/1383071):
  ChatGPT truncates MCP results "at a line boundary to fit the tool response budget" (size undisclosed); sending both
  text and structuredContent duplicated the payload. -> lesson: one representation, keep it small, put the
  most important items first (truncation cuts the tail).
- Anthropic "Writing effective tools for agents" (2025-09-11, https://www.anthropic.com/engineering/writing-tools-for-agents):
  few consolidated, workflow-shaped tools over many overlapping ones; return high-signal, natural-language fields
  (names, not UUIDs); `response_format` concise/detailed enum; pagination/truncation with sensible defaults and a
  truncation note that steers the agent to narrower searches (Claude Code caps tool results at 25k tokens);
  actionable errors; format (JSON/Markdown/XML) has "no one-size-fits-all", decide by eval; tool descriptions are
  prompts -- small wording changes gave large gains.

### Recommendations for Karl (MCP path)
1. **Keep one compact Markdown/plain text block, no duplicate JSON** (Karl already did this in `feat/frage-compact`).
   Only add structuredContent if a test shows Perplexity reads it; if added, keep text as the primary payload.
2. **Each result = one self-contained card**, most relevant first: `[n] Titel — Quelle (Typ), Datum (YYYY-MM-DD, "vor 3 Tagen")`,
   then the best passage (300-800 chars, the Jev-selected passage, not the doc start), then `URL: <canonical link>`.
   Plain URLs per item maximise the chance Perplexity cites them (by analogy with ChatGPT; unverified for Perplexity).
   Put the absolute date + age in text; models are bad at inferring recency from metadata.
3. **Top 8 at ~12k chars is in the right range**; better: return 6-10 cards with short passages (~6-8k chars) plus a
   final line "Weitere Treffer: 14 (mit `seite=2` oder `dokument(id)` abrufbar)". Gives Perplexity a cheap way to
   go deeper instead of us guessing (Anthropic pagination advice; OpenAI search/fetch split).
4. **Add/keep a `fetch`-style tool** (`dokument(id)` returning the full text or the requested section, capped) so the
   client reads full long articles only when needed. Cards carry the short id.
5. **Consolidate tools**: with deferred discovery, 16 tools with overlapping scopes risk wrong/no tool selection.
   Target ~5-7 read tools: `frage`/`suche` (general), `person`, `team`, `event/turnier`, `resultate/spiele`,
   `dokument`. Write/refresh tools hidden from Perplexity (or separate endpoint). Descriptions in plain words incl.
   "Frauen-Eishockey", examples of good queries, and what to do on empty results.
6. **Say what the search did** in a 1-line header: query understood as ..., time window, n candidates, "keine Treffer
   aus den letzten 14 Tagen" etc. Empty/weak results should return an actionable message (`isError` not needed),
   e.g. suggest the person tool or a different spelling -> lets the client iterate (see Q3).
7. Offer optional params the client model can set (models decide, not rules): `seit`/`bis` (date window),
   `typ` (news/resultat/social/bio), `sprache`, `anzahl`, `detail: kurz|voll`. Defaults stay sensible.
8. Measure what Perplexity actually does: log every call's args + response size + latency (already logged), and run
   2-3 manual probes: (a) a tool returning a resource_link + text URL, see what is cited; (b) a 30k-char response, see
   if truncation happens; (c) structuredContent-only response, see if Perplexity still answers.

## Q2. Reranker choice

Per-query workload assumed: 120 candidates x ~800 chars (~200-250 tokens DE/FR) = ~30k input tokens.

| Option | Cost / query (est.) | Latency (est.) | Multilingual DE/FR/EN | Instructions | Notes |
|---|---|---|---|---|---|
| Jev (TypeSafe, current) | ~$0.0013 (30k x $0.042/M) | measured 0.4-0.7 s (6 parallel chunks) | ok in our tests | yes (free-text `instructions` per question + `state`) | already returns per-level probabilities -> `score_answer` takes the expected level, so scores are effectively continuous; ties only when `probabilities` is missing |
| Claude Haiku 5.5 (`claude-haiku-5-5`) pointwise, 6 parallel calls | ~$0.003 in + ~$0.0004 out = **~$0.0035** ($0.10/$0.50 per MTok, prompts <100k tokens; skill table cached 2026-10-06) | unknown, not published; expect ~1-2 s for 6 parallel short calls with thinking off/low -- **must measure** | strong | full prompt | thinking is on by default; set low effort or `thinking: {type: "disabled"}` (allowed at effort high or below on Haiku 5.5). No forced tool_choice issue if using `output_config.format` JSON. Prompt caching of the instruction prefix helps only above the min cacheable size. |
| Claude Sonnet 5.5 (`claude-sonnet-5-5`) | ~$0.06-0.07 ($2/$10) | slower than Haiku | strong | full prompt | too expensive per search vs. `ANSWER_MAX_USD=0.08`; use only as an offline **judge/teacher** for evaluation labels |
| Voyage rerank-2.5 / -lite | billed (query_tokens x docs)+doc_tokens = ~32k tokens -> $0.0016 / $0.0006; **first 200M tokens free per account** (https://docs.voyageai.com/pricing) | not published; cross-encoder API, typically sub-second for ~100 short docs (unverified) | 31-language eval: +3.26% vs Cohere 3.5, +2.34% vs Qwen3-Reranker-8B (https://blog.voyageai.com/2025/08/11/rerank-2-5/, 2025-08-11) | **yes, natural-language instructions** (+8.13% domain, +11.48% real-world sets, vendor numbers) | 32k context; best "dedicated" fit for reader context; continuous scores |
| Cohere Rerank 4 Pro/Fast (2025-12-11) | ~$0.002-0.0025 per search unit (OpenRouter listing; a unit is up to 100 docs -> 2 units for 120) | Fast = low latency (no number) | 100+ languages, cross-lingual | not documented | https://docs.cohere.com/changelog/rerank-v4.0 |
| Cohere Rerank 5 Pro/Fast (2026-10-21, `rerank-v5.0-pro/-fast`) | unconfirmed (AlphaSignal: $0.05 / $0.02 per M tokens) | vendor-reported median 11.7 s (Pro) / 4.1 s (Fast) per 96-doc request (doc size unknown) -> too slow for Perplexity unless docs are short | multilingual | not documented | **Context Focus** returns the most relevant passages per doc (scored char spans), **calibrated scores** (one threshold across queries). Brand new (4 days old): treat claims as vendor-only. https://docs.cohere.com/docs/models |
| Jina reranker v3 / v3.5 (0.6B listwise) | API or self-host | GPU-oriented | MIRACL 72-74 (unified top-100 protocol, vendor) | no | v3.5 weights non-commercial |
| Local bge-reranker-v2-m3 (568M) on 4 vCPU | free | **my estimate: ~1-2 min for 120 x 256-token pairs** (≈300M non-embedding params x 2 FLOP x 256 tok x 120 ≈ 18 TFLOP at ~150 GFLOPS); no published CPU benchmark found | good | no | not viable on this box; base-size multilingual models (~35 s) and mMiniLM (~9 s) also too slow and weaker. Also competes with Postgres/Karl for CPU. |

Evidence on LLM rerankers:
- Fin (Intercom), 2025-09-11, https://fin.ai/research/using-llms-as-a-reranker-for-rag-a-practical-guide/ -- design nearly
  identical to Jev: **pointwise**, 0-10 rubric with few-shot anchors, K=40 passages of ~200 tokens split into 4 parallel
  batches **assigned round-robin by retrieval rank** (each batch sees a mix, reduces position bias), cross-encoder breaks
  ties/fills timeouts, tight timeouts, P50 +0.9 s. A/B: LLM pointwise beat the BGE cross-encoder significantly;
  **listwise = same quality, +40% latency, +15% cost**. Dropping passage ids from output hurt (index/score confusion).
  Later they distilled the LLM labels into a ModernBERT cross-encoder.
- Zhuang et al., NAACL 2024 (https://arxiv.org/abs/2310.14122): graded labels beat yes/no for pointwise LLM rankers; the
  expected value over label probabilities is a good score. -> Jev's 4 levels + probabilities is the recommended pattern;
  more levels (e.g. 0-6) give diminishing returns, the probability-weighted score already breaks ties.
- Abdallah et al., EMNLP Findings 2025 (https://arxiv.org/abs/2508.16757): LLM rerankers win on familiar queries, but
  generalisation to novel queries varies; lightweight models are competitive on efficiency.
- ZeroEntropy (vendor, 2025): listwise LLM +0.04 NDCG@10 at ~9x cost/35x latency; suggests specialised reranker +
  LLM only on top 10. Vendor bias.

Verdict for Karl:
1. **Keep Jev as the default** (cheapest, fast, already pointwise with graded probabilities). Cheap upgrades:
   round-robin chunk assignment by first-stage rank (Fin), check `probabilities` is always returned (log when it
   falls back to the integer), pass title + date + source type + the best passage (not only the doc start), and a
   short reader-context line (Q4).
2. **A/B against Voyage rerank-2.5 (with instruction)** -- effectively free (200M free tokens), multilingual, continuous
   scores, instruction-following. Most likely dedicated-reranker winner.
3. **Haiku 5.5 pointwise** as the "LLM judge" arm (~$0.0035/query, ~3x Jev). Use structured JSON output, ids kept,
   low effort/thinking disabled, same parallel chunking. Worth it only if it clearly beats Jev on our 33 queries.
4. Cohere Rerank 5 Fast: interesting because Context Focus would give the best passage per long doc for free, but
   latency claims (4.1 s/96 docs) and pricing need checking; test after Voyage.
5. Sonnet 5.5: offline only, as the labeller for the evaluation set (and a possible teacher if we ever distil).

## Q3. Agentic retrieval (client iterates) vs heavy server pipelines

Evidence:
- **Our own logs (live journal, 2026-10-24 15:09-15:50, UA `Perplexity-MCP/1.0`, 84 requests)**: Perplexity already
  behaves agentically. One question produced list_events x3 + get_team_roster + get_athlete + get_event in parallel,
  then a broad `frage` ("Bei welchen Klubs spielen ...") followed by **~12 parallel per-player `frage` calls**
  ("Bei welchem Klub spielt <Name>"), each answered in 0.2-1.2 s with ~300 chars. One broad `frage` took **33.7 s and
  returned 140,228 chars** (before the 12k cap went live at 21:44 the same day). -> Perplexity rewrites and fans out on
  its own; slow/huge responses are the real risk, not lack of iteration.
- BrowseComp-Plus (arXiv 2508.06600, 2025-08; ACL 2026): with a fixed corpus, a better retriever (Qwen3-Embedding-8B vs
  BM25) raised GPT-5 accuracy 55.9% -> 70.1% **and reduced the number of search calls**; agents compensate for weak
  retrieval only by spending more calls. Follow-up "Pi-Serini" (2026, abstract only, unverified): well-tuned lexical
  retrieval with enough depth + strong LLM is competitive.
- Anthropic tools guide (2025-09-11): tools should do the multi-step work that is always needed (consolidation), return
  only relevant high-signal results, and steer the agent with truncation/empty-result messages.

Sweet spot for Karl:
- **Server does what every query needs and the client cannot do**: hybrid candidates (FTS + vectors + labels), entity
  resolution (names, spelling variants, Umlaut/French forms), the rerank, dedup of near-duplicate news, dates, compact
  cards. That is the "retriever quality" lever that cuts client calls.
- **Server should NOT do answer-writing or rule-heavy intent routing for MCP**: the client model (Perplexity's) writes
  the answer, decides follow-ups and query rewrites. Server-side answer agents (`frage` with LLM interpretation) add
  latency (6-34 s seen) and cost, and duplicate what Perplexity already does. Keep `frage` as fast retrieval with an
  optional, explicit "kurz zusammengefasst" mode at most.
- **Make iteration cheap and well-guided**: fast (<2 s) calls, small responses (300-8k chars), clear tool descriptions,
  optional filters the model may set (date window, type, person/team), pagination/`dokument(id)`, and a header line
  telling the model what was searched and what is missing ("Keine Treffer seit 2026-10-10; versuche get_athlete").
- Hard budgets protect against fan-out: per-call timeout (e.g. 8 s, return partial results with a note), per-call char
  cap (12k already), and cheap caching of identical calls within minutes (Perplexity repeats near-identical calls).

## Q4. Reader context / instruction for the reranker

Which rerankers take instructions:
- **Voyage rerank-2.5/-lite**: yes, natural-language instructions prepended to the query; vendor-reported +8.13% (24 domain
  sets) and +11.48% (3 real-world sets); examples include disambiguation ("this is an e-commerce site about cars") --
  exactly our "Frauen-Eishockey, not football" case (https://blog.voyageai.com/2025/08/11/rerank-2-5/, 2025-08-11).
- **Cohere Rerank 4/5**: no documented instruction parameter (docs fetched 2026-10-25); prepending text to `query` is
  untested and eats the shared 32k window.
- **Qwen3-Reranker / Qwen3-Embedding**: instruction-aware; model card: instructions usually give +1-5%, write them in
  English (https://huggingface.co/Qwen/Qwen3-Embedding-4B).
- **LLM judges (Jev, Haiku)**: free-form; Jev already has an `instructions` field per question and a `state` text.
- Caution: FollowIR (arXiv 2403.15246, 2024) found classic retrievers/rerankers mostly use instructions as keyword cues;
  some API models got *worse* with changed instructions. Reasoning rerankers (Rank1, 2025, arXiv 2502.18418) follow
  them better. -> instructions must be short, concrete and tested, not long preference essays.

Best practice for Karl (keeps "models decide"):
1. One short reader-context paragraph (3-5 sentences), the same for all queries, e.g.
   "Heute ist 2026-10-25. Die Nutzerin ist Sportmoderatorin/Journalistin beim Schweizer Frauen-Eishockey und bereitet
   Sendungen und Kommentare vor; sie braucht überprüfbare, aktuelle Fakten mit Quelle. Relevant ist, was die Frage
   direkt beantwortet; Porträts/Biografien sind relevant, wenn nach Hintergrund oder Karriere gefragt wird. Andere
   Sportarten und Männer-Eishockey sind nicht relevant, ausser die Frage fragt danach."
   (Corrected 2026-10-25: Fabi is a sports presenter/journalist, not a fan.)
   Put preferences as **tie-breakers**, never as override -- otherwise a fresh Swiss post outranks the doc that
   actually answers the question.
2. Give the judge the facts it needs to apply the context: in each candidate's text put `Datum: 2026-10-21 (vor 4 Tagen)`,
   `Typ: News/Resultat/Instagram/Bio`, `Quelle`, title, then the best passage. A judge cannot prefer recent items if it
   cannot see dates.
3. Query-specific context comes from the client: let the tool accept an optional `kontext`/`absicht` string (Perplexity
   often knows the user's intent) and pass it to the reranker instruction.
4. Measure it: arm "Jev" vs "Jev + reader context" on the same 33 queries (free re-run cost is only Jev tokens).

## Q5. Embeddings

Facts about our data (loop copy `klugscheisser_loop`, read-only, 2026-10-25): 19,559 items; 10,002 "full" (avg ~1,200
chars incl. title) and 9,557 "light". Of the 10,002 embedded items only **2,016 have >2,000 chars and 292 >6,000**;
median text 181 chars. Largest types: web (3,782, avg 1,828 chars), profile/bio (1,791, avg 2,265). So chunking matters
for ~2k docs (web news + bios), not for posts/results.

Gemini docs (https://ai.google.dev/gemini-api/docs/embeddings, fetched 2026-10-25):
- `gemini-embedding-001` input limit is **2,048 tokens**. Our `t[:8000]` (~2,000-2,700 tokens in DE/FR) can exceed it ->
  the tail of the longest ~300 docs is silently cut (default auto-truncate). Minor today, but means "first 8,000 chars"
  is really "first ~2,048 tokens".
- 001 task types include `QUESTION_ANSWERING` (queries phrased as questions, matched to docs that answer them) and
  `FACT_VERIFICATION`; we use RETRIEVAL_QUERY. Perplexity sends questions ("Bei welchem Klub spielt ...") ->
  QUESTION_ANSWERING for question-shaped queries is a free A/B.
- MTEB by dimension (001): 3072 ~68.2, 1536 68.17, 768 67.99, 512 67.55, 256 66.19 -> **384 is roughly 66.9 (interpolated),
  ~1.1 points below 768**. Small but free to fix when re-embedding.
- **`gemini-embedding-2`** (preview 2026-03-10; https://blog.google/innovation-and-ai/technology/developers-tools/gemini-embedding-2/):
  MMTEB 69.9 vs 68.32 for 001 (tech report arXiv 2605.27295), **8,192-token input**, auto-normalised truncation, no
  task_type: use prefixes `task: search result | query: ...` / `task: question answering | query: ...` and documents as
  `title: {title} | text: {content}` (title is part of the embedded text by design). Price $0.20/M text tokens,
  batch $0.10/M (pricing page; 001 price not shown there now -- was $0.15/M, unverified).
- Alternatives on MMTEB (Qwen3-Embedding card, leaderboard 2025-05-24): Qwen3-Embedding-8B 70.58, 4B 69.45, 0.6B 64.33,
  gemini-embedding(-exp/001) 68.37, multilingual-e5-large-instruct 63.22, Cohere embed-multilingual-v3 61.12,
  OpenAI 3-large 58.93. Qwen3 8B/4B need a GPU or API to embed 20k+ docs; 0.6B runs on CPU but is weaker than Gemini.
  Cohere embed-v4 (256-1536 dims, 128k ctx) and Voyage-3.5 not on that table; no reason to switch providers.
  **Recommendation: stay on Gemini; move to gemini-embedding-2 at 768 dims (halfvec) when re-embedding.**

Re-embedding cost: all ~19.6k items ≈ 15M chars ≈ 4-5M tokens -> **~$0.50-1.00 at $0.20/M, half with batch**; plus
chunks for long docs (overlap adds ~10-20%). Negligible; the real cost is engineering time and a reindex window.

Chunking (only for docs > ~1,500 chars):
- News: ~300-500 tokens (~1,200-2,000 chars) per chunk, 10-15% overlap, split on paragraphs. Bios/Wikipedia: by
  section (Karriere, Nationalmannschaft, Statistik), each ≤500 tokens. Posts/results/teasers: one vector, no chunks.
- Keep **one doc-level vector** (title + lead) **plus chunk vectors**; doc score = max over its chunks (max-sim is the
  standard and simplest; sum/mean favours long docs). Return the **best chunk as the passage** for Jev and for the
  Perplexity card -- this aligns "why it was retrieved" with "what the judge reads" (today Jev reads an 800-char passage
  chosen by term overlap).
- **Contextual header per chunk** (cheap, deterministic, no LLM): `Titel | Quelle | Datum | Personen: A, B | Team` prepended
  to the chunk text before embedding. Anthropic Contextual Retrieval (2024-09-19, https://www.anthropic.com/engineering/contextual-retrieval):
  LLM-written chunk context cut top-20 retrieval failures by 35% (embeddings), 49% (+BM25), 67% (+rerank); cost ~$1.02 per
  M doc tokens with Claude 3 Haiku + caching. For Karl a deterministic header gets most of the benefit for news; an LLM
  context line (Haiku 5.5, ~$0.10/M in) is worth testing only for bios/long features (~2k docs, cents).
- Late chunking (Jina, arXiv 2409.04701) needs token-level access to a long-context embedder (Jina v3/v4 API flag);
  not available with Gemini -> skip.
- pgvector: put chunk vectors in the existing `embeddings` table (item_id, chunk_no, vector) with the HNSW cosine
  index; query top ~150 chunks with `hnsw.iterative_scan` (already used, strict_order) and `hnsw.ef_search` >= 100, then
  `DISTINCT ON (item_id)` to ~40 docs. Raise `hnsw.max_scan_tuples` if filters are selective. Scale (≤100k vectors) is
  tiny for this box; 768-dim halfvec ≈ 150 MB incl. index (estimate).
- Metadata into the embedded text: yes for title/date/source/person names (header above); not labels/scores.

## Q6. "Light" documents (RSS headline + teaser, ~9,557, avg ~350 chars incl. title)

Data (loop copy): top sources Berner Zeitung 1,765, TVP Sport 1,441, RTBF 748, STVR 659, ORF 606, RTV SLO 567, Yle 402,
NRK 383, NOS 381 ... mostly general sports feeds. Crude regex: **~1,324 (14%) mention hockey in some language, only ~84
(<1%) look like women's hockey**; dates 2020-11 .. 2026-10.

Pros of vectorising and admitting them: cheap (~1M tokens ≈ $0.10-0.20 one-off), headlines are dense and often name the
player/team + result, sometimes the only trace of an item (e.g. a Slovak/Finnish result line), raises recall for
"what happened recently" queries.
Cons: ~86% are other sports -> they would take a sizeable share of the ~40 vector slots and the 120 Jev slots; teaser
text is short, so embeddings match on generic sports words; a reranker can only judge the headline: it can say "this
is about X" but often cannot confirm the answer -> scores cluster in the middle levels ("passt teilweise"), and
the Perplexity card would have little to show except the link.

Recommendation: **do not mix them into the main pool unfiltered.** Instead:
1. Embed them (cheap) but tag them as `light` and **admit only those that pass a cheap hockey pre-filter**: either
   Jev's existing classifier on the headline (pennies per thousand) or the vector itself (cosine to a "Frauen-Eishockey"
   centroid) -- a model decision, not keyword rules. Expect ~1k-1.5k survivors.
2. Give them a **separate small quota** in the candidate pool (e.g. ≤10 of 120) so they cannot crowd out full docs, and
   let the reranker rank them normally; in the card mark them "nur Schlagzeile/Teaser" so Perplexity knows to open the
   URL itself if needed.
3. Measure: recall@40 on the 33 queries with and without light docs (free), and count how many light docs reach the
   top 8 and whether they were the right answer.

## Recommendations ranked by expected impact (MCP path)

1. **Fast, small, card-shaped results** (Q1/Q3): every MCP read tool answers in <2-3 s, 6-10 cards with date/age, type,
   source, best passage and URL, ~6-8k chars, plus a "what was searched / what's missing / more via dokument(id)"
   line. Kill server-side answer writing on the MCP path. Our logs show 6-34 s `frage` calls and a 140k-char response;
   Perplexity fans out by itself (12 parallel per-player calls), so speed + clarity beat server cleverness.
2. **Fewer, clearer tools** (Q1): Perplexity loads connector tools via deferred discovery (Agent API docs); consolidate
   16 tools to ~6 read tools with plain-language descriptions; hide write/refresh tools from the Perplexity endpoint.
3. **Judge sees the right text** (Q2/Q4/Q5): give Jev title + date + type + source + the best-matching passage/chunk and a
   3-5 sentence reader context with preferences as tie-breakers. Round-robin chunk assignment by first-stage rank.
4. **Chunk the ~2k long docs + deterministic context header** (Q5): doc vector + chunk vectors, max-over-chunks, best
   chunk becomes the passage. Do it together with a switch to gemini-embedding-2 at 768 dims (halfvec), title in the
   document format, question-style query prefix. ~$1 one-off.
5. **Test Voyage rerank-2.5 with instruction** against Jev (Q2/Q4): free within 200M tokens, continuous scores,
   instruction-following, multilingual. Haiku 5.5 pointwise as the LLM arm (~3x Jev cost). Keep Jev if neither wins
   clearly -- it is already cheap and graded.
6. **Light docs: embed, pre-filter by model, admit with a small quota** (Q6), not unfiltered.
7. Probe Perplexity behaviour once (citations from text URLs vs resource_link, truncation size, structuredContent),
   then fix the format to what it actually uses.

## What I'd test

Labelled set first (needed for everything): the 33 MCP queries in `scripts/search_compare.py`. Pool candidates from all
arms (top 20 per arm, dedup -> ~40-60 docs/query), label each (query, doc) 0-3.
- Cheapest: Ondrej/Fabi label ~10 queries by hand (free, best ground truth).
- Paid: Claude Sonnet 5.5 as offline labeller: ~33 x 50 docs x ~300 tokens ≈ 0.5M input -> **~$1-1.5** (at $2/$10;
  use Batches for -50%). Spot-check 50 labels by hand.

Free tests (no API, loop copy only):
- F1 candidate recall: for each query, recall@40/@120 of labelled-relevant docs per source (FTS, vectors, labels) and
  for the union -> shows whether misses are candidate misses (fix retrieval) or rerank misses (fix judge).
- F2 light docs: rerun F1 with `include_light=True` (FTS only, no vectors needed): extra relevant docs found vs noise added.
- F3 passage check: for labelled-relevant long docs, does the 800-char passage contain the answer? (string check vs label).
- F4 response shape: sizes/latency of every MCP read tool on the 33 queries (already logged); target <3 s, <8k chars.
- F5 log mining: from the journal, Perplexity call patterns (fan-out, repeated queries, timeouts) per session.

Small paid A/B (rerank on the same 120 candidates/query, metric nDCG@8 + "answer in top 3" against labels):
| Arm | Cost for 33 queries |
|---|---|
| A Jev (current) | ~$0.04 |
| B Jev + reader context + date/type in text | ~$0.05 |
| C Claude Haiku 5.5 pointwise, 6 parallel calls, JSON output, low effort | ~$0.12 (measure latency!) |
| D Voyage rerank-2.5 + instruction (and -lite) | $0 (free tier) |
| E Cohere rerank-v4.0-fast (or v5.0-fast if pricing/latency check out) | ~$0.17 (2 search units x $0.0025) |

Embedding A/B (recall@40 of labelled docs, vector source only):
| Arm | Cost |
|---|---|
| E1 current (001, 384, RETRIEVAL_QUERY, first 8k chars) | $0 |
| E2 001, query task QUESTION_ANSWERING | ~$0 (33 query embeddings) |
| E3 001 at 768 dims | re-embed ~$0.7 |
| E4 gemini-embedding-2 768, title format, doc + chunk vectors with header, max-over-chunks | ~$1 (batch ~$0.5) |
Run on a copy table/DB (`klugscheisser_loop`), never live.

Total paid budget: **~$3-4** including labelling. Decision rule: adopt an arm only if it wins on the labelled set and
does not add >0.5 s p50 latency; otherwise keep the simpler option.

Uncertainties: Perplexity's size limit, timeout, citation and structuredContent handling are undocumented; Claude Haiku
5.5 rerank latency is unmeasured; Cohere Rerank 5 (released 2026-10-21) numbers are vendor-only; CPU reranker latencies
are my FLOP estimates, not benchmarks; gemini-embedding-001 current price not on the pricing page.
