# Build plan: X (Twitter) as a source

Status: draft 2026-10-03, waits for "go". Key `X_BEARER_TOKEN` is in `.env` and tested (user lookup + 5 posts of
@SwissIceHockey: HTTP 200, verbatim text, date, permalink; cost ≈ 3.5 cents).

## Facts
- Official X API, pay-per-use since Feb 2026: $0.005 per post read, $0.010 per user lookup; prepaid credits.
- Our DB has 166 X handles from Wikidata (status "wahrscheinlich"), none for the Swiss WM roster yet.
- X's own `lang` field is unreliable (German post tagged `fr`): use our own language detection.

## Steps
1. **Adapter** `collectors/x_api.py` (httpx, no new dependency): user lookup by username (id, name, bio,
   verified), user timeline with `since_id` (only new posts), fields: text, created_at, author, permalink,
   referenced posts. Retweets skipped; quote posts kept with the quoted link.
2. **Handles** (same tiered check as Instagram, never auto-verified): candidates from Wikidata (P2002), links in
   Instagram bios / profile URLs, Fabi's input, official team/federation accounts (e.g. @SwissIceHockey as team
   source). Profile check: display name and bio vs person → `wahrscheinlich` / `verifiziert`.
3. **Ingest**: posts stored as documents (platform `x`, label 𝕏 with source kind: von ihr selbst / offiziell /
   Medien …), verbatim text, Jev classification + topic tags, vectors. Images later via the media queue (optional).
4. **When**: together with the daily Instagram update for persons asked about, in event preparation and in the
   nightly refresh. First read ≤ 20 posts per person, then only new posts.
5. **Costs**: ledger source `x`, daily cap `X_DAILY_COST_CAP_USD` (default 1 USD); estimate shown in event
   preparation. Rough: first read ≈ 11 cents per person, nightly ≈ 1–2 cents per person.
6. **X rules**: show @handle, unmodified text, date and permalink; posts deleted at the source get marked and
   hidden when we see them disappear (weekly check of shown posts, capped).
7. **UI / answers**: 𝕏 lines like Instagram; X handle in the "Datenquellen" box; quotes from posts verbatim.
8. **Tests**: fixtures in the documented response format (from today's real call), fakes; one network test.
9. **Live test**: 5 persons (Swiss players with an X handle + @SwissIceHockey), then decide on nightly use.
