"""2026-10-25: current season totals (SIHF league tables, national-team totals counted from stored game sheets)
and the read rule "the newer source wins" (stats/current.py, stats/queries.prefer_current). No network."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.results import leijonat, sihf
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server.stats_tools import stats as stats_tool
from klugscheisser.stats import current, queries
from klugscheisser.web.statistik import statistik_html, stats_data

SETTINGS = Settings(
    _env_file=None,
    auto_refresh=False,
    sihf_data_enabled=True,
    leijonat_enabled=True,
    season_totals_college_enabled=False,
    season_totals_swehockey_enabled=False,
    season_totals_leijonat_leagues_enabled=False,
    season_totals_jihf_enabled=False,
)
EP_DAY = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
TEAM = {"id": 105975, "name": "EV Zug", "acronym": "EVZ", "type": "team"}

PLAYER_TABLE = {
    "updateDateTime": "2026-10-25T20:17:21.1350000+01:00",
    "filters": [
        {"alias": "Phase", "selected": "773", "entries": [{"name": "Regular Season", "alias": "773"}]}
    ],
    "header": [
        {"alias": a}
        for a in (
            "rank",
            "player",
            "team",
            "position",
            "gamesPlayed",
            "goals",
            "assists",
            "points",
            "pointsPerGame",
            "pimTotal",
        )
    ],
    "data": [
        ["7", "Stalder Lara", TEAM, "Stürmer", "8", "6", "9", "15", "1.88", "4"],
        ["90", "Unbekannt Erika", TEAM, "Stürmer", "8", "0", "1", "1", "0.13", "0"],
    ],
}
GOALIE_TABLE = {
    "updateDateTime": "2026-10-25T20:10:00.0000000+01:00",
    "header": [
        {"alias": a}
        for a in (
            "rank",
            "player",
            "team",
            "gamesPlayed",
            "firstKeeper",
            "goalsAgainst",
            "goalsAgainstAverage",
            "secondsPlayed",
            "penaltyInMinutes",
            "goals",
            "assists",
        )
    ],
    "data": [
        ["1", "Brändli Andrea", TEAM, "7", "6", "14", "2.30", "364:59", "0", "0", "0"],
        [
            "",
            "Durchschnitt",
            {"id": 100000, "name": "Durchschnitt"},
            "6",
            "6",
            "15",
            "2.47",
            "365:05",
            "0",
            "0",
            "0",
        ],
    ],
}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def json(self, url, params=None, headers=None):
        self.calls.append(params)
        if params and params.get("alias") == "player":
            return PLAYER_TABLE
        if params and params.get("alias") == "goalkeeper":
            return GOALIE_TABLE
        return {"HomeTeamGameRoster": {"Players": []}, "AwayTeamGameRoster": {"Players": []}}


def test_sihf_table_parsers():
    rows = sihf.parse_player_table(PLAYER_TABLE)
    assert rows[0] == {
        "name": "Lara Stalder",
        "team": "EV Zug",
        "team_code": "EVZ",
        "games": 8,
        "goals": 6,
        "assists": 9,
        "points": 15,
        "penalty_minutes": 4,
    }
    goalies = sihf.parse_goalie_table(GOALIE_TABLE)
    assert [g["name"] for g in goalies] == ["Andrea Brändli"]  # league average row left out
    assert goalies[0]["gaa"] == 2.3 and goalies[0]["toi"] == "364:59"
    assert sihf.table_phase(PLAYER_TABLE) == "regular"
    assert sihf.table_stand(PLAYER_TABLE).isoformat() == "2026-10-25T20:17:21+01:00"


def test_leijonat_player_line_keeps_no_ids():
    line = leijonat.player_line(
        "Lara Stalder",
        "Schweiz",
        {"PersonID": 1, "DateOfBirth": "x", "RoleAbbrv": "VL", "Toi": 1278, "PenMin": 2, "PlusMinus": -1},
    )
    assert line == {
        "name": "Lara Stalder",
        "team": "Schweiz",
        "goalie": False,
        "toi": 1278,
        "pim": 2,
        "pm": -1,
    }


def _sheet(
    s,
    n,
    day,
    home,
    away,
    lineup=None,
    parts=(),
    lines=None,
    platform="leijonat",
    competition="Women's Euro Hockey Tour 2026/27, Lahti",
):
    raw = {
        "competition": competition,
        "home": home,
        "away": away,
        "participants": [{"name": a, "role": b, "team": c, "detail": d} for a, b, c, d in parts],
    }
    if lineup is not None:
        raw["lineup"] = lineup
    if lines is not None:
        raw["player_stats"] = lines
    s.add(
        m.Item(
            source_type="official_result",
            platform=platform,
            external_id=str(n),
            url=f"https://example.org/g/{n}",
            published_at=day,
            content_hash=f"h{n}",
            raw=raw,
            text="x",
        )
    )


def _ep(aid, league, team, kind, **v):
    return m.SeasonStat(
        athlete_id=aid,
        season=2026,
        league=league,
        team_name=team,
        team_kind=kind,
        phase="regular",
        source="eliteprospects",
        source_url="https://www.eliteprospects.com/player/1/x",
        fetched_at=EP_DAY,
        ep_team_id="19687" if kind == "club" else "50754",
        **v,
    )


@pytest.fixture
def world(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.upsert_athlete(s, sport, "Lara Stalder", nationality="SUI", position="F")
        andrea = repo.upsert_athlete(s, sport, "Andrea Brändli", nationality="SUI", position="G")
        mai = repo.upsert_athlete(s, sport, "Mai Probeyama", nationality="JPN", position="F")
        s.flush()
        s.add_all(
            [
                _ep(
                    lara.id,
                    "SWHL A (W)",
                    "EVZ Women's Team",
                    "club",
                    games=5,
                    goals=6,
                    assists=7,
                    points=13,
                    penalty_minutes=2,
                ),
                _ep(lara.id, "EHT (W)", "Switzerland", "national", games=5, goals=3, assists=3, points=6),
                _ep(
                    lara.id,
                    "NCAA (W)",
                    "Univ. of Minnesota-Duluth",
                    "college",
                    games=30,
                    goals=10,
                    assists=10,
                    points=20,
                ),
            ]
        )
        d1, d2 = datetime(2026, 10, 28, 17, tzinfo=UTC), datetime(2026, 10, 30, 17, tzinfo=UTC)
        roster = ["Lara Stalder", "Andrea Brändli", "Saskia Ersatz"]
        _sheet(
            s,
            1,
            d1,
            "Schweiz",
            "Schweden",
            {"Schweiz": roster, "Schweden": ["Svea A"]},
            [
                ("Lara Stalder", "scorer", "Schweiz", "1:0, 10:00"),
                ("Lara Stalder", "scorer", "Schweiz", "2:1 (Penalty-Schiessen)"),
                ("Andrea Brändli", "goalie", "Schweiz", "20 Paraden bei 21 Schüssen"),
            ],
            lines=[
                {"name": "Lara Stalder", "team": "Schweiz", "goalie": False, "toi": 1200, "pim": 2, "pm": 1},
                {
                    "name": "Andrea Brändli",
                    "team": "Schweiz",
                    "goalie": True,
                    "toi": 3600,
                    "pim": 0,
                    "pm": 0,
                    "saves": 20,
                    "ga": 1,
                },
                {
                    "name": "Saskia Ersatz",
                    "team": "Schweiz",
                    "goalie": True,
                    "toi": 0,
                    "pim": 0,
                    "pm": 0,
                    "saves": None,
                    "ga": None,
                },
                {"name": "Svea A", "team": "Schweden", "goalie": False, "toi": 900, "pim": 0, "pm": -1},
            ],
        )
        _sheet(
            s,
            2,
            d2,
            "Finnland",
            "Schweiz",
            {"Schweiz": roster, "Finnland": ["Fia B"]},
            [
                ("Lara Stalder", "assist", "Schweiz", "1:1, 30:00"),
                ("Andrea Brändli", "goalie", "Schweiz", "30 Paraden bei 32 Schüssen"),
            ],
            lines=[
                {"name": "Lara Stalder", "team": "Schweiz", "goalie": False, "toi": 1100, "pim": 0, "pm": 0},
                {
                    "name": "Andrea Brändli",
                    "team": "Schweiz",
                    "goalie": True,
                    "toi": 3600,
                    "pim": 0,
                    "pm": 0,
                    "saves": 30,
                    "ga": 2,
                },
                {"name": "Fia B", "team": "Finnland", "goalie": False, "toi": 900, "pim": 0, "pm": 0},
            ],
        )
        # IIHF sheet without line-ups (scorers only): Japan, no EP data
        _sheet(
            s,
            3,
            datetime(2025, 4, 10, 17, tzinfo=UTC),
            "Japan",
            "Dänemark",
            None,
            [
                ("Mai Probeyama", "scorer", "Japan", "1:0, 5:00"),
                ("Mai Probeyama", "scorer", "Japan", "2:1 (GWS)"),
            ],
            platform="iihf",
            competition="IIHF Frauen-WM 2025",
        )
        s.commit()
        yield s, {"lara": lara.id, "andrea": andrea.id, "mai": mai.id}


def test_refresh_stores_sihf_and_sheet_rows(world):
    s, ids = world
    out = current.refresh(s, SETTINGS, http=FakeHttp())
    assert out["fehler"] is None
    assert "Erika Unbekannt (EV Zug)" in out["nicht_erkannt_beispiele"]
    rows = {
        (r.athlete_id, r.league, r.source): r
        for r in s.scalars(select(m.SeasonStat).where(m.SeasonStat.source != "eliteprospects"))
    }
    lara_sihf = rows[(ids["lara"], "SWHL A (W)", "sihf")]
    assert (
        lara_sihf.games,
        lara_sihf.goals,
        lara_sihf.assists,
        lara_sihf.points,
        lara_sihf.penalty_minutes,
    ) == (8, 6, 9, 15, 4)
    assert lara_sihf.team_name == "EVZ Women's Team" and lara_sihf.ep_team_id == "19687"  # EP's name by vote
    assert lara_sihf.fetched_at.isoformat().startswith("2026-10-25")
    goalie = s.scalar(
        select(m.SeasonStat).where(m.SeasonStat.athlete_id == ids["andrea"], m.SeasonStat.source == "sihf")
    )
    assert (goalie.games, goalie.goals_against, goalie.gaa, goalie.saves) == (7, 14, 2.3, None)
    eht = s.scalar(
        select(m.SeasonStat).where(m.SeasonStat.athlete_id == ids["lara"], m.SeasonStat.source == "leijonat")
    )
    # shootout goal not counted, PIM and +/- from the roster lines, EP's team name
    assert (eht.games, eht.goals, eht.assists, eht.points, eht.penalty_minutes, eht.plus_minus) == (
        2,
        1,
        1,
        2,
        2,
        1,
    )
    assert eht.team_name == "Switzerland" and eht.team_kind == "national"
    g = s.scalar(
        select(m.SeasonStat).where(
            m.SeasonStat.athlete_id == ids["andrea"], m.SeasonStat.source == "leijonat"
        )
    )
    assert (g.games, g.saves, g.goals_against, g.save_pct) == (2, 50, 3, 0.943)
    jp = s.scalar(select(m.SeasonStat).where(m.SeasonStat.athlete_id == ids["mai"]))
    assert (jp.source, jp.league, jp.season, jp.games, jp.goals, jp.points) == (
        "iihf",
        "WC (W)",
        2024,
        None,
        1,
        1,
    )
    # a second run replaces, never duplicates
    current.refresh(s, SETTINGS, http=FakeHttp())
    assert s.query(m.SeasonStat).filter(m.SeasonStat.source != "eliteprospects").count() == len(rows)


def test_newer_rows_replace_ep_and_say_so(world):
    s, ids = world
    current.refresh(s, SETTINGS, http=FakeHttp())
    rows = queries.season_rows(s, ids["lara"], phase=None)
    by_league = {r.league: r for r in rows}
    assert by_league["SWHL A (W)"].source == "sihf"  # 25.10. beats EP 04.10.
    assert by_league["EHT (W)"].source == "leijonat"  # last game 30.10.
    assert by_league["NCAA (W)"].source == "eliteprospects"  # only EP covers it
    assert len([r for r in rows if r.league == "SWHL A (W)"]) == 1  # never counted twice
    tot = {t["league"]: t for t in queries.totals(s, ids["lara"], by="league")}
    assert tot["SWHL A (W)"]["games"] == 8

    res = stats_tool(s, person="Lara Stalder", season="2026/27")
    swhl = next(r for r in res["zeilen"] if r["league"] == "SWHL A (W)")
    assert swhl["quelle_link"].startswith("[sihf.ch, Stand 25.10.2026](https://www.sihf.ch/")
    assert swhl["ersetzt"] == "statt Elite Prospects (Stand 04.10.2026): GP 5 · G 6 · A 7 · PTS 13 · PIM 2"
    assert res["datenstand"].startswith(
        "Statistik: Leijonat-Spielberichte (von Karl gezählt) Stand 30.10.2026"
    )
    assert any("statt Elite Prospects (Stand 04.10.2026)" in h for h in res["hinweise"])

    html = statistik_html(stats_data(s, s.get(m.Athlete, ids["lara"])))
    assert "sihf.ch · Stand 25.10.2026" in html and "Quelle: Elite Prospects" in html


def test_counted_rows_win_even_before_the_ep_import_but_not_without_games(world):
    s, ids = world
    current.refresh(s, SETTINGS, http=FakeHttp())
    for r in s.scalars(select(m.SeasonStat).where(m.SeasonStat.source == "leijonat")):
        r.fetched_at = datetime(2026, 10, 3, 12, tzinfo=UTC)  # same games as the EP import (EP missed one)
    s.flush()
    rows = [r for r in queries.season_rows(s, ids["lara"], phase=None) if r.league == "EHT (W)"]
    assert [r.source for r in rows] == ["leijonat"]
    for r in rows:
        r.games = None  # a points-only count (IIHF/Wikipedia sheets) never beats EP's games
    s.flush()
    rows = [r for r in queries.season_rows(s, ids["lara"], phase=None) if r.league == "EHT (W)"]
    assert [r.source for r in rows] == ["eliteprospects"]


def test_middle_names_match(world):
    s, ids = world
    from klugscheisser.domain.matcher import AthleteMatcher

    assert current.match_name(AthleteMatcher(s), "Lara Maria Stalder") == ids["lara"]


def test_points_only_rows_say_games_are_missing(world):
    s, _ = world
    current.refresh(s, SETTINGS, fetch=False)
    res = stats_tool(s, person="Mai Probeyama")
    row = res["zeilen"][0]
    assert row["goals"] == 1 and "games" not in row
    assert "Spiele nicht erfasst" in row["hinweis"]


def test_incomplete_series_is_skipped(world):
    s, ids = world
    comp = m.Competition(
        sport_id=repo.get_sport(s, "icehockey_women").id,
        key="eht-x",
        name="Women's Euro Hockey Tour 2026/27, Lahti",
        kind="tournament",
    )
    s.add(comp)
    s.flush()
    s.add(
        m.Event(
            key="leijonat-99",
            kind="game",
            name="Schweiz – Tschechien",
            status="final",
            starts_at=datetime(2026, 10, 31, 15, tzinfo=UTC),
            competition_id=comp.id,
        )
    )
    s.flush()
    out = current.refresh(s, SETTINGS, fetch=False)
    assert any("Schweiz" in x for x in out["ausgelassen"])
    assert (
        s.scalar(
            select(m.SeasonStat).where(
                m.SeasonStat.athlete_id == ids["lara"], m.SeasonStat.source == "leijonat"
            )
        )
        is None
    )


CLASSIC = """
<table><caption>Individual Overall Skaters Statistics</caption>
<tr><th>#</th><th>Player</th><th>GP</th><th>Shots</th><th>Goals</th><th>Penalties</th><th>BLK</th><th>Bio Link</th></tr>
<tr><th>G</th><th>A</th><th>PTS</th><th>SH</th><th>SH%</th><th>+/-</th><th>PPG</th><th>SHG</th><th>FG</th><th>GWG</th>
<th>GTG</th><th>OTG</th><th>HTG</th><th>UAG</th><th>PN-PIM</th><th>MIN</th><th>MAJ</th><th>OTH</th></tr>
<tr><td>18</td><td><a>Wey, Ivana</a><span>Wey, Ivana</span></td><td>7</td><td>4</td><td>3</td><td>7</td><td>20</td><td>.200</td>
<td>+9</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>2-4</td><td>2</td><td>0</td>
<td>0</td><td>3</td><td>View Bio</td></tr>
<tr><td></td><td>Total</td><td>7</td><td>29</td><td>47</td><td>76</td></tr></table>
<table><caption>Individual Overall Goaltenders Statistics</caption>
<tr><th>#</th><th>Player</th><th>GP-GS</th><th>MIN</th><th>Goals</th><th>Saves</th><th>Record</th><th>Bio Link</th></tr>
<tr><th>GA</th><th>AVG</th><th>PPG</th><th>SHG</th><th>ENG</th><th>SV</th><th>SV%</th><th>W</th><th>L</th><th>T</th><th>SHO</th></tr>
<tr><td>1</td><td>Jönsson, Lisa</td><td>4-4</td><td>244:51</td><td>3</td><td>.74</td><td>1</td><td>0</td><td>0</td><td>96</td>
<td>.970</td><td>3</td><td>0</td><td>1</td><td>2</td><td>View Bio</td></tr></table>
<table><caption>Game-By-Game Team Statistics</caption>
<tr><td>10/23/26</td><td>at RIT</td><td>W</td><td>5-0</td></tr><tr><td>10/24/26</td><td>at RIT</td><td>W</td><td>8-1</td></tr>
<tr><td>11/01/26</td><td>Maine</td><td></td><td></td></tr></table>
"""
NUXT = """
<table><tr><th>#</th><th>Player</th><th>GP</th><th>G</th><th>A</th><th>PTS</th><th>SH</th><th>SH%</th><th>+/-</th>
<th>PN-PIM</th></tr><tr><td>11</td><td><a>Zimmermann, Laura</a></td><td>10</td><td>1</td><td>1</td><td>2</td><td>15</td>
<td>.067</td><td>0</td><td>2-4</td></tr></table>
<table><tr><th>Date</th><th>Opponent</th><th>W/L</th></tr><tr><td>10/24/2026</td><td>at Ohio State</td><td>L</td></tr></table>
"""


def test_college_stats_pages_both_layouts():
    from klugscheisser.collectors.results.college_stats import parse_stats_page, stats_url

    assert stats_url("https://nuhuskies.com/sports/womens-ice-hockey/roster") == (
        "https://nuhuskies.com/sports/womens-ice-hockey/stats"
    )
    rows, last = parse_stats_page(CLASSIC)
    wey = next(r for r in rows if r["name"] == "Ivana Wey")
    assert (wey["games"], wey["goals"], wey["assists"], wey["points"], wey["penalty_minutes"]) == (
        7,
        4,
        3,
        7,
        4,
    )
    assert wey["plus_minus"] == 9
    g = next(r for r in rows if r.get("goalie"))
    assert (g["name"], g["games"], g["saves"], g["goals_against"], g["save_pct"], g["shutouts"]) == (
        "Lisa Jönsson",
        4,
        96,
        3,
        0.97,
        2,
    )
    assert last.date().isoformat() == "2026-10-24"  # the unplayed 01.11. game does not count
    rows, last = parse_stats_page(NUXT)
    assert rows == [
        {
            "name": "Laura Zimmermann",
            "games": 10,
            "goals": 1,
            "assists": 1,
            "points": 2,
            "plus_minus": 0,
            "penalty_minutes": 4,
        }
    ]
    assert last.date().isoformat() == "2026-10-24"
