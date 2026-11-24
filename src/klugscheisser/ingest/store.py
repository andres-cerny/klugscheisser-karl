"""Store one collected item: dedup, link athletes, classify, embed, queue media. Shared by all collectors."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.classify.base import ClassificationResult, ClassifyInput
from klugscheisser.classify.router import ClassifierRouter
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.matcher import AthleteMatcher, Link
from klugscheisser.embed.providers import Embedder
from klugscheisser.interfaces import MediaRef
from klugscheisser.llm.providers import LlmProvider

log = logging.getLogger(__name__)


@dataclass
class ItemDraft:
    source_type: str
    platform: str
    url: str
    text: str
    source_name: str | None = None
    external_id: str | None = None
    author: str | None = None
    title: str | None = None
    published_at: datetime | None = None
    image_description: str | None = None
    media: list[MediaRef] = field(default_factory=list)  # transient; goes to the media queue only
    media_type: str | None = None
    tagged: list[str] = field(default_factory=list)
    paid_partnership: bool = False
    raw: dict | None = None
    source_kind: str | None = None


@dataclass
class IngestContext:
    session: Session
    settings: Settings
    classifier: ClassifierRouter
    embedder: Embedder | None  # None: no vectors (Settings.embeddings_on off, or a bulk backfill)
    llm: LlmProvider | None
    matcher: AthleteMatcher
    jev_client: object | None = None  # for account-kind questions; None when Jev is off
    media_analysis: bool = False  # queue media for background analysis (Gemini/Claude configured)
    team_matcher: object | None = None  # domain.team_matcher.TeamMatcher, built on first use
    skip_facts: bool = False  # bulk backfills: facts later (`facts-backfill`)


@dataclass
class StoreResult:
    item: m.Item
    created: bool
    changed: bool


def content_hash(text: str, title: str | None = None) -> str:
    return hashlib.sha256(f"{title or ''}\n{text}".strip().encode()).hexdigest()


def classify_input(item: m.Item) -> ClassifyInput:
    media = (item.raw or {}).get("media") or {}
    image = " ".join(
        filter(
            None,
            [item.image_description, media.get("visible_text"), " ".join(media.get("spoken_quotes") or [])],
        )
    )
    return ClassifyInput(
        text=item.text or "",
        source_type=item.source_type,
        author=item.author,
        athlete_names=tuple(a.athlete.full_name for a in item.athletes if a.athlete),
        paid_partnership=bool((item.raw or {}).get("paidPartnership")),
        image_description=image or None,
    )


def apply_classification(session: Session, item: m.Item, r: ClassificationResult) -> None:
    c = item.classification or m.Classification(item_id=item.id)
    c.relevant_p, c.relevant, c.category, c.category_p = r.relevant_p, r.relevant, r.category, r.category_p
    c.sensitive_p, c.vorsicht, c.newsworthiness, c.storyline = (
        r.sensitive_p,
        r.vorsicht,
        r.newsworthiness,
        r.storyline,
    )
    c.mood = r.mood
    c.topics = r.topics or {}
    c.classifier, c.classifier_version = r.classifier, r.version
    if r.main_persons:
        for ia in item.athletes:
            if ia.athlete and ia.athlete.full_name in r.main_persons:
                ia.main = r.main_persons[ia.athlete.full_name] >= 0.5
    item.classification = c
    session.flush()


def embed_item(session: Session, embedder: Embedder | None, item: m.Item) -> None:
    """Vector for one document. Never fails the ingest: on quota errors the vector is skipped and filled later
    in batches (`klugscheisser reembed`, 20 documents per request)."""
    if embedder is None:
        return
    text = " ".join(filter(None, [item.source_name, item.title, item.text, item.image_description]))
    try:
        vector = embedder.embed_documents([text])[0]
    except Exception as exc:
        log.warning("embedding skipped for item %s (filled later by reembed): %s", item.id, str(exc)[:120])
        return
    emb = session.scalar(
        select(m.Embedding).where(m.Embedding.item_id == item.id, m.Embedding.model == embedder.model)
    )
    if emb is None:
        session.add(m.Embedding(item_id=item.id, model=embedder.model, vector=vector))
    else:
        emb.vector = vector
    session.flush()
    embed_item_chunks(session, item)


_CHUNK_EMBEDDER: dict[str, object] = {}


def embed_item_chunks(session: Session, item: m.Item) -> None:
    """Passage vectors (RETRIEVAL_CHUNKS, gemini-embedding-2) for a new or changed full document. Never fails the
    ingest: on errors the passages are filled later by `klugscheisser reembed-chunks` (text hash check)."""
    from klugscheisser.config import get_settings

    settings = get_settings()
    if (
        not settings.retrieval_chunks
        or not settings.features["gemini"]
        or settings.embedding_provider != "gemini"
    ):
        return
    try:
        from klugscheisser.embed.chunks import store_chunks
        from klugscheisser.embed.providers import GeminiChunkEmbedder

        key = f"{settings.chunk_embedding_model}-{settings.chunk_embedding_dim}"
        if key not in _CHUNK_EMBEDDER:
            _CHUNK_EMBEDDER[key] = GeminiChunkEmbedder(settings)
        with session.begin_nested():
            store_chunks(session, _CHUNK_EMBEDDER[key], item)
    except Exception as exc:
        log.warning(
            "passage vectors skipped for item %s (filled later by reembed-chunks): %s",
            item.id,
            str(exc)[:120],
        )


def store_item(ctx: IngestContext, draft: ItemDraft, links: list[Link]) -> StoreResult:
    s = ctx.session
    digest = content_hash(draft.text, draft.title)
    item = s.scalar(select(m.Item).where(m.Item.url == draft.url).order_by(m.Item.id.desc()))
    created = item is None
    changed = created or item.content_hash != digest
    if item is not None and item.level == "light":  # passed the pre-filter now: full treatment
        item.level, changed = "full", True
    if created:
        item = m.Item(
            url=draft.url, content_hash=digest, source_type=draft.source_type, platform=draft.platform
        )
        s.add(item)
    elif changed:
        history = (item.raw or {}).get("previous_texts", [])
        keep = {k: v for k, v in (item.raw or {}).items() if k == "media"}  # keep earlier media analysis
        draft.raw = {**(draft.raw or {}), **keep, "previous_texts": [*history, item.text][-5:]}
    if changed:
        item.content_hash = digest
        item.source_type = draft.source_type
        item.source_name = draft.source_name
        item.external_id = draft.external_id
        item.author = draft.author
        item.title = draft.title
        item.published_at = draft.published_at
        item.text = draft.text
        item.media_type = draft.media_type
        item.tagged = draft.tagged
        item.raw = draft.raw
        if draft.image_description and not item.image_description:
            item.image_description = draft.image_description
    if draft.source_kind:
        item.source_kind = draft.source_kind
    s.flush()

    existing = {ia.athlete_id for ia in item.athletes}
    for link in links:
        if link.athlete_id not in existing:
            item.athletes.append(
                m.ItemAthlete(athlete_id=link.athlete_id, match_kind=link.kind, confidence=link.confidence)
            )
            existing.add(link.athlete_id)
    s.flush()

    if changed:
        apply_classification(s, item, ctx.classifier.classify(classify_input(item)))
        embed_item(s, ctx.embedder, item)
    # v15: a stored post that comes in again without an image description (reel never analysed, link had
    # expired) is queued again with its fresh links
    again = not created and not item.image_description and not item.media_pending
    if (created or again) and draft.media and ctx.media_analysis:
        from klugscheisser.media.policy import allowed_media

        media = allowed_media(s, item, draft.media, ctx.jev_client)  # v8: relevance, fan, tier, interview
        if media:
            s.add(m.MediaQueue(item_id=item.id, media=[asdict(x) for x in media]))
            item.media_pending = True
            s.flush()
    return StoreResult(item=item, created=created, changed=changed)
