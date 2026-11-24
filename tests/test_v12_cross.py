"""v12 part X: computed documents for the prosa writer (facts/relations also as follow-ups, Olympia roster
without coaches, English forms), a specific game recognised from the question, the app asking back."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.answer import cross, games
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds

OLY_Q = "Who from the swiss olympic team was the oldest"
GAME_URL = "https://www.iihf.com/en/events/2026/test/gamecenter/1"


def _oly_swiss(s: Session) -> list[m.EventEntry]:
    ev = s.scalar(select(m.Event).where(m.Event.key == "oly-2026"))
    sui = repo.get_team(s, "sui-women")
    return list(
        s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == sui.id))
    )


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
        swe = repo.upsert_athlete(s, sport, "Testa Svensson", nationality="SWE")
        s.add(m.Affiliation(athlete_id=swe.id, team_id=repo.get_team(s, "swe-women").id, season="2025-26"))
        lara = repo.resolve_athlete(s, "Lara Stalder")
        for a in (lara, swe):
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_id=brynas.id,
                    team_name="Brynäs IF Dam",
                    kind="club",
                    season_from=2019,
                    season_to=2020,
                    source="wikidata",
                    source_url=f"https://www.wikidata.org/wiki/Q{a.id}",
                )
            )
        # one Olympia player without a birth date: the computed document must name her
        entries = _oly_swiss(s)
        players = [en for en in entries if en.athlete.role == "athlete"]
        nodate = players[-1].athlete
        nodate.birth_date = None
        # the oldest by roster: a stored Wikidata fact for her (the other values come from the roster)
        oldest = min((en.athlete for en in players if en.athlete.birth_date), key=lambda a: a.birth_date)
        s.add(
            m.Fact(
                athlete_id=oldest.id,
                kind="birth_date",
                value={"date": oldest.birth_date.isoformat()},
                value_text=f"{oldest.birth_date:%d.%m.%Y}",
                source="wikidata",
                source_url="https://www.wikidata.org/wiki/Q999001",
                confidence=1.0,
                status="confirmed",
            )
        )
        s.add(
            m.Fact(
                athlete_id=oldest.id,
                kind="current_team",
                value={"team": "EV Zug"},
                value_text="EV Zug (PFWL, 2026/27)",
                source="eliteprospects",
                source_url="https://www.eliteprospects.com/player/1/x",
                confidence=1.0,
                status="confirmed",
            )
        )
        youngest = max((en.athlete for en in players if en.athlete.birth_date), key=lambda a: a.birth_date)
        coaches = [en.athlete for en in entries if en.athlete.role != "athlete"]
        s.commit()
        return db, {
            "lara": lara.id,
            "swe": swe.id,
            "oldest": oldest.full_name,
            "oldest_id": oldest.id,
            "youngest": youngest.full_name,
            "nodate": nodate.full_name,
            "players": [en.athlete_id for en in players],
            "coaches": [c.id for c in coaches],
        }


def test_understand_english_forms():
    assert cross.understand(OLY_Q).kind == "birth_date" and cross.understand(OLY_Q).limit == 1
    assert cross.understand("How old is she?").kind == "birth_date"
    assert cross.understand("Where was she born?").kind == "birth_place"
    assert cross.understand("Who is the shortest player?").sort == "asc"
    assert cross.understand("and her club?").kind == "current_team"
    assert cross.understand("Which number does she wear?").kind == "jersey_number"
    assert cross.understand("who was a teammate with whom").typ == "beziehung"
    f = cross.understand("List the Swiss Olympic roster with age and club")
    assert f.kind == "birth_date" and "current_team" in f.extra_kinds and f.limit is None
    # the v6 forms are unchanged
    assert cross.understand("Wer ist die Älteste im Olympia-Team?").kind == "birth_date"
    assert cross.understand("Wer wurde in Zürich geboren in welchem Jahr?").kind == "birth_place"


def test_oldest_swiss_olympic_english_is_computed(world):
    db, w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(s, OLY_Q, [], None)
        assert pi is not None and "Olympische Spiele 2026" in pi.scope_note
        assert set(pi.athlete_ids) == set(w["players"]) and not set(w["coaches"]) & set(pi.athlete_ids)
        doc = pi.docs[0]
        assert doc.computed and doc.key == "D1" and doc.quelle == cross.COMPUTED_LABEL
        assert f"Älteste: {w['oldest']}" in doc.text and "älteste zuerst" in doc.text
        assert f"Für 1 Spielerin ist kein Geburtsdatum gespeichert: {w['nodate']}" in doc.text
        # every value keeps its source: the Wikidata fact and the roster page for the others
        urls = {b["url"] for b in doc.basis}
        assert "https://www.wikidata.org/wiki/Q999001" in urls and any("wikipedia.org" in u for u in urls)
        assert all(d.url for d in pi.docs[1:]) and not any(d.computed for d in pi.docs[1:])
        assert len({d.key for d in pi.docs}) == len(pi.docs)
        # the per-person source document of the oldest player carries the date the writer can quote
        assert w["oldest"] in pi.docs[1].text and pi.docs[1].url == "https://www.wikidata.org/wiki/Q999001"


def test_follow_ups_use_the_previous_scope(world):
    db, w = world
    hist = [{"frage": OLY_Q, "antwort": "…", "personen": [w["oldest"]], "person_ids": [w["oldest_id"]]}]
    with Session(db) as s:
        pi = cross.prosa_inputs(s, "und die jüngste?", [s.get(m.Athlete, w["oldest_id"])], hist)
        assert pi is not None and set(pi.athlete_ids) == set(w["players"])
        assert f"Jüngste: {w['youngest']}" in pi.docs[0].text and "aus der vorherigen Frage" in pi.scope_note
        club = cross.prosa_inputs(s, "and her club?", [], hist)
        assert club.athlete_ids == [w["oldest_id"]]
        # 2026-11-24: the current club comes from the newest dated row (here this season's SIHF roster of the
        # same club) before an undated current-team fact
        assert "EVZ Women's Team (PFWL, 2026/27)" in club.docs[0].text
        assert club.docs[0].basis and club.docs[0].basis[0]["url"].startswith("https://www.sihf.ch/")


def test_relation_question_is_one_computed_document(world):
    db, w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(
            s, "Welche Spielerinnen aus Schweden und der Schweiz haben zusammen gespielt?", []
        )
        doc = pi.docs[0]
        assert (
            doc.computed
            and "Lara Stalder und Testa Svensson: zusammen bei Brynäs IF Dam (2019/20" in doc.text
        )
        assert {b["url"] for b in doc.basis} == {
            f"https://www.wikidata.org/wiki/Q{w['lara']}",
            f"https://www.wikidata.org/wiki/Q{w['swe']}",
        }
        assert len(pi.docs) == 3  # + one station document per player


def test_try_answer_liste_path_unchanged_but_without_coaches(world):
    db, w = world

    class Engine:
        pass

    with Session(db) as s:
        cr = cross.try_answer(Engine(), s, "Wer ist die Älteste im Olympia-Team?", [], None, None, False)
        stmts, _, _gaps, _, _, _note, _, ids = cr
        assert len(stmts) == 1 and not set(w["coaches"]) & set(ids)


# --- games -------------------------------------------------------------------------------------------------


def _game(s, home, away, when, city="Lahti", score="2:3", url=GAME_URL, lineup=None, linked=()):
    ev = m.Event(
        key=f"iihf-{url[-6:]}",
        name=f"{home.name} – {away.name}",
        kind="game",
        starts_at=when,
        city=city,
        status="final",
        score=score,
        home_team_id=home.id,
        away_team_id=away.id,
        source="IIHF",
        source_url=url,
    )
    s.add(ev)
    if lineup is not None:
        item = m.Item(
            source_type="official_result",
            platform="iihf",
            source_name="IIHF",
            url=url,
            title=f"{home.name} – {away.name} {score}",
            text=f"IIHF, {when:%d.%m.%Y}: {home.name} – {away.name} {score}.",
            published_at=when,
            content_hash=url,
            raw={"lineup": lineup},
            source_kind="result",
        )
        s.add(item)
        s.flush()
        for aid in linked:
            s.add(m.ItemAthlete(item_id=item.id, athlete_id=aid, match_kind="name"))
    s.flush()
    return ev


@pytest.fixture
def game_world(world):
    db, w = world
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        sui, fin = repo.get_team(s, "sui-women"), repo.get_team(s, "fin-women")
        finns = [repo.upsert_athlete(s, sport, f"Testi Finnelä {i}", nationality="FIN") for i in range(3)]
        swiss = [s.get(m.Athlete, i) for i in w["players"][:3]]
        when = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
        lineup = {"Schweiz": [a.full_name for a in swiss], "Finnland": [a.full_name for a in finns]}
        _game(s, sui, fin, when, lineup=lineup, linked=[a.id for a in swiss + finns])
        report = m.Item(
            source_type="srf",
            platform="rss",
            source_name="SRF",
            url="https://www.srf.ch/sport/test-schweiz-finnland",
            title="Schweizerinnen unterliegen Finnland 2:3",
            text="Die Nati verliert in Lahti gegen Finnland.",
            published_at=datetime(2026, 10, 3, 19, 0, tzinfo=UTC),
            content_hash="r1",
        )
        old = m.Item(
            source_type="srf",
            platform="rss",
            source_name="SRF",
            url="https://www.srf.ch/sport/test-alt",
            title="Schweiz schlägt Finnland",
            text="Ein altes Spiel.",
            published_at=datetime(2026, 2, 10, 19, 0, tzinfo=UTC),
            content_hash="r2",
        )
        s.add_all([report, old])
        s.flush()
        s.add(m.ItemAthlete(item_id=report.id, athlete_id=swiss[0].id, match_kind="name"))
        s.commit()
        return db, {**w, "swiss3": [a.id for a in swiss], "finns": [a.id for a in finns]}


def test_game_recognised_narrows_scope_window_and_docs(game_world):
    db, w = game_world
    q = "In the recent Switzerland Finland game in Euro Hockey Tour who was a teammate with whom"
    with Session(db) as s:
        ref = games.game_ref(s, q)
        assert ref is not None and ref.latest and ref.tournament[3] == "Euro Hockey Tour"
        # the stored game lies in the EHT window (tournament event from the seeds)
        pi = cross.prosa_inputs(s, "Wer hat im Spiel Schweiz gegen Finnland am 3.10.2026 getroffen?", [])
        assert pi.scope_note.startswith("Spiel Schweiz–Finnland, Lahti 03.10.2026, 2:3")
        assert set(pi.athlete_ids) == set(w["swiss3"] + w["finns"])
        assert pi.since.date() == date(2026, 10, 1) and pi.until.date() == date(2026, 10, 5)
        assert [d.url for d in pi.docs[:2]] == [GAME_URL, "https://www.srf.ch/sport/test-schweiz-finnland"]
        assert "https://www.srf.ch/sport/test-alt" not in {d.url for d in pi.docs}
        # relation question about that game: pairs only across the two lineups
        rel = cross.prosa_inputs(s, q, [])
        assert rel.docs[0].computed and "Spiel Schweiz–Finnland" in rel.docs[0].text
        assert rel.docs[1].url == GAME_URL
        # a follow-up keeps the game
        hist = [{"frage": q, "person_ids": rel.athlete_ids}]
        fu = cross.prosa_inputs(s, "und wer war die Älteste?", [], hist)
        assert set(fu.athlete_ids) == set(w["swiss3"] + w["finns"]) and fu.docs[0].computed


def test_two_candidate_games_ask_back(game_world):
    db, _w = game_world
    with Session(db) as s:
        sui, fin = repo.get_team(s, "sui-women"), repo.get_team(s, "fin-women")
        _game(s, fin, sui, datetime(2026, 9, 1, 16, 0, tzinfo=UTC), city="Vierumäki", url=GAME_URL + "-2")
        s.commit()
        q = "Wer hat im Spiel Schweiz gegen Finnland getroffen?"
        # 24.10.: a past question about several played games answers the newest and names the others
        assert cross.ask_back(s, q, []) is None
        note = cross.prosa_inputs(s, q, []).scope_note
        assert (
            "Lahti 03.10.2026" in note
            and "das neueste genommen (weitere: " in note
            and "Vierumäki 1.9." in note
        )
        # naming one game (as a tapped suggestion does)
        tap = f"{q} – Spiel Finnland–Schweiz, Vierumäki 1.9."
        assert cross.ask_back(s, tap, []) is None
        assert "Vierumäki 01.09.2026" in cross.prosa_inputs(s, tap, []).scope_note
        # "letztes Spiel" picks the newest, no question back
        assert cross.ask_back(s, "Wer traf im letzten Spiel Schweiz gegen Finnland?", []) is None


def test_no_stored_game_is_said(world):
    db, _w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(s, "Wer hat gegen Deutschland an der Euro Hockey Tour getroffen?", [])
        assert (
            "Spiel Schweiz–Deutschland (Euro Hockey Tour) nicht in den gespeicherten Resultaten"
            in pi.scope_note
        )
        # names the latest EHT tournament (Lahti, later Södertälje, …): which one depends on today's date
        assert "Women's Euro Hockey Tour, " in pi.scope_note and pi.since is not None
        assert "Schweiz als Gegner" not in pi.scope_note  # only said for a found game
        # v18: the seeded WM games would be found as the coming meeting; without them nothing is stored
        for g in s.scalars(select(m.Event).where(m.Event.key.like("iihf-%"))):
            s.delete(g)
        s.flush()
        pi2 = cross.prosa_inputs(s, "Wer hat gegen Deutschland getroffen?", [])
        assert "keine gespeicherte Begegnung" in pi2.scope_note


# --- asking back --------------------------------------------------------------------------------------------


def test_too_wide_who_question_asks_back(world):
    db, _w = world
    q = "Who from Switzerland and Sweden is happy?"
    with Session(db) as s:
        ids, _, _ = cross.persons_for(s, q, cross.understand(q))
        assert len(ids) > cross.WIDE
        ab = cross.ask_back(s, q, [])
        assert ab is not None and f"Das wären {len(ids)} Spielerinnen" in ab.text
        assert any("nur Schweiz" in v for v in ab.vorschlaege)
        narrowed = next(v for v in ab.vorschlaege if "nur Schweiz" in v)
        assert cross.understand(narrowed).nations == ["SUI"]
        # narrowed by time or topic: no question back
        assert cross.ask_back(s, "Who from Switzerland and Sweden is in form?", []) is None
        assert cross.ask_back(s, "Wer aus der Schweiz und Schweden studiert?", []) is None


def test_unclear_question_asks_back(world):
    db, w = world
    with Session(db) as s:
        ab = cross.ask_back(s, "who was a teammate with whom", [])
        assert ab is not None and "Mitspielerinnen" in ab.text and ab.vorschlaege
        # the suggestions are questions the app answers
        assert cross.prosa_inputs(s, ab.vorschlaege[0], []) is not None
        # with chat context it is answered, not asked back
        lara = s.get(m.Athlete, w["lara"])
        assert cross.ask_back(s, "who was a teammate with whom", [lara]) is None


def test_no_ask_back_and_no_inputs_for_normal_questions(world):
    db, w = world
    with Session(db) as s:
        lara = s.get(m.Athlete, w["lara"])
        for q in ("Was sagte Lara Stalder nach dem Spiel?", "Wie geht es ihr?", "Wer ist verletzt?"):
            assert cross.prosa_inputs(s, q, [lara]) is None
        for q in ("Wie alt ist Lara Stalder?", "Wie viele Punkte hat Lara Stalder diese Saison?", OLY_Q):
            assert cross.ask_back(s, q, []) is None
        # a named person's stored fact is a computed document; nothing stored → the engine reads documents
        assert cross.prosa_inputs(s, "Wo ist Lara Stalder geboren?", []) is None


def test_pronoun_on_a_person_page_keeps_that_person(world):
    """ "Für welches Team spielt sie aktuell?" on a person page: the word "Team" is no group, the person stays."""
    db, w = world
    with Session(db) as s:
        a = s.get(m.Athlete, w["oldest_id"])
        assert cross.understand("Für welches Team spielt sie aktuell?").kind == "current_team"
        pi = cross.prosa_inputs(s, "Für welches Team spielt sie aktuell?", [a], None)
        assert pi is not None and pi.athlete_ids == [a.id]
        assert pi.docs[0].computed and a.full_name in pi.docs[0].text


def test_relation_pairs_counted_once_with_summary(world):
    """The same club under another name (or from another source) is one acquaintance; the computed document
    starts with the count of pairs and of players per nation, so the writer can quote a number."""
    db, w = world
    with Session(db) as s:
        for aid in (w["lara"], w["swe"]):
            s.add(
                m.CareerStint(
                    athlete_id=aid,
                    team_name="Brynäs IF",
                    kind="club",
                    season_from=2019,
                    season_to=2020,
                    source="eliteprospects",
                    source_url=f"https://www.eliteprospects.com/player/{aid}",
                )
            )
        s.commit()
        pi = cross.prosa_inputs(
            s, "Welche Spielerinnen aus Schweden und der Schweiz haben zusammen gespielt?", []
        )
        text = pi.docs[0].text
    assert text.count("Lara Stalder und Testa Svensson") == 1
    assert "Ergebnis: 1 Paare von Spielerinnen" in text and "beteiligt sind 2 von" in text
    assert "Schweden: 1" in text and "Schweiz: 1" in text
    direct = pi.docs[0].extra["direct"]
    assert [r[0] for r in direct["rows"]] == ["Lara Stalder und Testa Svensson"]


def test_euro_hockey_without_tour_is_the_tournament():
    from klugscheisser.answer import games

    assert any(rx.search("the recent euro hockey game") for rx, *_ in games.TOURNAMENTS[:1])


def test_same_ep_team_id_is_one_team_despite_other_names(world):
    """ "PWHL Boston" (2023/24) and "Boston Fleet" (2024/25) are one club for Elite Prospects (team id 37963)."""
    from klugscheisser.facts.queries import played_together

    db, w = world
    with Session(db) as s:
        for aid, name in ((w["lara"], "PWHL Boston"), (w["swe"], "Boston Fleet")):
            s.add(
                m.CareerStint(
                    athlete_id=aid,
                    team_name=name,
                    ep_team_id="37963",
                    kind="club",
                    season_from=2023,
                    season_to=2024,
                    source="eliteprospects",
                    source_url=f"https://ep/{aid}",
                )
            )
        s.commit()
        pairs = played_together(s, [w["lara"]], [w["swe"]])
    assert any({p.a.team_name, p.b.team_name} == {"PWHL Boston", "Boston Fleet"} for p in pairs)


def test_age_threshold_questions():
    f = cross.understand("And how many players in that game were over 30")
    assert (f.typ, f.kind, f.age_min, f.age_text) == ("fakt", "birth_date", 31, "über 30")
    f = cross.understand("Wie viele Spielerinnen sind 30 oder älter?")
    assert (f.age_min, f.age_text) == (30, "30 oder älter")
    f = cross.understand("Who is under 25 in the Swiss team?")
    assert (f.age_max, f.age_text) == (24, "unter 25")
    assert cross.understand("Wer hat über 30 Punkte?").age_min is None  # points, not age


def test_age_answer_counts_lists_and_names_unknown(world):
    db, w = world
    with Session(db) as s:
        pi = cross.prosa_inputs(s, "Wie viele Spielerinnen im Olympia-Kader der Schweiz sind über 30?", [])
        d = pi.docs[0].extra["direct"]
    assert d["summary"].startswith("Ergebnis: ") and "sind über 30" in d["summary"]
    assert d["spalten"] == ["Spielerin", "Geboren (Alter)"]
    assert w["nodate"] in (d["unknown"] or "")
