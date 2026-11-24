"""v6 step 3: questions across people — understanding, persons, facts sorted by code, relations between
two nations, evidence per person."""

from datetime import date

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import cross
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds

SETTINGS = Settings(_env_file=None, auto_refresh=False)


def test_understand_rules():
    f = cross.understand("Wer ist die Älteste im Olympia-Team?")
    assert (f.typ, f.kind, f.sort, f.limit) == ("fakt", "birth_date", "asc", 1)
    f = cross.understand("Who has the most ig followers in the team")
    assert (f.typ, f.kind, f.sort) == ("fakt", "instagram_followers", "desc")
    f = cross.understand("Welche Spielerinnen aus Schweden und der Schweiz haben schon zusammen gespielt?")
    assert f.typ == "beziehung" and set(f.nations) == {"SWE", "SUI"}
    f = cross.understand("Wer ist vor dem Spiel gut in Form?")
    assert f.typ == "offen_pro_person" and f.days == 14
    assert cross.understand("Die drei Ältesten im Team?").limit is None


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        brynas = m.Team(
            sport_id=sport.id, key="brynas-test", name="Brynäs IF Dam", kind="club", country="SWE"
        )
        s.add(brynas)
        s.flush()
        swe = [repo.upsert_athlete(s, sport, f"Testa Svensson {i}", nationality="SWE") for i in range(10)]
        sui = repo.resolve_athlete(s, "Lara Stalder")
        for a, born in ((sui, date(1994, 5, 15)), (swe[0], date(1990, 1, 1))):
            s.add(
                m.Fact(
                    athlete_id=a.id,
                    kind="birth_date",
                    value={"date": born.isoformat()},
                    value_text=f"{born:%d.%m.%Y}",
                    source="wikidata",
                    source_url="https://www.wikidata.org/wiki/Q1",
                    confidence=1.0,
                    status="confirmed",
                )
            )
        for a in (sui, swe[0]):
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_id=brynas.id,
                    team_name="Brynäs IF Dam",
                    kind="club",
                    season_from=2019,
                    season_to=2020,
                    source="wikidata",
                    source_url="https://www.wikidata.org/wiki/Q2",
                )
            )
        # v8: Sweden has a seeded roster now; the test Swede is in the national team as well
        s.add(m.Affiliation(athlete_id=swe[0].id, team_id=repo.get_team(s, "swe-women").id, season="2025-26"))
        s.commit()
        return db, {"lara": sui.id, "swe0": swe[0].id}


def test_oldest_is_computed_not_written(world):
    world, ids0 = world
    with Session(world) as s:
        ids = [ids0["lara"], ids0["swe0"]]
        stmts, _ = cross.block_facts(s, cross.Form(typ="fakt", kind="birth_date", sort="asc", limit=1), ids)
        assert len(stmts) == 1 and "Testa Svensson 0" in stmts[0].aussage and "01.01.1990" in stmts[0].aussage
        assert stmts[0].url.startswith("https://www.wikidata.org/")


def test_played_together_between_two_nations(world):
    world, _ = world
    with Session(world) as s:
        form = cross.understand("Welche Spielerinnen aus Schweden und der Schweiz haben zusammen gespielt?")
        ids, note, groups = cross.persons_for(s, "Schweden und Schweiz", form)
        assert len(groups) == 2 and "Schweden" in note
        stmts = cross.block_relations(s, groups, ids)
        assert len(stmts) == 1 and "Lara Stalder" in stmts[0].aussage and "Brynäs IF Dam" in stmts[0].aussage
        assert "2019/20" in stmts[0].aussage


def test_evidence_per_person_parallel(world, monkeypatch):
    world, ids0 = world

    class Engine:
        settings = SETTINGS
        calls = 0

        def _write(self, question, docs, who):
            Engine.calls += 1
            return [], [], [], 0, {}

    with Session(world) as s:
        _, _, missing, _, _, n, _skipped = cross.block_evidence(
            Engine(), s, "Wer ist in Form?", [ids0["lara"], ids0["swe0"]], None
        )
        assert missing and n == 0  # no documents: nobody covered, no model call
