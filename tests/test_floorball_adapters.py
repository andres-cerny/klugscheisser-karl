"""2026-11-24: floorball adapters (collectors/floorball): Swiss Unihockey render-engine blocks and IFF games from
Wikipedia. Offline: recorded, trimmed pages under tests/fixtures/floorball/."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.floorball import iff, run
from klugscheisser.collectors.floorball import swissunihockey as su
from klugscheisser.collectors.floorball.common import CappedHttp, RequestCapReached, current_season
from klugscheisser.db import models as m

FX = Path(__file__).parent / "fixtures" / "floorball"
TODAY = date(2026, 11, 24)


def fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def su_handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    if request.url.host == "en.wikipedia.org":
        year = 2026 if "2026" in q.get("page", "") else 2024
        return httpx.Response(200, text=fx(f"wiki_wfc_{year}.json"))
    block = q.get("block_type")
    pages = {
        "player_details": "su_player_details_430489.html",
        "player_statistics": "su_player_statistics_430489.html",
        "national_players": "su_national_players_selection1.html",
        "national_player_statistics": "su_national_player_statistics_464607.html",
        "topscorers_su": "su_topscorers_lupl.html",
        "team_players": "su_team_players_429603.html",
    }
    if block == "topscorers_su" and q.get("league") != "24":
        return httpx.Response(200, text="<div class='su-spieldaten'></div>")
    if block in ("player_details", "player_statistics") and q.get("player_id") != "430489":
        return httpx.Response(200, text="<div class='su-spieldaten'></div>")
    return httpx.Response(200, text=fx(pages[block])) if block in pages else httpx.Response(404)


def fake_http(max_requests: int = 50) -> CappedHttp:
    client = httpx.Client(transport=httpx.MockTransport(su_handler))
    return CappedHttp(delay_s=0, max_requests=max_requests, client=client)


# --- Swiss Unihockey parsing --------------------------------------------------------------------------------


def test_player_details_in_fixed_order():
    p = su.parse_player_details(fx("su_player_details_430489.html"), "430489")
    assert (p.name, p.club, p.number, p.position) == ("Jan Zaugg", "Floorball Köniz Bern", "61", "Stürmer")
    assert (p.birth_year, p.height_cm, p.weight_kg) == (1997, 176, 80)
    assert p.license == "Herren Aktive GF L-UPL" and p.position_code == "F"
    assert p.url == "https://www.swissunihockey.ch/de/player-detail?player_id=430489"


def test_player_statistics_rows():
    name, rows = su.parse_player_statistics(fx("su_player_statistics_430489.html"))
    assert name == "Jan Zaugg" and len(rows) == 18
    top = rows[0]
    assert (top.season, top.league, top.team) == (2026, "L-UPL", "Floorball Köniz Bern")
    assert (top.games, top.goals, top.assists, top.points, top.penalty_minutes) == (10, 15, 5, 20, 2)
    youth = [r for r in rows if r.youth]
    assert youth and all(r.league.startswith("U") for r in youth)


def test_stints_split_gaps_and_youth_and_keep_current_open():
    _, rows = su.parse_player_statistics(fx("su_player_statistics_430489.html"))
    specs = su.stint_specs(rows, TODAY)
    clubs = [(s["team_name"], s["season_from"], s["season_to"]) for s in specs if s["kind"] == "club"]
    # 2019/20 and 2020/21 are missing at Swiss Unihockey (abroad): two Köniz stints, then the renamed club, open
    assert clubs == [
        ("Floorball Köniz", 2015, 2018),
        ("Floorball Köniz", 2021, 2022),
        ("Floorball Köniz Bern", 2023, None),
    ]
    assert any(s["kind"] == "youth" for s in specs)
    assert all(s["quote"].startswith("Spielerstatistik Nationalliga:") for s in specs)


def test_player_lists_scorers_roster_national_pool():
    scorers = su.parse_player_list(fx("su_topscorers_lupl.html"))
    zaugg = next(r for r in scorers if r.name == "Jan Zaugg")
    assert zaugg.player_id == "430489" and zaugg.team == "Floorball Köniz Bern" and zaugg.team_id == "432039"
    roster = su.parse_player_list(fx("su_team_players_429603.html"))
    assert roster[0].team == "Zug United" and roster[0].position == "Goalie" and roster[0].birth_year == 2003
    pool = su.parse_player_list(fx("su_national_players_selection1.html"))
    assert {r.name for r in pool} >= {"Jan Zaugg", "Jan Bürki"}
    assert all(r.national_player_id and r.player_id is None for r in pool)


def test_national_totals():
    (t,) = su.parse_national_statistics(fx("su_national_player_statistics_464607.html"), "464607")
    assert (t.selection, t.games, t.goals, t.assists, t.points) == (
        "Herren - Nationalmannschaft",
        93,
        66,
        61,
        127,
    )


def test_directory_finds_by_name_and_birth_year_and_falls_back_to_rosters():
    http = fake_http()
    d = su.Directory(http, seasons=(2026,))
    assert d.find("Jan Zaugg").player_id == "430489"
    assert http.requests == 1
    assert d.find("Zaugg Jan").player_id == "430489"  # reversed order
    assert d.find("Jari Portmann", 2003).player_id == "459038"  # goalie: only in the roster
    assert d.find("Jari Portmann", 1990) is None  # birth year does not fit
    assert d.find("Niemand Unbekannt") is None


def test_request_cap():
    http = fake_http(max_requests=1)
    su.fetch_scorers(http)
    with pytest.raises(RequestCapReached):
        su.fetch_scorers(http)


def test_current_season():
    assert current_season(date(2026, 11, 24)) == 2026 and current_season(date(2027, 3, 1)) == 2026


# --- Swiss Unihockey writing ---------------------------------------------------------------------------------


def _athlete(s: Session, name: str, nationality: str = "SUI", **kw) -> m.Athlete:
    sport = s.scalar(select(m.Sport).where(m.Sport.key == "floorball_men"))
    if sport is None:
        sport = m.Sport(key="floorball_men", name="Unihockey Männer")
        s.add(sport)
        s.flush()
    first, last = name.split(" ", 1)
    a = m.Athlete(
        sport_id=sport.id,
        full_name=name,
        first_name=first,
        last_name=last,
        name_normalized=name.lower(),
        nationality=nationality,
        **kw,
    )
    s.add(a)
    s.flush()
    return a


def test_import_dry_run_writes_nothing(db):
    with Session(db) as s:
        a = _athlete(s, "Jan Zaugg")
        res = su.import_for_athletes(s, [a], dry_run=True, http=fake_http(), today=TODAY)
        assert res["players"] == 1 and res["season_stats_added"] == 18 and res["stints_added"] == 4
        assert s.scalar(select(m.SeasonStat.id)) is None and a.federation_id is None


def test_import_writes_stats_and_stints_and_is_idempotent(db):
    with Session(db) as s:
        a = _athlete(s, "Jan Zaugg", birth_date=date(1997, 3, 1))
        res = su.import_for_athletes(s, [a], dry_run=False, http=fake_http(), today=TODAY)
        s.commit()
        assert res["season_stats_added"] == 18 and res["not_found"] == []
        assert a.federation_id == "su:430489" and a.position == "F"
        stats = list(s.scalars(select(m.SeasonStat).where(m.SeasonStat.athlete_id == a.id)))
        assert len(stats) == 18 and {x.source for x in stats} == {"swissunihockey"}
        cur = next(x for x in stats if x.season == 2026)
        assert (cur.league, cur.games, cur.goals, cur.points, cur.team_kind) == ("L-UPL", 10, 15, 20, "club")
        assert cur.source_url.endswith("player_id=430489")
        stints = list(s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == a.id)))
        assert {x.source for x in stints} == {"federation"} and len(stints) == 4
        assert all(x.quote and x.source_url for x in stints)
        # second run: found by federation_id, nothing new
        again = su.import_for_athletes(s, [a], dry_run=False, http=fake_http(), today=TODAY)
        assert again.get("season_stats_added", 0) == 0 and again.get("stints_added", 0) == 0
        assert again["requests"] == 2  # no directory lookups


def test_import_keeps_other_federation_stints(db):
    with Session(db) as s:
        a = _athlete(s, "Jan Zaugg")
        s.add(
            m.CareerStint(
                athlete_id=a.id,
                team_name="Pixbo Wallenstam",
                kind="club",
                season_from=2019,
                season_to=2020,
                source="federation",
                source_url="https://example.org/other",
                quote="x",
            )
        )
        s.flush()
        su.import_for_athletes(s, [a], dry_run=False, http=fake_http(), today=TODAY)
        names = {
            x.team_name for x in s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == a.id))
        }
        assert "Pixbo Wallenstam" in names


def test_birth_year_mismatch_is_not_imported(db):
    with Session(db) as s:
        a = _athlete(s, "Jan Zaugg", federation_id="su:430489", birth_date=date(2001, 1, 1))
        res = su.import_for_athletes(s, [a], dry_run=False, http=fake_http(), today=TODAY)
        assert res["birth_year_mismatch"] == 1 and s.scalar(select(m.SeasonStat.id)) is None


def test_run_enrich_combines_sources(db):
    with Session(db) as s:
        a = _athlete(s, "Jan Zaugg")
        b = _athlete(s, "Erik Svensson", nationality="SWE")
        out = run.enrich(
            s,
            [a.id, b.id],
            sources=("swissunihockey", "swissunihockey_nati", "iff", "nope"),
            dry_run=True,
            http=fake_http(),
            wiki_http=fake_http(),
        )
        assert out["swissunihockey_players"] == 1
        assert out["swissunihockey_not_found_sui"] == []
        assert "93 Sp., 66 T, 61 A, 127 P" in out["swissunihockey_nati"]["Jan Zaugg"]
        assert out["iff_wfc_2026_spiele"] == 2 and out["iff_wfc_2024_spiele"] == 2
        assert out["unbekannte_quellen"] == ["nope"]


# --- IFF games from Wikipedia ----------------------------------------------------------------------------------


def _games(year: int):
    d = json.loads(fx(f"wiki_wfc_{year}.json"))["parse"]
    return iff.parse_wfc_page(d["wikitext"], d["revid"], iff.WFCS[year])


def test_wfc_2026_schedule_with_iff_links_and_placeholders_skipped():
    games = _games(2026)
    assert len(games) == 2  # the quarterfinal "A2 v WPO2" is skipped
    g = next(x for x in games if x.raw["away_code"] == "SUI")
    assert (g.home, g.away, g.status, g.score) == ("Finnland", "Schweiz", "scheduled", None)
    assert g.starts_at.isoformat() == "2026-12-05T17:00:00+00:00"  # 19:00 in Tampere
    assert g.url == "https://app.floorball.sport/game-view/8673" and g.external_id == "iff-8673"
    assert g.venue == "Hakametsä Ice Hall, Tampere" and g.sport == "floorball_men"
    seed = iff.seed_games(games, 2026)
    row = next(x for x in seed if x["away"] == "SUI")
    assert row["key"] == "wfc-2026-8673" and row["venue"] == "Hakametsä Ice Hall" and row["city"] == "Tampere"
    assert row["utc"] == "2026-12-05T17:00:00Z" and row["source"] == "IFF"


def test_wfc_2024_results():
    games = _games(2024)
    g = next(x for x in games if x.raw["away_code"] == "SUI")
    assert (g.home, g.score, g.period_scores, g.status) == (
        "Deutschland",
        "2:8",
        ["0:1", "1:6", "1:1"],
        "final",
    )
    assert g.raw["spectators"] == "2038" and g.raw["phase"] == "Group A"
    assert g.url.endswith("magazinegameview/7168")


def test_overtime_score():
    text = (
        "===Final===\n{{Ice hockey box\n|date = 15 December 2024\n|time = 17:00\n|team1 = {{fl-rt|SWE}}\n"
        "|team2 = '''{{flb|FIN}}'''\n|score = 4–5 OT\n|periods = (2–0, 2–1, 0–3, 0–1)\n"
        "|reference = https://app.floorball.sport/#/magazinegameview/7214\n|stadium = Malmö Arena, [[Malmö]]\n}}\n"
    )
    (g,) = iff.parse_wfc_page(text, 1, iff.WFCS[2024])
    assert g.score == "4:5 n.V." and g.raw["phase"] == "Final"
