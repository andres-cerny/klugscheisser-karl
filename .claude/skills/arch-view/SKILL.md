---
name: arch-view
description: Create or update this repo's ARCHITECTURE.md (always from origin/main) and publish it as one interactive architecture page (Claude Artifact, same link every time). Use when the user says "/arch-view", "update the arch graph", "update the architecture view", "update the architecture page", "update the architecture diagram", "refresh the architecture map", or "show me the architecture".
---

# arch-view: architecture map from ARCHITECTURE.md

Turns the repo's `ARCHITECTURE.md` into a single-page interactive HTML map (mermaid diagram, clickable
components, flow highlighting, details panel) and publishes it as a Claude Artifact under a fixed link.
`build.py` (next to this file, Python standard library only, deterministic, no AI) does the rendering. Your job
is to keep `ARCHITECTURE.md` true to the code and to run the steps below.

Project-specific values (artifact URL, last mapped commit, how to find the live commit) live only in the
**State** section at the bottom. The steps themselves are generic.

## Steps

### a. Start from origin/main

1. `git fetch origin`. The architecture is always based on `origin/main`, never on the local checkout's
   working state (it may be on another branch or have uncommitted changes).
2. Read code through a temporary worktree (`git worktree add --detach <scratchpad>/arch-main origin/main`,
   remove it at the end with `git worktree remove`) or with `git show origin/main:<path>`.
3. If the State section names a live commit command, run it (read-only) and keep the short hash for `--live`.
   No live note: skip `--live`.

### b. ARCHITECTURE.md does not exist on origin/main: create it

Write it from scratch in the format below (build.py parses exactly this). For a large repo, split the reading
across sub-agents in parallel, one per component group (e.g. web/API, background jobs, data layer, outside
services); each returns its components' "How it works", "Stack & settings" and "In the code" with every number
checked in code. You assemble the diagram, tables and flows.

### c. ARCHITECTURE.md exists: incremental update

1. Take the last mapped commit from State (or from the "Last updated" line).
2. `git diff --stat <sha>..origin/main` and `git log --oneline <sha>..origin/main`; read the changed areas.
3. Update only what is affected: components, diagram nodes/edges, flows, settings tables, numbers, live notes
   ("(live: …)", "not yet live on <sha>": drop those when live has caught up).
4. Keep every `<!-- manual -->…<!-- /manual -->` block verbatim (do not edit, move into, or delete them).
5. Nothing structural changed: only bump the "Last updated" line.

### d. Changes go to main only through a PR

Commit on a branch `docs/arch-update-<YYYY-MM-DD>` made from `origin/main` (in the temporary worktree).
Ask the user before pushing and opening the PR, unless they already said to. Never push to main.

### e. Build

```bash
python3 .claude/skills/arch-view/build.py --ref origin/main [--live <sha>] \
  --title "<Project> Architecture" --out <scratchpad>/arch-view.html
```

- build.py reads `ARCHITECTURE.md` from `--ref` (`git show`). Before the PR is merged, build from the new
  branch instead (`--ref docs/arch-update-<date>`, or run it from that worktree, where a file not yet on the ref
  is used as a "draft"). `--arch <file>` forces a given file.
- Require **0 warnings** in the last output line. Each warning names a mismatch (table id not in diagram,
  diagram node without table row, flow step without a component id, missing component details, unknown
  dependency). Fix ARCHITECTURE.md, not build.py.
- If `node` exists: extract the inline `<script>` blocks and `node --check` them.
- Optional: a headless browser at 390 px width; `document.documentElement.scrollWidth <= 390` (no horizontal
  page scroll; the diagram scrolls inside its own box).

### f. Check, then publish

1. Scan the HTML for secret-looking strings (`grep -nEi 'sk-[a-z0-9]|api[_-]?key\s*[:=]|token\s*[:=]\s*\S{12}|bearer\s+[a-z0-9]|-----BEGIN|AKIA[0-9A-Z]{12}'`)
   and review every hit. Env var NAMES are fine, values never.
2. List all outside URLs (`grep -oE 'https?://[^"'"'"' )<]+' | sort -u`). Allowed: the pinned mermaid CDN
   (`cdn.jsdelivr.net/npm/mermaid@<exact version>`), Google Fonts (`fonts.googleapis.com`, `fonts.gstatic.com`),
   the repo's own GitHub/GitLab links, the SVG namespace `http://www.w3.org/2000/svg` (in the script), and
   public URLs that ARCHITECTURE.md itself mentions as text. Anything
   else: find out why before publishing.
3. Publish with the Artifact tool, passing `url` = the Artifact URL from State so the same link updates. If this
   conversation has not published or read it yet, first call the Artifact tool with `action: "read"` on that
   URL. No URL in State: publish new (icon "diagram") and write the new URL into State.

### g. Update State

Set Artifact URL, last mapped commit (`git rev-parse --short origin/main`) and date in the State section
below. Commit that together with the ARCHITECTURE.md change (same PR).

### h. Tell the user

The link, what changed in plain language (no file lists up front), and every open "(?)" item in
ARCHITECTURE.md (`grep -n '(?)' ARCHITECTURE.md`), so they can answer them.

## ARCHITECTURE.md format (what build.py parses)

Style: plain-language architecture document, mid-technical. Explain what a part does and why before naming
files (files go in "Key files" and "In the code"). Env var NAMES only, never secret values. Mark anything you
could not verify with "(?)". Verify every number (limits, prices, intervals, counts, model names, dimensions)
in the code; settings are written "default X (live: Y)" when the live value differs from the code default.

Sections, in this order (`##` headings; any other `##` section is shown as a note box):

1. `# Architecture`, then a line `Last updated: <YYYY-MM-DD> · commit <short sha> (origin/main)`.
2. `## Overview`: what the system does, in a few sentences.
3. `## Where it runs`: bullets: server(s), processes, threads, databases, proxies, scheduled work, outside
   services. Followed by one fenced ` ```mermaid ` block:
   - `flowchart TD`, `subgraph`s by where things run (users, each process, database, outside services),
   - about 15-20 nodes; node ids are letters and digits only (no `_` or `-`), e.g. `web[Web app]`,
     `db[(Databases)]`,
   - edges for the main data flows only, each labelled with what moves: `web -->|question| answer`.
4. `## Components`: one table, header starting with `Component`:
   `| Component | Responsibility | Runs where | Key files | Depends on |`
   - first cell `` `nodeid` Display name ``; every diagram node has exactly one row and vice versa,
   - Key files as markdown links relative to the repo root (`[src/pkg/web/](src/pkg/web/)`); they become
     GitHub/GitLab links from `git remote get-url origin`,
   - Depends on: node ids separated by commas, `—` for none.
   Text after the table (e.g. "the diagram shows only the main flows") is shown as a note.
5. `## Services & what they're for`: short intro + one table
   `| Service | Used for | Paid? / cost guard | Switch / key setting |`, every outside service and infrastructure
   piece.
6. `## Main flows`: `### <flow name>` + a numbered list; every step names at least one node id in backticks
   (the first one is the step's actor). 2-4 flows.
7. `## External dependencies`: grouped bullets (databases, AI/APIs, data sources, scheduled work, config
   switches) with the code location.
8. `## Dev loops` (optional): dev-only tooling that never runs live.
9. `## Component details`: one `### `nodeid` Name` per component (every table row), each with:
   - `#### How it works`: one or two plain sentences, then numbered steps, each ending with
     ` — via <service / tech / table> + key params` (shown as a "via" label), then a line
     `Design choice: <why it is built this way>`.
   - `#### Stack & settings`: a `| Uses | For what |` table and a `| Setting | Default | Meaning |` table
     (default column "default X (live: Y)" where they differ).
   - `#### In the code`: bullets with functions, files, constants (folded away on the page).

Markdown build.py renders: paragraphs, `-` and `1.` lists, tables, inline code, bold, italics, links, short
fenced code blocks. Keep each `## ` heading text as above (the parser matches "overview", "components" table,
"services", "main flow", "external", "component detail").

## State

- Artifact URL: https://claude.ai/artifact/HMjYEYLdjD97Y5jsyMYWbU
- Last mapped commit: 9aa5717 (origin/main)
- Date: 2026-10-24
- Live commit (this project, Klugscheisser Karl): the live install's checkout, read-only:
  `git -C ~/klugscheisser rev-parse --short HEAD` → pass as `--live <sha>`. Title: "Karl Architecture".
  Never touch `~/klugscheisser` otherwise (no pull, no edits, no `.env` reads).
