# Brief: cleaner UI for "Klugscheisser" (research app for a Swiss TV sports presenter)

## Who and what
Fabi is a sports presenter (women's ice hockey). Before and during a broadcast or an interview she opens a player's
page, often on her PHONE, and needs within 5 seconds: who is this (identity), what can I say (hooks: fun facts,
latest news, notable posts), what must I be careful with ("Vorsicht": injuries, private topics), and — only on
tap — where it comes from (every statement in this app has a source link; sources must stay reachable but quiet).
She can also chat with the data ("Frage zu Lara Stalder …"). UI language: German (Swiss usage: "ss" not "ß").

## What is wrong with the current UI (assessment of screenshots)
1. No hierarchy: a person page is ~9,000 px long; card, fun facts, clubs, handles, bio, settings, buttons,
   "Vorsicht" with full quotes, Instagram posts with full AI image descriptions, news, results, profile, podcasts,
   events and a 50-row "data sources" list are stacked with equal weight.
2. Emojis used as labels everywhere (✨🏒📸🪪🎚️🔄⤵️✅🎉ℹ️➡️🖼️🔤⚠️🌐🤕): noisy, meaning unclear.
3. Source links dominate facts: "#7 (Schweiz) · #7 [Kader IIHF Frauen-WM 2026, Herning/Esbjerg (provisorisch)] [Web]".
4. Duplicates (club shown twice, "Datenstand" several times).
5. Instagram cards dump the full image description plus OCR text: one post = half a screen.
6. Admin controls on her page (tier switch, "Alles neu laden", data-source list, cost per answer).
7. Phone: first screen is a fact table full of source links; hooks come several screens later.
8. Roster: each player 3–4 lines, "provisorisch" and tier badges repeated per row.
9. Home: two inputs (search vs question) without clear difference.

## Must keep (functions)
- Identity: name, nationality (SUI etc. — no flag emoji), position, number (national + club), age, club, a one-line
  intro (Wikipedia first sentence).
- Fun facts (3–5), each with a short quote and source.
- Vorsicht items (sensitive topics) — must be visible early but calm (not a big orange box).
- Latest: Instagram posts (caption, one-line image description), news, results, podcasts/videos (links only).
- Career (seasons/teams), events/rosters she is in.
- Sources reachable for every statement (small marker / tap to reveal / bottom sheet) — never long inline labels.
- Chat with the data about this person (desktop: side panel or slide-over; phone: own tab or bottom sheet).
- "Aktualisieren" (refresh) exists but secondary; admin things (tier, "Alles neu laden", data sources, costs)
  belong in a secondary "Daten"/"Mehr" place, not in Fabi's main flow.
- Search is the entry point (one field: names, teams, events; a question goes to chat).

## Real content
Use ONLY the real content in `ui_sample.json` (same folder as this brief): Lara Stalder's page (head, card_facts,
intro, career, fun_facts, vorsicht, instagram, news, profil, medien, anlaesse, datenstand), a search ("Lar"), and
the Swiss roster of the IIHF Frauen-WM 2026 (provisional). Shorten texts sensibly; never invent facts or numbers.
Missing values become a visible placeholder like [Foto].

## Deliverable: 3 artboards (files), written to
`/private/tmp/claude-501/-Users-ondrejcerny-dev-Fabi-agent/4f37cc2a-85fa-4c93-8557-eb2c2c8a7b4e/scratchpad/ui-canvas/project/`
- `<P>-Person-Desktop.dc.html` — person page, desktop PAGE, fluid (designed at 1280 wide), height = its real height.
- `<P>-Person-Phone.dc.html` — same person page on a phone, fixed 390 px wide (height as needed, e.g. 1700).
- `<P>-Roster-Phone.dc.html` — the Swiss WM roster on a phone, fixed 390 px wide.
`<P>` is your variant letter (A, B or C). Do NOT write canvas.json, do NOT publish anything, do NOT run a browser
or screenshot, do NOT touch the repository. Only write your three files. When done, report in ≤15 lines: the
idea of your variant, the type/colour choices, the sizes (w×h) of each artboard, and anything you assumed.

## File format (.dc.html) — rules that fail silently if broken
Each file is one self-contained page in this exact skeleton:
```html
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Lara Stalder – Variante A</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=...&display=swap" rel="stylesheet">
<style>
body{margin:0}
a{color:#...}a:hover{color:#...}
</style>
</helmet>
<div style="...root...">
  ... markup ...
</div>
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{"$preview":{"width":390,"height":1700}}'>
class Component extends DCLogic {
  renderVals() { return {}; }
}
</script>
</body>
</html>
```
- Keep `<script src="./support.js"></script>` EXACTLY. Close every non-void element, quote every attribute.
- All styling inline `style="…"`; `<helmet><style>` only for body margin, link colors, font imports.
- Phone artboards: root fixed `width:390px` and the declared height. Desktop: fluid root (`max-width` container,
  no px width on the root; flex-wrap so it still works narrow), `$preview` width 1280 and its real height.
- Layout with flex/grid + gap. Grid columns as `repeat(N, minmax(0, 1fr))`.
- `{{hole}}` = only a dotted lookup into `renderVals()`; no expressions. Prefer literal markup for copy.
- Interactivity (tabs etc.) is optional; if used: state in `this.state`, handlers returned from `renderVals()`,
  `onClick="{{ openTab }}"`, `<sc-if value="{{ isX }}" hint-placeholder-val="{{ true }}">…</sc-if>`. A static
  design showing one state is fine.
- No network except a Google Fonts css2 `<link>`. No images except placeholders (a grey box labelled [Foto]).
- Icons: small inline stroke `<svg>` only, NEVER emoji (no flags either: write SUI). Few icons, functional only.
- Real `<button>`, `<a href>`, `<input>`+`<label>`; text contrast ≥ 4.5:1 (grey captions too); touch targets
  ≥ 44px on phone; distinguish states by lightness, not hue alone.
- No AI tropes: no gradient washes, no left-border cards, no emoji; avoid Inter/Roboto/Arial; not cream+serif+clay
  unless it truly fits. Do not imitate SRF's or any other company's branding.
- Calm and fast to scan: one accent colour at most plus a calm warning tone for Vorsicht; generous but compact
  spacing; type scale that makes the hierarchy obvious.
