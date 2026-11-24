# Build plan v5: work with Perplexity, bigger database, fast answers

Status: draft 2026-10-03 from the discussion with Ondřej, **not started** (waits for "go").

## Direction
- We do not compete with Perplexity on open web search. Perplexity: general web and news (tested by Ondřej:
  good on recent news and on "who studies" via NCAA players). We: what Perplexity cannot see or aggregate —
  Instagram (own + tagged posts, image/reel analysis), X, national news outside the English-speaking web, rosters
  incl. coaches/staff, official results, verified handles, namesake protection.
- The Perplexity connector reads our database; our web app stays (search, chats, adding people).
- No heavy structuring: collect broadly, Jev annotates, Gemini answers with verbatim quotes.
- Not in v5: Muse Spark (Meta). LinkedIn (terms forbid scraping). Japanese sources (NHK) — acceptable loss.

## Findings that drive the plan (2026-10-03)
- Answers were slow (10–34 s, up to ~60 s) because everything runs in sequence: waiting for the daily Instagram
  update, news/results refresh in the request, Jev rerank in batches of 20, Gemini, then web search with up to 4
  sequential page fetches and a namesake check per page **per person in scope** (23 players → up to ~90 Jev calls).
- "No answer" cases: weak web query (raw question + "Sport"), no coaches/staff in the DB, no past results,
  engine is person-centric. Example: the SRF article naming coach Colin Muller was already stored, but he was not
  a person yet, so "Is the coach announced?" found nothing.
- Vectors are placeholders (`fake-hash-384` on all 164 documents): semantic search is effectively word matching.
- Full-text search is German-only; foreign-language texts would be searched badly.
- Wikipedia profiles only in de/en/fr (25 en, 22 de, 4 fr, no sv/fi/cs …), although the athlete's home-language
  article is usually the best. Team rosters only en/de.
- Team links: national-team cue words exist only for Switzerland; club names are matched as substrings and
  names ≤ 6 characters are skipped (EVZ, ZSC, SCB never match).
- Gemini billing is on (42 Flash-Lite requests today without a quota error; free tier stops at 20/model/day).
- Database today: 12 MB. Storing everything (~2,000 articles/day with v5 feeds) ≈ 5–8 GB/year (estimate).

## Phases (suggested order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8)

### 1. Fast answers from our database
- No web search in the answer path (web search moves to the preparation job, phase 4).
- No waiting for the daily Instagram update in a question: answer right away from the DB, show "Update läuft"
  (web app) / `refresh_status` hint (connector).
- Jev rerank in parallel; news/results refresh only in the background.
- Log time per step (scope, candidates, rerank, Gemini, checks) to measure, not guess. Goal: < ~10 s.
- Answer quality fixes seen in testing: no generic "Lücken" (relationship/health) for unrelated questions;
  review why clearly supporting quotes end up "Unbestätigt" (support threshold / Jev phrasing).

### 2. Storage and search
- Postgres stays (relational + full text + pgvector in one database; no separate vector DB at this size).
- Layers: entities (persons, aliases, handles, teams, affiliations, events, rosters) · raw documents (never
  altered; quotes only from here) · Jev annotations · search indexes.
- **Real multilingual vectors** (Gemini embeddings, billing on); re-embed all documents.
- **Multilingual full text** (language-neutral config or per-language configs) next to the German one.
- **Two storage levels, nothing deleted:**
  - light level for every fetched article: title, teaser, link, date, source, language (cheap; "already seen";
    searchable later when new names are added — `link_existing_items` already re-links stored news);
  - full level only after the pre-filter (a known name/alias, team or the sport appears): full text where the
    outlet allows it, Jev annotation, vector, image/reel analysis (media only for relevant documents).
- Switch `NEWS_FETCH_ALL_ARTICLES` back off after testing (teaser only where terms forbid full articles).

### 3. Jev annotations for cross-person questions
- Same Jev call as the existing classification, extra yes/no topic tags per document: studies, job, injury,
  comeback, transfer, family in sport, milestone, relationship (only when the person says it herself), plus
  per linked person "Hauptperson oder nur erwähnt?".
- Cross-person questions ("Wer studiert?") = SQL filter by persons in scope + topic tag → small, complete set →
  Gemini with quotes, plus "keine Angaben zu: …" for the rest.
- Re-annotate stored documents once.
- Optional later: a fact table (extracted statements with quote) only for question types that come up often.

### 4. Bigger, broader people database
- **Event preparation** in the app (e.g. "WM 2026, Herning, 6.–16.11."): all rosters incl. coaches and staff,
  persons added, deep Instagram load, profiles, past results, web search with good queries + namesake check (slow
  is fine here), all stored as documents with sources. Cost estimate + ✅ Ja first.
  Rough cost: ~0.11 USD per person first Instagram load → Swiss team ~3 USD, all 10 WM teams ~30 USD (estimate).
- **Nightly refresh** for an event's persons in a date range (from–to), with a daily USD cap.
- **Wikidata bulk seeding** (free): women's national-team ice hockey players and alpine skiers of the target
  nations.
- **Wikipedia by nationality:** take all language articles Wikidata links for the person, home language first
  (sv, fi, cs, da, nb, it, fr, sl, hr, sk, pl, de, en; Swiss: de/fr/it), always also de + en. Rosters too from the
  home-language Wikipedia (often more current). Answers quote the original and give the German meaning.
- Coaches of women's teams recognised correctly (Colin Muller was labelled "Eishockey Männer").

### 5. Team articles
- Person questions also consider articles about **her teams** in that period (ranked lower; the answer says
  "Bericht über das Team" and never claims anything about her personally that is not in the text).
- National-team cue words per nation and language (Damkronorna, Naisleijonat, …), not only Switzerland.
- Team matching at word boundaries with **explicit aliases per team** (EVZ, EV Zug, ZSC Lions Frauen, SCB …);
  drop the length rule. Ambiguous short names (Zug, Bern) only with ice hockey context or a Jev decision.

### 6. Perplexity connector
- Tools for "person missing → propose (Wikidata + Jev, ~2–5 s) → Fabi says ja → load (Instagram ~30–60 s,
  media analysis minutes later)" and status; same flow as the web app (Ja/Nein/Präzisieren, 5 CHF cap).
- Without a known handle: optional Instagram name search via Apify, result only "wahrscheinlich" with reason.
- Re-add the connector in Perplexity and test with Fabi's real questions.

### 7. News: Swiss regional + public broadcasters
Terms and robots.txt checked per source; **the list goes to Ondřej before building**. Label e.g.
📺 (öffentlich-rechtlich, SE); answers quote the original and summarise in German; Jev quote check on the original.
- Switzerland: RTS Sport, RSI Sport; regional: Luzerner/Zuger Zeitung, Berner Zeitung, Tagblatt, Bündner
  Tagblatt, 24 heures / Tribune de Genève, Le Matin, Le Temps; club and federation news (SIHF, ZSC, EVZ, SCB, HCD).
- Ice hockey WM nations: CBC / Radio-Canada, SVT, Yle, ČT sport, ARD Sportschau / ZDF, ORF, DR; USA: federation
  news (labelled as federation, not independent). Japan: left out.
- Downhill nations: ORF, RAI Sport, France Télévisions sport, NRK, RTV Slovenija (+ the above).
- Other European nations: HRT (CRO), STVR (SVK), TVP Sport (POL), Liechtensteiner Vaterland / Radio Liechtenstein,
  RTVA (AND), RTVE (ESP), RTBF / VRT (BEL), NOS (NED), BBC Sport (GBR), MTVA / M4 Sport (HUN).
- Independence of some public broadcasters is questioned (e.g. HU, SK): the label names broadcaster and country;
  no political judgement in code.

### 8. X via the Grok API
- xAI API key with small prepaid credit (subscription SuperGrok does not include API use). `XAI_API_KEY` exists.
- Test with 5 players: does X Search return post links and original text we can store and quote?
  If not, or if few players are active on X: drop it.
- If yes: X as a source with label 𝕏, nightly for event persons. Cost ≈ 1 cent per person per run
  (X Search $5 / 1,000 calls + Grok 4.1 Fast tokens; estimate, verify on xAI's price page).

### 9. Skiing (after ice hockey works)
- Downhill women and men as sports (`ski_alpine_women`, `ski_alpine_men`; the generic sport layer handles new
  sports without code changes).
- Check FIS data and terms for results history (venue history, podiums); only then build a collector.

## Alongside
- Gemini billing on: raise the media-analysis limit (now 60/day) and set a daily USD cap.
- Fabi's real example questions as a test set for phases 1, 3 and 6.

## Costs (rough, to confirm when built)
- Event preparation: Swiss team ~3 USD, all WM teams ~30 USD once.
- Nightly: Instagram a few USD for all WM teams, X ~3 USD for 280 persons, Jev and vectors cents.
- Storage: a few GB per year.
