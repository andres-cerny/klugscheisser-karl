"""v7: projects — one database per project, chosen per request (web prefix / cookie, MCP path, CLI)."""

import os
from pathlib import Path

import pytest
from conftest import DROP_OUR_OBJECTS, TEST_DB_URL, ensure_database, worker_db_url
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser import projects
from klugscheisser.config import Settings, get_settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import get_engine
from klugscheisser.projects_admin import copy_person, migrate

# the second project database: <test db>_v7 (own one per xdist worker), created from the template when missing
SECOND_DB = (
    worker_db_url(os.environ["TEST_DATABASE_URL_2"])
    if os.environ.get("TEST_DATABASE_URL_2")
    else make_url(TEST_DB_URL)
    .set(database=f"{make_url(TEST_DB_URL).database}_v7")
    .render_as_string(hide_password=False)
)
HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}
CALL_STATUS = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "status", "arguments": {}},
}


@pytest.fixture(scope="session")
def second_engine(tmp_path_factory):
    path: Path = tmp_path_factory.mktemp("v7") / "projects.yaml"
    path.write_text(
        f"""default: eishockey
projects:
  eishockey:
    name: Frauen-Eishockey
  zwei:
    name: Zwei
    emoji: "🗂️"
    database: {SECOND_DB}
    scheduled_jobs: false
    seeds: false
    sport:
    wording:
      person: Sportlerin oder Sportler
""",
        encoding="utf-8",
    )
    old = os.environ.get("PROJECTS_FILE")
    os.environ["PROJECTS_FILE"] = str(path)
    ensure_database(SECOND_DB)  # copy of the template (has `vector`, which only a superuser could create)
    eng = create_engine(SECOND_DB)
    with eng.begin() as conn:
        conn.execute(text(DROP_OUR_OBJECTS))  # keeps the extensions
    migrate(projects.get("zwei"))
    yield eng
    eng.dispose()
    os.environ["PROJECTS_FILE"] = old or ""


@pytest.fixture
def two(db, second_engine):
    names = ", ".join(t.name for t in reversed(m.Base.metadata.sorted_tables))
    with second_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    projects.clear_cache()
    return db, second_engine


def _person(engine, name: str, sport_key: str = "icehockey_women") -> int:
    with Session(engine) as s:
        sport = s.scalar(select(m.Sport).where(m.Sport.key == sport_key))
        if sport is None:
            sport = m.Sport(key=sport_key, name="Test")
            s.add(sport)
            s.flush()
        a = repo.upsert_athlete(s, sport, name, nationality="SUI")
        s.commit()
        return a.id


def test_registry_and_engine_follow_the_project(two):
    first, second = two
    assert projects.default().key == "eishockey"
    assert [p.key for p in projects.all_projects()] == ["eishockey", "zwei"]
    assert projects.get("zwei").wording.person == "Sportlerin oder Sportler"
    assert get_engine().url.database == first.url.database
    with projects.use("zwei"):
        assert get_engine().url.database == second.url.database
        assert (
            make_url(get_settings().database_url).database == make_url(SECOND_DB).database
        )  # also with ?host=
    assert get_engine().url.database == first.url.database


def test_project_settings_from_the_app_override_env(two):
    _, second = two
    with Session(second) as s:
        s.add(m.ProjectSetting(key="apify_daily_cost_cap_usd", value=0.5))
        s.commit()
    projects.clear_cache()
    with projects.use("zwei"):
        assert get_settings().apify_daily_cost_cap_usd == 0.5
    assert get_settings().apify_daily_cost_cap_usd != 0.5


def test_mcp_connector_per_project(two):
    _, second = two
    _person(second, "Nur Im Zweiten")
    from klugscheisser.mcp_server.server import build_app

    app = build_app(Settings(_env_file=None))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        r = c.post("/mcp/zwei", json=CALL_STATUS, headers=HEADERS)
        assert r.status_code == 200
        data = r.json()["result"]["structuredContent"]
        assert data["projekt"] == "Zwei" and data["counts"]["athletes"] == 1
        r = c.post("/mcp", json=CALL_STATUS, headers=HEADERS)
        assert r.json()["result"]["structuredContent"]["counts"]["athletes"] == 0


def test_web_prefix_remembers_the_project(two):
    _, second = two
    aid = _person(second, "Nur Im Zweiten")
    from klugscheisser.web.project_switch import ProjectMiddleware
    from klugscheisser.web.routes import build_web_app

    app = ProjectMiddleware(build_web_app(Settings(_env_file=None, web_answer_engine="fake")))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get(f"/person/{aid}").status_code == 404  # default project: not there
        r = c.get("/p/zwei/")
        assert r.status_code == 200 and "ks_project=zwei" in r.headers["set-cookie"]
        r = c.get(f"/person/{aid}")  # cookie: still project zwei without the prefix
        assert r.status_code == 200 and "Nur Im Zweiten" in r.text
        assert c.get("/p/gibtsnicht/").status_code == 404


def test_copy_person_between_projects(two):
    first, second = two
    from klugscheisser.seed.loader import load_all_seeds

    with Session(first) as s:
        load_all_seeds(s)
        a = repo.resolve_athlete(s, "Lara Stalder")
        item = m.Item(
            source_type="srf",
            platform="rss",
            url="https://www.srf.ch/x",
            title="Stalder",
            text="Lara Stalder studiert.",
            content_hash="h1",
        )
        s.add(item)
        s.flush()
        s.add(m.ItemAthlete(item_id=item.id, athlete_id=a.id))
        s.add(
            m.Fact(
                athlete_id=a.id,
                kind="fun_fact",
                value={"text": "x"},
                value_text="x",
                source="funfacts",
                source_item_id=item.id,
                source_url=item.url,
                quote="Lara Stalder studiert.",
                confidence=1.0,
                status="confirmed",
            )
        )
        s.commit()
        with Session(second) as dst:
            out = copy_person(s, dst, a)
            dst.commit()
            n_facts = s.scalar(select(func.count()).select_from(m.Fact).where(m.Fact.athlete_id == a.id))
            assert (
                out["neu"] and out["dokumente"] == 1 and out["fakten"] == n_facts
            )  # + captaincy from roster
            again = copy_person(s, dst, a)
            assert not again["neu"]
            assert dst.scalar(select(func.count()).select_from(m.Item)) == 1
            fact = dst.scalar(select(m.Fact).where(m.Fact.kind == "fun_fact"))
            assert fact.source_item_id == dst.scalar(select(m.Item.id))


def test_briefing_versions_and_prompt(db):
    from klugscheisser import briefing

    with Session(db) as s:
        assert briefing.prompt_block(s) == ("", None)
        v1 = briefing.save(s, text="Persönliches vor Interviews, keine Taktik.")
        assert briefing.save(s, text="Persönliches vor Interviews, keine Taktik.").id == v1.id  # unchanged
        block, vid = briefing.prompt_block(s)
        assert vid == v1.id and "keine Taktik" in block and "nie eine Quelle" in block
        v2 = briefing.save(s, learned="- Fragt oft nach Studium.")
        assert v2.id != v1.id and v2.text == v1.text
        s.commit()


def test_briefing_goes_into_answer_prompt_and_cache_key(db):
    from klugscheisser import briefing
    from klugscheisser.answer import cache
    from klugscheisser.answer.engine import GeminiAnswerEngine

    seen = {}

    def llm(system, prompt, schema):
        seen["system"] = system
        return {"statements": [], "luecken": []}, {"input": 0, "output": 0}

    with Session(db) as s:
        briefing.save(s, text="Keine Taktik.")
        s.commit()
    engine = GeminiAnswerEngine(Settings(_env_file=None), llm=llm)
    engine._ask_model("Frage: x")
    assert "Keine Taktik." in seen["system"]
    assert cache.key("x", [], None, None, None, True, 1) != cache.key("x", [], None, None, None, True, 2)
    assert cache.key("x", [], None, None, None, True) == cache.key("x", [], None, None, None, True, None)


def test_learned_suggestion_needs_her_ok(db):
    from klugscheisser import briefing

    with Session(db) as s:
        chat = m.Chat(title="t")
        s.add(chat)
        s.flush()
        for q in ["Wer studiert?", "Studium von Lara?", "Wer studiert Medizin?", "Hobbys?", "Studium?"]:
            s.add(m.ChatMessage(chat_id=chat.id, role="user", text=q))
        s.flush()
        assert briefing.due(s)
        sug = briefing.suggest(s, None, llm=lambda system, prompt: "- Fragt oft nach dem Studium.")
        assert sug.status == "offen" and sug.basis["fragen"] == 5
        assert briefing.prompt_block(s) == ("", None)  # nothing applied silently
        assert not briefing.due(s)  # one open suggestion at a time
        briefing.decide(s, sug, "annehmen", "- Fragt oft nach Studium und Beruf.")
        assert briefing.current(s).learned == "- Fragt oft nach Studium und Beruf."
        assert sug.status == "angenommen"


def test_project_page_and_settings(db):
    from klugscheisser.web.routes import build_web_app

    app = build_web_app(Settings(_env_file=None, web_answer_engine="fake"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        r = c.get("/projekt")
        assert r.status_code == 200 and "Briefing" in r.text and "/mcp/eishockey" in r.text
        assert "Projekt wechseln" in r.text  # switcher in the navbar
        r = c.post(
            "/projekt/briefing", data={"text": "Fokus WM."}, headers={"origin": "http://127.0.0.1:8000"}
        )
        assert r.status_code == 200 and "Fokus WM." in r.text
        r = c.post(
            "/projekt/einstellungen",
            data={"apify_daily_cost_cap_usd": "1,5", "nightly_hour": "30"},
            headers={"origin": "http://127.0.0.1:8000"},
        )
        assert "ungültiger Wert" in r.text
        r = c.post(
            "/projekt/einstellungen",
            data={"apify_daily_cost_cap_usd": "1,5"},
            headers={"origin": "http://127.0.0.1:8000"},
        )
        assert r.status_code == 200
        projects.clear_cache()
        assert get_settings().apify_daily_cost_cap_usd == 1.5
        assert "addMics" in c.get("/").text  # 🎤 dictation script
