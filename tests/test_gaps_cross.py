"""Chat gaps found by the evaluation set (2026-10-05), answered from stored data without a model:
phrasings (Schusshand, Geschwister, Draft with year/round), who plays in a league / college now, a relation
question naming two nations (no ask-back), transfers in a time window, the next tournament with date and roster."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from klugscheisser.answer import cross
from klugscheisser.answer.engine import GeminiAnswerEngine
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.labels import TZ
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import views

SETTINGS = Settings(_env_file=None, auto_refresh=False, answer_support_threshold_prosa=0.6)
EP = "https://www.eliteprospects.com/player"


def _fact(s, a, kind, value, text, source="eliteprospects", url=None, status="confirmed"):
    s.add(
        m.Fact(
            athlete_id=a.id,
            kind=kind,
            value=value,
            value_text=text,
            source=source,
            source_url=url or f"{EP}/{a.id}/x",
            confidence=1.0,
            status=status,
        )
    )


def _team(a, team, league, season=2026):
    return {"team": team, "league": league, "season": season, "status": "rostered"}


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.resolve_athlete(s, "Lara Stalder")
        mk = {}
        for name, nat in (
            ("Alina Testmüller", "SUI"),
            ("Andrea Testbrändli", "SUI"),
            ("Ivana Testwey", "SUI"),
            ("Vanessa Testschaefer", "SUI"),
            ("Nora Ohneteam", "SUI"),
            ("Testa Svensson", "SWE"),
            ("Kim Kanada", "CAN"),
        ):
            mk[name] = repo.upsert_athlete(s, sport, name, nationality=nat)
        s.flush()
        sui, swe = repo.get_team(s, "sui-women"), repo.get_team(s, "swe-women")
        for name in ("Alina Testmüller", "Andrea Testbrändli", "Ivana Testwey", "Vanessa Testschaefer"):
            s.add(m.Affiliation(athlete_id=mk[name].id, team_id=sui.id, season="2025-26"))
        s.add(m.Affiliation(athlete_id=mk["Testa Svensson"].id, team_id=swe.id, season="2025-26"))
        # A6 / A7 / A8
        _fact(s, lara, "shoots", {"text": "R"}, "rechts")
        _fact(
            s,
            lara,
            "family_in_sport",
            {"text": "älterer Bruder"},
            "älterer Bruder",
            "web",
            "https://www.srf.ch/x",
        )
        _fact(
            s,
            lara,
            "family_in_sport",
            {"text": "Bruder: Dario Stalder", "person": "Dario Stalder", "relation": "Bruder"},
            "Bruder: Dario Stalder",
        )
        müller = mk["Alina Testmüller"]
        _fact(
            s,
            müller,
            "draft",
            {"team": "Boston Fleet", "year": 2023},
            "Gedraftet von Boston Fleet (2023)",
            "wikidata",
            "https://www.wikidata.org/wiki/Q1",
        )
        _fact(
            s,
            müller,
            "draft",
            {"team": "Boston Fleet", "year": 2023, "round": 1, "league": "PWHL", "overall": 3},
            "Gedraftet von Boston Fleet (PWHL-Draft 2023, Runde 1, Nr. 3)",
        )
        # B4 / B5: current teams (EP "latest"), a former college station that must not count
        _fact(
            s,
            müller,
            "current_team",
            _team(müller, "PWHL Hamilton", "PWHL (W)"),
            "PWHL Hamilton (PWHL, 2026/27)",
        )
        brä = mk["Andrea Testbrändli"]
        _fact(s, brä, "current_team", _team(brä, "PWHL Detroit", "PWHL (W)"), "PWHL Detroit (PWHL, 2026/27)")
        s.add(
            m.CareerStint(
                athlete_id=brä.id,
                team_name="Ohio State Univ.",
                kind="college",
                league="NCAA",
                season_from=2016,
                season_to=2020,
                source="eliteprospects",
                source_url=f"{EP}/{brä.id}/x",
            )
        )
        wey = mk["Ivana Testwey"]
        _fact(
            s,
            wey,
            "current_team",
            _team(wey, "Northeastern Univ.", "NCAA (W)"),
            "Northeastern Univ. (NCAA, 2026/27)",
        )
        vs = mk["Vanessa Testschaefer"]
        _fact(
            s,
            vs,
            "current_team",
            _team(vs, "Univ. of British Columbia", "USports (W)"),
            "UBC (USports, 2026/27)",
        )
        kim = mk["Kim Kanada"]
        _fact(s, kim, "current_team", _team(kim, "PWHL Toronto", "PWHL (W)"), "PWHL Toronto (PWHL, 2026/27)")
        # D3: a Swiss and a Swedish player at the same club at the same time
        for a in (lara, mk["Testa Svensson"]):
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_name="Brynäs IF Dam",
                    kind="club",
                    season_from=2019,
                    season_to=2020,
                    source="eliteprospects",
                    source_url=f"{EP}/{a.id}/brynas",
                )
            )
        # F2: transfers stored on the Elite Prospects profile
        tr = "https://www.eliteprospects.com/transfer/2026"

        def t(day, src, to, typ="confirmed", extra=None, to_info=None, n=0):
            return {
                "url": f"{tr}/{day}/{n}",
                "date": day,
                "type": typ,
                "extra": extra,
                "season": 2026,
                "source": f"https://club.example/news/{day}/{n}",
                "to_team": to,
                "to_info": to_info,
                "from_info": None,
                "from_team": src,
            }

        profiles = {
            müller: [t("2026-06-09", "Boston Fleet", "PWHL Hamilton", extra="3 years", n=1)],
            brä: [
                t("2025-05-02", "MoDo Hockey", "Frölunda HC", n=2),  # before the window
                t("2026-08-14", "Frölunda HC", "PWHL Detroit", extra="3 years", n=3),
            ],
            wey: [t("2026-06-22", "EVZ", "EVZ", typ="extension", extra="2 years", n=4)],  # no change
            vs: [t("2026-07-01", "SC Bern Frauen", None, to_info="retired", n=5)],  # retired: no new club
        }
        for a, transfers in profiles.items():
            a.tier = 1
            s.add(
                m.EpProfile(
                    athlete_id=a.id,
                    ep_id=str(a.id),
                    url=f"{EP}/{a.id}/x",
                    fetched_at=datetime.now(UTC),
                    raw={},
                    data={"transfers": transfers},
                )
            )
        # E3: the next WM a month from now, a past one, roster with a coach that must not be listed
        now = datetime.now(UTC)
        nxt = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        nxt.starts_at, nxt.ends_at = now + timedelta(days=30), now + timedelta(days=40)
        s.add(
            m.Event(
                key="wch-2025",
                name="IIHF Frauen-WM 2025, Budweis",
                kind="tournament",
                status="final",
                starts_at=now - timedelta(days=200),
                ends_at=now - timedelta(days=190),
                city="Budweis (CZE)",
            )
        )
        s.execute(delete(m.EventEntry).where(m.EventEntry.event_id == nxt.id))
        coach = repo.upsert_athlete(s, sport, "Coach Testtrainerin", nationality="SUI")
        coach.role = "coach"
        s.flush()
        roster_url = "https://www.sihf.ch/roster"
        for a, role in ((brä, "G"), (müller, "F"), (lara, "F"), (coach, "HC")):
            s.add(
                m.EventEntry(
                    event_id=nxt.id,
                    athlete_id=a.id,
                    team_id=sui.id,
                    role=role,
                    list_type="provisional",
                    source="Annahme: letztes Aufgebot (Turnier X; offizieller WM-Kader noch nicht publiziert)",
                    source_url=roster_url,
                )
            )
        s.commit()
        return db, {n: a.id for n, a in mk.items()} | {"lara": lara.id, "coach": coach.id}


def _direct(pi):
    assert pi is not None and pi.docs and pi.docs[0].computed, pi
    return pi.docs[0].extra["direct"]


def engine():
    return GeminiAnswerEngine(
        SETTINGS,
        llm=lambda *a: pytest.fail("no model call"),
        support_checker=lambda *a: pytest.fail("no Jev"),
    )


# --- 1. phrasings ---------------------------------------------------------------------------------------------


def test_phrasings_reach_the_fact_rules():
    for q in (
        "Mit welcher Hand schiesst Lara Stalder?",
        "Welche Schusshand hat sie?",
        "Schiesst sie links oder rechts?",
        "Does she shoot left or right?",
        "Which hand does she shoot with?",
        "What is her handedness?",
        "Which hand does the goalie catch with? She catches left",
    ):
        assert cross.understand(q).kind == "shoots", q
    for q in (
        "Hat Lara Stalder Geschwister im Eishockey?",
        "Hat sie einen Bruder, der spielt?",
        "Does she have siblings in hockey?",
        "Is her sister a player too?",
        "Familie im Sport?",
    ):
        assert cross.understand(q).kind == "family_in_sport", q
    assert cross.understand("Was sagt sie über ihre Familie?").kind != "family_in_sport"
    assert cross.understand("Wer hat Alina Müller gedraftet?").kind == "draft"
    assert cross.understand("Welche Schwedinnen haben mit Schweizerinnen im selben Club gespielt?").typ == (
        "beziehung"
    )


def test_a6_shooting_hand_from_the_stored_fact(world):
    db, _w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(s, "Mit welcher Hand schiesst Lara Stalder?", [])
    assert "Lara Stalder: rechts" in pi.docs[0].text and pi.docs[1].url.startswith(EP)


def test_a8_siblings_all_values_with_sources_and_vorsicht(world):
    db, w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(s, "Hat Lara Stalder Geschwister im Eishockey?", [])
        d = _direct(pi)
        assert "Dario Stalder" in d["summary"] and d["summary"].index("Dario") < d["summary"].index("älterer")
        assert d["spalten"] == ["Spielerin", "Familie im Sport", "Quelle"]
        assert [r[2] for r in d["rows"]] == ["Elite Prospects", "srf.ch"]
        # sensitive kind: the computed document and every source document are marked Vorsicht
        assert pi.docs[0].vorsicht and all(x.vorsicht for x in pi.docs[1:])
        assert {b["url"] for b in pi.docs[0].basis} == {f"{EP}/{w['lara']}/x", "https://www.srf.ch/x"}
    res = engine().answer("Hat Lara Stalder Geschwister im Eishockey?", style="prosa")
    assert res.quellen[0].vorsicht and all(r["satz"].ton == "vorsicht" for r in res.antwort[1].zeilen)
    assert "Vorsicht" in views.answer_html(res)


def test_a7_draft_text_has_year_round_and_pick(world):
    db, _w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(s, "Wer hat Alina Testmüller gedraftet?", [])
    assert "Boston Fleet (PWHL-Draft 2023, 1. Runde, Nr. 3)" in pi.docs[0].text
    assert "wurde von Boston Fleet (PWHL-Draft 2023, 1. Runde, Nr. 3) gedraftet" in pi.docs[1].text
    assert cross.draft_text({"team": "X"}) == "X (Draft)" and cross.draft_text({}) is None


# --- 2. current league / college ----------------------------------------------------------------------------------


def test_b4_who_plays_in_the_pwhl_now(world):
    db, _w = world
    with Session(db) as s:
        assert cross.ask_back(s, "Welche Schweizerinnen spielen in der PWHL?", []) is None
        pi = cross.prosa_inputs(s, "Welche Schweizerinnen spielen in der PWHL?", [])
        d = _direct(pi)
    names = [r[0] for r in d["rows"]]
    assert names == ["Andrea Testbrändli", "Alina Testmüller"]  # by team: Detroit, Hamilton; not the Canadian
    assert d["rows"][1][1] == "PWHL Hamilton (PWHL, 2026/27)"
    assert d["summary"].startswith("2 Spielerinnen") and "aktuell in der PWHL" in d["summary"]
    assert d["unknown"].startswith("Ohne gespeichertes aktuelles Team") and "Nora Ohneteam" not in names
    assert "Spielerinnen aus Schweiz" in pi.scope_note
    assert all(x.url for x in pi.docs[1:]) and pi.docs[0].basis


def test_b5_college_in_the_us_is_current_ncaa_only(world):
    db, _w = world
    q = "Which Swiss national team players play college hockey in the US?"
    with Session(db) as s:
        d = _direct(cross.prosa_inputs(s, q, []))
        assert [r[0] for r in d["rows"]] == ["Ivana Testwey"]
        assert "Ohio State" not in str(d) and "NCAA" in d["summary"]
        canada = _direct(cross.prosa_inputs(s, "Welche Schweizerinnen spielen College-Hockey in Kanada?", []))
        assert [r[0] for r in canada["rows"]] == ["Vanessa Testschaefer"]
        both = _direct(cross.prosa_inputs(s, "Welche Schweizerinnen spielen am College?", []))
        assert {r[0] for r in both["rows"]} == {"Ivana Testwey", "Vanessa Testschaefer"}
        # career questions stay with the stations, not the current team
        assert cross.league_ref("Welche Schweizerinnen haben in der NCAA gespielt?") is None


# --- 3. two nations: no ask-back ------------------------------------------------------------------------------------


def test_d3_two_nations_relation_answers_with_pairs(world):
    db, _w = world
    q = "Welche Schwedinnen haben mit Schweizerinnen im selben Club gespielt?"
    with Session(db) as s:
        assert cross.ask_back(s, q, []) is None
        d = _direct(cross.prosa_inputs(s, q, []))
    assert any("Lara Stalder" in r[0] and "Testa Svensson" in r[0] for r in d["rows"])
    assert "Paare" in d["summary"]


# --- 4. transfers ---------------------------------------------------------------------------------------------------


def test_transfer_window_words():
    today = date(2026, 10, 5)
    assert cross.transfer_window("im Sommer 2026", today)[:2] == (date(2026, 5, 1), date(2026, 9, 30))
    assert cross.transfer_window("diesen Sommer", today)[:2] == (date(2026, 5, 1), date(2026, 9, 30))
    assert cross.transfer_window("letzten Sommer", date(2026, 7, 1))[:2] == (
        date(2025, 5, 1),
        date(2025, 9, 30),
    )
    assert cross.transfer_window("in den letzten Wochen", today)[:2] == (date(2026, 9, 7), today)
    assert cross.transfer_window("letzte Woche", today)[0] == date(2026, 9, 28)
    assert cross.transfer_window("im August", today)[:2] == (date(2026, 8, 1), date(2026, 8, 31))
    assert cross.transfer_window("im November", today)[:2] == (date(2025, 11, 1), date(2025, 11, 30))
    assert cross.transfer_window("Juli 2025", today)[:2] == (date(2025, 7, 1), date(2025, 7, 31))
    assert cross.transfer_window("2025", today)[:2] == (date(2025, 1, 1), date(2025, 12, 31))
    assert cross.transfer_window("Wer hat gewechselt?", today)[0] == today - timedelta(days=90)


def test_f2_transfers_in_summer_with_sources(world):
    db, _w = world
    q = "Wer hat im Sommer 2026 den Club gewechselt?"
    with Session(db) as s:
        assert cross.ask_back(s, q, []) is None
        pi = cross.prosa_inputs(s, q, [])
        d = _direct(pi)
    assert [r[0] for r in d["rows"]] == ["Alina Testmüller", "Andrea Testbrändli"]  # by date
    assert d["rows"][0][1:4] == ["Boston Fleet → PWHL Hamilton", "09.06.2026", "3 Jahre"]
    assert d["summary"].startswith("2 Wechsel zwischen 01.05.2026 und 30.09.2026")
    assert d["spalten"] == ["Spielerin", "Wechsel (von → nach)", "Datum", "Zusatz"]
    assert "Tier 1/2" in pi.scope_note  # the default scope is said
    urls = {b["url"] for b in pi.docs[0].basis}
    assert "https://www.eliteprospects.com/transfer/2026/2026-06-09/1" in urls
    assert "https://club.example/news/2026-06-09/1" in urls
    assert all(x.url for x in pi.docs[1:])
    # the engine shows the four columns without a model
    res = engine().answer(q, style="prosa")
    table = res.antwort[1]
    assert table.spalten == d["spalten"] and table.zeilen[0]["zellen"] == [
        "Alina Testmüller",
        *d["rows"][0][1:4],
    ]
    assert "<table" in views.answer_html(res) and res.kosten_usd == 0


def test_den_is_no_country_in_lowercase():
    from klugscheisser.adds.wiki import countries_in

    assert countries_in("Wer hat den Club gewechselt?") == [] and countries_in("who can play") == []
    assert countries_in("SUI gegen DEN") == ["SUI", "DEN"]


# --- 5. next tournament -------------------------------------------------------------------------------------------


def test_e3_next_wm_date_place_and_players(world):
    db, w = world
    q = "Wann ist die nächste WM und wer ist im Kader?"
    with Session(db) as s:
        assert cross.ask_back(s, q, []) is None
        pi = cross.prosa_inputs(s, q, [])
        d = _direct(pi)
        ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        start = ev.starts_at.astimezone(TZ)
    assert d["summary"].startswith("Nächste WM: IIHF Frauen-WM 2026, Herning/Esbjerg")
    assert f"{start:%d.%m.}" in d["summary"] and "Herning / Esbjerg (DEN)" in d["summary"]
    assert "Kader Schweiz: 3 Spielerinnen (provisorisch)" in d["summary"]
    assert d["rows"][0][0] == "Andrea Testbrändli"  # goalies first
    assert w["coach"] not in pi.athlete_ids and "Coach Testtrainerin" not in str(d)
    assert "offizielle Kader ist noch nicht publiziert" in d["unknown"]
    assert {b["url"] for b in pi.docs[0].basis} >= {ev.source_url, "https://www.sihf.ch/roster"}
    with Session(db) as s:
        last = _direct(cross.prosa_inputs(s, "Wann war die letzte WM?", []))
    assert (
        last["summary"].startswith("Letzte WM: IIHF Frauen-WM 2025, Budweis")
        and "(beendet)" in last["summary"]
    )


def test_relation_at_named_team_within_named_group():
    """ "Wer aus dem Nationalteam hat zusammen in Northeastern gespielt?" is a relation question (words between
    "zusammen" and "gespielt"), and "Nationalteam" is the group, not a team word."""
    q = "Wer aus dem Nationalteam hat zusammen in Northeastern gespielt?"
    assert cross.understand(q).typ == "beziehung"
    assert "nationalteam" in cross.GROUP_WORDS
