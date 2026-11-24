"""2026-11-01: one career row per station for persons without Elite Prospects (all sources as links), and the
one-off `staff-fix` (WCh staff research → team_staff ends/starts, missing staff persons, wrong old stints)."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import staff, staff_fix
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import card_data

EP_URL = "https://www.eliteprospects.com/player/1/x"
WIKI = "https://en.wikipedia.org/wiki/Coach_X"


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _person(s, name="Coach Testperson"):
    return repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), name, role="coach")


def _stint(s, a, team, frm, to, source, url, kind="college", role="coach", status="confirmed", team_id=None):
    s.add(
        m.CareerStint(
            athlete_id=a.id,
            team_name=team,
            team_id=team_id,
            kind=kind,
            season_from=frm,
            season_to=to,
            role=role,
            source=source,
            source_url=url,
            status=status,
        )
    )


def test_same_station_from_two_sources_is_one_row(seeded):
    """Miller: Duluth 1999–2015 (web) + 1999–2014 (Wikipedia en) → one row with Wikipedia's seasons and both links;
    Calgary Inferno with and without team link → one row; Canada 1991–93 and 1994–97 stay two stations."""
    with Session(seeded) as s:
        a = _person(s)
        club = s.scalar(select(m.Team).where(m.Team.kind == "club").order_by(m.Team.id))
        club_name, club_id = club.name, club.id
        _stint(s, a, "Minnesota Duluth Bulldogs", 1999, 2015, "web", "https://news.example/duluth")
        _stint(s, a, "Minnesota Duluth Bulldogs", 1999, 2014, "wikipedia-en", WIKI)
        _stint(
            s, a, club_name, 2018, 2018, "web", "https://hockeyfans.example/1", kind="club", team_id=club_id
        )
        _stint(s, a, club_name + " Women", 2018, 2018, "wikipedia-en", WIKI, kind="club")
        _stint(s, a, "Canada", 1991, 1993, "wikipedia-en", WIKI, kind="national")
        _stint(s, a, "Canada", 1994, 1997, "sasksportshof", "https://hof.example/miller", kind="national")
        s.commit()
        shown, more = card_data.career(s, a)
    assert [(r["team"], r["saison"]) for r in shown] == [
        ("Canada", "1991/92–1993/94"),
        ("Canada", "1994/95–1997/98"),
        ("Minnesota Duluth Bulldogs", "1999/00–2014/15"),
        (club_name + " Women", "2018/19"),  # equal seasons: Wikipedia before the web article
    ]
    duluth = shown[2]
    assert {x["url"] for x in duluth["sources"]} == {WIKI, "https://news.example/duluth"}
    assert len(shown[3]["sources"]) == 2 and more == []


def test_station_merge_keeps_different_teams_and_no_chaining(seeded):
    """Not merged: women's team vs U18 / men's team (the loose keys contain each other), and a vague open
    "SC Bern seit 2010" does not glue the two Bern stints 2010/11 and 2013–2019 together."""
    with Session(seeded) as s:
        a = _person(s)
        _stint(s, a, "Switzerland (women)", 2018, 2021, "web", "https://a.example/1", kind="national")
        _stint(s, a, "Switzerland U18 (women)", 2017, 2019, "web", "https://a.example/2", kind="national")
        _stint(s, a, "Switzerland (men)", 2018, 2020, "web", "https://a.example/3", kind="national")
        _stint(s, a, "SC Bern", 2010, None, "wikipedia", "https://de.wikipedia.org/wiki/X", kind="club")
        _stint(s, a, "SC Bern", 2010, 2010, "wikipedia-en", WIKI, kind="club")
        _stint(s, a, "SC Bern", 2013, 2019, "wikipedia-en", WIKI, kind="club")
        _stint(s, a, "SC Bern", 2012, 2014, "web", "https://a.example/4", kind="club", role="player")
        s.commit()
        shown, _more = card_data.career(s, a)
    rows = [(r["team"], r["saison"]) for r in shown]
    assert ("Switzerland (women)", "2018/19–2021/22") in rows
    assert ("Switzerland U18 (women)", "2017/18–2019/20") in rows
    assert ("Switzerland (men)", "2018/19–2020/21") in rows
    bern = [r for r in rows if r[0] == "SC Bern"]
    assert ("SC Bern", "2010/11") in bern and ("SC Bern", "2013/14–2019/20") in bern
    assert ("SC Bern", "seit 2010/11") not in bern  # merged into 2010/11 (the more precise row)
    one = next(r for r in shown if r["saison"] == "2010/11")
    assert {x["url"] for x in one["sources"]} == {WIKI, "https://de.wikipedia.org/wiki/X"}


def test_station_without_seasons_joins_the_station(seeded):
    with Session(seeded) as s:
        a = _person(s)
        _stint(s, a, "Calgary Inferno", 2018, 2018, "wikipedia-en", WIKI, kind="club")
        _stint(s, a, "Calgary Inferno", None, None, "web", "https://a.example/ci", kind="club")
        _stint(
            s,
            a,
            "Calgary Inferno",
            None,
            None,
            "web",
            "https://a.example/old",
            kind="club",
            status="unconfirmed",
        )
        _stint(
            s, a, "Calgary", None, 2018, "web", "https://a.example/old2", kind="club", status="unconfirmed"
        )
        s.commit()
        shown, more = card_data.career(s, a)
    assert [(r["team"], r["saison"]) for r in shown] == [("Calgary Inferno", "2018/19")]
    assert len(shown[0]["sources"]) == 2
    assert len(more) == 1 and len(more[0]["sources"]) == 2  # the unconfirmed ones: one row too


def test_ep_player_rows_unchanged_by_station_merge(seeded):
    """EP players: list and "weitere Angaben" exactly as before (other sources' stints are not merged there)."""
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        _stint(s, a, "Brynäs IF", 2019, 2022, "eliteprospects", EP_URL, kind="club", role="player")
        _stint(
            s, a, "HC Irgendwo", 2010, 2011, "wikipedia", "https://de.wikipedia.org/wiki/X", "club", "player"
        )
        _stint(s, a, "HC Irgendwo", 2010, 2012, "web", "https://a.example/x", "club", "player")
        s.commit()
        shown, more = card_data.career(s, a)
    assert [(r["team"], r["saison"]) for r in shown] == [("Brynäs IF", "2019/20–2022/23")]
    assert [(r["team"], r["saison"]) for r in more] == [
        ("HC Irgendwo", "2010/11–2011/12"),
        ("HC Irgendwo", "2010/11–2012/13"),
    ]


def test_role_labels_new_words():
    assert staff.role_of("Assistant General Manager") == "assistant_gm"
    assert staff.role_of("Læge") == "doctor" and staff.role_of("Fysioterapeut") == "physio"
    assert staff.role_of("Assistant Coach") == "assistant_coach"


# --- staff-fix ---------------------------------------------------------------------------------------------------


def _data(old_id: int, coach_id: int, reused: str) -> dict:
    hc = "https://www.hockeycanada.ca/en-ca/news/x"
    dk = "https://ishockey.dk/vm-trup"
    return {
        "staff_corrections": [
            {
                "team": "can-women",
                "person": "Old Coach",
                "athlete_id": old_id,
                "current_row": "head_coach, origin roster, until NULL",
                "suggested": "set until=2026-02 (Olympics 2026) or 2026-04",
                "source_url": hc,
                "date": "2026-09-24",
            },
            {
                "team": "can-women",
                "person": "New Coach",
                "athlete_id": coach_id,
                "current_row": "head_coach",
                "suggested": "keep; since=2026-09-24",
                "source_url": hc,
                "date": "2026-09-24",
            },
            {
                "team": "can-women",
                "person": "X",
                "athlete_id": coach_id,
                "suggested": "keep",
                "source_url": hc,
            },
            {
                "team": "den-women",
                "person": "Hist Assistant",
                "athlete_id": None,
                "suggested": "optional history row assistant_coach since 2026-08 until 2026-09-11",
                "source_url": dk,
                "date": "2026-09-11",
            },
        ],
        "missing_persons": [
            {
                "team": "den-women",
                "name": "Test Edlund",
                "role_label": "Landstræner (Head Coach)",
                "source_url": dk,
                "date": "2026-10-29",
                "quote": "Landstræner: Test Edlund",
            },
            {
                "team": "den-women",
                "name": "Mikkel Test",
                "role_label": "Assistenttræner",
                "source_url": dk,
                "date": "2026-10-29",
                "quote": "Assistenttræner: Mikkel Test",
            },
            {
                "team": "den-women",
                "name": "Hist Assistant",
                "role_label": "Assistant Coach (Aug–Sep 2026)",
                "source_url": dk,
                "date": "2026-08",
                "quote": "Assistant Coach Assistant Hist",
            },
            {
                "team": "can-women",
                "name": reused,
                "role_label": "Assistant General Manager",
                "source_url": hc,
                "date": "2026-09-24",
                "quote": "assistant general managers ...",
            },
            {
                "team": "fin-women",
                "name": "Vague Gm",
                "role_label": "General Manager (unconfirmed for 2026/27)",
                "source_url": "https://yle.fi/a/1",
                "date": "2025-10-05",
            },
        ],
        "persons": [
            {
                "athlete_id": None,
                "name": "Mikkel Test Nielsen",
                "team": "den-women",
                "nationality": "DEN",
                "birth_date": "1981-03-19",
                "notes": "",
                "career_stints": [
                    {
                        "team": "Dänemark Frauen",
                        "league": "national team",
                        "kind": "national",
                        "role": "coach",
                        "season_from": 2026,
                        "season_to": None,
                        "source": "diu",
                        "source_url": dk,
                        "quote": "q",
                    },
                    {
                        "team": "Skellefteå AIK",
                        "kind": "club",
                        "role": "coach",
                        "season_from": None,
                        "season_to": None,
                        "source": "own website (search snippet)",
                        "source_url": "https://x.example",
                        "quote": "q",
                    },
                ],
                "season_stats": [
                    {
                        "season": 1996,
                        "league": "Denmark",
                        "team": "Rungsted",
                        "team_kind": "club",
                        "source": "wikipedia-en",
                        "phase": "regular",
                        "games": 51,
                        "goals": 17,
                        "assists": 11,
                        "points": 28,
                        "penalty_minutes": 22,
                        "source_url": "https://en.wikipedia.org/wiki/M",
                    },
                ],
            }
        ],
        "existing_stint_issues": [{"athlete_id": coach_id, "finding": "Calgary 2007–2019 wrong"}],
    }


def test_staff_fix_applies_concrete_changes_only(seeded, monkeypatch):
    with Session(seeded) as s:
        can = repo.get_team(s, "can-women")
        old, new = _person(s, "Old Coach Fixtest"), _person(s, "New Coach Fixtest")
        reused = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Former Player Fixtest")
        old_row = staff.record(s, can, old, "Head Coach", source="Wikipedia", seen=date(2026, 2, 5))
        new_row = staff.record(s, can, new, "Head Coach", source="Hockey Canada", seen=date(2026, 10, 19))
        _stint(s, new, "Calgary", 2007, 2019, "web", "https://nzz.example/x", kind="club")
        _stint(s, new, "Calgary Inferno", 2018, 2018, "wikipedia-en", WIKI, kind="club")
        s.commit()
        den = repo.get_team(s, "den-women")
        # 03.11.2026: the official WCh list (seeds/rosters/wch-2026-den.yaml) already brings Denmark's real staff;
        # this test runs the fix on a team without it (as before the list)
        for row in s.scalars(
            select(m.TeamStaff).where(m.TeamStaff.team_id == den.id, m.TeamStaff.origin == "roster")
        ):
            s.delete(row)
        s.commit()
        monkeypatch.setattr(staff_fix, "STINT_ISSUES", [(new.id, r"^Calgary$", 2007, 2019, "nur 2018/19")])
        data = _data(old.id, new.id, "Former Player Fixtest")

        rep = staff_fix.run(s, data)
        s.commit()
        assert old_row.until == "2026-02" and old_row.source == "Hockey Canada"
        assert old_row.source_date == date(2026, 9, 24)
        assert new_row.since == "2026-09-24"
        assert any("«keep»" in x for x in rep.skipped)
        assert any("Vague Gm" in x for x in rep.skipped)
        assert repo.find_athletes(s, "Vague Gm") == [] or all(
            x.how != "exact" for x in repo.find_athletes(s, "Vague Gm")
        )
        # Denmark: new persons with source, date, quote; the short name from the list becomes an alias
        # (the seeded press officer, seeds/staff/den-women.yaml, is not part of this fix)
        cur = {a.name: a for a in staff.current(s, den, date(2026, 11, 1)) if a.key != "media"}
        assert set(cur) == {"Test Edlund", "Mikkel Test Nielsen"}
        assert (
            cur["Test Edlund"].key == "head_coach"
            and cur["Test Edlund"].row.quote == "Landstræner: Test Edlund"
        )
        assert cur["Test Edlund"].row.source_url == "https://ishockey.dk/vm-trup"
        assert cur["Test Edlund"].row.last_seen == date(2026, 10, 29)
        mikkel = cur["Mikkel Test Nielsen"].row.athlete
        assert repo.resolve_athlete(s, "Mikkel Test").id == mikkel.id
        assert mikkel.nationality == "DEN" and mikkel.birth_date == date(1981, 3, 19) and mikkel.tier == 2
        hist = next(a for a in staff.appointments(s, den, date(2026, 11, 1)) if a.name == "Hist Assistant")
        assert not hist.current and (hist.row.since, hist.row.until) == ("2026-08", "2026-09-11")
        assert hist.row.role_label == "Assistant Coach"
        # an existing person with the same name is reused (and reported), a player becomes staff
        assert any("Former Player Fixtest" in x and f"#{reused.id}" in x for x in rep.identity)
        assert reused.role == "staff"
        agm = s.scalar(select(m.TeamStaff).where(m.TeamStaff.athlete_id == reused.id))
        assert agm.role_key == "assistant_gm"
        apps = {a.name: a for a in staff.appointments(s, can, date(2026, 11, 1))}
        assert (
            not apps["Old Coach Fixtest"].current and apps["Old Coach Fixtest"].reason == "bis Februar 2026"
        )
        # history of the new person: stints (snippet → unconfirmed), stats as source 'wiki'
        stints = {
            x.team_name: x.status
            for x in s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == mikkel.id))
        }
        assert stints == {"Dänemark Frauen": "confirmed", "Skellefteå AIK": "unconfirmed"}
        stat = s.scalar(select(m.SeasonStat).where(m.SeasonStat.athlete_id == mikkel.id))
        assert stat.source == "wiki" and stat.games == 51
        # wrong old stint → unconfirmed, the right one stays
        rows = {
            x.team_name: x.status
            for x in s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == new.id))
        }
        assert rows == {"Calgary": "unconfirmed", "Calgary Inferno": "confirmed"}

        # a second run changes nothing
        again = staff_fix.run(s, data)
        assert "Personen neu" not in again.counts and "team_staff neu" not in again.counts
        assert again.counts.get("Statistikzeilen neu", 0) == 0 and "Stationen neu" not in again.counts
        assert not [x for x in again.applied if x.startswith("team_staff ")]
