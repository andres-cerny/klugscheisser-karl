"""Drain the media queue in the background: analyse, store the description, re-classify, re-embed.

Wiring (server start):  `start_background_worker(get_settings())` -> daemon thread.
One-off (cron / CLI):   `process_media_queue(session, settings, get_media_analyzer(settings))`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.interfaces import MediaAnalysis, MediaAnalyzer, MediaRef
from klugscheisser.media.analyzer import MediaRateLimited

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@dataclass
class MediaReport:
    analyzed: int = 0
    failed: int = 0  # attempt counted, row kept for a retry
    dropped: int = 0  # gave up after MAX_ATTEMPTS
    limit_reached: bool = False  # daily MEDIA_DAILY_LIMIT hit
    rate_limited: bool = False  # model kept answering 429/503; batch stopped
    errors: list[str] = field(default_factory=list)


def ledger_source(analyzer: MediaAnalyzer) -> str:
    return f"llm:{analyzer.name}"


def analyses_today(session: Session, source: str) -> int:
    since = datetime.now(UTC) - timedelta(hours=24)
    return int(
        session.scalar(
            select(func.coalesce(func.sum(m.CostLedger.units), 0)).where(
                m.CostLedger.source == source, m.CostLedger.created_at >= since
            )
        )
    )


def describe(a: MediaAnalysis) -> str | None:
    """image_description: the model's description plus visible text, if any."""
    parts = []
    if a.description:
        parts.append(a.description.strip())
    if a.visible_text and a.visible_text.strip() not in (a.description or ""):
        parts.append(f"Sichtbarer Text: «{a.visible_text.strip()}»")
    return " ".join(parts) or None


def _set_media(item: m.Item, media: dict) -> None:
    item.raw = {**(item.raw or {}), "media": media}  # new dict, so SQLAlchemy sees the change
    item.media_pending = False


def apply_analysis(session: Session, item: m.Item, a: MediaAnalysis, classifier, embedder) -> None:
    from klugscheisser.ingest.store import apply_classification, classify_input, embed_item

    desc = describe(a)
    if desc:
        item.image_description = desc
    _set_media(
        item,
        {
            "spoken_quotes": a.spoken_quotes,
            "visible_text": a.visible_text,
            "model": a.model,
            "analyzed": a.analyzed,
        },
    )
    session.flush()
    if item.text is None and item.text_purged_at is not None:
        return  # purged meanwhile; nothing to re-classify
    apply_classification(session, item, classifier.classify(classify_input(item)))
    embed_item(session, embedder, item)


def process_media_queue(
    session: Session,
    settings: Settings,
    analyzer: MediaAnalyzer,
    limit: int = 20,
    classifier=None,
    embedder=None,
    pause_s: float | None = None,
    shard: tuple[int, int] | None = None,
) -> MediaReport:
    """Analyse up to `limit` queued posts, oldest first. Commits after every item so a crash or a
    rate limit never loses finished work. `classifier`/`embedder` default to build_context(...)."""
    if classifier is None or (embedder is None and settings.embeddings_on):
        from klugscheisser.ingest.runner import build_context

        ctx = build_context(session, settings)
        classifier, embedder = classifier or ctx.classifier, embedder or ctx.embedder
    pause = settings.media_min_interval_s if pause_s is None else pause_s
    source = ledger_source(analyzer)
    report = MediaReport()
    rows = list(
        session.scalars(
            select(m.MediaQueue)
            .where(m.MediaQueue.attempts < MAX_ATTEMPTS)
            .where(m.MediaQueue.item_id % (shard[1] if shard else 1) == (shard[0] if shard else 0))
            .order_by(m.MediaQueue.created_at, m.MediaQueue.item_id)
            .limit(limit)
        )
    )
    if hasattr(analyzer, "analyze_batch"):  # v15: several posts per model call (Claude CLI)
        return _process_batches(session, settings, analyzer, rows, classifier, embedder, source, report)
    called = False
    for row in rows:
        if analyses_today(session, source) >= settings.media_daily_limit:
            report.limit_reached = True
            log.info("media analysis: daily limit %s reached", settings.media_daily_limit)
            break
        item = session.get(m.Item, row.item_id)
        if item is None:
            session.delete(row)
            session.commit()
            continue
        if called and pause > 0:
            time.sleep(pause)  # free tier: stay under the requests-per-minute limit
        refs = [MediaRef(kind=x["kind"], url=x["url"], duration_s=x.get("duration_s")) for x in row.media]
        try:
            analysis = analyzer.analyze(refs, item.text or "")
        except MediaRateLimited as exc:
            report.rate_limited = True
            report.errors.append(str(exc))
            log.warning("media analysis rate limited, stopping batch: %s", exc)
            session.rollback()
            break
        except Exception as exc:
            called = True
            session.rollback()
            report.errors.append(f"item {row.item_id}: {exc}")
            log.warning("media analysis failed for item %s: %s", row.item_id, exc)
            row = session.get(m.MediaQueue, row.item_id)
            row.attempts += 1
            if row.attempts >= MAX_ATTEMPTS:
                _set_media(session.get(m.Item, row.item_id), {"error": str(exc)[:500]})
                session.delete(row)
                report.dropped += 1
            else:
                report.failed += 1
            session.commit()
            continue
        called = bool(analysis.model)
        if called:
            from klugscheisser.ingest import costs

            if (
                getattr(analyzer, "name", "") == "claude"
            ):  # v17: Claude API (images only) at the Claude prices
                usd = (
                    analysis.input_tokens * settings.anthropic_input_usd_per_m
                    + analysis.output_tokens * settings.anthropic_output_usd_per_m
                ) / 1e6
            else:
                usd = (
                    analysis.input_tokens * settings.gemini_media_input_usd_per_m
                    + analysis.output_tokens * settings.gemini_media_output_usd_per_m
                ) / 1e6
            costs.record(
                session,
                source,
                1,
                usd,
                f"media item {item.id}: {', '.join(analysis.analyzed)} "
                f"({analysis.input_tokens} in / {analysis.output_tokens} out)",
            )
        apply_analysis(session, item, analysis, classifier, embedder)
        session.delete(row)
        session.commit()
        report.analyzed += 1
    return report


def _process_batches(session, settings, analyzer, rows, classifier, embedder, source, report) -> MediaReport:
    """Like the per-item loop, but `analyzer.batch_size` posts per call. A used-up subscription window
    (MediaRateLimited) stops the run; the rest stays queued for the next run."""
    from klugscheisser.ingest import costs

    size = analyzer.batch_size
    for start in range(0, len(rows), size):
        left = settings.media_daily_limit - analyses_today(session, source)
        if left <= 0:
            report.limit_reached = True
            log.info("media analysis: daily limit %s reached", settings.media_daily_limit)
            break
        chunk = []
        for row in rows[start : start + min(size, left)]:
            item = session.get(m.Item, row.item_id)
            if item is None:
                session.delete(row)
                continue
            refs = [MediaRef(kind=x["kind"], url=x["url"], duration_s=x.get("duration_s")) for x in row.media]
            chunk.append((row.item_id, item, refs))
        session.commit()
        if not chunk:
            continue
        try:
            results = analyzer.analyze_batch([(refs, item.text or "") for _, item, refs in chunk])
        except MediaRateLimited as exc:
            report.rate_limited = True
            report.errors.append(str(exc))
            log.warning("media analysis rate limited, stopping: %s", exc)
            session.rollback()
            break
        except Exception as exc:
            session.rollback()
            report.errors.append(f"batch {[i for i, _, _ in chunk]}: {exc}")
            log.warning("media batch failed: %s", exc)
            for item_id, _, _ in chunk:
                row = session.get(m.MediaQueue, item_id)
                row.attempts += 1
                if row.attempts >= MAX_ATTEMPTS:
                    _set_media(session.get(m.Item, item_id), {"error": str(exc)[:500]})
                    session.delete(row)
                    report.dropped += 1
                else:
                    report.failed += 1
            session.commit()
            continue
        for (item_id, item, _refs), analysis in zip(chunk, results, strict=True):
            if analysis.model:
                costs.record(  # subscription: no money, counted for the daily limit
                    session,
                    source,
                    1,
                    0.0,
                    f"media item {item.id}: {', '.join(analysis.analyzed)} (Claude-Abo)",
                )
            apply_analysis(session, item, analysis, classifier, embedder)
            session.delete(session.get(m.MediaQueue, item_id))
            session.commit()
            report.analyzed += 1
    return report


def purge_stale(session: Session, hours: int = 48) -> int:
    """Drop queue rows older than `hours` (CDN URLs have expired) and clear media_pending."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    rows = list(session.scalars(select(m.MediaQueue).where(m.MediaQueue.created_at < cutoff)))
    for row in rows:
        item = session.get(m.Item, row.item_id)
        if item is not None:
            item.media_pending = False
            if not (item.raw or {}).get("media"):
                _set_media(item, {"error": "nicht analysiert (Medien-Link abgelaufen)"})
        session.delete(row)
    session.flush()
    return len(rows)


def _rate_limit_base_s(analyzer: MediaAnalyzer) -> float:
    """First pause after a rate limit: 30 min when no model is left or the Claude subscription window is used up,
    else 60 s (a short 429 burst)."""
    if getattr(analyzer, "name", "") == "claude-cli":
        return 1800.0
    models = getattr(analyzer, "available_models", None)
    return 1800.0 if models is not None and not models() else 60.0


class RateLimitBackoff:
    """Exponential pause after rate limits in a row: base, 2x base, 4x base … up to `max_s`; reset on success."""

    def __init__(self, max_s: float) -> None:
        self.max_s = max(0.0, float(max_s))
        self.hits = 0
        self.until = 0.0  # time.monotonic() before which no pass runs

    def hit(self, base_s: float, now: float | None = None) -> float:
        self.hits += 1
        delay = min(base_s * 2 ** (self.hits - 1), max(self.max_s, base_s))
        self.until = (time.monotonic() if now is None else now) + delay
        return delay

    def reset(self) -> None:
        self.hits, self.until = 0, 0.0

    def paused(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) < self.until


def start_background_worker(
    settings: Settings,
    analyzer: MediaAnalyzer | None = None,
    idle_s: float = 10.0,
    stop: threading.Event | None = None,
) -> threading.Thread | None:
    """Run process_media_queue in a daemon thread until `stop` is set. Returns None when media
    analysis is off (no Gemini/Claude key). Nothing happens at import time."""
    from klugscheisser.media.analyzer import get_media_analyzer

    analyzer = analyzer or get_media_analyzer(settings)
    if analyzer is None:
        log.info("media analysis off (no MEDIA_PROVIDER / key)")
        return None
    stop = stop or threading.Event()

    def one_round(state: dict) -> tuple[float, MediaReport | None]:
        """One pass over the queue of the current project; returns the wait before its next pass + the report."""
        from klugscheisser.db.session import get_engine
        from klugscheisser.ingest.runner import build_context

        wait, r = idle_s, None
        try:
            with Session(get_engine()) as s:
                if "classifier" not in state:
                    ctx = build_context(s, settings)
                    state["classifier"], state["embedder"] = ctx.classifier, ctx.embedder
                    s.commit()
                if time.monotonic() - state.get("last_purge", 0.0) > 3600:
                    if n := purge_stale(s, settings.media_stale_hours):
                        log.info("media queue: purged %s stale rows", n)
                    s.commit()
                    state["last_purge"] = time.monotonic()
                r = process_media_queue(
                    s, settings, analyzer, classifier=state["classifier"], embedder=state["embedder"]
                )
            if r.analyzed or r.failed or r.dropped:
                log.info("media worker: %s", r)
            if r.analyzed:
                wait = 0.0  # more may be waiting; the per-item pause keeps the request rate low
            if r.limit_reached:
                wait = 600.0
        except Exception:
            log.exception("media worker iteration failed")
            wait = 60.0
        return wait, r

    backoff = RateLimitBackoff(settings.media_backoff_max_s)

    def loop() -> None:
        from klugscheisser import projects

        states: dict[str, dict] = {}
        next_at: dict[str, float] = {}
        while not stop.is_set():
            # v7: every active project (also "Allgemein": its media come from on-demand adds). The analyzer (and its
            # rate limit) is shared, so a rate limit pauses all projects; each project keeps its own next pass time
            # (2026-10-25: before, the other project's 10 s idle wait cut the 30 min pause short: 6,032 retries).
            for p in projects.active_projects():
                now = time.monotonic()
                if stop.is_set() or backoff.paused(now):
                    break
                if next_at.get(p.key, 0.0) > now:
                    continue
                with projects.use(p):
                    wait, r = one_round(states.setdefault(p.key, {}))
                next_at[p.key] = time.monotonic() + wait
                if r is not None and r.rate_limited:
                    delay = backoff.hit(_rate_limit_base_s(analyzer))
                    log.warning(
                        "media worker: rate limited (%s in a row), pausing %.0f s", backoff.hits, delay
                    )
                elif r is not None and r.analyzed:
                    backoff.reset()
            now = time.monotonic()
            due = min([*next_at.values(), now + idle_s])
            stop.wait(max(0.5, max(due, backoff.until) - now))

    t = threading.Thread(target=loop, name="media-worker", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    return t
