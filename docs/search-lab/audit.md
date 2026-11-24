# Karl MCP path audit (read-only, 2026-10-25, branch exp/search-jev-path @0664a91)

No code changed, no paid calls, no DB writes. One read-only length query on the copy DB `klugscheisser_loop`.
Paths are relative to `src/klugscheisser/` unless they start with `scripts/`, `docs/`, `tests/`.

## Summary in plain language
- Perplexity sees 16 tools. Several answer the same need in different ways: last games through 3 tools, news
  through 4. `frage` writes an answer with Claude, and then Perplexity writes it again. `search` returns
  documents but misses what `frage` adds (the persons' facts, status, computed tables).
- The MCP path is not yet the chat path. `frage` runs 5 word-pattern shortcuts before any model. It never uses the
  question interpreter or the agent: in the code both run only for the chat's prose answers. Only the document
  search (`retrieval/docs.py`) is shared, and even there Jev reads a different text for MCP and for chat.
- About 165 word patterns decide things a model could decide. Perplexity could pick them through tool
  parameters and enums, Jev could score them, or the interpreter could map the question. The biggest block is
  `answer/cross.py` (2,400 lines).
- Jev already gives continuous scores. We put 20 documents into one long text and ask about "Kandidat i".
  TypeSafe's own guides send one query+document pair per request, as structured JSON, and filter on a calibrated
  yes/no probability. That costs about the same and matches the model's known weaknesses (long state, indirection).
- Embeddings: one 384-dim Gemini vector per whole document, no date, persons or labels in the text. 1,424 long
  documents (Wikipedia/college bios, web pages) crowd open questions. Splitting long documents into chunks fixes
  the cause instead of tuning `passage`/`coverage`.
- Target: one evidence-pack tool (`suche`). It returns exactly what the chat writer would get, and Perplexity
  writes. The rules become structured tools or candidate documents that Jev scores. 10 reversible steps (5.3),
  each checked with `scripts/search_compare.py` (33 queries) or the MCP gap-loop stand-in.

## 1. MCP server inventory

### 1.1 The 16 tools (mcp_server/server.py)

| # | Tool | Line | What it does | Inputs | Output |
|---|---|---|---|---|---|
| 1 | `status` | 180 | row counts, newest item, features | – | **dict** (only tool returning a dict, so the SDK sends JSON; everything else is one Markdown text) |
| 2 | `get_event` | 231 | whole tournament sheet (~10k chars) | event, posts_per_player=0 | Markdown via `_run` |
| 3 | `get_athlete` | 252 | full portrait of one person (~10-14k) | name | Markdown |
| 4 | `stats` | 265 | season stats, rankings, comparisons (structured filters) | person(s), team, league, season, team_kind, phase, nationality, position, sort_by, group, min_games, limit | Markdown |
| 5 | `get_athlete_posts` | 330 | Instagram posts by/tagging one player | name, since, limit=10, include_tagged | Markdown |
| 6 | `get_team_roster` | 346 | roster (+ staff) of one team | team, competition | Markdown |
| 7 | `get_ig_handles` | 362 | verified handles | athletes[], team | Markdown |
| 8 | `whats_new` | 376 | newest documents about a person/team/event since a date (no ranking, newest first, 25) | target, since=7d | Markdown |
| 9 | `search` | 392 | document retrieval on the shared path (`retrieval/docs.find_documents`) | query, athlete, team, since, source_type, limit=8 | Markdown list of hits (title/first sentence 220 chars + excerpt 320 + up to 2 quotes + link; no score shown) |
| 10 | `refresh` | 420 | start a data update (paid Apify in background) | target | Markdown |
| 11 | `refresh_status` | 434 | wait ≤60 s for a job | job_id | Markdown |
| 12 | `frage` | 447 | **server-side answer**: 5 regex shortcuts, then the answer engine in list style (Claude Sonnet writes, Jev checks each quote) | frage, person, team, event, since, web=true | Markdown statements «quote» + link |
| 13 | `hinzufuegen` | 575 | add-person/team proposal (writes) | name, art, details, instagram | str |
| 14 | `hinzufuegen_antwort` | 604 | confirm/reject/refine (writes, starts paid load) | anfrage, antwort, text | str |
| 15 | `hinzufuegen_status` | 658 | wait ≤60 s for the add load | anfrage | str |
| 16 | `list_events` | 678 | events with keys; with `team`: its last N games, next games; with `opponent`: head-to-head | days=21, team, last, since, opponent | Markdown |

All tools are annotated `READ_ONLY` (server.py:49) except the three `hinzufuegen*` (`WRITES`, server.py:543); note
`refresh` is marked read-only although it starts paid work (deliberate: no confirmation prompt in Perplexity).

### 1.2 What Perplexity sees
- **Server instructions** (sent once per session, server.py:29-45): data scope; "output is German Markdown; answer
  in the language of the user's question"; rules (Datenstand first, keep links, ⚠️ first, quotes verbatim, handles
  only from tools, missing ≠ proof); and a **routing table in prose** ("stats for numbers …, frage for open
  questions, injuries, coaches/staff and tournament questions …, search for anything else").
- **Connector instructions** (outside the code, `gaploop/instructions_eishockey_v2.txt`, 1 kB): "ALWAYS call Karl
  first, look up every person individually", "usually 1 tool: stats / get_team_roster / frage / list_events /
  get_athlete". `search` is not mentioned there at all.
- **Tool descriptions** are long and partly overlap with each other; `frage`'s description (server.py:449-460) is
  ~900 chars and lists the regex shortcuts it has inside (staff, tournament, team games, connections, named
  tournament games, clubs of a whole team). Perplexity is in effect told "say these words and the right rule fires".
- **Per-call footer**: one line `_Jede Aussage mit ihrem Link übernehmen._` (present.py:358); `frage` ends with
  `_Aussage, «Zitat» und Link gehören zusammen._` (present.py:963).

### 1.3 Output format
- One `TextContent` with German Markdown, no `structuredContent`, no JSON copy (server.py:210-228, decided in the
  gap loop 2026-10-17 because the JSON copy doubled size). `status` is the exception (dict).
- **12,000-char cap** on every tool (`MAX_TOOL_CHARS`, server.py:108), cut at a line end with a per-tool "ask
  narrower" hint (`MORE_HINT`, server.py:109-118; `cap_text` 121-130). No paging/offset parameter exists, so the
  cut-off part is unreachable except by re-asking differently. Sections are also capped at 12 entries
  (`SECTION_MAX`, present.py:355).
- `one_source_per_block` (present.py:372) folds ≥3 lines with the same link into "Quelle für alle Zeilen: …".
- **Language**: output always German. Non-German/English documents get a cached German translation of title/first
  sentence (present.py:172-187, lookup only); `frage` quotes get `zitat_de` (engine.py:2075 `translate_quotes`,
  paid, cached). Input: `frage` accepts German or English; the interpreter (EN→DE, names, pronouns) runs only for
  the web chat's prose style (engine.py:1298-1313: `if prosa and self._use_interpreter()`), **never for MCP**.
  `search` passes the raw query to full text (German + `simple` config) and to the vector query.
- Every call is logged with duration, size and the first 600 chars (server.py:147-172) for the gap loop.

### 1.4 Overlaps
| Need | Tools that answer it | Note |
|---|---|---|
| a team's last games / form / head-to-head | `list_events(team, last, opponent)` **and** `frage` → `team_games.answer_text` (regex `RESULTS_Q`) | same data, two doors; routing decided by regex in frage, by Perplexity for list_events |
| games of a named tournament | `get_event`, `list_events`, `frage` → `event_games.answer_text` | three doors |
| tournament format / schedule | `get_event` (whole sheet), `frage` → `tournament.answer` (regex topics) | |
| coaches / staff | `get_team_roster` (staff where stored), `frage` → `staff.answer` (regex `STAFF_Q`) | |
| a person's news / posts | `whats_new`, `get_athlete_posts`, `get_athlete` (latest posts), `search(athlete=…)` | four doors, different ranking (newest vs Jev) |
| Instagram handles | `get_ig_handles`, `get_team_roster`, `get_athlete` | |
| open question | `frage` (answer) vs `search` (documents) | see 1.5 |

### 1.5 Are `frage` and `search` both needed?
They do different jobs today, but for the owner's goal they are one job done twice:
- `frage` = rule shortcuts + retrieval + **a second writer** (Claude Sonnet writes list statements, Jev checks
  every quote), then Perplexity writes the final reply again. Cost per call ~$0.01-0.08 (ANSWER_MAX_USD 0.08),
  latency several seconds, and Perplexity sees only the statements that survived, not the documents.
- `search` = the same document path without writing (Jev only, ~$0.001), but it lacks what `frage` adds before the
  writer: scope resolution from the question text (`scope_athletes`), the persons' facts/bio/status documents
  (`fact_docs`, `bio_docs`, `status_docs`), computed documents (stats, staff, games, tournament sheet, cross-person
  tables). So `search` is weaker on person questions and Perplexity is told to prefer `frage`.
- Conclusion: one tool is enough if it returns **the writer's input** (the ranked evidence pack the chat writer
  gets: passages + computed documents, each with link and date) and Perplexity writes. Keep the checked-statement
  mode only as an option during the transition (see section 5). The `frage(web=true)` parameter is dead: answers
  use web search only when `ANSWER_WEB_SEARCH` is on (default off, engine.py:1451), yet the schema advertises it
  "(default true)" (server.py:470-473).

## 2. Hard-coded rules on the MCP / retrieval path

Rough size: ~165 `re.compile` patterns in `answer/`, `staff.py`, `tournament.py`, `mcp_server/`, `retrieval/`,
`person_status.py`; `answer/cross.py` alone is 2,399 lines of mostly question-pattern → computed-answer rules.
"Who could decide": **J** = Jev (calibrated score/noul/choice, cheap), **S** = a small/fast LLM call (or the existing
Sonnet interpreter), **P** = Perplexity through better tool parameters/descriptions, **C** = keep in code (it is
data plumbing, not judgment). Risk = what may break if removed.

### 2.1 Routing before any model (MCP `frage`, server.py:500-531)
Order: staff → (no person) connections → event_games → team_games → (no person) tournament → answer engine.
| Rule | file:line | What it does | Could decide instead | Risk of removing |
|---|---|---|---|---|
| `STAFF_Q`, `WIDE_Q`, `ASK_ROLE` (10 role regexes), `HISTORY_Q`, `NOT_STAFF` | staff.py:112-149, entry `answer` 634 | coach/staff words → stored staff table, model-free | **P**: a `team_staff`/roster tool param (`get_team_roster` already carries staff); or staff table as an evidence document Jev scores | medium: staff answers are good today (gap loop); keep the data, drop the trigger |
| `CONNECT_Q` | answer/connections.py:28-37, `answer_text` 451 | "played together / same club" between two teams | **P** (a structured tool `connections(team_a, team_b)`) or **S** | low-medium; saves ~20 Perplexity calls when it fires |
| `GAME_DETAIL`, `NOT_GAMES`, `ASKS_GAMES`, `PLAYER_DETAIL`, `SWISS` | answer/event_games.py:25-35, 184-196, `answer_text` 242, `find` 118 | named tournament (Lahti, Södertälje) → its games and reports | **P** (`list_events(event=…)` / `get_event(section="spiele")`) | low; duplicated by list_events/get_event |
| `LAST_N`, `RESULTS_Q`, `NUMBER_WORDS` | answer/team_games.py:24-45, `answer_text` 297 | "letzte 3 Spiele / Form / head-to-head" → stored games | **P**: identical to `list_events(team, last, opponent)` | low (pure duplicate) |
| `WM_WORDS`, `TOURNAMENT`, `TOPICS` (~10 topic regexes), `OVERVIEW`, `PERSONAL_WORDS`, `STAT_WORDS`, `PLAYER_ASK`, `FORMAT_WORDS`, `OTHER_COMPETITION`, `CURRENT_CUE`, date regexes | tournament.py:59-260, `event_for_question` 311, `answer` 763 | tournament format/schedule/history → sheet sections | **P** (`get_event(key, section=…)`), or the sheet sections as evidence docs scored by **J** | medium: WM questions are the main use before Nov 2026; do after A/B |

### 2.2 Answer engine, list style (what MCP `frage` gets after 2.1; engine.py:1337-1490)
| Rule | file:line | What it does | Could decide instead | Risk |
|---|---|---|---|---|
| `_stats_step` + stats question patterns | engine.py:837; stats/ | stats questions computed from season rows | **P** (`stats` tool exists) / **S** interpreter template `statistik` | low on MCP (Perplexity has `stats`) |
| `event_games.find` | engine.py:1344 | 2nd chance for tournament-game questions | **P** | low |
| `cross.try_answer` with `FACT_RULES` (17 regexes), `EXTRA_KIND_CUES`, `RELATION`, `PER_PERSON`, `SAID`, `CLUB_NOW/PAST`, `AGE_*`, `PAST_Q/FUTURE_Q`, `PLAYS_NOW/NOT_NOW`, `LEAGUES`, `NATI_WORDS`, `TRANSFER_Q`, `TOURNAMENT_ASK`, `GROUP_WORDS`, `WIDE`=40 | answer/cross.py:49-190, 524, 954, 1646-2080, `try_answer` 796 | cross-person questions ("Wer studiert?", "älter als 30", "wer spielt in der SDHL") → computed tables + per-person evidence | **S**: the chat already has the Sonnet interpreter mapping to 14 templates (`ARTEN`, interpret.py:17-32); the MCP path skips it. Short term: run the interpreter for MCP too; long term: group tables as evidence docs | high: biggest rule block, many chat-eval cases rest on it; migrate last |
| `scope_from_text`, `EVENT_CUES`, `SWISS_TEAM`, `GENERIC_TEAM` | engine.py:454-534 | finds persons/team/event from question words | **P** (pass `person`/`team`/`event`, already in the schema) + **S** interpreter for chat | medium: wrong scope = wrong documents |
| `STRUCTURED` → add all `fact_docs` | engine.py:525, used 1441 | age/followers/number questions get every person's facts unranked | **J**: give fact docs to Jev with the rest | low-medium |
| `INJURY_Q` → add `status_docs`; `STATUS_INJURY_DAYS`=365 | engine.py:678-679, used 1439 | injury questions get the "newest injury + since" status | **J** (status docs as candidates) / **C** for the reconciliation itself | medium (injury status is sensitive) |
| `RECORD` | engine.py:370 (chat prose) | "record" questions skip stats | **S** | low |
| `TOP_DOCS`=15, `DOC_CHARS`=1500, `MAX_CANDIDATES`=150, `MAX_CROSS_STATEMENTS`=80, `MAX_FACT_PERSONS`=60 | engine.py:35-37, 530-531 | writer input size | **C** (budget), but derive from score drop-off instead of fixed 15 | low |
| support threshold 0.6 / 0.4 computed | config.py:109-117 | Jev quote-support gate | **J** already decides; threshold is the only knob | keep |

### 2.3 Shared retrieval (`retrieval/docs.py`, MCP `search`, chat, agent)
| Rule | file:line | What it does | Could decide instead | Risk |
|---|---|---|---|---|
| `INTENTS` (8 regexes → Jev label filters) | mcp_server/tools.py:684-715 | "verletz/ACL/…" → category injury, transfer, sponsor, liebe, vorsicht-only, comeback, milestone, karriereende | **P**: enum param `thema=[verletzung, transfer, …]` on the tool; **J** labels already exist per doc; vectors now cover synonyms | low since v2 (labels are only *candidates*, not shown first). A/B: drop them, compare recall on #18-#33 |
| `QUESTION_TOPIC_CUES` (10 regexes → Jev topic tags, up to 150 docs) | answer/engine.py:778-797 | chat only (via `candidate_items`) | same as above | low-medium (group questions "Wer studiert?") |
| `LABEL_CANDIDATES`=40, `Extra(...,40)` team reports | tools.py:681; engine.py:765-767 | sizes of label/team lists | **C** | low |
| `search_terms`: words ≥4 letters or 3 caps, first 6 letters, `_COMMON` stop list (10 words) | retrieval/docs.py:53-72 | terms for coverage, passage choice, fallback | gone with chunking (vector+FTS pick the chunk) | low |
| `passage`: 800 chars = first 250 + densest 550 window | retrieval/docs.py:75-90 | what Jev reads of a long document | **chunking** (section 4/5) | medium: this is what makes profiles readable today |
| `coverage` ordering when over the cap; `RESERVED`=15 FTS + 15 vector always kept | retrieval/docs.py:45-48, 184-192 | which candidates Jev sees | **J** on all (cap only for latency) | low; v2.1 judged better with RESERVED |
| caps `SEARCH_JEV_MAX_DOCS`=120 (MCP), `ANSWER_JEV_MAX_DOCS`=600 (chat), `FTS_K`=40, `VECTOR_K`=40, `FTS_POOL`=3, `NO_PERSON_NEWEST`=40 | config.py:142,146; retrieval/docs.py:42-49 | candidate pool sizes, MCP ≠ chat | **C**, but one number for both paths | low |
| `RANK_NORM`=1 (length normalisation) | retrieval/docs.py:45 | long bios don't win full text | chunking makes it moot | low |
| `RERANK_MIN_SCORE`=0.2 | config.py:78, used docs.py:284 | drop hits below expected level 0.6 of 3 | **J** noul "contains answer evidence" (calibrated, ~0.5), or **P**: show the score and return top-N | low-medium |
| sort ties: score, matched, newest | retrieval/docs.py:305-312 | tie-break | keep (continuous scores rarely tie) | none |
| `linked_only` when no person in scope | engine.py:759 | chat: only documents linked to a known person/team | **C** keep | – |
| since-widening: 0 hits → 30 more days back, with a note | tools.py:631-636 | MCP `search` only | **P**: return "0 since X; N older, newest dated …" and let Perplexity retry | low |
| `parse_since` defaults: search 30 d, whats_new 7 d, frage 30 d | tools.py:50-69, 570, 623; server.py:499 | date windows | **P** passes since; **C** parsing | none |
| `only_relevant` (Jev relevance at ingest), `level == "full"` | retrieval/search.py:103-123 | excludes irrelevant/light docs | **J** already decided at ingest | keep |
| `TOPIC_MIN_P`=0.5 | classify/base.py:36 | topic tag cut-off | **J** calibrated | keep |

### 2.4 Presentation / safety rules (keep, but note)
| Rule | file:line | Note |
|---|---|---|
| Vorsicht/caution: `TRUSTED_KINDS`, `SOCIAL_PLATFORMS`, `CORROBORATION_DAYS`=3, `CAUTION_WINDOW_DAYS`=21, `NOT_NEWS` | mcp_server/caution.py:28-32 | safety feature for a live presenter; Jev already gives `sensitive_p`; the trust rules are source plumbing → keep in code |
| `RECENT_INJURY_DAYS`=60, `STATUS_INJURY_DAYS`=365, `WATCH_MIN_WORDS`=4 | mcp_server/tools.py:174-175, 231 | display windows; keep |
| person status reconciliation (`GAME_LOOKBACK_DAYS`=200, `SINCE_MAX`=3) | person_status.py:33-36 | code, keep |
| `MAX_TOOL_CHARS`=12k, `SECTION_MAX`=12, `MORE_HINT` | server.py:108-118; present.py:355-366 | replace cut with paging (`offset`/`next`) so nothing is unreachable |
| `HINWEIS` (old long rules text, present.py:27-34) | present.py:27 | only shown when it differs; dead weight, could go |
| `EVENT_ALIASES` | mcp_server/tools.py:35 | data, keep |

### 2.5 Language rules
- MCP output always German; Perplexity translates (server.py:32-33). Fine; keep.
- MCP has no question normalisation (interpreter is chat-prose-only, engine.py:1298-1313). English MCP queries
  rely on the vectors (multilingual) and the `simple` FTS config; German stemming misses English words.
  Option: Perplexity is asked (tool description) to send the query in German **plus** key terms in the original
  language; cheaper than a server-side LLM call.
- Quote translation (`translate_quotes`, engine.py:2075) is paid per `frage`; with an evidence-pack tool the
  translation of titles stays as is (cached lookups), and Perplexity translates passages itself.

(Correction to 2.4: `CAUTION_WINDOW_DAYS` = 21 is documented in caution.py:6; the constant name in code may differ.)

## 3. How Jev is used for ranking

### 3.1 Today (classify/jev.py:250-279 `JevReranker.score`, retrieval/docs.py:226-264)
- One request per chunk of **20 candidates** (`JEV_CHUNK`, docs.py:50), 8 requests in parallel (`JEV_WORKERS`).
- `state` = one string: `"Kandidat 0: <text[:800]>\n\nKandidat 1: …"` (all 20 in one state).
- 20 `score` questions `c0..c19`, each `"Wie gut passt Kandidat {i} zur Frage: {query}"` with 4 levels:
  `"0: kein Bezug"`, `"1: entfernter Bezug"`, `"2: passt teilweise"`, `"3: beantwortet die Frage direkt"`.
- Mapping: `score_answer` (jev.py:213-221) uses `probabilities` if it is a **list** of 4 (expected level), else the
  returned `score`; divided by 3 → 0..1. The API returns `probabilities` as a **dict keyed "0".."3"** (TypeSafe
  docs, primitives/score), so the code always takes the `score` field. That field is already the
  probability-weighted mean level, i.e. **already continuous**; the result is fine, the list branch is dead code.
- What Jev reads per candidate:
  - MCP `search`: `jev_text` = `source_name + title` + `passage(text+image_description, terms, ~800)` (docs.py:260-264).
    **No date, no persons, no source kind.**
  - chat/agent: `f"{d.meta}\n{passage(d.text, terms)}"` where `meta` = label with date · Art · "über: names"
    (engine.py:284, 829). passage is 800 chars and the reranker cuts at 800 again (jev.py:272), so the **last
    ~100-150 chars of the passage are silently cut** for chat (meta is not subtracted, unlike `jev_text`).
  - So the "one path" still feeds Jev two different texts for MCP and chat.
- Threshold: MCP keeps score ≥ 0.2 (= expected level ≥ 0.6); chat keeps score > 0 and the top 15.
- Ties: score, then "matched by words/vector/label", then newest (docs.py:305-312). With continuous scores ties are
  rare; fine.
- Fallback when Jev is off/over budget: word overlap (`FakeReranker`, search.py:53-66).
- Quote support check uses the same pattern: all pairs in one state "Paar i", one noul per pair (engine.py:1118-1143).

### 3.2 What the TypeSafe API allows (docs.typesafe.ai, read 2026-10-25)
- Question types: `noul` (probability yes), `choice` (probabilities per option), `score` (2-10 ordered levels,
  continuous `score`, `probabilities` keyed by level, `confidence`, `legend`), `box` (images).
- **Score is continuous already**; up to 10 levels; levels may be strings or objects with examples. Docs: numeric
  labels like "0", "1" perform worse than descriptive ones (our levels start with "0: …").
- **No system prompt / custom instruction field** besides per-question `instructions` and `criteria`; context goes
  into `state`, which may be a **JSON object** (recommended: named fields, e.g. `{query, passage:{title, text,
  source_type}}`). Instructions and criteria also accept JSON structure ("Advanced: structure").
- Limits: 32 MiB body; no stated limit on questions or state length; 429 on rate limit.
- Known weaknesses (jev-1.13 jaggedness page): accuracy drops with **long state full of unrelated content**;
  **indirection** costs accuracy (identify things by name); weak at dates/ordering and numbers; leans to the first
  choice option.
- TypeSafe's own **re-ranking** and **RAG passage** cookbooks use **one state per query-candidate pair** (JSON
  state with `query` + one candidate), run in parallel (12 workers), and rank by a **noul** probability
  (`is_cited_source`; RAG: `is_relevant` ≥ 0.45, `contains_answer_evidence` ≥ 0.55, plus contradiction/injection
  nouls). Reported gain on CLERC: top-1 5% → 18%, top-10 38% → 62%, ~$0.065 for 1,200 pairs.

### 3.3 Consequences / options (all cheap: Jev costs $0.042 per M input tokens)
1. **One state per candidate** (JSON: `{frage, dokument:{quelle, art, datum, über, titel, text}}`) instead of 20 in
   one 16k-char state addressed by "Kandidat i". Matches the vendor pattern and the known weakness (long state,
   indirection). Token cost about the same (each document is still sent once; the question is repeated per pair,
   ~30 tokens). More HTTP requests: 120 pairs at 12-16 in parallel ≈ same latency as today's 6×20.
2. **Ask a noul for usefulness** ("Enthält dieses Dokument Information, die die Frage direkt beantwortet oder
   belegt?") in addition to or instead of the 4-level score: calibrated probability → threshold has a stable
   meaning (replaces the arbitrary 0.2). Keep `score` as the sort key or sort by the noul.
3. Add **a recency/currentness noul only when the question asks for "now"**: better left to code (Jev weak on dates)
   → put the date in the state for context, and let Perplexity pass `since`.
4. Descriptive level texts without the "0:"/"1:" prefixes; optionally 5 levels with examples.
5. Same reader text for MCP and chat (one `jev_text` with date, kind, persons), and stop the double cut at 800.
6. Speculative fan-out: in the same per-candidate request also ask `vorsicht`-like or "is it about the asked
   person (not a namesake)" nouls; costs nothing extra in latency.

## 4. Embeddings today

| Aspect | Today | file:line |
|---|---|---|
| Model | Gemini `gemini-embedding-001`, `output_dimensionality` = 384 (Matryoshka cut, re-normalised); stored model name `gemini-embedding-001-384` | embed/providers.py:63-110; config.py:68-70, 174 |
| Other providers | `fake` (hashed bag of words, default in code/tests), `local` (`intfloat/multilingual-e5-small`, with `passage:`/`query:` prefixes) | embed/providers.py:26-61, 113-119 |
| Task types | `RETRIEVAL_DOCUMENT` for documents, `RETRIEVAL_QUERY` for queries; Gemini's optional document `title` field is not used | embed/providers.py:102-106 |
| Text embedded | `source_name + title + text + image_description`, joined with spaces, **no** date, persons, team, platform, source type or Jev labels; cut at **8,000 chars** in our code (the model itself reads only the first ~2k tokens, so long documents are effectively their beginning; verify against Google's current limit) | ingest/store.py:108-124 (`embed_item`); cli.py:815-835 (`reembed`); providers.py:89 |
| Granularity | **one vector per document**, no chunks | models.py:342-357 (`embeddings`: item_id, model, vector(384), unique (item_id, model), HNSW cosine) |
| When | at ingest when the item is new or changed (store.py:173-175, after classification); again after image/reel analysis adds `image_description` (media/worker.py:69-87); in add loads (adds/load.py:199); `klugscheisser reembed` fills **missing** vectors for `level == "full"` items, 20 per Gemini request (cli.py:815-835) — it never refreshes changed text | as listed |
| Switch | `INGEST_EMBEDDINGS` (default on), forced on while any vector switch is on (`embeddings_on`, config.py:361-368) | |
| Query side | `vector_rows`: cosine distance, filters through `_filtered`, `SET LOCAL hnsw.iterative_scan = strict_order` so filters still return k rows; k = 40; query vector computed in parallel with Postgres | retrieval/search.py:172-202; retrieval/docs.py:139-182 |
| Column size | `Vector(EMBEDDING_DIM)` = 384 fixed in the schema; a bigger dimension needs a migration and a full re-embed | db/models.py:342-347 |
| Data (copy DB `klugscheisser_loop`) | 19,559 items, 10,002 full items, all 10,002 have a Gemini vector (164 old fake vectors remain). Text length (title+text+image): median 291, p90 2,342, p99 7,143 chars; 1,424 items with text > 3,000 chars. Long ones are `profile` (avg 2,265, 1,791 items, Wikipedia/college bios) and `web` (avg 1,828, 3,782 items). | read-only query |

What this means for search quality (seen in cmp-paid-v21/compare.md):
- Open questions get filled by **whole Wikipedia/college bios** (#21 Rücktritt → 5 Wikipedia pages; #25 College
  NCAA → 8 profiles; #29 goalie award → profiles): a bio mentions every topic somewhere, the single vector is
  the bio's "gist", and the 800-char `passage` window decides what Jev sees. Chunking fixes the cause instead of
  tuning `passage`, `coverage`, `RANK_NORM`, `RESERVED`.
- Short items (Instagram, results; median 291 chars) need no chunking; one chunk = the item.

Chunking redesign base (proposal):
- New table `chunks(id, item_id, ord, text, start_char)` + `chunk_embeddings(chunk_id, model, vector)`; items ≤ ~1,200
  chars = one chunk; longer ones split on paragraphs/sentences into ~600-900 chars with ~100 overlap; prepend a
  one-line header to the embedded text (title · source · date · persons) — the header is what lets a vector know
  "this paragraph is about Lara Stalder in 2026".
- Full text per chunk too (`tsvector` on chunk text) or keep item-level FTS and pick the best chunk by vector.
- Jev reads the best 1-2 chunks of a document (by vector distance / FTS rank) instead of `passage()`.
- Cost: ~1,424 long items × ~4 chunks + ~8.6k short = ~14k chunk vectors; Gemini embedding cost is negligible
  (< $1); a re-embed is a CLI run, reversible (old `embeddings` table stays).

## 5. Target design for the MCP path

### 5.1 Idea in one picture
```
question (Perplexity: query + optional person/team/event/since/thema)
  └─ scope: resolve names in code (repo.resolve_*), no regex routing
  └─ candidates (one function for chat and MCP): persons' docs, team reports, full text, vectors (chunks),
     Jev labels only if Perplexity passes `thema`; computed documents for the scope
     (staff table, last games, tournament sheet sections, facts/status, stats rows) as ordinary candidates
  └─ Jev scores every candidate, one JSON state per candidate (query + doc with source/date/persons/text)
  └─ evidence pack: top N passages + computed docs, each with date, source link, verbatim text
        ├─ web chat: Claude Sonnet writes prose, Jev checks quotes (as today)
        └─ MCP: returned as is; Perplexity writes
```
The only difference between chat and MCP is the last box, which is what the owner asked for.

### 5.2 Tool set (16 → about 9)
| Keep / new | Replaces | Contract (what Perplexity sees) |
|---|---|---|
| `suche` (new name or reuse `frage`) | `frage`, `search`, `whats_new`, `get_athlete_posts` | "Returns the stored evidence for a question: up to N documents/tables ranked by relevance, each with date, source link and the verbatim passage. You write the answer; cite links." Params: `frage` (full question, any language), `person[]`, `team`, `event`, `since`, `quelle` (enum: instagram, news, resultat, profil, alle), `thema` (enum: verletzung, transfer, karriereende, comeback, meilenstein, sponsor, privat, studium …; optional), `sortierung` (relevanz \| neueste), `limit`, `seite` (paging). `sortierung=neueste` + person = today's `whats_new`. |
| `get_athlete` | – | portrait; add `teil` (section) param + paging instead of 12k cut |
| `stats` | `frage` stats branch | unchanged (it is already structured; Perplexity chooses params well) |
| `get_team` (rename of `get_team_roster`) | `frage` staff branch, `get_ig_handles(team)` | roster + staff + handles, `competition` |
| `spiele` (rename/extend `list_events`) | `frage` team_games / event_games, `get_event` games part | `team`, `opponent`, `event`, `last`, `since`, `days` |
| `get_event` | `frage` tournament branch | add `teil` = uebersicht \| modus \| gruppen \| spielplan \| kader \| geschichte |
| `verbindungen` (optional) | `frage` connections branch | `team_a`, `team_b`, `game?` (structured, one call) |
| `refresh`/`refresh_status`, `hinzufuegen*`, `status` | – | unchanged (operational) |
| `get_ig_handles` | – | can fold into `get_athlete`/`get_team`; keep until the instructions are updated |

Tool contract rules: one paragraph per description with **when to use** and **what comes back**; no lists of
trigger phrases; enums instead of free strings where the server would otherwise guess; paging instead of a
silent cut; output stays German Markdown text, one representation (keep the 2026-10-17 decision), each item one
block: `datum · quelle · art · über` line, verbatim passage, link. Add the Jev relevance as a coarse word
(direkt/teilweise) so Perplexity can weigh hits, not a raw number.

Server instructions shrink to scope + citation rules + "suche for anything open; structured tools for numbers,
rosters, games, tournament sheet".

### 5.3 Migration in small reversible steps
Each step behind a switch (env/project setting), default off until its check passes; old path stays callable.

| # | Step | Switch | Free check | Paid check |
|---|---|---|---|---|
| 0 | Baseline: freeze the 33-query set + gap-loop questions; store the v2.1 run as reference | – | `scripts/search_compare.py` (free mode: word-overlap reranker) | done: cmp-paid-v21 (~$0.05) |
| 1 | **Jev reader text unified** (`jev_text` with date, kind, persons for both paths; no double 800 cut) | `JEV_READER_V2` | unit tests on text shape; free compare shows same pools | search_compare `--yes` (~$0.05) + chat-eval on loop copy (~$0.70-1) |
| 2 | **One JSON state per candidate** (+ optional usefulness noul); keep chunk mode as fallback | `JEV_PAIRWISE` | latency on loop instance (`scripts/loop_instance.sh`); tests with a fake Jev client | search_compare `--yes` A/B old vs pairwise (~$0.05-0.10); judge by hand as before |
| 3 | **Threshold from noul** (e.g. usefulness ≥ 0.5) instead of score ≥ 0.2; same for chat top-15 cut by drop-off | `RERANK_NOUL_MIN` | count of kept hits per query (free from the stored Jev answers of step 2) | re-judge only changed queries |
| 4 | **Labels only on request**: drop `INTENTS`/`QUESTION_TOPIC_CUES` regexes from candidate building; add `thema` enum param | `RETRIEVAL_INTENT_REGEX=false` | free compare: candidate pools with/without | search_compare #18-#33 (injury/transfer/retirement/sponsor/wedding/comeback) |
| 5 | **Chunks**: table + embeddings (migration adds tables only), `reembed --chunks`; vector candidates from chunks; Jev reads the best chunk | `RETRIEVAL_CHUNKS` | Alembic up/down on test DB; pool check on #21/#25/#29 (profiles) | Gemini re-embed (< $1, ask Ondrej) + search_compare + chat-eval |
| 6 | **Evidence-pack tool** (`suche`): the chat's candidate+scope+fact/status/computed docs, without the writer; output format above | new tool, `frage` untouched | render tests; size per call on loop instance | gap loop stand-in A/B: instructions v2 with `frage` vs instructions v3 with `suche` (`scripts/mcp_standin.py run --loop`, 12 seed questions, ≤ $1.20 per run) |
| 7 | **Routing out of `frage`**: computed docs (staff, last games, tournament sections, connections) become candidates of `suche`; `frage` regex shortcuts stay only behind a switch | `FRAGE_RULES` | unit tests for computed docs per scope | gap-loop stand-in on staff/tournament/game questions (p01-p06, s01-s06 + add 4-6 such questions) |
| 8 | **Tool consolidation**: rename/merge (`get_team`, `spiele`, `get_event(teil)`), paging; deprecate `search`, `whats_new`, `get_athlete_posts` as aliases for one release | aliases | MCP list_tools snapshot test | gap loop full run; then update the Perplexity connector instructions (Ondrej pastes v3) |
| 9 | Chat side: interpreter templates → same computed-doc candidates (cross.py rules retire one family at a time) | per family | chat-eval group by group (free part: fixture diff) | `chat-eval --yes` per family (~$0.70 each) |

Order rationale: steps 1-3 change only how Jev reads (cheap, measurable with the 33 queries); 4-5 change what is
found; 6-8 change the contract with Perplexity (needs the gap loop and new connector text); 9 is the large
chat-only rule block and goes last.

### 5.4 What not to remove
- Quote verification in the web chat (it is the product's promise there).
- Vorsicht/caution and person-status reconciliation (safety for a live presenter; code logic, not judgment).
- Structured `stats` (Perplexity uses its parameters well; a model would only add cost).
- Cost caps, the 12k cap as a last guard (but with paging before it).

## 6. Side findings (bugs / inconsistencies)
1. `frage(web=…)` advertised "default true" but has no effect unless `ANSWER_WEB_SEARCH` is on (server.py:470-473;
   engine.py:1451).
2. Chat Jev text is `meta + passage(800)` and then cut at 800 again in `JevReranker` (engine.py:829; jev.py:272):
   the end of the relevant window is lost; MCP's `jev_text` handles this, chat does not.
3. `score_answer` list branch is dead for the current API (probabilities come as a dict) (jev.py:213-221);
   harmless, the `score` field is used.
4. MCP `frage` never runs the interpreter or the agent (both prose-only, engine.py:1298-1313, 1372); the MCP
   path is the less capable twin of the chat path, not the same path.
5. A changed document is re-embedded at ingest (store.py:173-175), but if that call fails (quota) the old
   vector stays and `reembed` (cli.py:815-835) only fills *missing* vectors, so the stale one is never fixed;
   a text hash on `embeddings` would catch it.
6. `refresh` is annotated read-only though it starts paid jobs (server.py:420-431); deliberate, but worth a note in
   the description ("starts paid updates").
7. Query #16 in compare (team=USA, source_type=official_result, since 2026-08-01) returns 0 on both paths: data
   gap or filter (USA results may be linked to the team but stored under another source_type) — check with
   `scripts/gap_have.py --team USA`.
