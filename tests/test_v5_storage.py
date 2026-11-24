"""v5 phases 2+3: Jev topic tags, "Hauptperson", light/full documents, multilingual full text."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from klugscheisser.adds.load import link_existing_items
from klugscheisser.answer import engine as eng
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.embed.providers import FakeEmbedder
from klugscheisser.ingest.runner import build_context
from klugscheisser.retrieval.search import Filters, hybrid_search, list_items

SETTINGS = Settings(_env_file=None, auto_refresh=False)


def add_item(s, url, text, topics=None, level="full", person=None, main=None):
    item = m.Item(
        source_type="srf",
        platform="rss",
        url=url,
        title=text[:40],
        text=text,
        content_hash=url,
        published_at=datetime.now(UTC),
        level=level,
    )
    s.add(item)
    s.flush()
    if level == "full":
        item.classification = m.Classification(
            relevant_p=0.9,
            relevant=True,
            category="other",
            sensitive_p=0.1,
            newsworthiness=2,
            classifier="t",
            classifier_version="1",
            topics=topics or {},
        )
    if person is not None:
        item.athletes.append(m.ItemAthlete(athlete_id=person.id, main=main))
    s.flush()
    return item


def test_question_topics():
    assert eng.question_topics("Wer studiert neben dem Eishockey?") == ["studium", "beruf"]
    assert eng.question_topics("Welche Spielerinnen sind verletzt?") == ["verletzung"]
    assert eng.question_topics("Wie heisst ihr Hund?") == []


def test_topic_filter_level_and_main_person(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.upsert_athlete(s, sport, "Lara Testalder")
        add_item(s, "https://studi", "Sie studiert Psychologie.", {"studium": 0.92}, person=lara, main=True)
        add_item(s, "https://kader", "Kaderliste mit 23 Namen.", {"studium": 0.1}, person=lara, main=False)
        add_item(s, "https://light", "Golf: ein Golfer studiert.", level="light")
        urls = {i.url for i in list_items(s, Filters(topics=["studium"]))}
        assert urls == {"https://studi"}
        assert "https://light" not in {i.url for i in list_items(s, Filters())}
        assert "https://light" in {i.url for i in list_items(s, Filters(include_light=True))}
        kader = next(i for i in list_items(s, Filters()) if i.url == "https://kader")
        assert "Lara Testalder (nur erwähnt)" in eng.item_doc(kader).meta
        # a question about studies finds the tagged document for everyone in our database
        found = {i.url for i in eng.candidate_items(s, SETTINGS, "Wer studiert?", [], None)}
        assert "https://studi" in found and "https://light" not in found


def test_light_document_promoted_when_person_is_added(db):
    with Session(db) as s:
        add_item(s, "https://teaser", "Hanna Testsson wechselt nach Zug.", level="light")
        sport = repo.get_sport(s, "icehockey_women")
        hanna = repo.upsert_athlete(s, sport, "Hanna Testsson")
        assert link_existing_items(s, [hanna.id], ctx=build_context(s, SETTINGS)) == 1
        item = s.query(m.Item).filter_by(url="https://teaser").one()
        assert item.level == "full" and item.classification is not None
        assert item.classification.topics.get("transfer") == 0.7  # rules fallback tags it


def test_multilingual_full_text_finds_swedish_words(db):
    with Session(db) as s:
        add_item(s, "https://svt", "Hanna Olsson skadade knäet i matchen mot Luleå.")
        add_item(s, "https://de", "Ein ganz anderer Bericht über Fussball.")
        hits = hybrid_search(s, FakeEmbedder(), "knäet skadade", Filters(), k=5)
        assert hits and hits[0].item.url == "https://svt" and hits[0].scores.get("fulltext")
