from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.rss import DEFAULT_FEEDS, RssCollector
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.jobs import refresh as r
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures")
SRF_ONLY = DEFAULT_FEEDS[:1]  # other feeds are not mocked here
SETTINGS = Settings(_env_file=None, instagram_source="fixture")


def run_now(fn, *args):
    fn(*args)


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


@pytest.fixture
def feed():
    with respx.mock:
        respx.get(DEFAULT_FEEDS[0].url).mock(
            return_value=httpx.Response(200, content=(FIX / "rss/srf_sport.xml").read_bytes())
        )
        respx.get(url__regex=r".*test-beispiel-trifft-doppelt").mock(
            return_value=httpx.Response(200, text=(FIX / "rss/article_beispiel.html").read_text())
        )
        yield


def test_refresh_runs_news_now_and_instagram_as_job(world, feed):
    with Session(world) as s:
        out = r.refresh(
            s, SETTINGS, "Testa Beispiel", submit=run_now, news_collector=RssCollector(SRF_ONLY, delay_s=0)
        )
        s.commit()
        assert out["news"].startswith("geprüft: 1 neu")
        assert "erneut fragen" in out["instagram"] and out["job_id"]
        assert any("SRF" in i["quelle"] for i in out["neu_seit_aufruf"])
        status = r.refresh_status(s, out["job_id"])
    assert status["status"] == "fertig" and status["ergebnis"]["neue_posts"] == 3
    assert {i["url"] for i in status["neue_eintraege"]} >= {"https://www.instagram.com/p/SYN1/"}
    assert any(i.get("vorsicht") for i in status["neue_eintraege"])


def test_second_refresh_within_ttl_uses_cache(world, feed):
    with Session(world) as s:
        r.refresh(
            s, SETTINGS, "Testa Beispiel", submit=run_now, news_collector=RssCollector(SRF_ONLY, delay_s=0)
        )
        s.commit()
        again = r.refresh(
            s, SETTINGS, "Testa Beispiel", submit=run_now, news_collector=RssCollector(SRF_ONLY, delay_s=0)
        )
        assert again["news"].startswith("aktuell") and again["instagram"].startswith("aktuell")
        assert "job_id" not in again
        assert s.scalar(select(func.count()).select_from(m.Job)) == 1


def test_running_job_is_reused(world, feed):
    queued = []
    with Session(world) as s:
        first = r.refresh(
            s,
            SETTINGS,
            "Testa Beispiel",
            submit=lambda *a: queued.append(a),
            news_collector=RssCollector(SRF_ONLY, delay_s=0),
        )
        second = r.refresh(
            s,
            Settings(_env_file=None, instagram_source="fixture", ttl_news=0),
            "Testa Beispiel",
            submit=lambda *a: queued.append(a),
            news_collector=RssCollector(SRF_ONLY, delay_s=0),
        )
        assert first["job_id"] == second["job_id"] and len(queued) == 1
        assert r.refresh_status(s, first["job_id"])["status"] == "wartet"


def test_recover_marks_orphaned_jobs_failed(world):
    with Session(world) as s:
        s.add(m.Job(kind="instagram_refresh", params={}, status="running"))
        s.commit()
    assert r.recover_stale_jobs() == 1
    with Session(world) as s:
        assert s.scalar(select(m.Job.error)) == "Server neu gestartet"


def test_refresh_for_team_and_unknown(world, feed):
    with Session(world) as s:
        out = r.refresh(
            s, SETTINGS, "Schweiz", submit=lambda *a: None, news_collector=RssCollector(SRF_ONLY, delay_s=0)
        )
        assert out["ziel"] == "Schweiz"
        assert r.refresh_status(s, 999)["fehler"] == "Kein Auftrag 999."
