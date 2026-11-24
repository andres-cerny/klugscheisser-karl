# One way to find documents for chat and MCP search + search lab winner

## What this does
Until now Karl had two different ways to find stored documents:
- the **web chat** (and `frage`) let Jev read all documents of the persons asked about;
- the **MCP `search` tool** (used by Perplexity) mixed a vector search with a word search and let Jev look at
  only the top 24. Jev labels (injury, transfer …) were put in front of the hits unranked, so a question about
  injuries could show 8 unrelated injury reports (a Finnish wrestler, Haaland) before anything relevant.

Now there is **one path** (`retrieval/docs.py`) for chat, the answer agent and MCP `search`:
1. **Collect candidates** from Postgres, each source only *finds*:
   - the persons'/team's newest documents (when a person or team is asked about);
   - Jev label lists (injury, transfer, …) and topic lists;
   - full text: words OR-ed, long words also as word starts ("Kreuzband" finds "Kreuzbandriss"), ranked so
     that long biographies no longer win just by being long;
   - the ~40 nearest vectors (meaning and other languages: "Kreuzbandriss" ≈ "ACL", "goalie" ≈ "goaltender").
2. **Cut to the cap** (MCP 120, chat 600): the best 15 full-text and the best 15 vector hits always stay, the rest
   by how many question words are in the part of the document Jev reads.
3. **Jev scores every candidate** and decides; MCP keeps hits with score ≥ 0.2.

On top, the **search lab winner** (`docs/search-lab/lab-results.md`), defaults on:
- **passage vectors**: long documents are split into passages, embedded with gemini-embedding-2 (768 dims) with a
  small "source | date | persons" header (new table `item_chunks`, `klugscheisser reembed-chunks`); the best passage
  is what Jev reads;
- **Jev reads like a journalist** in MCP search: card line (title, source, date + age, persons) plus one sentence on
  the reader (Fabi, Sportmoderatorin: citable facts with date and source).

Also in this PR:
- MCP `search` **evidence cards** + `dokument(id)` for the full stored text (switch `MCP_SEARCH_CARDS`, **off**: it
  changes what Perplexity sees, goes on with new connector instructions);
- MCP read tools **no longer wait** for the daily person update (`MCP_REFRESH_NO_WAIT`, on): the lab timing showed the
  3 s wait never helped; the answer says an update is running.

Also fixed on the way: the vector index (HNSW) only looked at the 40 nearest vectors of *all* documents and the
filter then threw most away (team USA: 2 of 40 left). It now keeps scanning until it has 40 matching ones. This
also helps the old path.

## Results (MCP `search`, 33 queries: 18 real gap-loop calls, 12 free-text, 3 person/team; real Jev, judged by hand)
| Version | new better | old better | same | cost of the run |
|---|---|---|---|---|
| v1 (no vectors at all) | 4 | 11 | 10 (of 25) | $0.05 |
| v2 (vectors + labels as candidates) | 14 | 5 | 14 | $0.05 |
| v2.1 (top hits kept over the cap, USA/SUI/ACL as search words, vector call in parallel, index fix) | see lab: nDCG@8 0.71 | | | ~$0.05 |
| lab winner (this PR's default: passage vectors + Jev card text + reader context) | 15 of 32 better, 4 worse (nDCG@8 0.71 → 0.77) | | | ~$0.0015 per search |

Details: `docs/search-lab/lab-results.md` (33 labelled queries, ~9,000 graded pairs).

Chat check (2026-10-25): `chat-eval` (65 questions, Claude Sonnet) main (e40ab95) vs this branch, each on its own
copy of `klugscheisser_searchlab` (has the passage vectors), `docs/search-lab/chat_eval_copy.sh`:

| | passed | cost | median |
|---|---|---|---|
| main | 61/65 | $1.00 | 6.0 s |
| this branch | 61/65 | $0.95 | 5.9 s |

- Better on the branch: F1 (Lara Stalder news: now leads with the Euro Hockey Tour call-up of 13.10.), J2 (Enzler and
  Stalder in the same line: now the SIHF goals they set up for each other, main only "both play at EVZ").
- "Worse" on the branch: F4 (Shannon Miller quote) and J4 (wing or center). Both were run-to-run noise of the
  sentence check: re-run of F4/J3/J4 on both, the branch passed all three (F4 now with the Radio Central +
  bluewin.ch quote), main failed F4 this time.
- Read by hand (31 answers differ in wording): branch answers are mostly the same or richer: F3 (injured Swiss
  players: Manetsch foot injury, Sigrist concussion, Lutz open; main had only Lutz/Stalder), S4 (Northeastern record:
  254 points all-time leader, main only the 2023 168-point milestone), S8/S10/S11 more sources (S10: thestar.com
  "baby in December" instead of only a baby-shower post). Slightly weaker: S6 (Militär) lost the second source
  (Instagram), H3 ("Hat Alina Müller einen Freund?") now shows "nichts gefunden" (allowed by the fixture,
  `art: antwort|nichts`) instead of main's "the documents don't say" plus her posts about friends (same on re-run).
  Neither is wrong.
- Total paid for the check incl. re-runs: $2.16.

Speed: one MCP search sends 6 Jev requests of 20 documents in parallel (old: 1 request of 24); the Gemini
query vector is now computed while Postgres runs. v2 median 870 ms vs old 458 ms; v2.1 687 ms; with the lab winner
643 ms end to end (lab DB, 33 queries).
Cost per MCP search: about $0.001 Jev (old ~$0.0002) plus a Gemini query vector (< $0.00001).

## Switches (all reversible)
| Setting | Default | Meaning |
|---|---|---|
| `RETRIEVAL_VECTOR_CANDIDATES` | true | the ~40 nearest vectors join the candidates; false = full text + labels only |
| `MCP_SEARCH_EMBEDDINGS` | false | true = the old MCP search exactly (vectors + full text, RRF, Jev on top 24, labels first) |
| `SEARCH_JEV_MAX_DOCS` | 120 | candidates Jev scores per MCP search |
| `INGEST_EMBEDDINGS` | true | vectors for new documents at ingest (always on while a vector switch is on) |
| `RETRIEVAL_EMBEDDINGS` | false | unchanged: the old chat way (newest 150 + vector hits) |

| `RETRIEVAL_CHUNKS` | true | vector candidates from passage vectors (`item_chunks`, gemini-embedding-2 @768); falls back to the document vectors while the table is empty |
| `JEV_CARD_TEXT`, `JEV_READER_CONTEXT` | true | MCP search: Jev reads a card line (title, source, date + age, persons) and the reader context (Sportmoderatorin) |
| `JEV_PAIRWISE` | false | lab arm "one document per Jev request" (not better, 2-3x slower) |
| `MCP_SEARCH_CARDS` | false | MCP `search` answers with evidence cards and offers `dokument(id)`; switch on only together with new Perplexity instructions |
| `MCP_REFRESH_NO_WAIT` | true | MCP read tools answer at once from stored data, the person update runs in the background (false = wait `AUTO_REFRESH_WAIT_S` as before) |

## Deploy
One migration: `b20c0ffee020_item_chunks` (new table `item_chunks` only; downgrade drops it). Order:
1. backup (`uv run klugscheisser project backup`);
2. `git pull && uv sync --all-extras && uv run klugscheisser project migrate`;
3. `uv run klugscheisser reembed-chunks --dry-run` (free, shows the size), then `uv run klugscheisser reembed-chunks`
   (~$0.85 for ~14,000 passages) **before** the restart: once one passage vector exists, search uses the passage
   vectors, so the backfill should be complete when the new code starts;
4. restart; smoke test MCP `search` (e.g. "Kreuzbandriss").
New documents get their passage vectors at ingest (gemini-embedding-2, a fraction of a cent per day).

## Good to know
- The new injury trigger words (Kreuzband, Meniskus, Gehirnerschütterung, concussion, ACL/LCA, Fraktur) also
  feed the **old** label shortcut when `MCP_SEARCH_EMBEDDINGS=true`, and chat's injury topic.
- Search words now include 3-letter all-caps words (USA, SUI, ACL); this also changes which part of a long
  document Jev reads in chat.
- Chat: when more than 600 candidates come together, word/vector/label matches are now kept first (before:
  the newest ones, so full-text hits could be cut off).
- Fact extraction at ingest no longer depends on an embedder being configured (`IngestContext.skip_facts`).

## Follow-ups (later PRs)
- Give Jev a little reader context per candidate (source, date, persons) when scoring.
- Remove duplicates (same text from several sources, e.g. the same Instagram post twice) before Jev.
- Query #2 ("Frauen-WM November Verlegung IIHF PWHL Kalender"): still weak on both paths.
- `answer/cross.py` group questions with `RETRIEVAL_EMBEDDINGS=true` use a raw vector SQL that has the same
  HNSW filter issue (off by default).

## Tests
`TEST_DATABASE_URL=postgresql+psycopg:///klugscheisser_test_searchjev uv run pytest -n 4`: 898 passed, 7 skipped, 0 failed (rebased onto origin/main 45676a6, incl. PR #37). `ruff check`
clean. New: `tests/test_search_jev_path.py`, `tests/test_search_lab.py`. Also fixed: `test_v7_projects::
test_registry_and_engine_follow_the_project` failed with a socket database URL (`?host=` at the end); it now compares
database names. Comparison harness: `scripts/search_compare.py` (free, or `--yes --max-usd` for real Jev).

Architecture impact: chat, the answer agent and MCP `search` share `retrieval/docs.py` (candidates from full text,
vectors, Jev labels and the persons' documents; Jev scores all); the old MCP RRF ranking stays behind
`MCP_SEARCH_EMBEDDINGS`; new table `item_chunks` (passage vectors) written at ingest and by `reembed-chunks`;
MCP cards + `dokument` (off) and no-wait read tools (on); ARCHITECTURE.md updated (knowledge/answer/mcp, switches,
diagram edges).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01BLJNsDVMzYe85Cts78uFw5
