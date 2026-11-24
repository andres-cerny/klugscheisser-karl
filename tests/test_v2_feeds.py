"""v2 phase C: more news feeds (Blick, 20 Minuten, Watson, NZZ). Synthetic fixtures in each outlet's structure."""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.rss import (
    DEFAULT_FEEDS,
    EXCERPT_MAX,
    Article,
    RssCollector,
    extract_article_excerpt,
    is_womens_hockey,
    parse_feed,
)
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest.news import ingest_news
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/rss")
V2_FEEDS = DEFAULT_FEEDS[:5]  # the five v2 outlets; v5 feeds are tested in test_v5_news.py
FEEDS = {f.name: f for f in V2_FEEDS}
FIXTURE_OF = {
    "SRF": "srf_sport.xml",
    "Blick": "blick_eishockey.xml",
    "20 Minuten": "20min_eishockey.xml",
    "Watson": "watson_sport.xml",
    "NZZ": "nzz_eishockey.xml",
}
BLICK_WOMEN = (
    "https://www.blick.ch/sport/eishockey/frauen-nati-beispiel-schiesst-die-schweiz-zum-sieg/t3stb1k"
)
BLICK_NAME_ONLY = "https://www.blick.ch/sport/eishockey/beispiel-ueber-ihr-neues-team/t3stb2k"
MIN20 = "https://www.20min.ch/story/eishockey-frauen-hockeyanerin-beispiel-wechselt-in-die-pwhl-100000001"
WATSON = "https://www.watson.ch/sport/eishockey/100000011-womens-league-beispiel-trifft-beim-spitzenkampf"
NZZ = "https://www.nzz.ch/sport/eishockey/das-schweizer-frauen-eishockey-sucht-seinen-platz-ld.1000001"


def parse(name: str):
    return parse_feed((FIX / FIXTURE_OF[name]).read_bytes(), FEEDS[name])


def art(title: str, summary: str = "", url: str = "https://example.ch/sport/x") -> Article:
    return Article("T", "other_media", url, "media", title, summary, None)


# --- config -----------------------------------------------------------------------------------------------


def test_feed_config_kinds_and_article_policy():
    assert list(FEEDS) == ["SRF", "Blick", "20 Minuten", "Watson", "NZZ"]
    srf = FEEDS["SRF"]
    assert (srf.url, srf.source_kind, srf.source_type, srf.section_filter, srf.fetch_article) == (
        "https://www.srf.ch/sport/bnf/rss/718",
        "srf",
        "srf",
        "/eishockey/",
        True,
    )
    assert {n: f.source_kind for n, f in FEEDS.items() if n != "SRF"} == {
        "Blick": "boulevard",
        "20 Minuten": "boulevard",
        "Watson": "boulevard",
        "NZZ": "media",
    }
    others = [f for n, f in FEEDS.items() if n != "SRF"]
    assert all(f.source_type == "other_media" and f.section_filter for f in others)
    # terms of use forbid automated article reading for all new outlets -> teaser only
    assert not any(f.fetch_article for f in others)


# --- parsing + section filter ------------------------------------------------------------------------------


def test_blick_section_filter_drops_other_sports():
    arts = parse("Blick")
    assert [a.url.split("/")[-1] for a in arts] == [
        "t3stb1k",
        "t3stb2k",
        "t3stb3k",
    ]  # /sport/fussball/ dropped
    assert arts[0].source_kind == "boulevard" and arts[0].feed == "Blick"
    assert arts[0].summary.startswith("Die Frauen-Nati gewinnt")
    assert arts[0].published_at.isoformat() == "2026-10-03T12:05:01+00:00"


def test_20min_skips_empty_placeholder_items():
    arts = parse("20 Minuten")
    assert [a.url for a in arts] == [
        MIN20,
        "https://www.20min.ch/story/national-league-zsc-dreht-das-spiel-100000002",
    ]
    assert arts[0].summary == "Die Schweizer Stürmerin unterschreibt einen Zweijahresvertrag in Nordamerika."


def test_watson_atom_uses_description_and_sport_section():
    arts = parse("Watson")
    assert [a.url for a in arts] == [
        WATSON,
        "https://www.watson.ch/sport/fussball/100000012-brasilien-steht-im-achtelfinal",
    ]
    assert arts[0].summary == "Die Nati-Stürmerin entscheidet das Spiel in der Verlängerung."
    assert "lesen" not in arts[0].summary
    assert arts[0].published_at.isoformat() == "2026-10-02T14:00:48+00:00"


def test_nzz_keeps_sport_items_as_media():
    arts = parse("NZZ")
    assert len(arts) == 2 and {a.source_kind for a in arts} == {"media"}


# --- women's filter ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Frauen-Nati verliert gegen Kanada",
        "Eishockey-Frauen: Neue Trainerin",
        "Women's League: Zug gewinnt den Spitzenkampf",
        "Hockeyanerin wechselt nach Schweden",
        "Nati-Spielerin im Eishockey verletzt",
        "Hockey: Die Schweizerinnen siegen",
    ],
)
def test_womens_terms_from_new_outlets(title):
    assert is_womens_hockey(art(title, url="https://www.blick.ch/sport/eishockey/x"))


@pytest.mark.parametrize(
    "title,summary",
    [
        ("Nati-Spielerin fällt aus", "Die Fussballerin verpasst die WM-Playoffs."),  # women's football
        ("Servette-Topskorer wird gesperrt", "Der Stürmer fehlt im Eishockey zwei Spiele."),  # men's hockey
    ],
)
def test_filter_rejects_womens_football_and_mens_hockey(title, summary):
    assert not is_womens_hockey(art(title, summary))


# --- excerpts per outlet -----------------------------------------------------------------------------------


def test_blick_excerpt_from_article_paragraphs():
    text = extract_article_excerpt((FIX / "blick_article.html").read_text())
    paras = text.split("\n")
    assert paras[0].startswith("Die Schweizer Frauen-Nati gewinnt in Kloten")
    assert "«Ich habe einfach geschossen und gehofft»" in text
    assert "in der 14. Minute" in text
    assert (
        "Ringier" not in text and "Blick-App" not in text and "Kurz" not in paras
    )  # outside <article> / short


def test_20min_excerpt_from_json_ld():
    text = extract_article_excerpt((FIX / "20min_article.html").read_text())
    assert text.split("\n")[0].startswith("Testa Beispiel verlässt die Women's League")
    assert "«Das ist ein Kindheitstraum, der in Erfüllung geht»" in text
    assert len(text.split("\n")) == 2
    assert "Gerenderter Text" not in text and "Fehler gefunden" not in text


def test_watson_excerpt_from_json_ld_html_body():
    text = extract_article_excerpt((FIX / "watson_article.html").read_text())
    lines = text.split("\n")
    assert lines[0].startswith("Die ZSC Lions gewinnen den Spitzenkampf")
    assert "Telegramm: ZSC Lions - Lugano 3:2" in lines[-1]
    assert "<br>" not in text and "<strong>" not in text
    assert "watson-App" not in text and "Snippet-Absatz" not in text


def test_srf_excerpt_still_works():
    text = extract_article_excerpt((FIX / "article_beispiel.html").read_text())
    assert text.startswith("Das Schweizer Eishockey-Nationalteam") and "SRF-Livestream" not in text


def test_long_excerpt_never_cut_mid_sentence_or_after_ordinal():
    sentence = "Die Schweiz führt nach dem Mitteldrittel verdient mit zwei Toren Vorsprung. "
    tail = (
        "Das Tor fiel in der 14. Minute" + " und das Spiel ging weiter" * 12 + "."
    )  # 14. inside, end outside
    n = (EXCERPT_MAX - 60) // len(sentence)
    page = f"<article><p>{sentence * n}{tail}{' Noch mehr Text.' * 40}</p></article>"
    text = extract_article_excerpt(page)
    assert text.endswith("Vorsprung. […]") and len(text) <= EXCERPT_MAX + 4


# --- ingest ------------------------------------------------------------------------------------------------


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Testa Beispiel", nationality="SUI")
        s.commit()
    return db


def mock_feeds(router, failing: str | None = None):
    for name, fixture in FIXTURE_OF.items():
        ok = httpx.Response(200, content=(FIX / fixture).read_bytes())
        router.get(FEEDS[name].url).mock(return_value=httpx.Response(503) if name == failing else ok)
    router.get(url__regex=r".*test-beispiel-trifft-doppelt").mock(
        return_value=httpx.Response(200, text=(FIX / "article_beispiel.html").read_text())
    )


def test_ingest_all_feeds_sets_source_kind_and_type(world):
    with respx.mock(assert_all_called=False) as router, Session(world) as s:
        mock_feeds(router)
        report = ingest_news(build_context(s, Settings(_env_file=None)), RssCollector(V2_FEEDS, delay_s=0))
        s.commit()
        assert not report.errors
        rss = list(s.scalars(select(m.Item).where(m.Item.platform == "rss")))
        items = {i.url: i for i in rss if i.level == "full"}
        assert set(items) >= {BLICK_WOMEN, BLICK_NAME_ONLY, MIN20, WATSON, NZZ}
        assert not any("servette" in u or "zsc-dreht" in u or "brasilien" in u for u in items)
        # v5: the others are kept as light documents (title/teaser/link), not classified
        light = [i for i in rss if i.level == "light"]
        assert any("servette" in i.url for i in light) and all(i.classification is None for i in light)
        kinds = {u: (i.source_kind, i.source_type, i.source_name) for u, i in items.items()}
        assert kinds[BLICK_WOMEN] == ("boulevard", "other_media", "Blick")
        assert kinds[MIN20] == ("boulevard", "other_media", "20 Minuten")
        assert kinds[WATSON] == ("boulevard", "other_media", "Watson")
        assert kinds[NZZ] == ("media", "other_media", "NZZ")
        srf = next(i for u, i in items.items() if "srf.ch" in u)
        assert (srf.source_kind, srf.source_type) == ("srf", "srf")
        # name-only item (no women's term) is kept because a known athlete appears
        assert "Testa Beispiel" in {ia.athlete.full_name for ia in items[BLICK_NAME_ONLY].athletes}
        # teaser only for outlets without article permission
        assert (
            items[MIN20].text
            == "Die Schweizer Stürmerin unterschreibt einen Zweijahresvertrag in Nordamerika."
        )
        # only the feeds and the SRF article page were requested, no article page of the new outlets
        requested = {str(c.request.url) for c in router.calls}
        assert requested - {f.url for f in V2_FEEDS} == {
            "https://www.srf.ch/sport/eishockey/frauen-eishockey/test-beispiel-trifft-doppelt"
        }


def test_article_fetched_once_when_allowed(world):
    blick = replace(FEEDS["Blick"], fetch_article=True)  # e.g. after written permission
    with respx.mock(assert_all_called=False) as router, Session(world) as s:
        router.get(blick.url).mock(
            return_value=httpx.Response(200, content=(FIX / "blick_eishockey.xml").read_bytes())
        )
        page = router.get(BLICK_WOMEN).mock(
            return_value=httpx.Response(200, text=(FIX / "blick_article.html").read_text())
        )
        router.get(BLICK_NAME_ONLY).mock(return_value=httpx.Response(404))
        ingest_news(build_context(s, Settings(_env_file=None)), RssCollector([blick], delay_s=0))
        s.commit()
        item = s.scalar(select(m.Item).where(m.Item.url == BLICK_WOMEN))
        assert "«Ich habe einfach geschossen und gehofft»" in item.text
        assert item.source_kind == "boulevard"
        ingest_news(build_context(s, Settings(_env_file=None)), RssCollector([blick], delay_s=0))
        assert page.call_count == 1  # known article is not fetched again


def test_one_failing_feed_does_not_stop_the_others(world):
    with respx.mock(assert_all_called=False) as router, Session(world) as s:
        mock_feeds(router, failing="Watson")
        report = ingest_news(build_context(s, Settings(_env_file=None)), RssCollector(V2_FEEDS, delay_s=0))
        assert report.items_new == 5  # SRF 1, Blick 2, 20 Minuten 1, NZZ 1
        assert len(report.errors) == 1 and report.errors[0].startswith("feed Watson")
