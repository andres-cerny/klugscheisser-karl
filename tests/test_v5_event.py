"""v5 phase 4: event preparation (proposal in the background → Ja → persons + event entries + load) and the
nightly refresh window."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.adds import event as ev_mod
from klugscheisser.adds.load import action_budget_usd
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.labels import TZ
from klugscheisser.jobs import nightly
from klugscheisser.seed.loader import load_all_seeds

SETTINGS = Settings(_env_file=None, web_answer_engine="fake")


class FakeResolver:
    llm = None

    def wikidata_team(self, req, excluded, notes):
        class Entity:
            qid, url = "Q1", "https://www.wikidata.org/wiki/Q1"
            coaches = (("Q2", "Testa Trainerin"),)

        return Entity(), []

    def wikipedia_roster(self, entity):
        return None, None, None, None


@pytest.fixture
def wch(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        return s.scalar(select(m.Event.id).where(m.Event.key == "wch-2026"))


def test_event_proposal_roster_plus_coach_and_confirm(db, wch, monkeypatch):
    with Session(db) as s:
        event = s.get(m.Event, wch)
        sui_id = s.scalar(select(m.Team.id).where(m.Team.key == "sui-women"))
        req = ev_mod.propose_event(s, SETTINGS, event, [sui_id], submit=lambda *a: None)
        assert req.status == "resolving" and req.input["nightly_to"] == "2026-11-16"
        req_id = req.id
    ev_mod._resolve_job(req_id, SETTINGS, resolver=FakeResolver())
    with Session(db) as s:
        req = s.get(m.AddRequest, req_id)
        assert req.status == "proposed"
        names = [p["name"] for p in req.proposal["persons"]]
        assert "Testa Trainerin" in names and len(names) > 10  # stored roster + coach from Wikidata
        assert "Schweiz" in req.proposal["summary"] and "Trainer/Staff" in req.proposal["summary"]
        ev_mod.confirm_event(s, SETTINGS, req, submit=lambda *a: None)
        coach = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Testa Trainerin"))
        assert coach.role == "coach"
        entry = s.scalar(select(m.EventEntry).where(m.EventEntry.athlete_id == coach.id))
        assert entry.event_id == wch and entry.team_id == sui_id and entry.role == "coach"
        assert req.status == "loading" and action_budget_usd(SETTINGS, req) == pytest.approx(40 / 0.8)


def test_event_prep_web_page_from_proposal(client_free=None):
    assert ev_mod.estimate_event_cost_chf(SETTINGS, 30, 30) < SETTINGS.event_prep_cap_chf


def test_nightly_window_and_due(db, wch):
    with Session(db) as s:
        s.add(
            m.AddRequest(
                kind="event",
                status="done",
                input={"event_key": "wch-2026", "nightly_from": "2026-11-03", "nightly_to": "2026-11-16"},
                athlete_ids=[1, 2],
            )
        )
        s.commit()
        assert ev_mod.nightly_persons(s, date(2026, 11, 10)) == [1, 2]
        assert ev_mod.nightly_persons(s, date(2026, 11, 20)) == []
        early = datetime(2026, 11, 10, 1, 0, tzinfo=TZ)
        late = datetime(2026, 11, 10, 6, 0, tzinfo=TZ)  # NIGHTLY_HOUR 5 since 2026-10-11
        assert not nightly.due(s, SETTINGS, early) and nightly.due(s, SETTINGS, late)
        s.add(
            m.CacheEntry(
                source="nightly", target="events", fetched_at=datetime(2026, 11, 10, 3, 30, tzinfo=UTC)
            )
        )
        s.commit()
        assert not nightly.due(s, SETTINGS, late)


def test_event_page_has_prep_form(db, wch):
    from starlette.testclient import TestClient

    from klugscheisser.web import build_web_app

    with TestClient(build_web_app(SETTINGS)) as c:
        html = c.get("/event/wch-2026").text
        assert "Vorbereitung für diesen Anlass" in html and 'name="teams" value="schweiz"' in html
