# Sources v5: FIS alpine results (phase 9) and X via the xAI API (phase 8)

Research as of 2026-10-03 (read-only requests, identified user agent `klugscheisser-athlete/0.1`).
"Verified" = fetched ourselves. No paid API call was made.

## Summary

| Source | Data | Format | Key / licence | Live | Terms | Recommendation |
|---|---|---|---|---|---|---|
| **fis-ski.com** (`/DB/general/…`) | full results history per athlete, race, venue; WC, WSC, OWG, training | server-rendered HTML + PDF | none | yes (`live.fis-ski.com`) | **"You may not scrape the content on this Website."** | link only; ask FIS for written permission |
| **api.fis-ski.com** | unknown | – | – | – | robots.txt `Disallow: /` | no |
| **Wikipedia** (MediaWiki API) | podiums per race (season calendars), athlete victories, WSC/OWG results | wikitext / HTML via API | none; CC BY-SA 4.0 | no | API use allowed (etiquette: identified UA, low rate) | **PoC source for history** |
| **Wikidata** | FIS athlete id (P2772), some OWG/WSC participations with rank | SPARQL JSON | none; CC0 | no | open | id linking only |
| **Sportradar Winter Sports API** | alpine schedules, results, competitor profiles, historical results | REST JSON | paid (trial keys) | yes | licensed | production option |
| SportsDataIO | lists FIS Alpine WC + WSC coverage | REST | paid | ? | licensed | not checked further |
| ski-db.com | athlete stats | HTML | – | – | robots.txt returns **403** to our UA | no |

| X via xAI | Answer |
|---|---|
| X Search price (current) | **$5 per 1,000 posts fetched + $10 per 1,000 user profiles fetched**, plus tokens (per item since 2026-09-21). Neither blog number is current. |
| Cheapest suitable model | `grok-4.3` or `grok-4.20-0309-non-reasoning`: $1.25 / 1M input, $0.20 cached, $2.50 / 1M output. Grok 4.1 Fast is no longer listed. |
| Post URL in response? | **yes**, as citation URLs (`https://x.com/i/status/<id>` or `https://x.com/<handle>/status/<id>`) |
| Original post text / author / date? | **no** from the tool ("server-side tool call outputs are not returned"). Only via the model's own answer (not guaranteed verbatim) or X's oEmbed endpoint (verbatim) |
| Store verbatim text + permalink? | X policy: allowed if kept unmodified and deleted/updated when the post is deleted/changed; display needs author, @handle, timestamp, permalink |

---

## Part A: FIS alpine results

### A1. fis-ski.com (verified)

The site has a new Next.js front (`/_next/`) and the classic results database under `/DB/`. All `/DB/` pages
below returned 200 with full server-rendered HTML (no JavaScript, no bot challenge).

| What | URL pattern (verified) | Content |
|---|---|---|
| Athlete search | `https://www.fis-ski.com/DB/general/biographies.html?lastname=gut&firstname=lara&sectorcode=AL&gendercode=W&nationcode=SUI&search=true` | link to the biography with `competitorid` (Lara Gut-Behrami = `125871`, FIS code 516138) |
| Athlete results (all) | `https://www.fis-ski.com/DB/general/athlete-biography.html?sectorcode=AL&competitorid=125871&type=result` | date, place, nation, category (WC/WSC/OWG/EC/TRA …), discipline, position, FIS points, cup points; latest first |
| Athlete results filtered | `…&type=result&seasoncode=&disciplinecode=DH&categorycode=WC&limit=1000` | 123 World Cup downhill rows for Gut-Behrami (verified count, one page) |
| Race result | `https://www.fis-ski.com/DB/general/results.html?sectorcode=AL&raceid=118486` | rank, bib, FIS code, name, birth year, nation, time, diff, FIS points, cup points; organiser contact; PDF links |
| Race PDFs | `https://www.fis-ski.com/DB/v2/download/competition-attachment/<uuid>.pdf` | start list, results, "Results, Analysis, Standings" |
| Calendar (per season) | `https://www.fis-ski.com/DB/alpine-skiing/calendar-results.html?eventselection=results&place=&sectorcode=AL&seasoncode=2026&categorycode=WC&disciplinecode=DH&gendercode=W&…` | events with place, nation, race counts (8 women's WC DH events in 2025/26) |
| Venue history | same calendar with `place=Crans+Montana&seasoncode=2024` → `event-details.html?sectorcode=AL&eventid=53457` → `raceid`s | **`seasoncode` is required** (empty season returns "No events found"): one request per season and venue |
| Live | `https://www.fis-ski.com/DB/alpine-skiing/live.html` lists `https://live.fis-ski.com/lv-al{codex}.htm` | live timing pages (not fetched) |
| ICS | calendar pages offer an ICS link for the current selection | not fetched |

Filter codes on the biography page: categories `WC`, `WSC`, `OWG`, `EC`, `FIS`, `NC`, `TRA` (training) …;
disciplines `DH`, `SG`, `GS`, `SL`, `AC`, `TC`, `PAR`.

- **robots.txt** (`www.fis-ski.com`):
  ```
  User-Agent: *
  Allow: /
  Disallow: /api/
  Disallow: /_next/
  Disallow: /search
  Disallow: /portal/
  ...
  ```
  `/DB/` is allowed by robots.txt; pages carry `<meta name="robots" content="index,follow">`.
  `api.fis-ski.com/robots.txt`: `User-agent: *` / `Disallow: /`. `live.fis-ski.com`: no robots.txt (404 page).
- **Terms** (<https://www.fis-ski.com/terms-and-conditions>): "No commercial reproduction, adaptation, distribution
  or transmission of any part or parts of this Website or any information contained, herein by any means whatsoever
  is permitted without the prior written permission of FIS. This also includes scraping. Scraping refers to
  extracting data from this Website via an automated process, such as a bot or web crawler. **You may not scrape the
  content on this Website.** If we determine your usage to be significantly excessive, we reserve the right to
  suspend or terminate your access to this Website."
  The ownership clause covers "text, graphics, data, … and the selection and arrangement thereof".
- **Conclusion:** robots.txt allows `/DB/`, but the terms forbid automated extraction outright (not only commercial
  use). Unlike the hockey sources (where "personal / non-commercial" left room for the PoC), there is no such room
  here. We do not build a fis-ski.com collector without written permission. We may store and show **links** to
  biography and race pages (built from ids we get elsewhere, e.g. Wikidata P2772).
- **FIS data services:** the "Timing & Data" page (<https://www.fis-ski.com/inside-fis/general-fis-documents/timing-data>)
  and the Alpine Data Booklet describe the XML format timing providers **send to** FIS, not an outbound feed.
  No public FIS API or data-licence page found. Longines is the timing partner (link on the live page).

### A2. Wikipedia (verified)

- Season pages, e.g. `2025–26 FIS Alpine Ski World Cup`: sections "Women → Calendar" (each race with date, venue,
  discipline, winner, 2nd, 3rd) and discipline rankings. These give **venue history** (podiums per venue per season).
- Athlete pages, e.g. `Lara_Gut-Behrami`: "World Cup results" → "Season titles", "Season standings",
  "Race victories" (date, venue, discipline), "World Championship results", "Olympic results" (position per
  discipline). Not every athlete has all tables; lower-ranked athletes often have none.
- Access: `https://en.wikipedia.org/w/api.php?action=parse&page=<title>&prop=sections|wikitext&section=<n>&format=json`.
  robots.txt has `Disallow: /w/` for crawlers, but the API is meant for programs
  (<https://www.mediawiki.org/wiki/API:Etiquette>: identified User-Agent, serial requests). The project already uses
  Wikipedia (v3/v4 profiles).
- Licence: CC BY-SA 4.0 (attribution + link to the article; label "Wikipedia" as source, not "FIS").
- Gaps: only podiums/wins, not full rankings; community-edited; tables differ between pages (parser per table type).

### A3. Wikidata (verified)

- `P2772` = "FIS alpine skier ID" = the `competitorid` on fis-ski.com (`"125871"` → Q30293, Lara Gut-Behrami).
  Useful to link athletes to their FIS biography URL without searching fis-ski.com.
- Results: Gut-Behrami has only 10 `P1344` (participant in) statements, mostly Olympic events with rank
  (`pq:P1352`). No World Cup races. Not a results source.

### A4. Commercial

- **Sportradar Winter Sports API** (<https://developer.sportradar.com/more-sports/reference/winter-sports-overview>):
  "covers … Men's and Women's competitions: Alpine Skiing"; feeds Seasons, Stage Schedule, Stage Summary
  ("schedules and results information based on the stage provided (season, discipline, event, or run)"),
  Competitor Profile ("basic team/competitor profiles and historical results"). Paid; trial keys exist.
  Historical depth for venue history not checked.
- SportsDataIO lists "FIS Alpine Ski World Cup" and "FIS Alpine World Ski Championships" in its coverage page; not
  checked further.

### A5. Recommendation (phase 9, private PoC)

1. **History from Wikipedia** via the MediaWiki API: athlete pages (victories, WSC, OWG, season titles) and season
   pages (podiums per race → venue history). Label 📖 Wikipedia, store the article URL + revision id.
2. **Athlete ids from Wikidata** (P2772) to build FIS links (`athlete-biography.html?sectorcode=AL&competitorid=…`).
   The answer can say "full results: FIS" with a link; the tool does not fetch it.
3. **Ask FIS** for written permission to read `/DB/` results pages for a non-commercial prototype
   (or for a data feed). Until then: no fis-ski.com collector.
4. For production / SRF use: Sportradar Winter Sports (or SRF's existing licence, if any).
5. Live: none for the PoC (FIS live pages fall under the same terms; Sportradar has live).

### A6. Open questions

- Who at FIS handles data permission (no contact on the terms page; possibly the media / marketing department or
  FIS Marketing AG)? Does SRF already have a FIS or Infront data agreement?
- Is Wikipedia's podium-only depth enough for Fabi's venue questions ("how did X do in Crans-Montana before")?
  Positions 4–30 exist only at FIS / Sportradar.
- Sportradar: price and historical depth for alpine (seasons back to when?).

---

## Part B: X via the xAI (Grok) API

Sources (all fetched as `.md` from docs.x.ai on 2026-10-03; "Last updated" on the models page: September 21, 2026):
<https://docs.x.ai/developers/tools/x-search>, <https://docs.x.ai/developers/pricing>,
<https://docs.x.ai/developers/tools/citations>, <https://docs.x.ai/developers/tools/tool-usage-details>,
<https://docs.x.ai/developers/rest-api-reference/inference/responses>, <https://docs.x.ai/developers/release-notes>.

### B1. Pricing

- Pricing page, "Tool Invocation Costs": `x_search` — "**$5 / 1k posts, $10 / 1k profiles**".
  "X Search is billed per item fetched rather than per call: every post returned by a search or thread fetch,
  including parent and quoted posts, counts toward the post rate, and every profile returned by a user search
  counts toward the profile rate."
- Tool-usage-details: "As of September 21, 2026, X Search is billed per post and per user profile fetched rather
  than per call."
- History: release notes, November 2025: tool prices dropped "to no more than $5 per 1000 successful calls" —
  that is the "$5 / 1,000 calls" from blogs. "$25 / 1,000 sources" appears only in third-party blogs (likely the
  older Live Search / `search_parameters` price; not in today's docs). **Both are outdated.**
- Web Search stays at $5 / 1k calls.
- Exact cost per request comes back in `usage.cost_in_usd_ticks` (1 USD = 10,000,000,000 ticks) and
  `usage.server_side_tool_usage_details.x_posts_fetched` / `x_users_fetched`.

Models (text, < 200k prompt tokens, per 1M tokens):

| Model | Input | Cached | Output | Notes |
|---|---|---|---|---|
| `grok-4.3` | $1.25 | $0.20 | $2.50 | reasoning, default effort `low`; structured outputs; 1M context |
| `grok-4.20-0309-non-reasoning` | $1.25 | $0.20 | $2.50 | no reasoning tokens; structured outputs |
| `grok-4.7` (flagship) | $2.00 | $0.50 | $6.00 | |
| `grok-build-0.1` | $1.00 | $0.20 | $2.00 | coding model; not evaluated |

Grok 4.1 Fast: not on the models or pricing page any more (its model pages return 404); "Grok 4.7 Fast" exists
only in Cursor / Grok Build, not on the public API.

**Cost estimate per person** (unverified until a live run): one search with `allowed_x_handles` and `from_date` =
yesterday, say 5–20 posts fetched ($0.025–0.10) + ~10k input / 1k output tokens (~$0.015) ≈ **4–12 cents**, not
1 cent as in the build plan. 280 persons nightly ≈ $10–35 per night. Per-item billing makes narrow queries
(handles + date range + `max_turns`) the main cost lever.

### B2. Request format (Responses API, plain HTTP)

```
POST https://api.x.ai/v1/responses
Authorization: Bearer $XAI_API_KEY
{
  "model": "grok-4.3",
  "instructions": "...",
  "input": [{"role": "user", "content": "Find up to 10 recent X posts by or about the athlete …"}],
  "tools": [{"type": "x_search",
             "allowed_x_handles": ["handle1", "handle2"],   // max 20; not together with excluded_x_handles
             "from_date": "2026-10-01", "to_date": "2026-10-03"}],  // YYYY-MM-DD only, inclusive
  "text": {"format": {"type": "json_schema", "name": "x_posts", "schema": {...}, "strict": true}},
  "max_turns": 3,
  "store": false
}
```

Other `x_search` parameters: `excluded_x_handles`, `enable_image_understanding`, `enable_video_understanding`.
Internally the tool runs `x_keyword_search`, `x_semantic_search`, `x_user_search`, `x_thread_fetch`.
"Structured outputs with tools is only available for supported Grok 4 family models."

### B3. What the response contains

- `output[]`: `x_search_call` items (the call, not its result) and a `message` with `output_text`.
- `output_text.annotations[]`: `{"type": "url_citation", "url": …, "start_index", "end_index", "title": "1"}`.
  Example X URLs in the docs: `https://x.com/i/status/1975607901571199086`, `https://x.com/xai/status/1234567890`.
  So the **permalink is there**, the **author handle only sometimes** (in `/i/status/` URLs it is missing).
- Tool-usage-details: "Only the tool call invocations are shown — **server-side tool call outputs are not returned**
  in the API response." → **no original post text, no author, no date** from the tool.
- The model can be asked (structured output) for text, handle and time per cited post. That text is the model's
  rendering: it may be shortened, translated or wrong. It must not be shown as a quote without a check.
- **Verbatim check:** X's oEmbed API (`https://publish.x.com/oembed?url=<post url>&omit_script=1&dnt=true`, docs
  <https://docs.x.com/x-for-websites/oembed-api>: "Requires authentication? No", "Rate limited No", "recommended
  for performing tasks in bulk"). Verified with the docs' example post: returns `author_name`, `author_url`
  (→ handle), canonical `url` and an HTML blockquote with the exact post text and the date ("October 7, 2025",
  date only). **But** `publish.x.com/robots.txt` says `User-agent: *` / `Disallow: /oembed`. Documented API vs.
  robots.txt: decide before using it (the adapter has it, off by default).
- Python SDK `xai-sdk` (gRPC) exposes `response.citations` and `inline_citations` with `x_citation.url`; same
  information, no post text. We use plain httpx against `/v1/responses` (no new dependency).

### B4. Terms: storing and showing post content

- **xAI Enterprise terms** (<https://x.ai/legal/terms-of-service-enterprise>, last updated August 14, 2026; API users
  are "enterprise (business) users"): Customer "owns all right, title, and interest in the Output"; must not "use any
  Output to train any foundation models …" or "misrepresent that any Output was human-generated".
  "Third-Party Services are governed by the terms between Customer and the applicable third-party provider."
  No clause specific to X content found.
- **X Developer Policy** (<https://docs.x.com/developer-terms/policy>) — applies to "accessing X Content":
  - "If you store X Content offline, you must keep it up to date with the current state of that content on X.
    Specifically, you must delete or modify any content you have if it is deleted or modified on X … within 24 hours
    after receiving a request".
  - "You must maintain the integrity of all X Content that you display publicly or to people who use your service."
  - Redistribution to third parties: only Post IDs / User IDs (plus ≤ 500 post objects per person per day by
    non-automated means).
  - Off-X matching: linking an @handle to an off-X person needs opt-in consent or information "someone would
    reasonably expect to be used for that purpose" (an athlete's own public account is the expected case).
- **X Display requirements** (<https://docs.x.com/developer-terms/display-requirements>): "Display real, unmodified
  posts"; "Modify post text" is a Don't; author profile picture, @username and display name "must always be
  displayed"; "The post timestamp must be displayed and link to the post's permalink".
- **Consequence:** store the permalink + status id always; store text only if it is verbatim (oEmbed) and re-check
  it (deleted → remove). Never show model-reported text as a quote; show it as "Grok summary of an X post" with the
  link, or show the oEmbed version. Whether a post fetched via xAI counts as "X Content accessed via the X API" is
  not stated anywhere (open question).

### B5. Adapter skeleton (built, not wired)

`src/klugscheisser/collectors/x_grok.py`: `GrokXSearch(settings, client=None)`;
`search_person(name, handles=None, since=None, limit=10, session=None) -> list[XPost(url, author, text, posted_at,
text_verbatim)]`. It sends the request above, keeps only posts whose status id is in the citations (model-listed but
uncited posts are dropped), fills author/text/time from the model's JSON, records cost in `cost_ledger` (source
`xai`, units = posts fetched, exact USD from `cost_in_usd_ticks`) only if a session is passed.
`fetch_oembed(post)` swaps in X's verbatim text. Tests: `tests/test_v5_x.py` (fixture built from the documented
format, not recorded; one network test).

### B6. Recommendation (phase 8)

Run the planned 5-player test with `grok-4.3`, handles + `from_date`, `max_turns=3`, and log
`x_posts_fetched` and `cost_in_usd_ticks` per person. Keep X only if (a) most cited posts are really by/about the
athlete and (b) the per-person cost stays under ~5 cents. Use the X permalink as the source; quote text only after an
oEmbed check (decision on robots.txt needed) and remove posts that oEmbed reports as gone (404).

### B7. Open questions

- Does `x_search` with `allowed_x_handles` return the athlete's own posts reliably, and how many posts does a
  typical query fetch (= cost)? Only a live run shows this.
- Does the Responses API JSON also carry a top-level `citations` list (the SDK has one; the REST reference does not
  list it)? The adapter reads both.
- oEmbed: is the robots.txt `Disallow: /oembed` meant for crawlers only? Ask or skip; without it no verbatim text.
- Is X content reached via xAI bound by the X Developer Policy (24 h deletion duty)? Assume yes.
- The network test is skipped in normal runs: `tests/conftest.py` blanks `XAI_API_KEY` for all tests.
