# Instagram sources

Where Karl's Instagram data comes from, and how to switch. Code: `src/klugscheisser/collectors/instagram/`
(`sources.py`, `base.py`, `brightdata.py`), ingest: `src/klugscheisser/ingest/instagram.py`. How to get the Meta token:
`docs/meta-graph-setup.md`.

## The sources

| Source (`INSTAGRAM_SOURCE`) | What it reads | Cost |
|---|---|---|
| `graph` | Meta Graph API Business Discovery: own posts + profile of **public Business/Creator** accounts. No tagged posts, no mentions, no private or personal accounts. | free, ~200 calls/hour |
| `apify` | Apify actor (`APIFY_INSTAGRAM_ACTOR`): posts of any public account incl. collaboration posts, tagged posts ("mentions"), profiles with bio and category. | per result |
| `brightdata` | Bright Data Web Scraper API: own posts (incl. collaboration posts) and profiles of any public account. No tagged posts / mentions. | free up to 5000 records per calendar month |
| `hybrid` | Graph → Bright Data → Apify, each part optional (see below). | Apify part only |
| `fixture` | JSON files in `tests/fixtures/instagram` (tests, offline). | free |
| `auto` (default) | `hybrid` when **two or more** of Graph, Bright Data, Apify are configured, else whichever one is configured, else `fixture`. | |

Graph is configured when `META_GRAPH_TOKEN` and `META_IG_BUSINESS_USER_ID` are set; Bright Data when
`BRIGHTDATA_API_TOKEN` is set; Apify when `APIFY_TOKEN` is set. `INSTAGRAM_SOURCE=apify` keeps the old
behaviour (Apify only), `graph` / `brightdata` use only that one.

**Without Apify** (e.g. Graph + Bright Data): no paid Instagram source at all. Own posts and profiles come from
Graph and Bright Data; handles neither can read (Bright Data month cap reached, errors) are reported as
`skipped_budget` and read next time; mentions / tagged posts are skipped (nothing else has them). The nightly
cost estimate counts Instagram as $0. Adding a person still checks the Instagram profile (Graph/Bright Data).

## Fallback order in `hybrid`

- **Own posts** (`fetch_posts`): every handle goes to Graph first. Handles Graph cannot read (not a
  professional account, not found, Graph stopped, server error) go to Bright Data (batches of
  `BRIGHTDATA_BATCH_SIZE` profile URLs per snapshot); what Bright Data cannot serve either (month cap, error,
  stopped) is fetched in **one** batched Apify call. With no Apify budget left, only the Graph part is read; the rest is reported as
  `skipped_budget` and read next time (its `last_scraped_at` is not moved).
- **Profiles** (`fetch_profiles`): Graph first (a profile already seen while fetching posts costs no extra call),
  then Bright Data (bio, verified badge, private, followers), the rest via Apify. Graph knows no category and no
  verified badge; Bright Data never filled category or business flag in the 2026-10-28 test. For profiles from
  either, `apply_profile` (`handles/profile.py`) keeps the stored category, verified badge and business flag
  when the new profile has none (else the weekly refresh would wipe Apify's "Athlete"). An unknown business flag
  never overwrites a stored one, whatever the source.
- **Mentions** / tagged posts (`fetch_mentions`): Apify only (Graph and Bright Data have no such data);
  without Apify they are skipped. Most Swiss ones come back through the team accounts below.

## Team accounts

`INSTAGRAM_TEAM_ACCOUNTS` (`ingest_instagram_teams` in `ingest/instagram.py`, nightly step via the plan's
`ig_team`, at most every 12 h, up to `INSTAGRAM_TEAM_MAX_POSTS` 12 new posts per account): posts stored as
`official` items, linked to the team whose `instagram_handle` (seeds/teams.yaml) is the account, plus every team
and known player named or tagged; tags become `team_tags` evidence for player handles. They use the same source
as persons (`get_instagram_source`): in the hybrid Graph → Bright Data → Apify, so with Bright Data on they cost
Bright Data records (booked in the ledger, inside the monthly cap), not Apify money. Default since 2026-10-30:

| Account | Team link (seeds/teams.yaml) |
|---|---|
| `swissicehockeywomen` | `sui-women` (national team) |
| `postfinance_womensleague` | none (league; posts link to the teams/players they name or tag) |
| `fr_gotteron_ladies` | `fribourg-ladies` |
| `lionsfrauen` | `zsc-frauen` |
| `scb_frauen_official` | `scb-frauen` |
| `swissicehockeyfederation` | none (federation, also men's content) |

~12 posts a day together, ~360 Bright Data records a month (docs/brightdata-vs-apify-2026-10-28.md §6). A live
`.env` that sets `INSTAGRAM_TEAM_ACCOUNTS` itself overrides this list. The nightly cost estimate still counts
them at the Apify price while Apify is configured (safe side); without Apify it is $0.

Graph stops for the rest of the hour (in this process, so across all nightly buckets) when:

- Meta answers with a rate-limit error (codes 4, 17, 32, 613) or its usage headers say 95 % or more,
- the token is expired/invalid (code 190; logged as an error: renew `META_GRAPH_TOKEN`),
- a permission is missing (code 10, 200–299),
- `META_GRAPH_MAX_CALLS_PER_RUN` (default 150) calls were made in the last hour.

Everything after that goes to Apify, inside the normal Apify caps.

**Memory per handle.** After each run the ingest stores in `handles.profile` whether Graph could read the
account: `graph_ok` (true/false) and `graph_checked_at`. A `false` skips Graph for that handle for
`META_GRAPH_RECHECK_DAYS` (default 30; an account may turn professional later). A `true` makes the budget check
count that handle as free. Only handles that already have a profile get the hint.

## Bright Data

Code: `collectors/instagram/brightdata.py` (`BrightDataInstagramSource`, `parse_brightdata_post`,
`parse_brightdata_profile`, `BrightDataQuota`). Smoke test with the real key (8 records):
`with-secrets brightdata -- uv run python scripts/brightdata_smoke.py <handle1> <handle2>` (writes to no
database).

**API** (async): `POST https://api.brightdata.com/datasets/v3/trigger?dataset_id=…&include_errors=true&format=json`
with `{"input": [...], "limit_per_input": n}` and `Authorization: Bearer <token>` → `{"snapshot_id"}`;
`GET /datasets/v3/progress/<id>` until `status` is `ready` (`starting`, `running`, `failed`, `canceled`; poll
every `BRIGHTDATA_POLL_S`, give up after `BRIGHTDATA_TIMEOUT_S`); `GET /datasets/v3/snapshot/<id>?format=json`
(202 = still building, try again).

- Posts: dataset `gd_lk5ns7kz21pck8jpis` with `type=discover_new&discover_by=url`; input per handle
  `{"url": "https://www.instagram.com/<handle>/", "num_of_posts": limit, "start_date": "MM-DD-YYYY",
  "end_date": "MM-DD-YYYY"}` (dates only with `since`; end = tomorrow). Posts older than `since` are dropped
  again locally (start_date is a day).
- Profiles: dataset `gd_l1vikfch901nx3by4`, collect by URL: `{"url": "https://www.instagram.com/<handle>/"}`.
- Mapping (posts): `url`, `shortcode`, `post_id`, `user_posted`, `description` (caption), `hashtags` (with `#`),
  `date_posted`, `likes`, `num_comments`, `content_type` (Carousel/Image/Video/Reel), `alt_text`,
  `tagged_users`, `coauthor_producers`, `is_paid_partnership` / `partnership_details.username`; media from
  `post_content` (per carousel item), else `videos` (+ `videos_duration`) and `photos`. @mentions come from the
  caption. The owner's display name is not in the posts dataset. CDN links, comments and the input echo are
  never stored in `raw` (Bright Data's media links expire after 24 h anyway).
- Mapping (profiles): `account`, `full_name`/`profile_name`, `biography`, `followers`, `posts_count`,
  `is_verified`, `is_business_account` (else `is_professional_account`), `category_name` (else
  `business_category_name`), `is_private`, `external_url` (string or array).
- Error records (`include_errors=true`) carry `error_code` and a text in `error`. `dead_page` = nothing to read
  (the handle counts as read, nothing goes to Apify); the text says why (`dead_page_state`):
  - "This profile is private" → the ingest sets `handles.profile.private = true` (skipped from then on, like a
    private profile Apify found; report field `private`);
  - "Profile isn't available" / not found (renamed or deleted) → the handle goes back to `candidate` with
    evidence `ig_unavailable` (not scraped, not shown; report field `review`, log warning). Check it with
    `klugscheisser handles list --status candidate` (reason "Instagram: Profil nicht verfügbar, bitte prüfen"),
    then `handles verify <id>` (back in use) or `handles reject <id>`; a new handle (renamed account) is added
    with `handles add`. Same mechanism as a Jev doubt; no extra table.
  - "There are no public posts in the profile for the specified period" → just empty, nothing changes.

  Any other code (rate limits) = this handle goes on to Apify.
- Paid partnership: Bright Data never filled `is_paid_partnership` in the test, so a caption marker also sets it
  (`caption_paid_partnership`: `#ad`, `#sponsored`, `#werbung`, `#anzeige`, "Werbung", "Anzeige",
  "paid partnership", "bezahlte Partnerschaft"; whole words only, `#adidas` or "Anzeigetafel" do not count).

**Free records and the cap.** One record = one post or one profile; 5000 free credits per month, renewed on
the 1st; an account without funds gets a hard stop (error) when they are used up. Karl books every record
(error records too, the docs disagree on whether they are billed) in `cost_ledger` with source `brightdata`,
units = records, `cost_usd` = 0 inside `BRIGHTDATA_FREE_RECORDS_PER_MONTH`, else records ×
`BRIGHTDATA_COST_PER_RECORD_USD`. Before each snapshot it reserves the maximum (`limit` per handle for posts, 1
per profile) and only sends the handles that fit under `BRIGHTDATA_MONTHLY_RECORD_CAP` (default 4500, room for
manual runs); the rest goes on to Apify. A snapshot that times out is booked with its reserved maximum. Bright
Data records never count against the Apify caps.

**History loads are capped** (`BRIGHTDATA_HISTORY_MAX_POSTS`, default 50). The tier-1 deep first load and
"Alles neu laden" (`deep.py`, `max_posts=400`) and `klugscheisser instagram-refresh` may ask for more posts per
handle than Bright Data should serve in a month. `BrightDataInstagramSource.fetch_posts` lowers every request to
at most this many posts per handle (`num_of_posts`, `limit_per_input`, the local cut and the monthly
reservation all use the capped number), whoever calls it. Why there: it is the one place every Bright Data
request passes, so no caller can forget it, and the hybrid treats a capped handle as read (the older history is
**not** sent to Apify). Nightly runs (12 per handle) and team accounts (`INSTAGRAM_TEAM_MAX_POSTS` 12) are below
the cap and unchanged. With 50, a month start fits ~90 tier-1 handles instead of 11. Older history of tier-1
persons stays unread when it comes from Bright Data (Graph and Apify are not capped). `instagram-refresh` prints
the Bright Data records left this month and the maximum the run can use before it starts.

**Stop for the run** (`stopped`, no further call; the rest goes to Apify): HTTP 401 (token), 402/403 or an
error text about balance/funds/credits/quota (free records used up), 429 (too many jobs: Bright Data blocks the
IP after 25 × 429 in 5 min), a snapshot failing with "Account is suspended"/"activate". Other failures (5xx,
network, a failed snapshot, timeout) only lose that batch.

What was verified in the Bright Data docs (2026-10-28) and what is assumed: see the PR of
feat/instagram-brightdata. Assumed until the smoke test: the exact error for "free credits used up" (not
documented), the shapes of `tagged_users` / `coauthor_producers` (handled as strings or objects), `post_type`
left out = posts and reels, `start_date` inclusive, the field `discovery_input.url` on discovered posts.

## Costs and caps

- Only the Apify part is charged and booked in the cost ledger (source `apify`), with the number of handles that
  really went to Apify.
- The budget check before each batch counts only handles that will probably go to Apify: all handles not known
  to work on Graph. Handles never tried count as paid, so the very first hybrid run over-estimates (safe side);
  from the second run on, the hints make the estimate accurate. The real spend is still capped by Apify's
  `max_total_charge_usd` = what is left of the daily cap. While Bright Data is on, not stopped and has records
  left this month, the estimate is 0 (Bright Data is expected to serve the rest; if it fails, Apify still runs
  inside that cap).
- The nightly plan leaves out handles with `graph_ok = true` when Graph is configured, and counts Instagram as
  $0 when the chosen source has no Apify part. With Bright Data + Apify it keeps the full Apify estimate (safe
  side; the night may be skipped by the Apify caps as before).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `INSTAGRAM_SOURCE` | `auto` | `auto`, `hybrid`, `apify`, `graph`, `brightdata`, `fixture` |
| `META_GRAPH_TOKEN`, `META_IG_BUSINESS_USER_ID` | – | turn Graph on |
| `META_GRAPH_API_VERSION` | `v24.0` | Graph API version in the URL |
| `META_GRAPH_MAX_CALLS_PER_RUN` | 150 | max Graph calls per rolling hour in one process |
| `META_GRAPH_PAGE_SIZE` | 25 | posts per Graph call; longer histories page with the `after` cursor |
| `META_GRAPH_RECHECK_DAYS` | 30 | ask Graph again for a handle it could not read |
| `BRIGHTDATA_API_TOKEN` | – | turn Bright Data on |
| `BRIGHTDATA_FREE_RECORDS_PER_MONTH` | 5000 | free records per calendar month (booked at $0) |
| `BRIGHTDATA_MONTHLY_RECORD_CAP` | 4500 | no snapshot that could pass this many records in a month |
| `BRIGHTDATA_COST_PER_RECORD_USD` | 0.0015 | price beyond the free records (only for the ledger) |
| `BRIGHTDATA_TIMEOUT_S` | 600 | max wait for one snapshot |
| `BRIGHTDATA_POLL_S` | 10 | wait between progress checks |
| `BRIGHTDATA_BATCH_SIZE` | 20 | profile URLs per snapshot |
| `BRIGHTDATA_HISTORY_MAX_POSTS` | 50 | never more posts per handle from Bright Data (history loads ask for 400) |
| `INSTAGRAM_TEAM_ACCOUNTS` | 6 accounts (above) | official accounts read nightly |
| `APIFY_INSTAGRAM_ACTOR` | `apify/instagram-scraper` | posts and mentions actor; `apidojo/instagram-scraper` is cheaper |
| `APIFY_INSTAGRAM_PROFILE_ACTOR` | `apify/instagram-scraper` | profiles (bios) always use this actor |
| `APIFY_COST_PER_POST_USD` | 0.0027 | price per result for estimates (with apidojo: 0.0005) |

## The cheaper actor `apidojo/instagram-scraper`

About $0.50 per 1000 posts instead of ~$2.70 (needs a paid Apify plan). Input: `startUrls` (profile URLs; for
mentions the profile's `/tagged/` page, one run per handle so each post belongs to that handle), `maxItems` (for
the whole run: limit × handles), `until` (posts newer than this date). What it lacks stays empty: tagged users,
alt text, carousel children (only the first image/video goes to the media queue); hashtags and @mentions are
read from the caption. Profiles stay on `APIFY_INSTAGRAM_PROFILE_ACTOR` because apidojo has no bios.
