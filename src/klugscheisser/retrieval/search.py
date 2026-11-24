"""Retrieval: filters -> hybrid search (pgvector + German full text, reciprocal rank fusion) -> rerank."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Protocol

from sqlalchemy import Float, cast, exists, func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.names import normalize
from klugscheisser.embed.providers import Embedder

RRF_K = 60


@dataclass
class Filters:
    athlete_ids: list[int] | None = None
    team_ids: list[int] | None = None
    since: datetime | None = None
    until: datetime | None = None
    source_types: list[str] | None = None
    only_relevant: bool = True
    only_vorsicht: bool = False
    platform: str | None = None
    categories: list[str] | None = None
    storylines: list[str] | None = None
    moods: list[str] | None = None
    linked_only: bool = False  # only documents linked to some person or team in our database
    topics: list[str] | None = None  # v5: any of these Jev topic tags (classify.base.TOPICS)
    include_light: bool = False  # v5: also title/teaser-only documents that passed no pre-filter


@dataclass
class Hit:
    item: m.Item
    score: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)


class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: list[str]) -> list[float]: ...


class FakeReranker:
    """Lexical overlap between query and item (normalised tokens)."""

    name = "lexical"

    def score(self, query: str, texts: list[str]) -> list[float]:
        q = {t for t in normalize(query).split() if len(t) > 2}
        if not q:
            return [0.5] * len(texts)
        out = []
        for text in texts:
            tokens = set(normalize(text).split())
            out.append(sum(1 for t in q if t in tokens or any(w.startswith(t[:5]) for w in tokens)) / len(q))
        return out


class LlmReranker:
    name = "llm"
    SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {"scores": {"type": "array", "items": {"type": "integer", "enum": [0, 1, 2, 3]}}},
        "required": ["scores"],
        "additionalProperties": False,
    }

    def __init__(self, llm) -> None:
        self.llm = llm

    def score(self, query: str, texts: list[str]) -> list[float]:
        listing = "\n".join(f"[{i}] {t[:600]}" for i, t in enumerate(texts))
        out = self.llm.complete_json(
            "Bewerte für jeden Kandidaten, wie gut er die Frage beantwortet: 0 gar nicht, 3 direkt. "
            "Gib genau eine Zahl pro Kandidat in Reihenfolge zurück.",
            f"Frage: {query}\n\nKandidaten:\n{listing}",
            self.SCHEMA,
        )
        scores = list(out.get("scores", []))[: len(texts)]
        return [s / 3 for s in scores] + [0.0] * (len(texts) - len(scores))


def get_reranker(settings: Settings, llm=None) -> Reranker:
    if settings.features["jev"]:
        from klugscheisser.classify.jev import JevReranker

        return JevReranker(settings)
    if llm is not None:
        return LlmReranker(llm)
    return FakeReranker()


def _filtered(f: Filters):
    q = select(m.Item.id).join(m.Classification, isouter=True).where(m.Item.text.is_not(None))
    if not f.include_light:
        q = q.where(m.Item.level == "full")
    if f.topics:
        from klugscheisser.classify.base import TOPIC_MIN_P

        q = q.where(
            or_(
                *[
                    func.coalesce(
                        cast(func.jsonb_extract_path_text(cast(m.Classification.topics, JSONB), t), Float),
                        0.0,
                    )
                    >= TOPIC_MIN_P
                    for t in f.topics
                ]
            )
        )
    if f.only_relevant:
        q = q.where(or_(m.Classification.relevant.is_(True), m.Classification.id.is_(None)))
    if f.only_vorsicht:
        q = q.where(m.Classification.vorsicht.is_(True))
    if f.since:
        q = q.where(m.Item.published_at >= f.since)
    if f.until:
        q = q.where(m.Item.published_at <= f.until)
    if f.source_types:
        q = q.where(m.Item.source_type.in_(f.source_types))
    if f.platform:
        q = q.where(m.Item.platform == f.platform)
    label_conds = []
    if f.categories:
        label_conds.append(m.Classification.category.in_(f.categories))
    if f.storylines:
        label_conds.append(m.Classification.storyline.in_(f.storylines))
    if f.moods:
        label_conds.append(m.Classification.mood.in_(f.moods))
    if label_conds:
        q = q.where(or_(*label_conds))
    conds = []
    if f.athlete_ids:
        conds.append(
            exists().where(m.ItemAthlete.item_id == m.Item.id, m.ItemAthlete.athlete_id.in_(f.athlete_ids))
        )
    if f.team_ids:
        conds.append(exists().where(m.ItemTeam.item_id == m.Item.id, m.ItemTeam.team_id.in_(f.team_ids)))
    if conds:
        q = q.where(or_(*conds))
    elif f.linked_only:
        q = q.where(
            or_(
                exists().where(m.ItemAthlete.item_id == m.Item.id),
                exists().where(m.ItemTeam.item_id == m.Item.id),
            )
        )
    return q


def list_items(session: Session, f: Filters, limit: int = 20) -> list[m.Item]:
    """No query text: newest first."""
    ids = _filtered(f).order_by(m.Item.published_at.desc().nulls_last()).limit(limit)
    return list(
        session.scalars(
            select(m.Item).where(m.Item.id.in_(ids)).order_by(m.Item.published_at.desc().nulls_last())
        )
    )


def vector_rows(
    session: Session, embedder: Embedder | None, query: str, f: Filters, k: int = 40, qvec: list | None = None
) -> list:
    """(item_id, cosine distance) of the k nearest stored vectors under `f`; [] without embedder or when the
    query vector fails (e.g. embedding quota used up: the caller goes on with full text). `qvec`: a query vector
    computed already (retrieval/docs.py computes it while Postgres runs the full text)."""
    if embedder is None or k <= 0:
        return []
    if qvec is None:
        try:
            qvec = embedder.embed_query(query)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("query embedding failed, full text only: %s", str(exc)[:120])
            return []
    allowed = _filtered(f).subquery()
    # the HNSW index returns only ef_search (40) nearest vectors of ALL documents, the filter then drops most of
    # them (team USA: 2 of 40 left). pgvector >= 0.8 scans on until k filtered rows are found.
    try:
        with session.begin_nested():
            session.execute(sql_text("SET LOCAL hnsw.iterative_scan = strict_order"))
    except Exception:  # older pgvector: unknown setting, plain index scan as before
        pass
    distance = m.Embedding.vector.cosine_distance(qvec)
    return session.execute(
        select(m.Embedding.item_id, distance.label("d"))
        .where(m.Embedding.model == embedder.model, m.Embedding.item_id.in_(select(allowed.c.id)))
        .order_by(distance)
        .limit(k)
    ).all()


def chunk_vector_rows(
    session: Session,
    embedder,
    query: str,
    f: Filters,
    k: int = 40,
    qvec: list | None = None,
    per_doc: int = 4,
) -> list[tuple[int, float, str]]:
    """RETRIEVAL_CHUNKS: (item_id, cosine distance of its best passage, that passage's text) for the k documents
    with the nearest passages under `f`. Fetches k x per_doc passages (HNSW, iterative scan so filters still
    fill up), keeps the best one per document."""
    if embedder is None or k <= 0:
        return []
    if qvec is None:
        qvec = embedder.embed_query(query)
    allowed = _filtered(f).subquery()
    try:
        with session.begin_nested():
            session.execute(sql_text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            session.execute(sql_text(f"SET LOCAL hnsw.ef_search = {max(100, k * per_doc)}"))
    except Exception:
        pass
    from pgvector.sqlalchemy import HALFVEC

    distance = m.ItemChunk.vector.cosine_distance(cast(qvec, HALFVEC(len(qvec))))
    rows = session.execute(
        select(m.ItemChunk.item_id, distance.label("d"), m.ItemChunk.text)
        .where(m.ItemChunk.model == embedder.model, m.ItemChunk.item_id.in_(select(allowed.c.id)))
        .order_by(distance)
        .limit(k * per_doc)
    ).all()
    best: dict[int, tuple[int, float, str]] = {}
    for item_id, d, text in sorted(rows, key=lambda r: r[1]):  # relaxed_order: sort again
        best.setdefault(item_id, (item_id, float(d), text))
    return list(best.values())[:k]


def fulltext_rows(
    session: Session, query: str, f: Filters, k: int = 40, prefix: bool = False, norm: int = 0
) -> list:
    """(item_id, ts_rank_cd) of the best k full-text matches under `f` (German + language-neutral layer, words
    OR-ed). `prefix`: words of 5+ letters also match as word starts ('kreuzband' finds 'Kreuzbandriss').
    `norm`: ts_rank_cd normalisation (1 = divide by 1 + log(length): long bios no longer win by size)."""
    allowed = _filtered(f).subquery()

    def or_query(config: str):
        tsq = func.websearch_to_tsquery(config, func.immutable_unaccent(query))
        # OR-semantics for recall: rewrite the AND query into an OR query
        txt = func.replace(func.cast(tsq, m.Text()), "&", "|")
        if prefix:
            txt = func.regexp_replace(txt, "'([^' ]{5,})'", "'\\1':*", "g")
        return func.to_tsquery(config, txt)

    # German stemming plus the language-neutral layer for sv/fi/cs/fr/it documents (v5)
    tsq_or = or_query("german").op("||")(or_query("simple"))
    rank = (
        func.ts_rank_cd(m.Item.search_tsv, tsq_or, norm)
        if norm
        else func.ts_rank_cd(m.Item.search_tsv, tsq_or)
    )
    return session.execute(
        select(m.Item.id, rank.label("r"))
        .where(m.Item.id.in_(select(allowed.c.id)), m.Item.search_tsv.op("@@")(tsq_or))
        .order_by(rank.desc())
        .limit(k)
    ).all()


def hybrid_search(
    session: Session, embedder: Embedder | None, query: str, f: Filters, k: int = 40, prefix: bool = False
) -> list[Hit]:
    """Old MCP search (MCP_SEARCH_EMBEDDINGS=true): vectors + full text, fused by reciprocal rank.
    embedder None: full text only."""
    vec_rows = vector_rows(session, embedder, query, f, k)
    fts_rows = fulltext_rows(session, query, f, k, prefix=prefix)
    fused: dict[int, dict[str, float]] = {}
    for i, (item_id, d) in enumerate(vec_rows):
        fused.setdefault(item_id, {})["vector"] = round(1 - float(d), 4)
        fused[item_id]["rrf"] = fused[item_id].get("rrf", 0) + 1 / (RRF_K + i + 1)
    for i, (item_id, r) in enumerate(fts_rows):
        fused.setdefault(item_id, {})["fulltext"] = round(float(r), 4)
        fused[item_id]["rrf"] = fused[item_id].get("rrf", 0) + 1 / (RRF_K + i + 1)
    items = {i.id: i for i in session.scalars(select(m.Item).where(m.Item.id.in_(fused)))}
    hits = [Hit(items[i], s["rrf"], s) for i, s in fused.items() if i in items]
    return sorted(hits, key=lambda h: h.score, reverse=True)


def item_text(item: m.Item) -> str:
    return " ".join(filter(None, [item.source_name, item.title, item.text, item.image_description]))


def search(
    session: Session,
    embedder: Embedder,
    reranker: Reranker,
    query: str,
    f: Filters,
    top_n: int = 8,
    min_score: float = 0.2,
) -> list[Hit]:
    hits = hybrid_search(session, embedder, query, f)[: max(top_n * 3, 20)]
    if not hits:
        return []
    try:
        scores = reranker.score(query, [item_text(h.item) for h in hits])
    except Exception:
        scores = FakeReranker().score(query, [item_text(h.item) for h in hits])
    for h, sc in zip(hits, scores, strict=False):
        h.scores["rerank"] = round(sc, 3)
        h.score = sc + h.score  # rerank dominates; RRF breaks ties
    kept = [h for h in hits if h.scores["rerank"] >= min_score]
    return sorted(kept, key=lambda h: h.score, reverse=True)[:top_n]


def athlete_team_filter(session: Session, team: m.Team) -> Filters:
    """Items about a team: linked to the team, or to any athlete on its newest roster / affiliation."""
    entry_ids = select(m.EventEntry.athlete_id).where(m.EventEntry.team_id == team.id)
    aff_ids = select(m.Affiliation.athlete_id).where(m.Affiliation.team_id == team.id)
    athlete_ids = list(
        session.scalars(
            select(m.Athlete.id).where(or_(m.Athlete.id.in_(entry_ids), m.Athlete.id.in_(aff_ids)))
        )
    )
    return Filters(athlete_ids=athlete_ids or None, team_ids=[team.id])


__all__ = [
    "Filters",
    "Hit",
    "athlete_team_filter",
    "chunk_vector_rows",
    "fulltext_rows",
    "get_reranker",
    "hybrid_search",
    "list_items",
    "search",
    "vector_rows",
]
