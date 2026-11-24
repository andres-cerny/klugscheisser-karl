"""v8 phase 4: deep sources per tier (queries, link checks, teaser-only pages, podcast/video links), budget."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import deep, tiers
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.interfaces import WebPage, WebResult
from klugscheisser.seed.loader import load_all_seeds


def test_queries_by_tier_and_language(db):
    with Session(db) as s:
        load_all_seeds(s)
        tiers.recompute(s)
        stalder = repo.resolve_athlete(s, "Lara Stalder")
        q1 = deep.queries_for(stalder, 1)
        assert len(q1) == 30 and any(q.q.startswith("site:blick.ch") for q in q1)
        assert any(q.endpoint == "news" for q in q1) and len(deep.media_queries(stalder)) == 2
        czech = repo.resolve_athlete(s, "Klára Peslarová")
        q2 = deep.queries_for(czech, 2)
        assert len(q2) == 4 and any("rozhovor" in q.q for q in q2)
        assert all(q.country != "CZ" for q in q2)  # Brave has no CZ: Czech words, no country


def test_media_kind():
    assert deep.media_kind("https://www.youtube.com/watch?v=abc") == "video"
    assert deep.media_kind("https://www.youtube.com/@srfsport") is None  # a channel, not a video
    assert deep.media_kind("https://open.spotify.com/episode/123") == "podcast"
    assert deep.media_kind("https://www.srf.ch/audio/sportpanorama/xyz") == "podcast"
    assert deep.media_kind("https://www.blick.ch/sport/x") is None


class FakeWeb:
    session = None

    def __init__(self):
        self.queries = []

    def search(self, q, limit=5, **kw):
        self.queries.append((q, kw.get("endpoint")))
        if q.startswith('"Lara Stalder"') and kw.get("endpoint") == "web" and q.endswith('"'):
            return [
                WebResult(
                    "https://evz.ch/stalder", "Lara Stalder – EVZ", "Porträt", page_age="2025-12-02T00:00:00"
                ),
                WebResult(
                    "https://www.blick.ch/sport/stalder",
                    "Lara Stalder über ihre Schicksalsschläge",
                    "Blick-Interview",
                ),
                WebResult("https://other.ch/tim", "Tim Stalder wechselt", "Fussball"),
                WebResult(
                    "https://www.youtube.com/watch?v=1",
                    "Lara Stalder im Interview",
                    "SRF Sport",
                    extra={"kanal": "SRF Sport"},
                ),
                WebResult("https://www.instagram.com/lara.stalder7/", "Instagram", ""),
            ]
        return []

    def fetch(self, url):
        if "blick.ch" in url:
            return WebPage(url=url, title="", text=None, allowed=False, note="Nutzungsbedingungen")
        if "other.ch" in url:
            return WebPage(
                url=url, title="Tim Stalder wechselt", text="Tim Stalder, Fussballer, wechselt.", allowed=True
            )
        return WebPage(
            url=url,
            title="Lara Stalder – EVZ",
            text="Lara Stalder ist Captain des EVZ Women's Team.",
            allowed=True,
        )


class Jev:
    def ask(self, state, questions):
        p = 0.02 if "Tim Stalder" in state or "Fussball" in state else 0.95
        return {k: {"noul": p > 0.5, "probabilities": {"true": p, "false": 1 - p}} for k in questions}


def test_web_for_person_reads_checks_and_stores(db, monkeypatch):
    from klugscheisser.ingest import runner

    settings = Settings(_env_file=None)
    real = runner.build_context

    def ctx_with_jev(s, st):
        ctx = real(s, st)
        ctx.jev_client = Jev()
        return ctx

    monkeypatch.setattr(runner, "build_context", ctx_with_jev)
    monkeypatch.setattr(deep, "session_scope", _scope(db))
    with Session(db) as s:
        load_all_seeds(s)
        tiers.recompute(s)
        aid = repo.resolve_athlete(s, "Lara Stalder").id
        s.commit()
    report = deep.DeepReport()
    web = FakeWeb()
    deep.web_for_person(settings, aid, 1, deep.DeepBudget(5.0), report, web=web)
    assert report.queries == 32 and report.stored == 1 and report.teaser_only == 1 and report.media_links == 1
    assert report.not_her == 1  # Tim Stalder, footballer
    with Session(db) as s:
        items = {
            i.url: i
            for i in s.scalars(select(m.Item).join(m.ItemAthlete).where(m.ItemAthlete.athlete_id == aid))
        }
        blick = items["https://www.blick.ch/sport/stalder"]
        assert blick.raw["teaser_only"] and "Schicksalsschläge" in blick.text
        video = items["https://www.youtube.com/watch?v=1"]
        assert (video.source_type, video.platform, video.source_name) == ("media_link", "video", "SRF Sport")
        assert "https://other.ch/tim" not in items and not any("instagram.com" in u for u in items)
    again = deep.DeepReport()
    deep.web_for_person(settings, aid, 1, deep.DeepBudget(5.0), again, web=FakeWeb())
    assert again.stored == 0 and again.teaser_only == 0  # known URLs are skipped


def test_budget_counts_ledger_since_start(db, monkeypatch):
    monkeypatch.setattr(deep, "session_scope", _scope(db))
    start = datetime.now(UTC)
    with Session(db) as s:
        s.add(m.CostLedger(source="apify", units=1, cost_usd=0.7, note="x"))
        s.commit()
        b = deep.DeepBudget(1.0, start)
        assert round(b.spent(s), 2) == 0.7 and round(b.left(s), 2) == 0.3


def _scope(engine):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        with Session(engine) as s:
            yield s
            s.commit()

    return scope
