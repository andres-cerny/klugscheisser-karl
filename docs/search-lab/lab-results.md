# Search lab: results (2026-10-25)

Branch `exp/search-jev-path` (worktree `~/projects/klugscheisser_athlete-searchjev`), 7 local commits on top of
0664a91, not pushed. Lab database: `klugscheisser_searchlab` (copy of `klugscheisser_loop`, kept). Live databases and
`~/klugscheisser` untouched. Paid spend: **$4.33** of the $5 limit.

## In plain words (for Ondrej)

**What we measured.** For the 33 test searches, Claude Sonnet graded about 9,000 (search, document) pairs from 0 to 3,
from the point of view of Fabi as a TV sports presenter: "could she cite this for this question?". With these grades
we can score any search setup without guessing.

**What we found.**
1. **The ranking (Jev) was the weak spot, not the candidate list.** Today's search puts on average 0.71 of the
   ideal top 8 on screen (score "nDCG@8"); a perfect judge working on the same 120 candidates would reach 0.94.
2. **Better "meaning search" helps.** Splitting long documents into passages and using Google's newer embedding model
   (gemini-embedding-2), with a small header per passage ("source | date | persons"), finds clearly more of the good
   documents: 63% instead of 54% among the 40 nearest; one-off cost $0.85 for all 10,000 documents.
3. **Jev judges better when it sees what a journalist sees**: title, source, date ("vor 3 Tagen"), who the document
   is about, and one sentence on who the reader is (Sportmoderatorin, facts with date and source). Together with
   point 2: **0.71 → 0.77**, 15 of 32 searches better, 4 worse; fewer useless documents in the top 8 (26 → 23).
   Same cost per search (~$0.0015) and not slower end to end (0.64 s median vs 0.69 s before).
4. TypeSafe's recommended "one document per request" did **not** help here (0.70-0.71) and was 2-3x slower. Not adopted.
5. Scoring 240 instead of 120 candidates brought nothing (0.764 vs 0.770) and doubled Jev's time. Stay at 120.
6. **Perplexity now gets what Jev read.** Today a hit shows only its title/first sentence; with the new "cards" it
   shows the passage that matched (e.g. "Kreuzbandriss" now shows the paragraph of Poulin's Wikipedia page about her
   ACL surgery, not the start of her bio), plain words instead of emojis, exactly one link per card, and
   `dokument(id)` for the full stored text (important because Instagram/X/paywalls need a login). ~7-8k characters
   for 8 cards.
7. **Names matter.** When a search names a player only in the text (not in the athlete field), recall of her own
   relevant posts drops from 83% to 45% (98.8% of own posts don't contain the surname). Putting names into the
   full-text index did not fix it; the persons header in the new vectors helps a bit overall (68% → 73%). So Karl
   should recognise names in the question and filter by the person (cheap code, no model).
8. MCP read tools no longer wait for the daily person update: they answer at once from stored data and say an
   update is running.

**What we'd ship, in this order**
1. Jev card text + reader context + passage vectors (already default on in the branch; needs the one-off
   `klugscheisser reembed-chunks` on live, ~$0.85, plus the migration; without the backfill the old vectors are used).
2. No-wait person update for MCP tools (default on in the branch).
3. Evidence cards + `dokument(id)` (in the branch, switch `MCP_SEARCH_CARDS`, default off): switch on together with
   updated Perplexity instructions (the old emoji legend in `docs/client-instructions.md` line 57 must go), then run
   the probe questions below.
4. Name recognition in `search` (person in question text → athlete filter): next step, not built yet.
5. Later: tool consolidation (draft below); tags: on hold, unchanged from v2.1 (Ondrej wants to discuss).

Architecture impact: new table `item_chunks` (migration b20c0ffee020) + passage vectors at ingest and in MCP search
candidates; Jev reads card text with reader context in MCP search; MCP `search` cards and `dokument` tool behind a
switch; MCP tools no longer wait for the person update. ARCHITECTURE.md updated.

## Technical details

### Phase A: measuring stick
- Pool: merged pool at cap 240 + old MCP top 24 + vector top 60 = 7,218 pairs, 3,478 docs; later +520 (new vector
  arms) and +1,242 (person queries) pairs: 8,980 pairs in `labels.json`.
- Labeller: Haiku 5.5 screen on all pairs, then Sonnet 5.5 (`claude-sonnet-5-5`, thinking off via `between_tools`,
  effort low, JSON schema output, Batches API) on all Haiku>=2 pairs + 15% of Haiku-1 + 3% of Haiku-0. Reader in the
  rubric: Sportmoderatorin/Journalistin, both teams, bios relevant for background questions (no "fan" framing was
  ever used). Screen check: 1/130 Haiku-0 and 18% of Haiku-1 pairs were relevant for Sonnet -> ~270 relevant docs
  are probably missing among unsampled Haiku-1 pairs; recall numbers are a bit optimistic, evenly for all arms
  ("soft" recall with weight 0.18 in metrics_b.json shows the same ordering).
- Spot check of 32 Sonnet labels by me: 28 agree, 4 corrected by one grade (corrections.json).
- 952 relevant docs (grade >=2) on the 33 queries; #8, #16, #17 have none.
- Per-source recall (metrics_a.md, Phase A pool): fulltext@40 0.30, vector@40 0.58, labels 0.08, newest@40 0.14,
  pool@40 0.50, pool@120 0.73, pool@240 0.92, old MCP top 24 0.43. Relevant docs only found by full text cluster in
  broad queries (#21, #24, #25, #27); labels add little given vectors (#19, #26, #28 are the exceptions).
- Oracle nDCG@8 (perfect ranker on the pool): 40 -> 0.86, 120 -> 0.94, 240 -> 0.99. Actual v2.1: 0.71.
  => bottleneck = ranking. More than 120 adds ceiling, but in practice (Phase C) no gain.

### Phase B: embeddings
| | e1 001@384 (now) | e2 001 + QA query | e3 emb-2@768 passages | e3qa (QA prefix) |
|---|---|---|---|---|
| vector recall@40 | 0.535 | 0.552 | 0.629 | 0.643 |
| vector recall@120 | 0.675 | 0.693 | 0.777 | 0.777 |
| merged pool@120 | 0.655 | 0.669 | 0.729 | 0.743 |
| oracle nDCG@8 pool120 | 0.926 | 0.935 | 0.958 | 0.961 |
- Model `gemini-embedding-2` (stable, 8,192-token input, no task_type: query prefix `task: search result | query:`,
  documents `title: … | text: …`; each text must be its own `Content`, else one aggregated vector; auto-normalised).
  $0.20/M tokens (batch $0.10); measured 3.56 chars/token -> 4.25M tokens -> $0.85.
- Passages: <=2,000 chars one passage, else ~1,800 chars with ~200 overlap on sentence ends: 14,019 passages,
  halfvec(768) + HNSW, 65 MB. Header `Quelle: X (kind) | Datum: YYYY-MM-DD | Personen: …`.
- QA query prefix (e3qa) is marginally better than "search result"; within noise, kept "search result" (setting
  `CHUNK_QUERY_TASK`).
- Latency: query embedding 0.27 -> 0.32 s (runs in parallel to Postgres), chunk SQL +7 ms.
- Excerpt test (100 relevant multi-passage docs; Sonnet grades only the excerpt, $0.35): still relevant
  best passage full 92 (43 grade 3) | best passage cut to 800: 84 (32) | keyword window 800: 84 (28) | doc start: 54
  (15). -> best passage by vector, keyword window only as fallback; no need for Jev to pick among passages.
- Whole-doc vectors: kept (fallback until the backfill, and for chat paths); e3 replaces them as vector candidates.

### Phase C: Jev (33 queries, cap 120, `phase_c.py`)
| arm | nDCG@8 | rel. in top 3 | grade-0 in top 8 | median ms (Jev) | Jev tokens |
|---|---|---|---|---|---|
| C0 today (001 vectors, 20 per request) | 0.709 | 26/32 | 26 | 258 | 1.14M |
| C0 + passage vectors | 0.743 | 27/32 | 33 | 250 | 1.13M |
| + card text (title/source/kind/date/persons) | 0.741 | 27/32 | 27 | 364 | 1.15M |
| **+ reader context (shipped)** | **0.770** | 27/32 | 23 | 374 | 1.19M |
| same at cap 240 | 0.764 | 28/32 | 20 | 616 | 2.13M |
| C1 pairwise JSON, no context | 0.700 | 26/32 | 40 | 871 | 2.51M |
| C2 pairwise + context | 0.714 | 27/32 | 33 | 858 | 3.10M |
| C2 + noul cut-off 0.5 | 0.592 | 25/32 | 15 | 858 | |
- Shipped code path re-run (`find_documents` with branch defaults): 0.766, 27/32, 23 junk, end-to-end median 643 ms
  (v2.1 stored run: 687 ms). The card-text latency (+0.1 s) is removed by preloading persons in two queries.
- Pairwise latency (8 queries): 120 docs 1.42 s @16 workers, 0.86 s @32, 0.70 s @48; 240 docs 2.67 / 1.55 /
  1.19 s. Chunk mode at 240: 0.45 s @12-16 workers vs 0.61 s @8. No 429s anywhere.
- Noul ("enthält Belege …") as cut-off: halves junk but drops good documents, nDCG falls at every threshold 0.3-0.6.
  Not adopted; `RERANK_MIN_SCORE` 0.2 stays.
- Biggest losses of the winner: #19 (ranks Poulin's bio whose passage is her ACL surgery; labelled 1, arguably a
  label miss), #26 Sponsor (reader context makes Jev stricter on sponsor posts), #12, #33.

### Phase D: Perplexity tool output (`MCP_SEARCH_CARDS`, default off)
Card format (plain words, no emojis; one link, always last):
```
**[3] Marie-Philip Poulin**
Typ: Profil/Biografie · Quelle: Wikipedia (de) · 2026-07-23 (vor 3 Monaten) · über: Marie-Philip Poulin
In den Playoffs gehörte sie … Am 25. Juni 2026 gab sie bekannt, das sie sich im Juli 2026 wegen eines Kreuzband
und Meniskussrisses einer Operation unterziehen muss. … […]
Volltext: dokument(2376)
Link: https://de.wikipedia.org/wiki/Marie-Philip_Poulin
```
Before (same hit, #19 "Kreuzbandriss"):
```
- 📰 (Medienbericht) 🎉 (Freude) Marie-Philip Poulin [Wikipedia (de), 23.07.](https://de.wikipedia.org/wiki/Marie-Philip_Poulin) (Resultat · über Marie-Philip Poulin)
  CQ (* 28. März 1991 in Beauceville, Québec) ist eine kanadische Eishockeyspielerin, … […]
  > «Sidney Crosby des Fraueneishockeys»
```
- Excerpt rule: cleaned text <= 800 chars -> full text on the card (most posts and result lines); longer -> the
  passage Jev judged (best passage of the chunk vectors, else keyword window), 700 chars, shrinking to 300 around its
  best sentence until the answer fits ~8k; then `Volltext: dokument(id)`. URLs inside texts are removed (7 cards had
  t.co links). Vorsicht reason as its own line.
- `dokument(id, teil=1)`: stored cleaned text + image description, 15k chars per part, "weiter mit teil=2".
- Sizes on the 33 queries (real ranking): old median 4.6k chars, cards median 7.2k (max 8.1k); through the local MCP
  server (lab DB, free mode): median 7.8k incl. Datenstand/rules lines, search 0.4 s, dokument 41 ms; 17 tools listed.
- Emoji legend: none in server INSTRUCTIONS or tool descriptions; **`docs/client-instructions.md` line 57 has the
  legend** (Perplexity side) and must be updated when cards go on.
- No-wait update (`MCP_REFRESH_NO_WAIT`, default on): tools that waited were `get_event`, `get_athlete`,
  `get_athlete_posts`, `whats_new`, `search` (via `tools.freshen`); `frage`, web and chat already did not wait.

Draft consolidated tool list (read tools for Perplexity; write/refresh hidden or on a separate endpoint):
| tool | replaces | description (draft) |
|---|---|---|
| `suche(frage, person?, team?, seit?, quelle?, anzahl?)` | `search`, `whats_new`, `get_athlete_posts`, open part of `frage` | "Stored evidence on Swiss/international women's ice hockey for any question, as cards (date, source, passage, link). You write the answer and cite the links. Empty: try another spelling or `person`." |
| `dokument(id, teil?)` | – | full stored text of one card |
| `person(name, teil?)` | `get_athlete`, `get_ig_handles` | portrait: club, stats summary, status/injuries, handles |
| `team(name, wettbewerb?)` | `get_team_roster`, staff part of `frage` | roster + staff + handles |
| `spiele(team?, gegner?, turnier?, letzte?, seit?)` | `list_events`, `get_event` games, game parts of `frage` | results and schedule |
| `stats(...)` | – | unchanged |
| `turnier(name, teil?)` | `get_event`, tournament part of `frage` | sheet by section (modus, gruppen, spielplan, kader, geschichte) |

Probe questions for Ondrej in Perplexity (with cards on; don't need to be run by Claude):
1. "Welche Spielerinnen haben sich 2026 das Kreuzband gerissen?" - does Perplexity cite several cards (one link
   each) and keep each claim next to its link?
2. "Fasse alles zusammen, was Karl über Lara Stalders Saisonstart weiss, mit Quellen." - multi-card citation; does it
   call `dokument(id)` for a long card?
3. "Lies den ganzen Wikipedia-Artikel über Marie-Philip Poulin aus Karl und nenne ihre Verletzungen." - forces
   `dokument(id)` and `teil=2`; is a 15k part read completely or cut?
4. "Gib mir 20 Treffer zu Rücktritt Nationalspielerin." - with `limit=20` the answer is ~15k+ chars: is the tail
   truncated, and does Perplexity notice (check the server log for size)?
5. "Was postet Alina Müller in letzter Zeit auf Instagram? Zitiere wörtlich." - own posts without her name in the
   text; Instagram links need a login: does it fall back to the card text / dokument?
6. "Welche Schweizerinnen spielen in der NCAA? Nur Karl, kein Web." - broad question: does it iterate (`suche` with
   other words) or stop after one call?
7. "What did the Swiss team do in Lahti?" (English) - language handling and whether cards in German are cited.
8. Ask the same question twice in one chat - does it re-call the tool or reuse; check latency in the log.

### Persons (name in text vs athlete field), 8 person queries
| arm | recall@120 relevant | own relevant posts | oracle nDCG@8 |
|---|---|---|---|
| athlete field, 001 vectors | 0.833 | 0.833 | 0.972 |
| athlete field, passage vectors | 0.838 | 0.817 | 0.978 |
| name in text, 001 vectors | 0.680 | 0.439 | 0.897 |
| name in text, 001 + persons in full text | 0.665 | 0.439 | 0.897 |
| name in text, passage vectors (persons header) | 0.726 | 0.450 | 0.922 |
| name in text, passage vectors + persons in full text | 0.711 | 0.450 | 0.922 |
- Only 19 relevant own posts in total (small sample). Persons in full text: lab column `items.persons_tsv`
  (production would need a migration adding the column/trigger or a weighted part of `search_tsv`) - no gain, so
  not proposed. The whole-document 001 vectors were not re-embedded with a persons header (cost); the passage
  header covers it. Conclusion: a name recogniser (question text → athlete filter) is still needed.

### Tags
On hold per Ondrej: tag candidates exactly as in v2.1 (INTENTS unchanged). Only the per-source recall above.

## Spend
| item | USD |
|---|---|
| Labels Haiku screen (3 batches) | 0.42 |
| Labels Sonnet (3 batches) + smoke test | 1.85 |
| Excerpt re-check (Sonnet) | 0.35 |
| gemini-embedding-2 re-embed (14,019 passages) | 0.85 |
| Jev runs (Phase C arms, sweeps, code check; output tokens counted at input price) | 0.86 |
| Gemini query vectors | <0.01 |
| **total** | **4.33** |

## Open questions
- Perplexity behaviour with cards (truncation size, citation of several cards, use of `dokument`) - probe list above.
- Chat answers still use the old Jev text (`answer/engine.rerank`); the same card text + reader context could help
  there, but needs `chat-eval --yes` (~$0.70) on a copy first.
- Live rollout: migration + `reembed-chunks` (~$0.85) on `klugscheisser` (Ondrej's decision; not done).
- Name recognition in `search`: build next (person names in the question → athlete filter / person candidates).
- Labels: ~270 likely-relevant docs among unsampled Haiku-1 pairs; a full Sonnet pass on them (~$1.3) would tighten
  absolute recall numbers but not change the arm ordering.
- Tags: discussion with Ondrej pending.

## Files
Lab folder: this folder (`labels.json`, `pool*.json`, `metrics_a.md`, `metrics_b.json`, `c_*.json`, `excerpt*.json`,
`cards_examples.json`, `persons_pool.json`, `spend.json`, scripts `*.py`).
