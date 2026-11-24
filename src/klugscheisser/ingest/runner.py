"""Ingest entry points used by the CLI (cron) and by refresh jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from klugscheisser.classify.router import build_classifier
from klugscheisser.collectors.instagram.sources import get_instagram_source
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.matcher import AthleteMatcher
from klugscheisser.embed.providers import get_embedder
from klugscheisser.ingest.instagram import InstagramReport, ingest_instagram
from klugscheisser.ingest.store import IngestContext
from klugscheisser.llm.providers import get_llm

UPCOMING_WINDOW = timedelta(days=30)


def build_context(session: Session, settings: Settings) -> IngestContext:
    from klugscheisser.classify.jev import JevClient

    llm = get_llm(settings)
    features = settings.features
    if session.scalar(select(func.count()).select_from(m.Account)) == 0:
        from klugscheisser.domain.source_kind import load_accounts

        load_accounts(session)
    jev = JevClient(settings) if features["jev"] else None
    return IngestContext(
        session=session,
        settings=settings,
        classifier=build_classifier(settings, llm),
        embedder=get_embedder(settings) if settings.embeddings_on else None,  # off: no vectors at all
        llm=llm,
        matcher=AthleteMatcher(session, jev_client=jev),
        jev_client=jev,
        media_analysis=settings.media_provider == "claude-cli" or features["gemini"] or features["claude"],
    )


def athletes_for_tier(session: Session, tier: str) -> list[m.Athlete]:
    """daily: watchlist + anyone entered in an event that is running or starts within 30 days;
    weekly: everyone with a usable handle."""
    with_handle = select(m.Handle.athlete_id).where(m.Handle.status.in_(["verified", "wahrscheinlich"]))
    if tier == "weekly":
        return list(session.scalars(select(m.Athlete).where(m.Athlete.id.in_(with_handle))))
    now = datetime.now(UTC)
    upcoming = (
        select(m.EventEntry.athlete_id)
        .join(m.Event)
        .where(
            or_(m.Event.ends_at >= now, m.Event.ends_at.is_(None)),
            or_(m.Event.starts_at <= now + UPCOMING_WINDOW, m.Event.starts_at.is_(None)),
        )
    )
    return list(
        session.scalars(
            select(m.Athlete).where(
                m.Athlete.id.in_(with_handle), or_(m.Athlete.to_watch.is_(True), m.Athlete.id.in_(upcoming))
            )
        )
    )


def athletes_for_event(session: Session, event: m.Event) -> list[m.Athlete]:
    return list(
        session.scalars(select(m.Athlete).join(m.EventEntry).where(m.EventEntry.event_id == event.id))
    )


def run_instagram(
    session: Session, settings: Settings, athletes: list[m.Athlete], max_posts: int = 12
) -> InstagramReport:
    ctx = build_context(session, settings)
    return ingest_instagram(ctx, get_instagram_source(settings), athletes, max_posts=max_posts)


def purge_irrelevant(session: Session, days: int) -> int:
    """Data minimisation: drop text of items classified irrelevant (and not flagged) after N days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    irrelevant = select(m.Classification.item_id).where(
        m.Classification.relevant.is_(False), m.Classification.vorsicht.is_(False)
    )
    result = session.execute(
        update(m.Item)
        .where(m.Item.id.in_(irrelevant), m.Item.scraped_at < cutoff, m.Item.text_purged_at.is_(None))
        .values(text=None, image_description=None, raw=None, text_purged_at=datetime.now(UTC))
    )
    purged = select(m.Item.id).where(m.Item.text_purged_at.is_not(None))
    session.execute(m.Embedding.__table__.delete().where(m.Embedding.item_id.in_(purged)))
    return result.rowcount or 0


def reclassify_all(
    session: Session, settings: Settings, only_classifier: str | None = None
) -> dict[str, int]:
    """Re-run the classifier router on stored items (e.g. after a better classifier became available)."""
    from klugscheisser.ingest.store import apply_classification, classify_input

    ctx = build_context(session, settings)
    counts: dict[str, int] = {"items": 0, "changed_vorsicht": 0}
    q = select(m.Item).join(m.Classification).where(m.Item.text.is_not(None))
    if only_classifier:
        q = q.where(m.Classification.classifier == only_classifier)
    from klugscheisser.domain.source_kind import kind_for

    own = set(ctx.matcher.by_handle)
    for item in session.scalars(q):
        item.source_kind = kind_for(
            session,
            platform=item.platform,
            source_type=item.source_type,
            author=item.author,
            own_handles=own,
            feed_kind=None,
            author_full_name=(item.raw or {}).get("ownerFullName"),
            sample_text=item.text or "",
            jev_client=ctx.jev_client,
        )
        before = item.classification.vorsicht
        r = ctx.classifier.classify(classify_input(item))
        apply_classification(session, item, r)
        counts["changed_vorsicht"] += before != r.vorsicht
        counts["items"] += 1
        counts[r.classifier] = counts.get(r.classifier, 0) + 1
    return counts
