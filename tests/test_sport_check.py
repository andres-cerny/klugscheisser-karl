"""Sport check (domain/sport.py): women's football must not be stored / linked as women's ice hockey.

Fixtures are real live items (tests/fixtures/sport_items.json): SRF and Blick filed women's football
national-team stories under /sport/eishockey/, Watson tickers carry a sport list ("Fussball, Eishockey, …").
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.classify.rules import RuleBasedClassifier
from klugscheisser.collectors.rss import Article, RssCollector, canonical_url, is_womens_hockey
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.sport import sport_verdict, strip_enumerations
from klugscheisser.domain.sport_audit import apply_flags, audit_sport
from klugscheisser.domain.team_matcher import TeamMatcher
from klugscheisser.interfaces import FeedConfig
from klugscheisser.seed.loader import load_all_seeds

FIX = json.loads(Path("tests/fixtures/sport_items.json").read_text())
FOOTBALL = {d["id"]: d for d in FIX["football"]}
HOCKEY = {d["id"]: d for d in FIX["hockey"]}


def article(d: dict, canonical: str | None = None) -> Article:
    return Article(
        feed=d["feed"],
        source_type="srf" if d["feed"] == "SRF" else "other_media",
        url=d["url"],
        source_kind="srf",
        title=d["title"],
        summary=d["summary"] or "",
        published_at=datetime(2026, 10, 13, tzinfo=UTC),
        excerpt=d["text"],
        canonical_url=canonical,
    )


def body(d: dict) -> str:
    return f"{d['title']}\n{d['text']}"


# --- the verdict -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("item_id", [10300, 10359, 14174, 15662, 15724])
def test_football_body_beats_hockey_url(item_id):
    d = FOOTBALL[item_id]
    v = sport_verdict(body(d), d["url"])
    assert not v.is_hockey and v.source == "body", v
    assert not is_womens_hockey(article(d))


def test_austria_qualifier_is_football():
    """The item behind the invented 'hockey friendly vs Austria': ÖFB, FIFA, SFV, Brasilien 2027."""
    v = sport_verdict(body(FOOTBALL[14174]), FOOTBALL[14174]["url"])
    assert v.sport == "football" and v.other["football"] >= 4 and v.hockey == 0


def test_url_only_decides_when_text_names_no_sport_and_canonical_url_wins():
    d = FOOTBALL[12422]  # text names no sport; the feed URL says /eishockey/, the page is /fussball/
    assert sport_verdict(body(d), d["url"]).is_hockey  # without the page there is nothing better
    canon = FIX["canonical"]["12422"]
    assert sport_verdict(body(d), canon).sport == "football"
    assert not is_womens_hockey(article(d, canonical=canon))


def test_sport_list_in_boilerplate_is_no_cue():
    d = FOOTBALL[15710]
    assert "Eishockey" not in strip_enumerations(d["text"])
    v = sport_verdict(body(d), d["url"])
    assert v.sport == "unknown" and v.hockey == 0
    assert not is_womens_hockey(article(d))


@pytest.mark.parametrize("item_id", [75, 137, 567])
def test_real_swiss_womens_hockey_still_passes(item_id):
    d = HOCKEY[item_id]
    assert sport_verdict(body(d), d["url"]).is_hockey
    assert is_womens_hockey(article(d))


@pytest.mark.parametrize(
    "text,url",
    [
        ("Nati an der WM: Die Schweizerinnen verlieren gegen Kanada im Powerplay.", None),
        ("Die Frauen-Nati verliert an der WM 1:3 gegen Finnland.", "https://www.srf.ch/sport/eishockey/x"),
        ("Lara Stalder trifft im Schlussdrittel zum 3:2 für den EV Zug.", None),
        ("Good luck to Team Switzerland at this week's Women's Euro Hockey Tour!", None),
        ("Die Schweizerinnen gewinnen die PWHL-Partie, Goalie Andrea Brändli hält alles.", None),
        ("Hockey féminin: les Suissesses battues par la Finlande", None),
        ("Die frühere Fussballerin spielt jetzt Eishockey in der Women's League.", None),
        # RSI / RTS (real teasers): Italian and French cues
        ("Le ticinesi convocate per la tappa di Södertälje del WEHT, non c'è Dalessi", None),
        ("L'attaccante ticinese chiamata da Shannon Miller per il WEHT in Finlandia", None),
        ("Le palet a franchi la ligne au deuxième tiers à la patinoire de Malley", None),
        ("Le Suissesse sul ghiaccio di Lahti", None),
    ],
)
def test_hockey_texts(text, url):
    assert sport_verdict(text, url).is_hockey


@pytest.mark.parametrize(
    "text",
    [
        "Die Nati gewinnt das Spiel, die Saison ist lang und das Team will an die WM.",
        "Chelsea-Goalie Martinez nach Zusammenprall im Spital",
        "Die Torhüterin hielt stark, die Stürmerin traf doppelt.",
    ],
)
def test_generic_words_alone_decide_nothing(text):
    v = sport_verdict(text)
    assert v.sport == "unknown" and not v.is_hockey and v.weak > 0


@pytest.mark.parametrize(
    "text",
    [  # shared with football: rigori / tirs au but / gardienne decide nothing
        "La Svizzera riprende la Cechia nel finale e sorride ai rigori",
        "Les Suissesses s'imposent aux tirs au but, la gardienne brille",
    ],
)
def test_shootout_words_are_not_hockey(text):
    assert sport_verdict(text).sport == "unknown"


@pytest.mark.parametrize(
    "text,sport",
    [
        ("Mit diesen Unihockeyspielerinnen geht es an die WM", "unihockey"),
        ("Die Schweizerinnen verlieren das Elfmeterschiessen, Abseits-Tor aberkannt.", "football"),
        ("Selina Egloff holt Slalom-Gold im Riesenslalom.", "ski"),
        ("Die Frauen-Nati verpasst die EM-Endrunde.", "other"),
        ("Die SFV-Auswahl trifft im Letzigrund auf Italien.", "football"),
    ],
)
def test_other_sports(text, sport):
    assert sport_verdict(text, "https://www.blick.ch/sport/eishockey/x").sport == sport


def test_rules_classifier_relevance():
    football = RuleBasedClassifier().classify_text(body(FOOTBALL[14174]))
    hockey = RuleBasedClassifier().classify_text(body(HOCKEY[75]))
    assert football.relevant_p < 0.5 <= hockey.relevant_p


def test_canonical_url_from_page():
    page = '<html><head><link rel="canonical" href="https://www.srf.ch/sport/fussball/a"></head></html>'
    assert canonical_url(page, "https://x") == "https://www.srf.ch/sport/fussball/a"
    assert canonical_url("<html></html>", "https://www.srf.ch/final") == "https://www.srf.ch/final"


# --- team links --------------------------------------------------------------------------------------------


@pytest.fixture
def s(db):
    with Session(db) as session:
        load_all_seeds(session)
        session.commit()
        yield session


def keys(s, text, url=None, url_checked=False):
    return sorted(
        s.get(m.Team, link.team_id).key
        for link in TeamMatcher(s).from_text(text, url=url, url_checked=url_checked)
    )


@pytest.mark.parametrize("item_id", [10359, 14174, 15662, 15724])
def test_football_items_link_no_hockey_team(s, item_id):
    d = FOOTBALL[item_id]
    assert keys(s, body(d), d["url"]) == []


def test_feed_url_alone_links_no_team_but_a_checked_url_does(s):
    d = FOOTBALL[12422]  # text names no sport, feed URL says /eishockey/, the page is football
    assert keys(s, body(d), d["url"]) == []
    assert keys(s, body(d), FIX["canonical"]["12422"], url_checked=True) == []
    url = "https://www.srf.ch/sport/eishockey/frauen-eishockey/x"
    assert keys(s, "Die Nati siegt 3:1.", url=url) == []
    assert keys(s, "Die Nati siegt 3:1.", url=url, url_checked=True) == ["sui-women"]


@pytest.mark.parametrize("item_id,group", [(15710, "football"), (15662, "football"), (138, "hockey")])
def test_tickers_never_link_teams(s, item_id, group):
    d = FIX[group][[x["id"] for x in FIX[group]].index(item_id)]
    assert keys(s, body(d), d["url"]) == []
    assert keys(s, "ZSC Lions Frauen gewinnen +++ Fussball: FIFA sperrt Spieler") == []


def test_hockey_items_still_link_national_team(s):
    assert "sui-women" in keys(s, body(HOCKEY[137]), HOCKEY[137]["url"])
    assert "sui-women" in keys(s, body(HOCKEY[75]), HOCKEY[75]["url"])
    assert keys(s, "Frauen-Eishockey: Die Nati gewinnt gegen Schweden.") == ["sui-women"]
    assert keys(s, "Die Schweizerinnen gewinnen im Eishockey 3:1.") == ["sui-women"]
    # a club name is explicit: it links without a hockey cue
    assert keys(s, "Die ZSC Lions Frauen gewinnen 3:1.") == ["zsc-frauen"]


def test_national_and_city_aliases_need_a_hockey_cue(s):
    assert keys(s, "Die Schweizerinnen gewinnen 3:1.") == []
    assert keys(s, "Schweizerinnen gegen Österreich in Bern: das Spiel der Saison") == []
    assert keys(s, "Schweizerinnen gegen Österreich in Bern", "https://x.ch/sport/fussball/a") == []
    assert "scb-frauen" in keys(s, "Eishockey: Bern gewinnt das Derby gegen Zug.")


# --- ingest: the article page shows the real section -------------------------------------------------------

FEED = FeedConfig(
    name="SRF",
    url="https://www.srf.ch/test-feed",
    source_kind="srf",
    source_type="srf",
    section_filter="/eishockey/",
    fetch_article=True,
)
MISFILED = FOOTBALL[12422]["url"]
FEED_XML = f"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>SRF</title>
<item><title>{FOOTBALL[12422]["title"]}</title><link>{MISFILED}</link>
<description>{FOOTBALL[12422]["summary"]}</description><pubDate>Fri, 09 Oct 2026 17:27:17 +0200</pubDate></item>
</channel></rss>"""
PAGE = f"""<html><head><link rel="canonical" href="{FIX["canonical"]["12422"]}"></head><body>
<article class="articlepage__article"><p>{FOOTBALL[12422]["text"]}</p></article></body></html>"""


@respx.mock
def test_ingest_keeps_misfiled_football_light(s):
    from klugscheisser.ingest.news import ingest_news
    from klugscheisser.ingest.runner import build_context

    respx.get(FEED.url).mock(return_value=httpx.Response(200, text=FEED_XML))
    respx.get(MISFILED).mock(return_value=httpx.Response(200, text=PAGE))
    report = ingest_news(build_context(s, Settings(_env_file=None)), RssCollector([FEED], delay_s=0))
    s.commit()
    item = s.scalar(select(m.Item).where(m.Item.url == MISFILED))
    assert report.womens == 0 and report.items_new == 0
    assert item.level == "light" and item.teams == [] and item.classification is None


# --- cleaning stored items ---------------------------------------------------------------------------------


def _stored(s, d, team_keys, relevant=True):
    item = m.Item(
        url=d["url"],
        title=d["title"],
        text=d["text"],
        raw={"summary": d["summary"], "feed": d["feed"]},
        content_hash=str(d["id"]),
        source_type="srf",
        source_name=d["feed"],
        platform="rss",
        level="full",
        published_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    for key in team_keys:
        item.teams.append(m.ItemTeam(team_id=s.scalar(select(m.Team.id).where(m.Team.key == key))))
    item.classification = m.Classification(
        relevant_p=0.7,
        relevant=relevant,
        category="result",
        sensitive_p=0.05,
        vorsicht=False,
        newsworthiness=3,
        classifier="jev",
        classifier_version="1",
    )
    s.add(item)
    s.flush()
    return item


def test_audit_is_dry_by_default_and_apply_unlinks(s):
    austria = _stored(s, FOOTBALL[14174], ["sui-women"])
    ticker = _stored(s, FOOTBALL[15710], ["sui-women", "scb-frauen"])
    hockey_ticker = _stored(s, HOCKEY[138], ["sui-women", "lausanne-feminin"])
    rating = _stored(s, FOOTBALL[12422], ["sui-women"])  # only its feed URL says /eishockey/
    hockey = _stored(s, HOCKEY[137], ["sui-women"])
    s.commit()

    flags = audit_sport(s)
    by_id = {f.item_id: f for f in flags}
    assert set(by_id) == {austria.id, ticker.id, hockey_ticker.id, rating.id}
    assert by_id[austria.id].remove_teams == ["sui-women"] and by_id[austria.id].mark_irrelevant
    assert by_id[ticker.id].remove_teams == ["scb-frauen", "sui-women"]
    assert by_id[hockey_ticker.id].remove_teams == ["lausanne-feminin", "sui-women"]
    assert by_id[rating.id].remove_teams == ["sui-women"]
    # names no sport: unlinked, but not judged irrelevant
    assert not any(by_id[i].mark_irrelevant for i in (ticker.id, hockey_ticker.id, rating.id))
    assert "#" in flags[0].line()
    # nothing changed by the audit itself
    assert len(austria.teams) == 1 and austria.classification.relevant

    assert apply_flags(s, flags) == (6, 1)
    s.commit()
    s.expire_all()
    assert austria.teams == [] and austria.classification.relevant is False
    assert austria.raw["sport_check"]["sport"] == "football"
    assert ticker.teams == [] and ticker.classification.relevant is True
    assert rating.teams == [] and hockey_ticker.teams == []
    assert [t.team.key for t in hockey.teams] == ["sui-women"] and hockey.raw.get("sport_check") is None
    assert audit_sport(s) == []  # idempotent


def test_audit_resolves_canonical_url_only_for_fetch_allowed_hosts(s):
    rating = _stored(s, FOOTBALL[12422], ["sui-women"])
    s.commit()
    assert [f.mark_irrelevant for f in audit_sport(s)] == [False]  # unlinked, sport unknown

    class Http:
        calls = 0

        def get(self, url):
            Http.calls += 1
            return httpx.Response(200, text=PAGE, request=httpx.Request("GET", url))

    flags = audit_sport(s, resolve_urls=True, http=Http(), delay_s=0)
    assert Http.calls == 1
    assert [f.item_id for f in flags] == [rating.id]
    assert flags[0].canonical_url == FIX["canonical"]["12422"]
    assert flags[0].verdict.sport == "football" and flags[0].mark_irrelevant
    apply_flags(s, flags)
    assert rating.raw["canonical_url"] == FIX["canonical"]["12422"]
