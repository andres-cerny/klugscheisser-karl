"""What does Karl have stored? Read-only lookup in a project database (free, no API calls).

Shows, for a person and/or a team and a date window, the rows behind Karl's answers: latest and next games,
tournaments and rosters, news/posts, facts (incl. injuries), handles, season stats, plus overall freshness
(newest item per source type, hours since the newest finished Swiss game). Used to tell "Karl doesn't have
it" (ingest gap) from "Karl has it but didn't give it" (tool/answer gap).

Safety: the connection is opened with default_transaction_read_only=on and every lookup starts with
SET TRANSACTION READ ONLY; only SELECTs are run, and the transaction is rolled back at the end.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, desc, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

SWISS_TEAM_KEY = "sui-women"
READ_ONLY_OPTIONS = {"options": "-c default_transaction_read_only=on"}


def open_readonly(database_url: str) -> Engine:
    return create_engine(database_url, connect_args=READ_ONLY_OPTIONS)


def _dt(v: datetime | None) -> str | None:
    return v.astimezone(UTC).isoformat(timespec="minutes") if v else None


def _game(s: Session, e: m.Event) -> dict[str, Any]:
    names = {}
    for tid in (e.home_team_id, e.away_team_id):
        if tid:
            t = s.get(m.Team, tid)
            names[tid] = t.name if t else str(tid)
    return {
        "starts_at": _dt(e.starts_at),
        "name": e.name,
        "home": names.get(e.home_team_id),
        "away": names.get(e.away_team_id),
        "status": e.status,
        "score": e.score,
        "source": e.source,
        "url": e.source_url,
    }


def _item(i: m.Item) -> dict[str, Any]:
    return {
        "published_at": _dt(i.published_at),
        "scraped_at": _dt(i.scraped_at),
        "source_type": i.source_type,
        "source": i.source_name,
        "level": i.level,
        "title": (i.title or i.text or "")[:160].replace("\n", " "),
        "url": i.url,
    }


def _team_games(s: Session, team_id: int | None, *, final: bool, limit: int) -> list[m.Event]:
    q = select(m.Event).where(m.Event.kind == "game")
    if team_id is not None:
        q = q.where(or_(m.Event.home_team_id == team_id, m.Event.away_team_id == team_id))
    now = datetime.now(UTC)
    if final:
        q = q.where(m.Event.status == "final").order_by(desc(m.Event.starts_at))
    else:
        q = q.where(m.Event.status != "final", m.Event.starts_at >= now).order_by(m.Event.starts_at)
    return list(s.scalars(q.limit(limit)))


def overview(s: Session, since: datetime) -> dict[str, Any]:
    rows = s.execute(
        select(
            m.Item.source_type,
            func.max(m.Item.published_at),
            func.max(m.Item.scraped_at),
            func.count().filter(m.Item.published_at >= since),
        ).group_by(m.Item.source_type)
    ).all()
    from klugscheisser.sports import home_team_key

    # 2026-11-24: the project's Swiss team ("sui-men" in the floorball database); the URL may name any project's DB
    keys = dict.fromkeys([home_team_key(), SWISS_TEAM_KEY, "sui-men"])
    swiss = next((t for k in keys if (t := s.scalar(select(m.Team).where(m.Team.key == k)))), None)
    last_any = _team_games(s, None, final=True, limit=1)
    last_sui = _team_games(s, swiss.id, final=True, limit=1) if swiss else []
    now = datetime.now(UTC)
    return {
        "now": _dt(now),
        "items_by_source_type": [
            {"source_type": st, "newest_published": _dt(p), "newest_scraped": _dt(sc), "in_window": n}
            for st, p, sc, n in sorted(rows, key=lambda r: r[0] or "")
        ],
        "latest_final_game": _game(s, last_any[0]) if last_any else None,
        "latest_swiss_game": _game(s, last_sui[0]) if last_sui else None,
        "hours_since_latest_swiss_game": round((now - last_sui[0].starts_at).total_seconds() / 3600, 1)
        if last_sui and last_sui[0].starts_at
        else None,
    }


def team_lookup(s: Session, query: str, since: datetime, limit: int) -> dict[str, Any]:
    from klugscheisser.mcp_server.tools import NotFound, resolve_team

    try:
        team = resolve_team(s, query)
    except NotFound:
        team = None
    if team is None:
        return {"query": query, "found": False}
    tournaments = s.execute(
        select(m.Event, m.EventTeam.group_name)
        .join(m.EventTeam, m.EventTeam.event_id == m.Event.id)
        .where(m.EventTeam.team_id == team.id)
        .order_by(desc(m.Event.starts_at))
        .limit(limit)
    ).all()
    rosters = s.execute(
        select(
            m.Event.name,
            m.EventEntry.list_type,
            func.count(),
            func.max(m.EventEntry.fetched_at),
        )
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .where(m.EventEntry.team_id == team.id)
        .group_by(m.Event.name, m.EventEntry.list_type, m.Event.starts_at)
        .order_by(desc(m.Event.starts_at))
        .limit(limit)
    ).all()
    today = datetime.now(UTC).date()
    members = s.execute(
        select(m.Athlete.full_name, m.Affiliation.season, m.Affiliation.jersey_number)
        .join(m.Affiliation, m.Affiliation.athlete_id == m.Athlete.id)
        .where(
            m.Affiliation.team_id == team.id,
            or_(m.Affiliation.valid_to.is_(None), m.Affiliation.valid_to >= today),
        )
        .order_by(desc(m.Affiliation.season), m.Athlete.full_name)
    ).all()
    news = s.scalars(
        select(m.Item)
        .join(m.ItemTeam, m.ItemTeam.item_id == m.Item.id)
        .where(m.ItemTeam.team_id == team.id, m.Item.published_at >= since)
        .order_by(desc(m.Item.published_at))
        .limit(limit)
    ).all()
    return {
        "query": query,
        "found": True,
        "team": {"id": team.id, "key": team.key, "name": team.name, "kind": team.kind, "league": team.league},
        "latest_games": [_game(s, e) for e in _team_games(s, team.id, final=True, limit=limit)],
        "next_games": [_game(s, e) for e in _team_games(s, team.id, final=False, limit=limit)],
        "tournaments": [
            {
                "name": e.name,
                "key": e.key,
                "starts_at": _dt(e.starts_at),
                "ends_at": _dt(e.ends_at),
                "group": g,
            }
            for e, g in tournaments
        ],
        "rosters": [
            {"event": n, "list_type": lt, "players": c, "fetched_at": _dt(f)} for n, lt, c, f in rosters
        ],
        "current_members": {
            "count": len(members),
            "sample": [
                f"{n} ({season or '?'}{', #' + str(nr) if nr else ''})" for n, season, nr in members[:12]
            ],
        },
        "news_in_window": [_item(i) for i in news],
    }


def person_lookup(s: Session, query: str, since: datetime, limit: int) -> dict[str, Any]:
    from klugscheisser.db import repo

    a = repo.resolve_athlete(s, query)
    if a is None:
        cands = [x.athlete.full_name for x in repo.find_athletes(s, query, limit=5)]
        return {"query": query, "found": False, "candidates": cands}
    affs = s.execute(
        select(m.Team.name, m.Affiliation.season, m.Affiliation.valid_from, m.Affiliation.valid_to)
        .join(m.Team, m.Team.id == m.Affiliation.team_id)
        .where(m.Affiliation.athlete_id == a.id)
        .order_by(desc(m.Affiliation.season))
    ).all()
    handles = s.scalars(select(m.Handle).where(m.Handle.athlete_id == a.id)).all()
    facts = s.scalars(
        select(m.Fact).where(m.Fact.athlete_id == a.id).order_by(desc(m.Fact.created_at)).limit(limit * 2)
    ).all()
    injuries = s.scalars(
        select(m.Fact)
        .where(m.Fact.athlete_id == a.id, m.Fact.kind == "injury")
        .order_by(desc(m.Fact.created_at))
    ).all()
    items = s.scalars(
        select(m.Item)
        .join(m.ItemAthlete, m.ItemAthlete.item_id == m.Item.id)
        .where(m.ItemAthlete.athlete_id == a.id, m.Item.published_at >= since)
        .order_by(desc(m.Item.published_at))
        .limit(limit)
    ).all()
    items_total = s.scalar(
        select(func.count()).select_from(m.ItemAthlete).where(m.ItemAthlete.athlete_id == a.id)
    )
    entries = s.execute(
        select(m.Event.name, m.EventEntry.list_type, m.EventEntry.jersey_number, m.EventEntry.fetched_at)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .where(m.EventEntry.athlete_id == a.id)
        .order_by(desc(m.Event.starts_at))
        .limit(limit)
    ).all()
    stats = s.execute(
        select(
            m.SeasonStat.season,
            m.SeasonStat.league,
            m.SeasonStat.team_name,
            m.SeasonStat.phase,
            m.SeasonStat.games,
            m.SeasonStat.points,
        )
        .where(m.SeasonStat.athlete_id == a.id)
        .order_by(desc(m.SeasonStat.season))
        .limit(4)
    ).all()

    def fact(f: m.Fact) -> dict:
        return {
            "kind": f.kind,
            "value": f.value_text[:160],
            "valid_from": f.valid_from.isoformat() if f.valid_from else None,
            "status": f.status,
            "source": f.source,
            "created_at": _dt(f.created_at),
            "url": f.source_url,
        }

    return {
        "query": query,
        "found": True,
        "person": {
            "id": a.id,
            "name": a.full_name,
            "role": a.role,
            "tier": a.tier_manual or a.tier,
            "position": a.position,
            "nationality": a.nationality,
            "birth_date": a.birth_date.isoformat() if a.birth_date else None,
        },
        "affiliations": [
            {
                "team": t,
                "season": season,
                "from": vf.isoformat() if vf else None,
                "to": vt.isoformat() if vt else None,
            }
            for t, season, vf, vt in affs
        ],
        "handles": [
            {
                "platform": h.platform,
                "handle": h.handle,
                "status": h.status,
                "last_scraped_at": _dt(h.last_scraped_at),
            }
            for h in handles
        ],
        "injury_facts": [fact(f) for f in injuries],
        "recent_facts": [fact(f) for f in facts],
        "items_total": items_total,
        "items_in_window": [_item(i) for i in items],
        "event_entries": [
            {"event": n, "list_type": lt, "jersey": j, "fetched_at": _dt(f)} for n, lt, j, f in entries
        ],
        "season_stats": [
            {"season": se, "league": lg, "team": tn, "phase": ph, "games": g, "points": p}
            for se, lg, tn, ph, g, p in stats
        ],
    }


def lookup(
    s: Session,
    *,
    persons: list[str] = (),
    teams: list[str] = (),
    since: datetime | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """All lookups in one read-only transaction (call on a fresh session; it is rolled back afterwards)."""
    s.execute(text("SET TRANSACTION READ ONLY"))
    since = since or datetime.now(UTC) - timedelta(days=14)
    try:
        return {
            "since": _dt(since),
            "overview": overview(s, since),
            "teams": [team_lookup(s, t, since, limit) for t in teams],
            "persons": [person_lookup(s, p, since, limit) for p in persons],
        }
    finally:
        s.rollback()


# --- markdown ----------------------------------------------------------------------------------------------


def _table(rows: list[dict], cols: list[str] | None = None) -> list[str]:
    if not rows:
        return ["_none_", ""]
    cols = cols or list(rows[0])
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append(
            "| "
            + " | ".join(str(r.get(c) if r.get(c) is not None else "").replace("|", "/") for c in cols)
            + " |"
        )
    return [*out, ""]


def render(d: dict[str, Any]) -> str:
    o = d["overview"]
    lines = [f"# What Karl has stored (window since {d['since']})", "", f"Now: {o['now']}", ""]
    g = o["latest_swiss_game"]
    lines.append(
        f"- latest Swiss game: {g['starts_at']} {g['home']} – {g['away']} {g['score'] or ''} ({g['source']}), "
        f"{o['hours_since_latest_swiss_game']} h ago"
        if g
        else "- latest Swiss game: none stored"
    )
    g = o["latest_final_game"]
    lines += [
        f"- latest final game overall: {g['starts_at']} {g['name']} {g['score'] or ''}" if g else "",
        "",
    ]
    lines += ["## Items by source type", "", *_table(o["items_by_source_type"])]
    for t in d["teams"]:
        lines += [f"## Team: {t['query']}", ""]
        if not t["found"]:
            lines += ["**not found**", ""]
            continue
        lines += [
            f"`{t['team']['key']}` {t['team']['name']} ({t['team']['kind']}, {t['team']['league']})",
            "",
        ]
        lines += [
            "### Latest games",
            "",
            *_table(t["latest_games"], ["starts_at", "home", "away", "score", "status", "source"]),
        ]
        lines += [
            "### Next games",
            "",
            *_table(t["next_games"], ["starts_at", "home", "away", "status", "source"]),
        ]
        lines += ["### Tournaments", "", *_table(t["tournaments"])]
        lines += ["### Rosters", "", *_table(t["rosters"])]
        cm = t["current_members"]
        lines += [f"### Current members: {cm['count']}", "", ", ".join(cm["sample"]) or "_none_", ""]
        lines += [
            "### News in window",
            "",
            *_table(t["news_in_window"], ["published_at", "source_type", "source", "title", "url"]),
        ]
    for p in d["persons"]:
        lines += [f"## Person: {p['query']}", ""]
        if not p["found"]:
            lines += [f"**not found**; candidates: {', '.join(p['candidates']) or '-'}", ""]
            continue
        pp = p["person"]
        lines += [
            f"{pp['name']} (id {pp['id']}, {pp['role']}, tier {pp['tier']}, {pp['position'] or '?'}, "
            f"{pp['nationality'] or '?'}, born {pp['birth_date'] or '?'})",
            "",
        ]
        lines += ["### Affiliations", "", *_table(p["affiliations"])]
        lines += ["### Handles", "", *_table(p["handles"])]
        lines += ["### Injury facts (all)", "", *_table(p["injury_facts"])]
        lines += [
            "### Recent facts",
            "",
            *_table(p["recent_facts"], ["kind", "value", "valid_from", "source", "created_at"]),
        ]
        lines += [
            f"### Items in window ({len(p['items_in_window'])} shown, {p['items_total']} linked in total)",
            "",
        ]
        lines += _table(p["items_in_window"], ["published_at", "source_type", "source", "title", "url"])
        lines += ["### Event entries", "", *_table(p["event_entries"])]
        lines += ["### Season stats (newest)", "", *_table(p["season_stats"])]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from klugscheisser.gaploop.envfile import read_key
    from klugscheisser.mcp_server.tools import parse_since

    p = argparse.ArgumentParser(
        prog="gap_have", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--person", action="append", default=[], help="name (repeatable)")
    p.add_argument("--team", action="append", default=[], help="team, e.g. Schweiz, 'ZSC Lions' (repeatable)")
    p.add_argument("--since", default="14d", help="window start: 7d, 2w, 2026-10-01 (default 14d)")
    p.add_argument("--limit", type=int, default=8, help="rows per section")
    p.add_argument("--database-url", help="default: DATABASE_URL from the env or ~/klugscheisser/.env (live)")
    p.add_argument("--json", action="store_true", help="JSON instead of markdown")
    args = p.parse_args(argv)
    url = args.database_url or read_key("DATABASE_URL")
    if not url:
        p.error("no DATABASE_URL")
    eng = open_readonly(url)
    try:
        with Session(eng) as s:
            d = lookup(
                s, persons=args.person, teams=args.team, since=parse_since(args.since, 14), limit=args.limit
            )
    finally:
        eng.dispose()
    print(json.dumps(d, ensure_ascii=False, indent=2) if args.json else render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
