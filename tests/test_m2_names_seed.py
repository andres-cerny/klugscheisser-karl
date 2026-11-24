import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.wikidata import parse_memberships, parse_players
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.names import name_in_handle, name_variants, normalize
from klugscheisser.handles.service import VERIFIED_AT, add_handle_candidate, clean_handle, score, verify
from klugscheisser.seed.loader import load_all_seeds, load_wikidata_players
from klugscheisser.sports import get_sport

FIX = Path("tests/fixtures/wikidata")


def wikidata_players():
    sport = get_sport("icehockey_women")
    players = parse_players(json.loads((FIX / "players_sui.json").read_text()), "SUI", sport)
    parse_memberships(json.loads((FIX / "memberships_sui.json").read_text()), players)
    return list(players.values())


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        load_wikidata_players(s, wikidata_players())
        s.commit()
    return db


def test_normalize_and_variants():
    assert normalize("Lena-Marie Lütz") == "lena marie lutz"
    assert normalize("Laure Mériguet") == "laure meriguet"
    assert name_variants("Alina Müller") == {"alina muller", "alina mueller"}
    assert "lena lutz" in name_variants("Lena-Marie Lutz")


def test_name_in_handle():
    assert name_in_handle("Alina Müller", "alinamueller_") == 1.0
    assert name_in_handle("Andrea Brändli", "andrea.braendli") == 1.0
    assert name_in_handle("Laura Zimmermann", "zimmyla") == 0.0


def test_handle_scoring_ladder():
    assert score([{"kind": "wikidata"}]) == 0.5
    assert score([{"kind": "wikidata"}, {"kind": "profile_bio"}, {"kind": "profile_name"}]) >= VERIFIED_AT
    assert score([{"kind": "wikidata"}, {"kind": "name_in_handle"}]) < VERIFIED_AT
    assert clean_handle("https://www.instagram.com/alinamueller_/?hl=de") == "alinamueller_"


def test_wikidata_fixture_parse():
    players = {p.name: p for p in wikidata_players()}
    alina = players["Alina Müller"]
    assert alina.instagram == ["alinamueller_"]
    assert alina.position == "F"
    assert "Alina Mueller" in alina.aliases
    assert any(t.team_label == "Boston Fleet" for t in alina.teams)


@pytest.mark.parametrize("query", ["Alina Müller", "alina mueller", "ALINA MULLER", "Alina Muler"])
def test_find_athlete_spellings(seeded, query):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, query)
        assert a is not None and a.full_name == "Alina Müller"


def test_roster_and_wikidata_merge_into_one_athlete(seeded):
    with Session(seeded) as s:
        lutz = repo.find_athletes(s, "Lena Marie Lutz")
        assert len(lutz) == 1
        a = lutz[0].athlete
        assert a.wikidata_qid and a.birth_date is not None
        # 2026-10-28: Alina Müller (Lara Stalder is injured and off the WM list since 26.10.2026)
        alina = repo.find_athletes(s, "Alina Müller")[0].athlete
        entries = s.scalars(select(m.EventEntry).where(m.EventEntry.athlete_id == alina.id)).all()
        assert {e.list_type for e in entries} == {
            "confirmed"
        }  # Olympia, EHT lists, official WM list (01.11.2026)


def test_wch_roster_is_official_and_sourced(seeded):
    with Session(seeded) as s:
        wch = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        entries = s.scalars(
            select(m.EventEntry).where(m.EventEntry.event_id == wch.id, m.EventEntry.team_id == sui.id)
        ).all()
        crew = [
            e for e in entries if e.role not in (None, "G", "D", "F")
        ]  # v19: staff copied from the base list
        assert "Shannon Miller" in {e.athlete.full_name for e in crew}
        entries = [e for e in entries if e not in crew]
        # official WM list (SIHF 01.11.2026): Södertälje squad minus Aymon, Gaberell, Surdez; Dalessi for Stalder
        assert len(entries) == 25
        assert all(e.list_type == "confirmed" and "01.11.2026" in e.source for e in entries)
        names = {e.athlete.full_name for e in entries}
        assert "Elisa Dalessi" in names and not names & {"Jade Surdez", "Lara Stalder"}
        numbers = {e.athlete.full_name: e.jersey_number for e in entries}
        assert numbers["Alina Müller"] == 25  # number from her latest roster with numbers (Olympia)
        assert "Lara Stalder" not in numbers and "Elisa Dalessi" in numbers


def test_seeding_is_idempotent(seeded):
    with Session(seeded) as s:
        before = s.scalar(select(m.Athlete.id).order_by(m.Athlete.id.desc()))
        load_all_seeds(s)
        load_wikidata_players(s, wikidata_players())
        s.commit()
        assert s.scalar(select(m.Athlete.id).order_by(m.Athlete.id.desc())) == before


def test_handle_status_flow(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Laura Zimmermann")
        h = next(h for h in a.handles if h.handle == "zimmyla")
        assert h.status == "wahrscheinlich"
        verify(h, "test")
        assert h.status == "verified" and h.verified_by == "test"
        other = add_handle_candidate(s, a, "instagram", "@random.fan", {"kind": "search_hit"})
        assert other.status == "candidate" and not other.usable
