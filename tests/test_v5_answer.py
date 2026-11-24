"""v5 phase 1: fast answers from our database (no waiting, no web search), support check with context,
documents about her teams, no unlinked documents for questions without a person."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import engine as eng
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.retrieval.search import Filters, list_items

SETTINGS = Settings(_env_file=None, auto_refresh=False)


def test_quote_context_finds_surrounding_text():
    text = "A" * 500 + " Zusammen mit @lara.stalder7 haben wir den Alphabite entwickelt. " + "B" * 500
    ctx = eng.quote_context("Zusammen mit @lara.stalder7 haben wir den Alphabite entwickelt.", text, width=50)
    assert "Alphabite" in ctx and ctx.startswith("A") and ctx.rstrip().endswith("B")
    assert eng.quote_context("nicht im Text", "kurz") == "kurz"


def test_support_check_sends_source_and_context_to_jev(monkeypatch):
    seen = {}

    class FakeJev:
        def __init__(self, settings):
            self.usage = {"input_tokens": 10}

        def ask(self, state, questions):
            seen["state"], seen["questions"] = state, questions
            return {k: {"noul": True, "probabilities": {"true": 0.9, "false": 0.1}} for k in questions}

    monkeypatch.setattr("klugscheisser.classify.jev.JevClient", FakeJev)
    doc = eng.Doc(
        key="D1",
        text="Narthex Dental: Zusammen mit @lara.stalder7 haben wir den Alphabite entwickelt.",
        meta="[Instagram @narthexdental, 01.10.] · über: Lara Stalder",
        url="https://x",
        quelle="[X]",
        zeile_prefix="🏷️",
    )
    e = eng.GeminiAnswerEngine(Settings(_env_file=None, typesafe_api_key="k"))
    scores = e._support_scores(
        [("haben wir den Alphabite entwickelt", "Sie hat Alphabite mitentwickelt.", doc)]
    )
    assert scores == [pytest.approx(0.9, abs=0.05)]
    assert "über: Lara Stalder" in seen["state"] and "Narthex Dental" in seen["state"]
    assert "Pronomen" in seen["questions"]["p0"]["instructions"]


@pytest.fixture
def people(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.upsert_athlete(s, sport, "Lara Testalder", nationality="SUI")
        team = m.Team(sport_id=sport.id, key="evz-test", name="EV Testzug Frauen", kind="club")
        s.add(team)
        s.flush()
        s.add(m.Affiliation(athlete_id=lara.id, team_id=team.id, source="Test"))
        now = datetime.now(UTC)
        about_her = m.Item(
            source_type="srf",
            platform="rss",
            url="https://a",
            title="Lara Testalder trifft",
            text="Lara Testalder trifft doppelt.",
            content_hash="1",
            published_at=now,
        )
        about_team = m.Item(
            source_type="srf",
            platform="rss",
            url="https://b",
            title="EV Testzug verliert",
            text="Das Team verliert 1:4.",
            content_hash="2",
            published_at=now,
        )
        golf = m.Item(
            source_type="web",
            platform="web",
            url="https://c",
            title="Golf",
            text="Ein Golfer studiert Betriebswirtschaft.",
            content_hash="3",
            published_at=now,
        )
        s.add_all([about_her, about_team, golf])
        s.flush()
        about_her.athletes.append(m.ItemAthlete(athlete_id=lara.id))
        about_team.teams.append(m.ItemTeam(team_id=team.id))
        s.commit()
        return {"lara": lara.id, "team": team.id}


def test_person_questions_also_see_her_team_reports(db, people):
    with Session(db) as s:
        lara = s.get(m.Athlete, people["lara"])
        assert eng.person_team_ids(s, [lara]) == [people["team"]]
        urls = {i.url for i in eng.candidate_items(s, SETTINGS, "Wie lief das Spiel?", [lara], None)}
        assert {"https://a", "https://b"} <= urls and "https://c" not in urls
        doc = eng.item_doc(
            next(i for i in list_items(s, Filters(team_ids=[people["team"]])) if i.url == "https://b")
        )
        assert "Team EV Testzug Frauen" in doc.meta


def test_question_without_person_skips_unlinked_documents(db, people):
    with Session(db) as s:
        urls = {i.url for i in list_items(s, Filters(linked_only=True))}
        assert urls == {"https://a", "https://b"}


def test_answer_never_waits_and_never_searches_the_web(db, people, monkeypatch):
    calls = {}

    def fake_fresh(s, settings, athletes, wait_s=None, submit=None, fast_sources_now=True):
        from klugscheisser.jobs.freshness import Freshness

        calls["wait_s"], calls["fast"] = wait_s, fast_sources_now
        return Freshness("läuft", 1)

    monkeypatch.setattr("klugscheisser.jobs.freshness.ensure_fresh", fake_fresh)

    class NoWeb:
        def search(self, *a, **k):
            raise AssertionError("web search must be off while answering")

    e = eng.GeminiAnswerEngine(
        Settings(_env_file=None, auto_refresh=True),
        llm=lambda system, prompt, schema: ({"statements": [], "luecken": []}, {}),
        support_checker=lambda pairs: [1.0] * len(pairs),
        web_search=NoWeb(),
    )
    res = e.answer("Was gibt es Neues?", person_ids=[people["lara"]])
    assert calls == {"wait_s": 0, "fast": False}
    assert res.aktualisierung and "Hintergrund" in res.aktualisierung
