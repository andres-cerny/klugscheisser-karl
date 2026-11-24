# How we work on Klugscheisser: orchestrator + sub-agents

Ondrej talks to one Claude session, the **orchestrator**. The orchestrator does not build things itself:
it plans, hands work to **sub-agents**, checks their results, and reports back. This keeps the
conversation free, so Ondrej can talk to the orchestrator at any time while work runs in the background.

## Roles

**Ondrej** decides what to build, answers questions, approves anything live, paid, or destructive.

**Orchestrator** (the session Ondrej talks to)
- Discusses ideas and turns them into small, clear tasks.
- Spawns one sub-agent per task (in the background), several in parallel when tasks don't touch the same files.
- Reviews what comes back (diff, test results), and explains it to Ondrej in plain language.
- Keeps the task log below up to date.
- Only does tiny things itself: reading a file to answer a question, editing this file.

**Sub-agents** do the actual work: read code, write code, run tests, write build plans.

## Flow of one change

1. **Talk**: Ondrej describes what he wants. The orchestrator asks only what it really needs to know.
2. **Plan**: for bigger changes, a sub-agent writes `docs/build-plan-vNN-<topic>.md` (like the earlier
   ones). Ondrej reads/approves it.
3. **Build**: a sub-agent works on a feature branch `feat/<topic>` in `~/projects/klugscheisser_athlete`
   (in its own git worktree when other sub-agents are busy at the same time), writes tests, runs them.
4. **Check**: the orchestrator reviews the result. For any chat/answer change, a sub-agent runs
   `uv run klugscheisser chat-eval --yes` (~$0.05) and reports what got better or worse.
5. **PR**: the sub-agent pushes the branch and opens a PR. Ondrej merges (or says "merge it").
6. **Live**: only after Ondrej says so. Back up first (`uv run klugscheisser project backup`), then update
   `~/klugscheisser` as described in `docs/server-handoff.md` ("As built").

## Rules every sub-agent gets

- Work only on its own task and its own branch; never push to `main` directly.
- Tests: each worktree/agent uses its own test database, no waiting on others:
  `TEST_DATABASE_URL=postgresql+psycopg:///klugscheisser_test_<topic> uv run pytest -n 4` (~1 min; created from
  the template automatically, see docs/testing.md). Run the touched test files first. When done, drop it:
  `for d in $(psql -d postgres -Atc "select datname from pg_database where datname ~ '^klugscheisser_test_<topic>(_gw[0-9]+)?(_v7)?$'"); do dropdb "$d"; done`.
  A folder busy with another session: wait, no workarounds.
- Never touch the live install `~/klugscheisser` or the live databases unless the task says so and Ondrej approved it.
- No paid runs (`--yes` flags, API-heavy scripts) beyond the cheap chat-eval, unless Ondrej approved them.
- Secrets only via `with-secrets`; never print them.
- Finish with a short report: what changed, files, test results, open questions, anything Ondrej must decide.

## Reports to Ondrej

Short and plain: what was done, does it work (tests / chat-eval numbers), what he needs to decide.
When a long task finishes while Ondrej is likely away, he gets a push notification.

## Task log

Newest first. Status: planned / running / review / PR open / merged / live.

| Date | Task | Branch / PR | Status |
|------|------|-------------|--------|
| 2026-11-03 | Official WCh rosters of the last five teams as hand-checked seeds: SUI (SIHF 01.11., 25; Aymon, Gaberell, Surdez cut), USA (31.10., 25), DEN (29.10., 24), FIN (31.10., 25, list via Jatkoaika: leijonat.fi bot check), CZE (01.11., 25); all 10 teams official. Tests use `as_provisional` for kaderwatch cases | fix/wch-final-rosters | PR open |
| 2026-11-01 | Media managers / press officers of the 10 WCh teams as staff with email + phone (migration c21c0ffee021, seeds/staff/*, web 'Medienkontakt', MCP staff/get_event); raw findings `~/klugscheisser-wch-media-contacts.json`; Japan: only JIHF office, not stored | feat/staff-media-contacts | PR open |
| 2026-10-24 | Architecture view: ARCHITECTURE.md + CLAUDE.md rule + page generator `tools/arch-view/build.py` (draft artifact for review; skill comes after Ondrej OKs the page) | feat/architecture (worktree `-arch`, uncommitted) | review |
| 2026-10-19 | MCP gap loop round 1 (local `loop/base` = main + #19 #21 #20 #23, copy DB, $1.35): national-team games, output size, tournament routing fixed; new gaps: frage cost uncapped, staff missing, other teams thin, player game lines/stats stale, noisy "Zu beachten" | – | paused, Ondrej decides |
| 2026-10-19 | Seed keeps results links (`seed base` no longer wipes competition/end date) | PR #25 | PR open |
| 2026-10-19 | Compact MCP output (one markdown text, ~4x smaller) + tournament routing | PR #23 | PR open |
| 2026-10-18 | National-team results: Leijonat (EHT) + sihf.ch team pages | PR #20 | PR open |
| 2026-10-18 | Sport check: women's football no longer stored as hockey + `sport-audit` | PR #21 | PR open |
| 2026-10-17 | Loop instance on copy DB `klugscheisser_loop`, port 8020 | PR #19 | PR open |
| 2026-10-18 | WCh 2026 event + rosters + watcher | PR #22 | merged (not live) |
| 2026-10-17 | /ui-loop run 1, round 3: web 404 page via a last GET fallback route in `web_routes` (also mounted in the MCP app; `/mcp*`, `/.well-known/*`, `/healthz` keep the plain Starlette 404, tests prove it) | feat/ui-loop-20261017-1606 | live (99d7cb5) |
| 2026-10-17 | MCP gap-loop harness (stand-in client, answer logging, read-only lookup, seed questions; no score) | PR #18 | ready for review |
| 2026-10-17 | UI loop tooling: `/ui-loop` command, Playwright page checker, preview instance (DB `klugscheisser_preview`, port 8010, trycloudflare link). UI area only (scripts/, web/), no shared files | PR #16 | PR open |
| 2026-10-17 | /ui-loop run 1 (up to 12 rounds, based on PR #16 branch): clear navigation, Suche vs Chat, forgiving search, no bugs | feat/ui-loop-20261017-1606 | live (PR #24 99d7cb5, fix PR #26 dfd3c87) |
| 2026-10-17 | WCh 2026 as complete event (schedule, groups, format, history) + provisional rosters all WCh teams + roster-announcement watcher | feat/wch-2026 | running |
| 2026-10-17 | Switch Gemini text/vision LLM calls to Claude Sonnet (embeddings, TTS, video stay Gemini). chat-eval 64/65 (1 outdated question), ~11x cost, ~2x time | PR #17 | live (af90924) |
| 2026-10-16 | Instagram team account: Swiss women's national team only (like X team accounts) | PR #15 | live (664e577) |
| 2026-10-11 | Nightly refresh (option b: tier 1 daily, tier 2 weekly, 05:00) + one-off tier 1+2 on 15.10. 01:00 + MCP speed fix | PR #14 | live (51c5bbc) |
| 2026-10-11 | Rename Curdin → Karl | PR #13 | live |
| 2026-10-11 | MCP tools wait max 3 s (`AUTO_REFRESH_WAIT_S=3` in live .env) | – (config only) | live |
| 2026-10-11 | Perplexity connector "Klugscheisser Karl" + short instructions (drafts in `~/klugscheisser-instructions-draft.md`) | – | live |
| 2026-10-11 | Diagnose connector 502 (old trycloudflare URL in claude.ai) | – | done |
| 2026-10-11 | Set up this orchestrator workflow | – | done |

## MCP gap loop (agreed with Ondrej 2026-10-17)

- Goal: Karl gets, stores and delivers the right information **through the MCP** (Fabi uses Perplexity; the web chat matters little).
  Main focus: Swiss women's team + the other teams at the Women's WCh in Denmark (6–16 Nov 2026): recent results/stats,
  rosters (provisional + official announcement), injuries, coach/staff changes, pre-tournament and tournament games.
- Each round: a Sonnet stand-in (plays Perplexity, only Karl's tools, no web) asks a fresh mix of questions (real Perplexity
  questions from the log + new current ones); an agent judges better/worse (content, freshness, speed) — no rigid score —
  and traces failures through the whole pipeline (source → refresh → DB → tool → output). Fixes must be general, one PR each.
- Tests run on a copy DB (`klugscheisser_loop`, from the latest backup) + a branch instance on its own port; never live.
- Budget: $5 total. chat-eval (~$0.70 with Sonnet) only once at the end as a safety net. PRs only; Ondrej decides live.
- End: push notification + summary; 5 manual Perplexity questions for Ondrej to check.

### Loop status / handoff (2026-10-17 evening)
- Live: Sonnet (PR #17, af90924). Caps $5/day, agent $0.04.
- PR #18 `feat/mcp-gap-harness`: loop tools (stand-in `scripts/mcp_standin.py`, lookup `scripts/gap_have.py`, answer logging,
  `docs/mcp-gap-loop.md`). Stand-in costs ~$0.08/question.
- `feat/wch-2026` (worktree `~/projects/klugscheisser_athlete-wch`, commit c4a6f16): WCh 2026 event + provisional rosters
  of all WCh teams + roster watcher. Was running final pytest; then push + PR. Check if PR exists; if not, finish it.
- Ask Ondrej: merge #18 + WCh PR into main (not live) as loop base. Then start the loop (spec above).
- Gaps already found (start here): (1) no finished Swiss national-team game stored at all (EHT Lahti only as news;
  friendly vs Austria in Bern missing) → national-team results ingest; (2) MCP `frage` output 14–16k chars (markdown + same
  data as JSON) → compact output; (3) `frage` misreads tournament questions (answered "why November" with roster birth dates);
  (4) Swiss EHT Södertälje 28–31 Oct: games/times/opponents missing.
- Ondrej's 5 manual Perplexity questions are pending (he'll send results; use them to adjust the loop's questions).
- Merging is blocked for Claude (auto-mode safety check). Loop base therefore = local branch `loop/base`
  (origin/main + feat/mcp-gap-harness + feat/wch-2026 merged locally, not pushed to main). Loop fix branches start
  from `loop/base`; their PRs list the dependency. Ondrej merges #18, the WCh PR and loop PRs himself.

### Loop status (2026-10-19, after round 1)
- Merged to main (not live): #18, #22. Open: #19, #20, #21, #23, #25 (merge #25 with/after #20).
- Round 1 run: `~/projects/klugscheisser_athlete-loop/data/gap-runs/20261019-1321-round1/`. Budget used ~$1.42 of $5.
- Paused after round 1 on Ondrej's wish; he decides whether to continue.

### Loop status (2026-10-21, after round 3 — paused for Ondrej)
- Rounds: 1 ($1.35), 2 ($0.90), 3 ($0.51). Round 3 vs 1: Karl cost $0.57→$0.03, median 18.8→9.0 s, tool text/question 20k→5.9k chars, calls 3.8→2.1.
- Open PRs (merge order): #19 #21 #20 #25 #23 #27 #28 #29 #30 #31 #32. Conflict resolutions in local `loop/base3` (~/projects/klugscheisser_athlete-loop, c9a5910).
- Remaining gaps: list_events(team) lacks upcoming tournament games; English questions answered in German; frage routing takes one narrow path; stale EP stats (Ondrej decides); other teams' friendlies; per-game player lines; small labels (provisional shown ✅).
- Perplexity instruction v2: ~/klugscheisser-instructions-draft-v2.md (Ondrej pastes it when PRs go live).

### 2026-10-24: PRs #19–#32 merged to main (8fd2f3c), full suite green (825 passed). NOT live yet:
go-live blocked by the auto-mode safety check (production deploy) — waiting for Ondrej.

### 2026-10-24 evening: live 9aa5717
- PRs #19–#33 merged and live (go-live scripts: `~/klugscheisser-golive.sh`, `~/klugscheisser-update.sh`; Ondrej starts deploys,
  the auto-mode safety check blocks Claude-initiated production deploys unless Ondrej says "go live" in that turn).
- Chat-eval after #32: 61/65 ($0.91); E2 fixed in #33. Real Perplexity checks: answers good; Karl adds sources/freshness,
  plain Perplexity adds narrative colour → next loop idea: notable facts per game/meeting.
- German answers are fine (user is Fabi). Perplexity instruction drafts: v2, v3 (v3 = one combined call for multi-person questions).
