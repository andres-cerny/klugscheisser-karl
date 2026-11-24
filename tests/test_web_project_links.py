"""2026-10-19: every app link in a page carries the project prefix (/p/<key>/…), never only the cookie.

Before, links like `/event/wch-2026` or `/person/24?q=Balzer` resolved through the `ks_project` cookie only:
after using the other project in another tab they led there (404). The prefix is added in one place
(`web.project_switch.prefix_links`, run by the ProjectMiddleware on every HTML response).
"""

import re

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser import projects
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app
from klugscheisser.web.project_switch import ProjectMiddleware, prefix_links

SETTINGS = Settings(_env_file=None, web_answer_engine="fake")
# a root-relative app link without the project prefix (/p/…, //host, static files, login/logout are fine)
UNPREFIXED = re.compile(
    r"""\b(?:href|action|src|formaction|hx-get|hx-post|hx-put|hx-patch|hx-delete)=["']/(?!/|p/|static/|mcp\b|login\b|logout\b)"""
)
ENTITY_LINK = re.compile(r"""href=["']/p/[^/"']+/(?:person|team|event)/""")


def test_prefix_links_rewrites_app_links_only():
    html = (
        b"<a href='/person/12?tab=chat'>x</a><a href=\"/event/wch-2026\">y</a><form action='/chat'>"
        b"<div hx-get='/person/3/foto' hx-post=\"/suche\"></div><a href='/'>home</a><form action='/login'><a href='/logout'>"
        b"<a href='/p/zwei/'>other project</a><a href='//cdn.example/x'>cdn</a>"
        b"<a href='https://example.org/person/1'>ext</a><a href='#top'>top</a><a href='?q=1'>q</a>"
    )
    out = prefix_links(html, "eishockey").decode()
    assert "href='/p/eishockey/person/12?tab=chat'" in out
    assert 'href="/p/eishockey/event/wch-2026"' in out
    assert "action='/p/eishockey/chat'" in out
    assert "hx-get='/p/eishockey/person/3/foto'" in out and 'hx-post="/p/eishockey/suche"' in out
    assert "href='/p/eishockey/'" in out
    assert "href='/p/zwei/'" in out  # project switcher untouched
    assert "action='/login'" in out and "href='/logout'" in out  # belong to no project
    assert "href='//cdn.example/x'" in out and "href='https://example.org/person/1'" in out
    assert "href='#top'" in out and "href='?q=1'" in out


@pytest.fixture(scope="module")
def seeded(engine):
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
        s.commit()
    projects.clear_cache()
    return engine


@pytest.fixture
def client(seeded):
    with TestClient(ProjectMiddleware(build_web_app(SETTINGS)), base_url="http://127.0.0.1:8000") as c:
        yield c


def _pages(engine) -> list[str]:
    with Session(engine) as s:
        person = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Lara Stalder"))
        team = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        event = s.scalar(select(m.Event).order_by(m.Event.id).limit(1))
    pages = ["/", "/suche?q=Stalder", "/suche?q=Schweiz", "/brief", "/quellen", "/chat", "/chats", "/projekt"]
    pages += [f"/person/{person.id}", f"/person/{person.id}?tab=chat", f"/team/{team.id}"]
    if event is not None:
        pages.append(f"/event/{event.key}")
    return pages


def test_no_page_has_a_link_without_the_project_prefix(seeded, client):
    key = projects.default().key
    entity_links = 0
    for path in _pages(seeded):
        for url in (f"/p/{key}{path}", path):  # with the prefix, and through the cookie / default project
            r = client.get(url)
            assert r.status_code == 200, url
            bad = sorted({r.text[x.start() : x.end() + 30] for x in UNPREFIXED.finditer(r.text)})
            assert not bad, f"{url}: links without /p/{key}/: {bad[:5]}"
            assert int(r.headers["content-length"]) == len(r.content)
        entity_links += len(ENTITY_LINK.findall(client.get(f"/p/{key}{path}").text))
    assert entity_links > 10  # the pages do link persons, teams and events (all prefixed)


def test_start_page_and_search_results_link_into_the_project(seeded, client):
    key = projects.default().key
    home = client.get(f"/p/{key}/").text
    assert re.search(rf"""href=["']/p/{key}/(?:event|team|person)/""", home)
    hits = client.get(f"/p/{key}/suche", params={"q": "Stalder"}).text
    assert re.search(rf"""href=["']/p/{key}/person/\d+\?q=Stalder""", hits)


def test_htmx_fragments_are_prefixed_too(seeded, client):
    key = projects.default().key
    r = client.get(f"/p/{key}/suche", params={"q": "Stalder"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert not UNPREFIXED.search(r.text)
