# Handoff: search work, 2026-10-25 evening

Read this first, then `docs/orchestrator.md` (task log + "Search: ideas to test later") in the main checkout
`~/projects/klugscheisser_athlete`. Work in orchestrator mode: talk to Ondrej, sub-agents build.
Karl is for **Fabi, a Swiss TV sports presenter / journalist** (never "fan"); she uses it mainly via Perplexity.

## Approval from Ondrej (2026-10-25)
"Go ahead … merge what you think is good, you have my permission." Steps below in this order. Merge only with CI
green and the chat check OK; go live only with a backup first (`docs/server-handoff.md`). Keep paid test runs small
(about $5 in total for steps 1–4, plus about $0.85 for the live `reembed-chunks`).

## What is done
- Merged: PR #35 (ARCHITECTURE.md, `/arch-view` skill, arch-check), PR #36 (fast tests ~1 min, own test DB per
  worktree, GitHub CI). Live runs 9aa5717.
- Search lab finished ($4.33), results in `lab-results.md` (this folder):
  - **Winner, now the branch default:** passage vectors (gemini-embedding-2, table `item_chunks`), Jev card text +
    reader context in MCP search. nDCG@8 0.71 → 0.77, same speed and cost.
  - **Not adopted:** one document per Jev request, 240 candidates, Jev yes/no cut-off.
  - **Measured gap:** a person named only in the query text → her own posts found 45% instead of 83%.
- Timing/cost analysis of live calls: `timing.md`. Findings:
  - `frage` takes 82% of the time;
  - the 3 s refresh wait never helped;
  - no cache needed;
  - bot page views start paid refreshes;
  - the media worker hit its rate limit 6,000 times.
- Background reading: `research.md` (MCP, rerankers, embeddings), `audit.md` (16 tools, hard-coded rules).

## Where the code is
- `~/projects/klugscheisser_athlete-searchjev`, branch `exp/search-jev-path` (local, not pushed): v2.1 shared path +
  lab winner + MCP cards/`dokument` (OFF) + no-wait MCP tools (ON). Tests: 874 passed; 1 known failure
  (`test_v7_projects::test_registry_and_engine_follow_the_project`, fails on main too).
- `~/projects/klugscheisser_athlete-fixes`, branch `fix/paid-refresh-and-backoff` (local): only a WIP
  `src/klugscheisser/calllog.py` so far.
- Lab DB `klugscheisser_searchlab` (copy, has passage vectors). Never write to the live DBs or `klugscheisser_loop`.

## Next steps
1. **PR 1** from `exp/search-jev-path`:
   1. Rebase onto origin/main.
   2. Chat check: chat-eval main vs branch on the same copy DB (`chat_eval_copy.sh`, `compare_eval.py`; ≤ $2.20).
      The first attempt failed with "No module named 'anthropic'": run `uv sync --all-extras` in the main-code
      checkout first (`~/projects/klugscheisser_athlete-evalmain`, detached origin/main, if it exists).
   3. Full tests, push, PR (draft body in `pr-draft.md`), CI green, merge.
   4. Deploy: backup → `git pull` → migrate → restart → `klugscheisser reembed-chunks` (~$0.85, prints cost
      first) → smoke test MCP `search`.
2. **PR 2** from `fix/paid-refresh-and-backoff`:
   - bot/headless page views start no paid refresh;
   - media worker exponential backoff;
   - `ensure_profiles` off the request path;
   - one MCP call-log line per tool call (`calllog.py` started).

   Avoid the files PR 1 changes (retrieval/, mcp_server/tools.py, answer/engine.py).
3. **Name recognition in MCP search:** a name in the query text becomes the person filter or extra candidates
   (reuse the ingest name matcher). Measure with `scripts/search_compare.py` and the lab labels.
4. **Phase F:**
   - ~6 read tools: `suche`, `person`, `stats`, `team`, `events`, `dokument`;
   - maintenance tools hidden from Perplexity;
   - cards ON, no Sonnet writing for Perplexity;
   - new Perplexity instruction, with the emoji legend removed from `docs/client-instructions.md` line 57.

   Build next to the old tools behind a switch, test with the gap-loop stand-in. Ondrej pastes the instruction into
   Perplexity and runs the probe questions (in `lab-results.md`) before the switch goes on live.

## Rules to keep
- No secrets printed.
- Ask before deleting or spending beyond the above.
- Tags stay for the web app; whether MCP search still needs them is decided from numbers later.
- Sonnet at intake is fine (credits).
