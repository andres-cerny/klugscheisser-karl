"""Season totals of the Swedish women's leagues from stats.swehockey.se (Svenska Ishockeyförbundet, HTML).

One page per league holds every player of every team: "Players By Team"
(https://stats.swehockey.se/Teams/Info/PlayersByTeam/{scheduleId}) with a "Playing Statistics" table (GP, G, A,
TP, PIM, +/-) and a "Goalkeeping Statistics" table (GPI = games played in, MIP, GA, SVS, SOG, SVS%, GAA, SO, W, L)
per team. The "Scoring Leaders" page of the same league gives the "Last update" time (the Stand). Two requests per
league and night. No robots.txt (404) and no terms found (checked 2026-10-25 and 2026-10-28).

2026/27 schedule ids: SDHL 20960, NDHL (Norra) 20958, NDHL Södra 20959 (both NDHL groups are EP's "NDHL (W)").
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import PoliteHttp, first_last

log = logging.getLogger(__name__)

BASE = "https://stats.swehockey.se"
TZ = ZoneInfo("Europe/Stockholm")
_TAG = re.compile(r"<[^>]+>")
_TEAM = re.compile(r'<th class="tdTitle d-none d-xl-table-cell" colspan="\d+">([^<]+)</th>')
_UPDATE = re.compile(r"Last update:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")


@dataclass(frozen=True)
class SweLeague:
    schedule_id: int
    league: str  # as Elite Prospects writes it
    name: str  # for logs and the report

    @property
    def table_url(self) -> str:
        return f"{BASE}/Teams/Info/PlayersByTeam/{self.schedule_id}"

    @property
    def leaders_url(self) -> str:
        return f"{BASE}/Players/Statistics/ScoringLeaders/{self.schedule_id}"


def leagues_from_config(items: list[dict]) -> list[SweLeague]:
    return [SweLeague(int(x["schedule_id"]), x["league"], x.get("name") or x["league"]) for x in items]


def _clean(fragment: str) -> str:
    return " ".join(htmllib.unescape(_TAG.sub(" ", fragment)).split())


def _int(v: str) -> int | None:
    v = (v or "").strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else None


def _float(v: str) -> float | None:
    v = (v or "").strip().replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def _rows(section: str) -> list[list[str]]:
    return [
        [_clean(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", r, re.S)]
        for r in re.findall(r"<tr>(.*?)</tr>", section, re.S)
        if "<td" in r
    ]


def _header(section: str) -> list[str]:
    """Column labels of the first header row (the visible text of each <th class="tdHeader">)."""
    return [_clean(h) for h in re.findall(r'<th class="tdHeader[^"]*">(.*?)</th>', section, re.S)]


def parse_players_by_team(page: str) -> list[dict]:
    """Every player of the page: {name, team, goalie, games, goals, assists, points, penalty_minutes, plus_minus}
    for skaters, {name, team, goalie: True, games, goals_against, saves, save_pct, gaa, shutouts, wins, losses}
    for goalies who played (GPI > 0). Goalies' rows in the skater table are left out (dressed games, no saves)."""
    out: list[dict] = []
    starts = [(mt.start(), htmllib.unescape(mt.group(1)).strip()) for mt in _TEAM.finditer(page)]
    for i, (pos, team) in enumerate(starts):
        block = page[pos : starts[i + 1][0] if i + 1 < len(starts) else len(page)]
        parts = re.split(r'<th colspan="\d+" class="tdSubTitle">', block)
        for part in parts[1:]:
            title = _clean(part.split("</th>", 1)[0])
            head = _header(part)
            if not head or "Name" not in head:
                continue
            col = {h: n for n, h in enumerate(head)}
            for cells in _rows(part):
                if len(cells) < len(head):
                    continue
                name = first_last(cells[col["Name"]]) if cells[col["Name"]] else ""
                if not name:
                    continue
                if title.startswith("Playing"):
                    if cells[col["Pos"]] == "GK":
                        continue
                    out.append(
                        {
                            "name": name,
                            "team": team,
                            "goalie": False,
                            "games": _int(cells[col["GP"]]),
                            "goals": _int(cells[col["G"]]),
                            "assists": _int(cells[col["A"]]),
                            "points": _int(cells[col["TP"]]),
                            "penalty_minutes": _int(cells[col["PIM"]]),
                            "plus_minus": _int(cells[col["+/-"]]) if "+/-" in col else None,
                        }
                    )
                elif title.startswith("Goalkeeping"):
                    gpi = _int(cells[col["GPI"]])
                    if not gpi:
                        continue  # dressed only
                    pct = _float(cells[col["SVS%"]])
                    out.append(
                        {
                            "name": name,
                            "team": team,
                            "goalie": True,
                            "games": gpi,
                            "goals_against": _int(cells[col["GA"]]),
                            "saves": _int(cells[col["SVS"]]),
                            "save_pct": round(pct / 100, 3) if pct is not None else None,
                            "gaa": _float(cells[col["GAA"]]),
                            "shutouts": _int(cells[col["SO"]]),
                            "wins": _int(cells[col["W"]]),
                            "losses": _int(cells[col["L"]]),
                            "toi": cells[col["MIP"]] or None,
                        }
                    )
    return out


def last_update(page: str) -> datetime | None:
    hit = _UPDATE.search(page or "")
    return datetime.strptime(hit.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=TZ) if hit else None


def fetch_league(http: PoliteHttp, league: SweLeague) -> tuple[list[dict], datetime | None]:
    """(players, stand) of one league; two requests. Raises on HTTP errors (the caller keeps the old rows)."""
    table = parse_players_by_team(http.text(league.table_url))
    stand = None
    try:
        stand = last_update(http.text(league.leaders_url))
    except Exception as exc:  # the table is enough; the Stand falls back to now
        log.warning("swehockey %s: leaders page failed: %s", league.name, exc)
    return table, stand
