# v16: the last three search questions (S3, S4, S7)

Status: **built 2026-10-09**, taking over the search work of PR #11 ("so we fill up our database").

## What failed and why
After PR #11 (Jev reads all documents of the persons asked about) the eval stood at 62/65. The documents were in
the database every time; three different steps lost them:

| Question | Document | What went wrong |
|---|---|---|
| S3 Kunsteis aus Kunststoff (Stalder) | YouTube "Lara Stalder on Glice Hockey" | Found (Jev rank 1), but the writer made it "hat auf synthetischem Eis trainiert"; Jev rightly rejected that (0.23), so the answer was empty |
| S4 Punkterekord Northeastern (Müller) | Blick "Mit 168 Punkten ist sie Allzeit-Leaderin" | The stats step answered with the EP career table (254 points) before any document search |
| S7 Gehirn der Forschung (no person) | Wikipedia en (Fran Rider), fr (Erin Ambrose) | The agent's English search found Fran Rider, but every document was cut to its first 1,500 characters; the brain donation is at character 3,866 |

## Changes
1. **Repair step** (`engine._repair_claims`, `SYSTEM_REPAIR`): when *every* sentence of an answer fails Jev, one short
   Gemini call rewrites those sentences to say only what their quote says and what the source is ("Ein Video mit dem
   Titel «…» zeigt …"), without details taken from the question. Same quotes, Jev checks again; only a supported
   rewrite is shown. Costs one extra call only on answers that would otherwise be empty. Prompt `prosa-9`.
2. **Record questions go to the documents** (`engine.RECORD`: Rekord, Bestmarke, Allzeit, all-time, record): the
   stats step and the fast path skip them; articles report records, our season rows only add up points.
3. **Long documents read where the search words are** (`engine.passage`, `search_terms`): Jev and the writer get a
   document's start plus the part with the most words of the question (and, in the agent, of all its search
   variants), instead of only the start. Quotes stay verbatim, so `quote_in` still checks them.

## Result
`chat-eval` on live data: **65/65**, nothing worse, S3/S4/S7 better; $0.06 for the run, median 2.9 s.
