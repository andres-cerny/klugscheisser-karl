# Perplexity instruction for the Phase F tool set (draft, 2026-10-25)

Goes with the switch `MCP_NEW_TOOLS=true` (default off). Not live until Ondrej switches it on and pastes this text.
With the switch on, Perplexity sees six read tools instead of 16:

| tool | for | replaces |
|---|---|---|
| `suche(frage, person?, team?, seit?, quelle?, anzahl?, neueste?)` | any question: facts from Karl's tables (staff, games, tournament facts, computed tables across persons, person status) + evidence cards (type, source, date, passage, one link); `neueste=true` = newest documents first | `search`, `frage`, `whats_new`, `get_athlete_posts` |
| `person(name, teil?)` | full portrait of one person, in parts | `get_athlete`, `get_ig_handles` |
| `team(name, wettbewerb?, teil?)` | roster + staff + handles | `get_team_roster` |
| `events(turnier?, team?, gegner?, letzte?, seit?, tage?, teil?)` | events, a team's last/next games, head-to-head, tournament sheet | `list_events`, `get_event` |
| `stats(...)` | numbers, rankings, comparisons | unchanged |
| `dokument(id, teil?)` | full stored text of a card (Instagram/X/paywall links need a login) | new (search lab) |

Hidden with the switch on: `status`, `refresh`, `refresh_status`, `hinzufuegen`, `hinzufuegen_antwort`,
`hinzufuegen_status` (adding a person goes through the web app). No tool writes an answer with Sonnet any more:
Perplexity writes it from the material. All output is plain words (no emoji prefixes, so no legend is needed).

## Where to paste

Perplexity → Project "Eishockey" → Settings → Instructions: replace the current text with the block below
(925 characters, limit 1,000). The same text is in `src/klugscheisser/gaploop/instructions_eishockey_f.txt` for
the stand-in (`scripts/mcp_standin.py run --instructions …`).

## Instruction "Eishockey F"

```text
Du hilfst einer TV-Sportmoderatorin zu Frauen-Eishockey (Schweiz und alle Gegnerinnen).
WICHTIGSTE REGEL: Bei JEDER Frage ZUERST den Connector "Klugscheisser Karl" aufrufen, vor jeder Websuche. Web nur ergänzend.
- suche für jede Frage; person (Porträt), team (Kader, Staff), events (Spiele, Resultate, Turnier), stats (Zahlen). Neueste Posts/News: suche mit neueste=true und person oder team.
- Wenige Treffer: nochmals suche mit anderen Wörtern, person= oder seit=.
- Erste Zeile: die Datenstand-Zeile. Dann Kurzantwort; Vergleiche als Tabelle.
- Jede Aussage mit Datum und dem Link ihrer Karte oder Zeile. Web als "Web" markieren. Bei Widerspruch gilt Karl.
- Auszug zu kurz oder Link mit Login: dokument(id).
- Vorsicht-Einträge zuerst, als heikel markiert. Zitate wörtlich.
- Nichts gefunden: "Keine verifizierten Daten gefunden." Nie raten; Fehlen ist kein Gegenbeweis.
- Antworte in der Sprache der Frage; ss statt ß.
```

## What changed against "Eishockey final" / v2 and why

- Tool line names the six new tools; `suche` is the default door (the old line routed by question type to five
  tools, and `frage` wrote an answer that Perplexity then rewrote).
- `neueste=true` for "what's new / latest posts": `suche` ranks by fit, so a generic "was postet sie" would
  otherwise show old but well-matching posts first.
- "Wenige Treffer: nochmals suche …": the cards are evidence, not an answer; a second, narrower call is cheap
  (~0.5-3 s, ~$0.0015 Jev).
- `dokument(id)`: cards show a passage of long texts; Instagram/X links need a login.
- Dropped: `hinzufuegen` (hidden), "Quelle für alle Zeilen" (cards have one link each; the table lines keep their
  own links), "frage-Web-Treffer sind ungeprüft" (no `frage`).

## How to switch on (live, Ondrej)

1. Backup first (`docs/server-handoff.md`), then in `~/klugscheisser/.env`: `MCP_NEW_TOOLS=true`, restart.
2. Paste the instruction above into the Perplexity project.
3. Run the probe questions below in Perplexity; check the server log
   (`journalctl -u klugscheisser -o cat | grep mcp_call`) for tool calls, sizes and times.
4. Back: `MCP_NEW_TOOLS=false` (or delete the line), restart, paste the old instruction again.

## Probe questions (from docs/search-lab/lab-results.md, Phase D)

1. "Welche Spielerinnen haben sich 2026 das Kreuzband gerissen?" – several cards cited, each claim next to its link?
2. "Fasse alles zusammen, was Karl über Lara Stalders Saisonstart weiss, mit Quellen." – multi-card citation;
   `dokument(id)` for a long card?
3. "Lies den ganzen Wikipedia-Artikel über Marie-Philip Poulin aus Karl und nenne ihre Verletzungen." – `dokument`
   with `teil=2`; is a 15k part read completely?
4. "Gib mir 20 Treffer zu Rücktritt Nationalspielerin." – `anzahl=20`: is the tail cut, does Perplexity notice?
5. "Was postet Alina Müller in letzter Zeit auf Instagram? Zitiere wörtlich." – does it use `neueste=true`?
6. "Welche Schweizerinnen spielen in der NCAA? Nur Karl, kein Web." – broad: one call or several?
7. "What did the Swiss team do in Lahti?" – English question, German cards.
8. The same question twice in one chat – re-call or reuse; latency in the log.

## Stand-in check (2026-10-25, copy DB, not live)

Gap-loop stand-in (Claude Sonnet as Perplexity, `scripts/mcp_standin.py`) on a branch instance with the copy
database `klugscheisser_phasef` (backup 25.10. 17:27 + live passage vectors), 14 questions (probes 1-7 above +
round-3 q01/q03/q07/q09 + seed s01/s04/p05), old tools + "Eishockey final" vs new tools + "Eishockey F":

| | old tools | new tools |
|---|---|---|
| stand-in cost | $0.58 | $0.70 |
| Karl's own model cost (`frage` Sonnet, Jev) | $0.12 | ~$0 (Jev only) |
| tool calls | 33 (9 of them `status`) | 32 |
| tool time per question (median / sum) | 0.8 s / 38 s (`frage` 4-10 s) | 1.2 s / 22 s |
| total time per question (median) | 12.5 s | 12.6 s |
| characters sent per question (median) | 4.1k | 13.0k |
| answer length (median) | 2.1k | 2.6k |

Read side by side, the new answers were better on 11 of 14, about equal on 2 (NCAA list, Swiss staff) and
worse on 1 (q09 Enzler: the old `frage` named her last national-team goal, 16.02.2026 vs Canada; the new answer
only had season totals). Better: more facts with their own link and date
(Lahti: scorers, saves and SRF/watson reports instead of only the three scores; Waser: the 11.10. assist and the
cause of the surgery, flagged as sensitive; injuries: the "seither" status per player plus non-cases ruled out
with sources), and `dokument` was used for the Wikipedia probe. Probe 4 (`anzahl=20`) hit the 12k cap: 19 of 20
cards shown, with the cut note. Costs and speed are about the same; Perplexity
reads about three times as many characters. Worth watching live: Perplexity's own limits with 12k answers and
4 `suche` calls for broad questions.
