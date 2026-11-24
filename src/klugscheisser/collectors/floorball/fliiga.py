"""F-Liiga (Finland's top men's league): per-season stats and club history of players, checked 2026-11-24.

Source: fliiga.com (WordPress). robots.txt only disallows /wp-admin/.
- Player list: the site's public WordPress REST collection `/wp-json/wp/v2/pelaajat?per_page=100&page=N`
  (title, slug, link, meta; ~1,800 players, men and women, current and past; ~18 requests). Men's pages are
  `/pelaajat/miehet/<slug>/`.
- Player page `/pelaajat/miehet/<slug>/` (server-rendered HTML): number, team of the running season, birth
  year ("s. 1995"), a stats table per season from 2020/21 ("Runkosarja" = regular season, "Pudotuspelit"/"Play
  offs" = playoffs; field players PO M S P R L L% + – +/-, goalkeepers PO T T/O P P/O S R T%) and the club
  history ("Seurahistoria"). One request per matched player.

Not used: tulospalvelu.salibandy.fi (robots.txt disallows /person/, the player pages) and the Torneopal REST API
behind it (API key required). The F-Liiga player list is AJAX on the page, but the REST collection above is the
same data without JavaScript.
"""

from __future__ import annotations

import html
import logging
import re

from klugscheisser.collectors.floorball.common import CappedHttp, RequestCapReached, text_of, to_int
from klugscheisser.collectors.floorball.leagues import LeaguePlayer, LeagueRow

log = logging.getLogger(__name__)

BASE = "https://fliiga.com"
LIST_URL = BASE + "/wp-json/wp/v2/pelaajat"
SOURCE = "fliiga"  # SeasonStat.source
LEAGUE = "F-Liiga"
COUNTRY = "FIN"
PHASES = {
    "runkosarja": "regular",
    "pudotuspelit": "playoffs",
    "play offs": "playoffs",
    "playoffs": "playoffs",
}

_SEASON_ITEM = re.compile(r'id="accordion-item-(\d{4})-(\d{4})-label"')
_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_BORN = re.compile(r"\(s\.\s*(\d{4})\)")
_TEAM = re.compile(r'<div class="label">Joukkue</div>\s*<div class="value">(.*?)</div>', re.S)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.S)


def parse_list(data: list[dict]) -> list[LeaguePlayer]:
    """Men's players of one REST page (no stats yet)."""
    out = []
    for x in data or []:
        link = x.get("link") or ""
        if "/pelaajat/miehet/" not in link:
            continue
        name = text_of(((x.get("title") or {}).get("rendered")) or "")
        if name:
            out.append(LeaguePlayer(key=x.get("slug") or link, name=name, url=link))
    return out


def fetch_list(http: CappedHttp, max_pages: int = 40) -> list[LeaguePlayer]:
    out: list[LeaguePlayer] = []
    for page in range(1, max_pages + 1):
        resp = http.get(LIST_URL, params={"per_page": 100, "page": page, "_fields": "id,slug,link,title"})
        out.extend(parse_list(resp.json()))
        total = to_int(resp.headers.get("x-wp-totalpages"))
        if total is None or page >= total:
            break
    return out


def _num(v: str) -> float | None:
    v = (v or "").strip().replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def _row(season: int, phase: str, head: list[str], cells: list[str]) -> LeagueRow | None:
    team = cells[0].strip()
    if not team:
        return None
    vals = dict(zip(head, cells[1:], strict=False))
    r = LeagueRow(season=season, team=team, phase=phase, games=to_int(vals.get("PO")))
    if "T%" in head:  # goalkeeper table
        r.saves = to_int(vals.get("T"))
        r.goals_against = to_int(vals.get("P"))
        r.gaa = _num(vals.get("P/O", ""))
        r.assists = to_int(vals.get("S"))
        r.penalty_minutes = to_int(vals.get("R"))
        pct = _num(vals.get("T%", ""))
        r.save_pct = round(pct / 100, 4) if pct is not None and pct > 1 else pct
    else:
        r.goals, r.assists, r.points = to_int(vals.get("M")), to_int(vals.get("S")), to_int(vals.get("P"))
        r.penalty_minutes = to_int(vals.get("R"))
        r.plus_minus = to_int(vals.get("+/-"))
    return r


def parse_player(page: str, url: str, key: str = "") -> LeaguePlayer:
    """One player page -> LeaguePlayer with all season rows."""
    name = text_of(_H1.search(page).group(1)) if _H1.search(page) else ""
    born = _BORN.search(page)
    team = _TEAM.search(page)
    p = LeaguePlayer(
        key=key or url,
        name=name,
        url=url,
        birth_year=int(born.group(1)) if born else None,
        team=text_of(team.group(1)) if team else None,
    )
    stats = page.split('id="player-stats"', 1)[-1].split('id="player-clubs"', 1)[0]
    marks = list(_SEASON_ITEM.finditer(stats))
    goalie = False
    for i, mark in enumerate(marks):
        season = int(mark.group(1))
        block = stats[mark.end() : marks[i + 1].start() if i + 1 < len(marks) else len(stats)]
        phase, head = None, []
        for table in _TABLE.findall(block):
            for tr in _TR.findall(table):
                cells = [html.unescape(text_of(c)) for c in _TD.findall(tr)]
                if not cells:
                    continue
                label = cells[0].strip().lower()
                if label in PHASES or not cells[1:] or not any(re.search(r"\d", c) for c in cells[1:]):
                    # header row: "Runkosarja | PO | M | S …" (another phase = another header in the same table)
                    phase = PHASES.get(label)
                    head = [h.replace("\u2013", "-").strip() for h in cells[1:]]
                    goalie = goalie or (phase is not None and "T%" in head)
                    continue
                if phase is None:
                    continue
                r = _row(season, phase, head, cells)
                if r is not None:
                    p.rows.append(r)
    if goalie:
        p.position = "G"
    return p


def fetch_players(http: CappedHttp, wanted: list[str]) -> tuple[list[LeaguePlayer], dict]:
    """F-Liiga men's players whose name matches one of `wanted` (athlete names), with stats. Cost: the list
    (~18 requests) + one per candidate page. Stops quietly at the request cap."""
    from klugscheisser.collectors.floorball.leagues import Index

    report: dict = {}
    try:
        listed = fetch_list(http)
    except RequestCapReached as exc:
        return [], {"gestoppt": str(exc)}
    report["liste"] = len(listed)
    index = Index(listed)
    cands: dict[str, LeaguePlayer] = {}
    for name in wanted:
        for p in index.candidates(name):
            cands[p.key] = p
    out = []
    for ref in cands.values():
        try:
            page = http.get(ref.url).text
        except RequestCapReached as exc:
            report["gestoppt"] = str(exc)
            break
        except Exception as exc:
            log.warning("fliiga: %s failed: %s", ref.url, exc)
            report.setdefault("fehler", []).append(ref.url)
            continue
        p = parse_player(page, ref.url, ref.key)
        p.name = p.name or ref.name
        out.append(p)
    report["seiten"] = len(out)
    return out, report
