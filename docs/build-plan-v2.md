# Build plan v2 (agreed 2026-10-03)

Goal: answers about athletes' lives that are fresh, sourced (link on every line), fun to read, and safe
(Vorsicht). Results added. Hosting stays the temporary cloudflared tunnel for now.

Execution: **phases A and E sequential by the lead agent** (shared core files); **phase B–D modules in parallel by
sub-agents**, each in its own git worktree, against the interfaces fixed in phase A. Lead agent reviews and
integrates. Every phase ends with `uv run ruff check`, `uv run pytest` green, one commit.

## Phase A — core contracts (lead, sequential)

A1. **Datenstand first.** Envelope gets `datenstand` as the first key: per source (Instagram, SRF, other media,
    results) the newest `scraped_at` / last check for the athletes in the answer, German formatted
    ("Instagram: heute 09:12 · SRF: heute 09:10 · Resultate: 26.09."). `hinweis` instructs: start the answer with it.

A2. **Source kind + emoji label.** New `source_kind` per item, decided deterministically where possible:
    `self` 📸 (Von ihr selbst) · `official` ✅ (Offiziell; curated list of team/league/federation accounts in
    `seeds/accounts.yaml`) · `srf` 📺 (SRF) · `media` 📰 (Medienbericht) · `boulevard` 🍿 (Gossip) ·
    `fan` 💬 (Fan-Account) · `brand` 💸 (Werbung) · `result` 🏒 (Resultat).
    Unknown Instagram accounts → Jev choice question (fan / brand / media / official / other) from handle, full name, bio.

A3. **Mood emoji (Jev).** New Jev choice question `mood`: freude 🎉, trauer 😢, aerger 😠, verletzung 🤕,
    humor 😂, nachdenklich 🤔, liebe ❤️ (love / personal life), neutral (no emoji).

A4. **Line format.** Each item gets `zeile_prefix` = source emoji + "(meaning)" + mood emoji, and `quelle_link`
    (exists). `hinweis`: every line = prefix + one fact + quelle_link. Meanings in brackets always (for now).

A5. **Jev changes.** Storyline option `karriereende` ("Karriereende, Rücktritt, Pause"). Vorsicht = Jev
    `sensitive` probability only (category rule stays for the rules fallback). Calibrate the threshold on the
    stored posts: label ~30 posts by hand-review list in `tests/fixtures/vorsicht_labels.yaml`, pick the threshold
    that keeps all true cases (injury, mental health, career end, controversy) and minimises noise; document.

A6. **Schema migration** for: `classifications.mood`, `items.source_kind`, `classifications.media_pending` (bool),
    `results` table (see C), `accounts` table (handle → kind, name, source of the decision).

A7. **Interfaces fixed for sub-agents** (Python protocols + dataclasses in `src/klugscheisser/interfaces.py`):
    `ResultsCollector.collect(since) -> list[GameResult]`, `NewsFeed` config entries, `MediaAnalyzer.analyze(post) -> MediaAnalysis`.

## Phase B — sub-agent 1: results collectors (worktree)

B1. `collectors/results/sihf.py`: data.sihf.ch (PostFinance Women's League id 42): latest results, game detail
    (scorers, assists, goalies, penalties), standings, schedule via ICS export. No `/umbraco/` paths (robots).
    Store only needed player fields (name, team, jersey; no birth data).
B2. `collectors/results/sdhl.py`: stats.swehockey.se schedule + game events (HTML), low frequency, identified UA.
B3. `collectors/results/iihf.py` (WM 2026, event 773): realtime.iihf.com JSON (`GetLatestState/{id}`,
    `GetLatestScoresStateForEvents/{event}`) + stats.iihf.com PDFs. Game → realtime-id mapping from the schedule
    PDF or the event endpoint; fallback: one-time manual seed of the 29 games. **Never bypass the Cloudflare bot
    protection on www.iihf.com.**
B4. `collectors/results/pwhl.py`: HockeyTech feed used by thepwhl.com (schedule, game summary, roster) for
    Swiss players' teams only.
B5. Feature flags, all ON for the private PoC (decision 2026-10-03, terms risk accepted by Ondřej; re-evaluate
    before any SRF/commercial use): `SIHF_DATA_ENABLED`, `SDHL_ENABLED`, `IIHF_REALTIME_ENABLED`, `PWHL_ENABLED`.
    Low volume only: fetched on demand for asked athletes/events, TTL cache (1 min live, hours otherwise),
    identified user agent, only results/scorers/line-ups stored, no redistribution.
B6. Results become items (`source_type=official_result`, 🏒) linked to teams and to Swiss athletes who scored /
    assisted / played (name matching via existing matcher), plus events for upcoming games.
B7. Fixtures from one real response each (trimmed, no personal data beyond names), tests offline.

## Phase C — sub-agent 2: more news feeds (worktree)

C1. Feeds config (`seeds/feeds.yaml`): SRF (exists), Blick Sport, 20 Minuten Sport, Watson Sport, each with
    `source_kind` (srf / boulevard / media). Verify each feed URL + robots.txt before adding.
C2. Same women's-hockey + athlete filter as SRF; article excerpt only where robots allow; otherwise teaser only.
C3. Tests with synthetic fixtures in each outlet's structure.

## Phase D — sub-agent 3: Gemini media analysis (worktree)

D1. Parser keeps transient media URLs per post (first image, up to 5 carousel images, video URL + duration);
    never persisted.
D2. `MediaAnalyzer` (Gemini, free tier): images → one German factual sentence each; reels ≤ 3 min → summary,
    spoken quotes (marked `automatisch_transkribiert`), visible text. Retry with backoff on 429; daily cap.
D3. Runs **after** the synchronous ingest (background), then re-classifies the item; `media_pending` shown as
    "Bild/Video wird noch analysiert".
D4. Gemini embeddings as option for search (keeps the 512 MB server option open later).

## Phase E — on-demand freshness + integration (lead, sequential)

E1. **On-demand ingest per question:** resolve athletes in the question; if not ingested today → Instagram (Apify)
    + news + results for exactly those, synchronously up to **60 s**; then answer. Over 60 s → answer with what
    exists + `refresh_status(job_id)` (long-poll up to 60 s). Max once per day per athlete unless `refresh`.
E2. Jev category filters in `search` ("verletzt" → injury, etc.).
E3. Integrate B–D, re-classify all stored items, real-data check on the Swiss roster (cost cap applies).
E4. Update docs: client instructions (Datenstand first, emoji legend, line format), sources.md, perplexity.md,
    README with credentials → features table.
E5. Live test in Perplexity and Claude via the tunnel; read the access log to confirm tool calls.

## Later / parked
Meta Graph API (Ondřej sets it up later) · fixed tunnel / Hetzner hosting · downhill skiing and "any sport"
(same codebase, sport packs) · permission from IIHF/PWHL/SIHF before any SRF use · her example questions ·
one commercial multi-sport results API (e.g. Sportradar, API-Sports, Highlightly) as a single `ResultsCollector`
adapter replacing the per-league collectors when more sports come (A7 interface must stay sport-agnostic).
