"""Page data for the web app, built from the same helpers as the MCP tools (present.item_out, athlete_head, …).

Unlike the MCP tools, page loads never block on an update: the daily Instagram update is only *started* in the
background (non-blocking) and the page polls its status.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import staff
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.labels import date_de, flag, staff_label
from klugscheisser.domain.names import normalize
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.present import (
    athlete_head,
    datenstand,
    datenstand_times,
    item_compact,
    item_out,
    sort_items,
)
from klugscheisser.retrieval.search import Filters, athlete_team_filter, list_items

PROFILE_SOURCE_TYPES = {"profile", "interview"}
PROFILE_KINDS = {"web", "profile"}


# --- search / autocomplete --------------------------------------------------------------------------


def suggest(s: Session, query: str, limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    """Search for `/suche` (forgiving: typos, accents, other languages; see web/search.py)."""
    from klugscheisser.web import search as ws

    if not normalize(query or ""):
        return {"personen": [], "teams": [], "anlaesse": upcoming_events(s)[:5], "meinten": []}
    res = ws.search(s, query, limit, head=athlete_head, event_brief=event_brief)
    if not res["anlaesse"]:
        with contextlib.suppress(t.NotFound):
            res["anlaesse"] = [event_brief(t.resolve_event(s, query))]
            res["meinten"] = []
    return res


def _date_range(ev: m.Event) -> str:
    start, end = date_de(ev.starts_at, True), date_de(ev.ends_at, True)
    if ev.starts_at is None:
        return ""
    return start if ev.ends_at is None or end == start else f"{start}–{end}"


def event_brief(ev: m.Event) -> dict[str, Any]:
    return {
        "key": ev.key,
        "name": ev.name,
        "info": " · ".join(x for x in (_date_range(ev), ev.city) if x),
    }


def _von_bis(von: str | None, bis: str | None) -> str:
    """'28.09.2026' + 'ohne Datum' -> 'ab 28.09.2026' (no dangling dash)."""
    known = [x for x in (von, bis) if x and x != "ohne Datum"]
    if len(known) == 2:
        return von if von == bis else f"{von}–{bis}"
    if von in known:
        return f"ab {von}"
    return f"bis {bis}" if known else "Datum offen"


def upcoming_events(s: Session) -> list[dict[str, Any]]:
    """Tournaments only (single games from the results feeds are not 'Anlässe')."""
    games = set(s.scalars(select(m.Event.key).where(m.Event.kind == "game")))
    return [
        {
            "key": a["key"],
            "name": a["name"],
            "info": _von_bis(a["von"], a["bis"]) + (f" · {a['ort']}" if a["ort"] else ""),
        }
        for a in t.list_events(s)["anlaesse"]
        if a["key"] not in games
    ]


# --- person ------------------------------------------------------------------------------------------


def start_background_update(
    s: Session, settings: Settings, athletes: list[m.Athlete]
) -> dict[str, Any] | None:
    """Daily auto-update without waiting: returns {"status", "job_id"} when an update is running."""
    from klugscheisser.jobs.freshness import ensure_fresh, stale_athletes

    if not (settings.auto_refresh and settings.web_auto_refresh) or not stale_athletes(athletes):
        return None
    return ensure_fresh(s, settings, athletes, wait_s=0).out()


def _is_profile(i: m.Item) -> bool:
    return (
        i.source_type in PROFILE_SOURCE_TYPES
        or i.source_kind in PROFILE_KINDS
        or i.platform
        in {
            "web",
            "wikipedia",
        }
    )


def person_data(s: Session, settings: Settings, athlete: m.Athlete) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=settings.posts_lookback_days)
    recent = t.recent_items(s, [athlete.id], since, 200)
    newest = sorted(recent, key=lambda i: i.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    vorsicht = [i for i in recent if i.classification and i.classification.vorsicht]
    shown = {i.id for i in vorsicht}
    instagram = instagram_page(s, settings, athlete.id, 0)
    events = s.execute(
        select(m.Event, m.EventEntry)
        .join(m.EventEntry)
        .where(m.EventEntry.athlete_id == athlete.id)
        .order_by(m.Event.starts_at.desc().nulls_last())
    ).all()
    return {
        "datenstand": datenstand(s, [athlete.id]),
        "stand": datenstand_times(s, [athlete.id]),
        "athletin": athlete_head(s, athlete),
        "vorsicht": [item_out(i) for i in vorsicht],
        "instagram": [x for x in instagram["posts"] if x["id"] not in shown],
        "instagram_mehr": instagram["mehr"],
        "news": [item_out(i) for i in newest if i.platform == "rss" and i.id not in shown][:6],
        "x": [item_out(i) for i in newest if i.platform == "x" and i.id not in shown][:6],
        "resultate": [
            item_out(i) for i in newest if i.source_type == "official_result" and i.id not in shown
        ][:6],
        "profil": [item_out(i) for i in newest if _is_profile(i) and i.id not in shown][:6],
        "medien": [item_out(i) for i in media_links(s, athlete.id)],
        "ep": ep_timeline(s, athlete.id),
        "anlaesse": [
            {
                "key": ev.key,
                "name": ev.name,
                "datum": date_de(ev.starts_at, True),
                "start": ev.starts_at,
                "kader_status": e.list_type,
                "quelle": e.source,
                "url": e.source_url,
            }
            for ev, e in events
        ],
    }


def ep_timeline(s: Session, athlete_id: int) -> dict[str, Any] | None:
    """v11: her transfers from Elite Prospects (date, from, to, length, source link) for the timeline."""
    ep = s.get(m.EpProfile, athlete_id)
    if ep is None:
        return None
    return {"url": ep.url, "stand": ep.fetched_at, "transfers": (ep.data or {}).get("transfers") or []}


def media_links(s: Session, athlete_id: int, limit: int = 12) -> list[m.Item]:
    """v8: podcasts and videos with her (links only), newest first, any age."""
    return list(
        s.scalars(
            select(m.Item)
            .join(m.ItemAthlete)
            .where(m.ItemAthlete.athlete_id == athlete_id, m.Item.source_type == "media_link")
            .order_by(m.Item.published_at.desc().nulls_last())
            .limit(limit)
        )
    )


def instagram_page(s: Session, settings: Settings, athlete_id: int, offset: int) -> dict[str, Any]:
    """Newest first, `web_page_posts` per page; same source types as get_athlete_posts (own + tagged + SRF)."""
    n = settings.web_page_posts
    f = Filters(
        athlete_ids=[athlete_id],
        since=datetime.now(UTC) - timedelta(days=settings.posts_lookback_days),
        source_types=["athlete_post", "tagged_post", "srf"],
        platform="instagram",
    )
    items = list_items(s, f, limit=offset + n + 1)
    page = items[offset : offset + n]
    return {
        "posts": [{**item_out(i), "id": i.id} for i in page],
        "mehr": offset + n if len(items) > offset + n else None,
    }


# --- event / team --------------------------------------------------------------------------------------


def recent_items_by_athlete(
    s: Session, athlete_ids: list[int], since: datetime, platform: str | None = None, per: int = 200
) -> dict[int, list[m.Item]]:
    """Same as `tools.recent_items(s, [id], since, …)` for many persons at once: per person the `per` newest
    matching items, sorted like everywhere (Vorsicht first, newsworthiness, recency)."""
    from collections import defaultdict

    from sqlalchemy import func
    from sqlalchemy.orm import selectinload

    from klugscheisser.retrieval.search import _filtered

    out: dict[int, list[m.Item]] = defaultdict(list)
    if not athlete_ids:
        return out
    allowed = _filtered(Filters(athlete_ids=athlete_ids, since=since, platform=platform)).subquery()
    rank = (
        func.row_number()
        .over(partition_by=m.ItemAthlete.athlete_id, order_by=m.Item.published_at.desc().nulls_last())
        .label("rn")
    )
    pairs = (
        select(m.ItemAthlete.athlete_id, m.Item.id.label("item_id"), rank)
        .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
        .where(m.ItemAthlete.athlete_id.in_(athlete_ids), m.Item.id.in_(select(allowed.c.id)))
        .subquery()
    )
    rows = s.execute(select(pairs.c.athlete_id, pairs.c.item_id).where(pairs.c.rn <= per)).all()
    items = {
        i.id: i
        for i in s.scalars(
            select(m.Item)
            .where(m.Item.id.in_({r.item_id for r in rows}))
            .options(selectinload(m.Item.classification))
        )
    }
    for aid, item_id in rows:
        if item_id in items:
            out[aid].append(items[item_id])
    return defaultdict(list, {aid: sort_items(v) for aid, v in out.items()})


def event_data(s: Session, settings: Settings, ev: m.Event) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=settings.posts_lookback_days)
    teams = s.execute(
        select(m.Team, m.EventTeam.group_name)
        .join(m.EventTeam)
        .where(m.EventTeam.event_id == ev.id)
        .order_by(m.EventTeam.group_name, m.Team.name)
    ).all()
    teams = sorted(teams, key=lambda x: x[0].country != "SUI")  # Swiss team first, order otherwise kept
    entries = list(s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == ev.id)))
    by_team: dict[int | None, list[m.EventEntry]] = {}
    for en in entries:
        by_team.setdefault(en.team_id, []).append(en)
    order = {"G": 0, "D": 1, "F": 2}
    ids = [en.athlete_id for en in entries]
    recent = recent_items_by_athlete(s, ids, since)  # one query for the whole roster (was 2 per player)
    recent_ig = recent_items_by_athlete(s, ids, since, platform="instagram")
    blocks = []
    for team, group in teams:
        es = by_team.get(team.id, [])
        players = []
        for en in sorted(es, key=lambda x: (order.get(x.role or "", 3), x.athlete.full_name)):
            a = en.athlete
            head = athlete_head(s, a)
            items = recent[a.id][:10]
            posts = recent_ig[a.id][:1]
            players.append(
                {
                    "id": a.id,
                    "name": a.full_name,
                    "flagge": flag(a.nationality),
                    "nation": a.nationality,
                    "position": head["position"] or staff_label(en.role),
                    "nummer": en.jersey_number,
                    "rolle": en.role,
                    "tier": head["tier"],
                    "quellen": head["quellen"],
                    "status": en.list_type,
                    "verein": head["vereine_laut_quellen"][0] if head["vereine_laut_quellen"] else None,
                    "instagram": head["instagram"][0] if head["instagram"] else None,
                    "vorsicht": [
                        item_compact(i) for i in items if i.classification and i.classification.vorsicht
                    ],
                    "neuester_post": item_compact(posts[0]) if posts else None,
                }
            )
        status = None
        if es:
            status = "confirmed" if all(x.list_type == "confirmed" for x in es) else "provisional"
        blocks.append(
            {
                "id": team.id,
                "team": team.name,
                "gruppe": group,
                "schweiz": team.country == "SUI",
                "kader_status": status,
                "kader_quelle": {"quelle": es[0].source, "url": es[0].source_url} if es else None,
                "spielerinnen": players,
                "kontakte": staff.contacts(s, team),  # 2026-11-01: media manager / press officer
            }
        )
    confirmed = bool(entries) and all(x.list_type == "confirmed" for x in entries)
    from klugscheisser.web.brief import brief_form

    swiss = [x for x in entries if x.team and x.team.country == "SUI"] or entries
    return {
        "brief_form": brief_form(ev.key, [t for t, _ in teams]),
        "datenstand": datenstand(s, [x.athlete_id for x in entries]),
        "stand": datenstand_times(s, [x.athlete_id for x in entries]),
        "kader_stand": roster_stand(swiss),
        "anlass": {
            "name": ev.name,
            "key": ev.key,
            "von": date_de(ev.starts_at, True),
            "bis": date_de(ev.ends_at, True),
            "ort": ev.city,
            "status": ev.status,
            "url": ev.source_url,
        },
        "lineup_status": "Bestätigter Kader."
        if confirmed
        else "Kader PROVISORISCH – nicht die offizielle Matchaufstellung. Im Gespräch so kennzeichnen.",
        "teams": blocks,
    }


def roster_stand(entries: list[m.EventEntry]) -> dict[str, Any] | None:
    """v9: when the shown roster was stored and from where ("Kader 28.09.2026 (SIHF)")."""
    if not entries:
        return None
    newest = max(entries, key=lambda x: x.fetched_at)
    return {"datum": date_de(newest.fetched_at, True), "quelle": newest.source, "url": newest.source_url}


def team_data(s: Session, settings: Settings, team: m.Team) -> dict[str, Any]:
    roster = t.get_team_roster(s, team.key)
    f = athlete_team_filter(s, team)
    f.since = datetime.now(UTC) - timedelta(days=settings.posts_lookback_days)
    items = sort_items(list_items(s, f, limit=60))
    # v9: number, role and club per player from the same newest roster the tool uses
    entries = list(
        s.scalars(
            select(m.EventEntry)
            .join(m.Event)
            .where(m.EventEntry.team_id == team.id)
            .order_by(m.Event.starts_at.desc().nulls_last())
        )
    )
    entries = [x for x in entries if x.event_id == entries[0].event_id] if entries else []
    by_athlete = {x.athlete_id: x for x in entries}
    for p in roster["spielerinnen"]:
        en = by_athlete.get(p["id"])
        head = athlete_head(s, s.get(m.Athlete, p["id"]))
        p["nummer"] = en.jersey_number if en else None
        p["rolle"] = en.role if en else None
        p["position"] = p["position"] or (staff_label(en.role) if en else None)
        p["verein"] = head["vereine_laut_quellen"][0] if head["vereine_laut_quellen"] else None
        p["tier"], p["quellen"] = head["tier"], head["quellen"]
    return {
        "datenstand": roster["datenstand"],
        "stand": datenstand_times(s, [p["id"] for p in roster["spielerinnen"]]),
        "kader_stand": roster_stand(entries),
        "team": {"id": team.id, "name": team.name, "liga": team.league, "land": team.country},
        "kader_quelle": roster["kader_quelle"],
        "spielerinnen": roster["spielerinnen"],
        "kontakte": staff.contacts(s, team),  # 2026-11-01: media manager / press officer
        "vorsicht": [item_out(i) for i in items if i.classification and i.classification.vorsicht],
        "eintraege": [item_out(i) for i in items if not (i.classification and i.classification.vorsicht)][
            :20
        ],
    }
