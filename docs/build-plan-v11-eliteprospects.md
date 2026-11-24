# Build plan v11: Elite Prospects for richer person profiles, stats and teams

Status: **Part A (people) built and live 2026-10-04** (PR #1). Part B (teams) not built yet. See "As built" at the end.

## Why
- Fabi said Elite Prospects (EP) profiles are better and fresher than Wikipedia. Test on 5 Swiss players, then
  the tier 1 scrape (39 persons, 2026-10-04) confirmed it:
  - `season_stats` is empty for every person (the results collectors store games only, never player stats).
    EP: 1,200 season rows for tier 1, incl. 344 playoff rows and the current season (24 players already have
    2026-27 games).
  - Awards: app 47, EP 374 (with season and number, e.g. "SDHL Most Points (89)").
  - Stale current team for at least 6 tier-1 players (Wey → Northeastern, Mériguet and Surdez → Lausanne,
    Harju → EV Zug, Eggimann → Ambrì, Elsmore-Sautter → ZSC).
  - Height differs for 22, birthplace for 9 (e.g. Müller 176 cm in Wikidata, 167 cm on EP and de.wikipedia).
  - 94 transfers with date, length and a source link (club press release).
- Wikipedia stays the source for life story, quotes and background.

## Terms and risk (Ondřej's decision, 2026-10-04)
- EP's terms forbid scraping; robots.txt allows `/player/` and `/team/` pages and sets
  `ai-input=yes, ai-train=no`. Personal, non-commercial project: Ondřej accepts the risk of a block.
  Plan B if blocked: the official EP API (Personal plan; women's hockey is a +30% add-on, roughly
  $1,250–2,760/yr depending on regions, see developer.eliteprospects.com/pricing).
- `docs/sources.md` and the sources registry ("Scraping verboten: wird nie gelesen") are updated to say what is
  read, how often, and why.
- Never stored or shown: photos (copyright), projected stats (rows without a team), scouting reports (paid),
  per-game player stats (paid; we only get game results and the free last-5/last-10 totals).

## Polite fetching (all EP reads)
- One request at a time, 6–9 s random gap, normal browser user agent, no login, no cookies; only allowed paths.
- Stop at once on 403/429/captcha. On 5xx/timeouts: wait 30 s, 60 s, 2 min, then stop for the day.
  Honour `Retry-After`.
- One block → pause all EP reads for 7 days and tell Ondřej (push notification + line in `klugscheisser features`).
- Daily cap 300 pages; runs at night (02:00–05:00) with the other nightly jobs.
- Refresh only what can have changed: tier 1 daily in season, tier 2 weekly, tier 3 monthly; plus the players of
  a team the day after it played (from the results collectors). Typical day: 30–50 pages.
- Raw page data cached per person (`ep_raw` table or `data/eliteprospects/raw/`), so re-imports never refetch.

## Part A: people (first)
### A1. Fetch
- Tier 2 scrape (198 persons, ~25 min), same script as tier 1 (`data/eliteprospects/scrape.py`), then
  moved into the app as `klugscheisser ep fetch --tiers 1,2 [--person <id>] [--dry-run]`.
- Find the 39 tier 1/2 persons without an EP id via EP search: candidates go through the existing Jev namesake
  check (birth date, nationality, team); never set an id on a name match alone.

### A2. Import (`klugscheisser ep import`)
- **Profile facts** (kind, source `eliteprospects`, source URL = the player page, data date):
  height, weight, shoots/catches, detailed position, birth place, youth team, current team + jersey number +
  status, contract until, agency, draft, relatives in hockey, name pronunciation (audio URL).
  Social links only as handle *candidates* for the existing account check (Jev identity check unchanged).
- **Conflicts:** for hockey data (height, weight, position, teams, stats) EP wins; the other value stays stored
  and visible under "Daten & Einstellungen". Wikipedia/Wikidata keep life story, studies, quotes.
- **Awards** → `facts` kind `award` with season and number; medals first on the Erfolge list; deduplicated
  against existing award facts.
- **Season stats** → `season_stats` (exists): per season, league, team; regular season and playoffs; skaters
  GP/G/A/PTS/PIM/+/-; goalies GP/W/L/SO/GA/SVS/SVP/GAA/TOI. Old seasons with only GP: other values stay empty
  (no data), never 0. Needs a migration: `phase` (regular/playoffs), `plus_minus`, goalie columns.
- **Career totals per league** and **form** (last 5 / last 10 games) stored with the person.
- **Transfers** → `career_stints` with date, from → to, length, source link; fixes stale current teams.

### A3. Person page
- Top of the page (Steckbrief): links **Instagram · Wikipedia · Elite Prospects** (moved up from the closed
  "Daten & Einstellungen" box).
- Steckbrief gets the new profile fields, each with source and data age; pronunciation as a small play button.
- New **Statistik** section: season table (club / national team tabs, playoffs toggle), career totals per
  team and per league with points per game; goalies with save % and GAA. Form line: "letzte 5 Spiele: 3 T, 4 A".
- Timeline shows transfers with their source link.

### A4. Agent (MCP + chat)
- New MCP tool `stats`: filter by person, team, league, season, national team; sort by any stat (points,
  points per game, goals per game, save % …); compare two persons. Every row carries source and data date.
- `get_athlete` returns the new profile fields, career totals and form.
- Client instructions (`docs/client-instructions*.md`) get a few lines on the new tool.
- Example questions that must work: "Wie viele Punkte hat Stalder diese Saison?", "Beste Schweizerin nach
  Punkten pro Spiel in der SWHL", "Wer spielte zusammen in Northeastern?", "Brändli an Olympia vs WM".

## Part B: teams (second)
- **Team pages** (`/team/<id>/<season>`): roster per season with jersey numbers, positions, age, season
  stats. About 30 pages (Swiss league teams, PWHL/NCAA/SDHL teams with Swiss players, national teams).
  New players found there go through the normal add + Jev check (tier 3 unless they are Swiss).
- **League pages:** standings and scorer/goalie rankings for SWHL A, PWHL, SDHL, NCAA (about 10 pages), so the
  agent can say "Stalder ist 2. der Skorerliste".
- **Coach/staff pages** for the 9 coaches in tier 1/2.
- Team page in the app: current roster with stats, standing, top scorers; MCP `get_team_roster` returns them.

## Tests
- Parser tests on saved EP pages (fixtures from the tier 1 scrape, a skater, a goalie, a young player with
  youth leagues, a retired player): every field, projected rows dropped, empty values stay empty.
- Import: idempotent (second run changes nothing), conflicts keep both values, awards deduplicated, stints
  from transfers with source link, nothing written for other projects.
- Fetcher: stops on 403/429/captcha, backoff on 5xx, daily cap, 7-day pause after a block (no network: fake
  HTTP client).
- MCP `stats`: filters, sorting, per-game rates, source on every row.
- Web: person page shows top links, Statistik section, goalie variant.
- `uv run pytest` green; then manual check on the server's dev copy with tier 1 data, on a phone.

## Rollout
1. Tier 2 scrape (data only). 2. Build A1–A4 in `~/projects/klugscheisser_athlete` with tests.
3. Ondřej checks on his phone (dev copy, own port, not public). 4. Go → `git pull` in `~/klugscheisser`,
   migrate, import, restart (see docs/server-handoff.md "Update"). 5. Part B the same way.
- Costs: none, except Jev namesake checks for the 39 missing ids (small; asked before running).

## Shared data contract (foundation, 2026-10-04)
Everything below exists on branch `ep-v11` before the parallel work starts; all parts build against it.
- Migration `e1a0c0ffee11` (after `d931ec0654c7`):
  - `season_stats` + `team_kind` (club | college | national | youth), `phase` (regular | playoffs),
    `plus_minus`, goalie `wins/losses/ties/shutouts/goals_against/saves/toi`, `source` (eliteprospects | …).
    Unique: (athlete_id, season, league, team_name, phase, source). `season` = start year (2026 = 2026/27).
  - `ep_profiles` (one row per person): `ep_id`, `url`, `fetched_at`, `raw` (page data blocks as scraped),
    `data` (form last5/last10, career_totals, highlights, links, latest team), `imported_at`.
  - `source_state` (one row per scraper): `blocked_until`, `state` (day, pages, failures …).
- Fact kinds (facts/catalog.py): new `weight_kg`, `youth_team`, `current_team`, `contract`, `agency`;
  `award` value gains `season`, `number`, `league`. Source string for everything from EP: `eliteprospects`
  (facts.source, career_stints.source, season_stats.source), extractor `eliteprospects`, source_url = player page.
- Display priority: `facts.queries.EP_FIRST_KINDS` — for these kinds an `eliteprospects` fact ranks first. The
  importer marks differing non-EP facts of these kinds `status="conflict"` (they stay visible as conflicts).
- Pronunciation: EP audio goes into `name_pronunciations` with `source="eliteprospects"` (mp3, mime
  `audio/mpeg`); it replaces a computer voice (`tts`) but never a Wikimedia recording.
- Stats queries for web and MCP: `klugscheisser.stats.queries` (`season_rows`, `totals(by=team|league|team_kind)`,
  `rates`, `row_dict`, `season_label`).
- CLI group `klugscheisser ep …` (`fetch` = fetcher part, `import` = import part).
- Test fixtures: `tests/fixtures/eliteprospects/{stalder_skater,brandli_goalie,meriguet_youth,rochat_retired,
  mueller_pwhl}.json` (format of `data/eliteprospects/raw/<athlete_id>.json`: athlete_id, eliteprospects_id,
  name, tier, url, fetched_at + the page blocks).

## Parallel work (one git worktree and one test database each)
- **A. Parser + import** (`collectors/eliteprospects/parse.py`, `ep_import.py`, `ep import`): raw → facts,
  awards, season_stats, career_stints (transfers), ep_profiles.data, pronunciation, handle candidates.
- **B. Person page** (`web/person.py` & co.): Instagram · Wikipedia · Elite Prospects links on top, new
  Steckbrief fields, Statistik section, transfers in the timeline.
- **C. Agent** (`mcp_server`): `stats` tool, richer `get_athlete`, client instructions.
- **D. Fetcher** (`collectors/eliteprospects/fetch.py`, `ep fetch`, nightly job): polite fetching rules above,
  writes `ep_profiles.raw`; sources registry + docs/sources.md updated.

## As built (2026-10-04, live 23:03)
- Scrape: tier 1 (39) + tier 2 (197) athletes, 236 pages, 0 failures, no blocking (one-off script
  `data/eliteprospects/scrape.py`, raw files in `data/eliteprospects/raw/`, not in git). Coaches (staff pages)
  and the 39 persons without an EP id are not fetched yet.
- Live import (`ep import --from-dir … --no-audio`): 236 persons, 5,801 facts, 9,378 season rows, 2,526 stints,
  241 conflicts marked, 44 new handle candidates. Backup before: `~/klugscheisser/backups/eishockey-20261004-204723.dump`.
- **Automatic fetching is off** (Ondřej, 2026-10-04): `EP_ENABLED` is not set in the live `.env`, so the nightly
  job fetches nothing. Pronunciation audio not downloaded yet (`--no-audio`), it would fetch from EP's CDN.
- Person page: EP career is the list; other sources' stints of the same team become extra source links.
- Changes beyond the parts: `resolve_conflicts` uses EP as reference for hockey kinds; handle evidence
  "eliteprospects" weighs 0.4 (never "wahrscheinlich" alone); `stats.queries` counts one source and skips the
  "(all)" summary rows; nightly import also retries pages imported before their last fetch.
- Open: relegation/qualification rounds count as playoffs (`phase`); EYOF/YOG/Universiade medals stay awards;
  Part B (teams, leagues, coaches); lookup of the 39 missing EP ids (Jev namesake check, small cost).

## As built: WCh 2026 rosters nightly (2026-10-25, branch feat/ep-wch-nightly)
- Ondřej's decision 2026-10-25: EP may be read once per night, only for the players of the Women's WCh 2026
  (all teams, provisional + confirmed rosters = `event_entries` of event `wch-2026`), because the season stats
  were frozen at the import of 04.10.2026. Same terms risk, accepted for this limited, polite fetch.
- New setting `EP_SCOPE`: `tiers` (default, the v11 rule) or `event:<key>`: only that event's roster players
  (role athlete), each once per local day, never fetched first, then oldest (no tier order, so a roster larger
  than the cap rotates); no tier/after-game rule, nobody else. Persons without an EP id are counted (`ohne_ep_id`
  in the run result, listed by `--dry-run`), not looked up. Unknown key / event over (from the second day after
  its end) → nothing is read, the run says why.
- Timing: `EP_NIGHTLY_START_HOUR` 5 / `EP_NIGHTLY_END_HOUR` 9 (was 3/5, i.e. before the 05:00 refresh); a start at
  or after `NIGHTLY_HOUR` also waits until that day's refresh is done (marker set, no nightly job running; one
  running for more than 6 h counts as crashed). The worker runs EP in the same loop right after the refresh,
  then the importer, then the backup.
- Requests: a page read before is requested at its stored final URL (`/player/<id>/<slug>`, one request instead
  of two: every redirect hop counts against the cap); a 404 there falls back to `/player/<id>` once.
- Live count (read-only, 25.10.2026): 278 roster players, 201 with an EP id (64 never fetched), 77 without (all
  23 of Japan, 13 Austria, 13 Canada …). One night: ~201 requests (first night ~265), about 25 min at 6–9 s.
  The 300 cap is never exceeded (checked before every request, hops included).
- Blocks: unchanged (403/429/captcha → whole run stops, 7-day pause), plus `block_detail` in `source_state`.
- Test fetch 25.10.2026: 2 pages (Stalder, Aymon) → both **HTTP 403** (881 KB body). EP may now refuse this
  server or this client; see `docs/sources.md`.
- Live: `EP_ENABLED=true` and `EP_SCOPE=event:wch-2026` in `~/klugscheisser/.env`, restart. No migration.


## As built: history without EP data (2026-11-01, branch feat/missed-ep-history)
- `klugscheisser history-gaps` (read-only, free): who has thin history and why (no EP id / EP id never loaded,
  stations without seasons, no stats before this season), groups in priority order: this week's games, the WCh
  2026 rosters (by nation), tiers.
- `klugscheisser sihf-history [--yes]`: past SIHF seasons for players without EP data (docs/sources.md, "SIHF past
  seasons"); the person page lists the SIHF seasons as the career (PR #59).
- `klugscheisser ep-ids [--yes]`: missing EP ids from Wikidata (P2481: her own item, or name + same birth date, or
  without birth date one candidate of her nation); EP links in stored articles are only listed; never requests EP.
  Dry run 01.11.2026: 29 of 96 (games of the week, WCh, tiers 1–2) found, 2 for Ondrej; tier 3: 35 of 68.
- Prepared, not run: `ep urls --out` (player pages of persons without EP data, priority order) +
  `scripts/ep_mac_fetch.sh` (Mac, polite, stops at a block) + `ep from-html` (pages -> raw files for
  `ep import --from-dir`). Steps for Ondrej: `docs/ep-mac-fetch.md`. Untested against Cloudflare.
- `ep import --from-dir` sets the EP id of a person without one from a page that passed `ep from-html`.
