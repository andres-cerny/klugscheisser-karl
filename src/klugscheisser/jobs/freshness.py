"""On-demand freshness: before answering, update exactly the athletes the question is about.

- An athlete counts as fresh when her Instagram was fetched today (Europe/Zurich) — at most one automatic
  update per athlete per day; more only via the explicit `refresh` tool.
- The update (Instagram + news + results) runs as a job; the tool waits up to AUTO_REFRESH_WAIT_S seconds.
  If it is done in time the answer contains the new data; otherwise the answer says so and gives a job_id
  for refresh_status (which itself waits up to STATUS_WAIT_S).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.domain.labels import TZ
from klugscheisser.handles.service import usable_handles

log = logging.getLogger(__name__)


# extra sources run inside the update job, e.g. results collectors: fn(session, settings, athletes) -> dict
def _default_extra_sources() -> list[Callable[[Session, Settings, list[m.Athlete]], dict]]:
    from klugscheisser.ingest.results import results_for_update

    return [results_for_update]


EXTRA_SOURCES: list[Callable[[Session, Settings, list[m.Athlete]], dict]] = _default_extra_sources()


def _default_slow_sources() -> list[Callable[[Session, Settings, list[m.Athlete]], dict]]:
    from klugscheisser.ingest.profiles import profiles_for_update
    from klugscheisser.ingest.x import x_for_update

    return [x_for_update, profiles_for_update]


# slow sources (profiles: college sites ask for 30 s crawl delays) run only in the background update job
SLOW_SOURCES: list[Callable[[Session, Settings, list[m.Athlete]], dict]] = _default_slow_sources()


@dataclass
class Freshness:
    status: str  # "aktuell" | "aktualisiert" | "läuft" | "fehlgeschlagen" | "aus"
    job_id: int | None = None
    hinweis: str | None = None

    def out(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def start_of_today() -> datetime:
    return datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def stale_athletes(athletes: list[m.Athlete]) -> list[m.Athlete]:
    today = start_of_today()
    stale = []
    for a in athletes:
        handles = [h for h in usable_handles(a) if not (h.profile or {}).get("private")]
        if handles and any(h.last_scraped_at is None or h.last_scraped_at < today for h in handles):
            stale.append(a)
    return stale


def _run_update_job(job_id: int, athlete_ids: list[int], settings: Settings) -> None:
    from klugscheisser.collectors.rss import RssCollector
    from klugscheisser.ingest.news import ingest_news
    from klugscheisser.ingest.runner import build_context, run_instagram
    from klugscheisser.jobs.refresh import is_fresh, touch

    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.started_at = "running", datetime.now(UTC)
    result: dict[str, Any] = {}
    status, error = "done", None
    try:
        with session_scope() as s:
            athletes = [s.get(m.Athlete, i) for i in athlete_ids]
            result["instagram"] = asdict(run_instagram(s, settings, athletes))
        with session_scope() as s:
            if not is_fresh(s, "news", "all", settings.ttl_news):
                result["news"] = asdict(ingest_news(build_context(s, settings), RssCollector()))
                touch(s, "news", "all")
        for extra in [*EXTRA_SOURCES, *SLOW_SOURCES]:
            with session_scope() as s:
                athletes = [s.get(m.Athlete, i) for i in athlete_ids]
                result[getattr(extra, "__name__", "extra")] = extra(s, settings, athletes)
    except Exception as exc:
        log.exception("update job %s failed", job_id)
        status, error = "failed", str(exc)
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.result, job.error, job.finished_at = status, result, error, datetime.now(UTC)


def refresh_fast_sources(s: Session, settings: Settings, athletes: list[m.Athlete]) -> None:
    """News (TTL 5 min) and results (TTL 1 min live / 1 h) in the caller's request; errors never block an answer."""
    from klugscheisser.collectors.rss import RssCollector
    from klugscheisser.ingest.news import ingest_news
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.jobs.refresh import is_fresh, touch

    try:
        if not is_fresh(s, "news", "all", settings.ttl_news):
            ingest_news(build_context(s, settings), RssCollector())
            touch(s, "news", "all")
        for extra in EXTRA_SOURCES:
            extra(s, settings, athletes)
        s.commit()
    except Exception as exc:
        s.rollback()
        log.warning("fast source refresh failed: %s", exc)


def _fast_sources_job(athlete_ids: list[int], settings: Settings) -> None:
    with session_scope() as s:
        refresh_fast_sources(s, settings, [s.get(m.Athlete, i) for i in athlete_ids])


def _find_running(s: Session, athlete_ids: list[int]) -> m.Job | None:
    running = s.scalars(
        select(m.Job).where(m.Job.kind == "auto_update", m.Job.status.in_(["queued", "running"]))
    )
    wanted = set(athlete_ids)
    return next((j for j in running if wanted <= set(j.params.get("athlete_ids", []))), None)


def ensure_fresh(
    s: Session,
    settings: Settings,
    athletes: list[m.Athlete],
    wait_s: float | None = None,
    submit: Callable[..., Future | None] | None = None,
    fast_sources_now: bool = True,
) -> Freshness:
    """Update stale athletes (once per day) and wait up to `wait_s` seconds for the result.

    `fast_sources_now=False` (answers, v5): news/results are refreshed in the background, not in the request."""
    import time

    from klugscheisser import calllog

    started = time.monotonic()
    fr = _ensure_fresh(s, settings, athletes, wait_s, submit, fast_sources_now)
    calllog.note("refresh", f"{fr.status}:{time.monotonic() - started:.1f}s")  # MCP call log line
    return fr


def _ensure_fresh(
    s: Session,
    settings: Settings,
    athletes: list[m.Athlete],
    wait_s: float | None,
    submit: Callable[..., Future | None] | None,
    fast_sources_now: bool,
) -> Freshness:
    if not settings.auto_refresh:
        return Freshness("aus")
    stale = stale_athletes(athletes)
    if not stale:
        # Instagram is from today; news and results are cheap and change fast: refresh them on their own TTL
        if fast_sources_now:
            refresh_fast_sources(s, settings, athletes)
        else:
            from klugscheisser.jobs.refresh import executor

            (submit or executor().submit)(_fast_sources_job, [a.id for a in athletes], settings)
        return Freshness("aktuell")
    ids = [a.id for a in stale]
    job = _find_running(s, ids)
    future: Future | None = None
    if job is None:
        job = m.Job(kind="auto_update", params={"athlete_ids": ids})
        s.add(job)
        s.flush()
        s.commit()  # the worker thread must see the row
        from klugscheisser.jobs.refresh import executor

        future = (submit or executor().submit)(_run_update_job, job.id, ids, settings)
    wait = settings.auto_refresh_wait_s if wait_s is None else wait_s
    done = wait_for_job(s, job.id, wait, future)
    if done is None:
        return Freshness(
            "läuft",
            job.id,
            f"Aktualisierung läuft noch: refresh_status(job_id={job.id}) aufrufen, "
            f"es wartet bis zu {settings.status_wait_s:.0f} Sekunden auf das Ergebnis.",
        )
    if done.status == "failed":
        return Freshness(
            "fehlgeschlagen",
            job.id,
            f"Aktualisierung fehlgeschlagen: {done.error}. Antwort mit bestehenden Daten.",
        )
    return Freshness("aktualisiert", job.id)


def wait_for_job(s: Session, job_id: int, wait_s: float, future: Future | None = None) -> m.Job | None:
    """Block up to wait_s for the job; returns the finished job or None. Refreshes the caller's session."""
    import time

    if future is not None:
        try:
            future.result(timeout=max(wait_s, 0))
        except FutureTimeout:
            return None
        except Exception:  # job function logs and stores its own error
            pass
    else:
        deadline = time.monotonic() + wait_s
        while True:
            s.expire_all()
            job = s.get(m.Job, job_id)
            if job is None or job.status in ("done", "failed") or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
    s.expire_all()  # see rows written by the job's own sessions
    job = s.get(m.Job, job_id)
    return job if job is not None and job.status in ("done", "failed") else None
