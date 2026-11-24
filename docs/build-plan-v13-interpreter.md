# Build plan v13: Gemini reads the question first (interpreter)

Status: **built 2026-10-05 (night), go from Ondřej** ("build up a plan for yourself as option 1 and go to work … then retest"). See "As built".

## Why
The chat answers best from stored data (exact, free, the same every time), but only when a keyword rule
recognises the question. Every new phrasing, translation or typo needed a new rule. The 13 new eval questions
(2026-10-05) failed 6× although the data is stored: youth club, "Who coaches Finland?", form in the last games
(2×), an English Olympics ranking, the next game of one team.

## Idea
Gemini does not answer first; it **reads** the question: it rewrites it into one of a fixed list of German
question templates, with names, groups, leagues and seasons filled in (typos fixed, pronouns resolved from the
chat, English translated). Our existing deterministic code then answers the template from the data, with its
sources. Gemini may assume what is *meant*, never what the *answer* is: the answer still comes from stored data
or verbatim, checked quotes (`quote_in` + Jev). A wrong reading shows in "Gelesen als …" and gives a wrong or
empty answer, never an invented fact.

## Parts
1. `answer/interpret.py`: `interpret(llm, question, history, today) -> Interpretation(frage, art, sicher, usage)`.
   - Prompt: the template list (below), today's date, the last turns (question + answer sentences) for pronouns.
   - JSON schema: `{"art": enum, "frage": str, "personen": [str], "unklar": bool}`.
   - `art = dokumente` (news, quotes, injuries, opinions …): no template; the original question goes on as today.
   - Model: `gemini_answer_model` (flash-lite), temperature 0, short output. Cost recorded in the cost ledger.
   - Fallback: any error/timeout → None → today's flow with the original question.
2. Engine (`_answer_prosa`): interpret first; the template question drives scope, stats, ask-back and the
   computed answers; the original question (plus the template) goes to the document search and the writer.
   The scope note starts with "Gelesen als: …" when the template differs from the question.
   Off switch: `answer_interpreter` (settings, default on); tests inject a fake interpreter (or none).
3. Executors for templates that had none:
   - form: "Form von {Person} in den letzten 5|10 Spielen" → `ep_profiles.data.form` (direct, no model)
   - next/last game of one team: "Wann spielt {Team} das nächste Mal?" / "Wie ist das letzte Spiel von {Team}
     ausgegangen?" → stored games (events kind=game)
   - coach: "Wer ist der Trainer von {Team/Nation}?" → persons with role coach/staff of that team
   - youth club: "Juniorenklub von {Person}" → fact `youth_team`
4. Safety net: for ≤ 3 named persons the document search always gets their stored profile facts as documents
   (youth club, contract, draft, form …), not only for "structured" keywords.
5. Templates (German, the rules must route each one; a test checks every template):
   fact of a person · ranking in a group · age threshold · stats of a person / ranking / comparison / career in a
   league · form · relation (teammates, two nations, at a team) · who plays in a league now · transfers in a
   period · next/last tournament · last game between two teams · next/last game of one team · coach of a team ·
   documents (open) · unclear (ask back).

## Tests
- Offline: interpreter parsing with a fake llm; every template routes to its executor; engine with a fake
  interpreter (template drives rules, original goes to documents); fallback on interpreter error.
- Live: `klugscheisser chat-eval --yes` — the 50 questions; goal 50/50, nothing worse than the last run.

## Rollout
PR → merge → deploy (no migration) → chat-eval on live data. Ondřej is asleep: go given, results reported in the
morning.

## As built (2026-10-05, night)
- `answer/interpret.py` (prompt with the templates, `art` enum incl. `auszeichnungen`), wired into the prosa
  path of `engine.answer`; `answer_interpreter` (default on, real model only); the scope note starts with
  "Gelesen als: «…»". Cost: about $0.0004 per question (cost ledger note "frage-lesen").
- `answer/lookups.py`: coach of a team, form (last 5/10 games), next/last game of one team, awards (medals
  first, optional season); a club by its main word ("Frölunda" → Frölunda HC, only when unique). Youth club as a
  fact rule. Safety net: profile facts + form as documents for 1–3 named persons.
- Calibration with `chat-eval` (50 questions, live data): 44/50 before (old 37 + 7 of 13 new) → 46 → 49 → **50/50
  twice in a row**. Fixes found by the runs: the interpreter added a year ("bei Olympia" → "Olympia 2026": now
  forbidden); awards had no template; "Frölunda" alone was no team; the writer shortened values (draft without
  year: rule 13) and wrote "sie" instead of the name in the first sentence.
- Open: a team with no stored games/coach falls back to the document search (EV Zug: the SIHF feed stores no
  upcoming Swiss league games); the interpreter is web chat only (MCP clients read questions themselves).
