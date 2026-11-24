# MCP gap loop: how Karl does through its MCP server

Fabi uses Karl through Perplexity's connector "Klugscheisser Karl". This tooling shows what Karl gives a
client like Perplexity, and whether missing answers are a data gap (not stored) or a tool/answer gap (stored
but not delivered). A Claude agent reads the outputs and traces failures back through
sources → ingest → DB → tools → answer. There is no numeric score on purpose.

## 1. Answer logging (server)

Every MCP tool call now logs a second line after `tool <name> done in …`:
`tool <name> answer: <first 600 chars of what the client received, newlines shown as ⏎>`.
Read it with `journalctl -u klugscheisser -o cat | grep "answer:"` (after the next deploy).

## 2. Stand-in client (paid: Anthropic API)

Claude Sonnet (`claude-sonnet-5-5`) plays Perplexity: it gets the live Perplexity instructions
(`src/klugscheisser/gaploop/instructions_eishockey.txt`, copy of "Eishockey final" in
`~/klugscheisser-instructions-draft.md`) plus the server's own MCP instructions, connects to the live MCP
server (`http://127.0.0.1:8000/mcp/eishockey`, token `MCP_AUTH_TOKEN` read from `~/klugscheisser/.env`, never
printed) and answers each question. No web search: only Karl's tools. Tools that write or start paid jobs
(`hinzufuegen*`, `refresh*`) are never offered.

```
with-secrets klugscheisser -- uv run python scripts/mcp_standin.py run --only p05 s01 --max-cost 0.10 --label smoke
```

Options: `--questions FILE`, `--only ID …`, `--limit N`, `--project eishockey|allgemein`, `--instructions FILE`,
`--effort low|medium|high`, `--max-cost USD` (hard cap per run, default 0.50; checked before every API call with
a worst-case estimate, so the run stops before going over), `--out DIR`, `--label`.

Output in `data/gap-runs/<time>-<label>/` (git-ignored):
- `run.json`: everything (per question: tool calls with args, full raw tool outputs, final answer, duration,
  tokens, cost, stop reason; per run: Karl-side cost = `cost_ledger` delta during the run, read-only, may
  include background jobs).
- `report.md`: one section per question: tool calls + args, tool outputs trimmed to 1,500 chars, the answer,
  time and cost.

Cost: about $0.06–0.10 per question measured on 2026-10-17, when `frage`, `list_events` and `search` each sent
~14–16k chars (Markdown plus the same data as JSON). Since `feat/frage-compact` every tool sends one compact
Markdown text (typically 1–6k chars, `get_event`/`get_athlete` ~12–14k), so runs should be cheaper. A full run of
the 12 seed questions needs at most `--max-cost 1.20`.

Free helpers:
```
uv run python scripts/mcp_standin.py compare data/gap-runs/A data/gap-runs/B --out compare.md   # side by side
uv run python scripts/mcp_standin.py report data/gap-runs/A                                      # re-render report.md
```

## 3. What Karl has stored (free, read-only)

```
uv run python scripts/gap_have.py --team Schweiz --person "Alina Müller" --since 14d [--json]
```

Reads the live project database (`DATABASE_URL` from the env or `~/klugscheisser/.env`; `--database-url` for
another one) in a read-only connection (`default_transaction_read_only=on` + `SET TRANSACTION READ ONLY`,
SELECTs only, rolled back). Shows: newest item per source type, newest finished Swiss game and hours since;
per team: latest/next games, tournaments, rosters, current members, news in the window; per person:
affiliations, handles, injury facts, recent facts, items in the window, event entries, season stats.

## 4. Seed questions

`tests/fixtures/mcp_gap_questions.yaml`: `id`, `question`, `origin`, optional `notes`. `p01`–`p06` are the real
`frage` calls Perplexity sent on 2026-10-16 (its reformulation of Fabi's questions); `s01`–`s06` are synthetic
presenter questions about current data.

## 5. Loop instance: a branch of Karl on a copy database (never live)

Fixes are tested on a copy, not on live. `scripts/loop_instance.sh` runs Karl from any worktree/branch on
**http://127.0.0.1:8020** (MCP: `/mcp/eishockey`, localhost only) against the database **`klugscheisser_loop`**,
a copy of the newest nightly backup (`~/klugscheisser/backups/eishockey-*.dump`; the live DB is never read).

```
scripts/loop_instance.sh reset                       # klugscheisser_loop new from the newest backup (instance stopped)
scripts/loop_instance.sh start ~/projects/<worktree> # default: the checkout the script is in; migrates the copy
scripts/loop_instance.sh status
scripts/loop_instance.sh stop
scripts/loop_instance.sh exec <worktree> -- uv run klugscheisser …   # one CLI command against the copy
```

Then the stand-in against the instance (paid, as in section 2): `with-secrets klugscheisser -- uv run python
scripts/mcp_standin.py run --loop …` (`--loop` = `--url http://127.0.0.1:8020/mcp/eishockey` and Karl's cost from
the copy's `cost_ledger`; `--url` / `--database-url` for anything else; without them it stays live). The lookup:
`uv run python scripts/gap_have.py --database-url "postgresql+psycopg:///klugscheisser_loop?host=/var/run/postgresql" …`.

What the instance gets: the same answer path as live (Anthropic, Gemini embeddings, Jev, the live `MCP_AUTH_TOKEN`,
read from the environment or `~/klugscheisser/.env`, never printed; written to `~/.local/state/klug-loop/app.env`,
mode 600). Its own projects file with only `eishockey` → `klugscheisser_loop` and `scheduled_jobs: false` (no
nightly run, news poller, Elite Prospects or nightly `pg_dump`; no `allgemein` project, so the live
`klugscheisser_allgemein` is never opened). Always off: media analysis, Instagram/X team accounts. Karl's model
spend is capped at $2/day in the copy's ledger (`LOOP_LLM_CAP_USD=…` to change). `reset` also drops the app
settings copied from live (news interval, caps) so they cannot switch anything back on.

Off unless asked for at `start`: `--auto-refresh` (refresh asked persons before answering) and `--collectors`
(passes the Apify / X / Brave keys; without it they are not passed at all, their caps are 0 and Instagram reads nothing instead of the test fixtures). Free collectors
(RSS, SIHF, IIHF, Wikipedia …) only run when a tool or `exec` command asks for them; they only read the web.

Notes: the copy is made from the template `klugscheisser_preview_template` (has `vector`; made once with
`sudo bash ~/setup/preview-db-vector.sh`, shared with the UI preview). The worktree must not contain a `.env`.
Log: `<worktree>/data/loop-instance.log`. The process is a transient user unit `klug-loop` (`systemctl --user
status klug-loop`); it does not survive a reboot.
