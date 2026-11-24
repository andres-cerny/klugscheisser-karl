# Unihockey / Floorball: sources for Klugscheisser Karl

Checked 2026-11-24 from this server (plain `curl`, no login), unless noted otherwise.
"Machine-readable" means you can parse it without a browser: JSON/API, or server-rendered HTML tables.
"JS app" means the content only appears after JavaScript runs (needs a headless browser or Firecrawl).

## Summary: what to use for automated ingestion

| Need | Best source | Format | Notes |
|---|---|---|---|
| WFC 2026 schedule, results, standings (live) | Torneopal widgets of the official results service (`salibandy.api.torneopal.com/taso/widget.php`) | server-rendered HTML in `document.write()` | public, no key; stable match IDs; best option |
| WFC 2026 match pages | `tulospalvelu.salibandy.fi/match/<id>` | JS app | links only; data comes from the Torneopal API (key needed) |
| WFC squads (official) | IFF App `app.floorball.sport` | JS app + auth | team pages need login → use federation announcements |
| Swiss national-team selections | unihockey.ch news (swiss unihockey press releases) | server-rendered HTML | full squad lists with clubs since 2008; RSS feed |
| Swiss player birth date / number / position | unihockey.ch club "Kader" pages | HTML table | L-UPL clubs only; appears to show the 2025/26 rosters |
| WFC history (medals, records) | floorball.sport (IFF) + unihockey.ch medal table | HTML | static pages |
| Per-game history | Wikipedia per-edition pages | HTML/wikitext | Wikipedia API rate-limits this server, so throttle hard and send a UA |

## 1. IFF: International Floorball Federation

- **floorball.sport** (WordPress)
  - robots.txt: `User-agent: * / Disallow:` (everything allowed); sitemap `https://floorball.sport/sitemap_index.xml`.
  - Static HTML, easy to parse.
  - Useful pages:
    - WFC Men overview: https://floorball.sport/world-floorball-championships/wfc-men/ . Medal table, all editions with host cities and links into the IFF App (`app.floorball.sport/LeagueOrganizer/Magazine/<id>`: 650 = 1996 … 661 = 2018, 91 = 2020, 53 = 2022, 187 = 2024, 217 = 2026), and the "Event Evolution" format history.
    - Records: https://floorball.sport/wfc-records/mens-wfc-records/ . All-time top 10 points, most tournaments, games, goals and assists, single-tournament records.
    - Rankings: https://floorball.sport/rankings/mens-wfc-rankings/ . Order only, no points; last update 26 Mar 2026. Austria is 26th.
    - News: `https://floorball.sport/<slug>/`. Includes groups (2026-03-05), schedule (2026-04-08, with a PDF), officials (2026-09-08) and "team lists published" (2026-11-17).
  - The old IFF archive `archive.floorball.sport` (match pages `/match/<id>`, old news) **no longer resolves** (DNS). Old `floorball.org`/`iffwfc.org` links found on swissunihockey.ch are also historic.
- **IFF App `app.floorball.sport`** (the official results/stats system since about 2020; Next.js app)
  - Public without login:
    - league-organizer pages: `/league-organizer/217` = WFC 2026, with tabs `/standings`, `/games`, `/statistics`, `/posts`.
    - group "league" pages: `/league/2423`…`2426` = groups A-D, with tabs `/table`, `/fixtures`, `/teams`.
    - All are JS-rendered; Firecrawl renders them fine.
  - Club and team pages (`/club/<id>/teams`) and therefore the **official WFC team lists redirect to login** (SMS code).
  - Backend: `https://iff-api.azurewebsites.net/api/...` (e.g. `/api/leagueapi/getleagueteams`, `/api/leagueorganizerapi/getleagueorganizerheader?LeagueOrganizerID=217`) returns **401 without a token**. It is undocumented and not for us without an IFF agreement.
  - No usable robots.txt: `/robots.txt` is served by the app. The pages carry `<meta name="robots" content="noindex">`.
  - Player career history (club seasons, caps): **not available publicly**.
  - Swiss Unihockey uses the same platform as a white label (`myapp.swissunihockey.ch`), also login-based.
  - The game times in the app are shown in the viewer's time zone (Firecrawl showed EST).

## 2. Official WFC 2026 site and results service (Finland)

- **freeoflimits.fi** (official event site; `wfc2026.fi` redirects here; WordPress)
  - robots.txt only blocks `/wp-admin/`; sitemaps at `/sitemap.xml` and `/en/sitemap.xml`.
  - WordPress REST at `/wp-json/` and an events calendar at `/wp-json/tribe/events/v1/` (not tested in depth).
  - Mostly Finnish with an English version at `/en/`. Covers news, venues and tickets.
- **Torneopal widgets (official results service of the Finnish federation)**
  - Pattern: `https://salibandy.api.torneopal.com/taso/widget.php?competition=sb2026int&class=2702&group=<1-5>&widget=schedule&columns=date,time,home,away,pitch,score`
  - group 1-4 = groups A-D; group 5 = all playoff and placement games.
  - Returns HTML inside `document.write("...")`.
  - Each row has the class `team_<teamId> … matchid_<id>`.
  - Team IDs: FIN 31458, SUI 34679, NOR 34677, LAT 34678, SWE 31459, CZE 33855, GER 34680, SVK 34681, EST 34682, DEN 34683, SGP 34684, THA 34685, SLO 34686, PHI 34687, CAN 34688, JPN 34689.
  - Other widgets: `widget=team&team=<id>` (squad table: name, number, goals, position, cards; **still empty on 2026-11-24**) and `widget=teamstats`. Unknown widget names return an error text.
  - Times are Finnish local time (EET).
  - The Torneopal REST API (`/taso/rest/getMatches` …) **needs an API key** ("No API key provided").
- **tulospalvelu.salibandy.fi**: JS front-end of the same data; `…/category/2702!sb2026int/tables`, `…/match/<matchid>`.
- **F-liiga / Finnish club stats:** see §5.

## 3. Switzerland

- **swissunihockey.ch** (federation website, Concrete5 CMS)
  - robots.txt only blocks CMS system folders.
  - The national team page https://www.swissunihockey.ch/de/nationalteams/maenner/ has staff, dates and past tournaments (HTML).
  - News is no longer listed on the site itself. The federation's press releases appear on unihockey.ch with "Autor: swiss unihockey".
  - The RSS block URL returns HTTP 500.
  - The "Game-Center" redirects to the app `myapp.swissunihockey.ch` (same platform as the IFF App, login).
- **api-v2.swissunihockey.ch** (official API)
  - Docs at `/api/doc`: a "Table API" (`/api/...`) whose resources are documented partly as "public" (e.g. `/api/national_players`, `/api/players`, `/api/games`), a Business API (`/bo/...`) and `/api/v3` (JSON:API).
  - As of 2026-11-24 **every tested endpoint, including the "public" ones, returns 401 "Authorization required"**. Access needs a partner API key and secret (apply via media@example.org) and a token from `/bo/session/auth`.
  - **robots.txt: `Disallow: /api/`, `Crawl-delay: 20`.**
  - So it is not usable without a formal partner key. If Ondrej wants it, he would ask swiss unihockey for one.
- **unihockey.ch** (independent Swiss floorball portal, publishes the swiss unihockey press releases)
  - robots.txt blocks only `/admin/` and `/ads/`.
  - Server-rendered HTML; RSS at https://unihockey.ch/rss.php (latest ~10 items).
  - National team news `https://unihockey.ch/nationalteams/mnner-a/news/?page=N`. The articles (`…/<slug>-<id>/`) contain the full selections in the form "Torhüter: … Feldspieler: Name, Name (Club)".
  - Medal table: `/nationalteams/mnner-a/medaillen/`. WFC event pages since 2008: `/nationalteams/mnner-a/events/`.
  - **Club roster pages** `https://unihockey.ch/meisterschaft/sml-mnner/teams/<slug>/?a=kader` give an HTML table (Nr, Name, Geburtsdatum, Position) plus coaches for the 12 L-UPL clubs. No season is stated and the content looks like 2025/26, so birth dates are fine but clubs and numbers may be outdated.
  - Also: Swedish, Finnish and Czech league news and tables, the Champions Cup, and blogs with history.
  - Caveat: unihockey.ch publishes an April Fools' article every 1 April.
  - Player career history (seasons, stats, caps): **no**. Caps appear only occasionally in articles.
- **Swiss media:**
  - SRF Sport, https://www.srf.ch/sport/mehr-sport/unihockey . Showed Swiss WFC games live on SRF zwei in 2022 and the women in 2025; no 2026 TV plan found.
  - Blick, https://www.blick.ch/sport/unihockey/ . Good interviews.
  - Regional papers (Südostschweiz, BZ, Thurgauer Zeitung).
  - These are HTML articles, fine for single facts; not for bulk ingestion (check their terms).

## 4. Sweden

- **innebandy.se** (Svenska Innebandyförbundet) and **statistik.innebandy.se** (SSL/federation stats).
  - `statistik.innebandy.se` **did not respond from this server** (connection timeout, 2026-11-24).
  - See the roster report in rosters/SWE.yaml for what the Sweden researcher found (robots, URL patterns, player career data).
- **ssl.se** (Svenska Superligan): JS app with a public JSON API under `https://www.ssl.se/api/` (no key; no
  robots.txt). `statistics-v2/layout-info?statisticsType=player` lists every season ("ssgt" uuid), and
  `statistics-v2/stats-info/players_summary|goalkeepers_summary?ssgtUuid=…&provider=ibis&count=1000` gives all
  players of a regular season (GP/G/A/TP/PIM, GA/GAA/SV%, birth date, nationality, team) in one request.
  Regular seasons 2016/17–2026/27 (no 2018/19); playoff stats answer empty. Used: `collectors/floorball/ssl_se.py`
  (2026-11-24). Birth dates "YYYY-01-01" are placeholders; many players have nationality "N/A".
- **stats.innebandy.se**: robots.txt disallows AI agents by name (ClaudeBot, Claude-User, PerplexityBot …): not used.

## 5. Finland

- **salibandy.fi** (Suomen Salibandyliitto): see rosters/FIN.yaml notes.
- **F-liiga** (fliiga.com) and **tulospalvelu.salibandy.fi**. Both run on Torneopal (see §2). Widgets are public and the REST API needs a key.
  - fliiga.com robots.txt only blocks /wp-admin/. Its WordPress REST collection `/wp-json/wp/v2/pelaajat` lists all
    ~1,800 players (men + women, current and past); player pages `/pelaajat/miehet/<slug>/` are server-rendered with
    every season since 2020/21 (regular season + playoffs, field players and goalies) and the club history.
    Used: `collectors/floorball/fliiga.py` (2026-11-24).
  - salibandy.api.torneopal.com robots.txt: `User-agent: *` ends with `disallow: /` (and AI bots are named).

## 6. Czechia, Germany, Austria

- **ceskyflorbal.cz**, **floorball.de**, **saisonmanager.de** (German Bundesliga stats), **floorball.at**: see rosters/CZE.yaml, GER.yaml, AUT.yaml and the section "Findings from roster research" below.

## 7. Wikipedia / Wikidata

- Useful for history: the results table and participation table on https://en.wikipedia.org/wiki/Men%27s_World_Floorball_Championship , per-edition pages, and short player bios (de/sv).
- German player articles for Swiss players exist (e.g. Noël Seiler, Jan Bürki, Pascal Schmuki, Gianluca Persici), but their stats tables are often years out of date (Persici: data as of 2020).
- The **MediaWiki API rate-limited this server after ~20 quick requests** ("You are making too many requests"). Use a descriptive User-Agent, one request per second or slower, or the Wikimedia dumps/REST API. Wikidata is untested; it probably has birth dates for players with articles only.

## 8. Which sources give player career history?

| Source | Club seasons | Club stats | National caps/goals |
|---|---|---|---|
| IFF App | per tournament only (login for team pages) | per tournament | no public career |
| swiss unihockey API | yes (players/national_players) | yes | yes (national_players) — **but key required** |
| unihockey.ch | no (current roster only) | no | only in article text |
| Wikipedia (de/sv) | yes, often incomplete or stale | partly | rarely |
| SSL / Finnish / Czech / German stats | see roster files | | |

## Findings from roster research

(Appended from the SWE/FIN/CZE/GER/other-team research; see below.)
