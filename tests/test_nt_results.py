"""National-team games: Leijonat (Finnish federation JSON) + sihf.ch team pages, de-duplication against other
sources, tournament containers, and the answer/MCP paths that show them. Recorded fixtures only, no network."""

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.answer import lookups
from klugscheisser.collectors.results import iihf, leijonat, sihf_nt
from klugscheisser.collectors.results.base import TEAM_DE, PoliteHttp, season_label, season_of, slug
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest.results import enabled_collectors, ingest_results
from klugscheisser.ingest.runner import build_context
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.present import render_markdown
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/results")
ZRH = ZoneInfo("Europe/Zurich")
SINCE = datetime(2026, 10, 1, tzinfo=UTC)  # older finals (Lahti 30.9.) must still come in


def load(name):
    return json.loads((FIX / name).read_text())


def text(name):
    return (FIX / name).read_text()


# --- helpers ------------------------------------------------------------------------------------------


def test_shared_helpers():
    assert iihf.TEAM_DE is TEAM_DE and iihf.team_name("SUI") == "Schweiz"
    assert slug("Södertälje") == "sodertalje"
    assert season_label(2027) == "2026/27"
    assert season_of(datetime(2026, 10, 3).date()) == 2027 and season_of(datetime(2026, 2, 3).date()) == 2026


def test_subserie_names_give_stable_competitions_and_containers():
    info = leijonat.subserie_info("EHT: Lidl Hockey Games 28.-31.10.2026 Södertälje, SWE", 2027, 3528)
    assert info["competition"] == "Women's Euro Hockey Tour 2026/27, Södertälje"
    assert info["tournament"] == {
        "prefix": "eht",
        "key": "eht-2026-sodertalje",  # the key the WCh branch seeds by hand: same row
        "name": "Women's Euro Hockey Tour, Södertälje",
        "city": "Södertälje (SWE)",
    }
    lahti = leijonat.subserie_info("EHT: 4 Nations Tournament 30.9-3.10.2026 Lahti, FIN", 2027, 4740)
    assert lahti["tournament"]["key"] == "eht-2026-lahti"  # the seeded container
    wch = leijonat.subserie_info("Women's World Championship 6.-16.11.2026 Esbjerg, DEN Group A", 2027, 4640)
    assert wch["competition"] == "IIHF Frauen-WM 2026" and wch["tournament"]["key"] == "wch-2026"
    other = leijonat.subserie_info("Friendly games", 2027, 9)
    assert (
        other["competition"] == "Frauen-Länderspiele 2026/27: Friendly games" and other["tournament"] is None
    )


# --- Leijonat -----------------------------------------------------------------------------------------


def test_leijonat_games_times_are_finnish_local_time():
    games = leijonat.parse_games(load("leijonat_games_4740.json"), 2027)
    assert len(games) == 6
    swe_sui = games[0]
    assert (swe_sui.home, swe_sui.away, swe_sui.score, swe_sui.status) == (
        "Schweden",
        "Schweiz",
        "5:0",
        "final",
    )
    # feed: 12:30 (TimeZone Europe/Helsinki) = 11:30 Swiss time = 09:30 UTC
    assert swe_sui.starts_at.astimezone(UTC) == datetime(2026, 9, 30, 9, 30, tzinfo=UTC)
    assert swe_sui.starts_at.astimezone(ZRH).strftime("%H:%M") == "11:30"
    assert swe_sui.period_scores == ["0:0", "3:0", "2:0"]
    assert swe_sui.url == "https://tulospalvelu.leijonat.fi/game?season=2027&gameid=2713356&lang=en"
    so = next(g for g in games if g.external_id == "2713360")
    assert so.score == "3:2 n.P." and len(so.period_scores) == 4

    sod = leijonat.parse_games(load("leijonat_games_3528.json"), 2027)
    assert [g.status for g in sod] == ["scheduled"] * 6 and all(g.score is None for g in sod)
    fin_sui = sod[0]
    # feed says TimeZone Europe/Stockholm, but 15:00 is Finnish time: 14:00 Swiss time (SIHF programme)
    assert fin_sui.starts_at.astimezone(ZRH).strftime("%d.%m. %H:%M") == "28.10. 14:00"
    assert fin_sui.raw["tournament"]["key"] == "eht-2026-sodertalje"
    assert fin_sui.competition == "Women's Euro Hockey Tour 2026/27, Södertälje"


def test_leijonat_details_goals_shootout_goalies_no_personal_data():
    game = next(
        g for g in leijonat.parse_games(load("leijonat_games_4740.json"), 2027) if g.external_id == "2713360"
    )
    leijonat.apply_details(game, load("leijonat_report_2713360.json"), load("leijonat_summary_2713360.json"))
    assert game.raw["detail"] is True
    assert game.venue == "Webasto Areena, Lahti"
    assert [x["score"] for x in game.raw["goals"]] == ["0:1", "0:2", "1:2", "2:2", "3:2"]
    enzler = next(p for p in game.participants if p.name == "Rahel Enzler" and p.role == "scorer")
    assert (enzler.team, enzler.detail) == ("Schweiz", "1:2, 56:56 (ohne Torhüterin)")
    winner = game.raw["goals"][-1]
    assert (winner["scorer"], winner["time"]) == ("Leoni Balzer", "Penalty-Schiessen")
    goalies = {p.name: p.detail for p in game.participants if p.role == "goalie"}
    assert goalies == {
        "Andrea Brändli": "41 Paraden bei 43 Schüssen",
        "Klara Peslarova": "8 Paraden bei 10 Schüssen",
    }
    lineup = [p for p in game.participants if p.role == "lineup"]
    assert len(lineup) > 30 and {"Schweiz", "Tschechien"} == {p.team for p in lineup}
    dumped = json.dumps([game.raw, [p.__dict__ for p in game.participants]], ensure_ascii=False)
    assert "1999" not in dumped and "example.invalid" not in dumped and "DateOfBirth" not in dumped


def _leijonat_routes():
    def subseries(request):
        assert request.headers["accept"] == "application/json"
        return httpx.Response(200, json=load("leijonat_subseries.json"))

    def games(request):
        sub = request.url.params["subSerieId"]
        assert request.url.params["gamedays"] == "4"
        name = {"4740": "leijonat_games_4740.json", "3528": "leijonat_games_3528.json"}.get(sub)
        return httpx.Response(200, json=load(name) if name else [])

    def detail(kind):
        def handler(request):
            gid = request.url.params["gameid"]
            path = FIX / f"leijonat_{kind}_{gid}.json"
            return httpx.Response(200, json=load(path.name)) if path.exists() else httpx.Response(404)

        return handler

    respx.get(f"{leijonat.API}/serie/helpers/getsubseries").mock(side_effect=subseries)
    games_route = respx.get(f"{leijonat.API}/helpers/getgames").mock(side_effect=games)
    report = respx.get(f"{leijonat.API}/gamereport/getgamereportdata").mock(side_effect=detail("report"))
    respx.get(f"{leijonat.API}/game/helpers/getsummary").mock(side_effect=detail("summary"))
    return games_route, report


@respx.mock
def test_leijonat_collector_skips_wch_and_known_finals():
    games_route, report = _leijonat_routes()
    c = leijonat.LeijonatCollector(2027, http=PoliteHttp(0), skip="world championship")
    c.known_final = {"2713357", "2713358", "2713361"}
    games = c.collect(since=SINCE)
    assert games_route.call_count == 3  # Kloten, Lahti, Södertälje; both WCh groups skipped
    assert len(games) == 12  # Lahti 6 (finals older than `since` included) + Södertälje 6
    assert report.call_count == 3  # 2713356, 2713359, 2713360: the other Lahti finals are known
    only_sui = c.collect(since=SINCE, teams=["SUI"])
    assert {g.external_id for g in only_sui} >= {"2713356", "2757709"} and len(only_sui) == 6


@respx.mock
def test_national_team_collectors_never_raise():
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(500))
    assert leijonat.LeijonatCollector(2027, http=PoliteHttp(0)).collect() == []
    assert sihf_nt.SihfNtCollector(http=PoliteHttp(0)).collect() == []


# --- sihf.ch team pages -------------------------------------------------------------------------------


def test_sihf_nt_results_and_programme():
    results = sihf_nt.parse_results(text("sihf_nt_results.html"))
    assert len(results) == 15  # first page only
    assert results[0]["score"] == "3:2 n.P." and (results[0]["home_code"], results[0]["away_code"]) == (
        "SUI",
        "CZE",
    )
    programme = sihf_nt.parse_programme(text("sihf_nt_programm.html"))
    assert len(programme) == 13
    games = {g.external_id: g for g in sihf_nt.merge(sihf_nt.NtTeam(), results, programme)}
    swe_sui = games["sui-2026-09-30-swe-sui"]
    # programme column says "Zeit (MESZ)": Swiss local time -> same instant as Leijonat's 12:30 Helsinki
    assert swe_sui.starts_at.astimezone(UTC) == datetime(2026, 9, 30, 9, 30, tzinfo=UTC)
    assert (swe_sui.home, swe_sui.away, swe_sui.score) == ("Schweden", "Schweiz", "5:0")
    wch = games["sui-2026-11-06-sui-usa"]
    assert wch.status == "scheduled" and wch.starts_at.astimezone(UTC) == datetime(
        2026, 11, 6, 18, 0, tzinfo=UTC
    )
    assert wch.competition == "IIHF Frauen-WM 2026"
    friendly = games["sui-2026-04-25-ger-sui"]
    assert (friendly.home, friendly.score, friendly.competition) == (
        "Deutschland",
        "5:3",
        "Frauen-Länderspiele 2025/26",
    )
    assert friendly.raw["tournament"] is None and friendly.raw["time_known"] is False
    sod = games["sui-2026-10-28-fin-sui"]
    assert sod.raw["tournament"]["key"] == "eht-2026-sodertalje" and sod.raw["city"] == "Södertälje (SWE)"
    assert friendly.url.endswith("/resultate-statistiken/#sui-2026-04-25-ger-sui")


def test_leijonat_season_follows_the_date_unless_set():
    from datetime import date

    from klugscheisser.ingest.results import current_season

    assert current_season(today=date(2026, 7, 31)) == 2026
    assert current_season(today=date(2026, 8, 1)) == 2027
    assert current_season(today=date(2027, 3, 1)) == 2027
    auto = enabled_collectors(Settings(_env_file=None, leijonat_enabled=True))
    assert next(c for c in auto if c.name == "leijonat").season == current_season()
    fixed = enabled_collectors(Settings(_env_file=None, leijonat_enabled=True, leijonat_season=2026))
    assert next(c for c in fixed if c.name == "leijonat").season == 2026
    assert leijonat.LeijonatCollector().season == current_season(tz="Europe/Helsinki")


def test_enabled_collectors_include_national_teams():
    on = dict(
        sihf_data_enabled=True,
        iihf_realtime_enabled=True,
        pwhl_enabled=True,
        sdhl_enabled=True,
        leijonat_enabled=True,
        sihf_nt_enabled=True,
    )
    cs = enabled_collectors(Settings(_env_file=None, **on))
    assert [c.name for c in cs] == ["sihf", "iihf", "pwhl", "sdhl", "leijonat", "sihfnt"]
    assert cs[4].skip is not None  # WCh comes from IIHF while it is on
    no_iihf = enabled_collectors(Settings(_env_file=None, **{**on, "iihf_realtime_enabled": False}))
    assert next(c for c in no_iihf if c.name == "leijonat").skip is None


# --- ingest: events, items, de-duplication, containers -------------------------------------------------


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _sihf_nt_routes():
    team = sihf_nt.NtTeam()
    respx.get(team.results_url).mock(return_value=httpx.Response(200, text=text("sihf_nt_results.html")))
    respx.get(team.programme_url).mock(return_value=httpx.Response(200, text=text("sihf_nt_programm.html")))


def _iihf_routes():
    respx.get(f"{iihf.API}/GetLatestScoresStateForEvents/773").mock(
        return_value=httpx.Response(200, json=load("iihf_event_773.json"))
    )


def _run(s, *collectors):
    report = ingest_results(build_context(s, Settings(_env_file=None)), list(collectors), since=SINCE)
    assert not report.errors, report.errors
    return report


def _key(s, key):
    return s.scalar(select(m.Event).where(m.Event.key == key))


@respx.mock
def test_ingest_national_team_games_and_containers(world):
    _leijonat_routes()
    _sihf_nt_routes()
    _iihf_routes()
    with Session(world) as s:
        sui = repo.get_team(s, "sui-women")
        _run(s, iihf.IihfCollector(http=PoliteHttp(0)))
        r1 = _run(s, leijonat.LeijonatCollector(2027, http=PoliteHttp(0), skip="world championship"))
        assert (r1.events_new, r1.items_new) == (12, 2)  # report fixtures for two games only

        ev = _key(s, "leijonat-2713356")
        assert (ev.name, ev.score, ev.status, ev.source) == ("Schweden – Schweiz", "5:0", "final", "Leijonat")
        assert ev.away_team_id == sui.id and ev.city == "Lahti (FIN)"
        assert ev.competition.name == "Women's Euro Hockey Tour 2026/27, Lahti"
        assert ev.competition.kind == "tournament" and ev.competition.season == "2026/27"
        item = s.scalar(select(m.Item).where(m.Item.url == _key(s, "leijonat-2713360").source_url))
        assert item.source_type == "official_result" and item.title == "Schweiz – Tschechien 3:2 n.P."
        assert (
            "3:2 Penalty-Schiessen Leoni Balzer" in item.text and "Ort: Webasto Areena, Lahti." in item.text
        )

        # the seeded Lahti container gets its end date and stays the only Lahti container
        lahti = _key(s, "eht-2026-lahti")
        assert lahti.ends_at.astimezone(ZRH).strftime("%d.%m.%Y %H:%M") == "03.10.2026 23:59"
        assert lahti.starts_at.astimezone(UTC).date().isoformat() == "2026-09-28"  # seeded camp start kept
        assert lahti.status == "final" and lahti.competition_id == ev.competition_id
        # Södertälje: created from the games (start/end, city, teams)
        sod = _key(s, "eht-2026-sodertalje")
        assert (sod.kind, sod.city, sod.status) == ("tournament", "Södertälje (SWE)", "scheduled")
        assert sod.starts_at.astimezone(ZRH).date().isoformat() == "2026-10-28"
        assert sod.ends_at.astimezone(ZRH).date().isoformat() == "2026-10-31"
        teams = set(s.scalars(select(m.EventTeam.team_id).where(m.EventTeam.event_id == sod.id)))
        assert len(teams) == 4 and sui.id in teams
        assert s.scalar(select(func.count()).select_from(m.Event).where(m.Event.key.like("eht-%"))) == 2

        # sihf.ch: EHT and WCh games are skipped (Leijonat / IIHF have them); friendlies and Olympics come in
        r2 = _run(s, sihf_nt.SihfNtCollector(http=PoliteHttp(0)))
        assert r2.duplicates_skipped == 6 + 4  # Lahti 3 + Södertälje 3, WCh 4 (IIHF schedule)
        assert _key(s, "sihfnt-sui-2026-10-03-sui-cze") is None
        friendly = _key(s, "sihfnt-sui-2026-04-25-ger-sui")
        assert (friendly.score, friendly.home_team_id) == ("5:3", repo.get_team(s, "ger-women").id)
        assert friendly.competition.name == "Frauen-Länderspiele 2025/26"
        oly = _key(s, "sihfnt-sui-2026-02-19-sui-swe")
        assert oly.competition.key == "oly-2026" and oly.score == "2:1 n.V."
        assert s.scalar(select(m.Item).where(m.Item.url == friendly.source_url)).title == (
            "Deutschland – Schweiz 5:3"
        )

        # second run of everything: nothing new, nothing doubled
        before = s.scalar(select(func.count()).select_from(m.Event))
        r3 = _run(s, leijonat.LeijonatCollector(2027, http=PoliteHttp(0), skip="world championship"))
        r4 = _run(s, sihf_nt.SihfNtCollector(http=PoliteHttp(0)))
        assert (r3.events_new, r3.items_new, r4.events_new, r4.items_new) == (0, 0, 0, 0)
        assert s.scalar(select(func.count()).select_from(m.Event)) == before


@respx.mock
def test_better_source_takes_over_a_fallback_row(world):
    _leijonat_routes()
    _sihf_nt_routes()
    with Session(world) as s:
        _run(s, sihf_nt.SihfNtCollector(http=PoliteHttp(0)))  # Leijonat was down: sihf.ch stored the game
        old = _key(s, "sihfnt-sui-2026-10-03-sui-cze")
        old_id, old_url = old.id, old.source_url
        assert s.scalar(select(m.Item).where(m.Item.url == old_url)) is not None

        r = _run(s, leijonat.LeijonatCollector(2027, http=PoliteHttp(0), skip="world championship"))
        assert r.adopted == 6  # the Swiss games at Lahti (final) and Södertälje (scheduled)
        ev = _key(s, "leijonat-2713360")
        assert ev.id == old_id and ev.source == "Leijonat" and ev.score == "3:2 n.P."
        assert _key(s, "sihfnt-sui-2026-10-03-sui-cze") is None
        assert s.scalar(select(m.Item).where(m.Item.url == old_url)) is None  # score-only item replaced
        assert s.scalar(select(m.Item).where(m.Item.url == ev.source_url)) is not None
        swiss_lahti = s.scalar(
            select(func.count())
            .select_from(m.Event)
            .where(m.Event.kind == "game", m.Event.city == "Lahti (FIN)")
        )
        assert swiss_lahti == 6


# --- answers and MCP ----------------------------------------------------------------------------------


@pytest.fixture
def stored(world):
    with respx.mock:
        _leijonat_routes()
        _sihf_nt_routes()
        _iihf_routes()
        with Session(world) as s:
            _run(s, iihf.IihfCollector(http=PoliteHttp(0)))
            _run(s, leijonat.LeijonatCollector(2027, http=PoliteHttp(0), skip="world championship"))
            _run(s, sihf_nt.SihfNtCollector(http=PoliteHttp(0)))
            s.commit()
    return world


def test_last_and_next_swiss_game(stored):
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    with Session(stored) as s:
        last = lookups.team_game_inputs(s, "Wie ist das letzte Spiel der Schweiz ausgegangen?", now=now)
        direct = last.docs[0].extra["direct"]
        assert direct["rows"][0][:3] == ["03.10.2026 13:00", "Schweiz – Tschechien", "3:2 n.P."]
        nxt = lookups.team_game_inputs(s, "Wann spielt die Schweiz das nächste Mal?", now=now)
        assert nxt.docs[0].extra["direct"]["rows"][0][:2] == ["28.10.2026 14:00", "Finnland – Schweiz"]


def test_swiss_results_at_lahti_and_sodertalje_schedule(stored):
    with Session(stored) as s:
        res = lookups.direct_lookup(s, "Wie hat die Schweiz in Lahti gespielt?", [])
        rows = res.docs[0].extra["direct"]["rows"]
        assert [r[1:3] for r in rows] == [
            ["Schweden – Schweiz", "5:0"],
            ["Finnland – Schweiz", "4:1"],
            ["Schweiz – Tschechien", "3:2 n.P."],
        ]
        # the SDHL club of the same name (seeded) must not narrow the schedule to its (no) games
        assert repo.get_team(s, "sodertalje-sk") is not None
        sched = lookups.direct_lookup(s, "Spielplan Södertälje", [])
        rows = sched.docs[0].extra["direct"]["rows"]
        assert len(rows) == 6 and rows[0][:3] == ["28.10.2026 14:00", "Finnland – Schweiz", "geplant"]
        assert lookups.tournament_games_inputs(s, "Wie hat Lisa Rüedi in Lahti gespielt?", [object()]) is None
        # "Turnier" is a WM-sheet word: a tournament named by its city still gets its own games
        turnier = lookups.direct_lookup(s, "Wie hat die Schweiz am Turnier in Lahti gespielt?", [])
        assert len(turnier.docs[0].extra["direct"]["rows"]) == 3
        # the WM has a fact sheet: its games come from tournament.py, not from this lookup
        assert lookups.tournament_games_inputs(s, "Spielplan Herning", []) is None


def test_mcp_get_event_lists_tournament_games(stored, frozen_now):
    frozen_now(datetime(2026, 11, 4, 12, tzinfo=UTC), t)  # list_events shows a window around "now"
    with Session(stored) as s:
        out = t.get_event(s, "Södertälje")
        assert out["anlass"]["key"] == "eht-2026-sodertalje"
        assert [g["datum"] for g in out["spiele"]][:2] == ["28.10.2026 14:00", "28.10.2026 18:00"]
        md = render_markdown(out)
        assert "**Spiele** (Schweizer Zeit)" in md and "28.10.2026 14:00: Finnland – Schweiz geplant" in md
        lahti = t.get_event(s, "Euro Hockey Tour Lahti")  # the tournament, not one of its games
        assert lahti["anlass"]["key"] == "eht-2026-lahti"
        assert any(g["spiel"] == "Schweden – Schweiz" and g["resultat"] == "5:0" for g in lahti["spiele"])
        wm = t.get_event(s, "WM")  # fact sheet: schedule in `turnier`, `spiele` only for played games
        assert wm["turnier"] and wm["spiele"] == []
        listed = {e["key"] for e in t.list_events(s)["anlaesse"]}
        assert "eht-2026-sodertalje" in listed
