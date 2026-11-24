"""One document-finding path for the web chat, the answer agent and the MCP `search` tool
(exp/search-jev-path v2, 2026-10-25).

    candidates (Postgres)  ->  Jev scores every candidate  ->  threshold + top N

Candidates (each one only finds, Jev decides):
- persons / team in scope: their newest documents (up to `cap`);
- label / topic / team lists (`Extra`, e.g. "verletzt" -> Jev category injury), newest first, never shown unranked;
- full text (`items.search_tsv`, German + language-neutral layer, OR semantics, words of 5+ letters also as word
  starts so 'Kreuzband' finds 'Kreuzbandriss'), ranked length-normalised and by how many question words occur in
  the passage Jev reads (long bios do not win by size);
- the ~40 nearest stored vectors (Gemini query vector) for meaning and other languages ('Kreuzbandriss' ~ ACL,
  'goalie' ~ 'goaltender'); switch `RETRIEVAL_VECTOR_CANDIDATES` (default on);
- no person in scope: the newest `newest` documents as a small safety net;
- MCP search (`find_documents`) without a person/team filter: persons named in the question text (ingest name
  matcher) bring their own documents as if they were the filter, for 2/3 of the cap (`SEARCH_NAMES`, default on).
More than `cap` candidates: matched ones (words, vectors, labels) first, by passage word coverage, then newest.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.embed.providers import Embedder
from klugscheisser.retrieval.search import (
    FakeReranker,
    Filters,
    Hit,
    Reranker,
    chunk_vector_rows,
    fulltext_rows,
    item_text,
    list_items,
    vector_rows,
)

log = logging.getLogger(__name__)

FTS_K = 40  # full-text hits added when persons are in scope (as before in chat)
VECTOR_K = 40  # nearest vectors added as candidates
FTS_POOL = 3  # full text: fetch FTS_POOL x k rows, keep the k with the best passage coverage
RANK_NORM = 1  # ts_rank_cd normalisation: divide by 1 + log(document length)
RESERVED = (
    15  # over the cap: the best RESERVED full-text and RESERVED vector hits always stay (by their own rank)
)
NO_PERSON_NEWEST = 40  # MCP free text without person: newest documents as a safety net
JEV_CHUNK = 20  # documents per Jev request
JEV_WORKERS = 8

_COMMON = {
    "welche",
    "welcher",
    "welches",
    "spielerin",
    "spielerinnen",
    "spieler",
    "player",
    "hockey",
    "eishockey",
    "unihockey",
    "floorball",
    "haben",
    "wurde",
}


def search_terms(*texts: str) -> list[str]:
    """Word stems (first 6 letters, words of 4+ letters or 3-letter all-caps) of the question and its variants."""
    words = (
        w.casefold() for x in texts for w in re.findall(r"\w{4,}|\b[A-ZÄÖÜ]{3}\b", x or "")
    )  # 3-letter all-caps words count too: USA, SUI, ACL
    return list(dict.fromkeys(w[:6] for w in words if w not in _COMMON))


def passage(text: str, terms: list[str], width: int = 800) -> str:
    """v16: what Jev reads of a long document: its start, plus the part where most of the search words occur
    (a Wikipedia page mentions the brain donation after 4,000 characters; the first 800 hid it: S7)."""
    if len(text) <= width or not terms:
        return text[:width]
    low, head, span = text.casefold(), 250, width - 250
    hits = sorted((m_.start(), t) for t in terms for m_ in list(re.finditer(re.escape(t), low))[:30])
    best, best_n = 0, 0
    for pos, _t in hits:
        start = max(0, pos - 120)
        n = len({t for p_, t in hits if start <= p_ < start + span})
        if n > best_n:
            best, best_n = start, n
    if best_n == 0 or best < head:
        return text[:width]
    return f"{text[:head]} … {text[best : best + span]}"


def term_hits(text: str, terms: list[str]) -> int:
    low = (text or "").lower()
    return sum(1 for w in terms if w in low)


def coverage(item: m.Item, terms: list[str]) -> float:
    """Share of the question's words in the passage Jev would read (not in the whole document: a 20,000
    character bio contains every word somewhere)."""
    if not terms:
        return 0.0
    return term_hits(passage(item_text(item), terms), terms) / len(terms)


@dataclass
class Extra:
    """One more candidate list: `filters` newest first, at most `limit`; `matched` = it matched the question
    (a label or topic), so it is kept first when the candidates are over the cap. `why` names it in the hits."""

    filters: Filters
    limit: int
    matched: bool = True
    why: str = "label"


def candidate_items(
    session: Session,
    query: str,
    f: Filters,
    *,
    cap: int,
    newest: int | None = None,
    extra: list[Extra] | None = None,
    fts_k: int | None = None,
    embedder: Embedder | None = None,
    vector_k: int = VECTOR_K,
    passages: dict[int, str] | None = None,
    cov_cache: dict[int, float] | None = None,
) -> tuple[list[m.Item], dict[int, set[str]]]:
    """Candidate documents for `query` under `f`, at most `cap`. Returns (items, why) with why[id] a subset of
    {"newest", "label", "topic", "team", "fulltext", "vector"}; "newest"/"team" alone = not matched.
    `newest` default: `cap` when persons/teams are in scope, else NO_PERSON_NEWEST. `fts_k` default: FTS_K when
    in scope, else `cap` (free text depends on the words). With a passage embedder (RETRIEVAL_CHUNKS) the vector
    candidates come from item_chunks and `passages` (if given) gets each vector hit's best passage."""
    scoped = bool(f.athlete_ids or f.team_ids)
    newest = (cap if scoped else NO_PERSON_NEWEST) if newest is None else newest
    fts_k = (FTS_K if scoped else cap) if fts_k is None else fts_k
    terms = search_terms(query)
    cov_cache = {} if cov_cache is None else cov_cache  # item id -> coverage (same question; ~0.3 ms each)

    def cov(item: m.Item) -> float:
        if item.id not in cov_cache:
            cov_cache[item.id] = coverage(item, terms)
        return cov_cache[item.id]

    found: dict[int, m.Item] = {}
    why: dict[int, set[str]] = {}
    # the query vector (a Gemini call, ~0.3-0.5 s) is computed while Postgres runs the other lists
    qvec_future = None
    pool_ = None
    if embedder is not None and vector_k > 0:
        from concurrent.futures import ThreadPoolExecutor

        pool_ = ThreadPoolExecutor(max_workers=1)
        qvec_future = pool_.submit(embedder.embed_query, query)

    def add(item: m.Item, reason: str) -> None:
        found.setdefault(item.id, item)
        why.setdefault(item.id, set()).add(reason)

    try:
        if newest > 0:
            for item in list_items(session, f, limit=newest):
                add(item, "newest")
        for x in extra or []:
            for item in list_items(session, x.filters, limit=x.limit):
                add(item, x.why if x.matched else "team")
        fts_top: list[m.Item] = []
        if fts_k > 0:
            rows = fulltext_rows(session, query, f, k=fts_k * FTS_POOL, prefix=True, norm=RANK_NORM)
            pool = _load(session, [r[0] for r in rows])
            rank = {item_id: i for i, (item_id, _) in enumerate(rows)}
            fts_best = sorted(pool, key=lambda i: (-cov(i), rank[i.id]))[:fts_k]
            fts_top = pool[:RESERVED]  # by Postgres rank, whatever the word coverage (short result lines)
            for item in fts_top + fts_best:
                add(item, "fulltext")
        vec_best: list[m.Item] = []
        if qvec_future is not None:
            try:
                qvec = qvec_future.result()
            except Exception as exc:  # e.g. embedding quota used up: full text only
                log.warning("query embedding failed, full text only: %s", str(exc)[:120])
                qvec = None
            if qvec is not None:
                if getattr(embedder, "chunked", False):
                    rows = chunk_vector_rows(session, embedder, query, f, k=vector_k, qvec=qvec)
                    if passages is not None:
                        passages.update({r[0]: r[2] for r in rows})
                else:
                    rows = vector_rows(session, embedder, query, f, k=vector_k, qvec=qvec)
                vec_best = _load(session, [r[0] for r in rows])
                for item in vec_best:
                    add(item, "vector")
    finally:
        if pool_ is not None:
            pool_.shutdown(wait=False)
    items = list(found.values())
    if len(items) > cap:
        # the top full-text and vector hits stay whatever their word coverage (an official result line with
        # few question words must not be pushed out by the persons' newest posts), the rest by coverage
        keep = list(dict.fromkeys(i.id for i in fts_top + vec_best[:RESERVED]))[:cap]
        hit = {i.id: bool(why[i.id] - {"newest", "team"}) for i in items}
        rest = sorted((i for i in items if i.id not in set(keep)), key=lambda i: (not hit[i.id], -cov(i)))
        items = [found[i] for i in keep] + rest[: cap - len(keep)]
    return items, {i.id: why[i.id] for i in items}


NAMES_MAX = 6  # at most this many persons named in one question bring their documents
NAMES_TTL_S = 600  # the name index (aliases, handles, surnames) is rebuilt after this many seconds
_NAME_MATCHERS: dict[str, tuple[float, tuple, object]] = {}
# surnames in the matcher's surname index that are everyday words in a search question ("gleich viele Tore",
# "Wetter in Lahti"); full names with these surnames still count
WORD_SURNAMES = frozenset({"gleich", "wetter", "million", "kluge", "kessel", "pfeffer"})


def _name_matcher(session: Session):
    """The ingest name matcher (domain/matcher.AthleteMatcher, without Jev), cached per database: rebuilt when
    persons or aliases were added or removed (one cheap count query) or after NAMES_TTL_S (tier changes).
    Building reads ~2,300 aliases (40-300 ms); matching a question then takes < 1 ms."""
    import time

    from sqlalchemy import func, select

    from klugscheisser.domain.matcher import AthleteMatcher

    stamp = tuple(
        session.execute(
            select(
                select(func.count(m.AthleteAlias.id)).scalar_subquery(),
                select(func.max(m.AthleteAlias.id)).scalar_subquery(),
                select(func.count(m.Athlete.id)).scalar_subquery(),
                select(func.max(m.Athlete.id)).scalar_subquery(),
            )
        ).one()
    )
    key = str(session.get_bind().url)
    now = time.monotonic()
    hit = _NAME_MATCHERS.get(key)
    if hit is not None and hit[1] == stamp and now - hit[0] < NAMES_TTL_S:
        return hit[2]
    matcher = AthleteMatcher(session)
    matcher.surname_index()  # built now: the cached matcher must not need the session again
    _NAME_MATCHERS[key] = (now, stamp, matcher)
    return matcher


def named_persons(session: Session, query: str) -> list[int]:
    """Persons named in a search question, in the ingest matcher's way: full names and stored aliases (accents
    and umlaut spellings folded: 'Brandli', 'Braendli', 'Brändli'), unique surnames of tier 1/2 persons
    ('Stalder'; not common ones like 'Müller'), else two words close to one stored name ('Lara Staldr').
    Namesakes are skipped, never guessed. Order: as found; at most NAMES_MAX."""
    from klugscheisser.domain.names import normalize

    if not (query or "").strip():
        return []
    try:
        with session.begin_nested():  # a failing query here must not abort the search's transaction
            matcher = _name_matcher(session)
        ids = [link.athlete_id for link in matcher.from_text(query)]
        words = set(normalize(query).split())
        # 'Braendli' (umlaut written out) -> also 'brandli', the accent-stripped form the surname index holds
        words |= {w.replace("ae", "a").replace("oe", "o").replace("ue", "u") for w in words}
        words -= WORD_SURNAMES
        ids += [link.athlete_id for link in matcher.from_surnames(" ".join(words), set(ids))]
        if not ids:
            ids = [link.athlete_id for link, _ in matcher.from_text_fuzzy(query)]
    except Exception as exc:  # the name index never blocks a search
        log.warning("name recognition failed: %s", str(exc)[:120])
        return []
    return list(dict.fromkeys(ids))[:NAMES_MAX]


class _OnceEmbedder:
    """Wraps an embedder so two candidate passes for the same question make one query vector (Gemini call)."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self._cache: dict[str, list] = {}

    def embed_query(self, text: str):
        if text not in self._cache:
            self._cache[text] = self._inner.embed_query(text)
        return self._cache[text]

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def named_candidates(
    session: Session,
    settings: Settings,
    query: str,
    f: Filters,
    *,
    cap: int,
    extra: list[Extra] | None = None,
    embedder: Embedder | None = None,
    passages: dict[int, str] | None = None,
) -> tuple[list[m.Item], dict[int, set[str]], list[int]]:
    """candidate_items, plus (SEARCH_NAMES, no person/team filter) the documents of the persons named in the
    question: a share of the cap is filled as if the person were the filter (her newest documents, her labels,
    full text and vectors within her documents), the rest from the question as before, so a topic question
    keeps results about other people. Returns (items, why, named ids); her documents carry why 'person'.
    Search lab: with the name only in the text, 45% of her own relevant posts were among the candidates
    (83% with the athlete field): own posts rarely contain their author's name."""
    named: list[int] = []
    if settings.search_names and not (f.athlete_ids or f.team_ids):
        named = named_persons(session, query)
    if not named:
        items, why = candidate_items(
            session, query, f, cap=cap, extra=extra, embedder=embedder, passages=passages
        )
        return items, why, []
    once = _OnceEmbedder(embedder) if embedder is not None else None
    cov_cache: dict[int, float] = {}  # both passes rank by the same question's word coverage
    pf = replace(f, athlete_ids=named)
    p_cap = max(1, min(cap, round(cap * settings.search_names_share)))
    p_extra = [replace(x, filters=replace(x.filters, athlete_ids=named)) for x in extra or []]
    p_items, p_why = candidate_items(
        session, query, pf, cap=p_cap, extra=p_extra, embedder=once, passages=passages, cov_cache=cov_cache
    )
    o_items, o_why = candidate_items(
        session, query, f, cap=cap, extra=extra, embedder=once, passages=passages, cov_cache=cov_cache
    )
    items = list(p_items)
    seen = {i.id for i in items}
    why = {i.id: set(p_why[i.id]) | {"person"} for i in p_items}
    for item in o_items:
        if item.id in seen:
            why[item.id] |= o_why[item.id]
        elif len(items) < cap:
            items.append(item)
            seen.add(item.id)
            why[item.id] = set(o_why[item.id])
    return items, why, named


def _load(session: Session, ids: list[int]) -> list[m.Item]:
    if not ids:
        return []
    from sqlalchemy import select

    by_id = {i.id: i for i in session.scalars(select(m.Item).where(m.Item.id.in_(ids)))}
    return [by_id[i] for i in ids if i in by_id]


_CHUNKS_READY: dict[str, bool] = {}


def chunks_ready(settings: Settings) -> bool:
    """Are there passage vectors of the configured model in this project's database (checked once per process and
    database when found)? Before the backfill (`klugscheisser reembed-chunks`) RETRIEVAL_CHUNKS falls back to the
    document vectors. Keyed by database: a second project without passage vectors keeps its document vectors."""
    model = f"{settings.chunk_embedding_model}-{settings.chunk_embedding_dim}"
    key = f"{settings.database_url}|{model}"
    if _CHUNKS_READY.get(key):
        return True
    try:
        from sqlalchemy import select

        from klugscheisser.db.session import session_scope

        with session_scope() as s:
            ok = s.scalar(select(m.ItemChunk.id).where(m.ItemChunk.model == model).limit(1)) is not None
    except Exception:
        ok = False
    if ok:
        _CHUNKS_READY[key] = True
    return ok


def query_embedder(settings: Settings) -> Embedder | None:
    """The embedder for vector candidates, or None: switched off (RETRIEVAL_VECTOR_CANDIDATES and
    RETRIEVAL_EMBEDDINGS both false) or not usable (e.g. EMBEDDING_PROVIDER=gemini without key): full text only."""
    if not (settings.retrieval_vector_candidates or settings.retrieval_embeddings):
        return None
    from klugscheisser.embed.providers import get_chunk_embedder, get_embedder

    if settings.retrieval_chunks and chunks_ready(settings):
        return get_chunk_embedder(settings)

    try:
        return get_embedder(settings)
    except Exception as exc:
        log.warning("no query embedder, full text only: %s", str(exc)[:120])
        return None


def _budget_left() -> float:
    from klugscheisser.llm import budget as llm_budget

    b = llm_budget.current()
    return float("inf") if b is None else b.left()


def score_texts(
    settings: Settings,
    query: str,
    texts: list[str],
    *,
    fallback_texts: list[str] | None = None,
    reranker: Reranker | None = None,
) -> list[float]:
    """Jev score (0..1) per text, JEV_CHUNK texts per request, requests in parallel. Without Jev (no key, answer
    budget used up) or on a failed request: the free word overlap (FakeReranker) on `fallback_texts`."""
    if not texts:
        return []
    fallback = fallback_texts or texts
    if reranker is None:
        if not (settings.features["jev"] and _budget_left() > 0):
            return FakeReranker().score(query, fallback)
        from klugscheisser.classify.jev import JevReranker

        reranker = JevReranker(settings)
    from klugscheisser.projects import ContextPool

    chunks = [list(range(i, min(i + JEV_CHUNK, len(texts)))) for i in range(0, len(texts), JEV_CHUNK)]

    def score_chunk(idx: list[int]) -> list[float]:
        try:
            return reranker.score(query, [texts[i] for i in idx])
        except Exception as exc:
            log.warning("jev rerank failed: %s", exc)
            return FakeReranker().score(query, [fallback[i] for i in idx])

    with ContextPool(max_workers=min(JEV_WORKERS, len(chunks))) as pool:
        return [x for part in pool.map(score_chunk, chunks) for x in part]


def jev_text(item: m.Item, terms: list[str], best: str | None = None) -> str:
    """What Jev reads of one stored document: source + title, then the passage with the question's words (or
    `best`, the passage the chunk vectors found)."""
    head = " ".join(filter(None, [item.source_name, item.title]))
    body = best or " ".join(filter(None, [item.text, item.image_description]))
    return f"{head}\n{passage(body, terms, 800 - min(len(head), 200))}" if head else passage(body, terms)


CARD_TEXT_CHARS = 900  # passage length Jev reads in a document card (title/source/date are separate fields)


def age_de(published, now=None) -> str:
    from datetime import UTC, datetime

    if not published:
        return "unbekannt"
    now = now or datetime.now(UTC)
    days = (now - published).days
    when = "heute" if days <= 0 else "gestern" if days == 1 else f"vor {days} Tagen"
    if days > 60:
        when = f"vor {days // 30} Monaten" if days < 730 else f"vor {days // 365} Jahren"
    return f"{published.date().isoformat()} ({when})"


def doc_card(
    item: m.Item, terms: list[str], best: str | None = None, now=None, width: int | None = None
) -> dict[str, str]:
    """One stored document as Jev (pairwise state), the chat and the MCP cards read it: title, source, kind,
    absolute date + age, persons, and the passage (the chunk vectors' best passage if given, else the window
    with the question's words), CARD_TEXT_CHARS long at most."""
    names = [ia.athlete.full_name for ia in item.athletes if ia.athlete]
    if not names and item.teams:
        names = ["Team " + t.team.name for t in item.teams if t.team]
    body = best or " ".join(filter(None, [item.text, item.image_description]))
    return {
        "titel": (item.title or "")[:200],
        "quelle": item.source_name or item.platform or "",
        "art": item.source_kind or item.source_type or "",
        "datum": age_de(item.published_at, now),
        "über": ", ".join(sorted(names)[:8]) or "-",
        "text": passage(body, terms, width or CARD_TEXT_CHARS),
    }


CARD_LINE_PASSAGE = 620  # chunk mode reads 800 characters per candidate: card line + this much passage


def card_line(card: dict[str, str]) -> str:
    """A document card as one text for chunk-mode Jev (800 characters)."""
    return (
        f"{card['titel'][:80]} | {card['quelle']} ({card['art']}) | {card['datum']} | über: {card['über'][:60]}\n"
        f"{card['text']}"
    )


def preload_cards(session: Session, items: list[m.Item]) -> None:
    """Persons and teams of all candidates in two queries instead of one lazy load per card (~0.1 s at 120)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    ids = [i.id for i in items]
    if ids:
        list(
            session.scalars(
                select(m.Item)
                .where(m.Item.id.in_(ids))
                .options(
                    selectinload(m.Item.athletes).selectinload(m.ItemAthlete.athlete),
                    selectinload(m.Item.teams).selectinload(m.ItemTeam.team),
                )
            )
        )


def pair_reranker(settings: Settings, reranker: Reranker | None):
    """The pairwise Jev (JEV_PAIRWISE) if it is to be used here, else None (chunk mode / fallback)."""
    if reranker is not None:
        return reranker if getattr(reranker, "pairwise", False) else None
    if not (settings.jev_pairwise and settings.features["jev"] and _budget_left() > 0):
        return None
    from klugscheisser.classify.jev import JevPairReranker

    return JevPairReranker(settings)


def find_documents(
    session: Session,
    settings: Settings,
    query: str,
    f: Filters,
    *,
    top_n: int = 8,
    min_score: float | None = None,
    cap: int | None = None,
    extra: list[Extra] | None = None,
    reranker: Reranker | None = None,
    embedder: Embedder | None = None,
    group_stories: bool = False,
) -> list[Hit]:
    """MCP `search` on the shared path: candidates (words, vectors, labels, newest), Jev scores all of them,
    keep score >= `min_score` (default RERANK_MIN_SCORE), best `top_n`. Ties: matched first, then newest.
    Hit.scores: rerank score plus how the document was found (`gefunden`: fulltext/vector/label/...).
    `group_stories` (set by the caller from its switch, e.g. SEARCH_GROUP_STORIES): documents of one story are
    folded into the best one (retrieval/stories.py) before the top `top_n` are taken; the folded ones are in
    Hit.scores["auch"] (id, quelle, url, datum each), so `top_n` counts distinct stories."""
    cap = max(1, cap or settings.search_jev_max_docs)
    min_score = settings.rerank_min_score if min_score is None else min_score
    passages: dict[int, str] = {}
    items, why, _named = named_candidates(
        session, settings, query, f, cap=cap, extra=extra, embedder=embedder, passages=passages
    )
    if not items:
        return []
    terms = search_terms(query)
    pair = pair_reranker(settings, reranker)
    beleg: list[float | None] = [None] * len(items)
    if pair is not None:
        judged = pair.judge(query, [doc_card(i, terms, passages.get(i.id)) for i in items])
        scores = [j["score"] for j in judged]
        beleg = [j.get("beleg") for j in judged]
    else:
        if settings.jev_card_text:
            preload_cards(session, items)
            texts = [
                card_line(doc_card(i, terms, passages.get(i.id), width=CARD_LINE_PASSAGE)) for i in items
            ]
        else:
            texts = [jev_text(i, terms, passages.get(i.id)) for i in items]
        if (
            reranker is None
            and settings.jev_reader_context
            and settings.features["jev"]
            and _budget_left() > 0
        ):
            from klugscheisser.classify.jev import JevReranker

            reranker = JevReranker(settings, reader_context=True)
        scores = score_texts(
            settings, query, texts, fallback_texts=[item_text(i) for i in items], reranker=reranker
        )
    hits = []
    for item, sc, bl in zip(items, scores, beleg, strict=False):
        reasons = why.get(item.id, set())
        sc_: dict = {"rerank": round(sc, 3), "gefunden": sorted(reasons)}
        if bl is not None:
            sc_["beleg"] = round(bl, 3)
        if item.id in passages:
            sc_["passage"] = passages[item.id]
        if "label" in reasons or "topic" in reasons:
            sc_["label_match"] = True
        hits.append(Hit(item, sc, sc_))
    matched = {i for i, r in why.items() if r - {"newest", "team"}}
    noul_min = settings.rerank_noul_min
    if noul_min is not None and any(b is not None for b in beleg):
        kept = [h for h in hits if h.scores.get("beleg", 0.0) >= noul_min]
    else:
        kept = [h for h in hits if h.score >= min_score]
    kept.sort(
        key=lambda h: (
            h.score,
            h.item.id in matched,
            h.item.published_at.timestamp() if h.item.published_at else 0.0,
        ),
        reverse=True,
    )
    if group_stories and len(kept) > 1:
        return fold_hits(session, settings, kept)[:top_n]
    return kept[:top_n]


def fold_hits(session: Session, settings: Settings, hits: list[Hit]) -> list[Hit]:
    """Ranked hits -> one hit per story (retrieval/stories.group); the others under scores['auch']."""
    from klugscheisser.retrieval import stories

    try:
        groups = stories.group(session, settings, [h.item for h in hits], [h.score for h in hits])
    except Exception as exc:  # never fail a search over grouping (e.g. no item_chunks table)
        log.warning("story groups skipped: %s", str(exc)[:160])
        return hits
    out = []
    for g in groups:
        main = hits[g[0]]
        if len(g) > 1:
            main.scores["auch"] = [stories.also_entry(hits[i].item) for i in g[1:]]
        out.append(main)
    return out


__all__ = [
    "Extra",
    "age_de",
    "candidate_items",
    "coverage",
    "doc_card",
    "find_documents",
    "fold_hits",
    "jev_text",
    "named_candidates",
    "named_persons",
    "passage",
    "query_embedder",
    "score_texts",
    "search_terms",
    "term_hits",
]
