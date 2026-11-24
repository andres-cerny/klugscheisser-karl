"""Profile coverage of the focus groups (free profile sources, 2026-10-25): `klugscheisser free-profiles coverage`.

Focus (Ondrej): (a) the Swiss WCh roster, (b) players of the other WCh nations who play in the Swiss Women's League
(2025/26 or 2026/27: Elite Prospects season/current team "SWHL A", a PFWL club affiliation of those seasons, or a
SIHF game since August linked to them). Secondary: the whole WCh field.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

FIELDS = ("height", "weight", "shoots", "birth", "photo", "past_stats")

_ROSTER = """
select ee.athlete_id, t.key as team from event_entries ee
join events e on e.id = ee.event_id join teams t on t.id = ee.team_id join athletes a on a.id = ee.athlete_id
where e.key = :event and a.role = 'athlete'
"""
_SWISS_LEAGUE = """
select athlete_id from season_stats where season >= :season - 1 and league like 'SWHL A%'
union select athlete_id from facts where kind = 'current_team' and value_text ilike '%SWHL A%'
  and (value_text like :s1 or value_text like :s0)
union select aa.athlete_id from athlete_affiliations aa join teams t on t.id = aa.team_id
  where t.league = 'PFWL' and aa.season in (:l0, :l1)
union select ia.athlete_id from item_athletes ia join items i on i.id = ia.item_id
  where i.platform = 'sihf' and i.published_at >= :since
"""


def current_season(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 8 else today.year - 1


def focus_groups(
    s: Session, event: str | None = None, home: str | None = None, today: date | None = None
) -> dict:
    """{"a": [ids], "b": [ids], "all": [ids]} (roster players only, no staff). Defaults: the project's main
    tournament ("wch-2026") and home team ("sui-women")."""
    from klugscheisser import projects
    from klugscheisser.sports import home_team_key

    event = event or projects.current().main_event
    home = home or home_team_key()
    season = current_season(today)
    roster = s.execute(text(_ROSTER), {"event": event}).all()
    lab = lambda y: f"{y}-{(y + 1) % 100:02d}"  # noqa: E731
    swiss = {
        r[0]
        for r in s.execute(
            text(_SWISS_LEAGUE),
            {
                "season": season,
                "s1": f"%{season}/{(season + 1) % 100:02d}%",
                "s0": f"%{season - 1}/{season % 100:02d}%",
                "l0": lab(season - 1),
                "l1": lab(season),
                "since": date(season, 8, 1),
            },
        )
    }
    a = sorted({aid for aid, team in roster if team == home})
    b = sorted({aid for aid, team in roster if team != home and aid in swiss})
    return {"a": a, "b": b, "all": sorted({aid for aid, _ in roster})}


_COVER = """
select a.id,
  exists(select 1 from facts f where f.athlete_id = a.id and f.kind = 'height_cm' and f.status <> 'unconfirmed') as height,
  exists(select 1 from facts f where f.athlete_id = a.id and f.kind = 'weight_kg' and f.status <> 'unconfirmed') as weight,
  exists(select 1 from facts f where f.athlete_id = a.id and f.kind = 'shoots' and f.status <> 'unconfirmed')
    or a.shoots is not null as shoots,
  exists(select 1 from facts f where f.athlete_id = a.id and f.kind = 'birth_date' and f.status <> 'unconfirmed')
    or a.birth_date is not null as birth,
  exists(select 1 from facts f where f.athlete_id = a.id and f.kind = 'photo' and f.status <> 'unconfirmed') as photo,
  exists(select 1 from season_stats ss where ss.athlete_id = a.id and ss.season < :season) as past_stats
from athletes a where a.id = any(:ids)
"""


def coverage(s: Session, ids: list[int], today: date | None = None) -> dict[str, int]:
    out = {"n": len(ids), **{f: 0 for f in FIELDS}}
    if not ids:
        return out
    for row in s.execute(text(_COVER), {"ids": list(ids), "season": current_season(today)}).mappings():
        for f in FIELDS:
            out[f] += bool(row[f])
    return out


def missing(s: Session, ids: list[int], today: date | None = None) -> dict[int, list[str]]:
    """Per person the fields still missing."""
    out: dict[int, list[str]] = {}
    if not ids:
        return out
    for row in s.execute(text(_COVER), {"ids": list(ids), "season": current_season(today)}).mappings():
        gaps = [f for f in FIELDS if not row[f]]
        if gaps:
            out[row["id"]] = gaps
    return out


def table(s: Session, event: str | None = None, today: date | None = None) -> list[tuple[str, dict]]:
    g = focus_groups(s, event, today=today)
    return [
        ("(a) Schweiz WM-Kader", coverage(s, g["a"], today)),
        ("(b) andere WM-Nationen in der Women's League", coverage(s, g["b"], today)),
        ("ganzes WM-Feld", coverage(s, g["all"], today)),
    ]
