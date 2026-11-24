"""Floorball project (men, 2026-11-24): prompts and answer texts say Spieler/Unihockey, the ice hockey project keeps
its texts. No database, no model calls: only the words the prompts are built from."""

from pathlib import Path

import pytest

from klugscheisser import projects
from klugscheisser.answer import agent, engine, games, interpret
from klugscheisser.classify import jev
from klugscheisser.db import models as m
from klugscheisser.facts import extract, german_evidence
from klugscheisser.identity import about_question
from klugscheisser.llm.words import fill, position_labels_de, sport_topic_de
from klugscheisser.sports import home_team_key, national_team_key

REGISTRY = Path(__file__).resolve().parents[1] / "projects.yaml"


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("PROJECTS_FILE", str(REGISTRY))  # the repo's registry: eishockey (default) + unihockey
    yield
    projects.clear_cache()


def test_words_follow_the_project(registry):
    assert sport_topic_de() == "Frauen-Eishockey"
    assert (
        fill("{players} / {no_player} / {a_player_dat}") == "Spielerinnen / Keine Spielerin / einer Spielerin"
    )
    assert home_team_key() == "sui-women" and national_team_key("FIN") == "fin-women"
    with projects.use("unihockey"):
        assert sport_topic_de() == "Männer-Unihockey"
        assert fill("{players} / {no_player} / {a_player_dat}") == "Spieler / Kein Spieler / einem Spieler"
        assert position_labels_de()["F"] == "Stürmer"
        assert home_team_key() == "sui-men" and national_team_key("FIN") == "fin-men"


def test_prompts_in_the_floorball_project(registry):
    with projects.use("unihockey"):
        prompts = {
            "interpret": interpret.system(),
            "agent": fill(agent.PLAN_SYSTEM),
            "prosa": engine.prompt_words(engine.SYSTEM_PROSA),
            "liste": engine.prompt_words(engine.SYSTEM),
            "reader": jev.reader_context(),
            "extract": extract.system(),
            "german": german_evidence.system(),
        }
    for name, text in prompts.items():
        assert "Eishockey" not in text, name
        assert "Stürmerin" not in text and "Mitspielerin" not in text, name
    assert "über Männer-Unihockey (vor allem Schweizer Spieler)" in prompts["interpret"]
    assert "der älteste|jüngste|grösste|kleinste Spieler im" in prompts["interpret"]
    assert "Spielerin" not in prompts["interpret"]
    assert "Frage zu Männer-Unihockey" in prompts["agent"]
    assert "'X ist als Stürmer geführt'" in prompts["prosa"] and "'and his club?'" in prompts["prosa"]
    assert (
        "Schweizer Männer-Unihockey" in prompts["reader"]
        and "Frauen-Unihockey sind nicht" in prompts["reader"]
    )
    assert "(Unihockeyspieler oder Trainer) und schreibst Fakten über ihn" in prompts["extract"]
    assert "(nicht Trainer, Mitspieler, Funktionär" in prompts["german"]


def test_jev_questions_about_a_man(registry):
    with projects.use("unihockey"):
        rel = jev.project_questions()["relevant"]
    assert "über diesen Unihockeyspieler berichtet oder ihn interviewt" in rel["instructions"]
    assert rel["criteria"]["true"].startswith("betrifft seinen Sport, sein Team, seine Karriere")
    assert jev.project_questions()["relevant"]["instructions"].endswith("berichtet oder sie interviewt?")


def test_ice_hockey_prompts_unchanged(registry):
    assert interpret.system().startswith(
        "Du liest Fragen an eine Recherche-App über Frauen-Eishockey (vor allem Schweizer Spielerinnen)"
    )
    assert "{" not in interpret.system().split("Vorlagen")[0]
    prosa = engine.prompt_words(engine.SYSTEM_PROSA)
    assert "'X ist als Stürmerin geführt'" in prosa and "'und die Jüngste?', 'and her club?'" in prosa
    assert "Schweizer Frauen-Eishockey" in jev.reader_context()


def test_world_championship_is_the_projects_main_event(registry):
    wm = next(t for t in games.tournaments() if t[3] == "WM")
    assert wm[2] == "wch-"
    with projects.use("unihockey"):
        wm = next(t for t in games.tournaments() if t[3] == "WM")
        assert wm[2] == "wfc-" and wm[0].search("Wer ist im Kader für die WM?")
        assert dict((p, rx) for rx, p in engine.event_cues()).get("wfc-") is not None


def test_identity_question_about_a_male_player():
    a = m.Athlete(
        full_name="Noah Meier", nationality="SUI", role="athlete", sport=m.Sport(key="floorball_men")
    )
    q = about_question(a, "meant")["about"]["instructions"]
    assert q.startswith(
        "Ist mit «Noah Meier» (oder seinem Nachnamen) in diesem Text der Schweizer Unihockeyspieler"
    )
    assert about_question(a)["about"]["criteria"]["true"] == "die Seite beschreibt diesen Unihockeyspieler"
    w = m.Athlete(
        full_name="Lara Stalder", nationality="SUI", role="athlete", sport=m.Sport(key="icehockey_women")
    )
    assert about_question(w)["about"]["instructions"] == (
        "Diese Seite handelt von der Schweizer Eishockeyspielerin Lara Stalder."
    )


def test_chat_eval_files_per_project(registry):
    from klugscheisser import chat_eval

    assert (
        chat_eval.questions_file() == chat_eval.QUESTIONS
        and chat_eval.project_runs_dir() == chat_eval.RUNS_DIR
    )
    with projects.use("unihockey"):
        assert chat_eval.project_runs_dir() == chat_eval.RUNS_DIR / "unihockey"
        own = chat_eval.QUESTIONS.with_name("chat_eval_unihockey.yaml")
        assert chat_eval.questions_file() == (own if own.is_file() else chat_eval.QUESTIONS)
