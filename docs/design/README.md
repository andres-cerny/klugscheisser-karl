# UI design (v9): Variant C chosen

Canvas: https://claude.ai/artifact/M1XsZA6JpiEJSeynXWsjcQ (private; rows C final, A and B drafts).
`variant-c/*.dc.html` are the reference mockups (Design-canvas format: plain HTML with inline styles inside
`<x-dc>`; `<sc-if>` = alternative states). `ui-brief.md` = the assessment of the old UI and the brief.

Decisions by Ondřej (2026-10-04): Variant C, few or no emojis; Instagram handle and data age ("Stand: …" with
refresh) under the name and under team/event titles; phone: Info | Chat buttons under the person (and Kader | Chat
for teams/events); desktop: chat always visible next to the person/team, with an enlarge button and "Nächste
Fragen" hints; Erfolge block (medals first); fun facts as one sentence with a dropdown (quote + source link);
every timeline entry links to its original source; admin things (tier, Alles neu laden, data sources, costs)
under "Mehr".

## As built (v9, branch of the UI build)

Built into the real app (`src/klugscheisser/web/`: `html.py` tokens/CSS/JS/icons, `person.py`, `timeline.py`,
`roster.py`, `card.py`, `views.py`); one responsive template, breakpoints 900 px (chat column appears, Info|Chat
switch goes) and 1180 px (Steckbrief becomes the left column). Display name "Klugscheisser Karl".

Differences from the mockups, and why:
- No bottom tab bar on the phone (Person | Suche): the sticky top bar already has the search field and "Mehr".
- The Wikipedia intro is in the Steckbrief (drawer / left column), not in the desktop header: it is up to 700
  characters verbatim and would push the hooks down.
- The photo in the header is small; its licence credit is repeated visibly in the Steckbrief (the header keeps
  it for screen readers).
- Vorsicht line: the topic is the newest report's title as stored (often English), not an edited summary.
- Timeline rows: the "kind" line shows the source meaning without emoji ("offiziell · Freude · Bild: …"); titles
  wrap to two lines on the phone.
- Roster: grouping follows the roster entry's role; the last column shows a Vorsicht report or the newest post
  (desktop) instead of repeating the roster source per row (the source is in the "Provisorisch" note once).
- "Nächste Fragen" are templates over her own data (newest Vorsicht topic, first fun fact, next/last event) —
  deterministic, sometimes a bit stiff; no LLM.
- Medals: only from Wikidata statements (P166 medal items, P1344 + ranking 1–3) or award facts whose text names a
  medal. Real data needs a Wikidata re-import before medals appear.
- Dark mode kept (token set for `prefers-color-scheme: dark`); the mockups are light only.
