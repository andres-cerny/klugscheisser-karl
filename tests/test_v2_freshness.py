from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.rss import DEFAULT_FEEDS
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.jobs import refresh as r
from klugscheisser.mcp_server import tools as t
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures")
# these tests are about the waiting path (MCP_REFRESH_NO_WAIT=false); no-wait: test_nightly_refresh
ON = Settings(
    _env_file=None,
    auto_refresh=True,
    auto_refresh_wait_s=30,
    instagram_source="fixture",
    mcp_refresh_no_wait=False,
)


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr("klugscheisser.mcp_server.tools.get_settings", lambda: ON)
    monkeypatch.setattr("klugscheisser.config.get_settings", lambda: ON)
    with Session(db) as s:
        load_all_seeds(s)
        testa = repo.upsert_athlete(
            s, repo.get_sport(s, "icehockey_women"), "Testa Beispiel", nationality="SUI"
        )
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata"})
        s.commit()
    with respx.mock(assert_all_called=False):
        respx.get(DEFAULT_FEEDS[0].url).mock(
            return_value=httpx.Response(200, content=(FIX / "rss/srf_sport.xml").read_bytes())
        )
        respx.get(url__regex=r".*test-beispiel-trifft-doppelt").mock(
            return_value=httpx.Response(200, text=(FIX / "rss/article_beispiel.html").read_text())
        )
        yield db


def test_first_question_of_the_day_updates_and_waits(world):
    with Session(world) as s:
        out = t.get_athlete_posts(s, "Testa Beispiel")
    assert out["aktualisierung"]["status"] == "aktualisiert"
    assert {p["url"] for p in out["posts"]} >= {"https://www.instagram.com/p/SYN1/"}
    assert out["datenstand"].startswith("Instagram: heute")


def test_second_question_same_day_does_not_update_again(world):
    with Session(world) as s:
        t.get_athlete(s, "Testa Beispiel")
        again = t.get_athlete(s, "Testa Beispiel")
        assert again["aktualisierung"]["status"] == "aktuell"
        assert s.scalar(select(func.count()).select_from(m.Job)) == 1


def test_slow_update_returns_job_and_status_long_polls(world, monkeypatch):
    slow = Settings(_env_file=None, auto_refresh=True, auto_refresh_wait_s=0.2, instagram_source="fixture")
    monkeypatch.setattr("klugscheisser.mcp_server.tools.get_settings", lambda: slow)
    queued = []
    monkeypatch.setattr(
        "klugscheisser.jobs.refresh.executor",
        lambda: type("E", (), {"submit": staticmethod(lambda *a: queued.append(a))})(),
    )
    with Session(world) as s:
        out = t.get_athlete(s, "Testa Beispiel")
        assert out["aktualisierung"]["status"] == "läuft"
        job_id = out["aktualisierung"]["job_id"]
        assert "refresh_status" in out["aktualisierung"]["hinweis"]
        fn, *args = queued[0]
        fn(*args)  # the background job finishes
        status = r.refresh_status(s, job_id, wait_s=5)
    assert status["status"] == "fertig"


def test_search_finds_injury_by_label_not_by_words(world):
    with Session(world) as s:
        t.get_athlete(s, "Testa Beispiel")  # ingest fixtures
        out = t.search_tool(s, ON, "Wer hat sich verletzt?")
    assert out["erkannt"] == {"categories": ["injury"]}
    assert out["treffer"][0]["url"] == "https://www.instagram.com/p/SYN1/"
