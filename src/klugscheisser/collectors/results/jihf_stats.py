"""Japan Women's Ice Hockey League (女子日本アイスホッケーリーグ) totals counted from the JIHF game sheets.

JIHF (jihf.or.jp) publishes no current-season player table (only PDFs after the season), but every game has a
server-rendered game sheet:

- Games of one round ("meet"): https://www.jihf.or.jp/watching_games/season/detail.php?meet={meet}
  (2026/27: 903 = 1st round 21.–23.09., 904 = 2nd round from 27.11., 905 = final round)
- Game sheet: https://www.jihf.or.jp/watching_games/season/game/gamesheet.php?meet_id={meet}&game_no={n}
  Line-up per team (number, name in Japanese script, position), goals (scorer and assists by jersey number),
  penalties (number, minutes), saves per period per goalie, goalies' minutes played (MIP) and goals against.

Karl counts per player and team: GP (dressed skaters; goalies only with minutes played), G, A, PTS, PIM; goalies
GP, GA, saves, SV%, GAA, shutouts. A finished game sheet never changes, so each is fetched once and kept in
`source_state` ("jihf"); a night costs one page per round plus the sheets of new games. No robots.txt (404), no
terms found (checked 2026-10-28). Names are matched only through the Japanese spelling in Karl's roster seeds
(`name_orig`, seeds/rosters/*.yaml), never by guessing a romanisation.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

BASE = "https://www.jihf.or.jp/watching_games/season"
MEET_URL = BASE + "/detail.php?meet={meet}"
SHEET_URL = BASE + "/game/gamesheet.php?meet_id={meet}&game_no={game}"
TZ = ZoneInfo("Asia/Tokyo")
LEAGUE = "Japan (W)"  # as Elite Prospects writes it
# club names (after NFKC folding, lower case, prefix match) -> the English names JIHF/IIHF rosters use
TEAMS = (
    ("daishin", "Daishin"),
    ("道路建設ペリグリン", "DK Peregrine"),
    ("toyota cygnus", "Toyota Cygnus"),
    ("seibuプリンセスラビッツ", "Seibu Princess Rabbits"),
    ("御影グレッズ", "Mikage Gretz"),
    ("北海道バーバリアンズ", "Hokkaido Barbarians H.C. Infinity"),
    ("昭和医科大学", "Showa Medical University Blue Winds"),
    ("tokachi帯広レディース", "Tokachi Obihiro Ladies"),
    ("釧路ベアーズ", "Kushiro Bears"),
    ("vortex sapporo", "Vortex Sapporo"),
    ("u18全国選抜", "Japan U18 Selection"),
    ("中学選抜", "Japan Junior High Selection"),
)


def fold(text: str) -> str:
    """NFKC (full-width -> ASCII), lower case, single spaces."""
    return " ".join(unicodedata.normalize("NFKC", htmllib.unescape(text or "")).lower().split())


def team_name(raw: str) -> str:
    f = fold(raw)
    for key, name in TEAMS:
        if f.startswith(key):
            return name
    return unicodedata.normalize("NFKC", raw).strip()


def name_key(raw: str) -> str:
    """'野呂 里桜 (BP)' -> '野呂里桜' (marks like (C), (A), (BP) and all spaces removed)."""
    f = unicodedata.normalize("NFKC", htmllib.unescape(raw or ""))
    f = re.sub(r"\([^)]*\)", "", f)
    return re.sub(r"\s+", "", f)


def _cells(row: str) -> list[str]:
    return [
        " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", c)).replace("\xa0", " ").split())
        for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S)
    ]


def _tables(page: str) -> list[list[list[str]]]:
    return [
        [_cells(r) for r in re.findall(r"<tr\b[^>]*>(.*?)</tr>", t, re.S)]
        for t in re.findall(r"<table\b[^>]*>(.*?)</table>", page, re.S)
    ]


def _mins(clock: str) -> float | None:
    hit = re.fullmatch(r"(\d+):(\d{2})", clock or "")
    return int(hit.group(1)) + int(hit.group(2)) / 60 if hit else None


def _num(v: str) -> int | None:
    return int(v) if re.fullmatch(r"\d+", (v or "").strip()) else None


def meet_games(page: str) -> list[int]:
    """Game numbers of a round that have a game sheet link."""
    return sorted({int(n) for n in re.findall(r"gamesheet\.php\?meet_id=\d+&(?:amp;)?game_no=(\d+)", page)})


def parse_sheet(page: str) -> dict | None:
    """One game: {date, event, teams: [A, B], players: [{key, name, team, no, pos, gp, g, a, pim}],
    goalies: [{key, name, team, no, minutes, ga, saves}]}; None when the game is not finished."""
    tabs = _tables(page)
    head = next((t for t in tabs if t and t[0] and t[0][0] == "Event:"), None)
    rosters = [t for t in tabs if len(t) > 2 and t[1][:3] == ["No", "Name", "Pos"]]
    if head is None or len(rosters) != 2:
        return None
    info = dict(zip(head[0][0::2], head[0][1::2], strict=False))
    end = next((r for t in tabs for r in t if r and r[0] == "Start of game"), None)
    if not end or len(end) < 4 or not _mins(end[3]):
        return None  # no end of game: not final (yet)
    try:
        day = datetime.strptime(f"{info.get('Date:')} {info.get('Start:') or '00:00'}", "%Y/%m/%d %H:%M")
    except ValueError:
        return None
    teams = [team_name(t[0][0]) for t in rosters]
    players: dict[tuple[str, int], dict] = {}
    by_no: list[dict[int, dict]] = [{}, {}]
    for side, t in enumerate(rosters):
        col = {h: n for n, h in enumerate(t[1])}
        g_col, a1, a2 = col.get("G"), col.get("A1"), col.get("A2")
        pen_no = len(t[1]) - 5 if len(t[1]) >= 6 and t[1][-5] == "No" else None
        goals, pims = [], []
        for r in t[2:]:
            if len(r) < 3:
                continue
            no, name = _num(r[0]), r[1]
            if no is not None and name:
                p = {
                    "key": name_key(name),
                    "name": re.sub(r"\s*\([^)]*\)", "", name).strip(),
                    "team": teams[side],
                    "no": no,
                    "pos": r[2],
                    "g": 0,
                    "a": 0,
                    "pim": 0,
                }
                players[(teams[side], no)] = p
                by_no[side][no] = p
            if g_col is not None and len(r) > a2:
                goals.append((_num(r[g_col]), _num(r[a1]), _num(r[a2])))
            if pen_no is not None and len(r) > pen_no + 1:
                pims.append((_num(r[pen_no]), _num(r[pen_no + 1])))
        for g, x, y in goals:
            if g in by_no[side]:
                by_no[side][g]["g"] += 1
            for n in (x, y):
                if n in by_no[side]:
                    by_no[side][n]["a"] += 1
        for n, mins in pims:
            if n in by_no[side] and mins:
                by_no[side][n]["pim"] += mins
    # goalies: minutes and goals against per goalie (table "GKA MIP GA GKB MIP GA"), saves per period
    gk_tab = next((t for t in tabs if t and t[0][:3] == ["GKA", "MIP", "GA"]), [])
    goalies = []
    for side in (0, 1):
        order = []
        for r in gk_tab[1:]:
            if len(r) < 6:
                break
            no, mip, ga = (_num(r[3 * side]), _mins(r[3 * side + 1]), _num(r[3 * side + 2]))
            if no is not None and mip:
                order.append((no, mip, ga or 0))
        for i, (no, mip, ga) in enumerate(order):
            p = by_no[side].get(no)
            if p is None:
                continue
            goalies.append(
                {
                    "key": p["key"],
                    "name": p["name"],
                    "team": teams[side],
                    "no": no,
                    "minutes": round(mip, 2),
                    "ga": ga,
                    "slot": i,
                    "side": side,
                }
            )
    summary = next((t for t in tabs if len(t) > 2 and t[1][:2] == ["Period", "G A:B"]), None)
    if summary:
        cols = summary[1]
        slots = {(0, 0): "GKA1", (0, 1): "GKA2", (1, 0): "GKB1", (1, 1): "GKB2"}
        for gk in goalies:
            c = slots.get((gk["side"], gk["slot"]))
            if c in cols:
                gk["saves"] = sum(
                    _num(r[cols.index(c)]) or 0
                    for r in summary[2:]
                    if len(r) == len(cols) and (r[0].isdigit() or r[0] == "OVT")  # periods, not the total row
                )
    for p in players.values():
        p["gp"] = 1 if p["pos"] != "GK" else 0
    for gk in goalies:
        players.pop((gk["team"], gk["no"]), None)
    return {
        "date": day.replace(tzinfo=TZ).isoformat(),
        "event": info.get("Event:"),
        "teams": teams,
        "players": [p for p in players.values() if p["pos"] != "GK"],
        "goalies": goalies,
    }


def totals(sheets: list[dict]) -> list[dict]:
    """Season rows per player and team from parsed sheets (skaters and goalies)."""
    acc: dict[tuple[str, str], dict] = {}
    for sh in sheets:
        for p in sh["players"]:
            a = acc.setdefault(
                (p["team"], p["key"]),
                {
                    "name": p["name"],
                    "team": p["team"],
                    "key": p["key"],
                    "goalie": False,
                    "games": 0,
                    "goals": 0,
                    "assists": 0,
                    "penalty_minutes": 0,
                },
            )
            a["games"] += p["gp"]
            a["goals"] += p["g"]
            a["assists"] += p["a"]
            a["penalty_minutes"] += p["pim"]
        solo = {}
        for gk in sh["goalies"]:
            solo.setdefault(gk["team"], []).append(gk)
        for gk in sh["goalies"]:
            a = acc.setdefault(
                (gk["team"], gk["key"]),
                {
                    "name": gk["name"],
                    "team": gk["team"],
                    "key": gk["key"],
                    "goalie": True,
                    "games": 0,
                    "goals_against": 0,
                    "minutes": 0.0,
                    "shutouts": 0,
                },
            )
            a["games"] += 1
            a["goals_against"] += gk["ga"]
            a["minutes"] += gk["minutes"]
            if "saves" in gk and a.get("saves", 0) is not None:
                a["saves"] = a.get("saves", 0) + gk["saves"]
            else:
                a["saves"] = None  # one sheet without saves: no SV% for the season
            if len(solo[gk["team"]]) == 1 and gk["ga"] == 0 and gk["minutes"] >= 59.9:
                a["shutouts"] += 1
    out = []
    for a in acc.values():
        if a["goalie"]:
            sv, ga, mins = a.get("saves"), a["goals_against"], a.pop("minutes")
            if sv is not None and sv + ga:
                a["save_pct"] = round(sv / (sv + ga), 3)
            if mins:
                a["gaa"] = round(ga * 60 / mins, 2)
        else:
            a["points"] = a["goals"] + a["assists"]
        out.append(a)
    return out


def fetch_sheets(http, meets: list[int], cache: dict, max_new: int = 40) -> tuple[dict, int]:
    """Parsed sheets of every finished game of these rounds: cached ones from `cache` ("meet-game" -> sheet),
    new ones fetched (at most `max_new` per run). Returns (cache with the new sheets, requests made)."""
    cache = dict(cache or {})
    before = getattr(http, "requests", 0)
    new = 0
    for meet in meets:
        try:
            games = meet_games(http.text(MEET_URL.format(meet=meet)))
        except Exception as exc:
            log.warning("jihf: round %s failed: %s", meet, exc)
            continue
        for game in games:
            key = f"{meet}-{game}"
            if key in cache or new >= max_new:
                continue
            try:
                sheet = parse_sheet(http.text(SHEET_URL.format(meet=meet, game=game)))
            except Exception as exc:
                log.warning("jihf: sheet %s failed: %s", key, exc)
                continue
            new += 1
            if sheet:
                sheet["url"] = SHEET_URL.format(meet=meet, game=game)
                cache[key] = sheet
    return cache, getattr(http, "requests", 0) - before
