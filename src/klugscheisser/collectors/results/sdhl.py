"""SDHL from stats.swehockey.se (HTML only; no robots.txt; © Svenska Ishockeyförbundet).

- Schedule and results: https://stats.swehockey.se/ScheduleAndResults/Schedule/{scheduleId} (2026/27: 20960)
- Game events (goals, assists, goalies): https://stats.swehockey.se/Game/Events/{gameId}  (also the public link)

Low frequency: one schedule request plus one events page per new final game.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import (
    KnownFinal,
    PoliteHttp,
    first_last,
    in_window,
    ot_suffix,
    wants,
)
from klugscheisser.interfaces import GameResult, Participant

log = logging.getLogger(__name__)

BASE = "https://stats.swehockey.se"
COMPETITION = "SDHL 2026/27"
TZ = ZoneInfo("Europe/Stockholm")

_TAG = re.compile(r"<[^>]+>")
_GAME_ID = re.compile(r"/Game/Events/(\d+)")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
_NUMBER = re.compile(r'class="lnkTooltip" title="(\d+)"')
_GOAL = re.compile(r"^(\d+)-(\d+) \((\w+)\)\s*(.*)$")
_PLAYER = re.compile(r"^(\d+)\.\s*(.+)$")
_GK = re.compile(r"(\d+,\d+)% \((\d+)/(\d+)\)")
_PERIOD = re.compile(r"<h3>([^<]*(?:period|Overtime|Game Winning Shots|Shootout)[^<]*)</h3>", re.IGNORECASE)


def clean(fragment: str) -> str:
    return " ".join(htmllib.unescape(_TAG.sub(" ", fragment)).split())


def _cells(row: str) -> list[str]:
    return row.split("<td")[1:]


def _player(text: str) -> tuple[str | None, str | None]:
    m = _PLAYER.match(clean(text))
    if not m:
        return None, None
    return m.group(1), first_last(m.group(2))


def parse_schedule(page: str, schedule_id: int, competition: str = COMPETITION) -> list[GameResult]:
    games = []
    for row in re.split(r"(?=<tr>)", page):
        num = _NUMBER.search(row)
        if not num:
            continue
        cells = _cells(row)
        if len(cells) < 7:
            continue
        texts = [clean("<td" + c) for c in cells]
        date_m = _DATE.search(texts[1])
        teams = texts[2]
        if " - " not in teams or not date_m:
            continue
        home, away = (t.strip() for t in teams.split(" - ", 1))
        gid = _GAME_ID.search(cells[3])
        result = texts[3]
        periods = re.findall(r"(\d+)-(\d+)", texts[4])
        status = "final" if gid and re.match(r"^\d+ - \d+$", result) else "scheduled"
        score = None
        if status == "final":
            h, a = (x.strip() for x in result.split("-"))
            score = f"{h}:{a}{ot_suffix(len(periods))}"
        venue = texts[-1]
        starts = datetime.strptime(date_m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        games.append(
            GameResult(
                source="SDHL",
                sport="icehockey_women",
                competition=competition,
                external_id=num.group(1),
                url=f"{BASE}/Game/Events/{gid.group(1)}"
                if gid
                else f"{BASE}/ScheduleAndResults/Schedule/{schedule_id}",
                status=status,
                starts_at=starts,
                home=home,
                away=away,
                score=score,
                period_scores=[f"{h}:{a}" for h, a in periods],
                venue=venue or None,
                raw={"game_id": gid.group(1) if gid else None},
            )
        )
    return games


def apply_events(game: GameResult, page: str) -> GameResult:
    title = re.search(r"<title>(.*?)\(", page, re.S)
    short = clean(title.group(1)) if title else ""
    abbr = [t.strip() for t in short.split(" - ", 1)] if " - " in short else []
    teams = dict(zip(abbr, [game.home, game.away], strict=False))
    parts: list[Participant] = []
    goals = []
    actions = page.split("<h2>Actions</h2>", 1)[-1]
    # the page lists newest first; walk it per period block
    for row in re.split(r"(?=<tr>)", actions):
        cells = _cells(row)
        texts = [clean("<td" + c) for c in cells]
        if len(cells) >= 5 and (gk := _GK.search(texts[4])) and not texts[0]:
            _no, name = _player(texts[3])
            if name:
                saves, shots = gk.group(2), gk.group(3)
                parts.append(
                    Participant(
                        name, "goalie", teams.get(texts[2], texts[2]), f"{saves} Paraden bei {shots} Schüssen"
                    )
                )
            continue
        if len(cells) < 4 or not (m := _GOAL.match(texts[1])):
            continue
        team = teams.get(texts[2], texts[2])
        sit = m.group(3) if m.group(3) != "EQ" else ""
        extra = m.group(4).strip()
        tags = ", ".join(filter(None, [sit, extra]))
        detail = f"{m.group(1)}:{m.group(2)}, {texts[0]}" + (f" ({tags})" if tags else "")
        cell = cells[3].split(">", 1)[1]
        _no, scorer = _player(re.split(r"<span", cell, maxsplit=1)[0])
        assists = [n for _, n in (_player(d) for d in re.findall(r"<div[^>]*>(.*?)</div>", cell, re.S)) if n]
        if scorer:
            parts.append(Participant(scorer, "scorer", team, detail))
        parts += [Participant(n, "assist", team, detail) for n in assists]
        goals.append(
            {
                "score": f"{m.group(1)}:{m.group(2)}",
                "time": texts[0],
                "team": team,
                "scorer": scorer,
                "assists": assists,
            }
        )
    goals.sort(key=lambda g: g["time"] or "")
    game.participants = parts
    if "Final Score" in page:
        game.status = "final"
    spectators = re.search(r"Spectators:\s*(\d+)", page)
    game.raw = {
        **game.raw,
        "goals": goals,
        "spectators": spectators.group(1) if spectators else None,
        "detail": True,
    }
    return game


class SdhlCollector(KnownFinal):
    name = "sdhl"
    source_label = "SDHL"

    def __init__(
        self,
        schedule_id: int = 20960,
        competition: str = COMPETITION,
        delay_s: float = 1.0,
        max_details: int = 20,
        horizon_days: int = 60,
        http: PoliteHttp | None = None,
    ) -> None:
        self.schedule_id, self.competition = schedule_id, competition
        self.max_details = max_details
        self.horizon_days = horizon_days
        self.http = http or PoliteHttp(delay_s)
        self.known_final = set()

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        now = datetime.now(UTC)
        since = since or now - timedelta(days=14)
        until = now + timedelta(days=self.horizon_days)
        try:
            page = self.http.text(f"{BASE}/ScheduleAndResults/Schedule/{self.schedule_id}")
            games = parse_schedule(page, self.schedule_id, self.competition)
        except Exception as exc:
            log.warning("sdhl: schedule failed: %s", exc)
            return []
        out, details = [], 0
        for game in games:
            if not wants(teams, game.home, game.away) or not in_window(game, since):
                continue
            if game.status == "scheduled" and game.starts_at and game.starts_at > until:
                continue
            gid = game.raw.get("game_id")
            new_final = game.status == "final" and gid and not self.is_known_final(game.external_id)
            if new_final and details < self.max_details:
                details += 1
                try:
                    apply_events(game, self.http.text(f"{BASE}/Game/Events/{gid}"))
                except Exception as exc:
                    log.warning("sdhl: events %s failed: %s", gid, exc)
            out.append(game)
        return out
