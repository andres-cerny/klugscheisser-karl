# Build plan v10: offline package per event (installable web app)

Status: **plan, not built** (written 2026-10-04 with Ondřej). Order: v9 UI (Variant C) merged → server live
(docs/server-handoff.md) → v10 built on the Mac, tested on the server's HTTPS address with a real phone,
deployed with `git pull`.

## Why
In an ice arena (and later in the mountains for skiing) Fabi often has bad or no internet. The data she needs for
one event is small. She should be able to read it without a connection.

## What she gets
- The app installs on the phone like an app (home screen icon, full screen, own name "Klugscheisser").
- On an event page: **"Offline speichern"**. The app downloads everything for that event: both rosters (or all
  teams), every player's and coach's person page, the event page, the game briefs, and a small search index.
  Also per person ("Diese Person offline speichern").
- Without a connection: search and those pages work as usual. A quiet bar says
  "Offline · gespeichert am 05.11. 18:30". Data age lines show the stored state.
- Not offline: chat (answers are computed on the server), Aktualisieren, new posts/news, opening original
  sources (summaries and quotes are stored). These show a clear "Offline – nicht verfügbar".
- "Offline-Daten" in "Mehr": list of saved events with size and date, "Aktualisieren" (download again when
  online), "Löschen".

## How (technical)
1. **Web app manifest** (`/manifest.webmanifest`): name, icons (simple line-icon logo, 192/512 px + Apple touch
   icon), `display: standalone`, theme colour from Variant C, start URL `/p/eishockey/`.
2. **Service worker** (`/sw.js`, scope `/`): network-first for HTML when online (always fresh), cache fallback
   when offline; cache-first for CSS/JS/fonts/icons. Google Fonts (Barlow) self-hosted under `/static/` so they
   work offline. No caching of `/chat`, `/mcp`, POST requests or login.
3. **Offline package endpoint** `GET /offline/event/<key>.json`: list of URLs to store (event page, team pages,
   person pages, game briefs) + the search index for those persons (id, name, aliases, team, number, position,
   tier). Built from existing data, no new fetching, no paid calls. Person pages are rendered exactly as online
   (same templates), so offline = online minus live parts.
4. **Download** in the page: the service worker fetches the listed URLs into a named cache per event
   (`event-<key>-<date>`), shows progress ("120 / 280"), and stores metadata (saved at, size) in IndexedDB.
   Throttled (a few requests at a time) so the server is not hammered.
5. **Offline search**: the search field uses the stored index when offline (name/alias prefix match, umlaut
   variants like online).
6. **Auth**: pages are stored after login, so the cached pages need no login offline. The session cookie
   lifetime stays as today; when it expires, cached pages still show offline, live parts ask to log in again.
7. **Storage on iPhone**: works when the app is added to the home screen (Safari keeps that storage; a plain
   tab may lose it after weeks without use) — the app shows a one-time hint to add it. Ask the browser for
   persistent storage where supported (`navigator.storage.persist()`).
8. **Size check**: show the expected size before downloading (about 20–30 MB for a WM with ~300 persons;
   images are not stored, only text and the [Foto] slot / small Commons thumbnail if cached already).

## Tests
- Offline package endpoint: the right URLs for an event, nothing from other projects, no chat/MCP URLs.
- Service worker: unit-tested routing rules (which requests are cached, which never), plus a Playwright test
  with the network switched off: event page, a person page and search work; chat shows "nicht verfügbar".
- Manual on a real iPhone and Android: install, save WM 2026, airplane mode, browse.

## Not in v10
- Offline chat (would need a model on the phone).
- Full database on the phone / a native app (not needed for this use).
- Background sync of new data (only manual "Aktualisieren" when online).
