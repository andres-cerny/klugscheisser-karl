# Build plan v7: projects (one database per project)

Status: **built 2026-10-03** (go from Ondřej). Deviations and details: see "As built" at the end.

## Why
Fabi works in seasons: women's ice hockey now, downhill skiing next, maybe women's floorball. The projects do
not run in parallel and do not need to cross-match, and each wants its own people, fact fields, profile card,
feeds and questions. One-day topics need no pre-filled database at all.

## 1. Project registry
- One config file (`projects.yaml`), one entry per project: key, name, emoji, status (active / archived),
  database URL, sports, feeds file, fact catalog, profile-card fields, wording for Jev/Gemini
  ("Eishockeyspielerin" / "Skirennfahrerin"), result sources, cost caps.
- New projects are configuration, not code.

## 2. One database per project
- `klugscheisser project create <key>`: creates the database (pgvector, unaccent, pg_trgm), runs the
  migrations, loads the project's seeds.
- Migrations always run across all project databases; project-specific extra tables only where really needed
  (e.g. race results per venue for skiing). Most "different columns" are new fact *kinds* in the generic fact
  table (no schema change).
- Today's database becomes project **eishockey** without data loss.

## 3. Routing
- Web app: project prefix in addresses (`/p/eishockey/…`); every request uses that project's database.
- Perplexity: one connector address per project (`/mcp/eishockey`, `/mcp/ski`), each with its own
  instruction block (docs/client-instructions-<project>.md); one Perplexity Project ↔ one of our projects.

## 4. Background jobs only for the active project
- News polling, nightly refresh, backfills, media analysis, X: only for active projects.
- **Season switch:** final backup → archive the old project (jobs stop; data stays, can be reactivated next
  season with its full history) → activate the new project.

## 5. "Allgemein" catch-all for one-day projects
- Any sport, no seeding, no feeds, no nightly jobs.
- Only on demand: Perplexity → `hinzufuegen` (Wikidata + Jev namesake check) → Fabi says ja → Instagram via
  Apify + Jev → answers. Data stays if the topic comes back.

## 6. Copy between projects (rare)
- `klugscheisser project copy-person <from> <to> <person>`: person, handles, documents, facts — nothing is
  fetched or paid twice.

## 7. App per project
- Home page per project (its searches, chats, events), project badge + switcher in the navbar, small settings
  page (cost caps, nightly window, feeds on/off).

## 7b. Project briefing ("system prompt" per project)
- A text Fabi writes and edits per project in the app: what the project is for, which event/teams/people
  matter, what she wants (e.g. "Gossip und Persönliches vor Interviews, keine Taktik"), tone, what to avoid.
- Used everywhere the models judge: the answer prompt (Gemini), relevance and topic questions (Jev), question
  understanding, event preparation, game brief.
- **Learned part, never silent:** from her questions and ratings (👎 reasons) a short "Was wir gelernt haben"
  draft is suggested regularly (e.g. weekly: frequent topics, recurring complaints); Fabi accepts or edits it
  before it becomes part of the briefing.
- Versioned (who/when), so answers can be traced to the briefing they used.

## 7c. Dictation (voice input)
- Laptop: macOS/Windows dictation or tools like Wispr Flow work in every text field already — nothing to build.
- Phone: the keyboard's microphone (iOS/Android dictation) also works in our text fields at no cost.
- Small extra: a 🎤 button in the question field using the browser's built-in speech recognition
  (Web Speech API: Chrome/Android, Safari/iOS) — free, no server, no API costs; language de-CH/de-DE.
  Hidden where the browser does not support it. No own Whisper/STT service (cost + maintenance, not better).
- Note: High German is recognised well; Swiss German dialect poorly by all free options.

## 8. Ratings and test sets per project
- Ratings, review list and JSON export carry the project; Fabi's hockey questions test hockey, skiing questions
  test skiing.

## 9. Operations
- Nightly `pg_dump` per database with a retention rule; disk-space check.
- The laptop-server setup (Ubuntu Server, systemd services, fixed Cloudflare tunnel + Access, backups) is its
  **own small plan**, independent of projects.

## 10. Not in v7
- Skiing content (seeding, Wikipedia history, skiing feeds) and floorball: own project plans on top of v7.

## As built (2026-10-03)
- `projects.yaml` + `src/klugscheisser/projects.py`: the current project is a context variable; `get_engine()`
  and `get_settings()` follow it, so the rest of the code is unchanged. Thread pools keep it (`ContextPool`).
- Today's database is project `eishockey` unchanged (no data moved); `allgemein` = `klugscheisser_allgemein`.
- Web: `/p/<key>/…` sets the project and remembers it in a cookie; inner links need no prefix (deviation:
  addresses carry the prefix only when shared, not on every link). MCP: `/mcp/<key>`, `status` names the project.
- Jobs: news poller + nightly refresh only for active projects with `scheduled_jobs`; image analysis and refresh
  jobs for all active projects; nightly `pg_dump` per project into `backups/` (newest 7 kept).
- Prompts take their words from the project (`wording`: "Eishockeyspielerin" / "Sportlerin oder Sportler").
- Briefing: used by the answer writer only (answers, cross-person answers, fun facts), not in classification or
  ranking (deviation from "relevance and topic questions": a preference must not drop or hide data). New version
  → new answer-cache key. Learned part: weekly suggestion (≥5 new questions) or button; accept / edit / reject.
- Settings page: cost caps, nightly hour, news interval per project (table `project_settings`).
- `project copy-person`: person, aliases, handles, documents with checks and vectors, facts, career, stats, fun
  facts; teams linked when the target knows the same team key.
- Not built: per-project feeds file (only `eishockey` has scheduled feeds; add when skiing comes).
