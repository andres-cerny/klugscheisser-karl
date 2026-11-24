"""v3 P4: Brave web search, allowed-page fetching, storing verified pages. Offline (respx + fixtures)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.rss import USER_AGENT
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest import costs
from klugscheisser.ingest.runner import build_context
from klugscheisser.interfaces import WebPage
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web_search import get_web_search, web_search_enabled
from klugscheisser.web_search.brave import (
    BRAVE_URL,
    NOTE_BLOCKED,
    NOTE_ROBOTS,
    NOTE_TERMS,
    BraveSearch,
    FetchedPage,
)
from klugscheisser.web_search.domains import is_blocked, registrable_domain
from klugscheisser.web_search.extract import extract_main_text, extract_meta, truncate_sentences
from klugscheisser.web_search.fake import FakeWebSearch
from klugscheisser.web_search.store import store_web_page

FIX = Path("tests/fixtures/web")
PAGES = FIX / "pages"
SRF_URL = "https://www.srf.ch/sport/eishockey/testa-beispiel-studium"
BLOG_URL = "https://hockeyblog.example.org/2026/09/testa"
COLLEGE_URL = "https://examplestateathletics.com/sports/womens-ice-hockey/roster/testa-beispiel/1234"


def settings(**kw) -> Settings:
    return Settings(_env_file=None, brave_api_key="test-key", **kw)


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        repo.upsert_athlete(s, sport, "Testa Beispiel", nationality="SUI")
        repo.upsert_athlete(s, sport, "Prova Esempio", nationality="SUI")
        s.commit()
    return db


def athlete_ids(s: Session) -> list[int]:
    names = ("Testa Beispiel", "Prova Esempio")
    return [s.scalar(select(m.Athlete.id).where(m.Athlete.full_name == n)) for n in names]


# --- settings / factory ---------------------------------------------------------------------------


def test_enabled_only_with_key():
    assert not web_search_enabled(Settings(_env_file=None))
    assert get_web_search(Settings(_env_file=None)) is None
    assert web_search_enabled(settings())
    assert not web_search_enabled(settings(brave_daily_cap=0))
    assert isinstance(get_web_search(settings()), BraveSearch)


# --- search ---------------------------------------------------------------------------------------


@respx.mock
def test_brave_request_format_and_parsing(world):
    route = respx.get(BRAVE_URL).mock(
        return_value=httpx.Response(200, json=json.loads((FIX / "brave_response.json").read_text()))
    )
    with Session(world) as s:
        results = BraveSearch(settings(), session=s).search("Testa Beispiel Eishockey Studium", limit=5)
        req = route.calls.last.request
        assert req.headers["X-Subscription-Token"] == "test-key"
        assert req.headers["Accept"] == "application/json"
        assert req.url.params["q"] == "Testa Beispiel Eishockey Studium"
        assert req.url.params["country"] == "CH" and req.url.params["search_lang"] == "de"
        assert 1 <= int(req.url.params["count"]) <= 20
        # blocked domains (eliteprospects, instagram, flashscore.*) are dropped, HTML highlights stripped
        assert [r.url for r in results] == [SRF_URL, COLLEGE_URL, BLOG_URL]
        assert results[0].title == "Testa Beispiel – Studium und Eishockey"
        assert results[0].snippet == "Die Stürmerin Testa Beispiel studiert neben dem Eishockey Psychologie."
        # cost recorded as source "brave"
        row = s.scalar(select(m.CostLedger).where(m.CostLedger.source == "brave"))
        assert row.units == 1 and row.cost_usd == pytest.approx(0.005)


@respx.mock
def test_brave_limit_trims_results(world):
    respx.get(BRAVE_URL).mock(
        return_value=httpx.Response(200, json=json.loads((FIX / "brave_response.json").read_text()))
    )
    with Session(world) as s:
        assert len(BraveSearch(settings(), session=s).search("x", limit=1)) == 1


@respx.mock
def test_daily_cap_skips_search(world):
    route = respx.get(BRAVE_URL).mock(return_value=httpx.Response(200, json={"web": {"results": []}}))
    with Session(world) as s:
        brave = BraveSearch(settings(brave_daily_cap=2), session=s)
        assert brave.search("a") == [] and brave.search("b") == []
        assert route.call_count == 2
        assert brave.search("c") == []
        assert route.call_count == 2  # cap reached: no request
        assert "Tageslimit" in brave.last_note
        assert costs.spent_today(s, "brave") == pytest.approx(0.01)


@respx.mock
def test_failed_request_is_not_charged(world):
    respx.get(BRAVE_URL).mock(return_value=httpx.Response(429, json={"error": "rate limited"}))
    with Session(world) as s:
        with pytest.raises(httpx.HTTPStatusError):
            BraveSearch(settings(), session=s).search("x")
        assert s.scalar(select(func.count()).select_from(m.CostLedger)) == 0


def test_search_without_key_raises(world):
    with Session(world) as s, pytest.raises(RuntimeError):
        BraveSearch(Settings(_env_file=None), session=s).search("x")


# --- blocklist / robots / terms -------------------------------------------------------------------


def test_blocklist_patterns():
    blocked = Settings(_env_file=None).web_blocked_domains
    assert is_blocked("https://www.flashscore.ch/team/x", blocked)
    assert is_blocked("https://www.flashscore.co.uk/x", blocked)
    assert is_blocked("https://m.livesport.cz/x", blocked)
    assert is_blocked("https://x.com/someone", blocked)
    assert is_blocked("https://mobile.twitter.com/someone", blocked)
    assert not is_blocked("https://www.box.com/x", blocked)  # "x.com" is not a suffix match of box.com
    assert not is_blocked("https://www.srf.ch/sport", blocked)
    assert registrable_domain("https://partner-feeds.20min.ch/rss") == "20min.ch"
    assert registrable_domain("https://news.bbc.co.uk/a") == "bbc.co.uk"


@respx.mock
def test_fetch_refuses_blocked_domain_without_request():
    page = BraveSearch(settings()).fetch("https://www.eliteprospects.com/player/1/x")
    assert page.allowed is False and page.text is None and page.note == NOTE_BLOCKED
    assert not respx.calls


@respx.mock
def test_fetch_refuses_outlet_whose_terms_forbid_reading():
    page = BraveSearch(settings()).fetch("https://www.blick.ch/sport/eishockey/x-id1.html")
    assert page.allowed is False and page.note == NOTE_TERMS
    assert not respx.calls


@respx.mock
def test_fetch_respects_robots_disallow_and_caches_robots():
    robots = respx.get("https://hockeyblog.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    )
    blog = respx.get(BLOG_URL).mock(
        return_value=httpx.Response(
            200, text=(PAGES / "blog_article.html").read_text(), headers={"content-type": "text/html"}
        )
    )
    brave = BraveSearch(settings())
    page = brave.fetch("https://hockeyblog.example.org/private/secret")
    assert page.allowed is False and page.note == NOTE_ROBOTS and page.text is None
    ok = brave.fetch(BLOG_URL)
    assert ok.allowed is True and ok.text.startswith("Testa Beispiel hatte nach ihrer Knieverletzung")
    assert robots.call_count == 1  # cached per host
    assert blog.calls.last.request.headers["User-Agent"] == USER_AGENT


@respx.mock
def test_fetch_robots_names_our_agent():
    respx.get("https://hockeyblog.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: klugscheisser-athlete\nDisallow: /\n")
    )
    assert BraveSearch(settings()).fetch(BLOG_URL).note == NOTE_ROBOTS


@respx.mock
def test_fetch_robots_missing_allows_and_unreachable_refuses():
    respx.get("https://hockeyblog.example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(BLOG_URL).mock(return_value=httpx.Response(200, text=(PAGES / "blog_article.html").read_text()))
    assert BraveSearch(settings()).fetch(BLOG_URL).allowed is True
    respx.get("https://down.example.org/robots.txt").mock(return_value=httpx.Response(503))
    page = BraveSearch(settings()).fetch("https://down.example.org/a")
    assert page.allowed is False and page.text is None


@respx.mock
def test_fetch_checks_every_redirect_hop():
    respx.get("https://short.example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://short.example.org/t").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.instagram.com/testa.beispiel/"})
    )
    page = BraveSearch(settings()).fetch("https://short.example.org/t")
    assert page.allowed is False and page.note == NOTE_BLOCKED


@respx.mock
def test_fetch_skips_non_html_and_caps_size():
    respx.get("https://files.example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://files.example.org/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    )
    page = BraveSearch(settings()).fetch("https://files.example.org/a.pdf")
    assert page.allowed is True and page.text is None and "kein HTML" in page.note

    paras = "".join(f"<p>Testa Beispiel spielt seit {i} Jahren Eishockey in Zug.</p>" for i in range(5000))
    big = "<html><body><main>" + paras
    respx.get("https://files.example.org/big").mock(
        return_value=httpx.Response(200, text=big, headers={"content-type": "text/html; charset=utf-8"})
    )
    page = BraveSearch(settings(web_fetch_max_bytes=20_000)).fetch("https://files.example.org/big")
    assert page.text and len(page.text) <= 8000 + 4 and page.text.endswith("Zug. […]")


# --- text extraction ------------------------------------------------------------------------------


def test_extract_json_ld_article_first():
    page = (PAGES / "news_jsonld.html").read_text()
    text = extract_main_text(page)
    assert text.splitlines() == [
        "Testa Beispiel studiert seit diesem Herbst Psychologie an der Universität Zürich.",
        "«Das Studium gibt mir einen Ausgleich zum Eishockey», sagt die 24-jährige Stürmerin.",
        "Neben dem Studium trainiert sie fünfmal pro Woche mit dem EV Zug.",
    ]
    meta = extract_meta(page)
    assert meta.title == "Testa Beispiel – Studium und Eishockey"
    assert meta.site_name == "Schweizer Radio und Fernsehen (SRF)"
    assert meta.published_at == datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
    assert meta.language == "de"


def test_extract_article_paragraphs_without_scripts_and_chrome():
    page = (PAGES / "blog_article.html").read_text()
    text = extract_main_text(page)
    assert text.splitlines() == [
        "Testa Beispiel hatte nach ihrer Knieverletzung im Frühling ein starkes Comeback in der "
        "Nationalmannschaft.",
        "In der 14. Minute erzielte sie gegen Finnland das 1:0, ihr erstes Tor seit der Rückkehr aufs Eis.",
    ]
    meta = extract_meta(page)
    assert meta.published_at == datetime(2026, 9, 15, 10, 30, tzinfo=UTC)
    assert meta.site_name == "Hockeyblog"


def test_extract_profile_page_block_text():
    page = (PAGES / "college_profile.html").read_text()
    text = extract_main_text(page)
    lines = text.splitlines()
    assert "Testa Beispiel" in lines and "Psychology" in lines and "Zug, Switzerland" in lines
    assert "2025-26 · 34 · 12 · 18" in lines
    assert "Majoring in psychology with a minor in sports management." in text
    assert "Tickets" not in text and "All rights reserved" not in text and "Search" not in text


def test_truncate_never_cuts_mid_sentence():
    text = " ".join(f"Satz Nummer {i} endet in der {i}. Minute hier." for i in range(400))
    out = truncate_sentences(text, 500)
    assert len(out) <= 504 and out.endswith("hier. […]")
    assert truncate_sentences("kurz.", 500) == "kurz."


# --- fake -----------------------------------------------------------------------------------------


def test_fake_web_search_is_deterministic():
    fake = FakeWebSearch(FIX)
    results = fake.search("Testa Beispiel Studium")
    assert results[0].url == SRF_URL
    assert fake.search("unbekannt") == []
    page = fake.fetch(SRF_URL)
    assert page.allowed and page.text.startswith("Testa Beispiel studiert")
    refused = fake.fetch("https://www.blick.ch/sport/eishockey/testa-beispiel-id1.html")
    assert refused.allowed is False and refused.text is None


# --- store ----------------------------------------------------------------------------------------


def test_store_web_page_idempotent_with_links_and_kind(world):
    fake = FakeWebSearch(FIX)
    with Session(world) as s:
        ctx = build_context(s, Settings(_env_file=None))
        ids = athlete_ids(s)
        item = store_web_page(ctx, fake.fetch(SRF_URL), ids, "Testa Beispiel Studium")
        s.commit()
        assert item.source_type == "web" and item.platform == "web"
        assert item.source_kind == "srf" and item.source_name == "SRF"
        assert item.title == "Testa Beispiel – Studium und Eishockey"
        assert item.published_at == datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
        assert item.text.startswith("Testa Beispiel studiert seit diesem Herbst")
        assert item.raw["query"] == "Testa Beispiel Studium"
        assert {(ia.athlete_id, ia.match_kind, ia.confidence) for ia in item.athletes} == {
            (ids[0], "web", 0.8),
            (ids[1], "web", 0.8),
        }
        assert item.classification is not None

        again = store_web_page(ctx, fake.fetch(SRF_URL), ids[:1], "andere Frage")
        s.commit()
        assert again.id == item.id
        assert s.scalar(select(func.count()).select_from(m.Item).where(m.Item.url == SRF_URL)) == 1
        assert len(again.athletes) == 2

        blog = store_web_page(ctx, fake.fetch(BLOG_URL), ids[:1], "Comeback")
        assert blog.source_kind == "web" and blog.source_name == "Hockeyblog"
        college = store_web_page(ctx, fake.fetch(COLLEGE_URL), ids[:1], "Studium")
        assert college.source_kind == "web"
        assert college.source_name == "Example State University Athletics"


def test_store_web_page_skips_not_allowed(world):
    with Session(world) as s:
        ctx = build_context(s, Settings(_env_file=None))
        page = WebPage(url="https://www.blick.ch/a", title="a", text=None, allowed=False, note=NOTE_TERMS)
        assert store_web_page(ctx, page, athlete_ids(s), "q") is None
        empty = WebPage(url="https://example.org/a", title="a", text=None, allowed=True)
        assert store_web_page(ctx, empty, athlete_ids(s), "q") is None


def test_store_web_page_kind_from_seed_domains(world):
    with Session(world) as s:
        ctx = build_context(s, Settings(_env_file=None))
        for url, kind in [
            ("https://www.blick.ch/sport/a", "boulevard"),
            ("https://www.nzz.ch/sport/a", "media"),
            ("https://www.sihf.ch/de/a", "official"),
        ]:
            page = WebPage(url=url, title="t", text="Testa Beispiel spielt Eishockey.", allowed=True)
            assert store_web_page(ctx, page, athlete_ids(s)[:1], "q").source_kind == kind


def test_inline_tags_leave_no_extra_spaces():
    page = (
        '<html><body><main><p>Alina Testa (<a href="/x">* 1998</a> in <b>Lengnau AG</b>) ist eine '
        'Eishockeyspielerin.<sup><a href="#r">[1]</a></sup> Sie spielt für den EV Zug und studiert.</p>'
        "</main></body></html>"
    )
    text = extract_main_text(page)
    assert text == (
        "Alina Testa (* 1998 in Lengnau AG) ist eine Eishockeyspielerin.[1] Sie spielt für den EV Zug und studiert."
    )


def test_store_wikipedia_name_and_last_edit_date(world):
    with Session(world) as s:
        ctx = build_context(s, Settings(_env_file=None))
        page = FetchedPage(
            url="https://de.wikipedia.org/wiki/Testa_Beispiel",
            title="Testa Beispiel – Wikipedia",
            text="Testa Beispiel ist eine Schweizer Eishockeyspielerin.",
            allowed=True,
            site_name="Wikimedia Foundation, Inc.",
            published_at=datetime(2018, 1, 18, tzinfo=UTC),
            modified_at=datetime(2026, 9, 30, tzinfo=UTC),
        )
        item = store_web_page(ctx, page, athlete_ids(s)[:1], "q")
        assert item.source_name == "Wikipedia (de)" and item.source_kind == "web"
        assert item.published_at == datetime(2026, 9, 30, tzinfo=UTC)
