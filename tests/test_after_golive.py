"""Fixes after go-live (24.10.2026, real Perplexity calls): hard size cap on every tool answer, current clubs instead
of every career station, connections between two teams' players, tournament routing (Lahti/Södertälje), a named
person stays one person, past game questions only mean played games, staff once, tournaments never stay "läuft"."""

from datetime import UTC, datetime, timedelta

import pytest
from mcp.types import CallToolResult, TextContent
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import tournament
from klugscheisser.answer import connections, cross, event_games
from klugscheisser.answer.engine import scope_athletes
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server import server
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.present import render_markdown
from klugscheisser.seed.loader import load_all_seeds

Y = connections.season_start()
TAG = f"{Y}-{(Y + 1) % 100:02d}"


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


# --- 1. size cap -------------------------------------------------------------------------------------------------


def test_every_tool_answer_is_capped_with_a_hint():
    long = "\n".join(f"- Zeile {i} " + "x" * 80 for i in range(2000))
    out = server.cap_result(CallToolResult(content=[TextContent(type="text", text=long)]), "frage")
    text = out.content[0].text
    assert len(text) <= server.MAX_TOOL_CHARS
    assert "gekürzt" in text and "mehr: enger fragen" in text
    assert text.split("\n\n_… gekürzt")[0].endswith("x")  # cut at a line end
    short = CallToolResult(content=[TextContent(type="text", text="kurz")])
    assert server.cap_result(short, "frage") is short
    assert "list_events(team=" in server.cap_text("a\n" * 9000, "list_events")


def test_timed_wrapper_applies_the_cap():
    @server.timed
    def frage():
        return CallToolResult(content=[TextContent(type="text", text="y\n" * 100_000)])

    assert server.response_size(frage()) <= server.MAX_TOOL_CHARS


# --- current club instead of all stations ------------------------------------------------------------------------


def test_present_tense_club_question_means_the_current_club():
    q = "Bei welchen Klubs (Vereine) spielen die Spielerinnen des aktuellen Kaders der finnischen Frauen-Nationalmannschaft?"
    assert cross.understand(q).kind == "current_team"
    assert (
        cross.understand("Bei welchem Klub spielt Emma Ekoluoma in der Saison 2026/27?").kind
        == "current_team"
    )
    assert cross.understand("Bei welchen Vereinen hat sie früher gespielt?").kind == "stationen"
    assert cross.understand("Welche Stationen hatte sie in ihrer Karriere?").kind == "stationen"


def test_club_question_forms():
    cases = {
        "Bei welchen Klubs spielen in der Saison 2026/27 Petra Nieminen und Emma Ekoluoma?": "current_team",
        "Welche Spielerinnen aus Finnland spielen aktuell bei einem Schweizer Klub?": "current_team",
        "Was ist ihr aktueller Klub?": "current_team",
        "Bei welchen Klubs hat sie gespielt?": "stationen",
        "Bei welchem Klub spielte sie früher?": "stationen",
    }
    for q, kind in cases.items():
        assert cross.understand(q).kind == kind, q


def test_named_persons_are_the_scope_of_a_fact_question(world):
    with Session(world) as s:
        a = _person(s, "Testa Alpha", "SUI", "sui-women")
        g = _person(s, "Testa Gamma", "SUI", "sui-women")
        _person(s, "Testa Omega", "SUI", "sui-women")
        q = "Bei welchen Klubs spielen Testa Alpha und Testa Gamma aktuell?"
        res = cross.try_answer(None, s, q, [], None, None, False)
        assert sorted(res[-1]) == sorted([a.id, g.id]) and "Genannte Personen (2)" in res[5]


def _club(s, name, country="SWE"):
    team = m.Team(
        sport_id=repo.get_sport(s, "icehockey_women").id,
        key=f"t-{name.lower().replace(' ', '-')}",
        name=name,
        kind="club",
        country=country,
    )
    s.add(team)
    s.flush()
    return team


def _stint(s, a, team_name, start, end=None, source="eliteprospects", kind="club"):
    s.add(
        m.CareerStint(
            athlete_id=a.id,
            team_name=team_name,
            kind=kind,
            season_from=start,
            season_to=end,
            source=source,
            source_url=f"https://www.eliteprospects.com/player/{a.id}",
            quote=f"{team_name} {start}",
        )
    )


def _person(s, name, nation, team_key):
    a = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), name, nationality=nation)
    a.role = "athlete"
    s.add(m.Affiliation(athlete_id=a.id, team_id=repo.get_team(s, team_key).id, season=TAG, source="SIHF"))
    s.flush()
    return a


def test_current_clubs_best_evidence_and_block_facts_fallback(world):
    with Session(world) as s:
        a = _person(s, "Testa Alpha", "SUI", "sui-women")
        b = _person(s, "Testi Beta", "FIN", "fin-women")
        c = _person(s, "Testi Gamma", "FIN", "fin-women")
        zug = _club(s, "Testclub Zug", "SUI")
        s.add(
            m.Affiliation(
                athlete_id=a.id, team_id=zug.id, season=TAG, source="SIHF", source_url="https://sihf.ch/x"
            )
        )
        _stint(s, a, "Altclub", Y - 5, Y - 3)  # old station: not current
        s.add(
            m.SeasonStat(
                athlete_id=b.id, season=Y, league="SDHL (W)", team_name="Frölunda HC", team_kind="club",
                source="eliteprospects", source_url="https://www.eliteprospects.com/player/b",
            )
        )  # fmt: skip
        _stint(s, c, "Kiekko-Espoo", Y - 4, Y - 2)
        _stint(s, c, "HIFK", Y - 1, None)  # newest open station
        s.flush()
        clubs = connections.current_clubs(s, [a.id, b.id, c.id])
        assert [x.name for x in clubs[a.id]] == ["Testclub Zug"]
        assert [x.name for x in clubs[b.id]] == ["Frölunda HC"] and clubs[b.id][0].quelle == "Elite Prospects"
        assert [x.name for x in clubs[c.id]] == ["HIFK"]
        form = cross.understand("Bei welchem Klub spielt Testi Gamma aktuell?")
        stmts, missing = cross.block_facts(s, form, [c.id])
        assert missing == [] and stmts[0].aussage.startswith("Testi Gamma: aktueller Klub HIFK")


def test_many_persons_get_their_newest_stations_only(world):
    with Session(world) as s:
        people = [_person(s, f"Testa Nummer{i}", "SUI", "sui-women") for i in range(7)]
        for a in people:
            for y in range(Y - 12, Y):
                _stint(s, a, f"Klub {a.id}-{y}", y, y)
        s.flush()
        form = cross.understand("Welche Stationen hatten sie?")
        stmts, _ = cross.block_facts(s, form, [a.id for a in people])
        assert len(stmts) == 7 * cross.STATIONS_PER_PERSON
        assert f"Klub {people[0].id}-{Y - 1}" in stmts[0].aussage  # newest first


def test_roster_seed_club_text_counts_as_current_club(world):
    with Session(world) as s:
        holo = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Elisa Holopainen"))
        if holo is None or TAG != "2026-27":
            pytest.skip("seed roster of 2026-27 needed")
        clubs = connections.current_clubs(s, [holo.id])
        assert clubs[holo.id][0].name == "New York Sirens"
        assert (
            "jatkoaika.com" in clubs[holo.id][0].quelle
        )  # official WCh list (Jääkiekkoliitto, via Jatkoaika)


# --- 2. connections between two teams ----------------------------------------------------------------------------


@pytest.fixture
def duel(world):
    """SUI–FIN: Alpha (SUI) + Beta (FIN) at Frölunda together in season Y-1 (former), both now at Zug (current
    via affiliation / EP row); Gamma (SUI) + Delta (FIN) never together. A played game with a stored lineup
    (all four) 20 days ago, an older one 200 days ago and an upcoming one."""
    with Session(world) as s:
        sui, fin = repo.get_team(s, "sui-women"), repo.get_team(s, "fin-women")
        a = _person(s, "Testa Alpha", "SUI", "sui-women")
        b = _person(s, "Testi Beta", "FIN", "fin-women")
        g = _person(s, "Testa Gamma", "SUI", "sui-women")
        d = _person(s, "Testi Delta", "FIN", "fin-women")
        zug = _club(s, "Testclub Zug", "SUI")
        s.add(
            m.Affiliation(
                athlete_id=a.id, team_id=zug.id, season=TAG, source="SIHF", source_url="https://sihf.ch/a"
            )
        )
        s.add(
            m.SeasonStat(
                athlete_id=b.id, season=Y, league="SWHL (W)", team_name="Testclub Zug", team_kind="club",
                source="eliteprospects", source_url="https://www.eliteprospects.com/player/b",
            )
        )  # fmt: skip
        _stint(s, a, "Frölunda HC", Y - 1, Y - 1)
        _stint(s, b, "Frölunda HC", Y - 2, Y - 1)
        _stint(s, g, "Ohio State", Y - 6, Y - 4, kind="college")
        _stint(s, d, "Minnesota", Y - 6, Y - 4, kind="college")
        now = datetime.now(UTC)
        games = []
        for key, when, score in (("g-old", now - timedelta(days=200), "2:1"), ("g-new", now - timedelta(days=20), "4:1"), ("g-next", now + timedelta(days=4), None)):  # fmt: skip
            ev = m.Event(
                key=key, kind="game", name="Finnland – Schweiz", starts_at=when, status="final" if score else "scheduled",
                score=score, home_team_id=fin.id, away_team_id=sui.id, source="Leijonat", source_url=f"https://x.org/{key}",
            )  # fmt: skip
            s.add(ev)
            games.append(ev)
        s.flush()
        item = m.Item(
            source_type="official_result", platform="leijonat", url="https://x.org/g-new", content_hash="h1",
            title="Finnland – Schweiz 4:1",
            raw={"lineup": {"Finnland": ["Testi Beta", "Testi Delta"], "Schweiz": ["Testa Alpha", "Testa Gamma"]}},
        )  # fmt: skip
        s.add(item)
        s.flush()
        for x in (a, b, g, d):
            s.add(m.ItemAthlete(item_id=item.id, athlete_id=x.id, match_kind="name"))
        s.commit()
    return world


def test_connections_current_and_former_with_lineup_and_latest_game(duel):
    with Session(duel) as s:
        q = "Welche Spielerinnen von Finnland und der Schweiz kennen sich aus dem selben Klub?"
        assert connections.wants(q)
        text = connections.answer_text(s, q, "heute")
        assert text is not None
        assert "Spiel: Finnland–Schweiz" in text and "4:1" in text and "neuestes gespieltes Duell" in text
        current = text.split("**Aktuell im selben Klub")[1].split("**Früher")[0]
        assert "Testclub Zug" in current and "Testa Alpha 🏒" in current and "Testi Beta 🏒" in current
        former = text.split("**Früher zusammen")[1].split("\n\n_")[0]
        # Alpha and Beta: together now at Zug (current only) and before at Frölunda (former); Gamma/Delta never
        assert "Frölunda HC" in former and "Testclub Zug" not in former and "Testa Gamma" not in former
        assert "Ohne gespeicherten aktuellen Klub" in text and "Testa Gamma" in text.split("_Ohne")[1]
        assert len(text) < server.MAX_TOOL_CHARS


def test_connections_former_teammates_with_seasons(duel):
    with Session(duel) as s:
        # Beta moves away: now only "früher zusammen bei Frölunda HC (season Y-1)"
        b = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Testi Beta"))
        for r in s.scalars(select(m.SeasonStat).where(m.SeasonStat.athlete_id == b.id)):
            r.team_name = "Anderer Klub"
        s.flush()
        text = connections.answer_text(
            s, "Welche Spielerinnen aus Finnland und der Schweiz haben zusammen gespielt?", "x"
        )
        former = text.split("**Früher zusammen")[1]
        label = f"{Y - 1}/{Y % 100:02d}"
        assert (
            f"Testa Alpha 🏒 (Schweiz) – Testi Beta 🏒 (Finnland): früher zusammen bei Frölunda HC ({label})"
            in former
        )
        assert "eliteprospects.com" in former


def test_connections_need_two_teams_and_a_relation_word(duel):
    with Session(duel) as s:
        assert (
            connections.answer_text(s, "Welche Spielerinnen von Finnland spielen in Schweden?", "x") is None
        )
        assert connections.answer_text(s, "Wie hat die Schweiz gegen Finnland gespielt?", "x") is None
        sui = repo.get_team(s, "sui-women")
        assert connections.answer_text(s, "Wer kennt sich aus dem gleichen Team mit Finnland?", "x", team=sui)


def test_mcp_frage_routes_connections(duel, monkeypatch):
    monkeypatch.setattr(server, "session_scope", lambda: Session(duel))
    out = server.frage.__wrapped__(
        "In the recent Finland–Switzerland game, which players know each other because they play on the same team?"
    )
    assert "Verbindungen" in out.content[0].text


# --- 3. routing ----------------------------------------------------------------------------------------------------


@pytest.fixture
def ehts(world):
    """Lahti (played, no stored end date) and Södertälje (upcoming) with SUI games."""
    with Session(world) as s:
        sui, fin, cze = (repo.get_team(s, k) for k in ("sui-women", "fin-women", "cze-women"))
        lahti = s.scalar(select(m.Event).where(m.Event.key == "eht-2026-lahti"))
        sod = s.scalar(select(m.Event).where(m.Event.key == "eht-2026-sodertalje"))
        now = datetime.now(UTC)
        lahti.starts_at, lahti.ends_at, lahti.status = now - timedelta(days=26), None, "scheduled"
        sod.starts_at, sod.ends_at = now + timedelta(days=4), now + timedelta(days=7)
        rows = [
            ("l1", fin, sui, now - timedelta(days=22), "4:1"),
            ("l2", sui, cze, now - timedelta(days=21), "3:2 n.P."),
            ("s1", fin, sui, now + timedelta(days=4, hours=2), None),
            ("s2", sui, cze, now + timedelta(days=7, hours=1), None),
        ]
        for key, h, a, when, score in rows:
            s.add(
                m.Event(
                    key=key, kind="game", name=f"{h.name} – {a.name}", starts_at=when,
                    status="final" if score else "scheduled", score=score, home_team_id=h.id, away_team_id=a.id,
                    source="Leijonat", source_url=f"https://x.org/{key}",
                )
            )  # fmt: skip
        s.commit()
    return world


LAHTI_Q = (
    "Wie hat die Schweizer Frauen-Nati beim WEHT-Turnier in Lahti September/Oktober 2026 gespielt? Alle drei "
    "Resultate mit Datum. Gegen wen spielt die Schweiz beim WEHT in Södertälje 28.–31.10.2026, mit Anspielzeiten "
    "ausdrücklich Schweizer Zeit (nicht finnische Zeit)? Quellen je Spiel."
)


def test_named_other_tournament_wins_over_the_wch_sheet(ehts):
    with Session(ehts) as s:
        assert tournament.event_for_question(s, LAHTI_Q) is None
        assert [e.key for e in event_games.named_events(s, "Spiele 2026 in Lahti")] == ["eht-2026-lahti"]
        assert event_games.named_events(s, "Frauen-WM 2026") == []  # "2026" names no tournament
        text = event_games.answer_text(s, LAHTI_Q, "heute")
        lahti, sod = text.split("Södertälje (")[0], text.split("Södertälje (")[1]
        assert "Finnland – Schweiz 4:1 (Niederlage Schweiz)" in lahti and "beendet" in lahti
        assert "Schweiz – Tschechien 3:2 n.P. (Sieg n.P. Schweiz)" in lahti
        assert "📅" in sod and "Finnland – Schweiz" in sod and "bevorstehend" in sod
        assert "Schweizer Zeit" in text
        # scorers stay with the official results + writer
        assert event_games.answer_text(s, "Wer hat in Lahti für die Schweiz getroffen?", "x") is None


def test_mcp_frage_lahti_question_gets_the_games(ehts, monkeypatch):
    monkeypatch.setattr(server, "session_scope", lambda: Session(ehts))
    text = server.frage.__wrapped__(LAHTI_Q).content[0].text
    assert "Lahti" in text and "Frauen-WM" not in text.split("\n")[2]


def test_named_person_stays_one_person(world):
    with Session(world) as s:
        a = _person(s, "Testa Alpha", "SUI", "sui-women")
        _person(s, "Testa Gamma", "SUI", "sui-women")
        sui = repo.get_team(s, "sui-women")
        athletes, _ = scope_athletes(s, "Ist sie wieder fit?", [a.id], sui.id, None)
        assert [x.id for x in athletes] == [a.id]
        team_only, _ = scope_athletes(s, "Wer ist verletzt?", None, sui.id, None)
        assert len(team_only) > 1


def test_past_game_question_only_means_played_games(ehts):
    with Session(ehts) as s:
        q = "Wer hat für die Schweiz gegen Tschechien an der Euro Hockey Tour getroffen?"
        assert cross.asks_past(q)
        assert not cross.asks_past("Wann spielt die Schweiz das nächste Mal gegen Tschechien?")
        assert cross.ask_back(s, q, []) is None  # l2 is played: no "Welches Spiel meinst du?"
        sc = cross._game_scope(s, q, cross.understand(q), None)
        assert sc is not None and sc.event.key == "l2"


# --- 4. small ones -------------------------------------------------------------------------------------------------


def test_tournament_without_end_date_is_finished_when_its_games_are(ehts):
    with Session(ehts) as s:
        lahti = s.scalar(select(m.Event).where(m.Event.key == "eht-2026-lahti"))
        games = event_games.event_games(s, lahti)
        assert games and event_games.effective_end(lahti, games) < datetime.now(UTC)
        stale = m.Event(
            key="x-old", kind="tournament", name="Alt", starts_at=datetime.now(UTC) - timedelta(days=30)
        )
        assert event_games.effective_end(stale, []) < datetime.now(UTC)
        out = t.list_events(s, team="Schweiz")
        lahti_row = next((x for x in out["anlaesse"] if x["key"] == "eht-2026-lahti"), None)
        assert lahti_row is None or lahti_row["stand"] == "beendet"
        assert "Turnierspiele stehen beim Turnier" in out["mehr"]


def test_roster_lists_staff_once(world):
    with Session(world) as s:
        out = t.get_team_roster(s, "Finnland")
        listed = " ".join(z["text"] for z in out.get("staff") or [])
        for line in out.get("staff_im_aufgebot") or []:
            assert line.split(": ", 1)[1] not in listed
        md = render_markdown(out)
        if out.get("staff"):
            name = out["staff"][0]["text"].split(": ", 1)[1].split(" (")[0]
            assert md.count(name) == 1
