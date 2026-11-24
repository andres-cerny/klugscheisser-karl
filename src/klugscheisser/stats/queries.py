"""Shared stats queries (v11). Web app (Statistik section) and MCP (`stats`, `get_athlete`) both use these.

Rules: empty values mean "no data" and are never counted as 0; a total only sums rows that have the value, and
says how many games it is based on. Seasons are start years (2026 = 2026/27).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

COUNT_FIELDS = (
    "games",
    "goals",
    "assists",
    "points",
    "penalty_minutes",
    "plus_minus",
    "wins",
    "losses",
    "ties",
    "shutouts",
    "goals_against",
    "saves",
)


def season_label(season: int) -> str:
    return f"{season}/{(season + 1) % 100:02d}"


def is_summary(r: m.SeasonStat) -> bool:
    """EP's "Switzerland (all)" rows sum up other national-team rows of the season: never count them twice."""
    return (r.team_name or "").endswith("(all)") or r.league.startswith("International")


EP_SOURCE = "eliteprospects"
SOURCE_LABEL = {
    "eliteprospects": "Elite Prospects",
    "sihf": "sihf.ch",
    "leijonat": "Leijonat-Spielberichte (von Karl gezählt)",
    "iihf": "IIHF-Spielberichte (von Karl gezählt)",
    "college": "College-Teamstatistik",
    "wiki": "Wikipedia-Spielboxen (von Karl gezählt)",
    "pwhl": "PWHL",
    "sdhl": "SDHL",
    "swehockey": "Svenska Ishockeyförbundet (stats.swehockey.se)",
    "leijonat_liiga": "Leijonat-Ligatabelle (tulospalvelu.leijonat.fi)",
    "jihf": "JIHF-Spielberichte (von Karl gezählt)",
    "leijonat_u18": "Leijonat-Turnierstatistik (U18-WM)",
    "swissunihockey": "swissunihockey.ch",
    "ssl": "SSL (ssl.se)",
    "fliiga": "F-Liiga (fliiga.com)",
}


def prefer_current(rows: list[m.SeasonStat]) -> list[m.SeasonStat]:
    """2026-10-25: Elite Prospects (imported once, 04.10.2026) and current rows (SIHF season table, totals counted
    from complete game sheets) can cover the same person, season, league and phase. The current row wins: it is
    the league's own table or a count over every stored game of that series (EP missed games, e.g. Stalder EHT
    30.09.). Exception: a counted row without games played (IIHF/Wikipedia sheets have no line-ups) loses to an EP
    row that has them. The loser is left out, so no game is counted twice; a winning current row carries the EP
    row it replaces in `replaces` (shown as "ersetzt …"). Leagues only EP covers (NCAA, PWHL, SDHL …) stay EP."""
    groups: dict[tuple, list[m.SeasonStat]] = defaultdict(list)
    for r in rows:
        r.replaces = None  # transient (not a column); set again below
        groups[(r.athlete_id, r.season, r.league, r.phase)].append(r)
    drop: set[int] = set()
    for group in groups.values():
        ep = [r for r in group if r.source == EP_SOURCE]
        cur = [r for r in group if r.source != EP_SOURCE]
        if not ep or not cur:
            continue
        if all(r.games is None for r in cur) and any(r.games is not None for r in ep):
            drop |= {id(r) for r in cur}
            continue
        drop |= {id(r) for r in ep}
        for r in cur:
            r.replaces = ep
    return [r for r in rows if id(r) not in drop]


def replaced_text(r: m.SeasonStat) -> str | None:
    """'Elite Prospects (Stand 04.10.2026): GP 5 · G 6 · A 7 · PTS 13 · PIM 2' for a row that replaces EP."""
    old = [x for x in getattr(r, "replaces", None) or [] if x.games is not None]
    if not old:
        return None
    parts = []
    for x in old:
        vals = [
            f"{lbl} {getattr(x, f)}"
            for f, lbl in (
                ("games", "GP"),
                ("goals", "G"),
                ("assists", "A"),
                ("points", "PTS"),
                ("penalty_minutes", "PIM"),
                ("goals_against", "GA"),
                ("saves", "SV"),
            )
            if getattr(x, f) is not None
        ]
        parts.append(" · ".join(vals))
    stand = f" (Stand {old[0].fetched_at:%d.%m.%Y})" if old[0].fetched_at else ""
    return f"statt Elite Prospects{stand}: {'; '.join(parts)}"


def season_rows(
    s: Session,
    athlete_id: int,
    phase: str | None = "regular",
    team_kind: str | None = None,
    source: str | None = None,
    include_summary: bool = False,
) -> list[m.SeasonStat]:
    """Rows of all sources with `prefer_current` (or of one source only), so nothing is counted twice."""
    q = select(m.SeasonStat).where(m.SeasonStat.athlete_id == athlete_id)
    if source:
        q = q.where(m.SeasonStat.source == source)
    if phase:
        q = q.where(m.SeasonStat.phase == phase)
    if team_kind:
        q = q.where(m.SeasonStat.team_kind == team_kind)
    rows = list(s.scalars(q.order_by(m.SeasonStat.season, m.SeasonStat.team_kind, m.SeasonStat.team_name)))
    if not source:
        rows = prefer_current(rows)
    return [r for r in rows if include_summary or not is_summary(r)]


def rates(t: dict[str, Any]) -> dict[str, Any]:
    """Per-game rates and goalie percentages for a row or a total (None when the basis is missing)."""
    gp = t.get("games") or 0
    out: dict[str, Any] = {}
    for key, field in (
        ("points_per_game", "points"),
        ("goals_per_game", "goals"),
        ("assists_per_game", "assists"),
    ):
        out[key] = round(t[field] / gp, 2) if gp and t.get(field) is not None else None
    saves, ga = t.get("saves"), t.get("goals_against")
    out["save_pct"] = (
        round(saves / (saves + ga), 3) if saves is not None and ga is not None and saves + ga else None
    )
    return out


def row_dict(r: m.SeasonStat) -> dict[str, Any]:
    d = {f: getattr(r, f) for f in COUNT_FIELDS}
    d.update(
        season=r.season,
        saison=season_label(r.season),
        league=r.league,
        team=r.team_name,
        team_kind=r.team_kind,
        phase=r.phase,
        toi=r.toi,
        gaa=r.gaa,
        source=r.source,
        quelle_link=r.source_url,
        stand=r.fetched_at.isoformat() if r.fetched_at else None,
    )
    if ersetzt := replaced_text(r):
        d["ersetzt"] = ersetzt
    d.update(rates(d))
    if r.save_pct is not None:  # the source's own value wins over our computed one
        d["save_pct"] = r.save_pct
    return d


def totals(
    s: Session,
    athlete_id: int,
    by: str = "team",
    phase: str = "regular",
    team_kind: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Career totals per team or per league (by = "team" | "league" | "team_kind"), most games first.
    Summary rows ("Switzerland (all)") are left out."""
    groups: dict[tuple, dict[str, Any]] = defaultdict(lambda: {f: None for f in COUNT_FIELDS})
    for r in season_rows(s, athlete_id, phase=phase, team_kind=team_kind, source=source):
        key = {"team": (r.team_name, r.league), "league": (r.league,), "team_kind": (r.team_kind,)}[by]
        g = groups[key]
        g.setdefault("seasons", set()).add(r.season)
        g.setdefault("sources", set()).add(r.source_url)
        g["team_kind"] = r.team_kind
        for f in COUNT_FIELDS:
            v = getattr(r, f)
            if v is not None:
                g[f] = (g[f] or 0) + v
        if r.points is not None and r.games:  # basis for per-game rates: only games with point data
            g["_rate_games"] = g.get("_rate_games", 0) + r.games
    out = []
    for key, g in groups.items():
        seasons = sorted(g.pop("seasons"))
        sources = sorted(g.pop("sources"))
        rate_games = g.pop("_rate_games", 0)
        row = {
            **g,
            "seasons": len(seasons),
            "von": season_label(seasons[0]),
            "bis": season_label(seasons[-1]),
            "quelle_links": sources[:3],
        }
        if by == "team":
            row["team"], row["league"] = key
        else:
            row[by] = key[0]
        row.update(rates({**g, "games": rate_games or g.get("games")}))
        out.append(row)
    return sorted(out, key=lambda r: -(r.get("games") or 0))
