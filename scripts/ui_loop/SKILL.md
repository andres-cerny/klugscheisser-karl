---
name: ui-loop
description: Klugscheisser Karl web app improvement loop. Runs N rounds (default 5) in which sub-agents improve the web UI on a private preview copy (own branch, preview DB, no paid calls), checked by a browser bug probe, and ends with a private preview link for Ondrej's review. Use when Ondrej says /ui-loop, "UI-Loop", or asks to let agents polish the Klugscheisser web app.
argument-hint: "[N rounds, default 5]"
---

# /ui-loop [N]

You are the **orchestrator** (see `docs/orchestrator.md` in `~/projects/klugscheisser_athlete`): you plan, spawn
sub-agents, check results and talk to Ondrej. Sub-agents do the building. Full reference: `docs/ui-loop.md`.
N = the argument (default 5).

## Hard limits (the only fixed rules)

- **Nothing live**: never touch `~/klugscheisser`, the live databases or the live service. Only the preview
  (`klugscheisser_preview`, port 8010) and the loop's own worktree/branch.
- **No paid runs** of any kind (no `--yes`, no chat-eval, no API keys passed to anything).
- **UI area**: web templates/CSS/JS, `src/klugscheisser/web/` routes and views, display/formatting, new helper
  modules. Never the chat answer path, `config.py`, `answer/`, `mcp_server/`, `ingest/`, `llm/`, `classify/`,
  `facts/`, `media/`. If a change must touch code also used by MCP or answers (e.g. search in `retrieval/`):
  first add a line to the task log in `docs/orchestrator.md`, keep MCP behaviour unchanged, prove it with tests.
  Prefer a new web-only helper module.
- **Sources stay visible**: every statement keeps its source link/chip. UI texts in German.
- **Tests green**: the loop's own test DB, e.g.
  `TEST_DATABASE_URL=postgresql+psycopg:///klugscheisser_test_uiloop uv run pytest -n 4` (~1 min; created from the
  template automatically, docs/testing.md); relevant files first. No waiting on other sessions. When the loop ends:
  `for d in $(psql -d postgres -Atc "select datname from pg_database where datname ~ '^klugscheisser_test_uiloop(_gw[0-9]+)?(_v7)?$'"); do dropdb "$d"; done`.
- **No merge**: push the branch; a PR only after Ondrej's OK.
- Other Claude sessions may be working in the repo: work only in the loop's worktree, never in the main folder.

Everything else is judgment. The sub-agents decide what is most worth fixing or improving, including bigger
UX, navigation or look changes; there is no quota of fixes per round.

## Steps

1. **Prepare** (you, small steps):
   - Read `docs/orchestrator.md`; add a task-log row (`/ui-loop`, branch, "running").
   - `cd ~/projects/klugscheisser_athlete && git fetch`; `STAMP=$(date +%Y%m%d-%H%M)`;
     `git worktree add .claude/worktrees/ui-loop-$STAMP -b feat/ui-loop-$STAMP origin/main`. Call it `WT`.
   - If `$WT/scripts/ui_loop/preview.py` is missing, the tooling PR is not merged yet: stop and tell Ondrej.
   - In `WT`: `python3 scripts/ui_loop/preview.py db-refresh --max-age-hours 24 --worktree $WT`, then
     `python3 scripts/ui_loop/preview.py start $WT` (local only, no tunnel yet). If the template DB is missing,
     give Ondrej `sudo bash ~/setup/preview-db-vector.sh`.
   - Baseline: `uv run --no-project --with playwright==1.63.0 python scripts/ui_loop/check_pages.py --out
     $WT/data/ui-loop-runs/$STAMP/round-0 --seed 0` (~3 min). Keep `summary.txt` and the screenshots.

2. **Rounds 1..N** — one sub-agent per round (in the background, one at a time, all in `WT`). Give it the
   hard limits above, the path of the previous round's `report.json` and `shots/`, the list of what earlier
   rounds already changed, and this brief:
   - Look at the screenshots yourself (phone and desktop) and use the app's real pages; the checker's problem
     list is a **tool to catch bugs, not a score to optimize**. Don't chase its number, and don't treat a rise
     caused by a false positive as a failure (add a justified rule to `scripts/ui_loop/ignore.json` instead).
   - Decide by your own judgment what makes the biggest difference now. Ondrej's focus areas:
     (a) **easy to understand**: what is where; "Suche" (search) clearly separate from "Chat" — navigation,
     labels, page headings, empty states, hints; (b) **forgiving search on `/suche`**: typos, partial names,
     accents/umlauts, other languages (English/French team or country names, e.g. "Switzerland", "Suisse"),
     and a "Meinten Sie …?" suggestion instead of an empty result; plus real bugs (overlaps, overflow, broken
     things, too small tap targets) and the general look.
   - Step back and look for **root causes** of whole classes of problems (one layout rule causing overlaps on
     many pages, one component used everywhere). Fixes must be **general** (shared CSS/layout/components, the
     search helper), never tailored to one page, one person or one test query.
   - Add new probes so we don't overfit: new search cases in `scripts/ui_loop/search_cases.json` (other typos,
     languages), more pages if something is not covered.
   - Run the relevant pytest files (web, search; respect the test DB rule), then commit
     (`"Ondrej Cerny" <118163742+andres-cerny@users.noreply.github.com>`, message ends with the Co-Authored-By
     line from the session's attribution reminder). Report: what changed and why, files, tests, open questions.
   After the sub-agent: `python3 scripts/ui_loop/preview.py restart $WT`, then the checker with
   `--out .../round-<n> --seed <n>` (new seed = other persons/search probes each round). Compare with the
   previous round: read both summaries, look at a few before/after screenshots. If tests fail or the UI clearly
   got worse, revert that round's commits (`git revert`) and note why. Stop early when there is nothing worth
   improving left.

3. **Finish**:
   - `python3 scripts/ui_loop/preview.py tunnel` → private link `https://….trycloudflare.com/p/eishockey/`
     (login with the normal web password).
   - Short plain-language report for Ondrej: rounds, problem count per round (as a bug probe), what was fixed
     and improved, what was reverted and why, before/after screenshot paths (`round-0` vs last round), open
     decisions. Update the task-log row ("review").
   - `git push -u origin feat/ui-loop-$STAMP` (no PR yet).
   - Send Ondrej a push notification with the preview link ("UI-Loop fertig, bitte ansehen: <link>").
   - On his OK: open the PR (body ends with the Claude Code line from the attribution reminder). Never merge.
   - When he is done looking: `python3 scripts/ui_loop/preview.py stop`. Don't leave the tunnel running for days.
