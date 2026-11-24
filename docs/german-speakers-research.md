# German-speaking players of other nations: research (2026-10-25)

Fabi's question (via Ondrej): *"Which players from other nations (not Switzerland, and beyond the obvious
Germans and Austrians) speak German, so I could interview them in German, especially those who played or play
in the Swiss league?"* Context: Women's WCh 2026 in Denmark (6–16 Nov).

Research only: no code changes, no PR. Data: copy DB `klugscheisser_phasef` (backup 25.10. 17:27), read-only SQL.
Stand-in test: on a throwaway copy of that DB (`klugscheisser_german`, deleted afterwards) with origin/main code
(5218c6a, Phase F tools on, auto-refresh and all collectors off, port 8033). It did not run against live, because
`person` starts a background update of the asked persons on live (Apify + DB writes).

## Labelling rule (Ondrej, 2026-10-25)

- **confirmed German**: a German interview with her exists (her own words quoted in German, or audio/video in
  German), cited with source + date.
- **likely**: anything else (years at a German-speaking club, German parents, dual nationality, German Instagram
  posts), with the reason given.
- No evidence: not listed.

A note on "her own words quoted in German": club press releases are often translated. If the same quote also
appears in English on the club's English page, it is a translation, not a German interview, so it only counts
as **likely**.

## 1. What Karl knows today

Scope: WCh 2026 rosters (event `wch-2026`, all provisional except GER/AUT) of CZE, FIN, SWE, DEN, JPN, CAN, USA,
players and staff, compared against `career_stints` (EP, Wikipedia, Wikidata, web), `facts` (kind `languages`)
and the stored texts.

**Confirmed German: none.** Karl has no German interview with any player or coach from these teams.
- `facts.kind = 'languages'` (456 rows, all from Wikidata) has no German for any player of these teams. Wikidata
  lists only Czech/Finnish/Swedish/Japanese/Danish/English for them.
- Stored texts: there are no hits for "spricht Deutsch / speaks German / in German / tyska / saksaa / německy"
  that mean a player's German. The hits are false positives: "Saksa" means Germany as an opponent, Julia Schalin
  "visiting my boyfriend in Germany", Ebba Hedqvist's father played in Peiting (GER).
- German-language items about these players are game reports (SRF, Blick, SIHF) and EVZ / frauen-eishockey.com
  releases, not interviews.
- A quick web check (Neubauerová, Seroiszková, a general search) also found no German interview.

**Likely** (ranked by evidence strength):

| # | Name | Team (WCh 2026) | German-speaking club years | Evidence (source, date) | Label |
|---|---|---|---|---|---|
| 1 | Noemi Neubauerová | Tschechien (F/D) | EV Zug (Zug, German-speaking): Sept.–Oct. 2024 (9 games) and the full 2025/26 season (champion, cup winner, 23 G + 18 A). Now PWHL Seattle Torrent | EP; en Wikipedia; evz.ch 11.09.2024, 30.06.2025, 02.04.2026; frauen-eishockey.com 05.04.2026. The German quotes in the EVZ releases also appear in English on evz.ch/en, so they are club translations | likely: about 1.5 seasons in Zug |
| 2 | Klára Seroiszková | Tschechien (D) | HC Davos Ladies (Graubünden, German-speaking) since 2025/26, still there in 2026/27 | EP (2025/26–2026/27); SIHF game sheets 11.10.2026 and 24.10.2026 (linked to her); zug4you 04.11.2025. en Wikipedia still says HV71 (out of date) | likely: 2nd season in Davos, currently there |
| 3 | Tero Lehterä | Finnland (head coach) | Played in the Swiss NLA for EHC Basel (German-speaking) and HC Ambrì-Piotta, and in the German DEL. Years are not in Karl | fi Wikipedia (item 13825: "Sveitsissä, Saksassa"); en Wikipedia; Wikidata stints | likely, weak: men's playing career years ago, seasons unknown |
| 4 | Kris Sparre | Kanada (assistant coach) | Iserlohn Roosters (DEL), 2009 | Wikipedia stint | likely, weak: one season |
| 5 | Tereza Radová | Tschechien (D) | HK Budapest in the German DFEL, 2026/27 | EP | likely, very weak: Hungarian club, German league |

**Swiss league, but not German-speaking (not listed under the rule; tell Fabi why):** Michaela Pejzlová (CZE)
and Julia Liikala (FIN) played at HC Ambrì-Piotta in 2024/25, and Michelle Karvinen (FIN, Danish-Finnish) at
Ladies Team Lugano in 2020/21. Both clubs are in Ticino, where people speak Italian. Pejzlová and Liikala now play
at Frölunda.

**Not on the provisional WCh roster:** Sini Karjalainen (FIN, EV Zug 2026/27, 3 games) and Fanny Rask (SWE,
Ambrì; Karl has a "Karriereende/Pause" warning on her).

**Coverage gaps that limit the answer:**

| Roster | EP stints | Any stints |
|---|---|---|
| Dänemark | 0/23 | 7/23 |
| Japan | 0/32 | 11/32 |
| Tschechien | 19/39 | 25/39 |
| Finnland | 18/29 | 24/29 |
| Schweden | 13/34 | 20/34 |

Danes and Japanese with club years in Germany or Switzerland would not be found today. The EP fetch for WCh
rosters (`EP_SCOPE=event:wch-2026`, PR #42) should fill most of this after the next nightly run.
Karl also has no "language region" for clubs, so Zug, Davos, Ambrì and Lugano all look the same ("SWHL A").

## 2. Stand-in test (3 phrasings)

Run on 2026-10-25 against the copy, with the Phase F instructions `instructions_eishockey_f.txt` and Sonnet 5.5.
Stand-in cost: $0.37; Karl's own cost: $0. Output (not in git): the scratchpad `ger/run1/report.md`.

- **g01 (German, "Welche Spielerinnen … sprechen Deutsch … Schweizer Liga")**: 17 tool calls (team ×5, stats ×7,
  suche ×2, person ×2).
  - Correct core: it found no proof of German, and it listed Pejzlová, Neubauerová, Seroiszková, Liikala and
    Karvinen with EP links and games.
  - It flagged Lugano as Italian-speaking, but not Ambrì, and then called all five "im deutschsprachigen Umfeld"
    (wrong for 3 of them).
  - It called Seroiszková's club unclear (Wikipedia says HV71) although Karl has her in Davos SIHF game sheets from
    October 2026.
  - It wrongly said Karl's language data covers only the DACH rosters.
  - It missed the coaches and Radová. Useful extra: it named the Czech media officer as the person to ask.
- **g02 (English, "played in CH/GER/AUT, could give an interview in German")**: 14 calls. Similar list plus a
  roster check per player, and it was honest that German is only an assumption. It made the same Ambrì mistake.
  It also included a confusing claim that the DACH rosters are "als deutschsprachig erfasst" (citing one Wikidata
  page). No coaches.
- **g03 (lists PWL/DFEL/EWHL by nation)**: 9 calls, but no roster check.
  - It mixed in non-roster and very old entries (Křížová / Horálková at Slavia Praha in the EWHL 2007/08, Janka
    Hlinka, Durante, Provost) and said so at the end.
  - It found Radová (DFEL) and Lenka Serdar (Memmingen 2019/20; Serdar is not on the CZE roster).
  - It used `stats` with a league list, which works well.

**Verdict:** correct where it matters: no answer claimed a German-speaking player without evidence, and the
sources are cited. But the answers are incomplete or misleading on the point that matters for an interview:
- whether the club is in a German-speaking region (Ambrì/Lugano vs Zug/Davos),
- current club status (Davos),
- coaches and staff,
- Danes and Japanese (no data).

The stand-in had to piece the answer together from 9–17 calls, because Karl has no field for "language" or
"German-speaking club years".

## 3. Two questions for Ondrej to paste into Perplexity

1. **Without Karl** (connector off):
   > Welche Spielerinnen und Trainer der Frauen-Eishockey-WM 2026 in Dänemark aus Tschechien, Finnland, Schweden, Dänemark, Japan, Kanada und den USA sprechen Deutsch oder haben in einem deutschsprachigen Club gespielt (Schweizer PostFinance Women's League, deutsche DFEL, Österreich, Südtirol)? Bitte pro Person: Team, Club mit Saisons, und ob es ein Interview auf Deutsch mit ihr gibt (Link und Datum). Wenn es kein deutsches Interview gibt, schreib «nicht belegt». Nichts vermuten.
2. **With Karl** (connector on): the same text, starting with:
   > Nutze zuerst Klugscheisser Karl, dann das Web. …

What to compare: did it find a real German interview (that would be a gap in Karl's data)? Did it separate Ticino
clubs from German-speaking clubs? Did it get Seroiszková's current club right? Did it include the coaches? Did it
invent "spricht Deutsch" without a source?

## 4. Proposal: how Karl could answer this well (and other language questions)

The goal is one tool call per team that gives each person's German evidence, with the label and the source.

1. **Club language region (curated, cheap).** A small YAML file `seeds/club_regions.yaml` (about 80 entries) maps
   clubs and leagues to a language:
   - de: Zug, Davos, ZSC, Bern, Thurgau, Kloten, Basel, all DFEL/Austrian/German clubs, Bozen/South Tyrol.
   - it: Lugano, Ambrì.
   - fr: Lausanne, Neuchâtel, Genève.
   - bilingual: Fribourg, Biel.
   - not German-speaking although the league is: HK Budapest (DFEL), EWHL clubs outside AT/South Tyrol.

   From this, derive **"German-speaking club years"** per person from `career_stints`: club, seasons, games (EP),
   and whether the stint is current. Effort: about 0.5 day.
2. **Language-evidence facts from texts** (`facts.kind = 'language_evidence'`, next to the Wikidata `languages`).
   Each row has: language, label `confirmed` | `likely`, reason, source URL, date, verbatim quote.
   - **confirmed** only when an item is a German-language interview or audio/video with her own words (Jev/LLM
     check, "is this person speaking or being interviewed in German?"). If the quote also exists in English on the
     same club's site, it is a translation and only counts as **likely**.
   - **likely**: an explicit text statement ("spricht fliessend Deutsch", "German mother"), dual nationality,
     German Instagram captions she wrote herself, plus the derived club years from step 1.

   Backfill: only for items linked to WCh/EHT roster persons. A regex prefilter finds the candidates, then Sonnet
   or Jev reads only those (estimate: under $2 one-off). New items get the check in ingest. Effort: about 1–1.5 days.
3. **MCP output.**
   - `person` gets a line *"Deutsch: bestätigt (SRF-Interview, 12.03.2026, Link)"* or *"wahrscheinlich: 2 Saisons
     EV Zug (EP)"*, or nothing.
   - `team` roster gets a column "Deutsch" with the same label and a short reason. Then the question needs one
     `team` call per nation instead of 9–17 calls.
   - `suche` shows these facts in its facts table when the question is about language or interviews.
   - Optional: an "interview language" hint per person in this order: German confirmed, then German likely (ask
     the media officer), then English.

   Effort: about 0.5 day.
4. **Data gaps to close first (no new code):** the EP fetch for WCh rosters (DEN, JPN, AUT now 0 or 1 EP
   profiles), staff stints (coaches), and the newest club for Seroiszková-type cases (SIHF game sheets are newer
   than Wikipedia, so prefer them for "current club").
5. **Tests:** add 2 or 3 questions to `tests/fixtures/chat_eval.yaml` and the gap questions (g01–g03 above) and
   unit tests for the region map. Effort: about 0.5 day.

**Total effort:** about 3 days of agent work; one-off model cost under $2.

**Risks:**
- Never guess a language from a name, nationality or a club in a bilingual city. Without evidence, nothing is
  shown.
- "likely" must always carry its reason, so Fabi can decide.
- Translated press-release quotes must not turn into "confirmed".
- Provisional rosters change before 6 Nov.
- Club years say little about speaking ability: 1.5 seasons in Zug does not mean an interview is possible, so the
  practical advice remains to ask the team's media officer.

## 5. Built (branch feat/german-club-years, steps 1 + 3)

- **Club language map** `seeds/club_languages.yaml` (version 1, 54 entries; some cover several small clubs, ~150 club names):
  German-speaking Swiss clubs (Zug, ZSC, GCK, Bern, Davos, Langenthal, Reinach, Weinfelden, Thurgau, Kloten,
  Basel, Winterthur, Rapperswil, Thun, Visp …), Ticino = Italian (Ambrì, Lugano), Romandie = French (Lausanne,
  Neuchâtel, Genève …), bilingual Fribourg-Gottéron (mostly French), Biel/Bienne, South Tyrol (Bozen); German and
  Austrian leagues and clubs = German; HK Budapest / KMH / MAC Budapest = Hungarian although they play in the
  DFEL / EWHL. Swiss leagues have no language: an unlisted Swiss club counts as nothing.
- **German-speaking club years** (`src/klugscheisser/facts/german.py`): per person and club, from season stats
  (games = best source per season), Elite Prospects stints (open end = current season), affiliations; Wikipedia/
  web stints only when none of these has the club ("2020–2021" = one season).
- **Labels**: `bestätigt` only from `facts` rows of kind `language_evidence` with label bestätigt (none exist yet:
  step 2 writes them); `wahrscheinlich` = a German-speaking club (reason: seasons, club, place, games, "aktuell
  dort"), a bilingual club (said so), a stored "Sprachen: Deutsch" fact; `kein Hinweis` note for Swiss clubs and
  `warn` clubs that are not German-speaking. Persons of SUI/GER/AUT/LIE get nothing (Fabi asks about other
  nations; Swiss players' club regions are not shown, to keep it simple).
- **Output**: `person` line "Deutsch: wahrscheinlich – 2 Saisons EV Zug (Zug, deutschsprachig), 2024/25–2025/26,
  48 Spiele. Kein deutsches Interview in Karls Daten; Klubjahre sind nur ein Hinweis [Elite Prospects](…)";
  `team` roster: "· Deutsch: wahrscheinlich – 2 Saisons EV Zug (2024/25–2025/26)" per player and staff line, plus
  the rule once; `suche` with a German-language question ("Wer spricht Deutsch bei Tschechien?", "Who on Finland
  speaks German?") shows the labels per national team (named teams, else all teams of the newest tournament) with
  the coverage ("Karriere-Stationen bekannt für 7/23"). Switch: `GERMAN_HINTS` (default on).

### For step 2 (German interviews from texts)

Write one `facts` row per piece of evidence: `kind = 'language_evidence'`, `value = {"language": "de", "label":
"bestaetigt" | "wahrscheinlich", "reason": "SRF-Interview auf Deutsch", "date": "2026-03-12"}`, `value_text` =
the reason, `source`, `source_url`, `source_item_id`, the verbatim `quote`; `status = 'conflict'` hides a row.
`facts/german.py` then shows "Deutsch: bestätigt – SRF-Interview auf Deutsch (12.03.2026) [srf](…)" first and
keeps the club years as further reasons. No code change needed in the output.

## 6. Built: step 2 (branch feat/german-interview-evidence) — German interviews from stored texts

`src/klugscheisser/facts/german_evidence.py` writes the `language_evidence` rows that `facts/german.py` reads.

- **Who:** players and staff on rosters of current or upcoming tournaments (WCh 2026: 248 persons) of national
  teams outside SUI/GER/AUT/LIE.
- **Which texts (free prefilter):** her linked items with German text (distinctive German function words), German
  sentences spoken in a video of her posts (media analysis), and unlinked German items that name her in full.
  Wikipedia and results are skipped. A candidate needs a German direct quote («…», „…“, "…") with her name
  within 300 characters.
- **Judge (paid):** one Claude Sonnet call per (item, person), about $0.008. It only describes: whose words, the
  setting (interview, statement to this outlet, press conference, video/audio, club release, social media), and
  whether it was said in German (ja / nein / unklar). The code then labels:
  - **bestätigt**: her words, interview / statement to this outlet / press conference / video, German original
    "ja", not on a club or federation site, no "Medienmitteilung" wording, and the quote is in no other stored
    document;
  - **wahrscheinlich**, with the reason: a club or federation release ("oft übersetzt, kein Interview"), the same
    quote also in another document ("übernommene Mitteilung"), a release-style text, a social media quote, a
    German outlet quote whose original language is unclear; her own German post caption (no model call);
  - **dropped**: not her words, or clearly translated ("auf Englisch", "übersetzt"), or the quote is not verbatim.
- **Rows:** one per (person, item), value `{"language": "de", "label", "reason", "short", "date", "outlet"}`,
  verbatim `quote`, `source_url`, `source_item_id`; `confidence` orders the clues (interview 0.9 > unclear
  outlet quote 0.7 > own post 0.65 > club release 0.55 > copy 0.5). A row set to `status = 'conflict'` by hand
  stays hidden and is not rewritten.
- **Re-runnable, idempotent:** every checked pair gets a `cache_entries` mark (`german_ev`, `v1:item:{id}:a{id}`),
  so a rerun pays only for new items; `--force` checks again and replaces the row (never duplicates).
  - CLI: `uv run klugscheisser german-evidence` (free prefilter: candidates + estimate, nothing stored),
    `--yes` (paid, `--max-usd` 0.5), `--event wch-2026`, `--athlete 455`, `--json`.
  - Nightly step `deutsch_interviews` after the profiles (`GERMAN_EVIDENCE_NIGHTLY`, default on;
    `GERMAN_EVIDENCE_MAX_USD` 0.30 per night). New German interviews during the tournament are picked up the next
    morning.
- **Output** (`facts/german.py`): bestätigt first, with date and a short quote; the other text clues are one
  combined reason after the club years ("…; 2 weitere solche Texte"), and the team table shows the short form.

### Measurement (2026-10-25, copy of the backup 25.10. 21:04, DB `klugscheisser_deutsch2`)

- 248 roster persons, 25 (item, person) pairs with German text, 13 candidates, 13 model calls, $0.10 per full
  run (development total about $0.30 incl. reruns).
- **bestätigt: nobody.** Karl's stored texts hold no German interview with a player or coach of these teams.
- **wahrscheinlich (new text clue): Noemi Neubauerová (CZE)**, 3 rows, all EV Zug release quotes:
  - evz.ch 02.04.2026 «Die Saison 2025/26 beim EVZ werde ich nie vergessen. …» (club release; per section 1 the EVZ
    quotes also appear in English on evz.ch/en);
  - frauen-eishockey.com 05.04.2026, the same quote (copy of the EVZ release);
  - evz.ch 30.06.2025 «Ich habe nur gute Erinnerungen an Zug – vor allem an die fantastische Stimmung im
    Stadion!» (club release).
  - Her line now reads: "Deutsch: wahrscheinlich – 2 Saisons EV Zug (Zug, deutschsprachig), 2024/25–2025/26,
    48 Spiele; deutsches Zitat in einer Club-/Verbandsmitteilung (EV Zug), oft übersetzt, kein Interview
    (02.04.2026); 2 weitere solche Texte. Kein deutsches Interview in Karls Daten; …".
- **Dropped (correctly):** 10 pairs. Daniela Diaz's quote in a Neubauerová release (frauen-eishockey.com
  20.10.2024); Ivana Wey's quote in an EVZ Olympics text (Neubauerová named); Josef Hovorka (coordinator) in a
  Radio Prag German article about Adéla Šapovalivová; Alina Müller's quote in an SRF article that names coach Kris
  Sparre; Laura Kluge's quotes in rbb24 / sportschau (Megan Keller, Haley Winn, Aerin Frankel named, 6 pairs).
- A web check for German interviews outside Karl's data was not possible this time (Firecrawl out of credits).
