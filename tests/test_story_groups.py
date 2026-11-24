"""Story groups (retrieval/stories.py): the same story from several outlets becomes one search entry.
Switches: SEARCH_GROUP_STORIES (MCP suche/search, default on), CHAT_GROUP_STORIES (answer engine, default off)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import engine
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.present import render_markdown
from klugscheisser.retrieval import docs, stories
from klugscheisser.retrieval.search import Filters

SETTINGS = Settings(_env_file=None, auto_refresh=False, retrieval_vector_candidates=False)
MODEL = f"{SETTINGS.chunk_embedding_model}-{SETTINGS.chunk_embedding_dim}"
NOW = datetime.now(UTC)
STORY = [1.0] + [0.0] * 767  # the coach appointment
STORY2 = [0.99, 0.141] + [0.0] * 766  # same story, slightly other words (cos 0.99)
OTHER = [0.0, 1.0] + [0.0] * 766  # another story about the same person


def add(s, key, title, *, person=None, hours_ago=0, vec=None, source="SRF", kind="srf", stype="other_media"):
    item = m.Item(
        source_type=stype,
        source_kind=kind,
        platform="rss",
        source_name=source,
        url=f"https://x/{key}",
        title=title,
        text=f"{title}. Die Kanadierin Miller wird Trainerin der Schweizer Frauen-Nati.",
        content_hash=key,
        published_at=NOW - timedelta(hours=hours_ago),
    )
    s.add(item)
    s.flush()
    if person is not None:
        item.athletes.append(m.ItemAthlete(athlete_id=person.id))
    if vec is not None:
        s.add(
            m.ItemChunk(item_id=item.id, model=MODEL, ord=0, start=0, text=title, text_hash="h", vector=vec)
        )
    s.flush()
    return item


@pytest.fixture
def newsroom(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        miller = repo.upsert_athlete(s, sport, "Shannon Testmiller")
        add(s, "srf", "Miller wird Nati-Trainerin", person=miller, vec=STORY)
        add(
            s,
            "rts",
            "Miller nouvelle entraîneure",
            person=miller,
            hours_ago=3,
            vec=STORY2,
            source="RTS",
            kind="public",
        )
        add(
            s,
            "blick",
            "Miller übernimmt die Nati",
            person=miller,
            hours_ago=20,
            vec=STORY,
            source="Blick",
            kind="boulevard",
        )
        add(
            s,
            "fed",
            "Swiss Ice Hockey ernennt Miller",
            person=miller,
            hours_ago=5,
            vec=STORY,
            source="Swiss Ice Hockey",
            kind="official",
        )
        add(
            s, "game", "Miller gewinnt ihr erstes Spiel", person=miller, hours_ago=6, vec=OTHER
        )  # other story
        add(
            s, "late", "Miller-Wahl: Rückblick nach vier Tagen", person=miller, hours_ago=96, vec=STORY
        )  # too late
        add(
            s,
            "wiki",
            "Shannon Testmiller",
            person=miller,
            hours_ago=1,
            vec=STORY,
            source="Wikipedia (de)",
            stype="profile",
            kind="media",
        )
        add(
            s,
            "ticker",
            "Sportnews-Ticker: Miller wird Trainerin",
            person=miller,
            hours_ago=2,
            vec=STORY,
            source="watson",
            kind="media",
        )
        s.commit()
    return db


def _items(s):
    return {i.url.rsplit("/", 1)[1]: i for i in s.query(m.Item).all()}


def test_switch_defaults():
    st = Settings(_env_file=None)
    assert st.search_group_stories is True and st.chat_group_stories is False


def test_groupable_never_profiles_results_digests(newsroom):
    with Session(newsroom) as s:
        it = _items(s)
        assert stories.groupable(it["srf"])
        assert not stories.groupable(it["wiki"])  # profile / Wikipedia
        assert not stories.groupable(it["ticker"])  # multi-topic ticker
        it["srf"].source_type = "official_result"
        assert not stories.groupable(it["srf"])
        s.rollback()


def test_group_same_person_close_in_time_similar_vectors(newsroom):
    with Session(newsroom) as s:
        it = _items(s)
        order = ["srf", "rts", "game", "blick", "fed", "late", "wiki", "ticker"]
        items = [it[k] for k in order]
        scores = [0.9, 0.9, 0.85, 0.8, 0.9, 0.8, 0.8, 0.8]
        groups = stories.group(s, SETTINGS, items, scores)
        named = [[order[i] for i in g] for g in groups]
        # one story group: score tie (0.9) -> the federation (primary source) first, then SRF, RTS
        assert named[0] == ["fed", "srf", "rts", "blick"]
        for alone in ("game", "late", "wiki", "ticker"):
            assert [alone] in named  # other story, 4 days later, profile, ticker: never folded
        s.rollback()


def test_other_person_is_not_folded(newsroom):
    with Session(newsroom) as s:
        it = _items(s)
        other = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Alina Testmüller")
        it["rts"].athletes.clear()
        it["rts"].athletes.append(m.ItemAthlete(athlete_id=other.id))
        s.flush()
        groups = stories.group(s, SETTINGS, [it["srf"], it["rts"]], [0.9, 0.8])
        assert groups == [[0], [1]]
        s.rollback()


def test_also_line_lists_every_id():
    line = stories.also_line(
        [
            {"id": i, "quelle": q, "url": "u", "datum": None}
            for i, q in enumerate(["SRF", "RTS", "Blick", "TA", "NZZ"], 1)
        ]
    )
    assert line == "Auch berichtet von: SRF, RTS, Blick (+2) – dokument(1, 2, 3, 4, 5)"


class FixedReranker:
    name = "fixed"

    def __init__(self, scores):
        self.scores = scores

    def score(self, query, texts):
        return [next((v for k, v in self.scores.items() if k in tx), 0.5) for tx in texts]


RR = FixedReranker(
    {"Nati-Trainerin": 0.95, "nouvelle": 0.9, "übernimmt": 0.85, "ernennt": 0.8, "erstes Spiel": 0.7}
)


def test_find_documents_switch(newsroom):
    with Session(newsroom) as s:
        off = docs.find_documents(s, SETTINGS, "Miller Trainerin", Filters(), top_n=3, reranker=RR)
        assert [h.item.title for h in off][:2] == [
            "Miller wird Nati-Trainerin",
            "Miller nouvelle entraîneure",
        ]
        assert not any("auch" in h.scores for h in off)
        on = docs.find_documents(
            s, SETTINGS, "Miller Trainerin", Filters(), top_n=3, reranker=RR, group_stories=True
        )
        assert on[0].item.title == "Miller wird Nati-Trainerin"
        assert {e["quelle"] for e in on[0].scores["auch"]} == {"RTS", "Blick", "Swiss Ice Hockey"}
        assert "Miller gewinnt ihr erstes Spiel" in [h.item.title for h in on]  # freed place: next story
        assert len(on) == 3  # list stays full
        s.rollback()


@pytest.mark.parametrize("switch", [True, False])
def test_mcp_search_cards_follow_switch(newsroom, monkeypatch, switch):
    monkeypatch.setattr(t, "freshen", lambda s, athletes: {"status": "test"})
    real = docs.find_documents
    monkeypatch.setattr(t, "find_documents", lambda *a, **k: real(*a, **{**k, "reranker": RR}))
    st = SETTINGS.model_copy(update={"search_group_stories": switch})
    with Session(newsroom) as s:
        text = render_markdown(t.search_tool(s, st, "Miller Trainerin", limit=4, cards=True))
        assert ("Auch berichtet von: " in text) is switch
        assert (text.count("**[") == 4) and (("Miller nouvelle entraîneure**" in text) is not switch)
        old = t.search_tool(s, st, "Miller Trainerin", limit=4, cards=False)  # old list output
        assert any("auch_berichtet" in x for x in old["treffer"]) is switch
        s.rollback()


def _chat_docs(s):
    it = _items(s)
    out = []
    for k, sc in (("srf", 0.9), ("rts", 0.8), ("game", 0.7)):
        d = engine.item_doc(it[k])
        d.score = sc
        out.append(d)
    out.append(engine.Doc(key="", text="berechnet", meta="", url="", quelle="", zeile_prefix="", score=0.6))
    return out


def test_chat_unchanged_with_default_switch(newsroom):
    """Web chat (and MCP frage) keep their document list unless CHAT_GROUP_STORIES is set."""
    with Session(newsroom) as s:
        d = _chat_docs(s)
        assert engine.fold_stories(s, Settings(_env_file=None), d) is d
        s.rollback()


def test_chat_folds_with_switch_on(newsroom):
    with Session(newsroom) as s:
        d = _chat_docs(s)
        out = engine.fold_stories(s, SETTINGS.model_copy(update={"chat_group_stories": True}), d)
        assert [x.quelle for x in out][:1] == [d[0].quelle] and len(out) == 3  # RTS folded, computed doc kept
        assert out[0].extra["auch"][0]["quelle"] == "RTS"
        assert out[-1].text == "berechnet"
        s.rollback()


def test_grouping_failure_keeps_list(newsroom, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no table")

    monkeypatch.setattr(stories, "similarities", boom)
    with Session(newsroom) as s:
        it = _items(s)
        hits = [docs.Hit(it["srf"], 0.9, {}), docs.Hit(it["rts"], 0.8, {})]
        assert docs.fold_hits(s, SETTINGS, hits) == hits
        s.rollback()
