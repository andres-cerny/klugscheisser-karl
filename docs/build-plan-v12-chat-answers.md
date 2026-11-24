# Build plan v12: real answers in the web chat

Status: **built 2026-10-05 behind `web_answer_style` (default still `liste`)**; see "As built" at the end. Plan written 2026-10-04 with Ondřej on the server, after testing the chat.

## Why
In the web chat, a question comes back as a list of evidence lines, not an answer. Two tests on 2026-10-04:

1. "In the recent Switzerland Finland game in Euro Hockey Tour who was a teammate with whom"
   - "Switzerland" + "Finland" → both full rosters (41 + 24 persons). "who" → block 4 (`cross.block_evidence`):
     top 3 stored documents per person by similarity, one line each.
   - Result: a WM game, Finnish junior clubs, Instagram posts, and "Keine Belege für: …" with dozens of names.
     The question was about one game, which the app can't recognise.
2. "Who from the swiss olympic team was the oldest" (asked as a follow-up in the same chat)
   - The cross path is skipped when there is chat history (`answer/engine.py` ~:607), so the fact rule
     "oldest → sort birth_date" never ran.
   - The normal path had Gemini write "Jessica Lutz is the oldest, 37" from her profile line. Jev then rightly
     said the quote ("born 24.05.1989 (37 Jahre)") doesn't prove "oldest" → moved to Unbestätigt → the page said
     "Keine belegte Angabe".
   - The answer came back in German although the question was in English.

What already works and stays: Gemini reads the numbered sources, every claim carries a verbatim quote, the quote
is checked to exist in the document (`quote_in`), Jev checks that the quote supports the claim, and the
sensitive-topic rule (relationships/health only from the person herself) applies.

What's missing: no step writes an answer. `SYSTEM` asks for "eine Aussage pro Fakt, kurz, deutsch", and
`answer_html` lists them.

## Scope
- **Web chat only** gets the new answer style. The MCP connector keeps today's line format: there,
  Claude/Perplexity write the sentences (README, `mcp_server/present.py`).
- All projects get this style (Ondřej, 2026-10-04). Internal switch `answer_style = prosa | liste` only for
  testing and for MCP (`liste`); web default `prosa` everywhere.
- No new paid services. Expected cost: about the same Gemini call (same documents in, a few sentences out),
  plus Jev support checks per sentence, as today.

## Part A: Gemini writes the answer, Jev checks every sentence (points 1 + the core idea)
### A1. New prompt + schema (`answer/engine.py`)
- `SYSTEM_PROSA`, same source rules as `SYSTEM` (1–3, 6, 7), but:
  - Answer the question directly, first sentence = the answer. Gemini decides length and form: a few
    sentences, a longer text, or a table when that reads better (rankings, rosters, comparisons, "who are all
    …"). No fixed limit; the prompt only says "as short as the question allows".
  - Tables: every row (or cell with a fact) cites its documents like a sentence does.
  - Write in the language of the question (see Part C).
  - Every sentence cites 1–2 documents, each with a verbatim quote (3–30 words, untranslated).
  - Nothing in a sentence beyond what its quotes say. If the sources only partly answer, say what is known and
    what isn't (that becomes the `luecken` part).
  - Optional `rueckfrage` (Part D) instead of an answer when the question is too unclear.
- Schema: the answer is a list of blocks; each block is text or a table, and every claim inside points to
  our documents with a verbatim quote:
  ```json
  {"antwort": [
     {"typ": "text", "saetze": [{"satz": "...", "belege": [{"dok": "D3", "zitat": "...", "kategorie": "anderes"}]}]},
     {"typ": "tabelle", "spalten": ["Spielerin", "Geboren", "Club"],
      "zeilen": [{"zellen": ["Jessica Lutz", "24.05.1989", "..."], "belege": [{"dok": "D1", "zitat": "..."}]}]}
   ],
   "luecken": ["..."], "rueckfrage": "..." | null}
  ```
- **The quotes must come from the documents we provide** (Ondřej's condition). Gemini gets no web access
  and no other source; anything without a valid quote is removed by A2, never shown as fact.
### A2. Checks (reuse `_write` pieces)
- Per sentence and per table row: each `dok` exists, each `zitat` passes `quote_in`, sensitive categories only from own
  posts/interviews. Fail → sentence dropped, counted in `verworfen`.
- Jev `_support_scores` on (sentence or row as text, e.g. "Jessica Lutz | geboren 24.05.1989", quotes joined). Below `answer_support_threshold`:
  - The sentence/row is not shown as fact (a failing row is removed from the table). It goes to a collapsed "Nicht sicher belegt" list with its quote.
  - If the first sentence (the answer) fails, the answer starts with the confidence wording from Part E
    ("Die Quellen deuten an, dass …") instead of a plain statement. Decide in A4 after looking at real cases.
- Keep `translate_quotes` for the quotes in the source list.
### A3. Result + rendering
- `AnswerResult` gets `antwort: list[Satz]` (text, source numbers, support) and `rueckfrage: str | None`.
  `statements` stays filled (one per cited source) so MCP, caching and the rating review keep working.
- `web/views.py answer_html` for `prosa`:
  1. The answer blocks: paragraphs with clickable [1] [2] markers; tables as real HTML tables (mobile:
     horizontal scroll), source markers in a last narrow column. A minimal Markdown-like renderer of our own
     JSON, no raw HTML from the model (escape everything).
  2. "Quellen": numbered list, each with source label, date, «quote» (+ translation), link. Vorsicht sources
     marked inline (no separate VORSICHT block).
  3. Collapsed: "Nicht sicher belegt (n)", "Was die Quellen nicht sagen" (luecken), "Durchsucht: …, Kosten".
  4. Small line at the top: Datenstand · "Verstanden als …" (kept, it's useful).
- Cache key gets the answer style + prompt version, so old cached list answers aren't reused.
### A4. Calibrate
- Run the question set (Part H) with both styles, compare dropped/unsure counts. Adjust the prompt and the
  threshold for sentences (may need a separate `answer_support_threshold_prosa`).

## Part B: answers computed from data (point 2)
- Fact questions (oldest, youngest, tallest, followers, numbers, birthplace, …) and relations already have
  deterministic blocks (`cross.block_facts`, `block_relations`). Change:
  - Run the fact/relation blocks **also with chat history** (today all of `cross` is skipped then). Keep
    skipping only block 4 for follow-ups about one person.
  - Turn the result into one **computed source document** for the writer, e.g.
    `[D1] Olympia-Kader Schweiz 2026 nach Geburtsdatum (Wikidata/EP, Stand 04.10.): Jessica Lutz 24.05.1989;
    … (23 Personen, 0 ohne Datum)`, with the per-person sources as D2…Dn.
  - Gemini writes "Jessica Lutz is the oldest player (born 1989) [1]". Its quote comes from the computed
    document, so Jev can confirm it honestly.
  - The computed document is labelled in the source list as "Berechnet aus gespeicherten Daten" with links to
    the underlying sources. Missing values are named ("for 2 players no birth date is stored").
- Fact rules that are German/English regex today: add the English forms that are missing and check
  "olympic team" → Olympic roster. In test 2 the person list looked like the current squad and included the
  coach (Shannon Miller): roster scope for "Olympia" must use the event roster, and coaches/staff must be left
  out of player questions.

## Part C: language
- Answers are **always German** (Ondřej, 2026-10-04), also when the question is in English. No change needed;
  the prompt keeps "deutsch".
- Quotes stay original, with the existing German translation line under non-German/English quotes.

## Part D: ask back when the question is unclear (point 4)
- Gemini may return `rueckfrage` instead of an answer when:
  - the question is cut off or has no clear subject ("who was a teammate with whom"), or
  - it names a game/event that matches more than one candidate.
- The app also asks back (no Gemini needed) when the scope would be more than ~40 persons for a "who" question
  and there is no narrowing (game, date, topic).
- Shown as a chat bubble with 2–3 tap-to-send suggestions, e.g. "Spiel Schweiz–Finnland, Lahti 3.10.?" (from the
  stored games), "Alle Spielerinnen der Nati?".

## Part E: confidence in words (point 3)
- Replace VORSICHT/Unbestätigt headings in the prosa view with wording in the answer:
  - all sentences confirmed by Jev, ≥2 sources → plain statement
  - one source only → "laut SRF …"
  - Vorsicht source (boulevard, fan account) → "Blick berichtet, dass …"
  - below threshold → "nicht sicher belegt" (collapsed, see A2)
  - nothing found → "Dazu habe ich nichts gefunden." + closest sources (today's `naechste_quellen`)
- Rules in the prompt + a post-check that downgrades wording if Jev's score is lower than the wording claims.

## Part F: recognise a specific game (point 5)
- New understanding rule: two teams/nations + game words (Spiel, Match, game, gegen, vs, –) + optional
  tournament/date ("Euro Hockey Tour", "WEHT", "letztes", "recent").
- Look up the game in the stored results (games table from the results collectors). One match → scope =
  both teams' players **in that game** (lineups if stored, else roster), date window = game day ±2 days, and the
  game's match reports go in first.
- Several matches → Part D question. None → say so and answer from the latest meeting, labelled.
- Block 4 for a game: only persons with documents in that window, not 65 × top-3 by similarity.

## Part G: follow-ups (point 6)
- History already goes to Gemini (rule 7). With prosa answers, pass the previous answer's sentences + person ids
  (not the whole list), so "und die jüngste?" / "and her club?" work.
- Follow-up fact questions run through Part B (no more skipping because of history).
- Suggested next questions (`Nächste Fragen` exists in the UI) can come from the writer: up to 3, only ones the
  stored data can answer.

## Part I: dictation that shows the words while speaking
- Today (`web/html.py` `addMics`): browser speech recognition, `lang='de-CH'`, `interimResults=true`,
  `continuous=false`. Interim results are on, so words should already appear live. Likely reasons they don't:
  - iPhone Safari delivers interim results late or only at the end, and `continuous=false` stops at the first
    short pause.
  - English questions are recognised as Swiss German (`de-CH`) and come out garbled.
- Steps:
  1. Check on Ondřej's phone (which browser?) with a small test page that logs every interim event.
  2. `continuous=true`; stop on the mic button or after ~3 s of silence; show interim text in grey, final text
     in black, keep the cursor at the end.
  2b. A clear "I'm listening" signal, even before any words arrive (Ondřej, 2026-10-04):
     - mic button turns red and pulses as soon as recording starts (`onaudiostart`), with "Ich höre zu …" in the
       text field as grey placeholder;
     - a small live sound-level bar or wave next to the button that moves with the voice (Web Audio
       `AnalyserNode` on the mic). Check on the iPhone that this second mic use doesn't break the speech
       recognition; if it does, use the recognition events instead (`onspeechstart` → stronger pulse,
       each interim result → short flash);
     - a short vibration on start and stop where the phone supports it.
  3. Language: `de-CH` default, tap-and-hold on the mic (or a small DE/EN switch) for English; remember the
     last choice.
  4. If the phone browser still can't stream: optional server speech-to-text that streams (audio chunks over a
     WebSocket to a streaming STT service). This costs money per minute → ask Ondřej before choosing one.
- Note: the iPhone keyboard's own dictation key (in the text field) already streams words live and is a
  zero-work fallback.

## Part H: tests and acceptance
- **Replay of every question asked so far** (Ondřej's request): a script `chat-replay` reads all user questions
  from `chat_messages` in every project database (with their chat context: person/team/event, and the earlier
  turns for follow-ups), answers them with the old and the new style, and writes a side-by-side HTML report
  (question · old answer · new answer · sources dropped/unsure · cost). Fabi's ratings (gut/okay/schlecht +
  reason) are shown next to the old answer. Read-only: replays never write to the chats. Run before switching
  the default, and again after each part.
- Question set (`tests/fixtures/chat_questions.yaml`) with expected shape, run against the test DB with fake
  Gemini/Jev, plus a manual live run on the server:
  1. "Who from the swiss olympic team was the oldest" (fresh chat and as follow-up) → "Jessica Lutz (born
     24.05.1989) is the oldest …" with a computed source, in German.
  1b. "List the Swiss Olympic roster with age and club" → a table, every row with a source.
  2. "In the recent Switzerland Finland game in Euro Hockey Tour who was a teammate with whom" → rueckfrage, or
     with the game recognised: connections between players in that game (block_relations) with sources.
  3. "Wer hat gegen Finnland das 1:1 geschossen?" → one sentence, SRF quote.
  4. One sensitive question (relationship) → only own posts, else "nicht belegt".
  5. A question with no data → "Dazu habe ich nichts gefunden" + closest sources.
  6. Same questions through MCP → unchanged line format.
- Unit tests: schema parsing, per-sentence checks (invented quote, wrong doc, low support), computed document,
  rueckfrage rendering, cache key change.
- Docs: `docs/web-app.md` (answer format), README (web chat writes answers, MCP stays data-only).

## Order
1. B (fact blocks also with history, computed source document, roster/coach fix) — small, fixes test 2.
2. A (writer, checks, rendering) behind `answer_style`, calibrate (A4) with the replay report.
3. E (confidence wording).
4. D + F (ask back, game recognition).
5. G (follow-ups, suggested questions).
   I (dictation) is independent and small: can go first or in between.
6. H runs throughout; switch the web default to `prosa` after the live run looks good to Ondřej.

## Open questions for Ondřej
- Should "Nicht sicher belegt" be visible at all in the web chat, or only in the source list?

Decided 2026-10-04: all projects use this style; Gemini chooses length and form (text or table); every claim
must quote the documents we provide.

## Build night 2026-10-04/05 (go from Ondřej: "read the plan and execute it yourself")
Built by the v11 session after v11 went live, in parallel parts like v11. Ondřej is asleep, so:
- The web default stays **`liste`** (`WEB_ANSWER_STYLE`, project setting `web_answer_style`); `prosa` is built
  and tested behind it. Switching the default waits for Ondřej's look at the replay report (Order step 6).
- Paid calls only for a small live check (a handful of questions, cost from `cost_ledger` in the report);
  all tests use fake Gemini/Jev. No streaming speech-to-text service (Part I step 4 needs his choice).
- The open question ("Nicht sicher belegt" visible?) → built as a collapsed list, easy to hide.

### Shared contract (foundation commit on branch `chat-v12`)
- `interfaces.py`: `AnswerSentence(text, quellen [1-based numbers into AnswerResult.quellen], support, ton)`,
  `AnswerBlock(typ text|tabelle, saetze, spalten, zeilen [{"zellen", "satz"}])`; `AnswerResult` gains `stil`,
  `antwort`, `quellen` (numbered Statements), `nicht_sicher`, `rueckfrage`, `rueckfrage_vorschlaege`,
  `naechste_fragen`. `statements` stays filled (one per cited source) for MCP, cache and ratings.
- `config.py`: `web_answer_style = "liste"` (default), `answer_support_threshold_prosa = 0.6`.
- `answer/engine.py` `Doc`: `computed` (bool) + `basis` ([{"quelle", "url"}]) for documents computed from data.
- `answer/cross.py`: `ProsaInputs(docs, athlete_ids, scope_note, since, until)`, `AskBack(text, vorschlaege)`,
  `prosa_inputs(s, question, athletes, history)` and `ask_back(s, question, athletes, history)` (stubs returning
  None in the foundation; the cross part fills them).
- Parts: **W** writer (A, E, G: engine prosa path, checks, confidence wording, follow-ups, cache key, answer
  rendering in web/views.py), **X** cross (B, D, F: fact/relation blocks with history, computed documents,
  English forms, Olympia roster without coaches, game recognition, app-side ask-back), **Y** dictation +
  replay (I steps 2–3 in web/html.py `addMics`; H: `chat-replay` report, question set fixture).

## As built (2026-10-05, night)
- Parts A, B, D, E, F, G, H, I (steps 2–3) built; tests: `test_v12_prosa`, `test_v12_cross`,
  `test_v12_dictation_replay`; full suite green.
- Switch: project setting `web_answer_style` = `prosa` (or env `WEB_ANSWER_STYLE`); default `liste`. MCP `frage`
  always `liste`. No toggle on the settings page yet.
- Calibration (A4) with `chat-replay` on a copy of the live data (19 real questions, both styles, $0.07 per run):
  - fixed: "Für welches Team spielt sie?" on a person page kept the national team instead of the person; more
    current-team phrases; sentences like "Die Dokumente enthalten keine …" were checked as facts (Jev rejects
    them) → now gaps ("Was die Quellen nicht sagen"); prompt `prosa-2` (no contradiction between "leave empty"
    and "write what is related").
  - result: most answers are one or two sourced sentences; ~1 sentence per answer lands in "Nicht sicher
    belegt", mostly rightly (sentences claiming more than their quote, e.g. "Studium" from "four years at UMD");
    ~6 of 19 say "Dazu habe ich nichts gefunden" with the closest sources.
  - open: "the last WM" is read as the next WM (2026 Herning, not played yet); typos ("olderst") miss the fact
    rules; the first sentence failing Jev is not reworded yet ("Die Quellen deuten an …").
- Part I: continuous dictation, grey live words, red pulsing mic + level bars, DE/EN switch; Ondřej must try it
  on his iPhone (Safari may deliver live words late; no vibration on iPhone). Step 4 (paid streaming STT) not done.
- Games: the Euro Hockey Tour games are not in the stored results (only the tournament), so game questions use
  the tournament fallback until the results collectors store them.
