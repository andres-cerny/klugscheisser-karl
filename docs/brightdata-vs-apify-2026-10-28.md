# Bright Data vs Apify for Instagram: real-world test (2026-10-28)

Question: can Karl drop Apify (subscription renews 3 Nov) and read Instagram with Bright Data (later + the free
Meta Graph API)? Baseline: what Apify stored in the live DB `klugscheisser` (read-only queries only), plus one
small fresh Apify run (4 handles, $0.06, not booked in the live ledger) where the stored data was too old to judge.

Scripts: `scripts/brightdata_compare.py` (fetch via Bright Data, hard record budget, raw output outside git) and
`scripts/brightdata_compare_report.py` (comparison against the DB, read-only connection).

## Verdict: yes, with changes

For the regular nightly work Bright Data matches Apify: the same posts (incl. collaboration posts and reels), the same
captions and dates, as good or better tags/alt texts, the same profile data except the category. Expected use
is ~1,700–2,600 records/month on a 4,500 cap, so the free 5,000 are enough **if** the deep/history flows are capped.
What Fabi loses is the **mentions** (posts of others that tag a player but don't list her as a collaborator).
Today these come only from the deep "first load" of tier-1 persons, not from the nightly run. Most of the
valuable ones come from club/league accounts that Karl can follow directly as team accounts.

## 1. The test set (25 handles)

| Group | Handles | Why |
|---|---|---|
| Tier 1, professional ("Athlete") | lara.stalder7, alinamueller_, andrea.braendli, ele_lengwiler, zimmyla | core Swiss players; several collab posts / mentions |
| Tier 1, personal | saskia.m29, kaleigh_quennec (3 collabs), monja_wagner (Apify found nothing since 15 Sep) | personal accounts |
| Tier 1, private | steffi.wetli, wey.ivana | private behaviour |
| Tier 2, many posts | nursey16, jenngardiner12, svejdovaasw, hilaryknight, natspooner5, emasch38 | 8–12 posts in the window |
| Tier 2, many collabs | hayleyscamurra (21 collabs), sandra.ab_30, nataliemlynkova, kakina29 | collaboration posts |
| Tier 2, nothing found / odd | amandakessel28 (816 posts, 0 since 15 Sep), julialiikala (0), lenks47 (private), a.kjellbin (stored profile empty) | empty / dead cases |
| Team account | swissicehockeywomen | `INSTAGRAM_TEAM_ACCOUNTS` |

Window: posts published since 2026-09-15, max 15 per handle (`num_of_posts` + `limit_per_input`).

## 2. Coverage (own feed incl. collabs, vs posts Apify stored for the same handles)

| | Posts |
|---|---|
| found by both | **112** |
| only Apify | 28: 24 = Bright Data's test limit of 15 reached (hayleyscamurra 26 posts in window, swissicehockeywomen 24); 4 = old Apify finds no longer on the profile (see below) |
| only Bright Data | 14: 13 published after Apify's last scrape of that handle (tier 2 is read weekly; team account 4 posts on 28 Oct); 1 reel Apify never stored |

The 4 "only Apify" posts and the 1 "only Bright Data" reel were checked with a **fresh Apify run** (svejdovaasw,
zimmyla, alinamueller_, jenngardiner12; 25 results, $0.0575): **Apify today returns exactly the same 25 posts as
Bright Data.** So the differences come from when each scrape ran (collab removed, post deleted, reel published
later), not from the source.

- Collaboration posts: 43 of the 126 Bright Data posts are owned by another account, with the player in
  `coauthor_producers` (clubs, NHL, PWHL, sponsors). Same as Apify.
- Reels: 65 of 126 are reels (`product_type=clips`); both sources include them.
- Pinned posts: no old pinned post came back (0 records outside the window): `start_date` works.
- Empty / private / gone: one error row per handle, all with `error_code=dead_page`, the text tells them apart:
  "There are no public posts in the profile for the specified period" (monja_wagner, amandakessel28,
  julialiikala, lenks47), "This profile is private" (steffi.wetli, wey.ivana), "Profile isn't available"
  (a.kjellbin: renamed or deleted; Apify's stored profile for it is empty too).

## 3. Field quality on the 112 matched posts

| Field | Result |
|---|---|
| caption | 112/112 identical |
| date | 112/112 identical (to the second) |
| tagged users | 108/112 identical; Bright Data has all of Apify's tags in 111/112 (the extra tags were added after Apify's scrape) |
| coauthors | 108/112 identical (52 vs 51 posts with coauthors) |
| alt text | present in all 112; identical text, Bright Data often adds Instagram's "May be an image of …" part |
| media type | 112/112 same (image / video / carousel); media links present for all 112; carousel item count = Apify's analysed count in 50/50 comparable cases |
| likes | Bright Data higher by the growth since Apify's scrape; hidden like counts: Apify -1, Bright Data `null` |
| comments | 100/112 within 10 % (rest = growth) |
| paid partnership | **lost**: Bright Data never fills `is_paid_partnership` / `partnership_details` (0 of 126); Apify flagged 2 of the 112 |
| owner display name | not in the posts dataset (known; profiles have it) |

Media links are Instagram CDN links with the same lifetime as Apify's (32–108 h, not 24 h); the media queue is
drained in the background within hours, so this is not a problem.

## 4. Profiles (24 + 1 gone)

- followers / posts_count / bio / private / verified / external URL: equal to the stored Apify profile (followers
  within ±1 % except normal growth; bio 23/23 identical; Apify had `posts_count` missing for 4 accounts, Bright
  Data has it for all).
- **category is never filled** (`category_name` / `business_category_name` empty in all 34 profiles of this test),
  and `is_business_account` is always `null`. Apify had "Athlete" for 12 of these 24. `apply_profile` keeps the old
  category only for Graph, so the weekly refresh **would wipe the stored categories** (fix below).
- Bonus: every profile record carries the **12 latest posts** (full caption, URL, date by day, type; also collab
  posts). For 1 record this is a cheap "anything new?" check (see the optional change below).

## 5. Records, billing, run time

| Snapshot | Inputs | Rows | Data records (`dataset_size`) | Errors | Time |
|---|---|---|---|---|---|
| profiles | 25 | 25 | 24 | 1 | 204 s |
| posts batch 1 | 13 | 56 | 53 | 3 | 156 s |
| posts batch 2 | 12 | 77 | 73 | 4 | 113 s |
| profiles of 10 club/league accounts | 10 | 10 | 10 | 0 | 124 s |

- 1 record = 1 post or 1 profile: confirmed (posts were never more than the posts delivered; profiles 1 each).
- Error rows: the snapshot list API reports them separately (`errors`) from `dataset_size`. Whether they are
  billed is not visible via the API (the token may not read the balance). **Check in the Bright Data dashboard:**
  this month's usage should read **168** if error rows are free, **176** if they are billed (8 smoke-test
  records + 160 data rows + 8 error rows of this test).
- Records used by this test: 160 data + 8 error rows = **168** (budget was 600). Month total incl. this morning's
  smoke test: 176 at most.
- No 429 or timeout. A snapshot takes 2–3.5 min (8–12 s per input). For a web/MCP refresh that is slower than
  Apify (~1 min), but those already run in the background.

## 6. What the mentions give today (Apify "mentions" = posts of others tagging a player)

Mentions are fetched **only** by the deep run for tier-1 persons (`deep.social_for_persons`: first load 730 days, 50
per person; "Alles neu laden" 30 days). The nightly run never fetches them. In the live DB:

- Published in the last 60 days: **66 mention items** from 40 accounts, found for 14 players (12 tier 1, 2 tier 2 at the time), 37 persons linked.
  All were scraped in one deep run on 2026-10-04. Of the 66: 44 official, 8 fan, 8 media, 6 brand.
- Collaboration posts (owner = someone else, player = coauthor) are a separate group: 145 in the last 60 days. They
  are **not** lost: Bright Data returns them in the player's own feed.
- Sample of 14 mentions, judged for Fabi:
  - useful: SCSU Huskies "Laura Zimmermann 100th career game" (club, citable milestone);
    swissicehockeywomen "Sinja Leemann mit dem Ausgleich" (already covered: we follow that account);
    Swiss Ice Hockey Federation "Switzerland 2038" with Kaleigh Quennec; PostFinance Women's League media day / player
    features; RSI-style interview by ac_taverne with Nicole Vallario on the Olympic bronze (media, interview quotes);
    EVZ / HCD / Lausanne / Gottéron Ladies / SCB Frauen club posts.
  - low value: game photos by photographers, fans' edits, kitesurfing posts (kiteclasses/swisssailing, 15 of the
    44 "official" ones are about Elena Lengwiler's kite racing, not hockey).
  - Roughly **20 hockey-relevant official mentions in 60 days**; 6 of them come from swissicehockeywomen,
    which is already followed.
- The posting clubs/leagues can be followed directly. Posts per day (from their profile records, 10 records):
  postfinance_womensleague ~2, fr_gotteron_ladies ~2.7, lionsfrauen ~2.7, scb_frauen_official ~3 (burst),
  swissicehockeyfederation ~0.4, scsuhuskies_whk ~2.7; men's-dominated club accounts (official_evz ~8, hcd_official
  ~2, lausannehc_officiel ~4) are mostly men's content: not worth the records.
  Today only 1 of 393 teams has an Instagram handle (`sui-women` → swissicehockeywomen); team accounts come from
  `INSTAGRAM_TEAM_ACCOUNTS` (+ `seeds/teams.yaml` `instagram_handle` for the team link).

**What Fabi loses:** posts by clubs, colleges, media and sponsors that tag a player without making her a
collaborator, e.g. club milestone posts, an interview clip on a media account. Following the Swiss women's
league and club accounts brings most of the Swiss ones back, and some extra content on top. Mentions by foreign
colleges/media for players abroad (e.g. SCSU for Zimmermann) are only partly replaced (one more account each).

## 7. Projected monthly Bright Data use

Last 30 days of Apify results (cost / $0.0027 per result; profiles = units):

| Flow | Apify volume | Bright Data records/month |
|---|---|---|
| nightly posts (steady 20–28 Oct) | 35.7/day | ~1,070 |
| profiles (7-day refresh) | 18.9/day | ~570 |
| team account swissicehockeywomen | 1.9/day | ~60 |
| **regular total** | | **~1,700** |
| error rows for handles with nothing new, if billed (~20–25 of ~40 handles per night) | | +0–750 |
| bursts (full night run 15 Oct: 316; web/add-person day 19 Oct: 235) | | ~+550 |
| 5 extra league/club accounts (~12 posts/day) | | ~+360 |
| **expected range** | | **~2,250–3,350** of the 4,500 cap (5,000 free) |
| deep first load tier 1 (4 Oct: 2,725 posts + 166 profiles, `max_posts=400`) | | **would use 58–64 % of the month alone** |
| `klugscheisser instagram-refresh` (8 Oct: 1,588 posts, 12 latest per account) | | **~1,600** |

So the regular flows fit with a good margin; the history/backfill flows do not and must be capped for Bright Data.
The monthly cap guard (`BrightDataQuota`, reserve `limit` per handle) already stops before 4,500, but with
`max_posts=400` it reserves 400 per tier-1 handle (only 11 handles fit even at the start of a month). Without
Apify the rest is silently `skipped_budget`.

## 8. Required changes before cancelling Apify

Status 2026-10-30 (branch feat/instagram-brightdata): 2, 3 (as `BRIGHTDATA_HISTORY_MAX_POSTS`, default 50, for
every Bright Data request), 4 and 5 are done, and the paid-partnership caption markers; see
docs/instagram-sources.md. Open: 1 (go-live) and 6 (dashboard check).

1. **Merge and deploy PRs #50 + #52** with `BRIGHTDATA_API_TOKEN` in the live `.env` (today live runs Apify only),
   then remove `APIFY_TOKEN`.
2. **Keep the stored category** when the profile comes from Bright Data (`handles/profile.py` `apply_profile`:
   treat `source == "brightdata"` like Graph for `category`), else the weekly refresh wipes "Athlete" etc.
3. **Cap history flows for Bright Data:** deep tier-1 first load `max_posts=400` → e.g. 30–50 per person when the
   source has no Apify; `instagram-refresh` CLI: refuse or cap (e.g. `--max-posts 3`, tier 1 only) without Apify.
   Show "Bright Data records left this month" before these flows run.
4. **Read the `dead_page` text:** "Profile isn't available" → mark the handle for review (renamed/deleted; today it
   silently counts as read-empty); "This profile is private" → set `profile.private`; "no public posts …" →
   empty, fine as now.
5. **Add league/club accounts** to `INSTAGRAM_TEAM_ACCOUNTS` to replace most mentions: postfinance_womensleague,
   fr_gotteron_ladies, lionsfrauen, scb_frauen_official, swissicehockeyfederation (~360 records/month).
   Optionally the colleges/clubs abroad of tier-1 players (scsuhuskies_whk).
6. **Check the dashboard once** (168 vs 176, see §5) to know whether error rows count; if they do, plan with
   the upper range.

Nice to have:

- Paid partnership is lost: let the classifier read "Anzeige / #ad / bezahlte Partnerschaft" from the caption if
  it matters.
- Records saver: fetch the profile (1 record, 12 latest posts) first and run the posts snapshot only for handles
  with unknown shortcodes; it also makes the weekly profile refresh nearly free. Worth it only if usage gets close
  to the cap.
- Bright Data concurrency: the code stops the run on 429; nightly snapshots run one after the other, so this did
  not happen. Parallel web refreshes could hit it; they then fall back to "not read this time".

## Records and money used by this test

- Bright Data: 168 rows (160 data records + 8 error rows) of the 600 budget; month total ≤176 of 5,000.
- Apify: 1 run, 25 results, $0.0575 (cap $0.30), not booked in the live cost ledger.
- Live DB: SELECT only (read-only connection). No ingest, nothing written.
