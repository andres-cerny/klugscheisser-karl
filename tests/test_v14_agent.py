"""v14: the second stage (agent): Jev gate, plan, tools (data package, match reports, stats, search), round and
cost limits, and the engine using stage 2 only when the fast answer is not enough."""

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import agent
from klugscheisser.answer import engine as eng
from klugscheisser.answer.engine import GeminiAnswerEngine
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.interfaces import AnswerBlock, AnswerResult, AnswerSentence, Statement
from klugscheisser.seed.loader import load_all_seeds

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        lara = repo.resolve_athlete(s, "Lara Stalder")
        rahel = repo.resolve_athlete(s, "Rahel Enzler")
        s.add(
            m.Fact(
                athlete_id=lara.id,
                kind="appearances",
                value={"number": 200},
                value_text="200 Länderspiele",
                source="wikipedia",
                source_url="https://de.wikipedia.org/wiki/Lara_Stalder",
                status="confirmed",
                extractor="wikipedia",
            )
        )
        s.add(
            m.Item(
                platform="sihf",
                source_type="official_result",
                source_name="SIHF",
                url="https://sihf/game/1",
                external_id="g1",
                content_hash="g1-hash",
                title="SC Bern – EV Zug 1:3",
                text="SC Bern – EV Zug 1:3",
                published_at=datetime(2026, 9, 26, tzinfo=UTC),
                raw={
                    "home": "SC Bern",
                    "away": "EV Zug",
                    "score": "1:3",
                    "goals": [
                        {
                            "team": "EV Zug",
                            "time": "14:49",
                            "score": "0:1",
                            "scorer": "Rahel Enzler",
                            "assists": ["Lara Stalder"],
                        },
                        {
                            "team": "EV Zug",
                            "time": "47:54",
                            "score": "1:2",
                            "scorer": "Lara Stalder",
                            "assists": ["Rahel Enzler"],
                        },
                        {
                            "team": "SC Bern",
                            "time": "40:26",
                            "score": "1:1",
                            "scorer": "Lea MacLeod",
                            "assists": [],
                        },
                    ],
                },
            )
        )
        s.commit()
        return db, {"lara": lara.id, "rahel": rahel.id}


def _res(*sentences, berechnet=False, rueckfrage=None):
    blocks = [AnswerBlock(typ="text", saetze=[AnswerSentence(t) for t in sentences])] if sentences else []
    st = [Statement("a", "z", None, "u", "q", "q", "", berechnet=berechnet)] if sentences else []
    return AnswerResult(
        frage="q", datenstand="", statements=st, stil="prosa", antwort=blocks, rueckfrage=rueckfrage
    )


def test_gate():
    run = agent.AgentRun()
    assert agent.needs_agent(SETTINGS, "q", _res(), run, jev=lambda *a: 0.9) is True  # nothing found
    assert agent.needs_agent(SETTINGS, "q", _res(rueckfrage="Wen?"), run, jev=lambda *a: 0.0) is False
    assert (
        agent.needs_agent(SETTINGS, "q", _res("Ergebnis: 5", berechnet=True), run, jev=lambda *a: 0.0)
        is False
    )
    assert agent.needs_agent(SETTINGS, "q", _res("Sie ist gut."), run, jev=lambda *a: 0.2) is True  # thin
    assert (
        agent.needs_agent(SETTINGS, "q", _res("Sie hat 200 Länderspiele."), run, jev=lambda *a: 0.9) is False
    )


def test_tools_data_package_and_match_reports(world):
    db, w = world
    with Session(db) as s:
        lara, rahel = s.get(m.Athlete, w["lara"]), s.get(m.Athlete, w["rahel"])
        docs = agent.data_package(s, [lara])
        assert any("Einsätze: 200 Länderspiele" in d.text for d in docs)
        reports = agent.match_reports(s, [lara, rahel])
        assert len(reports) == 1
        text = reports[0].text
        assert "Tor Rahel Enzler (Lara Stalder)" in text and "Tor Lara Stalder (Rahel Enzler)" in text
        assert "MacLeod" not in text  # goals without our persons are left out
        assert reports[0].url == "https://sihf/game/1"


def test_collect_rounds_and_limits(world):
    db, w = world
    plans = []

    def ask(system, prompt, schema):
        plans.append(prompt)
        return {"suchen": ["Stalder Länderspiele"], "werkzeuge": ["datenpaket", "dokumente"]}, {
            "input": 500,
            "output": 30,
        }

    eng = GeminiAnswerEngine(SETTINGS, llm=lambda *a: ({}, {}))
    with Session(db) as s:
        lara = s.get(m.Athlete, w["lara"])
        run = agent.collect(eng, s, "Länderspiele?", "Länderspiele?", [lara], ask, jev=lambda *a: 0.1)
        assert run.rounds == 2 and len(plans) == 2  # never enough → stops at agent_max_rounds
        assert "Bisher gefunden" in plans[1] and run.docs
        cheap = GeminiAnswerEngine(
            SETTINGS.model_copy(update={"agent_max_usd": 0.0}), llm=lambda *a: ({}, {})
        )
        run = agent.collect(cheap, s, "Länderspiele?", "Länderspiele?", [lara], ask, jev=lambda *a: 0.1)
        assert run.rounds == 0  # cost cap reached before the first round


def test_engine_uses_stage_two_only_when_needed(world):
    _db, w = world

    def llm(system, prompt, schema):
        props = schema.get("properties", {})
        if "suchen" in props:
            return {"suchen": ["Lara Stalder Länderspiele"], "werkzeuge": ["datenpaket"]}, {
                "input": 400,
                "output": 20,
            }
        if "Vertiefte Suche" not in prompt:
            return {"antwort": [], "luecken": ["keine Angabe"]}, {}  # stage 1 finds nothing
        key = re.search(r"\[(D\d+)\][^\n]*\n[^\n]*200 Länderspiele", prompt).group(1)
        return {
            "antwort": [
                {
                    "typ": "text",
                    "saetze": [
                        {
                            "satz": "Lara Stalder hat 200 Länderspiele bestritten.",
                            "belege": [
                                {
                                    "dok": key,
                                    "zitat": "Einsätze: 200 Länderspiele",
                                    "kategorie": "anderes",
                                }
                            ],
                        }
                    ],
                }
            ],
            "luecken": [],
        }, {"input": 1000, "output": 40}

    e = GeminiAnswerEngine(
        SETTINGS,
        llm=llm,
        support_checker=lambda pairs: [0.9] * len(pairs),
        agent=True,
        jev_judge=lambda *a: 0.9,
    )
    res = e.answer(
        "Was weiss man über Lara Stalders Erfahrung?", person_ids=[w["lara"]], use_cache=False, style="prosa"
    )
    assert res.antwort[0].saetze[0].text == "Laut Wikipedia: Lara Stalder hat 200 Länderspiele bestritten."
    assert res.scope_hinweis.startswith("Vertiefte Suche") and res.durchsucht.startswith(
        "Vertiefte Suche: 1 Runde"
    )
    # without agent=True (all older tests): no second stage
    assert GeminiAnswerEngine(SETTINGS, llm=llm)._use_agent() is False


def test_group_tables_one_document_per_field_without_staff(world):
    db, w = world
    with Session(db) as s:
        team = repo.get_team(s, "sui-women")
        from klugscheisser.retrieval.search import athlete_team_filter

        ids = athlete_team_filter(s, team).athlete_ids or []
        group = [s.get(m.Athlete, i) for i in ids] + [s.get(m.Athlete, w["lara"])]
        docs = agent.group_tables(s, group)
    laender = [d for d in docs if "Länderspiele/Einsätze" in d.text]
    assert len(laender) == 1 and "Lara Stalder: 200 Länderspiele" in laender[0].text and laender[0].computed
    assert all("Shannon Miller" not in d.text for d in docs)  # coaches are no players


def test_two_players_together_from_match_reports(world):
    db, w = world
    from klugscheisser.answer import lookups

    with Session(db) as s:
        pair = [s.get(m.Athlete, w["rahel"]), s.get(m.Athlete, w["lara"])]
        pi = lookups.pair_goal_inputs(s, "Spielen Enzler und Stalder in der gleichen Linie?", pair)
        d = pi.docs[0].extra["direct"]
        assert d["summary"].startswith(
            "Linien-Aufstellungen sind nicht gespeichert. In 1 gespeicherten Spielbericht"
        )
        assert "an 2 gemeinsamen Toren" in d["summary"] and [r[3] for r in d["rows"]] == [
            "Rahel Enzler",
            "Lara Stalder",
        ]
        assert lookups.pair_goal_inputs(s, "Wie alt sind Enzler und Stalder?", pair) is None


def test_caps_table_keeps_the_caveat(world):
    db, w = world
    from klugscheisser.answer import lookups

    with Session(db) as s:
        lara = s.get(m.Athlete, w["lara"])
        pi = lookups.caps_inputs(s, "Anzahl Länderspiele", [lara])
        d = pi.docs[0].extra["direct"]
    assert "unvollständig, keine offizielle Länderspielzahl" in d["summary"]
    assert d["rows"][0][:2] == ["Lara Stalder", "200 Länderspiele (Wikipedia)"]


def test_relationship_and_health_questions_are_treated_like_others():
    """Ondřej 07.10.: answer with what we have here too (the writer still allows relationship/health statements
    only from the person's own posts and interviews)."""
    run = agent.AgentRun()
    assert (
        agent.needs_agent(SETTINGS, "Hat Alina Müller einen Freund?", _res(), run, jev=lambda *a: 0.0) is True
    )


def test_unknown_person_answered_at_once(world):
    _db, _w = world
    calls = []

    def llm(system, prompt, schema):
        calls.append(schema)
        if "art" in schema.get("properties", {}):
            return {
                "art": "fakt",
                "frage": "Wie alt ist Maria Musterfrau?",
                "personen": ["Maria Musterfrau"],
            }, {}
        raise AssertionError("no writer, no agent for a person we have nowhere")

    e = GeminiAnswerEngine(SETTINGS, llm=llm, interpreter=True, agent=True, jev_judge=lambda *a: 0.0)
    res = e.answer("Wie alt ist Maria Musterfrau?", use_cache=False, style="prosa")
    assert res.antwort[0].saetze[0].text.startswith("Zu Maria Musterfrau haben wir keine Daten")
    assert len(calls) == 1 and res.scope_hinweis.startswith("Person nicht gefunden")
    # a known person is never "unknown"
    from klugscheisser.answer.interpret import Interpretation

    with Session(_db) as s:
        known = Interpretation("fakt", "Wie alt ist Lara Stalder?", personen=["Lara Stalder"])
        assert eng._unknown_persons(s, known, []) == []
