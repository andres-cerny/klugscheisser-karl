"""NCAA season stats from the college athletics sites Karl already uses for bios (Sidearm, seeds/college_sites.yaml).

The team's own stats page (roster URL with /roster -> /stats, e.g. https://nuhuskies.com/sports/womens-ice-hockey/stats)
is server-rendered: tables "Individual Overall Skaters Statistics" (GP, G, A, PTS, +/-, PN-PIM …), "Individual
Overall Goaltenders Statistics" (GP-GS, MIN, GA, SV, SV%, W, L, T, SHO) and "Game-By-Game Team Statistics" (dates
of the games played: the Stand). robots.txt allows /sports/ with Crawl-delay 30 (checked 2026-10-25); every request
goes through PoliteClient (robots.txt + crawl delay). One page per college and night.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import first_last

log = logging.getLogger(__name__)

SKATERS = "Individual Overall Skaters Statistics"
GOALIES = "Individual Overall Goaltenders Statistics"
GAMES = "Game-By-Game Team Statistics"
NOT_PLAYERS = {"total", "opponents", "team", "totals"}
US_TZ = ZoneInfo("America/New_York")


def stats_url(roster_url: str) -> str:
    return re.sub(r"/roster/?$", "/stats", roster_url.rstrip("/"))


def _cell(c: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", c)).split())


def _tables(page: str) -> dict[str, list[list[str]]]:
    """Caption -> rows of cell texts (first table per caption). Tables without a caption (newer Nuxt layout) are
    keyed "#<n>"."""
    out: dict[str, list[list[str]]] = {}
    for n, m in enumerate(re.finditer(r"<table\b[^>]*>(.*?)</table>", page, re.S | re.I)):
        body = m.group(1)
        cap = re.search(r"<caption[^>]*>(.*?)</caption>", body, re.S | re.I)
        name = (_cell(cap.group(1)) if cap else "") or f"#{n}"
        if name in out:
            continue
        out[name] = [
            [_cell(c) for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
            for r in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body, re.S | re.I)
        ]
    return out


def _flat(tables: dict[str, list[list[str]]], need: set[str]) -> list[list[str]] | None:
    """Newer layout: one header row with every label ('#', 'Player', 'GP', 'G', 'A', 'PTS' …)."""
    for rows in tables.values():
        if rows and "Player" in rows[0] and need <= set(rows[0]):
            return rows
    return None


def _name(cell: str) -> str:
    """'Wey, Ivana Wey, Ivana' (the cell repeats the name for small screens) -> 'Ivana Wey'."""
    words = cell.split()
    half = len(words) // 2
    if len(words) % 2 == 0 and words[:half] == words[half:]:
        cell = " ".join(words[:half])
    return first_last(cell)


def _int(v: str | None) -> int | None:
    try:
        return int(str(v).replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def _float(v: str | None) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _player_rows(rows: list[list[str]], lead: int) -> tuple[dict[str, int], list[list[str]]]:
    """Column index per sub-header label (data rows: `lead` leading cells, then the sub-header columns)."""
    sub = next((r for r in rows[:3] if r and r[0] in ("G", "GA")), None)
    if sub is None:
        return {}, []
    cols = {label: lead + i for i, label in enumerate(sub)}
    data = [r for r in rows if len(r) > lead and r is not sub and r[1] and r[1].lower() not in NOT_PLAYERS]
    data = [r for r in data if _int(r[2].split("-")[0]) is not None]  # header rows have no number there
    return cols, data


def parse_stats_page(page: str) -> tuple[list[dict], datetime | None]:
    """Skater and goalie rows ({name, games, goals …}) and the date of the last game played."""
    tables = _tables(page)
    out: list[dict] = []
    if SKATERS not in tables and (flat := _flat(tables, {"GP", "G", "A", "PTS"})):
        tables[SKATERS] = [
            flat[0][:3],
            flat[0][3:],
            *flat[1:],
        ]  # as the classic layout: lead, sub-header, rows
    if GOALIES not in tables and (flat := _flat(tables, {"GA", "SV%"})):
        tables[GOALIES] = [flat[0][:4], flat[0][4:], *flat[1:]]
    if GAMES not in tables:
        tables[GAMES] = [
            r for rows in tables.values() for r in rows if r and re.fullmatch(r"\d\d/\d\d/\d{4}", r[0])
        ]
    cols, data = _player_rows(tables.get(SKATERS) or [], lead=3)  # #, Player, GP
    for r in data:
        pim = r[cols["PN-PIM"]].split("-")[-1] if "PN-PIM" in cols and cols["PN-PIM"] < len(r) else None

        def get(label: str, row: list[str] = r) -> str | None:
            i = cols.get(label)
            return row[i] if i is not None and i < len(row) else None

        out.append(
            {
                "name": _name(r[1]),
                "games": _int(r[2]),
                "goals": _int(get("G")),
                "assists": _int(get("A")),
                "points": _int(get("PTS")),
                "plus_minus": _int(get("+/-")),
                "penalty_minutes": _int(pim),
            }
        )
    cols, data = _player_rows(tables.get(GOALIES) or [], lead=4)  # #, Player, GP-GS, MIN
    for r in data:

        def gget(label: str, row: list[str] = r) -> str | None:
            i = cols.get(label)
            return row[i] if i is not None and i < len(row) else None

        games = _int(r[2].split("-")[0])
        if not games:
            continue
        sv_pct = _float(gget("SV%"))
        out.append(
            {
                "name": _name(r[1]),
                "goalie": True,
                "games": games,
                "toi": r[3] or None,
                "goals_against": _int(gget("GA")),
                "gaa": _float(gget("AVG")),
                "saves": _int(gget("SV")),
                "save_pct": sv_pct if sv_pct is not None and sv_pct <= 1 else None,
                "wins": _int(gget("W")),
                "losses": _int(gget("L")),
                "ties": _int(gget("T")),
                "shutouts": _int(gget("SHO") or gget("SO")),
            }
        )
    goalies = {r["name"] for r in out if r.get("goalie")}
    out = [r for r in out if r.get("goalie") or r["name"] not in goalies]  # goalies also in the skater table
    last = None
    for r in tables.get(GAMES) or []:
        if len(r) >= 3 and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}(\d\d)?", r[0]) and r[2] in ("W", "L", "T"):
            fmt = "%m/%d/%Y" if len(r[0].split("/")[-1]) == 4 else "%m/%d/%y"
            d = datetime.strptime(r[0], fmt).replace(hour=12, tzinfo=US_TZ)
            last = max(last, d) if last else d
    return out, last


def fetch_college_stats(client, sites) -> list[tuple[object, str, list[dict], datetime | None]]:
    """(site, stats URL, rows, last game) per college; a failed or empty page is logged and left out."""
    out = []
    for site in sites:
        url = stats_url(site.roster_url)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            # some sites answer with a meta refresh to the season page (/stats -> /stats/2026-27): follow once
            if hop := re.search(r'http-equiv="refresh"[^>]*url=([^"\s>]+)', resp.text[:2000], re.I):
                url = urljoin(url, hop.group(1))
                resp = client.get(url)
                resp.raise_for_status()
            rows, last = parse_stats_page(resp.text)
        except Exception as exc:
            log.warning("college stats %s failed: %s", url, exc)
            continue
        if rows:
            out.append((site, url, rows, last))
        else:
            log.warning("college stats %s: no player table", url)
    return out
