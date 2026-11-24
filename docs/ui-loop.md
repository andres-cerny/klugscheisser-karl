# UI loop (`/ui-loop [N]`)

A repeatable way to make the web app better without touching live: a private copy of the app (the
**preview**) runs on this server, sub-agents improve the UI in a few rounds on their own branch, a browser
checker catches objective bugs after every round, and at the end Ondrej gets a private link to look at
everything before anything is merged.

The command itself is the user-level skill `~/.claude/skills/ui-loop/SKILL.md` (found from every session,
also the Remote Control one in `~/projects`). Its source is `scripts/ui_loop/SKILL.md` in this repo; after
changing it copy it over: `cp scripts/ui_loop/SKILL.md ~/.claude/skills/ui-loop/SKILL.md`.

## Pieces

| File | What |
|---|---|
| `scripts/ui_loop/preview.py` | Preview instance: database copy, start/stop, public link (standard library, plain `python3`) |
| `scripts/ui_loop/check_pages.py` | Browser checker (Playwright): pages × phone/desktop, problems, screenshots, search probes |
| `scripts/ui_loop/search_cases.json` | Fixed search probes (English/French country names, no umlauts, typos); add more freely |
| `scripts/ui_loop/ignore.json` | Known false positives: `{"type", "page" (regex), "viewport", "match" (regex on the detail), "why"}` |
| `scripts/ui_loop/SKILL.md` | The `/ui-loop` procedure (copy of the user skill) |
| `scripts/ui_loop/tests_busy.sh` | "busy"/"free": is another session running pytest on the same test DB (name as argument) |

## Preview instance

```bash
python3 scripts/ui_loop/preview.py db-refresh [--max-age-hours 24]   # klugscheisser_preview from the newest dump
python3 scripts/ui_loop/preview.py start <worktree> [--tunnel]       # http://127.0.0.1:8010/p/eishockey/
python3 scripts/ui_loop/preview.py restart [<worktree>]              # after code changes (tunnel keeps running)
python3 scripts/ui_loop/preview.py tunnel                            # https://….trycloudflare.com/p/eishockey/
python3 scripts/ui_loop/preview.py status
python3 scripts/ui_loop/preview.py stop                              # app + tunnel
```

- **Database**: `db-refresh` drops and recreates only `klugscheisser_preview` (guarded in code), copied from
  the template `klugscheisser_preview_template` (has the `vector` extension; made once with
  `sudo bash ~/setup/preview-db-vector.sh`), then `pg_restore --no-owner` of the newest
  `~/klugscheisser/backups/eishockey-*.dump` (read only) and the migrations of the worktree. `start` runs the
  worktree's migrations again (a branch may add one; preview DB only).
- **Process**: transient systemd user units `klug-preview` (app) and `klug-preview-tunnel` (cloudflared quick
  tunnel), not the live unit. Logs and state: `~/.local/state/klug-preview/` (`app.log`, `tunnel.log`,
  `app.env` mode 600).
- **Why it is safe** (verified with `klugscheisser features`: every service "off"):
  - the app gets an allow-list environment (systemd `EnvironmentFile`), runs in a worktree without `.env`
    (refused otherwise) and never in `~/klugscheisser`: no API key reaches it, so Apify, Jev, Gemini, Claude,
    X, Brave, Firecrawl are off; `MEDIA_PROVIDER=off`, `CLAUDE_CLI_PATH` points nowhere, `LLM_PROVIDER=fake`,
    `WEB_ANSWER_ENGINE=fake` (the chat answers in "Testmodus"), all cost caps 0;
  - free collectors off too: `AUTO_REFRESH`/`WEB_AUTO_REFRESH=false` (no update on page view),
    `PROFILES_ENABLED`, `SIHF/IIHF/PWHL/SDHL_*_ENABLED`, `EP_ENABLED=false`, `INSTAGRAM_SOURCE=fixture`;
  - its own projects file lists only `eishockey` → `klugscheisser_preview` with `scheduled_jobs: false`, so the
    nightly refresh, news poller and nightly `pg_dump` never run (the weekly briefing suggestion would need a
    Gemini key and fails harmlessly). What still runs: the job recovery at start (preview DB only), and on a
    person page the photo / pronunciation lookups on Wikimedia/Wikidata (free, stored in the preview DB).
  - `/mcp` gets a random bearer token and the tunnel hostname is not an allowed MCP host: MCP is closed.
  - The web password is the live one (read from `~/klugscheisser/.env` without printing); the session secret
    is new on every start.

## Page checker

```bash
uv run --no-project --with playwright==1.63.0 python scripts/ui_loop/check_pages.py \
    --out data/ui-loop-runs/<stamp>/round-<n> --seed <n>
```

Logs in, visits home, `/suche` (hit and no hit), `/quellen`, `/brief`, `/bewertungen`, `/projekt`, five person
pages (three tier-1, one with little data, one with a long name), the posts fragment, two teams, two events and
the newest `/hinzufuegen/{id}`, a person with no stored item, the chat tab of a person, a missing person and an
unknown address (both expected 404), and the login page in a logged-out browser — at 390×844 (phone) and 1440×900 (desktop). Probe persons and generated search
probes come from the preview DB and change with `--seed`. Read only: the browser aborts every POST except the
login and never opens chat, refresh, "Alles neu laden", fun-fact generation or the TTS audio.

Problem types: `http_status`, `load_error`, `console_error`, `failed_request`, `slow_page` (> 3 s), `h_overflow`,
`overlap` (text lines / controls of different elements covering each other, clipped by scroll containers,
closed `<details>` ignored), `text_clipped` (overflow hidden without ellipsis/line clamp), `tap_target`
(phone, < 32 px, inline links in running text exempt, a "stretched link" counts with the box its absolute
`::after`/`::before` covers), `broken_image`, `html_entity` (raw `&nbsp;` etc. visible as text), `scroll_hint` (a sideways scroll box hides content but shows no fade/hint), `broken_link` (same-origin GET),
`search_miss` (the expected person/team is not among the top 5 results).

Output: `report.json` (problems, ignored ones, probes, per page status/load time/screenshot), `summary.txt`,
`shots/<page>-<viewport>.png` (full page). `data/` is gitignored.

The number is a **probe for objective bugs, not the goal**: a round is judged by whether the UI got better
(screenshots, clarity, usability), not by the count alone.

Baseline on main (2026-10-17, seed 0): 82 problems = tap_target 73, search_miss 7 (English/French country
names, "Lausane"), slow_page 2 (`/event/oly-2026` ~4.8 s). Typos, partial and umlaut-less person names were
already found.

## The loop (summary of the skill)

1. Orchestrator: task-log line, worktree + branch `feat/ui-loop-<YYYYMMDD-HHMM>` from `origin/main`,
   `db-refresh --max-age-hours 24`, `start <worktree>` (local only), baseline check.
2. N rounds (default 5), one sub-agent per round with full freedom inside the hard limits: it decides what is
   most worth fixing or improving (bugs, navigation, look, bigger UX changes), looks at the screenshots itself,
   searches for root causes of whole classes of problems, makes general fixes (shared CSS/components, the search
   helper; never tailored to one page or one probe), runs the relevant tests, commits. Then restart, new check
   with a new seed. A round that breaks tests or makes the UI worse is reverted.
3. Focus areas from Ondrej (2026-10-17): make the app easy to understand (what is where; "Suche" clearly
   separate from "Chat": navigation, labels, headings, empty states, hints) and a forgiving `/suche` (typos,
   partial names, accents/umlauts, English/French team or country names, "Meinten Sie …?" instead of nothing).
4. Hard limits: UI area only (`web/` templates/CSS/JS/routes, display/formatting, new helper modules); code also
   used by MCP/answer only with a task-log line first and unchanged MCP behaviour (tests); never chat,
   `config.py`, `answer/`, `mcp_server/`, `ingest/`, `llm/`; every statement keeps its visible source; German UI
   texts; tests green; no paid runs; nothing live; no merge.
5. End: tunnel, short plain-language report with before/after screenshots, push the branch, push notification
   with the link; PR only after Ondrej's OK; `preview stop` when he is done.
