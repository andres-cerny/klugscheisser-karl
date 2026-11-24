"""v3 P6: own web app (search, person/event/team pages, question box, refresh, access control)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.collectors.instagram.sources import FixtureInstagramSource
from klugscheisser.collectors.rss import DEFAULT_FEEDS, RssCollector
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest.instagram import ingest_instagram
from klugscheisser.ingest.news import ingest_news
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import FakeAnswerEngine, build_web_app, get_answer_engine, web_routes
from klugscheisser.web.html import e

FIX = Path("tests/fixtures")
SETTINGS = Settings(_env_file=None, web_answer_engine="fake")
HX = {"HX-Request": "true"}


@pytest.fixture(scope="module")
def data(engine):
    """Seeds + fixture Instagram + synthetic SRF news (same data as test_m5_tools)."""
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
        testa = repo.upsert_athlete(s, sport, "Testa Beispiel", nationality="SUI", position="F")
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata"})
        wch = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        s.add(
            m.EventEntry(
                event_id=wch.id,
                athlete_id=testa.id,
                team_id=sui.id,
                role="F",
                list_type="provisional",
                source="Test",
            )
        )
        for name, handle in [
            ("Alina Müller", "alinamueller_"),
            ("Andrea Brändli", "andrea.braendli"),
            ("Lara Stalder", "lara.stalder7"),
        ]:
            add_handle_candidate(s, repo.resolve_athlete(s, name), "instagram", handle, {"kind": "wikidata"})
        athletes = [
            repo.resolve_athlete(s, n)
            for n in ["Testa Beispiel", "Alina Müller", "Andrea Brändli", "Lara Stalder"]
        ]
        ingest_instagram(build_context(s, SETTINGS), FixtureInstagramSource(FIX / "instagram"), athletes)
        with respx.mock:
            respx.get(DEFAULT_FEEDS[0].url).mock(
                return_value=httpx.Response(200, content=(FIX / "rss/srf_sport.xml").read_bytes())
            )
            respx.get(url__regex=r".*test-beispiel-trifft-doppelt").mock(
                return_value=httpx.Response(200, text=(FIX / "rss/article_beispiel.html").read_text())
            )
            ingest_news(build_context(s, SETTINGS), RssCollector(DEFAULT_FEEDS[:1], delay_s=0))
        s.commit()
    return engine


def ids(engine) -> dict[str, int]:
    with Session(engine) as s:
        return {
            n: repo.resolve_athlete(s, n).id
            for n in ["Testa Beispiel", "Alina Müller", "Andrea Brändli", "Lara Stalder"]
        }


@pytest.fixture
def client(data):
    with TestClient(build_web_app(SETTINGS)) as c:
        yield c


# --- access control ---------------------------------------------------------------------------------------


def test_open_without_access_settings(client):
    r = client.get("/")
    assert r.status_code == 200 and "Suche" in r.text and "htmx.org" in r.text


def test_cloudflare_access_email_allow_list(data):
    settings = Settings(_env_file=None, web_access_emails=["Fabi@example.ch"])
    with TestClient(build_web_app(settings)) as c:
        assert c.get("/").status_code == 403
        assert (
            c.get("/", headers={"Cf-Access-Authenticated-User-Email": "other@example.ch"}).status_code == 403
        )
        r = c.get("/", headers={"Cf-Access-Authenticated-User-Email": "fabi@example.ch"})
        assert r.status_code == 200
        assert c.get("/suche?q=ali").status_code == 403  # fragments are guarded too


def test_password_fallback_with_signed_cookie(data):
    settings = Settings(_env_file=None, web_password="geheim", web_access_emails=["fabi@example.ch"])
    with TestClient(build_web_app(settings), follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.post("/login", data={"password": "falsch", "next": "/"}).status_code == 401
        c.cookies.set("ks_session", "9999999999.forged")
        assert c.get("/").status_code == 303
        c.cookies.clear()
        r = c.post("/login", data={"password": "geheim", "next": "/"})
        assert r.status_code == 303 and "ks_session" in r.cookies
        assert c.get("/").status_code == 200
        # the email header still works without a cookie
        c.cookies.clear()
        assert (
            c.get("/", headers={"Cf-Access-Authenticated-User-Email": "fabi@example.ch"}).status_code == 200
        )


def test_cross_origin_post_rejected(client):
    r = client.post("/frage", data={"frage": "Wer?"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_routes_do_not_clash_with_mcp():
    paths = {r.path for r in web_routes(SETTINGS)}
    assert "/mcp" not in paths and "/healthz" not in paths and "/" in paths


# --- search -----------------------------------------------------------------------------------------------


def test_search_autocomplete_persons_teams_events(client, data):
    pid = ids(data)["Alina Müller"]
    r = client.get("/suche", params={"q": "ali"}, headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    assert f'href="/person/{pid}?q=ali"' in r.text and "Alina Müller" in r.text
    r = client.get("/suche", params={"q": "brandli"}, headers=HX)  # umlaut-free spelling
    assert "Andrea Brändli" in r.text
    r = client.get("/suche", params={"q": "schweiz"}, headers=HX)
    assert 'href="/team/' in r.text and "Schweiz" in r.text
    r = client.get("/suche", params={"q": "WM 2026"}, headers=HX)
    assert 'href="/event/wch-2026?q=' in r.text
    r = client.get("/suche", params={"q": ""}, headers=HX)  # empty: upcoming events
    assert 'href="/event/' in r.text
    full = client.get("/suche", params={"q": "ali"})  # without htmx: full page
    assert "<html" in full.text and "Alina Müller" in full.text


# --- person page ------------------------------------------------------------------------------------------


def test_person_page_vorsicht_first_emoji_lines_links(client, data):
    pid = ids(data)["Testa Beispiel"]
    r = client.get(f"/person/{pid}")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Testa Beispiel</h1>" in html and "Stand: " in html
    v, tl = html.index("<span class='vt'>Vorsicht</span>"), html.index('aria-labelledby="tl-h"')
    assert html.index("Stand: ") < v < tl  # the calm Vorsicht line sits right under the header
    assert html.index("https://www.instagram.com/p/SYN1/") < tl  # the Vorsicht post opens from that line
    assert "von ihr selbst" in html  # the meaning of the source, without the emoji
    assert 'href="https://www.instagram.com/p/' in html and 'target="_blank" rel="noopener"' in html
    assert 'name="target" value="' + str(pid) + '"' in html  # Aktualisieren button
    assert 'name="person" value="' + str(pid) + '"' in html  # chat panel with context
    assert "zu Testa Beispiel" in html and "Datenquellen</h2>" in html


def test_person_page_header_and_news(client, data):
    r = client.get(f"/person/{ids(data)['Andrea Brändli']}")
    html = r.text
    assert "Torhüterin" in html and "@andrea.braendli" in html and "Instagram-Bio @andrea.braendli" in html
    assert "möglicherweise veraltet" in html or "Verein laut Quellen" in html
    news = client.get(f"/person/{ids(data)['Testa Beispiel']}").text
    assert "<span class='k'>News</span>" in news and "(SRF)" not in news and "SRF" in news


def test_person_more_posts_and_unknown(client, data):
    pid = ids(data)["Lara Stalder"]
    page1 = client.get(f"/person/{pid}").text
    first_urls = {u for u in page1.split('data-url="')[1:]}
    r = client.get(f"/person/{pid}/posts", params={"offset": SETTINGS.web_page_posts}, headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    for chunk in r.text.split('data-url="')[1:]:
        assert chunk.split('"')[0] not in {u.split('"')[0] for u in first_urls}
    assert client.get("/person/999999").status_code == 404


def test_background_update_starts_without_waiting(data, monkeypatch):
    import klugscheisser.jobs.refresh as jr

    submitted = []

    class NoRun:
        def submit(self, *args, **kwargs):
            submitted.append(args)

    monkeypatch.setattr(jr, "executor", lambda: NoRun())
    with Session(data) as s:  # Instagram last fetched yesterday -> stale
        for h in repo.resolve_athlete(s, "Alina Müller").handles:
            h.last_scraped_at = datetime.now(UTC) - timedelta(days=2)
        s.commit()
    settings = Settings(_env_file=None, auto_refresh=True, web_auto_refresh=True, web_answer_engine="fake")
    with TestClient(build_web_app(settings)) as c:
        r = c.get(f"/person/{ids(data)['Alina Müller']}")
    assert r.status_code == 200 and submitted
    assert 'hx-get="/aktualisieren/' in r.text and "läuft im Hintergrund" in r.text
    with Session(data) as s:
        for job in s.scalars(select(m.Job).where(m.Job.status.in_(["queued", "running"]))):
            job.status = "failed"
        s.commit()


# --- event / team -----------------------------------------------------------------------------------------


def test_event_page(client, data):
    pid = ids(data)["Testa Beispiel"]
    r = client.get("/event/wch-2026")
    assert r.status_code == 200
    html = r.text
    assert "IIHF Frauen-WM 2026" in html and "PROVISORISCH" in html and "Stand: Kader" in html
    assert html.index("Schweiz") < html.index("USA")
    assert f"href='/person/{pid}'" in html and "badge-prov" in html
    assert "USA" in html  # v18: every WM team has a (provisional) roster
    assert "https://www.instagram.com/p/" in html  # latest post line with link
    assert client.get("/event/WM").status_code == 200  # alias resolves
    assert client.get("/event/tour-de-france").status_code == 404


def test_team_page(client, data):
    with Session(data) as s:
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
    r = client.get(f"/team/{sui.id}")
    assert r.status_code == 200 and "<h1>Schweiz</h1>" in r.text and "Kader" in r.text
    assert "Testa Beispiel" in r.text


# --- question box -----------------------------------------------------------------------------------------


def test_answer_engine_falls_back_to_fake():
    assert isinstance(get_answer_engine(SETTINGS), FakeAnswerEngine)


def test_question_with_fake_engine(client, data):
    pid = ids(data)["Lara Stalder"]
    form = client.get(f"/chat?person={pid}").text
    assert "Bezogen auf: <strong>Lara Stalder</strong>" in form and 'name="person"' in form
    assert (
        client.get(f"/frage?person={pid}", follow_redirects=False).headers["location"]
        == f"/chat?person={pid}"
    )
    r = client.post("/chat", data={"frage": "Was gibt es Neues?", "person": str(pid)}, headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    assert "Was gibt es Neues?" in r.text and "/antwort" in r.text and "Suche Quellen" in r.text
    chat_id = int(r.headers["HX-Push-Url"].rsplit("/", 1)[1])
    html = client.post(f"/chat/{chat_id}/antwort", headers=HX).text
    assert "Datenstand:" in html and "«" in html and "Kosten $0.000" in html
    assert "href='https://www.instagram.com/p/" in html
    assert "Lücken</h2>" in html
    again = client.post(f"/chat/{chat_id}/antwort", headers=HX).text  # idempotent: same answer again
    assert "Datenstand:" in again
    full = client.get(f"/chat/{chat_id}").text
    assert "<html" in full and "Was gibt es Neues?" in full and "Datenstand:" in full
    empty = client.post("/chat", data={"frage": "  "}, headers=HX)
    assert "Bitte eine Frage eingeben" in empty.text


def test_answer_rendering_vorsicht_unbestaetigt_verworfen():
    from klugscheisser.interfaces import AnswerResult, Statement
    from klugscheisser.web.views import answer_html

    st = Statement(
        aussage="Sie ist verletzt.",
        zitat="Ich falle <leider> aus",
        item_id=1,
        url="https://www.instagram.com/p/X/",
        quelle="[Instagram @x, 01.10.]",
        quelle_link="[Instagram @x, 01.10.](https://www.instagram.com/p/X/)",
        zeile_prefix="📸 (von ihr selbst)",
        vorsicht=True,
    )
    weak = Statement(**{**st.__dict__, "vorsicht": False, "aussage": "Schwach belegt."})
    html = answer_html(
        AnswerResult(
            frage="?",
            datenstand="Instagram: heute",
            statements=[st],
            unbestaetigt=[weak],
            luecken=["Studium unbekannt"],
            verworfen=2,
            kosten_usd=0.0123,
        )
    )
    assert html.index("Datenstand") < html.index("Vorsicht</h2>")
    assert e("«Ich falle <leider> aus»") in html  # escaped
    assert "<details><summary>Unbestätigt (1)" in html and "2 Aussagen verworfen (nicht belegbar)" in html
    assert "Kosten $0.012" in html and "<li>Studium unbekannt</li>" in html


# --- refresh ----------------------------------------------------------------------------------------------


def test_refresh_button_starts_job_and_polls(client, data, monkeypatch):
    import klugscheisser.jobs.refresh as jr

    monkeypatch.setattr(jr, "executor", lambda: type("X", (), {"submit": lambda self, *a, **k: None})())
    with Session(data) as s:
        jr.touch(s, "news", "all")  # news fresh: no network
        s.commit()
    pid = ids(data)["Alina Müller"]
    r = client.post("/aktualisieren", data={"target": str(pid)}, headers=HX)
    assert r.status_code == 200 and "News: aktuell" in r.text
    job_id = int(r.text.split('hx-get="/aktualisieren/')[1].split('"')[0])
    poll = client.get(f"/aktualisieren/{job_id}", headers=HX)
    assert "hx-get" in poll.text and "HX-Refresh" not in poll.headers
    with Session(data) as s:
        job = s.get(m.Job, job_id)
        job.status, job.result = "done", {"items_new": 2, "handles": 1, "cost_usd": 0.01}
        job.started_at = job.finished_at = datetime.now(UTC)
        s.commit()
    done = client.get(f"/aktualisieren/{job_id}", headers=HX)
    assert done.headers.get("HX-Refresh") == "true" and "2 neue Posts" in done.text
    bad = client.post("/aktualisieren", data={"target": "Tour de France xyz"}, headers=HX)
    assert bad.status_code == 200 and "class='err'" in bad.text


def test_login_brake_after_five_wrong_passwords(monkeypatch):
    from klugscheisser.web import access
    from klugscheisser.web.routes import build_web_app

    monkeypatch.setattr(access, "_fails", {})
    app = build_web_app(Settings(_env_file=None, web_password="richtig-lang-123", web_answer_engine="fake"))
    with TestClient(app) as c:
        for _ in range(5):
            r = c.post("/login", data={"password": "falsch", "next": "/"}, follow_redirects=False)
            assert r.status_code == 401
        r = c.post("/login", data={"password": "richtig-lang-123", "next": "/"}, follow_redirects=False)
        assert r.status_code == 429 and "Zu viele falsche Versuche" in r.text  # even the right one waits
        now = __import__("time").time()
        assert access.locked_for("testclient", now + access.WINDOW_S + 1) == 0  # free again after 15 min


def test_login_success_clears_failures(monkeypatch):
    from klugscheisser.web import access
    from klugscheisser.web.routes import build_web_app

    monkeypatch.setattr(access, "_fails", {})
    app = build_web_app(Settings(_env_file=None, web_password="richtig-lang-123", web_answer_engine="fake"))
    with TestClient(app) as c:
        for _ in range(4):
            c.post("/login", data={"password": "falsch", "next": "/"})
        r = c.post("/login", data={"password": "richtig-lang-123", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303 and access._fails == {}
