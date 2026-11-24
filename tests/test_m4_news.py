from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.rss import DEFAULT_FEEDS, RssCollector, extract_article_excerpt, parse_feed
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest.news import ingest_news
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/rss")
SRF_ONLY = DEFAULT_FEEDS[:1]  # other feeds are not mocked here
FEED_URL = DEFAULT_FEEDS[0].url
ARTICLE_URL = "https://www.srf.ch/sport/eishockey/frauen-eishockey/test-beispiel-trifft-doppelt"


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        testa = repo.upsert_athlete(
            s, repo.get_sport(s, "icehockey_women"), "Testa Beispiel", nationality="SUI"
        )
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata"})
        s.commit()
    return db


def test_parse_feed_keeps_only_hockey_section():
    arts = parse_feed((FIX / "srf_sport.xml").read_bytes(), DEFAULT_FEEDS[0])
    assert [a.url.split("/")[-1] for a in arts] == ["test-beispiel-trifft-doppelt", "test-kloten"]
    assert arts[0].summary == "Die Schweizerinnen gewinnen 3:1."
    assert arts[0].published_at.isoformat().startswith("2026-10-02T17:19")


def test_excerpt_drops_boilerplate_and_keeps_quotes():
    text = extract_article_excerpt((FIX / "article_beispiel.html").read_text())
    assert text.startswith("Das Schweizer Eishockey-Nationalteam")
    assert "«Wir haben heute als Team gespielt, das freut mich am meisten»" in text
    assert "JavaScript" not in text and "SRF-Livestream" not in text and "Zweigniederlassung" not in text


@respx.mock
def test_ingest_news_links_athletes_and_team(world):
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=(FIX / "srf_sport.xml").read_bytes()))
    article = respx.get(ARTICLE_URL).mock(
        return_value=httpx.Response(200, text=(FIX / "article_beispiel.html").read_text())
    )
    with Session(world) as s:
        ctx = build_context(s, Settings(_env_file=None))
        report = ingest_news(ctx, RssCollector(SRF_ONLY, delay_s=0))
        s.commit()
        assert report.womens == 1 and report.items_new == 1 and not report.errors
        item = s.scalar(select(m.Item).where(m.Item.url == ARTICLE_URL))
        assert item.source_type == "srf" and item.source_name == "SRF"
        assert {ia.athlete.full_name for ia in item.athletes} == {"Testa Beispiel", "Lara Stalder"}
        assert [t.team.key for t in item.teams] == ["sui-women"]
        assert item.classification.category == "result"
        assert item.search_tsv is not None

        # second run: article page is not fetched again
        ingest_news(build_context(s, Settings(_env_file=None)), RssCollector(SRF_ONLY, delay_s=0))
        assert article.call_count == 1


def test_kickoff_time_is_not_a_score():
    from klugscheisser.classify.rules import SCORE

    assert SCORE.findall("Spiel am Samstag ab 12:50 Uhr") == []
    assert len(SCORE.findall("Die Schweiz gewinnt 3:1")) == 1
