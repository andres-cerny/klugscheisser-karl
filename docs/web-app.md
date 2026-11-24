# Web app (v3 P6)

Own web app on the same Python server as the MCP endpoint: server-rendered HTML + htmx (from unpkg), no
separate frontend stack, German UI, mobile first, light/dark via `prefers-color-scheme`. Code: `src/klugscheisser/web/`.

v9: display name "Klugscheisser Karl" (was "Klugscheisser Curdin" until 2026-10-11) (code name stays `klugscheisser`). Design "Variant C" (`docs/design/`): Barlow /
Barlow Condensed (Google Fonts), ink #0D1117, grey #545C68, one accent (cobalt #1B44E0), amber only for Vorsicht;
no emoji labels, a small set of inline stroke icons (`html.icon`). One responsive template for phone and desktop.
Every statement keeps its source link (a quiet grey chip or the whole row as link). The data age ("Stand: …",
older than 7 days in amber) sits under every person, team and event title; Vorsicht comes first.
Only the chat uses an LLM; all other pages are deterministic.

## Projects (v7)

Every request belongs to one project (`projects.yaml`), each with its own database: `/p/<key>/…` opens the app in
that project and remembers it (cookie). Views write inner links without prefix (`/person/12`); the
ProjectMiddleware adds the request's prefix to every root-relative link of an HTML response (`href`, `action`,
`src`, `hx-*`; not `/login`, `/logout`, `/static`, `/mcp`), so a page never leads into the other project after
the cookie changed in another tab (2026-10-19). Navbar: project switcher
in the "Mehr" menu, with "Projekt: Briefing & Einstellungen". `/projekt` holds:
- **Briefing** (Fabi's text, versioned; every answer stores the version it used) and **Was wir gelernt haben**
  (suggested weekly or on request from her questions and ratings; only applied when she accepts or edits it).
  The briefing goes into the answer writer's prompt (answers, cross-person answers, fun facts); never into
  classification or ranking, so a preference never hides data.
- **Settings** for this project: cost caps, nightly hour, news interval (stored in the project's database).
- The **Perplexity address** of this project (`/mcp/<key>`) and the direct app link.
- Microphone button next to every question field: browser speech recognition (free, nothing goes through our server);
  hidden where the browser has none. Phone keyboard dictation works anyway. v12: listens until the button is tapped
  again or ~3 s of silence; recognised words go into the field after what was typed, words still being recognised
  show grey above it; the button turns red and pulses ("Ich höre zu …"), small bars flash with every recognised
  word, short vibration on start/stop; DE/EN switch next to it (de-CH / en-US, remembered in the browser); errors
  (no speech, microphone not allowed) show a short German hint. `MIC_ANALYSER` (off) would show the real sound
  level from a second mic stream.
- v12 `klugscheisser chat-replay [--project KEY|--all] [--limit N] [--styles liste,prosa] [--out PATH] [--fake]
  [--yes]`: answers every stored chat question again with the old and the new answer style and writes a
  side-by-side HTML report (default `data/chat-replay-<time>.html`) with dropped / unsure counts, cost and Fabi's
  rating. Read-only for the chats, no answer cache; without `--yes` it only counts and estimates the cost.

## Tiers and deep sources (v8)

- Badge "Tier 1 · 64 Quellen" (Quellen = stored documents with a link) in search results and in the person
  page's "Daten" section; rosters show a quiet "T1". Tier 1 = Swiss pool and staff, tier 2 = other WM teams,
  tier 3 = no badge.
- Person page: national and club number on the card ("#7 (Schweiz) · #7 (EVZ Women's Team)"), section
  podcasts and videos (links only, in the timeline), German translation under quotes from sources neither in
  German nor English, the Wikipedia intro prefers German/English.
- Rosters show head coaches ("Headcoach") after the players.

## Pages (v4, v9 layout)

Header (ui-loop 2026-10-17): logo (phone: just the "K" mark) · the two places **Suche | Chat**, always visible,
the current one marked · on wide screens a quick search field (names, teams, events with suggestions while
typing; a sentence is offered to the chat as a suggestion row, never sent there by itself) · "Mehr" menu (project switcher,
Projekt page, Alle Chats ("Neuer Chat" removed in round 2: the Chat tab is the new chat), Person/Team hinzufügen, Quellen, Bewertungen; on a person page also
"Daten zu dieser Person"). Chats and searches are stored on the server (tables `chats`, `chat_messages`,
`searches`), so they are the same on every device.

Every sideways scroller (filter chips, stat tables, tabs, answer tables) fades out on the side where more is
hidden (`xs`, set by the page script). The "Stand" line joins sources fetched the same day ("Instagram, News,
Resultate heute 05:08", oldest time; older than 7 days stays apart in amber). Typing suggestions: arrow keys and
Escape, last row "Alle Treffer für «…»".

**Forgiving search** (`web/search.py`, web only; MCP and chat keep their own lookups): typos (1 letter in words of
5–7 letters, 2 from 8), missing accents/umlauts, word order, partial words while typing, swapped letters
("Stlader"); country names in German/English/French/Italian plus nicknames ("Switzerland", "Suisse", "Svizzera",
"Damkronorna" → the national team first); event words in other languages ("Olympia", "World Championship").
English national-team records from imports ("Finland women's national ice hockey team") are folded into the
German national team's row ("Auch erfasst als: …", still linked; nothing deleted).

**Person page** (`web/person.py`, `web/timeline.py`): header with photo slot, name, fact line
("Stürmerin · SUI · #7 · 32 J. · EV Zug" from the sourced Steckbrief facts), Instagram handle with status,
pronunciation buttons (not for German-language names), "Stand: Instagram … · News … · Resultate …" + Aktualisieren,
Vorsicht as one collapsed amber line (newest topic, date, number of reports) that opens into the linked reports.
Phone: "Profil | Chat zu <Vorname>" switch under the header (`?tab=chat` opens the chat; ui-loop round 2, was
"Info | Chat"). Profil: filter chips with counts (Alles · Statistik · Erfolge · Fun Facts · Vorsicht · Social · News ·
Podcasts; client side) in a bar that stays visible under the switch while scrolling (a chip jumps back to the top of
the content), "Steckbrief & Karriere"
(drawer on the phone, left column from 1180 px: key facts, intro, career, photo credit, Instagram bio, Anlässe),
**Erfolge** (facts of kind medal, award, milestone, captaincy; medals first with a gold/silver/bronze label;
same text once; phone 4 + "Alle n Erfolge", desktop two columns; each links to its source), pinned **Fun Facts**
(one sentence each; tap shows the verbatim quote, source + date, German translation when needed), then ONE dated
**timeline** (visible heading "Zeitleiste"; whole months until 12 rows, older months behind "Ältere Einträge
anzeigen (n)", opened by any filter chip; phone: the source chip sits in the row's second line) ("Demnächst" for upcoming events, then by month) merging Instagram/X posts, news, results, web pages,
podcasts/videos and events; every row links to the original (new tab). Bottom, collapsed: **Daten & Einstellungen**
(Tiefe der Suche, Alles neu laden, Verein laut Quellen, handles, profiles, data sources).
Desktop: the chat about her is always visible on the right (sticky), "Vergrössern" widens it to ~60 % (remembered
in the browser), "Nächste Fragen" chips (3 hints from her data: newest Vorsicht topic, a fun fact, the latest or
next event; no LLM) sit under the latest answer; on the phone they sit above the input, which stays at the bottom.

**ui-loop round 3** (2026-10-17): person page starts with **Auf einen Blick** (`web/glance.py`, from data already on
the page: current club, this season's line, next tournament, a medal, the two newest entries; every value with its
source chip; the season line falls back to the last season with games; left out when there is too little). The Steckbrief is no longer a long narrow left column: it sits in
the main column after the glance (phone: drawer; from 1180 px an open section with a three-column fact grid);
Karriere newest first, older stations behind "Frühere Stationen anzeigen (n)", source chips in their own column.
No photo: initials (header and search rows). Search result rows: photo (Wikimedia, credit as tooltip) or initials,
name, fact line, tier badge; teams/events with an icon. Phone chat tab: no repeated "Chat zu …" heading, the "+" reads "Neuer Chat".
Spiel-Brief: header like team pages, jump bar, source chips instead of "[Quelle]", rosters side by side from
1000 px grouped Tor / Verteidigung / Sturm, connections after 6 behind "Alle n Verbindungen". Quellen: jump bar,
cards (two columns on desktop), aktiv/aus labels. Login and unknown addresses: a calm card with logo only (no
navigation, no project names); a last GET route renders the 404 page, `/mcp*`, `/.well-known/*`, `/healthz` keep
Starlette's plain "Not Found".

**ui-loop round 4** (2026-10-17): orientation on long pages. Person page: the sticky chip bar follows the page
order like a table of contents (new chip **Steckbrief** with the number of sourced facts; on the phone it opens the
drawer); Erfolge on wide screens show 8 (two columns of four) + "Alle n Erfolge" (phone still 4). Event page: with
two or more rosters a "Springen zu" chip bar (one chip per team + "Weitere Teams"); teams without a stored roster
are one compact block "Weitere Teams · n · Kader noch nicht erfasst" (team links by group) instead of one big
heading each. Anchor jumps land below the sticky header (`scroll-padding-top`). The chat field next to a long
event/team name says "Frage zum Anlass …" / "Frage zum Team …" (the placeholder never wraps). Search: words that
only say "women's" in any language (Frauen, Damen, Ladies, Women's, Féminin, Femmes, Donne …) match each other in
team and event names («EV Zug Frauen» → EV Zug Damen; the men's team does not match), filler words in event
queries are dropped («Championnat du monde» → Frauen-WM), "Meinten Sie" puts tier 1 first on equal spelling.

**ui-loop round 5** (2026-10-18, Ondrej's picks): the glance's season tile no longer repeats club and league when the
club tile shows the same team (only the stats line + season); the Steckbrief drops its "Anlässe" list (dates and
roster status are in "Auf einen Blick" and the Zeitleiste "Demnächst" / by month) and keeps one line of links to
the event pages; Erfolge on wide screens show 6 (phone 4); Spiel-Brief connections whose two chips come from the
same source read "EP · Müller" / "EP · Meier" (links unchanged). 404 keeps "Zur Suche" + "Zur Startseite".

**Team / event pages** (`web/roster.py`): title, "Stand: Kader <date> (<source>) · Instagram … · News …",
"Provisorisch" note once, roster as a dense list (number · name · position · club · Vorsicht or newest post),
grouped Tor / Verteidigung / Sturm, staff after. Phone: "Kader | Chat"; desktop: the team/event chat on the right
(latest chat with that context, or a new one), hints "Wer hat die meisten Länderspiele?", "Wer spielt im Ausland?",
"Wer ist verletzt?". Event: Spiel-Brief button; "Vorbereitung" and "Alles neu laden" in the collapsed "Daten" part.

| Path | What |
|---|---|
| `/` | Two separate cards: **Suche** (field; add person / team) and **Chat: Karl fragen** (question box → new chat); recent searches, recent chats, current tournaments |
| `/suche?q=` | Suggestions (htmx) or full result page (logged as a search), grouped Personen / Teams / Anlässe with counts; a question → "Das ist eine Frage? Im Chat fragen"; no hit → "Meinten Sie …?" (links straight to the page), tips, "«…» hinzufügen", "Im Chat fragen". Without `q`: field, recent searches, current tournaments |
| `/person/{id}` | Person page as above (`?tab=chat`, `?neu=1` for a new chat) |
| `/person/{id}/posts?offset=` | Older Instagram posts as timeline rows |
| `/team/{id}`, `/event/{key}` | Roster pages as above (`?tab=chat`, `?neu=1`) |
| `/chat`, `/chat/{id}`, `/chats` | New chat (optional `?person=`/`?team=`/`?event=`), one conversation, all chats. Answers: Datenstand, Vorsicht, `aussage «zitat» [Quelle]`, Unbestätigt (collapsed), Lücken, cost |
| `POST /chat/{id}/nachricht`, `POST /chat/{id}/antwort` | Store the question (shown at once), then compute the answer (htmx `load` trigger; one answer at a time per chat, idempotent) |
| `/hinzufuegen` (+ `/{id}`, `/{id}/ja`, `/{id}/nein`, `/{id}/praezisieren`) | Add a person or team: proposal with sources and cost estimate → Ja / Nein / Präzisieren → background load with progress |
| `/quellen` | All general sources: link, what we take, on/off, terms note |
| `/frage` | Old URL: GET redirects to `/chat`, POST creates a chat |
| `POST /aktualisieren`, `/aktualisieren/{job_id}` | Refresh button (unchanged) |
| `POST /alles-neu` (`target=person:<id>` or `event:<key>`) | v8 "Alles neu laden" for tier 1/2: web/news search, Instagram, tagged posts, Facebook, X, then facts, fun facts, translations; background job, cap per person ($2 tier 1, $0.40 tier 2) |
| `POST /person/{id}/tier` | v8: "Tiefe der Suche" (automatisch / Tier 1 / 2 / 3); a manual choice wins |
| `/login`, `/logout` | Only with `WEB_PASSWORD` |

**Medals** (v9): fact kind `medal` (`facts/catalog.py`). The Wikidata import stores one when a person has "award
received" (P166) of a medal item (gold/silver/bronze medal) or "participant in" (P1344) an Olympics / world
championship with ranking (P1352) 1–3. Without such a statement nothing is shown.

**Follow-ups.** The answer engine gets the last `CHAT_HISTORY_TURNS` (4) question/answer pairs as context only
(never quoted). Scope: a person named in the question wins ("und Lara Stalder?"); otherwise the previous turn's
persons ("was hat sie dazu gesagt?"); otherwise the chat's page context. Search for documents uses the previous
question plus the follow-up.

**Answer style** (v12). `WEB_ANSWER_STYLE` / project setting `web_answer_style` (row in `project_settings`, wins):
`liste` (default: one checked statement per line, as v5–v11) or `prosa` (written answer). MCP `frage` is always
`liste`. Prosa: ask-back from the app (`cross.ask_back`, no model) → stats rows as a table (no model) → computed
documents from `cross.prosa_inputs` or the usual document search → Gemini (`SYSTEM_PROSA`, `PROSA_VERSION`) writes
text and/or table blocks, every sentence/row citing 1–2 documents with verbatim quotes. Checks per sentence/row:
documents exist, `quote_in`, relationships/health only from the person herself (else dropped, "verworfen"); Jev
support below `ANSWER_SUPPORT_THRESHOLD_PROSA` → collapsed "Nicht sicher belegt". Confidence wording from the real
checks (`ton`): ≥2 sources or computed → plain; one source → "Laut SRF: …"; Vorsicht source → "Blick berichtet:
…" (added when the model wrote a plain statement). Nothing found → "Dazu habe ich nichts gefunden." + closest
sources. The page shows Datenstand · Verstanden als, the text with [n] markers (tables scroll sideways on the
phone), the numbered sources (computed ones "Berechnet aus gespeicherten Daten" + their basis links), then
collapsed unsure sentences, gaps and what was searched; ask-backs and next questions are tap-to-send chips.
Follow-ups pass the previous answer's sentences to the writer. Cache keys include style + prompt version.

**Adding people** (`src/klugscheisser/adds/`): `flow.py` (propose / refine / reject, chat command "Füge … hinzu"),
resolvers (Wikidata, Wikipedia rosters, Brave, Jev fit check), `load.py` (create persons, handle candidate with
evidence "von Fabi angegeben" → tiered verification, never auto-verified; link stored news by name; Instagram
for as many persons as the 5 CHF action budget allows; results and profiles). Coaches and staff are included for
teams. Unknown sports are registered on the fly (generic sport layer).

Page loads never block on an update: when `AUTO_REFRESH` and `WEB_AUTO_REFRESH` are on and the person's Instagram
was not fetched today, the person page *starts* the daily update in the background and shows its progress.
Event and team pages never start paid updates on their own; use the Aktualisieren button. Page views by bots and
headless browsers (user agent `HeadlessChrome`, `bot`, `crawler`, `curl`, `python-…`, link previews; `web/bots.py`)
start no update at all (2026-10-25; `WEB_REFRESH_FOR_BOTS=true` = as before).

The question box uses `klugscheisser.answer.engine.build_answer_engine(settings)` (P3) when it exists, otherwise
`FakeAnswerEngine` (no LLM: quotes the first sentence of the three newest stored items, marked "Testmodus").
`WEB_ANSWER_ENGINE=fake` forces the fake.

## Run locally

```bash
uv sync --all-extras
# standalone web app (no MCP endpoint), open http://127.0.0.1:8765
uv run uvicorn --factory klugscheisser.web:build_web_app --port 8765
```

In production the routes are mounted into the MCP server app (`klugscheisser serve`), next to `/mcp` and
`/healthz`.

## Access control

| Setting | Effect |
|---|---|
| none | Open. Local development only. |
| `WEB_ACCESS_EMAILS=["fabi@…","ondrej@…"]` | Header `Cf-Access-Authenticated-User-Email` (set by Cloudflare Access) must be in the list, else 403 |
| `WEB_PASSWORD=…` (+ optional `WEB_SECRET`) | Password form at `/login`, HMAC-signed cookie for `WEB_SESSION_DAYS`; works alongside the email list |

POSTs from another origin are rejected (CSRF guard). `/mcp` keeps its own bearer token.

### Cloudflare Access setup (P7)

1. Cloudflare Zero Trust → Access → Applications → *Self-hosted*, domain e.g. `athlete.<domain>`, path empty.
2. Policy *Allow*, include *Emails*: Fabi's and Ondřej's address. Login method: *One-time PIN* (email code).
3. Add a second application (or a *Bypass* policy) for path `/mcp` so the MCP connector is not sent to the
   Access login; `/mcp` is protected by `MCP_AUTH_TOKEN`.
4. Set `WEB_ACCESS_EMAILS` to the same addresses.

The email header can be trusted only because the server binds to `127.0.0.1` and is reachable only through the
Cloudflare tunnel. Never expose the port directly while relying on `WEB_ACCESS_EMAILS`.
