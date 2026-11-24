"""v5 phase 6: connector tools to add a missing person from the chat app (proposal → ja → load)."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.adds import flow
from klugscheisser.adds.base import Proposal, ProposedPerson, SourceRef
from klugscheisser.db import models as m
from klugscheisser.mcp_server import server
from klugscheisser.seed.loader import load_all_seeds


@pytest.fixture
def seeded(db, monkeypatch):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()

    def resolve(settings, s, req, previous):
        p = ProposedPerson(
            name="Hanna Testsson",
            nationality="SWE",
            birth_date=date(1999, 1, 20),
            sources=[SourceRef("Wikidata Q1", "https://www.wikidata.org/wiki/Q1")],
        )
        return Proposal(
            kind="person",
            found=True,
            summary="Hanna Testsson (SWE), Eishockeyspielerin",
            persons=[p],
            notes=["präzisiert"] if req.refine_text else [],
        )

    monkeypatch.setattr(flow, "_resolve", resolve)
    monkeypatch.setattr(
        "klugscheisser.jobs.refresh.executor",
        lambda: type("X", (), {"submit": lambda self, *a, **k: None})(),
    )
    monkeypatch.setattr("klugscheisser.config.Settings.auto_refresh_wait_s", 0.0, raising=False)
    return db


def test_add_from_chat_app(seeded):
    text = server.hinzufuegen("Hanna Testsson", details="schwedische Stürmerin")
    assert (
        "Anfrage #1 (person): Hanna Testsson (SWE)" in text
        and "[Wikidata Q1](https://www.wikidata.org/wiki/Q1)" in text
    )
    assert "hinzufuegen_antwort(anfrage=1" in text
    assert "präzisiert" in server.hinzufuegen_antwort(1, "präzisieren", "Jahrgang 1999")
    out = server.hinzufuegen_antwort(2, "ja")
    assert "Hanna Testsson" in out and ("geladen" in out or "Fertig" in out)
    with Session(seeded) as s:
        assert s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Hanna Testsson")) is not None
    assert "nicht offen" in server.hinzufuegen_antwort(2, "ja")


def test_unknown_person_hint_in_frage(seeded):
    res = server.frage("Was gibt es Neues?", person="Niemand Unbekannt")
    assert "hinzufuegen(name='Niemand Unbekannt'" in res.content[0].text
