"""WFC 2026 schedule and results from the official results service (Torneopal widgets, checked 2026-11-24).

`https://salibandy.api.torneopal.com/taso/widget.php?competition=sb2026int&class=2702&group=<1-5>&widget=schedule`
returns public server-rendered HTML inside `document.write("…")` (no key; the REST API needs one, not used). Groups
1–4 = groups A–D, group 5 = the knock-out games. Every row carries `matchid_<id>` (stable, = the match page
`tulospalvelu.salibandy.fi/match/<id>`), date, time (Finnish local time), home, away, venue and score ("–" before
the game). Game events use the key `torneopal-<id>`, the same as the seed (seed/floorball_info.py), so a result
updates the seeded row; knock-out games get their teams as soon as the service names them.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.floorball.common import CappedHttp
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.sports import national_team_key

WIDGET = "https://salibandy.api.torneopal.com/taso/widget.php"
MATCH_URL = "https://tulospalvelu.salibandy.fi/match/{id}"
HELSINKI = ZoneInfo("Europe/Helsinki")
SOURCE = "Torneopal"  # events.source (String(30))
GAME_HOURS = 3  # a game with a score is final this long after its start (the widget has no live flag)

_ROW = re.compile(r"<tr class='([^']*matchid_(\d+)[^']*)'>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td class='([a-z_ ]+)'>(.*?)</td>", re.S)


@dataclass
class Game:
    match_id: int
    day: str  # "5.12."
    time: str  # "19:00" (Finnish local time)
    home: str
    away: str
    venue: str
    score: str | None  # "5–4" / "5–4 ja" ; None before the game
    canceled: bool = False

    def starts(self, year: int) -> datetime | None:
        mt = re.match(r"(\d{1,2})\.(\d{1,2})\.", self.day)
        tm = re.match(r"(\d{1,2}):(\d{2})", self.time or "")
        if not mt:
            return None
        h, mi = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
        return datetime(year, int(mt.group(2)), int(mt.group(1)), h, mi, tzinfo=HELSINKI).astimezone(UTC)


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def parse(widget: str) -> list[Game]:
    body = widget.encode().decode("unicode_escape", errors="ignore") if "\\'" in widget else widget
    out = []
    for cls, mid, row in _ROW.findall(body):
        cells = {c.split()[0]: _text(v) for c, v in _CELL.findall(row)}
        score = cells.get("score") or ""
        has_score = bool(re.search(r"\d+\s*[–:-]\s*\d+", score))
        out.append(
            Game(
                match_id=int(mid),
                day=cells.get("date", ""),
                time=cells.get("time", ""),
                home=cells.get("home", ""),
                away=cells.get("away", ""),
                venue=cells.get("pitch", ""),
                score=score if has_score else None,
                canceled="canceled" in cls,
            )
        )
    return out


def fetch(
    http: CappedHttp, competition: str = "sb2026int", klass: int = 2702, groups=(1, 2, 3, 4, 5)
) -> list[Game]:
    games: list[Game] = []
    for g in groups:
        r = http.get(
            WIDGET,
            params={
                "competition": competition,
                "class": klass,
                "group": g,
                "widget": "schedule",
                "columns": "date,time,home,away,pitch,score",
            },
        )
        games += parse(r.text)
    return games


def score_text(raw: str) -> str:
    """'5–4' -> '5:4'; '5–4 ja' (jatkoaika, overtime) -> '5:4 n.V.'; 'vl' (penalty shots) -> 'n.P.'."""
    mt = re.search(r"(\d+)\s*[–:-]\s*(\d+)", raw)
    if not mt:
        return raw
    low = raw.lower()
    sfx = (
        " n.P."
        if re.search(r"\bvl\b|rl|pso|shoot", low)
        else (" n.V." if re.search(r"\bja\b|ot\b", low) else "")
    )
    return f"{mt.group(1)}:{mt.group(2)}{sfx}"


def store(
    session: Session, games: list[Game], event_key: str, year: int = 2026, now: datetime | None = None
) -> dict[str, int]:
    """Update / add the game events (key torneopal-<id>): teams once known, score and status. Returns counts."""
    from klugscheisser.seed.floorball_info import code

    now = now or datetime.now(UTC)
    parent = session.scalar(select(m.Event).where(m.Event.key == event_key))
    counts = {"spiele": len(games), "neu": 0, "resultate": 0, "teams_gesetzt": 0}
    for g in games:
        key = f"torneopal-{g.match_id}"
        ev = session.scalar(select(m.Event).where(m.Event.key == key))
        h, a = code(g.home), code(g.away)
        if ev is None:
            if not (h and a):
                continue  # knock-out game without teams yet: the tournament file lists it
            ev = m.Event(key=key, kind="game", name="", status="scheduled")
            session.add(ev)
            counts["neu"] += 1
        ev.competition_id = ev.competition_id or (parent.competition_id if parent else None)
        ev.starts_at = ev.starts_at or g.starts(year)
        ev.venue = ev.venue or g.venue or None
        ev.source, ev.source_url = SOURCE, MATCH_URL.format(id=g.match_id)
        if h and a:
            home, away = (
                repo.get_team(session, national_team_key(h)),
                repo.get_team(session, national_team_key(a)),
            )
            if home and away and (ev.home_team_id, ev.away_team_id) != (home.id, away.id):
                ev.home_team_id, ev.away_team_id = home.id, away.id
                counts["teams_gesetzt"] += 1
            if not ev.name:
                from klugscheisser.tournament import NATION_DE

                ev.name = f"{NATION_DE.get(h, h)} – {NATION_DE.get(a, a)}"
        if g.canceled:
            ev.status = "canceled"
        elif g.score:
            new = score_text(g.score)
            started = ev.starts_at or now
            status = "final" if now >= started + timedelta(hours=GAME_HOURS) else "live"
            if (ev.score, ev.status) != (new, status):
                ev.score, ev.status = new, status
                counts["resultate"] += 1
    session.flush()
    return counts


def update(session: Session, event_key: str, http: CappedHttp | None = None) -> dict[str, int]:
    http = http or CappedHttp(delay_s=1.0, max_requests=10)
    return store(session, fetch(http), event_key)
