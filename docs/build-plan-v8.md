# Build plan v8: tiers, deeper sources for the people that matter

Status: **built 2026-10-04** (go from Ondřej). Deviations and details: see "As built" at the end.

## Why
- Most of what we know comes from Wikipedia/Wikidata: of 2,432 fun facts, ~2,400 come from there and only 8
  from Instagram/X. Fabi can read Wikipedia herself; she needs what she cannot find easily: social media,
  boulevard and local media, older interviews.
- Coverage is thin exactly where it matters (counts from 2026-10-04):
  - Swiss WM/Olympia roster + coach (24): median 3 non-Wikipedia documents per person.
  - Other Swiss national players since 2024 (8): 7 have nothing outside Wikipedia.
  - Other WM nations (~118 found, ~230 real): 111 have nothing outside Wikipedia; 1 verified Instagram handle.
  - Only 1 coach in the database; Olympia/WM rosters only for Switzerland, without jersey numbers.
- Wikipedia quotes in Finnish, French, Swedish, Czech, Danish (~1,300 quotes, ~400 persons): Fabi reads
  German and English only.

## 1. Tiers
- **Tier 1:** Swiss national roster (WM/Olympia), Swiss WM candidates (national team since 2024), Swiss
  coaches and staff (~32 persons). Gets the deep treatment.
- **Tier 2:** players, coaches and staff of the other WM teams (~230). Gets a light treatment.
- **Tier 3:** everyone else. Unchanged.
- Set automatically from rosters and national-team stints; Fabi can move a person up or down on the person
  page (manual choice wins).
- Shown next to the name in search, person page and rosters: "Tier 1 · 64 Quellen" (Quellen = stored
  documents, each with its URL). Tier 3: no badge.

## 2. IIHF rosters, coaches and staff, jersey numbers
- Import the official rosters of the WM 2026 and Olympia 2026 teams (public IIHF pages/PDFs): players with
  position and **national-team number**, head coaches, assistants, staff. New persons go through the usual
  Wikidata + Jev namesake check.
- Club numbers from club/league roster pages (EVZ, SIHF, PWHL, SDHL); show both ("#7 EVZ · #… Nati") on the
  person card, quick summary and rosters.

## 3. Handles
- Check the "wahrscheinlich" Instagram handles of tiers 1–2 (6 for tier 1, 72+ for tier 2) with the existing
  evidence scoring; only verified handles are scraped.
- Facebook: Brave `site:facebook.com "name"` per tier 1 person (~$0.16); scrape only real own pages.

## 4. Backfill per tier (one-time, then on demand)

| | Tier 1 | Tier 2 |
|---|---|---|
| Brave (web + news, own language, `site:` queries for boulevard/local outlets) | 30 queries | 4 queries |
| Instagram own posts | full history; images analysed; reels only when they look like an interview (see below) | latest 10, images only |
| Instagram tagged posts | latest 50 from the last 2 years | none |
| Facebook (Apify, $0.005/post) | latest 30 where a page exists, text only (no image/video analysis) | none |
| X | light test: 4 handles × 20 posts + name search 10 posts for ~10 players | none |
| Fun facts | 5, at least 1 from social media (if there is one; never invented) | 3 |

- Every hit: fetch the full article → Jev: right person? main person? relevant? → store with its date.
  Failed hits are stored unlinked, never deleted.
- **Gemini only after Jev passed** (fact extraction and image analysis), fan accounts caption only.
- **Reels: only interviews.** What matters is a reel where she talks about herself, not training clips. Jev
  pre-checks caption + account kind ("Interview", "Q&A", "erzählt", media/club/sponsor account, podcast
  clip) → only then Gemini watches/listens to the reel (≤ 3 min). Other reels: caption only.
- GDELT tested and dropped (rate limits, 0 results); SMD not used (terms).

## 4b. Podcasts and videos (links only, tier 1)
- Per tier 1 person 2 extra Brave queries (video search + "name" podcast/Interview), plus all YouTube,
  Spotify/Apple Podcasts, SRF Audio/Play hits from the backfill.
- Stored as links only: title, outlet/channel, date, length, link. No download, no transcript.
- Jev checks title + description: is it her, and does she appear/talk (not only mentioned)?
- Person page: section "🎧 Podcasts & Videos", newest first, so Fabi can watch or listen if she wants.
- Cost: ~64 Brave queries ≈ $0.32.

## 5. News: wider gate + identity check
- Boulevard/local outlets: fetch the full text of sport/people articles, match names on the full text,
  including surnames of tier 1–2 persons.
- Jev identity check on the full article for every linked person (today news are linked by name only; a
  namesake outside our database is not caught).
- Outlets found by the backfill (e.g. Engadiner Post, RTR) are candidates for new feeds.

## 6. Jev relevance fix
- Add private life, family, hobbies, personal stories to the "relevant" criteria (a Watson piece on the
  players' partners at the WM scored 0.05).

## 6b. Fun-fact update
- After each backfill step (and after "Alles neu laden"), fun facts are regenerated only for persons with new
  documents: tier 1 → 5 (at least 1 from social media if there is one), tier 2 → 3, others unchanged.
- Old fun facts are replaced; new ones keep source link + verbatim quote (+ German translation, see 7).
- Cost: ~$0.002 per person (more input than before) → ~300 persons ≈ $0.60; all 1,378 ≈ $2–3.
- Fun-fact cost gets its own ledger label (today it is booked as "frage").

## 7. Translations
- Quotes from sources not in German or English: original stays (proof), German translation below, marked
  "automatisch übersetzt". Made once with Flash-Lite and stored. English sources stay English.

## 8. "Alles neu laden"
- Button on the person page (and per event): reload everything for that person according to their tier.
  Scrapes stay on demand; nothing new runs on a schedule.

## 9. Cost tracking and limits
- Media analysis writes its real token cost to the cost ledger (today: $0 recorded).
- The daily media limit (150 analyses) is lifted for the backfill when it is run through Claude Code; the
  dollar caps stay.

## 10. Budget
Estimate ~$31–33 for the one-time backfill; **hard cap $40** (room for estimate errors, so the run does not stop
halfway). Tier 1 runs first, then tier 2. Jev is not part of the cap (not priced in the ledger).

| Item | Estimate |
|---|---|
| Tier 1: Brave 30 q ($4.80), Instagram own + tagged (~$8), Facebook (~$2), X (~$1) | ~$16 |
| Tier 1: Gemini (images, interview reels only, facts, only after Jev) | ~$3–4 |
| Tier 2: Brave 4 q ($4.60), Instagram 10 posts (~$4) | ~$9 |
| Tier 2: Gemini (images, facts, only after Jev) | ~$3 |
| Fun-fact update (persons with new documents, ~300) + translations | ~$1–2 |
| **Total** | **~$31–33 estimated, hard cap $40** |

Jev not included (no price in the config; check with TypeSafe). Estimates are rough (±30%); the ledger shows
the real numbers after the run.

## Order
1. Tiers + badge + manual override; IIHF rosters, coaches, numbers.
2. Handle checks; Facebook page check.
3. Jev relevance fix; identity check on news; wider gate.
4. Backfill tier 1 → fun facts tier 1 → look at the results with Ondřej.
5. Backfill tier 2 (within the remaining budget).
6. Translations; "Alles neu laden"; media cost tracking.

## Not in v8
- Instagram history and tagged posts for tier 2; reels for tier 2; non-interview reels; Facebook media.
- YouTube/podcast transcripts or downloads (links only, see 4b).
- Scheduled scrapes.

## As built (2026-10-04)

- **Rosters:** the IIHF roster PDFs sit behind bot protection, so `seed rosters-wikipedia` reads the English
  Wikipedia roster page of Olympia 2026 (which cites the IIHF PDFs; both links kept): 10 teams × 23 players with
  number, position, birth date, club, captaincy, plus the head coach, QIDs via Wikipedia pageprops →
  `seeds/rosters/oly-2026-<nat>.yaml`. Current Swiss squad from SIHF (Euro Hockey Tour Lahti, Sept. 2026, head
  coach Shannon Miller, 4 withdrawals kept as "called up"); the provisional WM roster is based on it, numbers
  from the newest roster with numbers. WM 2026 rosters are not published yet; DEN/AUT come in via national stints.
  Assistant coaches/staff: not in any open source we may read; head coaches only.
- **Tiers** (`tiers.py`): 45 / 232 / 1,170 after import. Manual choice on the person page ("Tiefe der Suche").
- **Club numbers** (`numbers.py`): club Wikipedia rosters, only when one of our players is on them (free).
- **Handles** (`handles/check.py`): Jev reads profile + captions ("eigener Account?"): ≥0.85 adds evidence
  (→ verified with name + bio), ≤0.15 sets the handle back to candidate. Facebook via Brave + same check.
- **Terms respected:** Blick, 20 Minuten, Watson (and other outlets with `fetch_article: false`) forbid automatic
  reading of articles. Their search hits are stored as title + teaser + link (after the Jev check), never fetched.
- **News identity check** (mode "meant"): calibrated live — players named in match reports 0.71–0.90, namesakes
  0.02–0.05 → threshold 0.5. Surnames of tier 1/2 persons open the news gate (unique, ≥5 letters, not common).
- **Media rules** (`media/policy.py`) apply to every ingest, not only the backfill. Media cost is now recorded
  from the token counts (estimate $0.50/$3.00 per M tokens until checked against the bill).
- **Translations** (`translate.py`, table `translations`): card intro, fun-fact quotes, answer quotes, item
  summaries/titles; made once, cost "llm:gemini-translate". The card intro now prefers German/English Wikipedia.
- **CLI:** `klugscheisser deep --tiers 1,2 --cap 40` (steps handles, social, facebook, x, web, media, after).
