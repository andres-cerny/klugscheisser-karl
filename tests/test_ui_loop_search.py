"""ui-loop 2026-10-17: forgiving /suche (web/search.py), Suche | Chat navigation, start page, result page."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app
from klugscheisser.web import search as ws
from klugscheisser.web.views import looks_like_question

SETTINGS = Settings(_env_file=None, web_answer_engine="fake")
HX = {"HX-Request": "true"}


# --- pure helpers -------------------------------------------------------------------------------------


def test_edit_distance_counts_swaps_once_and_stops_early():
    assert ws.edit_distance("stalder", "stalder") == 0
    assert ws.edit_distance("stlader", "stalder") == 1  # swapped letters
    assert ws.edit_distance("lausane", "lausanne") == 1
    assert ws.edit_distance("abcdefgh", "zzzzzzzz", limit=2) == 3  # capped above the limit


def test_text_score_allows_typos_by_word_length():
    assert ws.text_score("zsc lions", "zsc lions frauen") == 88  # prefix
    assert ws.text_score("lions", "zsc lions frauen") == 80  # word start
    assert ws.text_score("lausane", "lausanne hc feminin") > 50  # one typo in 7 letters
    assert ws.text_score("zgu", "ev zug") == 0  # no typos in short words
    assert ws.text_score("frauen lions", "zsc lions frauen") == 75  # word order


def test_country_names_in_other_languages():
    for q, ioc in [
        ("switzerland", "SUI"),
        ("suisse", "SUI"),
        ("svizzera", "SUI"),
        ("united states", "USA"),
        ("czech republic", "CZE"),
        ("suede", "SWE"),
        ("kanda", "CAN"),  # typo
        ("switz", "SUI"),  # still typing
    ]:
        assert ioc in ws.countries_in_query(q), q
    assert ws.countries_in_query("stalder") == set()
    assert ws.countries_in_query("us") == {"USA"}  # short codes only as the whole query
    assert "USA" not in ws.countries_in_query("lara us")


def test_question_detection_for_the_chat_offer():
    assert looks_like_question("Wer spielt im Ausland?")
    assert looks_like_question("wer ist verletzt bei Kanada")
    assert not looks_like_question("Lara Stalder")
    assert not looks_like_question("zsc lions")


# --- with the database --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE "
                + ", ".join(x.name for x in m.Base.metadata.sorted_tables)
                + " RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        # an English duplicate of a national team, as imported from Wikipedia / Elite Prospects
        s.add(
            m.Team(
                sport_id=sport.id,
                key="finland-womens-national-ice-hockey-team",
                name="Finland women's national ice hockey team",
                kind="club",
                country="FIN",
            )
        )
        # same country code, other country name: must NOT be folded into the national team
        s.add(
            m.Team(
                sport_id=sport.id,
                key="puerto-rico-womens-national-ice-hockey-team",
                name="Puerto Rico women's national ice hockey team",
                kind="club",
                country="USA",
            )
        )
        s.commit()
    return engine


@pytest.fixture
def client(seeded):
    with TestClient(build_web_app(SETTINGS)) as c:
        yield c


def _team(engine, key):
    with Session(engine) as s:
        return s.scalar(select(m.Team).where(m.Team.key == key))


def test_country_names_find_the_german_national_team_first(seeded):
    with Session(seeded) as s:
        for q, key in [("Switzerland", "sui-women"), ("Suisse", "sui-women"), ("Finland", "fin-women")]:
            hits = ws.find_teams(s, q)
            assert hits and hits[0].team.key == key, q


def test_english_duplicate_is_folded_into_the_national_team(seeded, client):
    fin, dup = _team(seeded, "fin-women"), _team(seeded, "finland-womens-national-ice-hockey-team")
    with Session(seeded) as s:
        hits = ws.find_teams(s, "Finland")
        assert [h.team.id for h in hits].count(dup.id) == 0  # not its own row
        assert [x.id for x in hits[0].also] == [dup.id]
        usa = ws.find_teams(s, "United States")
        assert usa[0].team.key == "usa-women" and not usa[0].also  # Puerto Rico is no duplicate of the USA
    page = client.get("/suche", params={"q": "Finland"}).text
    assert f'href="/team/{fin.id}?q=Finland"' in page
    assert "Auch erfasst als:" in page and f"href='/team/{dup.id}?q=Finland'" in page  # still reachable


def test_typos_and_missing_accents_in_people_and_teams(seeded):
    with Session(seeded) as s:
        assert "Lara Stalder" in [a.full_name for a in ws.find_people(s, "Stlader")]  # swapped letters
        assert "Andrea Brändli" in [a.full_name for a in ws.find_people(s, "brandli")]
        assert ws.find_teams(s, "schweitz")[0].team.key == "sui-women"


def test_events_with_words_in_other_languages(seeded):
    with Session(seeded) as s:
        keys = [ev.key for ev in ws.find_events(s, "wm 2026")]
        assert "wch-2026" in keys
        world = [ev.key for ev in ws.find_events(s, "world championship 2026")]
        assert "wch-2026" in world


def test_did_you_mean_links_straight_to_the_page(seeded, client):
    with Session(seeded) as s:
        aid = repo.resolve_athlete(s, "Andrea Brändli").id
        dym = ws.did_you_mean(s, "Andrae Brndlli")
    assert {"label": "Andrea Brändli", "href": f"/person/{aid}"} in dym
    page = client.get("/suche", params={"q": "Andrae Brndlli"}).text
    assert "Keine Treffer für «Andrae Brndlli»" in page and "Meinten Sie" in page
    assert f"href='/person/{aid}'" in page
    assert "hinzufügen" in page and "Im Chat fragen" in page  # the two ways on


def test_result_page_groups_by_kind_and_offers_the_chat_for_questions(client):
    page = client.get("/suche", params={"q": "Schweiz"}).text
    assert "<h2>Teams <span class='n'>" in page and "class='suggest-list'" in page
    assert "Das ist eine Frage?" not in page
    q = client.get("/suche", params={"q": "Wer spielt im Ausland?"}).text
    assert "Das ist eine Frage?" in q and "askChat(" in q  # offered, never sent by itself
    typing = client.get("/suche", params={"q": "wer spielt"}, headers=HX).text
    assert "Karl fragen</span>" in typing and "<html" not in typing


def test_empty_search_page_is_a_start_not_an_error(client):
    page = client.get("/suche").text
    assert "Keine Treffer" not in page and "Fragen in ganzen Sätzen gehören in den" in page


def test_navigation_suche_and_chat_on_every_page(client):
    home = client.get("/").text
    chat = client.get("/chat").text
    for html in (home, chat):
        assert 'class="mainnav"' in html and 'href="/suche"' in html and 'href="/chat"' in html
    assert (
        'class="nav-suche" href="/suche" title="Person, Team oder Anlass suchen" aria-current="page"' in home
    )
    assert 'class="nav-chat" href="/chat" title="Karl eine Frage stellen" aria-current="page"' in chat
    # start page: two separate places, the question box posts a new chat
    assert 'id="h-suche"' in home and "Chat: Karl fragen" in home
    assert '<form class="askbox" method="post" action="/chat">' in home
    assert "searchSubmit" in home and "isQuestion" not in home  # no silent jump from the search to the chat
