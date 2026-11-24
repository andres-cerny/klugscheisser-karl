"""Game watch (2026-10-31): IIHF games read right after the final buzzer, then a follow-up per game.

Why: during the WCh 2026 Fabi prepares the next broadcast right after a game. The nightly run (05:00) and the
on-demand results TTL came too late or only when someone asked; now Karl stores the game sheet a few minutes after
the final and, about an hour later, the first match reports, quotes and posts of both teams.

Thread `game-watch` (started by `serve`, next to the nightly worker), per scheduled project, every TICK_S:
1. Games of every current IIHF event (`ingest.results.iihf_events`: IIHF_EVENT_ID + IIHF_EVENTS) from the
   database (schedule rows written by the seeds, the nightly results step and earlier polls). Nothing to do = no
   request at all.
2. Poll (one event feed request + the game state of live/new final games, the same collector and ingest path as
   the nightly results step) every GAME_WATCH_POLL_MINUTES while a game is in its window: from the scheduled start
   until it is final with a game sheet, at most GAME_WATCH_MAX_HOURS. While an event runs (games within 2 days)
   the schedule is also read every GAME_WATCH_SCHEDULE_HOURS (knock-out games appear in the feed late).
3. Final with sheet -> marker `gamewatch-final`; the sheet is read again at GAME_WATCH_RECHECK_MINUTES after the
   final (IIHF corrects scorers), and at every poll while it is incomplete (fewer goals than the score). New or
   changed sheets -> national-team tournament totals recounted from the stored sheets (`stats` shows the game),
   and players who dressed but are not on the tournament roster added from the sheet (jobs/game_rosters.py,
   GAME_WATCH_SHEET_ROSTERS; database only, nobody removed).
4. GAME_WATCH_FOLLOWUP_DELAY_MIN after the final, once per game (marker `gamewatch-followup`, claimed before the
   run): news feeds, the two teams' official Instagram accounts and their roster players (handles not read since
   the game started), inside the daily LLM cap, the Apify daily/cycle caps (plus GAME_WATCH_FOLLOWUP_APIFY_USD per
   follow-up) and Bright Data's own monthly cap. A job row `gamewatch_followup` keeps what was done.
   Deferred while a nightly run (05:00, by hand or the web button) is going. First step (free, also when the
   LLM cap is used up): the roster watcher (kaderwatch, `jobs/roster_watch.run(only=…)`) for the two teams
   (GAME_WATCH_ROSTER_WATCH); the 05:00 nightly kaderwatch of all teams stays as it is.

Idempotent: results go through `ingest_results` (events keyed iihf-<GameId>, items by URL), under the same
advisory lock as the nightly results step; markers are (source, target) rows in `cache_entries`.
`klugscheisser gamewatch --status` lists the games with what was done; `--once` runs one pass by hand.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.domain.labels import TZ

log = logging.getLogger(__name__)

TICK_S = 60.0  # the thread looks at the database this often (no request unless something is due)
LEAD = timedelta(minutes=5)  # polling starts this long before the scheduled start
SPAN = timedelta(days=2)  # games this close to now count (event "running", markers, schedule reads)
LATE = timedelta(hours=18)  # a follow-up later than this after the start is no longer useful: skipped
NIGHTLY_MAX_AGE = timedelta(hours=8)  # a "running" nightly job older than this counts as crashed

SRC_POLL = "results-gamewatch"  # target "iihf:<event id>": last poll (also the MCP "Resultate" Datenstand)
SRC_FINAL = "gamewatch-final"  # target = game key: final with game sheet first stored
SRC_RECHECK = "gamewatch-recheck"  # target = game key: last re-read of the sheet after the final
SRC_FOLLOWUP = "gamewatch-followup"  # target = game key: follow-up claimed (once per game)
JOB_KIND = "gamewatch_followup"
SHOOTOUT_PERIODS = {"SO", "GWS", "PS", "PSS"}
_SCORE = re.compile(r"\s*(\d+)\s*:\s*(\d+)")


# --- state of one game ---------------------------------------------------------------------------------


@dataclass
class Game:
    key: str
    external_id: str
    name: str
    starts_at: datetime
    status: str
    score: str | None
    home_team_id: int | None
    away_team_id: int | None
    final_at: datetime | None = None
    rechecked_at: datetime | None = None
    followup_at: datetime | None = None
    has_sheet: bool = False
    complete: bool = False


def sheet_complete(item: m.Item | None) -> bool:
    """The stored sheet has a scorer for every goal of the score (shootout winner left out) and goalies of both
    teams. IIHF fills the sheet during the game; right at the buzzer the last actions can still be missing."""
    if item is None:
        return False
    raw = item.raw or {}
    score = raw.get("score") or ""
    hit = _SCORE.match(score)
    if not hit:
        return False
    expected = int(hit[1]) + int(hit[2]) - (1 if "n.P." in score else 0)
    goals = [g for g in raw.get("goals") or [] if str(g.get("period") or "").upper() not in SHOOTOUT_PERIODS]
    if len(goals) < expected or any(not g.get("scorer") for g in goals):
        return False
    goalie_teams = {p.get("team") for p in raw.get("participants") or [] if p.get("role") == "goalie"}
    return len(goalie_teams) >= 2


def _markers(s: Session, source: str, keys: list[str]) -> dict[str, datetime]:
    if not keys:
        return {}
    rows = s.execute(
        select(m.CacheEntry.target, m.CacheEntry.fetched_at).where(
            m.CacheEntry.source == source, m.CacheEntry.target.in_(keys)
        )
    )
    return dict(rows.all())


def _mark(s: Session, source: str, target: str, when: datetime) -> None:
    entry = s.get(m.CacheEntry, (source, target))
    if entry is None:
        s.add(m.CacheEntry(source=source, target=target, fetched_at=when))
    else:
        entry.fetched_at = when
    s.flush()


def _claim(s: Session, source: str, target: str, when: datetime) -> bool:
    """Set a marker only if it is not there yet (two passes at once: only one wins)."""
    new = s.scalar(
        pg_insert(m.CacheEntry)
        .values(source=source, target=target, fetched_at=when)
        .on_conflict_do_nothing(index_elements=[m.CacheEntry.source, m.CacheEntry.target])
        .returning(m.CacheEntry.target)
    )
    return new is not None


def watch_events(settings: Settings) -> list:
    """The IIHF events the game watch looks at (the same as the regular results run)."""
    if not settings.iihf_realtime_enabled:
        return []
    from klugscheisser.ingest.results import iihf_events

    return iihf_events(settings)


def event_games(s: Session, event, now: datetime | None = None, span: timedelta | None = SPAN) -> list[Game]:
    """Stored games of one IIHF event (by its competition name), within `span` of now (None = all)."""
    comp_ids = select(m.Competition.id).where(m.Competition.name == event.competition)
    q = select(m.Event).where(
        m.Event.kind == "game",
        m.Event.key.startswith("iihf-"),
        m.Event.competition_id.in_(comp_ids),
        m.Event.starts_at.is_not(None),
    )
    if span is not None and now is not None:
        q = q.where(m.Event.starts_at >= now - span, m.Event.starts_at <= now + span)
    evs = list(s.scalars(q.order_by(m.Event.starts_at, m.Event.key)))
    keys = [e.key for e in evs]
    ext = [k.split("-", 1)[1] for k in keys]
    items = {
        i.external_id: i
        for i in s.scalars(
            select(m.Item).where(
                m.Item.platform == "iihf",
                m.Item.source_type == "official_result",
                m.Item.external_id.in_(ext),
            )
        )
    }
    final, recheck, follow = (_markers(s, src, keys) for src in (SRC_FINAL, SRC_RECHECK, SRC_FOLLOWUP))
    out = []
    for e, x in zip(evs, ext, strict=True):
        item = items.get(x)
        out.append(
            Game(
                key=e.key,
                external_id=x,
                name=e.name,
                starts_at=e.starts_at,
                status=e.status,
                score=e.score,
                home_team_id=e.home_team_id,
                away_team_id=e.away_team_id,
                final_at=final.get(e.key),
                rechecked_at=recheck.get(e.key),
                followup_at=follow.get(e.key),
                has_sheet=item is not None,
                complete=sheet_complete(item),
            )
        )
    return out


def mark_finals(s: Session, games: list[Game], now: datetime) -> list[Game]:
    """Games now final with a stored sheet get their `gamewatch-final` marker (also when another path, e.g. an
    on-demand results refresh, stored them first). Returns the games marked just now."""
    new = []
    for g in games:
        if g.final_at is None and g.status == "final" and g.has_sheet and _claim(s, SRC_FINAL, g.key, now):
            g.final_at = now
            new.append(g)
    return new


def _offsets(settings: Settings) -> list[timedelta]:
    return sorted(timedelta(minutes=x) for x in settings.game_watch_recheck_minutes if x >= 0)


def recheck_due(g: Game, now: datetime, settings: Settings) -> bool:
    if g.final_at is None:
        return False
    due = [o for o in _offsets(settings) if now >= g.final_at + o]
    if not due:
        return False
    return g.rechecked_at is None or g.rechecked_at < g.final_at + due[-1]


def needs_poll(g: Game, now: datetime, settings: Settings) -> str | None:
    """Why this game needs the feed now (None = it does not)."""
    if g.final_at is None:
        start, end = g.starts_at - LEAD, g.starts_at + timedelta(hours=settings.game_watch_max_hours)
        if start <= now <= end:
            return "final ohne Spielblatt" if g.status == "final" else g.status
        return None
    offsets = _offsets(settings)
    last = g.final_at + (offsets[-1] if offsets else timedelta(0))
    if not g.complete and now <= last:
        return "Spielblatt unvollständig"
    if recheck_due(g, now, settings):
        return "Nachkontrolle"
    return None


def followup_due(g: Game, now: datetime, settings: Settings) -> bool:
    return (
        settings.game_watch_followup
        and g.final_at is not None
        and g.followup_at is None
        and now >= g.final_at + timedelta(minutes=settings.game_watch_followup_delay_min)
    )


def other_run_active(s: Session, now: datetime) -> bool:
    """A nightly run (05:00 thread, by hand, or the web button) is going: the follow-up waits for the next tick.
    Uses the double-run guard of jobs/ondemand.py when that module exists (PR #57), else the job rows."""
    try:
        from klugscheisser.jobs import ondemand
    except ImportError:
        ondemand = None
    if ondemand is not None and hasattr(ondemand, "active_run"):
        return ondemand.active_run(s, now) is not None
    running = s.scalar(
        select(func.count())
        .select_from(m.Job)
        .where(
            m.Job.kind == "nightly",
            m.Job.status.in_(["queued", "running"]),
            m.Job.created_at >= now - NIGHTLY_MAX_AGE,
        )
    )
    return bool(running)


# --- poll ----------------------------------------------------------------------------------------------


def poll_event(settings: Settings, event, refetch: set[str], now: datetime, http=None) -> dict[str, Any]:
    """One read of the event feed (+ game states) through the regular results ingest."""
    from klugscheisser.collectors.results.base import PoliteHttp
    from klugscheisser.collectors.results.iihf import IihfCollector
    from klugscheisser.ingest.results import RESULTS_LOCK, ingest_results
    from klugscheisser.ingest.runner import build_context

    with session_scope() as s:
        # the nightly / on-demand results refresh holds this lock while it runs: try again next tick
        if not s.scalar(sql_text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": RESULTS_LOCK}):
            return {"status": "läuft bereits"}
        http = http or PoliteHttp(settings.results_request_delay_s)
        before = http.requests
        collector = IihfCollector([event], max_details=settings.results_max_details, http=http)
        collector.refetch = set(refetch)
        report = ingest_results(build_context(s, settings), [collector], since=now - SPAN)
        _mark(s, SRC_POLL, f"iihf:{event.event_id}", now)
        return {**asdict(report), "anfragen": http.requests - before}


def _line(g: Game) -> str:
    return f"{g.name} {g.score or ''} ({g.status})".replace("  ", " ")


def watch_event(settings: Settings, event, now: datetime, http=None) -> dict[str, Any] | None:
    """Decide and, when due, poll one event. Returns the poll summary (None = idle)."""
    target = f"iihf:{event.event_id}"
    with session_scope() as s:
        games = event_games(s, event, now)
        mark_finals(s, games, now)
        reasons = {g.key: r for g in games if (r := needs_poll(g, now, settings))}
        last = s.get(m.CacheEntry, (SRC_POLL, target))
        last_at = last.fetched_at if last else None
    if not games:
        return None
    if reasons:
        gap = timedelta(minutes=settings.game_watch_poll_minutes) - timedelta(seconds=30)
        why = "; ".join(f"{g.name}: {reasons[g.key]}" for g in games if g.key in reasons)
    else:
        if settings.game_watch_schedule_hours <= 0:
            return None
        gap = timedelta(hours=settings.game_watch_schedule_hours)
        why = "Spielplan"
    if last_at is not None and now - last_at < gap:
        return None
    refetch = {g.external_id for g in games if g.key in reasons and g.final_at is not None}
    report = poll_event(settings, event, refetch, now, http)
    out: dict[str, Any] = {"event": event.event_id, "grund": why, **report}
    if report.get("status") == "läuft bereits":
        log.info("gamewatch %s: results refresh already running, next tick", target)
        return out
    with session_scope() as s:
        after = event_games(s, event, now)
        new = mark_finals(s, after, now)
        for g in after:
            if g.external_id in refetch and g.final_at is not None:
                _mark(s, SRC_RECHECK, g.key, now)
        if report.get("items_new") or report.get("items_changed"):
            from klugscheisser.stats.current import TotalsReport, sheet_totals

            totals = sheet_totals(s, TotalsReport(), ("iihf",))
            out["statistik"] = {"gespeichert": totals.stored, "fehler": totals.errors}
            if settings.game_watch_sheet_rosters:
                from klugscheisser.jobs.game_rosters import reconcile_finals

                kader = reconcile_finals(s, [g.key for g in after if g.final_at is not None])
                changed = {
                    k: {
                        c: v
                        for c, v in r.items()
                        if isinstance(v, dict) and (v.get("dazu") or v.get("unklar"))
                    }
                    for k, r in kader.items()
                }
                out["kader_spielbericht"] = {k: v for k, v in changed.items() if v}
        live = [_line(g) for g in after if g.status == "live"]
        out["live"] = live
        out["final_neu"] = [_line(g) for g in new]
        out["unvollstaendig"] = [g.name for g in after if g.final_at is not None and not g.complete]
    log.info(
        "gamewatch %s: %s | %s Anfragen | live: %s | neu final: %s | Spielblätter neu/geändert: %s/%s%s",
        target,
        why,
        out.get("anfragen", 0),
        ", ".join(live) or "-",
        ", ".join(out["final_neu"]) or "-",
        report.get("items_new", 0),
        report.get("items_changed", 0),
        f" | Fehler: {report['errors']}" if report.get("errors") else "",
    )
    return out


# --- follow-up -----------------------------------------------------------------------------------------


def game_roster(s: Session, ev: m.Event, team_id: int) -> list[int]:
    """The team's roster for the tournament this game belongs to (a tournament event whose dates include the
    game); without one, everyone Karl links to the team (as the team's "Aktualisieren" does)."""
    day = ev.starts_at.astimezone(TZ).date()
    ids: list[int] = []
    for c in s.scalars(
        select(m.Event).where(m.Event.kind != "game", m.Event.starts_at.is_not(None)).order_by(m.Event.id)
    ):
        first = c.starts_at.astimezone(TZ).date()
        last = (c.ends_at or c.starts_at).astimezone(TZ).date()
        if first - timedelta(days=1) <= day <= last + timedelta(days=1):
            ids += s.scalars(
                select(m.EventEntry.athlete_id).where(
                    m.EventEntry.event_id == c.id, m.EventEntry.team_id == team_id
                )
            )
    if not ids:
        from klugscheisser.retrieval.search import athlete_team_filter

        ids = athlete_team_filter(s, s.get(m.Team, team_id)).athlete_ids or []
    return list(dict.fromkeys(ids))


def _not_read_since(a: m.Athlete, since: datetime) -> bool:
    from klugscheisser.handles.service import usable_handles

    hs = [h for h in usable_handles(a) if not (h.profile or {}).get("private")]
    return any(h.last_scraped_at is None or h.last_scraped_at < since for h in hs)


def followup_settings(s: Session, settings: Settings, now: datetime) -> tuple[Settings, float | None]:
    """Settings for the follow-up's Instagram read: the Apify cap lowered to what is left today, in the Apify
    billing cycle and GAME_WATCH_FOLLOWUP_APIFY_USD. Returns (settings, Apify room in USD or None = free source)."""
    from klugscheisser.collectors.instagram.sources import apify_used
    from klugscheisser.ingest import costs
    from klugscheisser.jobs.nightly import apify_cycle_spent

    if not apify_used(settings):
        return settings, None
    room = min(
        settings.game_watch_followup_apify_usd,
        costs.remaining(s, "apify", settings.apify_daily_cost_cap_usd),
    )
    if settings.apify_cycle_cap_usd:
        spent = apify_cycle_spent(s, settings, now.astimezone(TZ).date())
        room = min(room, settings.apify_cycle_cap_usd - spent)
    room = max(0.0, room)
    cap = costs.spent_today(s, "apify") + room
    return settings.model_copy(update={"apify_daily_cost_cap_usd": cap}), room


def _step(result: dict, name: str, fn) -> None:
    try:
        result[name] = fn()
    except Exception as exc:  # one source failing must not stop the others
        log.exception("gamewatch follow-up step %s failed", name)
        result[name] = {"fehler": str(exc)}
        result.setdefault("fehler", []).append(f"{name}: {exc}")


def run_followup(
    settings: Settings,
    job_id: int,
    now: datetime | None = None,
    instagram_source=None,
    news_collector=None,
    roster_watch: Callable | None = None,
) -> dict[str, Any]:
    """The follow-up of one game (its job row was created and the marker claimed by `start_followups`).
    `roster_watch(session, codes)` = the kaderwatch run for the two teams (tests pass a fake)."""
    from klugscheisser.collectors.instagram.sources import get_instagram_source, instagram_configured
    from klugscheisser.collectors.rss import RssCollector
    from klugscheisser.ingest.instagram import ingest_instagram, ingest_instagram_teams
    from klugscheisser.ingest.news import ingest_news
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.jobs.refresh import is_fresh, touch
    from klugscheisser.llm.budget import daily_answer_spend

    now = now or datetime.now(UTC)
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.started_at = "running", datetime.now(UTC)
        key = job.params["spiel"]
        ev = s.scalar(select(m.Event).where(m.Event.key == key))
        starts_at = ev.starts_at
        team_ids = [t for t in (ev.home_team_id, ev.away_team_id) if t]
    result: dict[str, Any] = {}
    skipped: list[str] = []
    if settings.game_watch_roster_watch:

        def kaderwatch() -> dict:  # free (official pages, IIHF mirror), so before the LLM cap check
            with session_scope() as s:
                codes = {
                    (t.country or "").upper() for t in (s.get(m.Team, i) for i in team_ids) if t and t.country
                }
                fn = roster_watch or _roster_watch
                return compact_roster_watch(fn(s, codes))

        _step(result, "kaderwatch", kaderwatch)
    try:
        with session_scope() as s:
            llm_spent = daily_answer_spend(s, settings)
        if settings.llm_daily_cost_cap_usd and llm_spent >= settings.llm_daily_cost_cap_usd:
            skipped.append(
                f"alles: LLM-Tageslimit erreicht (${llm_spent:.2f} von ${settings.llm_daily_cost_cap_usd})"
            )
        else:

            def news() -> dict:
                with session_scope() as s:
                    if is_fresh(s, "news", "all", settings.ttl_news):
                        return {"status": "aktuell"}
                    r = asdict(ingest_news(build_context(s, settings), news_collector or RssCollector()))
                    touch(s, "news", "all")
                    return r

            _step(result, "news", news)
            if instagram_source is None and not instagram_configured(settings):
                skipped.append("instagram: keine Instagram-Quelle konfiguriert")
            else:
                with session_scope() as s:
                    local, room = followup_settings(s, settings, now)
                    teams = [s.get(m.Team, t) for t in team_ids]
                    accounts = [t.instagram_handle for t in teams if t is not None and t.instagram_handle]
                    persons: list[int] = []
                    for t in team_ids:
                        athletes = [s.get(m.Athlete, i) for i in game_roster(s, ev_of(s, key), t)]
                        fresh = [a for a in athletes if a is not None and _not_read_since(a, starts_at)]
                        fresh.sort(key=lambda a: (a.tier, a.id))
                        persons += [a.id for a in fresh[: settings.game_watch_followup_max_persons]]
                if room is not None and room <= 0:
                    skipped.append(
                        "instagram (Apify-Teil): Apify-Limite erreicht (Tag, Zyklus oder Follow-up)"
                    )
                result["apify_rahmen_usd"] = None if room is None else round(room, 3)
                source = instagram_source or get_instagram_source(local)
                gap = max(
                    60, int((now - starts_at).total_seconds())
                )  # read again when last read before the game

                def team_accounts() -> dict:
                    if not accounts:
                        return {"status": "keine Teamaccounts"}
                    with session_scope() as s:
                        r = ingest_instagram_teams(build_context(s, local), source, accounts, min_gap_s=gap)
                        return asdict(r)

                def players() -> dict:
                    if not persons:
                        return {"status": "alle schon nach Spielbeginn gelesen oder ohne Account"}
                    with session_scope() as s:
                        r = ingest_instagram(
                            build_context(s, local), source, [s.get(m.Athlete, i) for i in persons]
                        )
                        return {**asdict(r), "personen": len(persons)}

                _step(result, "instagram_teams", team_accounts)
                _step(result, "instagram_personen", players)
                for part in ("instagram_teams", "instagram_personen"):
                    if (result.get(part) or {}).get("skipped_budget"):
                        skipped.append(
                            f"{part}: {len(result[part]['skipped_budget'])} Accounts über einer Limite"
                        )
    except Exception as exc:  # the job row must not stay "running"
        log.exception("gamewatch follow-up %s failed", key)
        result.setdefault("fehler", []).append(str(exc))
    if skipped:
        result["uebersprungen"] = skipped
        log.warning("gamewatch follow-up %s: skipped %s", key, skipped)
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.result, job.finished_at = result, datetime.now(UTC)
        job.status = "failed" if result.get("fehler") else "done"
        job.error = "; ".join(result.get("fehler", [])) or None
    log.info("gamewatch follow-up %s: %s", key, summarize(result))
    return result


def _roster_watch(s: Session, codes: set[str]) -> dict:
    from klugscheisser.jobs.roster_watch import run

    return run(s, only=codes)


def compact_roster_watch(report: dict) -> dict:
    """Per team only what matters after a game: list status, added/removed players, errors."""
    out: dict[str, Any] = {"teams": sorted(report.get("geprueft") or [])}
    for code, ch in (report.get("kader") or {}).items():
        out[code] = {k: ch.get(k) for k in ("status", "dazu", "weg", "quelle", "datum") if ch.get(k)}
    if report.get("fehler"):
        out["fehler_seiten"] = report["fehler"]  # a federation page down is no follow-up failure
    if report.get("neu"):
        out["neu"] = report["neu"]
    return out


def ev_of(s: Session, key: str) -> m.Event:
    return s.scalar(select(m.Event).where(m.Event.key == key))


def start_followups(
    settings: Settings, event, now: datetime, submit: Callable | None = None, **kwargs
) -> list[int]:
    """Claim and start every due follow-up of one event (not while a nightly run is going). Returns job ids."""
    started: list[int] = []
    with session_scope() as s:
        games = [g for g in event_games(s, event, now) if followup_due(g, now, settings)]
        if not games:
            return []
        if other_run_active(s, now):
            log.info("gamewatch: follow-up of %s waits, a nightly run is going", [g.name for g in games])
            return []
        for g in games:
            if not _claim(s, SRC_FOLLOWUP, g.key, now):
                continue
            late = now - g.starts_at > LATE
            job = m.Job(
                kind=JOB_KIND,
                params={"spiel": g.key, "name": g.name, "resultat": g.score},
                status="done" if late else "queued",
            )
            if late:
                job.result = {"uebersprungen": ["zu spät (Server war aus?)"]}
                job.finished_at = datetime.now(UTC)
            s.add(job)
            s.flush()
            if not late:
                started.append(job.id)
    for job_id in started:
        if submit is None:
            run_followup(settings, job_id, now, **kwargs)
        else:
            submit(run_followup, settings, job_id, None, **kwargs)
    return started


def summarize(result: dict | None) -> str:
    """'News 3 neu · Instagram 5 Posts (2 Team) · $0.02' for status lines and logs."""
    r = result or {}
    bits = []
    news = r.get("news") or {}
    if "items_new" in news:
        bits.append(f"News {news['items_new']} neu")
    elif news.get("status"):
        bits.append(f"News {news['status']}")
    posts = sum(
        int((r.get(p) or {}).get("items_new") or 0) for p in ("instagram_teams", "instagram_personen")
    )
    team = int((r.get("instagram_teams") or {}).get("team_posts_new") or 0)
    cost = sum(
        float((r.get(p) or {}).get("cost_usd") or 0) for p in ("instagram_teams", "instagram_personen")
    )
    if "instagram_teams" in r or "instagram_personen" in r:
        bits.append(f"Instagram {max(posts, team)} Posts ({team} Team)" + (f" · ${cost:.2f}" if cost else ""))
    kw = r.get("kaderwatch") or {}
    if kw.get("teams"):
        changes = [
            f"{c} +{len(v.get('dazu') or [])}/-{len(v.get('weg') or [])}"
            for c, v in kw.items()
            if isinstance(v, dict) and v.get("status") == "übernommen"
        ]
        bits.append(
            "Kaderwatch " + "/".join(kw["teams"]) + (": " + ", ".join(changes) if changes else " unverändert")
        )
    if r.get("uebersprungen"):
        bits.append("übersprungen: " + "; ".join(r["uebersprungen"]))
    if r.get("fehler"):
        bits.append("Fehler: " + "; ".join(r["fehler"]))
    return " · ".join(bits) or "-"


# --- one pass, status, thread --------------------------------------------------------------------------


def watch_pass(
    settings: Settings,
    now: datetime | None = None,
    http=None,
    submit: Callable | None = None,
    instagram_source=None,
    news_collector=None,
    roster_watch: Callable | None = None,
) -> dict[str, Any]:
    """One look at every watched event of the current project: poll when due, then start due follow-ups."""
    if not settings.game_watch:
        return {"status": "aus (GAME_WATCH=false)"}
    events = watch_events(settings)
    if not events:
        return {"status": "aus (IIHF_REALTIME_ENABLED=false)"}
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {"polls": [], "follow_ups": []}
    for event in events:
        try:
            if (r := watch_event(settings, event, now, http)) is not None:
                out["polls"].append(r)
        except Exception as exc:
            log.exception("gamewatch: event %s failed", event.event_id)
            out.setdefault("fehler", []).append(f"{event.event_id}: {exc}")
        try:
            out["follow_ups"] += start_followups(
                settings,
                event,
                now,
                submit,
                instagram_source=instagram_source,
                news_collector=news_collector,
                roster_watch=roster_watch,
            )
        except Exception as exc:
            log.exception("gamewatch: follow-ups of event %s failed", event.event_id)
            out.setdefault("fehler", []).append(f"follow-up {event.event_id}: {exc}")
    return out


def _local(dt: datetime | None) -> str | None:
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M") if dt else None


def status(s: Session, settings: Settings) -> list[dict[str, Any]]:
    """Every game of the watched events: start (Swiss time), result, when the final was stored, the last re-check,
    the follow-up and what it did (CLI `klugscheisser gamewatch --status`)."""
    rows = []
    jobs = {
        (j.params or {}).get("spiel"): j
        for j in s.scalars(select(m.Job).where(m.Job.kind == JOB_KIND).order_by(m.Job.id))
    }
    from klugscheisser.jobs.game_rosters import STATE as ROSTERS

    sheets = (r.state or {}) if (r := s.get(m.SourceState, ROSTERS)) is not None else {}
    for event in watch_events(settings):
        for g in event_games(s, event, span=None):
            job = jobs.get(g.key)
            kader = {
                c: {k: v[k] for k in ("dazu", "unklar", "nicht_eingesetzt") if v.get(k)}
                for c, v in (sheets.get(g.key) or {}).items()
                if isinstance(v, dict)
            }
            rows.append(
                {
                    "event": event.event_id,
                    "spiel": g.name,
                    "key": g.key,
                    "anpfiff": _local(g.starts_at),
                    "status": g.status,
                    "resultat": g.score,
                    "final_gespeichert": _local(g.final_at),
                    "spielblatt": ("vollständig" if g.complete else "unvollständig") if g.has_sheet else None,
                    "nachkontrolle": _local(g.rechecked_at),
                    "follow_up": _local(g.followup_at),
                    "follow_up_status": job.status if job else None,
                    "follow_up_ergebnis": summarize(job.result) if job and job.result else None,
                    "kader_spielbericht": {c: v for c, v in kader.items() if v} or None,
                }
            )
    return rows


def start_game_watch(settings: Settings | None = None, tick_s: float = TICK_S) -> threading.Thread:
    """Thread `game-watch` (started by `serve`): `watch_pass` per scheduled project every `tick_s`; follow-ups run
    on the refresh pool (jobs/refresh.py executor), so polls of other games go on meanwhile."""
    from klugscheisser import projects
    from klugscheisser.config import get_settings
    from klugscheisser.jobs.nightly import scheduled_projects

    stop = threading.Event()

    def loop() -> None:
        from klugscheisser.jobs.refresh import executor

        while not stop.is_set():
            for p in scheduled_projects():
                with projects.use(p):
                    try:
                        local = get_settings()
                        from klugscheisser.sports import current_spec

                        # the game watch reads the IIHF feed: ice hockey only (floorball: nightly results gap)
                        if local.game_watch and "gamewatch" not in current_spec().nightly_skip:
                            watch_pass(local, submit=executor().submit)
                    except Exception:
                        log.exception("gamewatch failed (%s)", p.key)
            stop.wait(tick_s)

    thread = threading.Thread(target=loop, name="game-watch", daemon=True)
    thread.start()
    return thread


__all__ = [
    "Game",
    "event_games",
    "followup_due",
    "needs_poll",
    "run_followup",
    "sheet_complete",
    "start_followups",
    "start_game_watch",
    "status",
    "watch_event",
    "watch_pass",
]
