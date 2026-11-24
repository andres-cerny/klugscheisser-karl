"""TEAM_KEY_PLAYERS: the block "Schlüsselspielerinnen aus Schweizer Sicht" in the MCP `team` tool
(mcp_server/key_players.py). Fixtures only, no network, no model."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server import key_players as kp
from klugscheisser.mcp_server import suche as F

NOW = datetime.now(UTC)


def _team(s, sport, key, name, country, kind="national"):
    t = m.Team(sport_id=sport.id, key=key, name=name, kind=kind, country=country)
    s.add(t)
    s.flush()
    return t


def _game(s, key, home, away, score, when, goals, participants=(), comp="IIHF Frauen-WM 2025"):
    c = s.query(m.Competition).filter_by(name=comp).one_or_none()
    if c is None:
        c = m.Competition(sport_id=home.sport_id, key=key + "-c", name=comp, kind="tournament")
        s.add(c)
        s.flush()
    url = f"https://results.test/{key}"
    g = m.Event(
        key=key,
        name=f"{home.name} – {away.name}",
        kind="game",
        status="final",
        starts_at=when,
        home_team_id=home.id,
        away_team_id=away.id,
        score=score,
        source="IIHF",
        source_url=url,
        competition_id=c.id,
    )
    s.add(g)
    s.add(
        m.Item(
            source_type="official_result",
            platform="iihf",
            url=url,
            content_hash=key,
            published_at=when,
            text=f"{g.name} {score}",
            raw={"goals": goals, "participants": list(participants)},
        )
    )
    s.flush()
    return g


@pytest.fixture
def cze(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        sui = _team(s, sport, "sui-women", "Schweiz", "SUI")
        cze = _team(s, sport, "cze-women", "Tschechien", "CZE")
        swe = _team(s, sport, "swe-women", "Schweden", "SWE")
        mly = repo.upsert_athlete(s, sport, "Natálie Mlýnková")
        hol = repo.upsert_athlete(s, sport, "Tereza Holešinská")
        s.add(m.AthleteAlias(athlete_id=hol.id, alias="Tereza Vanišová", alias_normalized="tereza vanisova"))
        neu = repo.upsert_athlete(s, sport, "Noemi Neubauerová")
        pes = repo.upsert_athlete(s, sport, "Klára Peslarová")
        other = repo.upsert_athlete(s, sport, "Denisa Křížová")  # scored, but not on the roster
        for a in (mly, hol, neu, pes, other):
            a.nationality = "CZE"
        wm = m.Event(
            key="wch-2026", name="Frauen-WM 2026", kind="tournament", starts_at=NOW + timedelta(days=30)
        )
        s.add(wm)
        s.flush()
        for a, pos in ((mly, "F"), (hol, "F"), (neu, "F"), (pes, "G")):
            s.add(
                m.EventEntry(
                    event_id=wm.id,
                    athlete_id=a.id,
                    team_id=cze.id,
                    role=pos,
                    list_type="provisional",
                    source="ČSLH",
                    source_url="https://roster.test",
                )
            )
            s.add(
                m.Fact(
                    athlete_id=a.id,
                    kind="position",
                    value={"text": "Torhüterin" if pos == "G" else "Stürmerin"},
                    value_text="Torhüterin" if pos == "G" else "Stürmerin",
                    source="eliteprospects",
                    source_url="https://ep.test",
                )
            )
        _game(
            s,
            "g1",
            cze,
            sui,
            "7:0",
            NOW - timedelta(days=500),
            [
                {
                    "team": "CZE",
                    "time": "13:15",
                    "score": "1:0",
                    "scorer": "Natalie Mlynkova",
                    "assists": ["Tereza Vanisova"],
                },
                {"team": "CZE", "time": "48:15", "score": "2:0", "scorer": "Natalie Mlynkova", "assists": []},
                {"team": "CZE", "time": "53:41", "score": "3:0", "scorer": "Natálie Mlýnková", "assists": []},
                {"team": "CZE", "time": "55:00", "score": "4:0", "scorer": "Denisa Krizova", "assists": []},
            ],
            [{"name": "Klara Peslarova", "role": "goalie", "team": "Tschechien", "detail": None}],
        )
        _game(
            s,
            "g2",
            sui,
            cze,
            "3:2 n.P.",
            NOW - timedelta(days=25),
            [
                {"team": "CZE", "time": "51:27", "score": "0:1", "scorer": "Tereza Vanisova", "assists": []},
                {
                    "team": "CZE",
                    "time": "Penalty-Schiessen",
                    "score": "2:3",
                    "scorer": "Natalie Mlynkova",
                    "assists": [],
                },
            ],
            comp="Women's Euro Hockey Tour 2026/27, Lahti",
        )
        _game(
            s,
            "g3",
            cze,
            swe,
            "1:0",
            NOW - timedelta(days=20),  # not against Switzerland
            [{"team": "CZE", "time": "10:00", "score": "1:0", "scorer": "Klara Peslarova", "assists": []}],
        )
        s.add(
            m.SeasonStat(
                athlete_id=neu.id,
                season=2025,
                league="SWHL A (W)",
                team_name="EVZ Women's Team",
                games=26,
                goals=18,
                assists=15,
                points=33,
                source="eliteprospects",
                source_url="https://ep.test/neubauerova",
            )
        )
        s.add(
            m.Fact(
                athlete_id=mly.id,
                kind="award",
                value={},
                value_text="Zlatá hokejka (Czech Player of the Year), 2025/26",
                season=2025,
                source="eliteprospects",
                source_url="https://ep.test/mlynkova",
            )
        )
        s.commit()
    return db


def _team_text(db, name="Tschechien"):
    with Session(db) as s:
        return F.team(s, name)


def test_against_switzerland_counts_sheet_goals_by_roster_name_and_alias(cze):
    text = _team_text(cze)
    head = text.index("**Schlüsselspielerinnen aus Schweizer Sicht – Tschechien**")
    assert head < text.index("**Spielerinnen (")  # before the roster, same part
    assert "Tschechien – Schweiz 7:0 (WM 2025) [IIHF](https://results.test/g1)" in text  # home team first
    assert "Schweiz – Tschechien 3:2 n.P. (EHT Lahti) [IIHF](https://results.test/g2)" in text
    line = next(x for x in text.splitlines() if x.startswith("- Natálie Mlýnková"))
    assert "3 T, 0 A" in line and "(Hattrick)" in line  # shootout goal of g2 not counted
    hol = next(x for x in text.splitlines() if x.startswith("- Tereza Holešinská"))
    assert "auch Vanišová" in hol and "2 T, 0 A" not in hol and "1 T, 1 A" in hol
    assert "Křížová" not in text.split("**Spielerinnen (")[0]  # not on the roster
    assert "Torhüterin Klára Peslarová im Tor:" in text and "ohne Gegentor" in text
    assert "results.test/g3" not in text  # the game against Sweden is not a Swiss game
    assert "Bilanz Tschechien: 2 Spiele: 1 Sieg, 1 Niederlage (1 n.V./n.P.)" in text


def test_swiss_clubs_form_and_awards(cze):
    text = _team_text(cze)
    swiss = text.split("**Schweiz-Bezug**")[1].split("**")[0]
    assert "Noemi Neubauerová: 1 Saison EV Zug (Zug, deutschsprachig), 2025/26, 26 Spiele" in swiss
    assert "Deutsch: wahrscheinlich" in swiss and "https://ep.test/neubauerova" in swiss
    assert "Zlatá hokejka" in text.split("**Auszeichnungen**")[1]
    assert "EVZ Women's Team (SWHL A (W)) 26 Sp., 18 T, 15 A, 33 P" in text


def test_no_block_for_switzerland_clubs_or_switch_off(cze, monkeypatch):
    from klugscheisser.config import get_settings

    assert "Schlüsselspielerinnen" not in _team_text(cze, "Schweiz")
    with Session(cze) as s:
        assert not kp.applies(s.query(m.Team).filter_by(key="sui-women").one())
    monkeypatch.setattr(get_settings(), "team_key_players", False)
    assert "Schlüsselspielerinnen" not in _team_text(cze)


def test_team_without_swiss_games_says_so(cze):
    text = _team_text(cze, "Schweden")  # no roster: no block at all
    assert "Schlüsselspielerinnen" not in text
    with Session(cze) as s:
        swe = s.query(m.Team).filter_by(key="swe-women").one()
        a = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Testa Svensson")
        lines = kp.block(s, swe, [a], {a.id: {"name": a.full_name}})
    assert any("Keine Spiele Schweden – Schweiz in Karls Daten" in x for x in lines)


def test_name_match_never_guesses_between_namesakes(cze):
    with Session(cze) as s:
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Anna Novak")
        b = repo.upsert_athlete(s, sport, "Alena Novak")
        match = kp.NameMatch(s, [a, b])
        assert match("Anna Novak") == a.id
        assert match("A. Novak") is None  # same initial, two persons
        assert match("Zora Novak") is None
