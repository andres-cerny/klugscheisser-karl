# Build plan v6: questions across people and teams, feedback, game brief

Status: draft 2026-10-03, discussed with Ondřej, **not started** (waits for "go").

## Why (test of 2026-10-03)
- "Wer ist die Älteste im Olympia-Team?" / "Wer hat die meisten Instagram-Follower im Team?" → "Keine Angabe",
  although birth dates (23/23) and follower counts exist.
  - The engine ranks documents and takes the top 15: questions that need one fact from *every* person can
    never be complete that way (design gap, not missing data).
  - "the team" / "Olympia-Team" was not understood as a scope (only person names were).
  - Stored data (birth dates, followers) is not readable by the answer step; Wikipedia existed only for 25 of
    1,378 persons (bug fixed 2026-10-03, backfill running).
- "Was hat die Trainerin vor der Euro Hockey Tour gesagt?" → there is no such quote in our sources, but the
  answer only said "Keine Angabe" instead of what was searched, what was found and why it is not enough.

## Order

### 1. Ready for Fabi: feedback, saved chats, honest "no answer" (first, so she can start testing)
- Chats are already stored on the server (every question and answer, shared across devices).
- **Rating per answer:** 👍 gut · 🙂 okay · 👎 schlecht + optional free-text reason; stored with question,
  scope, answer, sources, timings and cost. Review list for Ondřej (`/bewertungen`), export as test set.
- Same rating in the connector? Not possible inside Perplexity; the app is the place for ratings.
- **Honest "no answer"** (from the paused stash): what was searched (scope, number of documents), the closest
  sources (title + link), what is missing, unconfirmed statements with the reason.
- **Scope from the question** (from the stash): "Nati", "Olympia-Team", "WM-Kader", "the team" → that roster;
  assumptions are stated ("Angenommen: Schweizer Frauen-Nationalteam, 23 Personen").
- **Answer cache:** same question + same scope answers instantly ("aus Cache, Stand …", 🔄 to renew);
  invalidated when new documents arrive for those persons.
- Fabi's ~30 real questions become the first test set (with her ratings).

### 2. Fact layer (extracted once at ingest)
- Fields (each with source, quote, date/season, confidence, status confirmed/conflict):
  - identity: name variants/maiden name, nickname, birth date and place, hometown, height, shoots/catches,
    languages she speaks (interview language);
  - career: **club history with seasons** (club, league, from–to, role incl. captain), youth clubs, college
    (school, major, years), draft, national team (debut, tournaments with result/medal, appearances), awards,
    milestones, retirement/comeback, **season stats** (from SIHF/PWHL/SDHL);
  - off the ice: studies, job, family in sport, engagement, sponsors (paid partnerships), injuries (⚠️ always
    Vorsicht, only with source);
  - accounts/links: Wikidata QID, Wikipedia URL per language, federation/college profile URL, EliteProspects
    (link only), league player IDs, Instagram/X follower counts over time;
  - coaches/staff: role, team, since, earlier stations.
  - derived on the fly, not stored: "played together", "same college", "same hometown".
- Shape: typed tables for frequent joins (`career_stints`, national-team tournaments, season stats) + one
  general fact table for the rest.
- Sources: Wikidata directly (memberships with dates, birth date); Gemini Flash-Lite extracts from Wikipedia,
  college bios, federation pages, news — **each fact with a verbatim quote + document link**, checked like
  answers (quote in text, Jev support check).
- Conflicts kept side by side; newer/official source preferred and the conflict shown.
- Clubs normalised to our team entities (aliases) so "ZSC Lions Frauen" = "ZSC Lions Women".
- Backfill over all stored documents once; new documents on ingest. Rough cost: ~0.1 cent per document.

### 3. Questions across people and teams
- Jev routes the question: **open question** (as today: rank → Gemini with quotes) or **across
  people/teams** (scope → facts of *every* person → compare/list, a source per line, "kein Wert für: …").
- Cross-team: e.g. Sweden vs Switzerland "wer hat schon zusammen gespielt?" = overlapping seasons at the same
  club, college or tournament team, sources for both sides.

#### How questions across people run (agreed 2026-10-03)
- **Step 0 – understand (Gemini → JSON form, not SQL):** type (sorted list / compare / relation / open per
  person), scope (game, event, team, nation, role), asked (a known fact type or a free topic), time window,
  extra facts.
- **Four general building blocks, written once by us; the code picks them deterministically from the form:**
  1. *Who?* — persons by game (both rosters), event, team, nation, role.
  2. *Fact X for these persons* — one template for every fact type (the type is a parameter).
  3. *What do they share?* — overlaps: same club in the same season, same college, same hometown.
  4. *Evidence per person on a free topic* — one SQL query (LATERAL, pgvector + full text + topic tags) returns
     each person's top 3 own documents; Jev checks each (person, document) pair; Gemini writes one sourced line
     per person with evidence; "keine Belege für: …" for the rest. Safety net for every question nobody
     anticipated ("gut in Form?", "emotional?"). Evidence, not verdicts.
- Free SQL by Gemini only when no block fits: read-only views with source columns, own DB role, read-only
  transaction, 5 s timeout, row limit, EXPLAIN check + one retry; SQL logged with the answer.
- Frequent block-4 topics (seen in ratings/logs) become new fact types later.
- Example "Wer ist vor dem Spiel morgen gegen Tschechien gut in Form?": game → 2 rosters (46 persons) →
  block 2 (points in last games, open injuries) + block 4 (last 14 days, topic "Form") → Jev → one line per
  person, ⚠️ Vorsicht first.

#### Speed (measured 2026-10-03: SQL ≤0.05 s, question vector + candidates 0.4–0.6 s, Jev 0.4 s,
Gemini answer + quote check 4–7 s → the language model is the bottleneck, not SQL)
1. Fact answers rendered directly from SQL rows (no LLM): < 1 s.
2. Stream answer lines in the web app as they are written: each line is shown only after its quote check
   (verbatim in source + Jev support); failed lines never appear. Not possible in Perplexity (a tool result
   arrives complete).
3. Cross-person: Gemini in parallel groups of ~10 persons (paid-tier rate limits; check the exact limit in
   the Google console). Helps the app and the connector.
4. *Later, after talking to Fabi:* prepared events with per-person evidence on typical topics precomputed
   nightly.
5. Answer cache.
Targets: fact questions < 1 s; open single-person 3–6 s; open cross-person first lines 3–5 s, complete ~8–10 s.

### 4. Profile card on the person page
- Photo from Wikimedia Commons (with author + licence; 60 of 129 Swiss players have one), number, position,
  age, nation, current club ("laut Quelle, Stand …"), club history table, Wikipedia intro as a verbatim quote.
- No Instagram profile pictures (copyright, expiring links).

### 5. Game brief
- "Tschechien – Schweiz morgen" → both rosters (provisional/confirmed), per player one line (number, position,
  age, club, latest news/post, ⚠️ Vorsicht), connections between the teams, recent results between them.
- Every name links to the profile card; follow-up questions in the chat there.
- Structure from facts (deterministic); short summaries by Gemini with quotes.

### 6. Fun facts per player (requested 2026-10-03)
- Up to 5 short, surprising, broadcast-friendly points per person (card shows the top 3, "mehr" for the rest) ("Mutter aus der Slowakei — morgen gegen die
  Slowakei", "spielt links, schiesst rechts", an interview anecdote), shown under the profile card summary and
  as one line in the game brief.
- Sources: Wikipedia, interviews/news, Instagram/X captions + image analysis, the fact table. **Every sentence
  with link + verbatim quote**, same checks (quote in source, Jev support).
- How: extra Jev topic tag at ingest ("Anekdote / ungewöhnlich / persönlich interessant"); once per person
  Gemini Flash-Lite reads all her tagged documents (up to ~20, best first) plus Wikipedia, interviews,
  Instagram/X captions with image analysis and the fact table, and picks the best up to 5; stored **statically**, renewed only when a
  clearly new strong document arrives (newsworthiness 4–5).
- Sensitive topics (relationships, family, health) only when she says it herself or a reputable outlet reports
  it — always ⚠️ Vorsicht; tabloid items stay 🍿 Gossip with ⚠️.
- Rough cost: 0.3–0.8 cents per person (a few dollars once for the whole database).
- **What counts (added 2026-10-03, built):** anything that genuinely works as a fun fact for the
  viewers and gives Fabi a hook for the interview or a way to relate to the player. Kept open on purpose, no
  fixed category list. Examples only: other sports she played, her studies or job next to hockey, history or
  background, hobbies, a pet, and hockey itself (a really remarkable statistic, a record, a special career
  story; she is still there to talk hockey). No own database fields for any of these: they are all just fun facts.
- Built: the fun-fact prompt carries this definition with varied examples. Rerun of all persons
  (`funfacts-backfill --force`, ~0.1 cent per person measured) waits for Ondřej's go.

### 7. Pronunciation of names (agreed 2026-10-03; BUILT 2026-10-03, see "As built" below)
- 🔊 / 🐢 next to every name (profile card, game brief, rosters).
- First choice: a real recording from Wikimedia (Wikidata "pronunciation audio"), labelled "Originalaufnahme".
- Otherwise a computer voice in her native language (language from nationality / home-language Wikipedia
  name), normal + slow, labelled "Computerstimme (Tschechisch)". PoC: Gemini TTS with our key
  (`gemini-3.8-flash-lite-tts`, tested 2026-10-03: ~3 s, WAV); later Google Cloud Text-to-Speech (check the
  free monthly allowance first). Generated once per name and stored.
- IPA spelling where it exists (Wikidata/Wikipedia), read exactly by the voice; next to it a German-readable
  hint ("PESCH-la-ro-wa"), labelled "automatisch erzeugt, ohne Gewähr".
- Swiss names: de-CH voice where available, otherwise labelled.
- **As built** (`src/klugscheisser/pronounce.py`, table `name_pronunciations`): 🔊 / 🐢 next to names on the person
  page, event and team rosters and the game brief; on the person page a line with the hint ("automatisch erzeugt,
  ohne Gewähr"), the IPA with its source when found (Wikidata P898 or the stored Wikipedia intro), and the audio
  source. Made on first click and stored; cost per name ~0.3 ct (TTS) + hint, under the LLM daily cap.
  Deviations, from live tests with transcription:
  - The TTS model reads any instruction aloud ("Sprich nur den Namen …" was spoken). So only the name is sent and
    the language goes into the speech config (cs-CZ, sv-SE …). 🐢 = slower playback in the browser, not a
    second clip. The IPA cannot be passed to the voice; it is shown as text.
  - German-language names (SUI, GER, AUT, LIE) get no computer voice and no buttons: with five voices and de-DE /
    de-CH, "Lara Stalder" and "Alina Müller" were often read the English way. Fabi says these names natively.
  - Wikimedia recordings are labelled "Aufnahme (Wikimedia Commons)" with the file link (not "Originalaufnahme":
    Commons recordings are usually made by volunteers, not by the player).
  - Czech, Swedish and English names checked by transcription: correct (e.g. [ˈʃapɔvalɪva], [mɵˈreːn]).

## Already running (not part of v6)
- Wikipedia backfill for all persons (home language first), news every 30 minutes for everyone, X, nightly
  refresh for prepared events.

## Open points
- Fabi's real questions (needed for step 1's test set and to tune 2–3).
- Meta Graph setup (Ondřej, later; Apify works meanwhile).
