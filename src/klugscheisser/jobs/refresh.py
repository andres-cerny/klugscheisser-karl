"""On-demand refresh: fast sources synchronously, Instagram as a background job.

TTL cache per (source, target) so repeated questions don't trigger repeated paid scrapes.
Jobs live in the `jobs` table and run in a small in-process thread pool (no Redis needed).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import session_scope
from klugscheisser.projects import ContextPool, wording

log = logging.getLogger(__name__)
_executor: ContextPool | None = None


def executor() -> ContextPool:
    global _executor
    if _executor is None:
        _executor = ContextPool(max_workers=2, thread_name_prefix="refresh")
    return _executor


# --- TTL cache ------------------------------------------------------------------------------------


def is_fresh(s: Session, source: str, target: str, ttl_s: int) -> datetime | None:
    entry = s.get(m.CacheEntry, (source, target))
    if entry and entry.fetched_at > datetime.now(UTC) - timedelta(seconds=ttl_s):
        return entry.fetched_at
    return None


def touch(s: Session, source: str, target: str) -> None:
    entry = s.get(m.CacheEntry, (source, target))
    if entry is None:
        s.add(m.CacheEntry(source=source, target=target, fetched_at=datetime.now(UTC)))
    else:
        entry.fetched_at = datetime.now(UTC)
    s.flush()


# --- target resolution ----------------------------------------------------------------------------


def resolve_targets(s: Session, target: str) -> tuple[str, str, list[m.Athlete]]:
    """Returns (cache key, label, athletes) for an athlete, team or event name."""
    from klugscheisser.mcp_server.tools import NotFound, resolve_event, resolve_team
    from klugscheisser.retrieval.search import athlete_team_filter

    a = repo.resolve_athlete(s, target)
    if a is not None:
        return f"athlete:{a.id}", a.full_name, [a]
    try:
        team = resolve_team(s, target)
        ids = athlete_team_filter(s, team).athlete_ids or []
        return f"team:{team.id}", team.name, [s.get(m.Athlete, i) for i in ids]
    except NotFound:
        ev = resolve_event(s, target)
        ids = list(s.scalars(select(m.EventEntry.athlete_id).where(m.EventEntry.event_id == ev.id)))
        return f"event:{ev.id}", ev.name, [s.get(m.Athlete, i) for i in ids]


# --- jobs -----------------------------------------------------------------------------------------


def _run_instagram_job(job_id: int, athlete_ids: list[int], settings: Settings, cache_key: str) -> None:
    from klugscheisser.ingest.runner import run_instagram

    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.started_at = "running", datetime.now(UTC)
    try:
        with session_scope() as s:
            athletes = [s.get(m.Athlete, i) for i in athlete_ids]
            report = run_instagram(s, settings, athletes)
            touch(s, "instagram", cache_key)
            result = asdict(report)
        status, error = "done", None
    except Exception as exc:  # report the failure through refresh_status
        log.exception("instagram refresh job %s failed", job_id)
        result, status, error = None, "failed", str(exc)
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.result, job.error, job.finished_at = status, result, error, datetime.now(UTC)


def start_instagram_job(
    s: Session, settings: Settings, cache_key: str, athletes: list[m.Athlete], submit: Callable | None = None
) -> m.Job:
    running = s.scalar(
        select(m.Job).where(
            m.Job.kind == "instagram_refresh",
            m.Job.status.in_(["queued", "running"]),
            m.Job.params["target"].as_string() == cache_key,
        )
    )
    if running is not None:
        return running
    job = m.Job(
        kind="instagram_refresh", params={"target": cache_key, "athlete_ids": [a.id for a in athletes]}
    )
    s.add(job)
    s.flush()
    s.commit()  # the worker thread must see the row
    (submit or executor().submit)(_run_instagram_job, job.id, [a.id for a in athletes], settings, cache_key)
    return job


def recover_stale_jobs() -> int:
    """On server start: jobs left 'running' by a previous process can't finish anymore."""
    with session_scope() as s:
        stale = list(s.scalars(select(m.Job).where(m.Job.status.in_(["queued", "running"]))))
        for job in stale:
            job.status, job.error, job.finished_at = "failed", "Server neu gestartet", datetime.now(UTC)
        return len(stale)


# --- the two tools --------------------------------------------------------------------------------


def refresh(
    s: Session, settings: Settings, target: str, submit: Callable | None = None, news_collector=None
) -> dict[str, Any]:
    from klugscheisser.collectors.rss import RssCollector
    from klugscheisser.domain.labels import datetime_de
    from klugscheisser.ingest.news import ingest_news
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.mcp_server.present import envelope, item_compact

    started = datetime.now(UTC)
    cache_key, label, athletes = resolve_targets(s, target)
    out: dict[str, Any] = {"ziel": label}

    # fast path: news (shared feed, one cache entry)
    if fresh := is_fresh(s, "news", "all", settings.ttl_news):
        out["news"] = f"aktuell (zuletzt geprüft {datetime_de(fresh)})"
    else:
        report = ingest_news(build_context(s, settings), news_collector or RssCollector())
        touch(s, "news", "all")
        out["news"] = f"geprüft: {report.items_new} neu" + (
            f", Fehler: {report.errors}" if report.errors else ""
        )

    # slow path: Instagram in the background
    if fresh := is_fresh(s, "instagram", cache_key, settings.ttl_instagram):
        out["instagram"] = f"aktuell (zuletzt abgerufen {datetime_de(fresh)}); kein neuer Abruf nötig."
    elif not athletes:
        out["instagram"] = f"Keine {wording().players} mit bekanntem Account."
    else:
        job = start_instagram_job(s, settings, cache_key, athletes, submit=submit)
        out["instagram"] = "Instagram wird aktualisiert, in ca. 1 Minute erneut fragen (refresh_status)."
        out["job_id"] = job.id

    ids = [a.id for a in athletes]
    new = (
        s.scalars(
            select(m.Item)
            .join(m.ItemAthlete)
            .where(m.ItemAthlete.athlete_id.in_(ids), m.Item.scraped_at >= started)
            .distinct()
        ).all()
        if ids
        else []
    )
    out["neu_seit_aufruf"] = [item_compact(i) for i in new]
    return envelope(s, athlete_ids=ids, **out)


def refresh_status(s: Session, job_id: int, wait_s: float | None = None) -> dict[str, Any]:
    from klugscheisser.config import get_settings
    from klugscheisser.domain.labels import datetime_de
    from klugscheisser.jobs.freshness import wait_for_job
    from klugscheisser.mcp_server.present import envelope, item_compact

    job = s.get(m.Job, job_id)
    if job is None:
        return envelope(s, fehler=f"Kein Auftrag {job_id}.")
    if job.status in ("queued", "running") and wait_s != 0:
        wait_for_job(s, job_id, get_settings().status_wait_s if wait_s is None else wait_s)
        job = s.get(m.Job, job_id)
    out: dict[str, Any] = {
        "job_id": job.id,
        "status": {"queued": "wartet", "running": "läuft", "done": "fertig", "failed": "fehlgeschlagen"}[
            job.status
        ],
    }
    if job.status in ("queued", "running"):
        out["hinweis"] = "Noch nicht fertig, in ca. 30 Sekunden erneut fragen."
    if job.error:
        out["fehler"] = job.error
    if job.status == "done":
        r = job.result or {}
        out["ergebnis"] = {
            "neue_posts": r.get("items_new", 0),
            "accounts": r.get("handles", 0),
            "kosten_usd": round(r.get("cost_usd", 0.0), 3),
        }
        if r.get("skipped_budget"):
            out["hinweis"] = "Tagesbudget für Instagram erreicht; einige Accounts wurden nicht abgerufen."
        out["fertig_um"] = datetime_de(job.finished_at)
        ids = job.params.get("athlete_ids", [])
        new = (
            s.scalars(
                select(m.Item)
                .join(m.ItemAthlete)
                .where(
                    m.ItemAthlete.athlete_id.in_(ids),
                    m.Item.scraped_at >= job.started_at,
                    m.Item.platform == "instagram",
                )
                .distinct()
            ).all()
            if ids and job.started_at
            else []
        )
        out["neue_eintraege"] = [item_compact(i) for i in new]
    return envelope(s, **out)
