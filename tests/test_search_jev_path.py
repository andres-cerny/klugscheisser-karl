"""exp/search-jev-path v2: one document-finding path for chat and MCP `search` (words, vectors, labels -> Jev)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest.runner import build_context
from klugscheisser.mcp_server import tools as t
from klugscheisser.retrieval import docs
from klugscheisser.retrieval.search import FakeReranker, Filters

SETTINGS = Settings(_env_file=None, auto_refresh=False)


def add_item(s, url, text, *, person=None, days_ago=0, source_type="srf", category="other"):
    item = m.Item(
        source_type=source_type,
        platform="rss",
        source_name="SRF",
        url=url,
        title=text[:40],
        text=text,
        content_hash=url,
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    s.add(item)
    s.flush()
    item.classification = m.Classification(
        relevant_p=0.9,
        relevant=True,
        category=category,
        sensitive_p=0.1,
        newsworthiness=2,
        classifier="t",
        classifier_version="1",
    )
    if person is not None:
        item.athletes.append(m.ItemAthlete(athlete_id=person.id))
    s.flush()
    return item


@pytest.fixture
def world(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.upsert_athlete(s, sport, "Lara Testalder")
        for i in range(60):  # newer noise: would push the hit out of a "newest only" list
            add_item(s, f"https://noise/{i}", f"Trainingslager in Davos Tag {i}, gute Stimmung.", days_ago=i)
        add_item(s, "https://knie", "Kreuzbandriss im Knie: die Saison ist vorbei.", person=lara, days_ago=90)
        add_item(s, "https://lara-new", "Lara Testalder trifft doppelt beim Sieg.", person=lara, days_ago=1)
        s.commit()
    return db


class CountingReranker:
    name = "count"

    def __init__(self):
        self.calls: list[int] = []

    def score(self, query, texts):
        self.calls.append(len(texts))
        return FakeReranker().score(query, texts)


def test_free_text_without_person_finds_old_document_by_full_text(world):
    with Session(world) as s:
        items, matched = docs.candidate_items(s, "Kreuzband Knie", Filters(), cap=30)
        urls = [i.url for i in items]
        assert "https://knie" in urls  # 90 days old, behind 60 newer documents: found by full text
        assert len(items) <= 30
        knie = next(i for i in items if i.url == "https://knie")
        assert knie.id in matched


def test_over_cap_keeps_full_text_matches_first(world):
    with Session(world) as s:
        lara = repo.resolve_athlete(s, "Lara Testalder")
        f = Filters(athlete_ids=[lara.id], team_ids=None)
        items, _ = docs.candidate_items(s, "Kreuzband", f, cap=1)
        assert [i.url for i in items] == ["https://knie"]  # not the newest, the one with the word


def test_find_documents_scores_and_thresholds(world):
    with Session(world) as s:
        hits = docs.find_documents(s, SETTINGS, "Kreuzband Knie Saison", Filters(), top_n=3, min_score=0.3)
        assert hits and hits[0].item.url == "https://knie"
        assert all(h.scores["rerank"] >= 0.3 for h in hits)
        assert docs.find_documents(s, SETTINGS, "zzzqqq unbekannt", Filters(), min_score=0.5) == []


def test_score_texts_in_chunks_keeps_order():
    rr = CountingReranker()
    texts = [f"text {i} kreuzband" if i == 33 else f"text {i}" for i in range(45)]
    scores = docs.score_texts(SETTINGS, "kreuzband", texts, reranker=rr)
    assert sorted(rr.calls) == [5, 20, 20] and len(scores) == 45
    assert scores[33] == max(scores)


class StubEmbedder:
    """Query vector = the first axis; documents get their vectors stored directly in the test."""

    model = "stub-384"

    def embed_query(self, text):
        return [1.0] + [0.0] * 383

    def embed_documents(self, texts):
        return [[0.0] * 384 for _ in texts]


def test_vector_candidates_find_what_no_word_matches(world):
    with Session(world) as s:
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        s.add(m.Embedding(item_id=knie.id, model="stub-384", vector=[1.0] + [0.0] * 383))
        s.flush()
        _, why = docs.candidate_items(s, "torn ACL", Filters(), cap=30, embedder=StubEmbedder())
        assert knie.id in why and "vector" in why[knie.id]
        _, why = docs.candidate_items(s, "torn ACL", Filters(), cap=30, embedder=None)
        assert knie.id not in why  # no word in common: only the vector finds it
        s.rollback()


def test_query_embedder_switches():
    assert docs.query_embedder(SETTINGS) is not None  # default: vector candidates on (fake embedder in tests)
    off = Settings(_env_file=None, retrieval_vector_candidates=False, retrieval_embeddings=False)
    assert docs.query_embedder(off) is None
    no_key = Settings(_env_file=None, embedding_provider="gemini", gemini_api_key=None)
    assert docs.query_embedder(no_key) is None  # unusable embedder: full text only, no error


def test_full_text_prefers_the_passage_with_the_words_over_a_long_bio(db):
    with Session(db) as s:
        add_item(s, "https://bio", "Kreuzband " + "Lebenslauf und Vereine " * 900 + " Knie")
        add_item(s, "https://kurz", "Kreuzband und Knie: Saisonende.")
        items, _ = docs.candidate_items(s, "Kreuzband Knie", Filters(), cap=1, newest=0, fts_k=1)
        assert [i.url for i in items] == ["https://kurz"]


def test_labels_are_ranked_not_prepended(world):
    with Session(world) as s:
        add_item(s, "https://fussball", "Stürmer fällt mehrere Wochen aus.", category="injury")
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        knie.classification.category = "injury"
        s.flush()
        out = t.search_tool(s, SETTINGS, "Kreuzbandriss Knie")
        s.rollback()
    assert out["erkannt"] == {"categories": ["injury"]}
    urls = [h["url"] for h in out["treffer"]]
    assert urls[0] == "https://knie" and "https://fussball" not in urls  # label candidate, but Jev says no
    assert out["treffer"][0]["_scores"]["label_match"] is True


def test_mcp_search_old_path_behind_switch(world, monkeypatch):
    called = {}
    real = t.search

    def spy(*a, **kw):
        called["old"] = True
        return real(*a, **kw)

    monkeypatch.setattr(t, "search", spy)
    with Session(world) as s:
        t.search_tool(
            s, Settings(_env_file=None, auto_refresh=False, mcp_search_embeddings=True), "Kreuzband"
        )
    assert called.get("old")


def test_mcp_search_since_widening_on_shared_path(world):
    with Session(world) as s:
        lara = repo.resolve_athlete(s, "Lara Testalder")
        s.execute(
            m.Item.__table__.update()
            .where(m.Item.url == "https://knie")
            .values(published_at=datetime.now(UTC) - timedelta(days=35))
        )
        out = t.search_tool(s, SETTINGS, "Kreuzbandriss", athlete=lara.full_name, since="20d")
    assert out.get("erweitert") and out["treffer"][0]["url"] == "https://knie"


def test_vectors_at_ingest_unless_everything_is_off(db):
    assert SETTINGS.embeddings_on is True  # v2 default: ingest embeds, vector candidates on
    off = Settings(
        _env_file=None,
        ingest_embeddings=False,
        retrieval_vector_candidates=False,
        retrieval_embeddings=False,
        mcp_search_embeddings=False,
    )
    assert off.embeddings_on is False
    with Session(db) as s:
        assert build_context(s, SETTINGS).embedder is not None
        assert build_context(s, off).embedder is None
        assert build_context(s, off).skip_facts is False  # facts do not depend on vectors any more


def test_ingest_with_everything_off_stores_no_embedding(db):
    from klugscheisser.ingest.store import ItemDraft, store_item

    off = Settings(
        _env_file=None, ingest_embeddings=False, retrieval_vector_candidates=False, retrieval_embeddings=False
    )
    with Session(db) as s:
        ctx = build_context(s, off)
        draft = ItemDraft(
            source_type="srf",
            platform="rss",
            url="https://x/1",
            title="Titel",
            text="Ein Text über das Eishockey-Nationalteam.",
            published_at=datetime.now(UTC),
        )
        store_item(ctx, draft, [])
        assert s.scalar(select(func.count()).select_from(m.Embedding)) == 0


def test_search_terms_keep_short_all_caps_words():
    terms = docs.search_terms("USA gegen SUI: ACL gerissen, die Saison ist aus")
    assert {"usa", "sui", "acl"} <= set(terms) and "die" not in terms and "aus" not in terms


def test_top_vector_hits_survive_the_cap(world):
    with Session(world) as s:
        noise = s.scalar(select(m.Item).where(m.Item.url == "https://noise/59"))  # no question word at all
        s.add(m.Embedding(item_id=noise.id, model="stub-384", vector=[1.0] + [0.0] * 383))
        s.flush()
        items, why = docs.candidate_items(
            s, "Kreuzband Knie Saison", Filters(), cap=2, embedder=StubEmbedder()
        )
        assert noise.id in {i.id for i in items} and "vector" in why[noise.id]
        s.rollback()
