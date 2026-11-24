"""Chat evaluation gaps in the stats layer (2026-10-05): C3 save percentage at "Olympia 2026", C5 "X vs Y an
Weltmeisterschaften", C6 career in a named league, G2 stats follow-up ("hat sie …?") from the chat history.
Rules only, no model calls."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import cross
from klugscheisser.answer import engine as eng
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.stats import search

SETTINGS = Settings(_env_file=None, auto_refresh=False)
CUR = search.current_season()
FETCHED = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
EP = "https://www.eliteprospects.com/player/{}/x"


def _row(aid, season, league, team, kind="club", phase="regular", **stats):
    return m.SeasonStat(
        athlete_id=aid,
        season=season,
        league=league,
        team_name=team,
        team_kind=kind,
        phase=phase,
        source="eliteprospects",
        source_url=EP.format(aid),
        fetched_at=FETCHED,
        **stats,
    )


def _sk(gp, g, a, pim=None):
    return {"games": gp, "goals": g, "assists": a, "points": g + a, "penalty_minutes": pim}


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.resolve_athlete(s, "Lara Stalder")
        alina = repo.resolve_athlete(s, "Alina Müller")
        andrea = repo.resolve_athlete(s, "Andrea Brändli")
        andrea.position = "G"
        norina = repo.resolve_athlete(s, "Norina Müller") or repo.upsert_athlete(
            s, sport, "Norina Müller", nationality="SUI", position="F"
        )
        s.flush()
        goalie = {"wins": 2, "losses": 2, "shutouts": 1, "gaa": 1.94}
        s.add_all(
            [
                # Stalder: this season SWHL + EHT, World Championships
                _row(lara.id, CUR, "SWHL A (W)", "EVZ Women's Team", **_sk(5, 6, 7, pim=2)),
                _row(lara.id, CUR, "EHT (W)", "Switzerland", kind="national", **_sk(5, 3, 3)),
                _row(lara.id, 2023, "WC (W)", "Switzerland", kind="national", **_sk(7, 3, 4)),
                _row(lara.id, 2024, "WC (W)", "Switzerland", kind="national", **_sk(7, 2, 5)),
                _row(
                    lara.id, 2024, "International (W)", "Switzerland (all)", kind="national", **_sk(30, 9, 9)
                ),
                # Alina Müller: Northeastern 2018–2022 (159 games), World Championships
                *[
                    _row(alina.id, y, "NCAA (W)", "Northeastern Univ.", kind="college", **_sk(gp, 20, 30))
                    for y, gp in zip(range(2018, 2023), (37, 38, 25, 21, 38), strict=True)
                ],
                _row(alina.id, 2023, "WC (W)", "Switzerland", kind="national", **_sk(7, 4, 2)),
                _row(alina.id, 2024, "WC (W)", "Switzerland", kind="national", **_sk(7, 1, 1)),
                # Norina Müller: same surname, only club games (no WM)
                _row(norina.id, CUR, "SWHL A (W)", "HC Fribourg-Gottéron Ladies", **_sk(5, 5, 6)),
                # Brändli: Olympics 2022 and 2026 (season 2025/26), WM 2025 (season 2024/25)
                _row(
                    andrea.id,
                    2021,
                    "OG (W)",
                    "Switzerland Olympics",
                    kind="national",
                    games=7,
                    save_pct=0.883,
                ),
                _row(
                    andrea.id,
                    2025,
                    "OG (W)",
                    "Switzerland Olympics",
                    kind="national",
                    games=4,
                    save_pct=0.953,
                    **goalie,
                ),
                _row(andrea.id, 2024, "WC (W)", "Switzerland", kind="national", games=5, save_pct=0.921),
            ]
        )
        s.commit()
        ids = {"lara": lara.id, "alina": alina.id, "andrea": andrea.id, "norina": norina.id}
    return db, ids


def _no_llm(system, prompt, schema):
    raise AssertionError("stats questions must not call the model")


def _engine():
    return eng.GeminiAnswerEngine(SETTINGS, llm=_no_llm, support_checker=lambda p: [1.0] * len(p))


def _ask(s, question, athletes=()):
    cr = cross.try_stats(s, question, list(athletes))
    assert cr is not None, question
    return cr


# --- C3: save percentage at "Olympia 2026" ------------------------------------------------------------------


def test_tournament_year_is_the_season_that_ends_in_it():
    assert search.tournament_season("Olympics 2026") == 2025
    assert search.tournament_season("an der WM 2025") == 2024
    assert search.tournament_season("bei den Olympischen Spielen 2022") == 2021
    assert search.tournament_season("the 2026 Olympics") == 2025
    assert search.tournament_season("Wie viele Punkte 2026?") is None  # a bare year: rules unchanged


@pytest.mark.parametrize(
    "question",
    [
        "What was Andrea Brändli's save percentage at the Olympics 2026?",
        "What was Brändli's save percent at the 2026 Olympics?",
        "Wie war die Fangquote von Andrea Brändli an Olympia 2026?",
        "Abwehrquote von Brändli bei den Olympischen Spielen 2026",
    ],
)
def test_c3_save_percentage_at_olympics_2026(world, question):
    db, ids = world
    with Session(db) as s:
        q = search.understand(s, question)
        assert q is not None, question
        assert q.params["person"] == [ids["andrea"]] and q.params["league"] == ["OG (W)"]
        assert q.params["season"] == 2025 and q.params["group"] == "season"
        assert any("Saison 2025/26" in n for n in q.annahmen)
        ok, *_ = _ask(s, question)
        assert len(ok) == 1 and "Fangquote 95,3 %" in ok[0].aussage and "2025/26" in ok[0].aussage
        assert ok[0].quelle_link.startswith("[Elite Prospects, Stand 04.10.2026](https://www.eliteprospects")


def test_c3_world_championship_year(world):
    db, _ = world
    with Session(db) as s:
        ok, *_ = _ask(s, "Fangquote von Brändli an der WM 2025?")
        assert len(ok) == 1 and "Fangquote 92,1 %" in ok[0].aussage and "2024/25" in ok[0].aussage


# --- C5: "Stalder vs Müller an Weltmeisterschaften" ----------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Stalder vs Müller an Weltmeisterschaften",
        "Stalder gegen Müller an der WM",
        "Stalder und Müller im Vergleich an der Weltmeisterschaft",
        "Stalder vs Mueller at the World Championships",
    ],
)
def test_c5_compare_two_persons_in_a_league(world, question):
    db, ids = world
    with Session(db) as s:
        q = search.understand(s, question)
        assert q is not None, question
        assert q.params["person"] == [ids["lara"], ids["alina"]]  # Norina Müller has no WM games
        assert q.params["league"] == ["WC (W)"] and q.params["group"] == "career"
        assert any("Alina Müller verstanden" in n for n in q.annahmen)
        ok, _u, _g, _d, _us, note, *_ = _ask(s, question)
        assert [st.aussage.split(",")[0] for st in ok] == ["Lara Stalder", "Alina Müller"]
        assert "14 Spiele, 5 Tore, 9 Assists, 14 Punkte" in ok[0].aussage and "(WC (W))" in ok[0].aussage
        assert "14 Spiele, 5 Tore, 3 Assists, 8 Punkte" in ok[1].aussage  # the (all) row is not counted
        assert "WC (W)" in note and "Alina Müller" in note


def test_ambiguous_surname_without_league_stays_open(world):
    db, _ = world
    with Session(db) as s:
        assert search.understand(s, "Wie viele Punkte hat Müller diese Saison?") is None
        assert search.persons_named(s, "Müller", ["SWHL A (W)"])[0]  # only Norina plays SWHL: resolved


# --- C6: a named league without a season = the career there ------------------------------------------------


def test_c6_games_in_ncaa_career(world):
    db, ids = world
    with Session(db) as s:
        q = search.understand(s, "Wie viele Spiele hat Alina Müller in der NCAA gespielt?")
        assert q.params["person"] == [ids["alina"]] and q.params["league"] == ["NCAA (W)"]
        assert "season" not in q.params and q.params["group"] in {"league", "career"}
        assert not any("angenommen" in n for n in q.annahmen)
        ok, *_ = _ask(s, "Wie viele Spiele hat Alina Müller in der NCAA gespielt?")
        assert len(ok) == 1 and "159 Spiele" in ok[0].aussage and "2018/19–2022/23" in ok[0].aussage
        en, *_ = _ask(s, "How many games did Alina Müller play in the NCAA?")
        assert "159 Spiele" in en[0].aussage
        # "diese Saison" / "aktuell" / rankings in a club league stay on the running season
        for question in (
            "Wie viele Punkte hat Lara Stalder diese Saison in der SWHL?",
            "Wie viele Punkte hat Lara Stalder aktuell in der SWHL?",
        ):
            assert search.understand(s, question).params["season"] in (CUR, "diese Saison"), question
        assert (
            search.understand(s, "Beste Schweizerin nach Punkten pro Spiel in der SWHL").params["season"]
            == CUR
        )


# --- G2: follow-up from the chat history ----------------------------------------------------------------------


def _g1(ids, many=True):
    others = [ids["alina"], ids["andrea"], ids["norina"]] * (3 if many else 0)
    return {
        "frage": "Wer ist die älteste Spielerin im Olympia-Kader?",
        "personen": [],
        "person_ids": [*others, ids["lara"]],
        "saetze": ["Die älteste Spielerin im Olympia-Kader ist Lara Stalder."],
    }


def test_followup_person_from_previous_answer(world):
    db, ids = world
    hist = [_g1(ids)]
    with Session(db) as s:
        assert search.followup_person_ids(s, "Und wie viele Punkte hat sie diese Saison?", hist) == [
            ids["lara"]
        ]
        assert search.followup_person_ids(s, "And how many points does she have this season?", hist) == [
            ids["lara"]
        ]
        # a named person wins; no reference back → no follow-up; no history → nothing
        assert search.followup_person_ids(s, "Wie viele Punkte hat Alina Müller?", hist) == []
        assert search.followup_person_ids(s, "Wie viele Punkte hat die beste Skorerin?", hist) == []
        assert search.followup_person_ids(s, "Und wie viele Punkte hat sie?", None) == []
        # the previous answer names nobody: its persons when few, else nothing
        few = [{"frage": "x", "person_ids": [ids["andrea"]], "saetze": ["Dazu habe ich nichts gefunden."]}]
        assert search.followup_person_ids(s, "und ihre Fangquote?", few) == [ids["andrea"]]
        many = [{**_g1(ids), "person_ids": [ids["alina"], ids["andrea"]] * 4, "saetze": ["Keine Angabe."]}]
        assert search.followup_person_ids(s, "und ihre Punkte?", many) == list(
            dict.fromkeys(many[0]["person_ids"])
        )
        # only names: resolved from "personen"
        named = [{"frage": "x", "personen": ["Lara Stalder"], "saetze": []}]
        assert search.followup_person_ids(s, "Und wie viele Tore hat sie?", named) == [ids["lara"]]

        q = search.understand(s, "Und wie viele Punkte hat sie diese Saison?", history=hist)
        assert q.params["person"] == [ids["lara"]] and any("Folgefrage" in n for n in q.annahmen)
        squad = list(s.get(m.Athlete, i) for i in dict.fromkeys(hist[0]["person_ids"]))
        cr = search.try_stats_with_history(s, "Und wie viele Punkte hat sie diese Saison?", squad, hist)
        assert cr is not None and "13 Punkte" in cr[0][0].aussage and "Folgefrage" in cr[5]
        assert cr[7] == [ids["lara"]]
        assert search.try_stats_with_history(s, "und wo spielt sie?", squad, hist) is None  # not stats


def test_g2_prosa_followup_in_the_engine(world):
    _, ids = world
    hist = [_g1(ids)]
    r = _engine().answer(
        "Und wie viele Punkte hat sie diese Saison?",
        person_ids=list(dict.fromkeys(hist[0]["person_ids"])),  # the chat keeps the previous turn's persons
        history=hist,
        use_cache=False,
        style="prosa",
    )
    text = " ".join(st.text for b in r.antwort for st in (b.saetze or [z["satz"] for z in b.zeilen or []]))
    assert "13 Punkte" in text and r.person_ids == [ids["lara"]]
    assert "Folgefrage: bezogen auf Lara Stalder" in r.scope_hinweis
    # the list style (MCP) as well
    r = _engine().answer(
        "Und wie viele Punkte hat sie diese Saison?",
        person_ids=list(dict.fromkeys(hist[0]["person_ids"])),
        history=hist,
        use_cache=False,
    )
    assert r.statements and "13 Punkte" in r.statements[0].aussage


# --- unchanged: C1 ----------------------------------------------------------------------------------------------


def test_c1_unchanged(world):
    db, _ = world
    with Session(db) as s:
        ok, *_ = _ask(s, "Wie viele Punkte hat Lara Stalder diese Saison?")
        assert "13 Punkte" in ok[0].aussage and len(ok) == 2


def test_today_parameter_for_tournament_year(world):
    db, _ = world
    with Session(db) as s:
        q = search.understand(s, "Fangquote von Brändli an Olympia 2026", today=date(2030, 1, 1))
        assert q.params["season"] == 2025  # the year names the tournament, not "now"
