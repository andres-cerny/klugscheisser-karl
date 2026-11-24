# Build plan v14: second stage — an agent that searches deeper when the fast answer is not enough

Status: **built 2026-10-06, go from Ondřej** ("plan that, build and … test it on the questions"). See "As built".

## Why
Fabi's first questions on the v13 chat (06.10.2026) got "Dazu habe ich nichts gefunden" three times out of
four although we store related data:
1. "Anzahl länderspiele liste" (Swiss team page) — Länderspiele facts for 74 players (Wikipedia), articles
   ("200. Länderspiel"), EP international games (incomplete).
2. "Enzler Stalder EVZ spielen in der gleichen Linie?" — lines are not stored, but the match reports show
   Enzler and Stalder assisting each other's goals.
3. (follow-up) "Ja aber flügel oder center" — only "Stürmerin" is stored for her; the answer should say that.
The interpreter read all of them correctly; what failed is the step after: no building block, one document
search with the question's words, and the writer gives up instead of saying what is known.

## Idea (agentic RAG, cheap)
- **Stage 1** stays as it is (interpreter → building blocks → document search → writer).
- **Gate (Jev):** if stage 1 found nothing, or Jev says the answer does not answer the question
  ("Beantwortet die Antwort die Frage zumindest zu einem wesentlichen Teil mit konkreten Angaben?", yes/no
  probability), stage 2 starts. Answers computed from data (stats, direct tables) skip the gate.
- **Stage 2 (agent), at most 2 rounds:**
  1. **Plan (Gemini, one call):** 2–4 search variants (other words, other languages, the names spelled out)
     and the tools to use: `datenpaket` (everything stored about the persons in scope: profile fields incl.
     Länderspiele, debut, milestones, current team, position with detail), `spielberichte` (stored match
     reports with goals/assists of these persons), `statistik` (career per league, international games),
     `dokumente` (document search with each variant).
  2. **Fetch:** run the tools; all results are documents with their source.
  3. **Sort (Jev):** rerank everything against the question; keep the best ~25.
  4. **Enough? (Jev):** "Enthalten diese Auszüge Angaben, mit denen sich die Frage zumindest teilweise
     beantworten lässt?" — if not, round 2 with a new plan that sees what round 1 found.
  5. **Write (Gemini):** the usual prosa writer with all checks (verbatim quotes, `quote_in`, Jev support),
     told to answer partly: what the sources say + what is missing; never empty when a source is related.
- Jev does the judging (gate, sorting, enough?, sentence support) — about 6–35× cheaper than Gemini. Gemini
  only reads the question, plans the searches and writes.

## Limits and cost
- `answer_agent` (default on, real models only), `agent_max_rounds = 2`, `agent_max_usd = 0.02` per question
  (stop and answer with what is there), the app's daily caps apply.
- Expected: ~0.2–0.3 cents per agent question; normal questions unchanged (~0.07 cents). Every agent answer
  says so in "Durchsucht" ("Vertiefte Suche: 2 Runden, 6 Suchen …") and the cost ledger notes "agent".

## Tests
- Offline: gate (empty / thin / good / direct answers), plan parsing, each tool's documents (data package with
  Länderspiele, match reports with goals of the persons), round limit, cost cap, engine integration (stage 2
  only when needed, result replaced only when it has an answer).
- Eval: Fabi's 4 questions become J1–J4 (with their page context); `chat-eval` reports cost and time; goal: all
  54 right, nothing worse, agent cost per question visible.

## As built (2026-10-06)
- `answer/agent.py`: Jev gate (`needs_agent`), Gemini plan (search variants + tools), tools `data_package`
  (1–3 persons: full profile; groups: `group_tables`), `match_reports`, `stats_docs`, `search_docs`; Jev rerank
  and "enough?"; max 2 rounds, $0.02 cap; result shown with "Vertiefte Suche" and its searches in "Durchsucht".
- **Context by page** (Ondřej's idea, 06.10.): names in the question win; else the page decides — person page →
  that person's full profile in stage 1 already; team/event page or a group → one computed table per field
  (Länderspiele, EP national-team games marked incomplete, team, position, number, debut, college), so list
  questions are answered from one document; no context for the general chat (search as before). No staff in
  player tables.
- Found by the eval runs and fixed:
  - The writer turned joint goals into "spielen in der gleichen Linie" (stage 1 and agent). Jev rejected it;
    rules 14 (no conclusions beyond the documents), 15 (more specific question → stored value + gap) and 16
    (keep caveats such as "unvollständig"). Prompt `prosa-8`.
  - Two building blocks where the writer stayed unreliable: two players together (lines are not stored → the
    goals both were involved in, from the match reports) and Länderspiele (stored number with its source next to
    EP national-team games marked incomplete).
- `chat-eval`: page context per question (`person:` / `team:`), table headers checked; Fabi's 4 questions are
  J1–J4. Result on live data: **54/54**; one question (of 54) went to the agent (10.5 s, $0.0028), all others
  ~3 s, ~$0.0007.
- Open: Stalder's "200. Länderspiel" is only in an article (not extracted as a number); line combinations are
  not in any feed we read.
