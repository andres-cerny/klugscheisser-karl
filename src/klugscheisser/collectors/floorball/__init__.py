"""Free data sources for the floorball (Unihockey) project, checked 2026-11-24.

- `swissunihockey`: Swiss Unihockey player profiles, per-season league stats (NLA/L-UPL, NLB, U21 …) and national
  team totals. Read from the public website's render engine (`www.swissunihockey.ch/renderengine/load_view.php`,
  the HTML blocks the site itself loads; robots.txt allows it). The documented REST API v2
  (api-v2.swissunihockey.ch) answers "Authorization required" on every endpoint, and its robots.txt disallows
  /api/ — not used.
- `iff`: World Floorball Championship games (date, teams, score, periods, venue, IFF game link) from the Wikipedia
  tournament pages, which link every game to its IFF game sheet. IFF's own results/stats (app.floorball.sport,
  backed by iff-api.azurewebsites.net) need a bearer token from the IFF app (anonymous calls: 401), and IFF says
  "All player statistics are now on the IFF App" — so no player tournament stats from IFF.
- `run.enrich`: one entry point for `klugscheisser -p unihockey seed floorball`.

- `ssl_se`: Swedish SSL season stats (public ssl.se JSON API; regular seasons 2016/17+); `fliiga`: Finnish
  F-Liiga season stats (fliiga.com REST player list + player pages, 2020/21+); shared matching and writing in
  `leagues` (also ends outdated open club stints).

Not built (checked 2026-11-24):
- stats.innebandy.se (Swedish federation stats): robots.txt disallows AI agents by name (ClaudeBot, Claude-User,
  GPTBot, PerplexityBot …: `Disallow: /`) — not used; the league's own site ssl.se has the same SSL tables.
  statistik.innebandy.se (old site) times out from this server (90 s, 2026-11-24).
- tulospalvelu.salibandy.fi: robots.txt has `Disallow: /person/` (the player pages); the Torneopal REST API needs
  a key. salibandy.api.torneopal.com robots.txt ends with `User-agent: * … disallow: /` (and names ClaudeBot,
  anthropic-ai …) — note: the WFC results step `torneopal` reads its widgets.
- SSL playoff stats: the ssl.se API answers empty / HTTP 500 for playoff ssgts.
"""
