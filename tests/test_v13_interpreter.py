"""v13: Gemini reads the question into a template first (answer/interpret.py); the rules answer the template,
the original question goes to the document search and the writer. Plus the new direct lookups (coach, form,
next/last game of one team, youth club) and the profile safety net."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import cross, interpret, lookups
from klugscheisser.answer import engine as eng
from klugscheisser.answer.engine import GeminiAnswerEngine
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.resolve_athlete(s, "Lara Stalder")
        s.add(
            m.EpProfile(
                athlete_id=lara.id,
                ep_id="369459",
                url="https://www.eliteprospects.com/player/369459",
                fetched_at=datetime(2026, 10, 4, tzinfo=UTC),
                raw={},
                data={
                    "form": {"last5": {"games": 5, "goals": 3, "assists": 4, "points": 7, "plus_minus": -1}}
                },
            )
        )
        s.add(
            m.Fact(
                athlete_id=lara.id,
                kind="youth_team",
                value={"text": "HC Luzern"},
                value_text="HC Luzern",
                source="eliteprospects",
                source_url="https://www.eliteprospects.com/player/369459",
                status="confirmed",
                extractor="eliteprospects",
            )
        )
        club = m.Team(sport_id=sport.id, key="frolunda-test", name="Frölunda HC", kind="club", country="SWE")
        other = m.Team(
            sport_id=sport.id, key="farjestad-test", name="Färjestad BK Dam", kind="club", country="SWE"
        )
        s.add_all([club, other])
        s.flush()
        soon = datetime.now(UTC) + timedelta(days=4)
        s.add(
            m.Event(
                key="g-next",
                name="Färjestad – Frölunda",
                kind="game",
                starts_at=soon,
                status="scheduled",
                home_team_id=other.id,
                away_team_id=club.id,
                venue="Löfbergs Arena",
                source_url="https://sdhl/1",
            )
        )
        s.commit()
        return db, {"lara": lara.id}


# --- interpreter --------------------------------------------------------------------------------------------


def test_interpret_parses_and_falls_back():
    seen = {}

    def ask(system, prompt, schema):
        seen.update(system=system, prompt=prompt)
        return {"art": "trainer", "frage": "Wer ist der Trainer von Finnland?"}, {"input": 900, "output": 20}

    it = interpret.interpret(ask, "Who coaches Finland?")
    assert it.rules_question == "Wer ist der Trainer von Finnland?" and it.usage["input"] == 900
    assert "trainer: 'Wer ist der Trainer von <Team oder Nation>?'" in seen["system"]
    assert interpret.changed("Who coaches Finland?", it)
    # documents / unclear: the rules read the original question
    doc = interpret.interpret(lambda *a: ({"art": "dokumente", "frage": "Was gibt es Neues?"}, {}), "News?")
    assert doc.rules_question is None and not interpret.changed("News?", doc)
    # a broken or odd reply never blocks the answer
    assert interpret.interpret(lambda *a: (_ for _ in ()).throw(TimeoutError("slow")), "x") is None
    assert interpret.interpret(lambda *a: ({"art": "quatsch", "frage": "x"}, {}), "x") is None
    # follow-ups: the last turns go along so pronouns can be resolved
    interpret.interpret(
        ask, "and her club?", [{"frage": "Wer ist die Älteste?", "saetze": ["Lara Stalder."]}]
    )
    assert "Lara Stalder." in seen["prompt"]


TEMPLATES = [
    "Wie alt ist Lara Stalder?",
    "Mit welcher Hand schiesst Lara Stalder?",
    "Juniorenklub von Lara Stalder",
    "Hat Lara Stalder Geschwister im Eishockey?",
    "Wer ist die älteste Spielerin im Schweizer Olympia-Kader?",
    "Wie viele Spielerinnen im Schweizer Olympia-Kader sind über 30?",
    "Haben Lara Stalder und Andrea Brändli schon im selben Club gespielt?",
    "Welche Schwedinnen haben mit Schweizerinnen im selben Club gespielt?",
]


@pytest.mark.parametrize("q", TEMPLATES)
def test_fact_and_relation_templates_are_understood(q):
    assert cross.understand(q).typ in ("fakt", "beziehung")


def test_lookup_templates_are_understood():
    assert lookups.COACH.search("Wer ist der Trainer von Finnland?")
    assert lookups.FORM.search("Form von Lara Stalder in den letzten 5 Spielen")
    assert lookups.NEXT.search("Wann spielt Frölunda HC das nächste Mal?")
    assert lookups.LAST.search("Wie ist das letzte Spiel von Frölunda HC ausgegangen?")


# --- direct lookups ---------------------------------------------------------------------------------------------


def test_coach_form_youth_and_next_game_from_data(world):
    db, w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(s, "Wer ist der Trainer von Finnland?", [], None)
        assert pi.docs[0].extra["direct"]["summary"] == "Trainer von Finnland: Tero Lehterä"  # seeded roster
        lara = s.get(m.Athlete, w["lara"])
        pi = cross.prosa_inputs(s, "Form von Lara Stalder in den letzten 5 Spielen", [lara], None)
        d = pi.docs[0].extra["direct"]
        assert (
            "5 Spiele, 3 Tore, 4 Assists, 7 Punkte, +/- -1" in d["summary"]
            and "Stand 04.10.2026" in d["summary"]
        )
        pi = cross.prosa_inputs(s, "Juniorenklub von Lara Stalder", [lara], None)
        assert "HC Luzern" in pi.docs[0].text
        pi = lookups.team_game_inputs(s, "Wann spielt Frölunda HC das nächste Mal?")
        assert pi is not None and "Färjestad BK Dam – Frölunda HC" in pi.docs[0].extra["direct"]["summary"]


def test_profile_safety_net_carries_stored_fields(world):
    db, w = world
    with Session(db) as s:
        docs = eng.fact_docs(s, [s.get(m.Athlete, w["lara"])])
    texts = " ".join(d.text for d in docs)
    assert "Juniorenklub: HC Luzern" in texts and "letzte 5 Spiele: 5 Spiele, 3 Tore" in texts


# --- engine ------------------------------------------------------------------------------------------------------


def test_engine_rules_read_the_template_and_note_it(world, monkeypatch):
    seen = {}

    def llm(system, prompt, schema):
        if "art" in schema.get("properties", {}):  # the interpreter call
            return {"art": "trainer", "frage": "Wer ist der Trainer von Finnland?"}, {
                "input": 800,
                "output": 15,
            }
        raise AssertionError("no writer call: the coach comes from data")

    real = cross.prosa_inputs

    def spy(s, q, a, h=None):
        seen["rules_q"] = q
        return real(s, q, a, h)

    monkeypatch.setattr(cross, "prosa_inputs", spy)
    e = GeminiAnswerEngine(
        SETTINGS, llm=llm, support_checker=lambda pairs: [0.9] * len(pairs), interpreter=True
    )
    res = e.answer("Who is in charge of the Finnish team behind the bench?", use_cache=False, style="prosa")
    assert seen["rules_q"] == "Wer ist der Trainer von Finnland?"
    assert res.scope_hinweis.startswith("Gelesen als: «Wer ist der Trainer von Finnland?»")
    assert res.antwort[0].saetze[0].text.startswith("Trainer von Finnland: Tero Lehterä")


def test_engine_without_interpreter_unchanged(world):
    """Injected llm without interpreter=True (all older tests): no reading step at all."""
    e = GeminiAnswerEngine(SETTINGS, llm=lambda *a: ({"antwort": [], "luecken": []}, {}))
    assert e._use_interpreter() is False


def test_short_club_name_and_awards(world):
    db, w = world
    with Session(db) as s:
        pi = lookups.team_game_inputs(s, "Wann spielt Frölunda das nächste Mal?")
        assert pi is not None and "Frölunda HC" in pi.docs[0].extra["direct"]["summary"]
        lara = s.get(m.Athlete, w["lara"])
        for kind, text, season in (
            ("award", "SDHL Most Points (89), 2021/22", 2021),
            ("medal", "Olympische Spiele 2026: Bronze", 2025),
        ):
            s.add(
                m.Fact(
                    athlete_id=lara.id,
                    kind=kind,
                    value={"text": text, "season": season},
                    value_text=text,
                    season=season,
                    source="eliteprospects",
                    source_url="https://ep/1",
                    status="confirmed",
                    extractor="eliteprospects",
                )
            )
        s.flush()
        pi = lookups.award_inputs(
            s, "Welche Auszeichnungen hat Lara Stalder in der Saison 2025/26 gewonnen?", [lara]
        )
        d = pi.docs[0].extra["direct"]
        assert [r[1] for r in d["rows"]] == ["Olympische Spiele 2026: Bronze"] and "2025/26" in d["summary"]
        pi = lookups.award_inputs(s, "Welche Auszeichnungen hat Lara Stalder gewonnen?", [lara])
        assert pi.docs[0].extra["direct"]["rows"][0][1].startswith("Olympische Spiele")  # medals first


def test_interpreter_prompt_forbids_added_years():
    assert "Füge NIE ein Jahr" in interpret.SYSTEM and "auszeichnungen" in interpret.ARTEN


def test_readings_are_cached_and_cost_once():
    from klugscheisser.answer import engine as e_mod

    e_mod._READINGS.clear()
    calls = []

    def read():
        calls.append(1)
        return interpret.Interpretation("fakt", "Wie alt ist Lara Stalder?", usage={"input": 900})

    first = e_mod._cached_reading("Wie alt ist Lara Stalder?", None, read)
    again = e_mod._cached_reading("wie alt ist lara stalder", None, read)
    assert len(calls) == 1 and first.usage == {"input": 900} and again.usage == {}
    e_mod._cached_reading("Wie alt ist Lara Stalder?", [{"frage": "andere"}], read)  # other chat turn
    assert len(calls) == 2


def test_fast_path_skips_reading_when_rules_answer_directly(world):
    calls = []

    def llm(system, prompt, schema):
        calls.append(schema)
        return {"art": "trainer", "frage": "Wer ist der Trainer von Finnland?"}, {}

    e = GeminiAnswerEngine(
        SETTINGS, llm=llm, support_checker=lambda pairs: [0.9] * len(pairs), interpreter=True
    )
    res = e.answer("Wer ist der Trainer von Finnland?", use_cache=False, style="prosa")
    assert res.antwort[0].saetze[0].text.startswith("Trainer von Finnland") and calls == []  # no reading
    e.answer("Who is in charge of the Finnish team behind the bench?", use_cache=False, style="prosa")
    assert len(calls) == 1  # a wording no rule knows needs the reading step
