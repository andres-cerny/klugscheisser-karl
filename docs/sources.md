# Data sources: results, schedules, rosters, live scores

Research as of 2026-10-03 (read-only requests, identified user agent). "Verified" = fetched ourselves.
Decision summary at the end.

## Summary

| Source | Women's coverage | Access | Credentials | Live | Terms of use | Status in code |
|---|---|---|---|---|---|---|
| **IIHF** (realtime.iihf.com, stats.iihf.com) | WM 2026 (event 773), all IIHF events | Undocumented JSON (`gamestate/GetLatestState/{id}`), SignalR push, PDFs | none | yes (push) | **No reuse without written IIHF consent** | `collectors/results/iihf.py`, on for the PoC (`IIHF_REALTIME_ENABLED`) |
| **SIHF** (data.sihf.ch) | PostFinance Women's League (league 42), SWHL B–D | Undocumented JSON (results, game detail, standings) + ICS | none | `isLive` flag; push on new site | **Personal, non-commercial use only**; commercial needs SIHF permission | `collectors/results/sihf.py`, on for the PoC (`SIHF_DATA_ENABLED`) |
| **PWHL** (HockeyTech lscluster) | PWHL | JSON feed with a client key found in open-source projects | key (unofficial) | yes | **Forbids automated scripts** | `collectors/results/pwhl.py`, on for the PoC (`PWHL_ENABLED`) |
| **SDHL** (stats.swehockey.se) | SDHL, Damkronorna | HTML only | none | near-live HTML | none found; © Svenska Ishockeyförbundet | `collectors/results/sdhl.py`, on for the PoC (`SDHL_ENABLED`) |
| **Leijonat** (tulospalvelu.leijonat.fi, Finnish federation) | Women's national teams: every Euro Hockey Tour tournament, WCh groups | Undocumented keyless JSON (`accept: application/json`) | none | `GameStatus` 1 | none found; no robots.txt | `collectors/results/leijonat.py`, on (`LEIJONAT_ENABLED`) |
| **SIHF national-team pages** (sihf.ch) | Swiss women's national team: all games incl. friendlies/camps (results: newest 15; programme: current season with times) | HTML | none | – | personal, non-commercial use (as SIHF above) | `collectors/results/sihf_nt.py`, on, fallback only (`SIHF_NT_ENABLED`) |
| **NCAA** (ncaa.com, Sidearm school sites) | D-I women | HTML; Sidearm robots: `Disallow: /services/`, `Crawl-delay: 30` | none | partly | per site | not implemented |
| **Licensed SRF data feed** | whatever SRF licenses | to be defined | yes | yes | licensed | `SrfFeedLiveScores` stub (priority 1) |
| **Sportradar Global Ice Hockey v2** | PWHL confirmed (`sr:competition:41857`); WM / Swiss women's league to check in the coverage matrix | REST + push | paid (trial keys) | yes | licensed | `SportradarLiveScores` stub |
| API-Sports hockey / Highlightly | partial, unverified (Highlightly lists "WCH Women IIHF", PWHL, SDHL; no Swiss league) | REST | free tier (100 req/day) | claimed | licensed | not implemented |
| Flashscore / Livesport / Sofascore | – | – | – | – | **forbid scraping** | **never** |
| EliteProspects | broad (profiles, season stats, awards, transfers) | HTML player pages (`__NEXT_DATA__` JSON); paid API as plan B | none | – | **forbid scraping**; robots.txt allows `/player/` | `collectors/eliteprospects/fetch.py`, polite nightly fetch (`EP_ENABLED`, private project, risk accepted 2026-10-04; WCh 2026 rosters only via `EP_SCOPE=event:wch-2026`, 2026-10-25) |

## Results collectors (v2 phase B, verified 2026-10-03)

Feature flags are ON for the private PoC (decision 2026-10-03, terms risk accepted); re-evaluate before any
SRF/commercial use. `ingest/results.py` stores every game as an `events` row (`key` = `<source>-<id>`) and
every final game as an `official_result` item linked to teams and to athletes found by name.

| Source | Data used | Public game URL (source link) |
|---|---|---|
| SIHF | `cache300?alias=results` (latest round only) + `gameoverview?alias=gameDetail` | `https://www.sihf.ch/de/game-center/game/{gameId}` |
| IIHF | `GetLatestScoresStateForEvents/773` (all 29 games with realtime ids 13545–13573) + `GetLatestState/{id}` | `https://www.iihf.com/en/events/2026/ww/gamecenter/playbyplay/{75138+gameNo}/{gameNo}-{home}-vs-{away}` (linked, never fetched) |
| PWHL | `modulekit` seasons + schedule, `gc` gamesummary; teams in `PWHL_TEAMS` only | `https://www.thepwhl.com/en/stats/game-center/{gameId}` |
| SDHL | `ScheduleAndResults/Schedule/20960` + `Game/Events/{gameId}` (HTML) | `https://stats.swehockey.se/Game/Events/{gameId}` |

| Leijonat | `serie/helpers/getsubseries` (level 133) + `helpers/getgames` per subseries; per new final `gamereport/getgamereportdata` + `game/helpers/getsummary` | `https://tulospalvelu.leijonat.fi/game?season=2027&gameid={id}&lang=en` |
| SIHF national team | `nationalteams/womens-national-team/resultate-statistiken/` (first page) + `…/programm/` (HTML) | results page + `#{id}` fragment |

- The IIHF event endpoint solves the game → realtime-id mapping; no seed list is needed. The iihf.com game-centre
  id (`75138 + game number`) was checked against 8 indexed game-centre URLs (games 1–27), not by fetching iihf.com.
- IIHF `GG_Athlete_Id` and SIHF `ageGroup` encode birth data and are never stored.
- Gaps: SIHF returns only the latest round (no upcoming fixtures); IIHF state has no per-goalie stats
  (goalie names only); stats.iihf.com PDFs are not used (garbled text encoding).

## IIHF (verified)

- Schedule page `https://www.iihf.com/en/events/2026/ww/schedule` (Cloudflare blocks non-browser clients).
  29 games, 6–16 Nov 2026, Herning/Esbjerg. Group A: AUT, CZE, SUI, USA, GER. Group B: SWE, CAN, DEN, FIN, JPN.
- Each Game Centre page carries a realtime id (`data-game-id`), e.g. SUI–USA 2026 = 13547.
- `https://realtime.iihf.com/gamestate/GetLatestState/{realtimeId}`: 200 JSON without auth
  (`GameId, EventId, Gender, Status, GameTime, HomeTeam, AwayTeam, CurrentScore, Venue, Periods…`); `null` before the game.
- Live push via SignalR (`gamesHub`, `receiveGame`). Not tested during a live game.
- PDFs at `https://stats.iihf.com/Hydra/{event}/…pdf`; text extraction is garbled (custom font encoding).
- robots.txt: none on www, realtime or stats.
- Terms (<https://www.iihf.com/en/static/5109/terms-and-conditions>): "you may not store, modify, reproduce, transmit,
  sell or distribute (for commercial or non-commercial purposes) any portion of the Content of this Website …
  without prior IIHF written consent." Contact: webmaster@iihf.com. Media/marketing partner: Infront.

## Past tournaments: backfill (verified 2026-10-19)

The event endpoint `GetLatestScoresStateForEvents/{eventId}` answers `[]` once an event is over, but
`GetLatestState/{gameId}` still serves each old game (with `EventId`, local date/time, scorers, goalies). Game ids are
roughly chronological across all IIHF events (men, juniors, divisions), so each past event is configured with its
id range (`IIHF_BACKFILL_EVENTS`, found by sampling ids politely, 1 request/s):

| Event | IIHF event id | Realtime game ids | Notes |
|---|---|---|---|
| Women's WCh 2025 (České Budějovice) | 712 | 12088–12116 (29 games) | game number = id − 12087 |
| Women's WCh 2025 Division I A (Shenzhen; Austria promoted) | 793 | 12322–12336 (15 games) | Austria won all 5 |
| Women's Olympic tournament 2026 (Milano) | 852 | – | `GetLatestState` answers `null` for Olympic games; read from Wikipedia instead |

- There was no 2026 Division I for Austria: it was promoted in April 2025 (Div I A, Shenzhen); 2026 Div I A
  (Budapest) was won by France.
- **Line-ups (checked 2026-11-04, game watch rosters):** the realtime state names only scorers, assists and goalies
  (with number and position); there is no line-up in it. The official game sheet PDFs (`www.iihf.com/pdf/…`) answer
  403 with a Cloudflare challenge from this server (not worked around). Leijonat lists every WCh game but has no
  line-ups for WCh games (`getsummary` empty, also for Finland's games in 2025). So "who dressed" = everyone the
  realtime sheet names; a listed player who did not dress cannot be seen (logged only when a feed has line-ups).
- The iihf.com game-centre id of past events is unknown (no offset found), so these games link to their
  realtime game state (`https://realtime.iihf.com/gamestate/GetLatestState/{id}`), the data we store.
- **Olympics 2026:** the Wikipedia page "Ice hockey at the 2026 Winter Olympics – Women's tournament" (MediaWiki
  API, one request) has an `{{Ice hockey box}}` per game with date, time, teams, score, periods, goalies, goals
  with assists, venue, attendance and the IIHF game sheet PDF as `reference`
  (`https://www.iihf.com/pdf/852/ihw852a01_…`). That PDF is the game's link, labelled "IIHF via Wikipedia"
  (`collectors/results/wiki_games.py`, `WIKI_BACKFILL_PAGES`). Wikipedia text is CC BY-SA.
- Run by hand only: `klugscheisser results-backfill [--only wch-2025,oly-2026] [--dry-run]` (rules classifier,
  no model call; re-runs skip stored finals). Never part of the nightly run, which keeps the current event (773)
  plus optional `IIHF_EVENTS`. The Swiss Olympic games stored earlier from sihf.ch (fallback source) are taken
  over by the Wikipedia rows (same day, same teams).

## SIHF (verified)

- League list: `https://data.sihf.ch/Statistic/api/cms/cache300?alias=results&searchQuery=1,8,10,11/2026/&language=de`
  (PF WL = 42, SWHL B = 43, SWHL C = 101, SWHL D = 104).
- Results (latest round): `…cache300?alias=results&searchQuery=42//42&filterQuery=2027/42&language=de`
  (rows with teams, period scores, status, gameId, broadcaster; `isLive`; exports Csv/Ics/Pdf).
- Game detail: `…gameoverview?alias=gameDetail&searchQuery={gameId}&language=de` (goals with scorer/assists,
  penalties, line-ups, players, stats). Player records contain birth year → personal data, store only what's needed.
- Standings: `alias=standing&searchQuery=42//42&filterQuery=2027/42`. ICS export: `…/export?…&format=ics`.
- Not resolved: paging / upcoming-fixtures syntax (only the latest round returned).
- robots.txt (sihf.ch): `Disallow: /umbraco/` → the new site's htmx/export endpoints are off limits. data.sihf.ch: no robots.txt.
- Women's national team: no league id found in data.sihf.ch; national-team exports sit under the disallowed `/umbraco/`.
- Terms (<https://www.sihf.ch/de/nutzungsbedingungen/>): "… für Ihren persönlichen, nichtkommerziellen Gebrauch …";
  "Bei unberechtigter Weiterverwendung … für kommerzielle Zwecke behält sich SIHF rechtliche Schritte vor."

## National-team games (2026-10-17)

Before this, no Swiss women's national-team game was stored outside the WCh (IIHF event 773): Euro Hockey Tour,
friendlies and camps had no source.

**Leijonat** (`collectors/results/leijonat.py`, verified 2026-10-17)
- Level 133 = "National Team Women". Season alias 2027 = 2026/27, derived from today's date (from August: year + 1);
  `LEIJONAT_SEASON` only overrides it. Subseries 2026/27: 3691 EHT Kloten,
  4740 EHT Lahti, 3528 EHT Södertälje (Lidl Hockey Games), 4640/4641 WCh groups A/B.
- `helpers/getgames?…&subSerieId={id}&gamedays=4&dog=…`: with a subseries id, gamedays ≥ 2 returns every game of the
  subseries whatever `dog` is (tested before/inside/after the tournament and empty); gamedays=0 = only the `dog` day,
  gamedays=1 = nothing. Fields used: GameID, GameDateDB, GameTime, Home/AwayTeamAbbrv, Home/AwayGoals, GameStatus
  (0 scheduled, 1 live, 2 final), FinishedType (1 regulation, 2 OT → "n.V.", 3 shootout → "n.P."), PeriodSummary,
  RinkName, SubSerieName, Spectator.
- **Times are Finnish local time (Europe/Helsinki) at every venue**, whatever `TimeZone` says ("Europe/Stockholm"
  for Södertälje): Lahti SWE–SUI 12:30 = 11:30 Swiss; Kloten SUI–SWE 20:00 = 19:00 Swiss; Södertälje FIN–SUI 15:00
  = 14:00 Swiss; WCh SUI–USA 20:00 = 19:00 Swiss (all checked against the SIHF programme and IIHF).
- Details per new final (2 requests): `gamereport/getgamereportdata` (goal log with time, scorer, assists, goal type
  YV=PP, AV=SH, TM=EN, IM=goalie pulled, VL=shootout winner; arena; spectators) and `game/helpers/getsummary`
  (line-ups, goalie saves/GA). **Never stored:** DateOfBirth (summary), `UsersInfo` (scorekeeper name, e-mail,
  phone), referees, player/link ids.
- Competition per subseries ("Women's Euro Hockey Tour 2026/27, Lahti"; WCh → "IIHF Frauen-WM 2026"), created when
  missing. The WCh subseries are skipped while the IIHF collector is on (`LEIJONAT_SKIP_WHEN_IIHF`).
- The season is small (~20 games), so all its games are listed whatever the 14-day lookback says; details are
  fetched once per game (`known_final`), at most `RESULTS_MAX_DETAILS` per run.

**SIHF national-team pages** (`collectors/results/sihf_nt.py`, verified 2026-10-17)
- Results table, first page only (newest 15 games of all seasons; no game links). Pages 2+ and the CSV/PDF
  exports load from `/umbraco/statistics/…`, which robots.txt disallows: never requested.
- Programme: one accordion per camp/tournament with date, time, "SUI - SWE", place. The header says "Zeit (MESZ)",
  but the times are Swiss local time all year (stored as Europe/Zurich). Games without a programme row (older
  results) have no time: stored at 00:00 local and shown as date only.
- External id `sui-{date}-{home}-{away}` (IIHF codes, lowercase). Teams to read: `SIHF_NT_TEAMS` (default the women's
  page; opponents are mapped to the senior national teams, so only add senior pages).
- **Fallback source**: its games are skipped when another source stored the same game (same local day, same two
  teams); when a better source finds a game first stored from here, it takes the row over (key changes, the
  score-only item is replaced). Final games get a short score-only `official_result` item.

**Tournament containers.** Games carrying `raw["tournament"]` (EHT, WCh, Olympics) update the matching
tournament event (same key, or same key prefix with an overlapping date span and shared teams): end date, start
date (only moved earlier, so a seeded camp start stays), city, competition, status (final when all games are
final) and `event_teams`; a missing one is created (`eht-2026-sodertalje`). MCP `get_event` lists a tournament's
stored games ("spiele"); `frage`/web chat answer "Wie hat die Schweiz in Lahti gespielt?" / "Spielplan
Södertälje" from them (`answer/lookups.tournament_games_inputs`, first in the direct lookups; a tournament with a fact sheet in
`seeds/events/`, i.e. the WM, is left to `tournament.py`). With the WM fact sheet, `get_event` shows its sheet
(`turnier`) instead of the `spiele` list.

## Current season totals (2026-10-25, `stats/current.py`)

Elite Prospects was imported once (04.10.2026) and is now behind a Cloudflare bot challenge (never worked around).
The nightly step `saisonwerte` (after `results`; by hand `klugscheisser season-totals`, switch
`SEASON_TOTALS_ENABLED`) writes newer rows into `season_stats` next to the EP rows:

| Source (`season_stats.source`) | What | How | Stand (`fetched_at`) |
|---|---|---|---|
| `sihf` | PostFinance Women's League, all players: skaters GP/G/A/PTS/PIM, goalies GP/GA/GAA/minutes (no saves) | data.sihf.ch `cache300?alias=player` and `alias=goalkeeper`, `searchQuery=42//42&filterQuery=2027/42` (2 requests, whole league; phase from the table's Phase filter) | table update time |
| `leijonat` | National teams per tournament series (EHT, WCh groups), all teams: GP, G, A, PTS, PIM, +/-, goalies GP/saves/GA/SV% | counted from the stored Leijonat sheets; since 2026-10-25 the collector keeps each player's roster line (`player_stats`: time on ice, PIM, +/-, saves; no ids, no birth dates). Older sheets: `season-totals --refetch-sheets` once (1 request per game) | end of the last game counted |
| `college` | NCAA, the colleges in `seeds/college_sites.yaml` (Swiss players: Northeastern, St. Cloud State, Minnesota State (added 2026-10-25), Holy Cross, Union): GP/G/A/PTS/+-/PIM, goalies GP/GA/SV/SV%/GAA/W-L-T/SO | the college's own stats page (`/sports/womens-ice-hockey/stats`, Sidearm; classic layout with captions and the newer Nuxt layout, which has no goalie table in the HTML), `collectors/results/college_stats.py`, via PoliteClient (robots.txt, Crawl-delay 30 s: ~3 min a night), switch `SEASON_TOTALS_COLLEGE_ENABLED` | the date of the last game played (US date) |
| `iihf`, `wiki` | WCh / Div I (IIHF realtime) and Olympics (Wikipedia game boxes), all teams incl. JPN/AUT/DEN: G, A, PTS; goalies GP | counted from the stored sheets; these have no line-ups, so skater GP stays empty ("Spiele nicht erfasst") and players without a point have no row | end of the last game |
| `swehockey` (2026-10-28) | SDHL and NDHL (Norra + Södra), every player: GP/G/A/PTS/PIM/+-, goalies GP (games played in)/GA/SV/SV%/GAA/SO/W/L/minutes | stats.swehockey.se "Players By Team" (`/Teams/Info/PlayersByTeam/{id}`, every player of every team, skaters + goalies) + "Scoring Leaders" for the Stand; `collectors/results/swehockey_stats.py`; 2 requests per league (6 a night); ids 2026/27 SDHL 20960, NDHL 20958/20959 (`SEASON_TOTALS_SWEHOCKEY_LEAGUES`), switch `SEASON_TOTALS_SWEHOCKEY_ENABLED` | the page's "Last update" |
| `leijonat_liiga` (2026-10-28) | Auroraliiga (Finland), every player: GP/G/A/PTS/PIM/+-, one row per team after a move; goalies GP (played)/GA/SV/SV%/SO | tulospalvelu.leijonat.fi keyless JSON `helpers/getplayers` + `helpers/getgoalkeepers` (+ `serie/helpers/getsubseries`, `getsubserie` for the game days); `collectors/results/leijonat_stats.py`; 4 requests (`SEASON_TOTALS_LEIJONAT_LEAGUES`, level 73), switch `SEASON_TOTALS_LEIJONAT_LEAGUES_ENABLED`. No GAA (the feed has no time on ice) | end of the last final game day |
| `jihf` (2026-10-28) | Japan women's league (WJIHL, EP "Japan (W)"): GP/G/A/PTS/PIM, goalies GP/GA/SV/SV%/GAA/SO | counted from the JIHF game sheets (`jihf.or.jp/watching_games/season/game/gamesheet.php?meet_id=…&game_no=…`; rounds `SEASON_TOTALS_JIHF_MEETS` 903/904/905); `collectors/results/jihf_stats.py`. A finished sheet is fetched once and kept in `source_state` "jihf": a night = 3 round pages + new sheets (2 s apart). Names are in Japanese script: matched only through `name_orig` in the roster seeds (WCh 2026 Japan list), the rest is skipped and counted. Switch `SEASON_TOTALS_JIHF_ENABLED` | start of the last game counted |

Why the league is not summed from the stored SIHF game sheets: the results feed lists only the latest round, so
Karl holds 14 of the ~33 league games played by 25.10.2026 (sheets since 26.09.); a sum would undercount. The sheets
are a cross-check (`--check`: sheet goals/assists never above the table; 25.10.: one SIHF inconsistency, Oona
Emmenegger 1 assist on the 11.10. sheet, 0 in the table).

Rules: names are matched to Karl persons with the existing matcher (exact names, both name orders, no guessing between
namesakes, no paid calls); a tournament series is skipped for a team when a final game of it has no stored sheet;
shootout goals (Leijonat "Penalty-Schiessen", IIHF "GWS") do not count. Team names follow the EP rows of the same
players (vote), so "EV Zug" is stored as EP's "EVZ Women's Team" with EP's team id. On reading
(`stats.queries.prefer_current`), a current row and an EP row for the same person, season, league and phase are never
both counted: the current row wins (league table, or a count over every stored game of the series; EP missed games, e.g. Stalder EHT 30.09.), except a points-only count without games played (IIHF/Wikipedia) against an EP row with games; a winning row names the EP values it replaces (`ersetzt`). Leagues only EP has
(NCAA, PWHL, SDHL, DFEL …) stay EP.

Checked by hand 25.10.2026 (copy of live data): Lara Stalder EVZ 2026/27 SIHF 8 GP 6 G 9 A 15 P 4 PIM (EP 04.10.:
5 GP 6 G 7 A 2 PIM; since then 2 A on 14.10. vs ZSC, 2 PIM on 24.10.) · Stalder EHT 2026/27 6 GP 3 G 3 A 10 PIM
(EP 5 GP: she played 21:18 min on 30.09. in Lahti) · Andrea Brändli EHT 4 GP 135 saves 12 GA (EP 134/12) · Alina
Müller WCh 2025 1 G 2 A, OG 2026 4 G 2 A (= EP) · Ivana Wey Northeastern 7 GP 4 G 3 A (EP 04.10.: 1 GP) · Laura
Zimmermann St. Cloud State 10 GP 1 G 1 A.

Match check for the club tables of other countries (`swehockey`, `leijonat_liiga`, `jihf`; `stats/current.plausible`):
after the name match, a row is **not stored** when the person is a coach/staff, goalie vs skater disagree, Leijonat's
birth year differs, or Elite Prospects (04.10.) has her playing this season in another league. It is stored and
listed as "ohne Vereinsbeleg" when nothing confirms the club (no known club, or only an older club from roster seeds /
career stations: players move). Cross-check (`season-totals --check`): goals/assists counted from the stored SDHL
game events never above the table (28.10.: Wilma Sundin 3 assists in the sheets, 2 in the table: the league
corrected an assist; no wrong matches).

Checked by hand 28.10.2026 against the source pages: Klára Peslarová Brynäs SDHL 6 GP, 6–0, 2 SO, 96.27 %, GAA 0.83 ·
Anna Meixner (AUT) Brynäs SDHL 9 GP 5 G 1 A 4 PIM · Mille Kunnerup Sørensen (DEN) Leksand NDHL 5 GP 4 G 10 A ·
Emma Ekoluoma (FIN) Ilves Auroraliiga 11 GP 13 G 5 A · Rio Noro (JPN) Daishin 3 GP 2 G 4 A (sum of 3 JIHF sheets).

Coverage 28.10.2026 (WCh 2026 lists): SDHL 37 players (SWE, CZE, FIN, GER, AUT, DEN), NDHL 9 (DEN 8, AUT 1),
Auroraliiga 5 (FIN, DEN, JPN), Japan league 21 of 23 Japanese players (the other two play in Finland and the PWHL).
No Swiss player is in these leagues this season.

Checked 2026-10-28 and **not** built (see the PR):
- **DFEL (GER), EWHL and the Austrian women's league:** stats only through the hockeydata.net widget API
  (`api.hockeydata.net`, customer API key embedded in deb-online.live / ewhl.hockey pages, no public terms; DEB
  imprint: no copying "zur Weitergabe … auf anderen Web-Sites"). Not used without permission; one non-commercial key
  from hockeydata (datainmotion.ai) or DEB would open all three.
- **Denmark (Kvindeligaen):** official stats at stats.sportsadmin.dk, robots.txt `Disallow: /`.
- **Czech Extraliga žen:** ceskyhokej.cz `competition/player-stats/28` (server-rendered, robots allow, Content-Signal
  `ai-train=no, use=reference`, no terms page): usable, but no WCh 2026 player plays there (3 Karl persons last
  season); goalie saves are all 0 in the source. Easy to add later (3–4 requests).
- **PWHL:** terms forbid automated scripts; regular season starts 05.12.2026. Only permitted route: the Wikipedia
  team season pages (full per-player tables, edited by hand with lag, "Updated …" date); the league season page has
  only top-10 leaders. Stays at EP's frozen 04.10. state for now.

Not covered yet (2026-10-25): PWHL (season starts 23.11.; HockeyTech player stats exist, PWHL terms forbid automated
collection), SDHL (no Swiss WCh player there now; the scoring page lists only the top 25), Auroraliiga (Saara
Kivenmäki, Ilves: the Leijonat service carries it, another level id), lower Swiss leagues (Melanie Häfliger: SWHL B
is SIHF league 43, same table endpoint, but she is not listed there).

## SIHF past seasons (2026-11-01, `stats/history.py`)

One-off, can re-run (idempotent): `klugscheisser sihf-history` (dry run with before/after per group) / `--yes`
(stores). For players **without Elite Prospects data** (added after the EP import of 04.10.2026, or never fetched):
every season 2014/15 to 2025/26 of SWHL A (league 42), SWHL B (43) and the SWHL C groups (44, 45, 46; served only
2014/15–2016/17). The current season stays with the nightly `saisonwerte` step (unchanged).

- **Endpoint:** `https://data.sihf.ch/statistic/api/cms/table?alias=player|goalkeeper&searchQuery=1//<league>
  &filterBy=Season,Phase&filterQuery=<END year>/<phase id>&language=de` (keyless JSON, the same service as the
  current tables; data.sihf.ch has no robots.txt; personal, non-commercial use as for SIHF above). ~280 requests,
  1.5 s apart (~10 min, the service answers slowly).
- **Caveats:** the season is the END year (2025 = 2024/25; Karl stores the START year). Seasons before 2014/15 and
  unknown league ids silently answer the current season, an unknown phase answers the first phase: every answer's
  `filters[].selected` is checked, a season with a missing part is left out whole. Empty tables have one row "Keine
  Spieler gefunden!". The no-phase season total counts "Playoff Overall" and the rounds twice: never used. Phases:
  playoffs = "Playoff Overall" alone when the season has it (2018/19 SWHL B, 2025/26 SWHL A; there the round tables
  repeat each other), else the rounds (1/4, 1/2, Final, Kleiner Final / Bronze); everything else (Regular Season,
  Phase 1, Masterround, Platzierungsrunde, Pre-Playoffs, Playout, Liga-Qualifikation, Aufstiegsspiele) = regular.
  Goalies only from the goalie table (it has their G/A/PIM too; GAA = SIHF's for one phase, else recomputed from
  minutes). No save %, no plus/minus. SWHL C rows of several groups (final round) are summed.
- **Stored:** `season_stats` source `sihf`, team = SIHF's team name, source link = the phase table; `career_stints`
  source `sihf` (consecutive seasons per team + league; open end when the current SIHF table has her at the same
  club, e.g. "EV Zug" → "EVZ Women's Team"), confidence 0.95. Same values as the hand load of 01.11.2026 (5 Swiss
  WCh players): rows with equal values stay untouched, nothing is deleted.
- **Namesake check** (`check_person`): unique exact name (both orders) via the matcher, role athlete, women's
  hockey, age 11–45 when the birth year is known, goalie/skater as Karl's position, and the club: a SIHF team equal
  to a team Karl knows for her (stations, season rows, current-team facts), or she is Swiss, or a stored document
  about her names the club (`text_evidence`; venue cities like Bern/Lugano only with the club prefix). Else listed
  as "fraglich", not stored. A season she played fully abroad by another source (>= 12 games) is left out.

Copy of live data (01.11.2026, `klugscheisser_history`): 825 past-season rows for 100 persons (769 new, 55
unchanged, 1 filled: Pfosi 2023/24 SWHL B playoffs GAA 0.33), 269 stations (252 new, 17 unchanged), 22 persons
"fraglich" (foreign players whose Swiss club Karl cannot confirm, e.g. Italian national players in Lugano/Ambrì).

## Youth history: U18 Women's WCh (2026-10-28, `stats/youth.py`)

One-off, can re-run: `klugscheisser youth-backfill` (dry run with coverage before/after) / `--yes` (stores).
Source: tulospalvelu.leijonat.fi level 134 "National Team Women U18": every top-division U18 WCh since 2016/17 (not
2020/21: cancelled), all teams, per part (groups, playoffs, relegation): `helpers/getplayers` / `getgoalkeepers`,
summed per player (older seasons answer every part with the whole tournament: identical parts count once; a sum
above 7 games drops the tournament). Rows `WJC-18 (W)`, team "<Nation> U18", `team_kind` national, source
`leijonat_u18`, only where Elite Prospects has no row of that season and league. Check: birth year (feed's date of
birth, never stored; Karl has some 1 January placeholders and day/month swaps, so the year decides) or nation.
~90 requests, 2 s apart (the service answers 429 when asked faster for long; backs off). 2017/18: the goalie table
answers 500, so that tournament has skaters only.
Coverage 28.10.2026 (353 persons on the WCh 2026 lists): with U18 WCh rows 123 → 189; of the 216 without any EP
data, 66 now have them (CAN 19, JPN 17, FIN 8, CZE 6, USA 5, SWE 5, SUI 3, GER 3; 356 rows for all matched Karl persons). AUT and DEN: none (their
U18 teams play Division I). Checked: Jael Manetsch 2024/25 4 GP 1 G 2 A (Group B 3 GP 1 G 2 A + Playoffs 1 GP).
Not covered: Division I/II (Finland plays the top division; Wikipedia has top-10 leaders for some years only, IIHF
realtime has no line-ups), junior club seasons (swehockey "U19 Dam" exists but is Swedish only), Hockey Canada (full
stats for Canada's games only).

## PWHL, SDHL, NCAA, commercial APIs

- **PWHL:** HockeyTech feed (`lscluster.hockeytech.com/feed/index.php?…client_code=pwhl`) works with a key widely
  documented in open-source projects, but it is not officially issued, and PWHL terms forbid "automated scripts to
  collect information". License via PWHL/HockeyTech or Sportradar for production.
- **SDHL:** `https://stats.swehockey.se/ScheduleAndResults/Schedule/20960` (2026-27), game events and line-ups as HTML.
  The collector stores games of the followed teams (scorers, assists, goalies; no line-ups). Player totals since
  2026-10-28 from `Teams/Info/PlayersByTeam/20960` (every player; source `swehockey`, above);
  `Players/Statistics/ScoringLeaders/20960` lists only the top 25.
- **NCAA:** no clean API; school sites (Sidearm) as HTML with a 30 s crawl delay. Not practical for live data.
  Season stats: `/sports/womens-ice-hockey/stats` on the Sidearm sites of `seeds/college_sites.yaml` is allowed by
  robots.txt (`/sports/` allowed, Crawl-delay 30) and server-rendered with full 2026-27 player and goalie tables
  (checked nuhuskies.com 2026-10-25: GP, G, A, PTS, PIM, +/-). Used since 2026-10-25 (source `college`, above).
- **Sportradar:** real-time scoring via on-venue scouts, push feeds; price on request. Check coverage for the
  Women's WC and PostFinance Women's League at <https://coverage-matrix.sportradar.com/>.

## Elite Prospects (v11, 2026-10-04)

- **What:** player pages `https://www.eliteprospects.com/player/<id>` of persons with an EP id (role athlete).
  Kept from `__NEXT_DATA__` → `props.pageProps`: `playerData`, `initialLeagueStats`, `initialCupStats`,
  `initialCareerTotals`, `initialAwards`, `initialTransactions`, `initialGameLog` → `ep_profiles.raw`; the
  importer (`klugscheisser ep import`) turns them into facts, season stats, awards and transfers. Never stored or
  shown: photos, projected stats, scouting reports, per-game player stats.
- **Terms:** EP's terms forbid scraping; robots.txt allows `/player/` and `/team/` (`ai-input=yes, ai-train=no`).
  Private, non-commercial project: Ondřej accepts the risk of a block (2026-10-04). Plan B: the official EP API
  (Personal plan + women's add-on, roughly $1,250–2,760/yr).
- **How (all `EP_*` settings):** one request at a time, random 6–9 s gap, browser user agent, Accept-Language en,
  no login, no cookies, redirects followed only to `/player/` pages; a page read before is requested at its stored
  final URL (`/player/<id>/<slug>`), so it costs one request instead of two. At night only, right after the
  nightly refresh (`EP_NIGHTLY_START_HOUR` to `EP_NIGHTLY_END_HOUR`, 05:00–09:00 since 2026-10-25, was
  03:00–05:00), at most 300 requests per day (retries and redirects included).
- **Who (`EP_SCOPE`):** `tiers` (default): only what can have changed: tier 1 daily, tier 2 weekly, tier 3 monthly
  (`EP_TIER3`, off), never fetched first; plus persons whose team played a final game since their last fetch
  (games of the results collectors, `EP_AFTER_GAME`). `event:<key>`: only the players on that event's rosters
  (provisional + confirmed, `event_entries`, role athlete), each once per night, never fetched first, then oldest;
  persons without an EP id are counted, not looked up; from the second day after the event's end nothing is read.
  **Ondřej's decision 2026-10-25: `EP_SCOPE=event:wch-2026`** (Women's WCh 2026, Denmark 6–16 Nov): season stats
  were frozen at the one-off import of 04.10.2026 and Fabi needs fresh numbers before and during the WCh. Same
  terms risk, accepted for this limited fetch. Live count 25.10.2026: 278 roster players, 201 with an EP id
  (~201 requests a night, first night ~265 because 64 pages are new; ~25 min), 77 without (all 23 of Japan).
- **Stops:** 403/429/captcha → no EP request for 7 days (`source_state.blocked_until`; shown by
  `klugscheisser features` and on the sources page as "pausiert bis …"). 5xx/timeouts → wait 30 s, 60 s, 120 s
  (`Retry-After` honoured, more than 10 min stops instead), then stop for the day. 404 / page without player
  data → noted, tried again after 7 days; 5 in a row → stop for the day. A block also keeps what the answer
  looked like (status, size, page title, captcha marker, `Server` header; never the page) in
  `source_state.state.block_detail`, to tell a bot wall from a ban.
- **Test 2026-10-25:** two test pages from this server (httpx, same headers as the fetcher) both answered
  **HTTP 403** (881 KB body, not kept); on 04.10.2026 the one-off script (urllib) got all 236 pages. Going live
  as it is today would pause EP for 7 days at the first page (safe, but no data).
- **By hand:** `klugscheisser ep fetch [--tiers 1,2] [--scope event:wch-2026] [--person ID] [--limit N] [--dry-run] [--force]`
  (`--dry-run` lists who is due and why without a request; `--force` ignores "due", never the block or the cap).

## Decision

1. **Live scores, priority order** (as specified): (1) licensed SRF data feed if SRF grants access;
   (2) official SIHF / IIHF data, **only with written permission**; (3) a commercial API (Sportradar) behind an adapter.
2. The SIHF and IIHF collectors are implemented and tested against fixtures but **disabled by default**.
   Turn them on (`SIHF_DATA_ENABLED=true`, `IIHF_REALTIME_ENABLED=true`) only after SRF has written consent
   (an internal tool of a broadcaster is likely "commercial" under both terms).
   *Superseded for the private PoC on 2026-10-03: all four results collectors are on (see above).*
3. Until then, results reach the tool through SRF articles (RSS) and posts by teams/leagues that tag the players.
4. Actions for SRF: ask SIHF (Glattbrugg) and IIHF (webmaster@iihf.com / Infront) for permission, check whether SRF
   Sport already licenses a data feed (e.g. Sportradar) that covers the Women's WM and the PostFinance Women's League.

## News feeds (verified 2026-10-03)

Config: `seeds/feeds.yaml`. All feeds returned 200 with our identified user agent. Every outlet is filtered to women's
ice hockey: a women's term plus a hockey term, or a known athlete's full name. "Teaser only" means we store the feed
title and teaser and never request the article page.

| Feed | URL | Kind | Section filter | robots.txt (feed / articles) | Terms of use | Article page |
|---|---|---|---|---|---|---|
| SRF Sport | `https://www.srf.ch/sport/bnf/rss/718` | 📺 srf | `/eishockey/` | allowed / allowed | – | **fetched** (unchanged) |
| Blick Eishockey | `https://www.blick.ch/sport/eishockey/rss.xml` | 🍿 boulevard | `/sport/eishockey/` | allowed / allowed (`*` only blocks `/suche/`, `/sport/resultate/`, `/archiv/` …) | Ringier AGB §11: scraping, crawling and TDM "ohne vorherige schriftliche Genehmigung … ausdrücklich verboten" | teaser only |
| 20 Minuten Eishockey | `https://partner-feeds.20min.ch/rss/20minuten/sport/eishockey` | 🍿 boulevard | `/story/` (URLs carry no section; the feed is the section) | feed host has no robots.txt / `*` allows `/story/`, but all AI agents get `Disallow: /*story*` | robots.txt legal notice: automated access "without the express permission of 20min.ch is prohibited", except what robots.txt permits | teaser only |
| Watson Sport | `https://www.watson.ch/api/1.0/rss/index.xml?tag=Sport` (Atom) | 🍿 boulevard | `/sport/` | `Allow: /`, `Crawl-delay: 1` | CH Media AGB: "der automatisierte Abruf von Informationen und deren Verbreitung in anderen Systemen" only with express consent | teaser only |
| NZZ Eishockey | `https://www.nzz.ch/sport/eishockey.rss` | 📰 media | `/sport/` (some hockey items sit directly under `/sport/`) | `*` allowed; all AI crawlers (GPTBot, ClaudeBot, PerplexityBot …) `Disallow: /` | not checked; most articles are paywalled | teaser only |

Notes:
- **Sport check** (`domain/sport.py`, 2026-10-17). SRF and Blick file women's FOOTBALL national-team stories under
  `/sport/eishockey/…` (SRF redirects them to `/sport/fussball/…`), and Watson tickers carry the teaser "Fussball,
  Eishockey, Boxen, Tennis und mehr". So "hockey" is decided by specific cues in title + text (Eishockey, Puck,
  Powerplay, IIHF …); more cues for another sport (Fussball, FIFA, ÖFB, SFV, Elfmeter, EM, WM in Brasilien …) win
  over the URL; the URL section only decides when the text names no sport; sport lists count for nothing; generic
  words (Nati, WM, Spiel, Team, Goalie) never decide alone. For fetched pages (SRF) the canonical URL is used.
  The same check gates national-team and city aliases in the team matcher: they need a hockey cue in the text,
  a page-checked (canonical) `/eishockey/` URL or a named player; a feed URL alone links no team. Multi-sport
  tickers ("… +++ …", "Sportnews-Ticker") never link teams. Stored items:
  `klugscheisser sport-audit` (dry run; `--resolve-urls` reads SRF canonical URLs; `--apply` unlinks / marks).
- **Feed details.** 20 Minuten emits empty placeholder items (`/undefined`, no title, "Invalid Date"); these are
  skipped. Watson is Atom: `<content>` is only "(Auf watson.ch lesen)", and the teaser sits in a non-standard
  `<description>`. Watson's `?tag=Eishockey` feed was stale (newest item 2 June 2026), so the Sport tag feed is used.
  The general Watson feed (`index.xml`) has few sport items.
- **Excerpt extraction** (only for feeds with `fetch_article: true`): JSON-LD `articleBody` first (present on Watson
  and 20 Minuten; Blick has a `NewsArticle` without a body), then SRF's article container, then the `<p>` inside
  `<article>` (Blick). It is tested on synthetic pages in each structure and was checked once on one real page per
  outlet. To switch a feed to full excerpts after written permission, set `fetch_article: true`.
- **Not added.** Tages-Anzeiger: `partner-feeds.publishing.tamedia.ch/rss/tagesanzeiger/sport` answers, but its
  newest item was from July 2026, the `/sport/eishockey` feed is empty, and robots.txt blocks AI crawlers.
  Keystone-SDA: no public RSS found. Its reports reach us through Watson and Blick (author "keystone-sda").
- **Action for SRF:** if article excerpts from Blick, Watson or 20 Minuten are wanted, ask Ringier, CH Media and
  20 Minuten (verlag@20minuten.ch) for written permission. Until then the boulevard items carry only title and teaser.

## Team staff (v19, checked 2026-10-19)

Stored in `team_staff` (one row per person + role + appointment, `since`/`until`, source, verbatim quote) by
`klugscheisser/staff.py`; event rosters keep their own staff lines in `event_entries`. Check: `klugscheisser staff <team>`
(`--sync` loads `seeds/staff` and the staff lines of all stored rosters; `seed base` does both too).

| Team | Source | What |
|---|---|---|
| SUI | SIHF team page `https://www.sihf.ch/de/nationalteams/womens-national-team/` (Management, Coaching, Medical, Other Staff) | current staff → `seeds/staff/sui-women.yaml` |
| SUI | SIHF roster PDFs linked on the programme page (`/media/…/wnt-roster-oktober-2026.pdf`, `wnt-aufgebot-weht-september-2026.pdf`; robots: only `/umbraco/` etc. disallowed) | staff per event → `seeds/rosters/eht-2026-*-sui.yaml`; the WCh list (`based_on`) copies them |
| SUI | Keystone-SDA 10.07.2026 (Südostschweiz), blue News 25.04.2026 | coaching change Muller (2019 – 25.04.2026) → Miller (from August 2026, contract to summer 2029) |
| SWE | roster PDF attached to the Svenska Ishockeyförbundet press release (via.tt.se `/files/…`, robots allows) | "Team Management" of the Lahti squad |
| GER, AUT, CAN, CZE, USA, JPN | the roster announcements already used for the WCh rosters (`seeds/rosters/wch-2026-*.yaml`) | staff blocks were there; now also team history |
| all Olympics teams | `rosters-wikipedia` (reads only the head coach) | head coach Feb 2026 → history evidence |

Not available: FIN (leijonat.fi answers with a Cloudflare JavaScript challenge, not fetched), DEN (the Danish
announcement page has no staff list). The SIHF news list is loaded via `/umbraco/` (disallowed), so press releases are
found through search/RSS, not by listing. The roster watcher keeps stored staff lines when it loads an official list
(the SIHF articles list players only).

## Free profile sources (2026-10-25, branch feat/free-profile-sources)

Why: Elite Prospects answers 403 since 25.10.2026 and never had 141 of the 278 WCh roster players
(docs/stats-sources-gap.md). Focus (Ondrej): (a) the Swiss WCh roster (28 players), (b) players of the other WCh
nations in the Swiss Women's League (2025/26 or 2026/27; 8 players: Fazokas, Kutzer, Lintner, Trummer (AUT),
Neubauerová, Pejzlová, Seroiszková (CZE), Liikala (FIN)). Everything free, no model call. Measured on a copy of the
live data (`klugscheisser_phasef`, 25.10.2026), `klugscheisser free-profiles coverage`:

| Group | n | Height | Weight | Shoots | Birth date | Photo | Past-season stats |
|---|---|---|---|---|---|---|---|
| (a) SUI WCh roster, before → after | 28 | 27 → 28 | 22 → 24 | 23 → 25 | 27 → 27 | 5 → 12 | 23 → 23 |
| (b) other nations in the Swiss league | 8 | 5 → 5 | 5 → 5 | 5 → 5 | 5 → 5 | 1 → 3 | 5 → 5 |
| whole WCh field | 278 | 167 → 224 | 134 → 198 | 167 → 212 | 226 → 235 | 10 → 124 | 137 → 137 |

Still missing in the focus groups: the three Austrians in Zug/Zurich (Kutzer, Lintner, Trummer) and three young Swiss
(Pfosi, Waser, Nora Harju; Harju also no birth date): on no past tournament roster, no Wikipedia/Wikidata. They come
with the official WCh 2026 rosters on Hockey Canada (the nightly step picks them up).

| Source | What | Adds (run order HC → Wikidata → Wikipedia) | Verdict |
|---|---|---|---|
| **Hockey Canada stats site** (`collectors/hockeycanada.py`, `facts/hockeycanada_import.py`) | rosters of all teams of WCh 2026/2025/2024, Olympics 2026, U18 WCh 2026/2025: number, position, shoots/catches, height (ft'in → cm), weight (lb → kg), birth date, hometown, club | 295 persons matched (604 roster lines; 343 lines are players we do not have, mostly U18, ITA/FRA). Field: height +34, weight +42, shoots +31, birth date +9; SUI: Manetsch (U18 WCh 2025). Canada's official WCh 2026 roster is already filled (23 players: number + club) | **keep**; the WCh 2026 pages of the other 9 teams are still empty (usually filled days before the event) |
| **Wikidata** (`facts/wikidata_import.py` + `facts/wikidata_extra.py`) | weight (P2067, new), Commons photo (P18) as link + author + licence | photos: SUI +7, (b) +2, field +114; weight field +4 | **keep** (photos) |
| **Wikipedia infobox** (`facts/wikipedia_infobox.py`) | height, weight, shoots/catches, position, birth place of the stored en/de/fr articles (wikitext, 7 API requests for 280 articles) | SUI: Aymon (height, weight, shoots); field: height +23, weight +18, shoots +14 | **keep** (cheap); 72 of its values differ from EP/HC/Wikidata and are stored as conflict (mostly birth place = home town elsewhere) |
| Wikipedia career-statistics tables | season rows | 0 of 36 focus players would gain a row (all with a table have EP seasons); 28 other players would | **dropped**: hand-made tables, often years old (Stalder: last row 2023-24), and the season-totals work (branch feat/season-totals) lets non-EP rows replace EP rows of the same season |
| Olympedia | height/weight of Olympians | 0 focus players left without height/weight who have an Olympedia id | **dropped** (not built) |

Rules and design:
- **Hockey Canada**: robots.txt allows the paths (seasons <= 2012-13 and AJAX stats are disallowed; no
  Crawl-delay); identified User-Agent, 3 s between requests (`HOCKEYCANADA_DELAY_S`); data from `__NEXT_DATA__`
  (`teamDataTournament.TeamRoster_Players/_Goalies`); team ids from the event's schedule page (they differ per event
  family; the site writes Austria's abbr as "AUS", so teams are mapped by name). Weight "0" and hometown ", " mean
  unknown. Copyright note on the site: only facts are kept (with the roster page as link and the tournament as data
  date), photos (assets.leaguestat.com) are never stored or shown.
- **Matching** (never creates a person): exact name/alias hit (also "Last First") of the team's nationality whose
  stored birth year is at most one off (the column is sometimes year-only = 1 January, or a year off in Wikidata:
  Rebecca Leslie 1995 vs. roster 1996); a fuzzy hit only with the same birth date; two candidates → skipped.
- **Trust** (`facts/source_facts.py`, `facts/queries._rank`): Elite Prospects > Hockey Canada > Wikidata > Wikipedia
  infobox > web/news; birth date: Wikidata > Hockey Canada. A free-source value that differs from a more trusted
  confirmed one is stored with status "conflict" and shown as "abweichend" (±2 cm / ±2 kg agree: feet/inches and
  pound rounding). Other sources' rows are never edited. Each source owns its rows (source + extractor) and is
  idempotent: a rerun adds nothing.
- **Output**: person page and MCP `person`/`get_athlete` show the values with "Hockey Canada, Kader IIHF Frauen-WM
  2025" (the tournament dates the value) or "Wikipedia de"; photos on the person page with author and licence.
- **Runs**: `klugscheisser free-profiles hockeycanada [--only wch-2026] [--who focus|event|all] [--dry-run] [--unmatched]`
  (~62 requests for all events), `free-profiles wikidata [--who …]`, `free-profiles wikipedia-infobox [--who …]`,
  `free-profiles coverage`. Nightly step `hockeycanada` (`HOCKEYCANADA_NIGHTLY`, on): the WCh 2026 rosters only, from 3
  weeks before until the day after the event (~11 requests); persons on them get the new values, rows of older events
  stay.
