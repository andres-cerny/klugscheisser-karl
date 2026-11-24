# Build plan v4: add people on demand, sources overview, chat-first UI

Status: agreed 2026-10-03 (answers from Ondřej); built 2026-10-03 (see docs/web-app.md).

## Decisions
- Chats and searches are stored **on the server**, shared across devices (single user now; own server later).
- **Chat with follow-ups** ("und Lara Stalder?", "was hat sie dazu gesagt?"): conversation context per chat.
- Person page: **info on one side, chat about that person on the other**; mobile: tabs "Info | Chat"
  (chat tab opens by default when coming from a chat) — to be confirmed in the first test.
- Instagram handles entered by Fabi go through the **tiered verification** (evidence "von Fabi angegeben"
  + profile checks → wahrscheinlich / verifiziert), not auto-verified.
- Adding people: always show **cost estimate + ask**; hard cap **5 CHF per action**.
- Adding a team includes **coaches and staff by default**.

## Features
1. **Add a person** (search has no hit → "➕ Person hinzufügen"):
   popup: name (prefilled), Instagram handle (optional), free text with details.
   Resolve: Wikidata + Wikipedia + Brave web + Instagram profile (if handle) → Jev picks the candidate that fits
   the details → reply with a concrete description (name, sport, nation, born, club/team, handle + status,
   sources) → ✅ Ja · ❌ Nein · ✏️ Präzisieren (free text → new proposal).
   On "Ja": create person (role, sport), handle candidate with evidence, then background load (profiles,
   Instagram, news, results) with progress.
2. **Add several** (popup or chat: "Füge das schwedische Frauen-Nationalteam hinzu"):
   interpret request → find roster (IIHF/federation/Wikidata team membership/Wikipedia roster sections) incl.
   coaches/staff → reply with team, source, count, sample names, cost estimate → Ja/Nein/Präzisieren →
   background load with progress, cost cap 5 CHF.
3. **Sources**: page "Quellen" (all general sources with link, what we take, on/off, terms note) +
   per person "Datenquellen" box (Instagram, Wikidata, Wikipedia, EliteProspects, college page).
4. **UI**: navbar logo + 🔎 Suche + 💬 Chat. Home: search field and chat field, below recent searches and chats
   (newest first, no alphabetical lists). Chat page: conversation history with sourced answers (statements with
   «quote» + link), follow-ups, "neuer Chat". Calmer design (fewer boxes/tags, more whitespace).
   Person page: two columns (info | chat about her); mobile tabs.

## Technical notes
- New tables: `chats` (id, title, created/updated, context person/team/event), `chat_messages` (chat_id, role,
  text, answer JSON, created), `searches` (query, result ref, created), `add_requests` (kind person/team,
  input, proposal JSON, status proposed/confirmed/rejected/loading/done, cost estimate, job_id).
- Follow-ups: the answer engine gets the last turns (questions + statement summaries + resolved persons) to
  resolve "sie", "und X?"; scope carries over unless a new person is named.
- Person resolution for adds reuses Wikidata (any sport/occupation, not only ice hockey), Wikipedia, Brave,
  and Jev identity checks (calibrated phrasing as in v3).
- Generic sport layer: sports registered on the fly for adds outside ice hockey (name_de/person_de from the
  confirmed proposal), so skiing works without code changes.
