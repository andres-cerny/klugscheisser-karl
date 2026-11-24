# Story groups in search (2026-10-25)

**Problem.** In MCP `suche` (live, 15 queries × 20 hits) ~27% of the hits and ~28% of the characters were the same
story again: "Trainer Nationalteam Schweiz" showed Shannon Miller's appointment 10 times (SRF, RTS, RSI, Blick, TA …),
"Transfer PWHL Schweizerin" 9× Alina Müller to Hamilton, "Claire Thompson" 10× the same retirement.

**What it does now.** After Jev has scored the candidates and before the top N are taken, documents of one story are
folded into one card. The card keeps its full text; one extra line lists the others, each reachable with `dokument(id)`:

```
**[1] Shannon Miller ist neue Headcoach des Schweizer Frauennationalteams**
Typ: Webseite · Quelle: swisshockeynews.ch · 2026-07-10 (vor 3 Monaten) · über: Shannon Miller
…
Auch berichtet von: SRF, swisshockeynews.ch, RTS (+4) – dokument(6852, 6846, 6981, 6554, 6971, 6919, 6987, 6944)
Volltext: dokument(6855)
Link: https://…
```

The freed places go to the next distinct stories, so the list stays full. Query time only, nothing stored
(`retrieval/stories.py`; one SQL query per search, 7-15 ms).

## Rule

Same story = all of
- both documents may be grouped: never profiles/Wikipedia & co., result sheets, stats pages, multi-topic digests or
  tickers ("Sportnews-Ticker", "Die wichtigsten News", "Briefing" …);
- same person (shared linked athlete), or the same team when neither document names a person;
- published within **48 h** of each other;
- best passage-pair similarity of their passage vectors (`item_chunks`, gemini-embedding-2) **≥ 0.91**;
- compared with the group's first (best) member only, no chains.

Main card: best Jev score; on a tie the primary source (club/league/federation/the player's own post > SRF and
public broadcasters > other media > the rest).

Switches: `SEARCH_GROUP_STORIES` (MCP `suche` and `search`, default **on**), `CHAT_GROUP_STORIES` (answer engine:
web chat and MCP `frage`, default **off** until `chat-eval` has been run with it). The web search page lists
persons/teams/events, not documents, so it has nothing to group.

## Threshold test

- 48 searches (the 15 live queries on `klugscheisser_phasef`, the 33 lab queries on `klugscheisser_searchlab`),
  real Jev scoring; all pairs in each top 40 that pass the person/team + date check: **713 pairs**.
- Labels "same story?" from the presenter's point of view ("would she lose a distinct fact if B were only a line
  under A?"): Claude Haiku 5.5 on all 713, Claude Sonnet 5.5 on a stratified sample of 150 (25 per similarity band
  0.84-1.0). I read all Sonnet "not same" pairs ≥ 0.89 and 16 random "same" pairs ≥ 0.91: agreed with all 16 "same";
  the "not same" ones were real (follow-up interview 3 days later, the next game of a tournament, a news digest).
- 72 h and no digest rule reached at best 0.96 precision even at high similarity (digests, the next game, follow-ups);
  48 h + digest rule + 0.91:

| labels | merges | wrong | merge precision | merge recall |
|---|---|---|---|---|
| Sonnet sample (150) | 65 | 1 (two players' generic "gold!" posts) | 0.985 | 0.58 (0.70 weighted by band size) |
| Haiku all (713) | 355 | 7 (3 of them I'd call same) | 0.980 | 0.67 |

Target was ≤ 1 wrong merge in 20 (precision ≥ 0.95): met with margin. Recall is deliberately lower (a duplicate
kept is better than a story lost); e.g. an Instagram post and an article about the same signing are often < 0.91.

## Before / after (same Jev scores, grouping applied afterwards)

Distinct stories counted with the same-story labels (pairs without a label count as distinct).

| | before | after |
|---|---|---|
| distinct stories in top 8 (15 live queries, mean) | 5.6 | 7.1 |
| distinct stories in top 20 (15 live queries, mean) | 14.1 | 17.9 |
| distinct stories in top 8 (33 lab queries, mean) | 6.6 | 7.4 |
| lab: stories with a relevant document in the top 8 (sum) | 126 | 156 |
| lab: nDCG@8 (raw, duplicates count as gain) | 0.782 | 0.747 |
| lab: relevant documents lost from the top 8 | – | **0** (29 left the top 8 as cards, all folded under a shown card of the same story) |
| characters, 8 cards (live queries, mean) | 6,891 | 7,033 |
| characters, 20 cards (live queries, mean) | 12,245 | 12,337 |
| grouping time | – | median 7-10 ms, max 18 ms (search itself ~0.7-0.8 s) |

Raw nDCG falls because a second copy of a relevant story counted as a relevant hit; the freed places get the next
(less relevant) stories. Per live query:

| query | top 8 | top 20 | chars (20) |
|---|---|---|---|
| Rücktritt Nationalspielerin | 6 → 7 | 12 → 19 | 12,579 → 12,511 |
| Kreuzbandriss | 3 → 7 | 14 → 19 | 12,166 → 12,121 |
| Lara Stalder | 6 → 8 | 17 → 19 | 12,380 → 13,133 |
| Alina Müller Verletzung | 7 → 7 | 13 → 13 | 9,133 → 8,596 |
| Transfer PWHL Schweizerin | 4 → 6 | 8 → 17 | 12,786 → 13,303 |
| WM 2026 Kader Schweiz | 8 → 8 | 20 → 20 | 11,400 → 11,444 |
| Andrea Brändli Detroit | 4 → 4 | 5 → 14 | 13,608 → 13,598 |
| Claire Thompson | 6 → 8 | 11 → 19 | 13,584 → 14,402 |
| Poulin Verletzung | 5 → 7 | 15 → 18 | 11,984 → 11,723 |
| Euro Hockey Tour Södertälje | 8 → 8 | 20 → 20 | 12,348 → 12,400 |
| Trainer Nationalteam Schweiz | 1 → 6 | 9 → 15 | 13,210 → 12,927 |
| Sinja Leemann | 7 → 8 | 19 → 20 | 12,234 → 12,297 |
| Olympia 2026 Schweiz Bronze | 4 → 6 | 12 → 15 | 12,263 → 12,058 |
| Northeastern Huskies | 8 → 8 | 20 → 20 | 10,817 → 10,909 |
| EV Zug Saisonstart | 7 → 8 | 17 → 20 | 13,184 → 13,634 |

Paid: ~$0.62 (Jev on 48 searches ~$0.07, Gemini query vectors < $0.01, Haiku labels $0.10, Sonnet labels $0.44).

## Known limits
- Leader-only comparison keeps some duplicates (20 Minuten on Miller's appointment is 0.89 to the main card but
  0.92 to two folded ones). Comparing with every member would merge more but allows chains.
- Wording on the folded line is the source name; the same outlet can appear (another article, e.g. its English
  version).
